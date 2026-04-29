"""Manual start/stop website-enrichment fan-out.

One asyncio task per process: many leads in flight (bounded by the
``SCAN_CONCURRENCY`` infra setting) with per-host serialization so
the same domain is not hammered in parallel. No scheduler, idle
loop, TTL, or workspace settings — use the Scans page Start/Stop or
call :func:`start_scan` / :func:`stop_scan` from tests.

Concurrency note: the default of 20 is deliberately polite. Each
in-flight lead can issue up to 3 sequential HTTP requests
(robots.txt, root, sitemap) against a unique host, so peak
simultaneous outbound TCP connections track ``SCAN_CONCURRENCY``.
The shared ``httpx.AsyncClient`` pool in :mod:`autosdr.webhook` is
sized off the same number; if you raise concurrency, raise the
pool too or every lead will land on ``status=timeout`` waiting for
a connection slot inside its own 4-second budget.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlparse

from sqlalchemy import select

from sqlalchemy.orm import Session

from autosdr.config import get_settings
from autosdr.db import session_scope
from autosdr.enrichment import enrich_lead, persist_enrichment
from autosdr.models import Lead, Workspace

logger = logging.getLogger(__name__)

_BUDGET_S = 4.0
_RESPECT_ROBOTS = True


def _resolve_concurrency() -> int:
    """Read the runtime scan concurrency from infra settings, with a floor.

    Centralised so ``start_scan`` and any future direct caller of
    :func:`_fan_out` see the same value. The floor of 1 prevents an
    operator typo (``SCAN_CONCURRENCY=0``) from silently producing a
    deadlocked semaphore.
    """

    return max(1, int(get_settings().scan_concurrency))


@dataclass
class RunnerState:
    running: bool = False
    total: int = 0
    done: int = 0
    ok: int = 0
    failed: int = 0
    started_at: datetime | None = None
    task: asyncio.Task[None] | None = None
    cancel: asyncio.Event = field(default_factory=asyncio.Event)


_state = RunnerState()
_lock_creation: asyncio.Lock | None = None


def _locks_lock() -> asyncio.Lock:
    global _lock_creation
    if _lock_creation is None:
        _lock_creation = asyncio.Lock()
    return _lock_creation


def get_runner_state() -> RunnerState:
    """Return the singleton runner snapshot (counts + task handle)."""

    return _state


def get_scan_state_snapshot() -> dict[str, Any]:
    """JSON-friendly dict for ``GET /api/scans/summary``."""

    s = _state
    return {
        "runner_running": s.running,
        "runner_total": s.total,
        "runner_done": s.done,
        "runner_ok": s.ok,
        "runner_failed": s.failed,
        "runner_started_at": s.started_at.isoformat() if s.started_at else None,
    }


def _host_lock_key(website: str | None, lead_id: str) -> str:
    """Stable key so the same hostname shares one asyncio lock."""

    if not website or not str(website).strip():
        return f"noop:{lead_id}"
    cand = website.strip()
    if "://" not in cand:
        cand = f"https://{cand}"
    loc = urlparse(cand).netloc.lower().strip()
    return loc or f"noop:{lead_id}"


async def _get_host_lock(
    locks: dict[str, asyncio.Lock], host_key: str
) -> asyncio.Lock:
    async with _locks_lock():
        if host_key not in locks:
            locks[host_key] = asyncio.Lock()
        return locks[host_key]


def _snapshot_workspace_lead_pairs() -> list[tuple[str, str | None]]:
    """(lead_id, website) for every scannable row in the one workspace."""

    with session_scope() as session:
        workspace = session.query(Workspace).first()
        if workspace is None:
            return []

        stmt = (
            select(Lead.id, Lead.website)
            .where(
                Lead.workspace_id == workspace.id,
                Lead.do_not_contact_at.is_(None),
                Lead.website.isnot(None),
            )
            .order_by(Lead.import_order.asc())
        )
        rows = session.execute(stmt).all()
        return [(str(r[0]), r[1]) for r in rows]


def start_scan() -> bool:
    """Begin a full pass over all scannable leads, or ``False`` if busy."""

    global _state
    if _state.running:
        return False

    pairs = _snapshot_workspace_lead_pairs()
    _state.total = len(pairs)
    _state.done = 0
    _state.ok = 0
    _state.failed = 0

    if not pairs:
        _state.running = False
        _state.started_at = None
        return True

    _state.cancel.clear()
    _state.running = True
    _state.started_at = datetime.now(tz=timezone.utc)

    concurrency = _resolve_concurrency()
    logger.info(
        "scan runner starting: leads=%d concurrency=%d budget_s=%.1f",
        len(pairs),
        concurrency,
        _BUDGET_S,
    )

    async def runner() -> None:
        host_locks: dict[str, asyncio.Lock] = {}
        started_monotonic = time.monotonic()
        try:
            await _fan_out(
                pairs,
                host_locks=host_locks,
                concurrency=concurrency,
            )
        except asyncio.CancelledError:
            logger.info(
                "scan runner cancelled: done=%d ok=%d failed=%d elapsed_s=%.1f",
                _state.done,
                _state.ok,
                _state.failed,
                time.monotonic() - started_monotonic,
            )
            raise
        except Exception:
            logger.exception("scan runner crashed")
        else:
            logger.info(
                "scan runner finished: total=%d ok=%d failed=%d elapsed_s=%.1f",
                _state.total,
                _state.ok,
                _state.failed,
                time.monotonic() - started_monotonic,
            )
        finally:
            _state.running = False
            _state.task = None
            _state.total = 0
            _state.done = 0
            _state.ok = 0
            _state.failed = 0
            _state.started_at = None

    loop = asyncio.get_running_loop()
    _state.task = loop.create_task(runner(), name="autosdr.scan_fanout")
    return True


def stop_scan() -> bool:
    """Cancel the runner if live. Returns ``True`` if a task was interrupted."""

    if not _state.running or _state.task is None:
        return False
    _state.cancel.set()
    _state.task.cancel()
    return True


async def _fan_out(
    pairs: list[tuple[str, str | None]],
    *,
    host_locks: dict[str, asyncio.Lock],
    concurrency: int | None = None,
) -> None:
    sem = asyncio.Semaphore(concurrency if concurrency is not None else _resolve_concurrency())

    async def one(lead_id: str, website: str | None) -> None:
        if _state.cancel.is_set():
            return
        # Single-write counter discipline: every task increments the
        # done/ok/failed counters at most once. ``counted`` flips True
        # the moment we have a definitive outcome (success or handled
        # failure), so the outer ``except`` only counts crashes that
        # happened before we got there.
        counted = False
        try:
            async with sem:
                if _state.cancel.is_set():
                    return

                hk = _host_lock_key(website, lead_id)
                lock = await _get_host_lock(host_locks, hk)
                async with lock:
                    if _state.cancel.is_set():
                        return

                    # Run the network fetch with NO DB session held.
                    # ``website`` was snapshotted at start_scan(); we
                    # don't need the row until persist time. Holding
                    # the session across this await drains the pool
                    # under high concurrency (one connection per
                    # in-flight scan, default pool 5+10=15).
                    result = await enrich_lead(
                        website_url=website,
                        budget_s=_BUDGET_S,
                        respect_robots=_RESPECT_ROBOTS,
                    )

                    with session_scope() as session:
                        lead = session.execute(
                            select(Lead).where(Lead.id == lead_id)
                        ).scalar_one_or_none()
                        if lead is None:
                            _state.done += 1
                            _state.failed += 1
                            counted = True
                            return
                        persist_enrichment(lead, result)

                    _state.done += 1
                    if result.status == "ok":
                        _state.ok += 1
                    else:
                        _state.failed += 1
                    counted = True
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("scan lead_id=%s failed", lead_id)
            if not counted:
                _state.done += 1
                _state.failed += 1

    tasks = [asyncio.create_task(one(lid, w)) for lid, w in pairs]
    try:
        await asyncio.gather(*tasks)
    except asyncio.CancelledError:
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        raise


async def scan_one_lead(
    *,
    session: Session,
    lead: Lead,
) -> str:
    """Enrich one lead and persist — detail page \"Re-scan now\"."""

    result = await enrich_lead(
        website_url=lead.website,
        budget_s=_BUDGET_S,
        respect_robots=_RESPECT_ROBOTS,
    )
    persist_enrichment(lead, result)
    session.commit()
    session.refresh(lead)

    logger.info(
        "scan lead=%s status=%s latency_ms=%s cms=%s sitemap_count=%s",
        lead.id,
        result.status,
        result.meta.get("latency_ms"),
        result.signals.get("cms"),
        result.signals.get("sitemap_count"),
    )
    return result.status


__all__ = [
    "get_runner_state",
    "get_scan_state_snapshot",
    "scan_one_lead",
    "start_scan",
    "stop_scan",
]
