"""Three-layer kill switch.

Layer 1 — POSIX signals (SIGINT / SIGTERM). ``install_signal_handlers`` wires
the process-wide ``asyncio.Event`` that the scheduler awaits. One signal
requests a graceful drain; a second signal forces an immediate exit.

Layer 2 — Flag file. A file at ``pause_flag_path`` (checked every second by
:func:`watch_flag_file`) pauses the *autopilot*. Webhooks still ack 202; the
scheduler tick idles; LLM and autonomous connector hot paths raise
:class:`KillSwitchTripped`. The pause flag is **not** meant to block explicit
human-initiated actions (HITL send-draft, operator connectivity tests) — the
pilot can still fly manually while autopilot is off. Those call sites open a
:func:`allow_manual_send` context so the guard sees the pause as cleared for
the duration of the action. Shutdown (layer 1) still aborts either way.

Layer 3 — CLI wrappers in ``autosdr.cli`` (``pause`` / ``resume`` / ``stop``)
that manipulate the flag file or send a signal to the PID recorded at startup.
"""

from __future__ import annotations

import asyncio
import contextlib
import contextvars
import logging
import os
import signal
from collections.abc import Iterator
from pathlib import Path

from autosdr.config import get_settings

logger = logging.getLogger(__name__)


class KillSwitchTripped(RuntimeError):
    """Raised from hot-path helpers when a pause or shutdown is in effect."""


# Module-level shutdown state. ``_shutdown_event`` is an ``asyncio.Event`` that
# wakes any task awaiting it; ``_hard_stop`` is a plain flag checked by
# synchronous hot paths (LLM / connector callers).
_shutdown_event: asyncio.Event | None = None
_signals_installed = False
_hard_stop = False

# Per-task opt-out for the *pause* check only. Explicit human-initiated
# actions set this True via :func:`allow_manual_send` so ``is_paused()``
# ignores the flag file while the hard-stop (SIGTERM / lifespan shutdown)
# still wins. Using a ``ContextVar`` means each asyncio task — each FastAPI
# request handler — gets its own copy and we don't have to thread a keyword
# argument through every connector implementation.
_bypass_pause_flag: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "autosdr.killswitch.bypass_pause_flag", default=False
)


def _get_or_create_event() -> asyncio.Event:
    """Lazily create the asyncio.Event on the running loop."""

    global _shutdown_event
    if _shutdown_event is None:
        _shutdown_event = asyncio.Event()
    return _shutdown_event


def shutdown_event() -> asyncio.Event:
    """Return the shared shutdown event (creating it if needed)."""

    return _get_or_create_event()


def install_signal_handlers(loop: asyncio.AbstractEventLoop | None = None) -> None:
    """Wire SIGINT/SIGTERM to the shutdown event.

    Safe to call more than once — subsequent calls are no-ops.
    """

    global _signals_installed
    if _signals_installed:
        return

    event = _get_or_create_event()

    def _handle(signum: int) -> None:  # pragma: no cover - signal integration
        global _hard_stop
        logger.warning(
            "received %s — triggering graceful shutdown",
            signal.Signals(signum).name,
        )
        _hard_stop = True
        event.set()

    loop = loop or asyncio.get_event_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _handle, sig)
        except (NotImplementedError, RuntimeError):
            signal.signal(sig, lambda s, _f: _handle(s))

    _signals_installed = True


def flag_path() -> Path:
    return get_settings().pause_flag_path


def is_flag_set(path: Path | None = None) -> bool:
    """Cheap synchronous check — safe in hot paths."""

    return (path or flag_path()).exists()


def is_shutting_down() -> bool:
    """True if SIGINT/SIGTERM has been received (or lifespan shutdown fired)."""

    return _hard_stop


def mark_shutting_down() -> None:
    """Flip the hard-stop flag from outside the signal handler.

    Used by the FastAPI lifespan ``finally`` block so that hot paths observe
    ``is_shutting_down() == True`` during graceful uvicorn shutdown, even when
    we don't install our own signal handlers.
    """

    global _hard_stop
    _hard_stop = True


def is_paused() -> bool:
    """Combined check: shutting down OR (pause flag present AND not bypassed).

    Hard-stop always wins. The user-requested pause flag can be suspended per
    task via :func:`allow_manual_send` so HITL sends and operator tests don't
    get swallowed by the autopilot kill switch.
    """

    if _hard_stop:
        return True
    if _bypass_pause_flag.get():
        return False
    return is_flag_set()


def raise_if_paused() -> None:
    """Hot-path guard. Call before dispatching work that cannot be undone."""

    if is_paused():
        raise KillSwitchTripped()


@contextlib.contextmanager
def allow_manual_send() -> Iterator[None]:
    """Locally treat the pause flag as cleared for explicit human actions.

    Wrap a deliberate, user-initiated connector call (``send-draft`` from the
    HITL UI, a connectivity test from Settings) in this context so the
    downstream ``raise_if_paused()`` guards don't fire. Shutdown (SIGINT /
    SIGTERM / lifespan finalisation) still aborts — this *only* suppresses
    the user-requested pause flag, not the hard stop.

    The bypass is scoped to the current ``asyncio`` task via a
    :class:`~contextvars.ContextVar`, so it never leaks into the scheduler,
    the inbound poller, the auto-reply path, or the follow-up beat running
    concurrently on other tasks.
    """

    token = _bypass_pause_flag.set(True)
    try:
        yield
    finally:
        _bypass_pause_flag.reset(token)


async def await_shutdown_or_timeout(seconds: float) -> bool:
    """Sleep up to ``seconds`` or wake early on shutdown.

    Returns True if shutdown fired, False on timeout.
    """

    event = _get_or_create_event()
    try:
        await asyncio.wait_for(event.wait(), timeout=seconds)
        return True
    except asyncio.TimeoutError:
        return False


async def watch_flag_file(poll_interval_s: float = 1.0) -> None:
    """Background task: poll the flag file and log transitions.

    This task does not itself set the shutdown event — it only lets the
    scheduler and hot paths observe the flag via ``is_paused()``. Exits when
    the shutdown event is set.
    """

    event = _get_or_create_event()
    last_state = False
    while not event.is_set():
        state = is_flag_set()
        if state != last_state:
            if state:
                logger.warning("kill-switch flag present at %s — processing paused", flag_path())
            else:
                logger.info("kill-switch flag removed — resuming")
            last_state = state
        try:
            await asyncio.wait_for(event.wait(), timeout=poll_interval_s)
            return
        except asyncio.TimeoutError:
            continue


def touch_flag(path: Path | None = None) -> Path:
    """Create the pause flag. Returns the path created."""

    target = path or flag_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.touch(exist_ok=True)
    return target


def remove_flag(path: Path | None = None) -> bool:
    """Remove the pause flag. Returns True if removed, False if absent."""

    target = path or flag_path()
    if target.exists():
        target.unlink()
        return True
    return False


def write_pid_file() -> Path:
    """Write the current PID so ``autosdr stop`` can signal us."""

    settings = get_settings()
    path = settings.pid_file_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(str(os.getpid()))
    return path


def clear_pid_file() -> None:
    path = get_settings().pid_file_path
    if path.exists():
        try:
            path.unlink()
        except OSError:  # pragma: no cover - best effort
            pass


def read_pid_file() -> int | None:
    path = get_settings().pid_file_path
    if not path.exists():
        return None
    try:
        return int(path.read_text().strip())
    except (OSError, ValueError):
        return None


def reset_for_tests() -> None:
    """Reset module state between test cases."""

    global _shutdown_event, _signals_installed, _hard_stop
    _shutdown_event = None
    _signals_installed = False
    _hard_stop = False
    # ContextVar is per-task; still reset the default-bound view so a test
    # that forgot to exit its ``allow_manual_send()`` doesn't bleed into
    # the next case.
    try:
        _bypass_pause_flag.set(False)
    except LookupError:  # pragma: no cover - no current context
        pass
