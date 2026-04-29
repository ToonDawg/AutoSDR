"""Fan-out scan runner — start/stop and persistence."""

from __future__ import annotations

import asyncio
import time
from datetime import datetime, timezone

import httpx
import pytest
from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import QueuePool

from autosdr import config as config_module
from autosdr import db as db_module
from autosdr.db import session_scope
from autosdr.enrichment import EnrichmentResult
from autosdr.models import Lead, LeadStatus
from autosdr.pipeline import scans as scans_mod
from autosdr.pipeline.scans import get_runner_state, start_scan, stop_scan


@pytest.mark.asyncio
async def test_fanout_writes_envelopes(workspace_factory, monkeypatch):
    async def fake_enrich(*, website_url, http_client, budget_s, respect_robots):
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

    transport = httpx.MockTransport(lambda r: httpx.Response(404))
    async with httpx.AsyncClient(
        limits=httpx.Limits(max_connections=260, max_keepalive_connections=50),
        transport=transport,
    ) as http_client:
        assert start_scan(http_client) is True
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
    """No running task."""

    assert stop_scan() is False


@pytest.mark.asyncio
async def test_fanout_does_not_exhaust_connection_pool(
    workspace_factory, monkeypatch
):
    """Regression: the runner used to hold a DB session across the
    network ``enrich_lead`` await, so a Postgres-style pool of 5+10
    connections drained the moment we ran the realistic 200-way
    concurrent scan. The fix is to defer the session until the
    persist step, after the slow await has resolved.

    This test pins a tiny ``QueuePool`` (size 2, no overflow, 1s
    timeout) and runs a fake ``enrich_lead`` that simulates a slow
    HTTP fetch. Pre-fix, more than two in-flight tasks would
    ``QueuePool ... timeout`` almost immediately. Post-fix, the
    pool is only acquired briefly per lead so all leads complete.
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

    # Swap in a deliberately tiny QueuePool. The autouse
    # ``_isolate_settings`` fixture in conftest.py disposes the
    # engine and clears the singletons after the test, so this is
    # safe to mutate in place.
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

    async def slow_enrich(*, website_url, http_client, budget_s, respect_robots):
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

    transport = httpx.MockTransport(lambda r: httpx.Response(404))
    async with httpx.AsyncClient(transport=transport) as http_client:
        assert start_scan(http_client) is True
        task = get_runner_state().task
        assert task is not None

        # Let the fan-out spin up and park inside the fake fetcher,
        # then release everything in one go. With the bug present
        # this stage would already have raised TimeoutError as
        # tasks beyond the second one starved waiting for a DB
        # connection that the first two were holding across the
        # await.
        await asyncio.wait_for(fetch_started.wait(), timeout=2.0)
        await asyncio.sleep(0.05)
        release_fetch.set()

        await asyncio.wait_for(task, timeout=10.0)

    # Every lead persisted exactly once, no double-count, no pool
    # timeout.
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


@pytest.mark.asyncio
async def test_fanout_prod_style_load(workspace_factory, monkeypatch):
    """Prod-state smoke test for the scan fan-out.

    Exercises the *real* :func:`enrich_lead` (no monkey-patch) against
    a :class:`httpx.AsyncClient` whose pool is sized exactly the way
    :func:`autosdr.webhook.create_app` sizes it from
    ``Settings.scan_concurrency``. The mock transport adds 50 ms of
    latency per response so the latency profile resembles a slow
    real-world site, and every lead has a unique host so the per-host
    asyncio locks never artificially serialise the run.

    The test catches two classes of regression that a fast unit-style
    fake (zero-latency, unbounded pool) would miss:

    1. **Pool starvation** — concurrency too high for the httpx pool.
       Tasks queue inside httpx, queueing time eats into the per-lead
       4-second enrichment budget, and ``status=timeout`` shows up
       across most leads. This was the symptom in prod after the
       earlier DB-pool fix landed.
    2. **Wall-clock blow-up** — the run takes far longer than the
       latency × ceil(leads / concurrency) lower bound, suggesting
       contention somewhere on the hot path (locks, DB session
       checkout, semaphore mis-sizing).

    The wall-clock bound is generous (10 s for 100 leads at 50 ms
    each, theoretical lower bound ~250 ms at concurrency=20) so the
    test stays stable on CI but still flags an order-of-magnitude
    regression.
    """

    monkeypatch.setenv("SCAN_CONCURRENCY", "20")
    config_module.reset_settings_for_tests()

    ws_id = workspace_factory()

    lead_count = 100
    with session_scope() as session:
        for i in range(lead_count):
            session.add(
                Lead(
                    workspace_id=ws_id,
                    name=f"Prod{i}",
                    contact_uri=f"+614300{i:07d}",
                    contact_type="mobile",
                    category=None,
                    # Unique host per lead — bypass the per-host
                    # asyncio lock so the run actually exercises
                    # the concurrency cap.
                    website=f"https://lead{i:04d}.example",
                    raw_data={},
                    import_order=i + 1,
                    source_file="t",
                    status=LeadStatus.NEW,
                )
            )

    response_html = (
        b"<html><head><title>Demo</title></head>"
        b"<body><h1>Hello</h1><p>Some content here.</p></body></html>"
    )

    in_flight = 0
    max_in_flight = 0

    async def slow_handler(request: httpx.Request) -> httpx.Response:
        nonlocal in_flight, max_in_flight
        in_flight += 1
        max_in_flight = max(max_in_flight, in_flight)
        try:
            await asyncio.sleep(0.05)
        finally:
            in_flight -= 1
        path = request.url.path
        if path.endswith("robots.txt"):
            return httpx.Response(404, text="")
        if "sitemap" in path:
            return httpx.Response(
                200,
                content=(
                    b'<?xml version="1.0"?><urlset>'
                    b"<url><loc>https://x</loc></url>"
                    b"</urlset>"
                ),
                headers={"content-type": "application/xml"},
            )
        return httpx.Response(
            200,
            content=response_html,
            headers={"content-type": "text/html"},
        )

    transport = httpx.MockTransport(slow_handler)

    scan_conc = max(1, int(config_module.get_settings().scan_concurrency))
    max_conn = max(64, scan_conc * 4)
    max_keep = max(32, scan_conc * 2)

    started = time.monotonic()
    async with httpx.AsyncClient(
        transport=transport,
        limits=httpx.Limits(
            max_connections=max_conn,
            max_keepalive_connections=max_keep,
        ),
    ) as http_client:
        assert start_scan(http_client) is True
        task = get_runner_state().task
        assert task is not None
        await asyncio.wait_for(task, timeout=30.0)
    elapsed = time.monotonic() - started

    state = scans_mod._state
    assert state.running is False

    with session_scope() as session:
        leads = list(
            session.execute(select(Lead).where(Lead.workspace_id == ws_id)).scalars()
        )

    assert len(leads) == lead_count
    ok_count = sum(1 for lead in leads if lead.enrichment_status == "ok")
    timeout_count = sum(1 for lead in leads if lead.enrichment_status == "timeout")
    error_count = sum(
        1 for lead in leads if lead.enrichment_status not in ("ok", "timeout")
    )
    assert ok_count == lead_count, (
        f"prod-style scan should produce all-ok results when the "
        f"httpx pool is sized to the concurrency budget; got "
        f"ok={ok_count} timeout={timeout_count} error={error_count} "
        f"of {lead_count} leads"
    )

    assert max_in_flight <= scan_conc, (
        f"observed {max_in_flight} concurrent fetches against a "
        f"semaphore cap of {scan_conc}; the limit is leaking"
    )

    # 50ms latency × 3 requests per lead = 150ms per lead serially.
    # ceil(100 / 20) batches × 150ms = 750ms theoretical lower bound.
    # Allow 10× margin for test-runner jitter and asyncio scheduling.
    assert elapsed < 10.0, (
        f"prod-style scan of {lead_count} leads at concurrency "
        f"{scan_conc} took {elapsed:.2f}s (expected <10s); something "
        f"is serialising the hot path"
    )
