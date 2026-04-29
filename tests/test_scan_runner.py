"""Fan-out scan runner — start/stop and persistence (crawlee-backed).

The previous prod-style-load test exercised the real ``enrich_lead``
against an ``httpx.MockTransport`` to verify pool sizing. After the
crawlee swap there is no httpx pool to size, so that test was removed
— the crawlee fetcher is exercised end-to-end by the live-run report
in ``data/crawlee-test-report-20260429.md``.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import pytest
from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import QueuePool

from autosdr import db as db_module
from autosdr.db import session_scope
from autosdr.enrichment import EnrichmentResult
from autosdr.models import Lead, LeadStatus
from autosdr.pipeline import scans as scans_mod
from autosdr.pipeline.scans import get_runner_state, start_scan, stop_scan


@pytest.mark.asyncio
async def test_fanout_writes_envelopes(workspace_factory, monkeypatch):
    async def fake_enrich(*, website_url, budget_s, respect_robots):
        return EnrichmentResult(
            status="ok",
            signals={"cms": "stub"},
            meta={
                "fetched_at": datetime.now(tz=timezone.utc).isoformat(),
                "latency_ms": 1,
            },
        )

    monkeypatch.setattr(scans_mod, "enrich_lead", fake_enrich)

    ws_id = workspace_factory()

    with session_scope() as session:
        for i in range(3):
            session.add(
                Lead(
                    workspace_id=ws_id,
                    name=f"L{i}",
                    contact_uri=f"+614100{i:07d}",
                    contact_type="mobile",
                    category=None,
                    website=f"https://site{i}.example",
                    raw_data={},
                    import_order=i + 1,
                    source_file="t",
                    status=LeadStatus.NEW,
                )
            )

    assert start_scan() is True
    task = get_runner_state().task
    assert task is not None
    await task

    assert scans_mod._state.running is False

    with session_scope() as session:
        leads = list(
            session.execute(select(Lead).where(Lead.workspace_id == ws_id)).scalars()
        )
    assert len(leads) == 3
    for lead in leads:
        blob = lead.raw_data["enrichment"]
        assert blob["_meta"]["status"] == "ok"


def test_stop_scan_when_idle_returns_false():
    assert stop_scan() is False


@pytest.mark.asyncio
async def test_fanout_does_not_exhaust_connection_pool(
    workspace_factory, monkeypatch
):
    """Regression: the runner used to hold a DB session across the
    network ``enrich_lead`` await, draining a small connection pool.
    The fix defers the session until the persist step.
    """

    ws_id = workspace_factory()

    with session_scope() as session:
        for i in range(10):
            session.add(
                Lead(
                    workspace_id=ws_id,
                    name=f"L{i}",
                    contact_uri=f"+614200{i:07d}",
                    contact_type="mobile",
                    category=None,
                    website=f"https://site{i}.example",
                    raw_data={},
                    import_order=i + 1,
                    source_file="t",
                    status=LeadStatus.NEW,
                )
            )

    settings_url = db_module.get_settings().database_url
    cramped = create_engine(
        settings_url,
        connect_args={"check_same_thread": False},
        poolclass=QueuePool,
        pool_size=2,
        max_overflow=0,
        pool_timeout=1.0,
        future=True,
    )

    @event.listens_for(cramped, "connect")
    def _enable_sqlite_fk(dbapi_conn, _record):  # noqa: ARG001
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA foreign_keys=ON")
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA busy_timeout=5000")
        cur.close()

    db_module._engine.dispose()
    db_module._engine = cramped
    db_module._SessionLocal = sessionmaker(
        bind=cramped,
        autoflush=False,
        autocommit=False,
        expire_on_commit=False,
        future=True,
    )

    fetch_started = asyncio.Event()
    release_fetch = asyncio.Event()
    in_flight = 0
    max_in_flight = 0

    async def slow_enrich(*, website_url, budget_s, respect_robots):
        nonlocal in_flight, max_in_flight
        in_flight += 1
        max_in_flight = max(max_in_flight, in_flight)
        fetch_started.set()
        try:
            await release_fetch.wait()
        finally:
            in_flight -= 1
        return EnrichmentResult(
            status="ok",
            signals={"cms": "stub"},
            meta={
                "fetched_at": datetime.now(tz=timezone.utc).isoformat(),
                "latency_ms": 1,
            },
        )

    monkeypatch.setattr(scans_mod, "enrich_lead", slow_enrich)

    assert start_scan() is True
    task = get_runner_state().task
    assert task is not None

    await asyncio.wait_for(fetch_started.wait(), timeout=2.0)
    await asyncio.sleep(0.05)
    release_fetch.set()

    await asyncio.wait_for(task, timeout=10.0)

    assert max_in_flight >= 3, (
        "fan-out should run more leads in flight than the pool size"
    )
    with session_scope() as session:
        leads = list(
            session.execute(select(Lead).where(Lead.workspace_id == ws_id)).scalars()
        )
    assert len(leads) == 10
    for lead in leads:
        assert lead.enrichment_status == "ok"
        assert lead.raw_data["enrichment"]["_meta"]["status"] == "ok"
