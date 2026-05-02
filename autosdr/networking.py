"""Networking diagnostics for the operator (ticket 0005 unit 8).

Two surfaces:

* :func:`probe_tailscale_state` — best-effort: shell out to
  ``tailscale status`` and report ``running`` / ``not_installed``.
  PATH issues, sandboxes, Windows, slow start: probe failures *return*
  ``not_detected``, never raise.
* :func:`networking_status` — the data the Settings → Networking card
  reads. Layers the Tailscale probe on top of the configured
  :class:`~autosdr.config.Settings.host` and the resolved
  ``dashboard_origin`` so the operator can spot the *PC-bind-interface
  footgun* the Pragmatist surfaced in § *Remote-access architecture*.

The startup banner (``log_host_bind_warning``) calls the same probe
once at lifespan-startup and emits a single ``warning`` log line if
``HOST=127.0.0.1`` *and* Tailscale is up. The banner doesn't block
boot — it just lands next to the rest of uvicorn's startup so the
operator's first scrollback hit names the problem.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
from dataclasses import dataclass
from typing import Literal

from autosdr.config import get_settings

logger = logging.getLogger(__name__)


TailscaleState = Literal["running", "not_running", "not_detected"]


@dataclass(frozen=True)
class TailscaleProbe:
    state: TailscaleState
    detail: str | None = None


def probe_tailscale_state(*, timeout_s: float = 1.5) -> TailscaleProbe:
    """Best-effort: did ``tailscale status`` exit 0?

    The probe is conservative on purpose: any failure (binary not on
    PATH, exit non-zero, exec timeout, exception during subprocess
    setup) collapses to ``not_detected`` so we never blame the
    operator for a probe-side problem.
    """

    binary = shutil.which("tailscale")
    if binary is None:
        return TailscaleProbe(state="not_detected", detail="tailscale not on PATH")
    try:
        result = subprocess.run(  # noqa: S603 — args list, not shell=True
            [binary, "status", "--json=false"],
            capture_output=True,
            timeout=timeout_s,
            check=False,
            text=True,
        )
    except subprocess.TimeoutExpired:
        return TailscaleProbe(state="not_detected", detail="probe timed out")
    except OSError as exc:
        return TailscaleProbe(state="not_detected", detail=f"probe failed: {exc}")
    if result.returncode == 0:
        first_line = (result.stdout or "").splitlines()[0:1]
        return TailscaleProbe(state="running", detail=first_line[0] if first_line else None)
    stderr = (result.stderr or "").strip()
    if "Stopped" in stderr or "stopped" in stderr or result.returncode in (1,):
        return TailscaleProbe(state="not_running", detail=stderr or None)
    return TailscaleProbe(state="not_detected", detail=stderr or None)


@dataclass(frozen=True)
class NetworkingStatus:
    host: str
    port: int
    bound_for_remote_access: bool
    tailscale: TailscaleProbe
    warning: str | None


def networking_status() -> NetworkingStatus:
    """Snapshot the operator-facing networking state.

    ``bound_for_remote_access`` is true when ``HOST`` is one of
    ``0.0.0.0`` / ``::`` / a non-loopback address; that's the only
    bind that lets a phone-on-tailnet reach the dashboard. The
    warning is the human-readable nudge the Settings card displays.
    """

    settings = get_settings()
    host = settings.host
    bound = host not in {"127.0.0.1", "localhost", "::1"}
    probe = probe_tailscale_state()
    warning: str | None = None
    if probe.state == "running" and not bound:
        warning = (
            "AutoSDR is bound to localhost but Tailscale is up — your phone "
            "won't be able to reach this dashboard. Set HOST=0.0.0.0 in "
            "your .env and restart so FastAPI listens on the tailnet."
        )
    return NetworkingStatus(
        host=host,
        port=settings.port,
        bound_for_remote_access=bound,
        tailscale=probe,
        warning=warning,
    )


def log_host_bind_warning() -> None:
    """Emit one warning log if HOST=127.0.0.1 and Tailscale is up."""

    status = networking_status()
    if status.warning is None:
        return
    logger.warning("networking: %s", status.warning)


__all__ = [
    "NetworkingStatus",
    "TailscaleProbe",
    "TailscaleState",
    "log_host_bind_warning",
    "networking_status",
    "probe_tailscale_state",
]
