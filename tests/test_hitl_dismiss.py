"""HITL dismiss / restore — "Needs your eye" notification semantics.

The dismiss flow is conceptually a notification ack: the thread stays
``paused_for_hitl`` (its outcome is undecided) but it stops nagging the
operator from the inbox. A *new* HITL event — anything that calls
``pause_thread_for_hitl`` or one of the routes that simulates one
(``regenerate_suggestions``, ``take-over``) — automatically clears the
flag, so the thread re-surfaces. These tests pin both sides of that
contract and the cheap-count endpoint that backs the sidebar badge.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

from autosdr.db import session_scope
from autosdr.models import (
    Campaign,
    CampaignLead,
    CampaignLeadStatus,
    CampaignStatus,
    Lead,
    LeadStatus,
    Thread,
    ThreadStatus,
    Workspace,
)
from autosdr.pipeline._shared import pause_thread_for_hitl
from autosdr.webhook import create_app


def _client() -> TestClient:
    return TestClient(
        create_app(run_scheduler_task=False), raise_server_exceptions=False
    )


def _make_thread(
    fresh_db,
    workspace_factory,
    *,
    status: str = ThreadStatus.PAUSED_FOR_HITL,
    hitl_reason: str | None = "awaiting_human_reply",
    hitl_dismissed_at: Any = None,
    name: str = "Tester",
    tone_register: str | None = None,
    contact_uri: str | None = None,
) -> str:
    """Spin up a workspace + campaign + lead + thread; return the thread id.

    ``contact_uri`` defaults to a per-process counter so multiple calls
    inside one workspace don't trip the ``(workspace_id, contact_uri)``
    unique index — useful for the bulk-bucket fixtures (ticket 0018).
    """

    ws_id = workspace_factory()
    if contact_uri is None:
        _make_thread._counter = getattr(_make_thread, "_counter", 0) + 1
        contact_uri = f"+6140000{_make_thread._counter:04d}"
    with fresh_db() as session:
        ws = session.get(Workspace, ws_id)
        campaign = Campaign(
            workspace_id=ws.id,
            name="C",
            goal="Book a call",
            outreach_per_day=5,
            connector_type="file",
            status=CampaignStatus.ACTIVE,
        )
        session.add(campaign)
        session.flush()

        lead = Lead(
            workspace_id=ws.id,
            name=name,
            contact_uri=contact_uri,
            contact_type="mobile",
            category="Retail",
            address="Brisbane",
            raw_data={},
            import_order=1,
            source_file="x",
            status=LeadStatus.NEW,
        )
        session.add(lead)
        session.flush()

        cl = CampaignLead(
            campaign_id=campaign.id,
            lead_id=lead.id,
            queue_position=1,
            status=CampaignLeadStatus.QUEUED,
        )
        session.add(cl)
        session.flush()

        thread = Thread(
            campaign_lead_id=cl.id,
            connector_type="file",
            status=status,
            hitl_reason=hitl_reason,
            hitl_dismissed_at=hitl_dismissed_at,
            tone_register=tone_register,
        )
        session.add(thread)
        session.flush()
        return thread.id


def test_dismiss_sets_timestamp_and_keeps_status(fresh_db, workspace_factory):
    """Dismiss must NOT change the thread's outcome state."""

    thread_id = _make_thread(fresh_db, workspace_factory)

    with _client() as client:
        res = client.post(f"/api/threads/{thread_id}/dismiss")
        assert res.status_code == 200
        body = res.json()
        assert body["status"] == ThreadStatus.PAUSED_FOR_HITL
        assert body["hitl_reason"] == "awaiting_human_reply"
        assert body["hitl_dismissed_at"] is not None

    with session_scope() as session:
        t = session.get(Thread, thread_id)
        assert t.status == ThreadStatus.PAUSED_FOR_HITL
        assert t.hitl_dismissed_at is not None


def test_dismiss_rejects_thread_not_in_hitl_state(fresh_db, workspace_factory):
    """Dismissing an active thread is meaningless — surface a 409."""

    thread_id = _make_thread(
        fresh_db,
        workspace_factory,
        status=ThreadStatus.ACTIVE,
        hitl_reason=None,
    )

    with _client() as client:
        res = client.post(f"/api/threads/{thread_id}/dismiss")
        assert res.status_code == 409
        assert res.json() == {"error": "thread_not_in_hitl_state"}


def test_restore_clears_timestamp(fresh_db, workspace_factory):
    """Restore is the explicit operator-driven undo for dismiss."""

    from datetime import datetime, timezone

    thread_id = _make_thread(
        fresh_db,
        workspace_factory,
        hitl_dismissed_at=datetime.now(tz=timezone.utc),
    )

    with _client() as client:
        res = client.post(f"/api/threads/{thread_id}/restore")
        assert res.status_code == 200
        assert res.json()["hitl_dismissed_at"] is None

    with session_scope() as session:
        t = session.get(Thread, thread_id)
        assert t.hitl_dismissed_at is None


def test_list_threads_dismissed_filter(fresh_db, workspace_factory):
    """``dismissed=true|false`` partitions the HITL list cleanly."""

    from datetime import datetime, timezone

    active_id = _make_thread(fresh_db, workspace_factory, name="ActiveOne")
    dismissed_id = _make_thread(
        fresh_db,
        workspace_factory,
        name="DismissedOne",
        hitl_dismissed_at=datetime.now(tz=timezone.utc),
    )

    with _client() as client:
        active = client.get(
            "/api/threads",
            params={
                "status_filter": ThreadStatus.PAUSED_FOR_HITL,
                "dismissed": "false",
            },
        ).json()
        dismissed = client.get(
            "/api/threads",
            params={
                "status_filter": ThreadStatus.PAUSED_FOR_HITL,
                "dismissed": "true",
            },
        ).json()
        both = client.get(
            "/api/threads",
            params={"status_filter": ThreadStatus.PAUSED_FOR_HITL},
        ).json()

    active_ids = {t["id"] for t in active}
    dismissed_ids = {t["id"] for t in dismissed}
    both_ids = {t["id"] for t in both}

    assert active_id in active_ids
    assert active_id not in dismissed_ids
    assert dismissed_id in dismissed_ids
    assert dismissed_id not in active_ids
    assert {active_id, dismissed_id} <= both_ids


def test_hitl_count_matches_list(fresh_db, workspace_factory):
    """Regression: the cheap counter must agree with the list endpoint.

    The sidebar uses ``/api/threads/hitl/count`` for its badge precisely
    so it doesn't have to fan-out to the full list every 10 seconds; if
    the two ever diverge, the operator sees the wrong number.
    """

    from datetime import datetime, timezone

    _make_thread(fresh_db, workspace_factory, name="A")
    _make_thread(fresh_db, workspace_factory, name="B")
    _make_thread(
        fresh_db,
        workspace_factory,
        name="C",
        hitl_dismissed_at=datetime.now(tz=timezone.utc),
    )

    with _client() as client:
        count = client.get("/api/threads/hitl/count").json()
        active_list = client.get(
            "/api/threads",
            params={
                "status_filter": ThreadStatus.PAUSED_FOR_HITL,
                "dismissed": "false",
            },
        ).json()
        dismissed_list = client.get(
            "/api/threads",
            params={
                "status_filter": ThreadStatus.PAUSED_FOR_HITL,
                "dismissed": "true",
            },
        ).json()

    assert count["active"] == len(active_list)
    assert count["dismissed"] == len(dismissed_list)
    assert count["active"] == 2
    assert count["dismissed"] == 1
    # ``by_reason`` (ticket 0018) is the new Inbox filter chip dimension —
    # one row per ``hitl_reason`` token, NULLs bucketed under ``"unknown"``.
    # The fixture seeds two ``awaiting_human_reply`` actives.
    assert count["by_reason"] == {"awaiting_human_reply": 2}


def test_pause_for_hitl_clears_dismissed_at(fresh_db, workspace_factory):
    """Auto-resurface: a fresh HITL event nukes a stale dismissal flag.

    This is what makes "dismiss" feel like an ack rather than a mute —
    the lead replying (or any other HITL trigger) re-raises the thread
    on the inbox without the operator having to remember to restore it.
    """

    from datetime import datetime, timezone

    thread_id = _make_thread(
        fresh_db,
        workspace_factory,
        hitl_reason="connector_send_failed",
        hitl_dismissed_at=datetime.now(tz=timezone.utc),
    )

    with session_scope() as session:
        t = session.get(Thread, thread_id)
        assert t.hitl_dismissed_at is not None
        pause_thread_for_hitl(
            t, reason="awaiting_human_reply", context={"incoming_message": "hey"}
        )
        session.flush()
        session.refresh(t)
        assert t.hitl_dismissed_at is None
        assert t.hitl_reason == "awaiting_human_reply"


def test_take_over_clears_dismissed_at(fresh_db, workspace_factory):
    """Manual take-over is also an explicit "this is new" signal — re-surface."""

    from datetime import datetime, timezone

    thread_id = _make_thread(
        fresh_db,
        workspace_factory,
        hitl_dismissed_at=datetime.now(tz=timezone.utc),
    )

    with _client() as client:
        res = client.post(
            f"/api/threads/{thread_id}/take-over", json={"note": "I'll handle it"}
        )
        assert res.status_code == 200
        assert res.json()["hitl_dismissed_at"] is None
        assert res.json()["hitl_reason"] == "taken_over_by_human"


@pytest.mark.parametrize("dismissed", [True, False])
def test_thread_out_includes_hitl_dismissed_at(
    fresh_db, workspace_factory, dismissed
):
    """``ThreadOut`` plumbs the new column through to API consumers."""

    from datetime import datetime, timezone

    when = datetime.now(tz=timezone.utc) if dismissed else None
    thread_id = _make_thread(
        fresh_db, workspace_factory, hitl_dismissed_at=when
    )

    with _client() as client:
        body = client.get(f"/api/threads/{thread_id}").json()

    assert "hitl_dismissed_at" in body
    if dismissed:
        assert body["hitl_dismissed_at"] is not None
    else:
        assert body["hitl_dismissed_at"] is None


# ---------------------------------------------------------------------------
# Inbox filter chips (ticket 0018) — list endpoint ``hitl_reason`` filter +
# count endpoint ``by_reason`` breakdown.
# ---------------------------------------------------------------------------


def _seed_mixed_hitl_bucket(fresh_db, workspace_factory) -> str:
    """Seed one workspace with three connector_send_failed actives, two
    awaiting_human_reply actives, one eval_failed_after_max_attempts
    active. Returns the workspace id.
    """

    ws_id = workspace_factory()
    seeds = [
        ("connector_send_failed", 3),
        ("awaiting_human_reply", 2),
        ("eval_failed_after_max_attempts", 1),
    ]
    for reason, n in seeds:
        for _ in range(n):
            _make_thread(fresh_db, workspace_factory=lambda: ws_id, hitl_reason=reason)
    return ws_id


def test_list_threads_filters_by_hitl_reason(fresh_db, workspace_factory):
    """``GET /api/threads?hitl_reason=connector_send_failed`` returns
    only that bucket — drives the Inbox 'Connector failed · N' chip
    selection."""

    _seed_mixed_hitl_bucket(fresh_db, workspace_factory)

    with _client() as client:
        connector_failed = client.get(
            "/api/threads",
            params={
                "status_filter": ThreadStatus.PAUSED_FOR_HITL,
                "dismissed": "false",
                "hitl_reason": "connector_send_failed",
            },
        ).json()
        awaiting = client.get(
            "/api/threads",
            params={
                "status_filter": ThreadStatus.PAUSED_FOR_HITL,
                "dismissed": "false",
                "hitl_reason": "awaiting_human_reply",
            },
        ).json()
        all_paused = client.get(
            "/api/threads",
            params={
                "status_filter": ThreadStatus.PAUSED_FOR_HITL,
                "dismissed": "false",
            },
        ).json()

    assert len(connector_failed) == 3
    assert all(t["hitl_reason"] == "connector_send_failed" for t in connector_failed)
    assert len(awaiting) == 2
    assert all(t["hitl_reason"] == "awaiting_human_reply" for t in awaiting)
    # Filter is additive, not exclusive — unfiltered returns the union.
    assert len(all_paused) == 6


def test_hitl_count_by_reason_breakdown(fresh_db, workspace_factory):
    """``GET /api/threads/hitl/count`` returns per-reason counts that
    sum to ``active``. Drives the Inbox filter chip row's badges."""

    _seed_mixed_hitl_bucket(fresh_db, workspace_factory)

    with _client() as client:
        count = client.get("/api/threads/hitl/count").json()

    assert count["active"] == 6
    assert count["dismissed"] == 0
    assert count["by_reason"] == {
        "connector_send_failed": 3,
        "awaiting_human_reply": 2,
        "eval_failed_after_max_attempts": 1,
    }
    assert sum(count["by_reason"].values()) == count["active"]


def test_hitl_count_by_reason_buckets_null_as_unknown(
    fresh_db, workspace_factory
):
    """Legacy threads from before ``hitl_reason`` was populated should
    bucket under ``"unknown"`` so the filter chip row never has to
    defend against a NULL key."""

    ws_id = workspace_factory()
    _make_thread(fresh_db, workspace_factory=lambda: ws_id, hitl_reason=None)
    _make_thread(fresh_db, workspace_factory=lambda: ws_id, hitl_reason="awaiting_human_reply")

    with _client() as client:
        count = client.get("/api/threads/hitl/count").json()

    assert count["active"] == 2
    assert count["by_reason"] == {
        "unknown": 1,
        "awaiting_human_reply": 1,
    }


@pytest.mark.parametrize("register", ["tradie", "professional", None])
def test_thread_out_round_trips_tone_register(fresh_db, workspace_factory, register):
    """``ThreadOut`` plumbs ``tone_register`` through both list and detail endpoints.

    Ticket 0017: the operator UI uses this field to render a chip next to the
    lead so they can see which voice the prompt was built against. Both
    concrete registers and the legacy ``None`` (kill-switch / pre-0017) path
    must serialize cleanly.
    """

    thread_id = _make_thread(
        fresh_db, workspace_factory, name=f"R-{register}", tone_register=register
    )

    with _client() as client:
        detail = client.get(f"/api/threads/{thread_id}").json()
        listed = client.get(
            "/api/threads", params={"status_filter": ThreadStatus.PAUSED_FOR_HITL}
        ).json()

    assert "tone_register" in detail
    assert detail["tone_register"] == register

    matched = [t for t in listed if t["id"] == thread_id]
    assert matched, "thread missing from /api/threads list response"
    assert matched[0]["tone_register"] == register
