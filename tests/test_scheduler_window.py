"""Scheduler — outreach-window pacing integration tests.

These tests drive ``run_campaign_outreach_batch`` with an injected
``now_local`` so the working-hours gate is deterministic. They are
the "wired-up" companion to the pure-function tests in
``tests/test_pacing.py``.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from autosdr.config import default_workspace_settings
from autosdr.connectors.base import BaseConnector, IncomingMessage, OutgoingMessage, SendResult
from autosdr.models import (
    Campaign,
    CampaignLead,
    CampaignLeadStatus,
    CampaignStatus,
    Lead,
    LeadStatus,
    Message,
    MessageRole,
    Thread,
    ThreadStatus,
    Workspace,
)
from autosdr.scheduler import run_campaign_outreach_batch


class _StubConnector(BaseConnector):
    """Minimal connector — most cases never actually send because pacing
    returns 0 before we hit ``send``; the ones that do send have
    ``run_outreach_for_campaign_lead`` monkeypatched to a fake."""

    connector_type = "file"

    async def send(self, message: OutgoingMessage) -> SendResult:  # pragma: no cover
        return SendResult(success=True, provider_message_id="stub-1")

    async def poll_incoming(self) -> list[IncomingMessage]:  # pragma: no cover
        return []

    def parse_webhook(self, payload: dict) -> IncomingMessage | None:  # pragma: no cover
        return None

    async def validate_config(self) -> tuple[bool, str]:  # pragma: no cover
        return True, ""


def _build_campaign_with_leads(
    session,
    ws_id: str,
    *,
    num_leads: int,
    outreach_per_day: int = 50,
    outreach_window: dict | None = None,
) -> Campaign:
    campaign = Campaign(
        workspace_id=ws_id,
        name="Test campaign",
        goal="g",
        outreach_per_day=outreach_per_day,
        connector_type="file",
        status=CampaignStatus.ACTIVE,
        outreach_window=outreach_window,
    )
    session.add(campaign)
    session.flush()
    for i in range(num_leads):
        lead = Lead(
            workspace_id=ws_id,
            name=f"Lead {i}",
            contact_uri=f"+6140000000{i:02d}",
            contact_type="mobile",
            category="x",
            address="x",
            raw_data={},
            import_order=i + 1,
            source_file="x",
            status=LeadStatus.NEW,
        )
        session.add(lead)
        session.flush()
        cl = CampaignLead(
            campaign_id=campaign.id,
            lead_id=lead.id,
            queue_position=i + 1,
            status=CampaignLeadStatus.QUEUED,
        )
        session.add(cl)
    session.flush()
    session.refresh(campaign)
    return campaign


def _make_workspace(session, *, window_overrides: dict | None = None) -> Workspace:
    settings = default_workspace_settings()
    if window_overrides is not None:
        settings["outreach_window"] = window_overrides
    ws = Workspace(
        business_name="Test",
        business_dump="x",
        tone_prompt="x",
        settings=settings,
    )
    session.add(ws)
    session.flush()
    return ws


def _local(hour: int, minute: int = 0) -> datetime:
    """Reference-day tz-aware datetime."""

    return datetime(2026, 4, 28, hour, minute, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Window cutoff
# ---------------------------------------------------------------------------


async def test_no_send_before_window_start(fresh_db):
    """At 07:30, the default 08–17 window should send zero outreach."""

    with fresh_db() as session:
        ws = _make_workspace(session)
        campaign = _build_campaign_with_leads(session, ws.id, num_leads=5)

        summary = await run_campaign_outreach_batch(
            session=session,
            connector=_StubConnector(),
            workspace=ws,
            campaign=campaign,
            max_count=5,
            respect_quota=True,
            now_local=_local(7, 30),
        )

    assert summary.sent == 0
    assert summary.attempted == 0
    assert summary.capped_by_window is True


async def test_no_send_after_window_end(fresh_db):
    """At 18:00, default window has closed. No outreach goes out."""

    with fresh_db() as session:
        ws = _make_workspace(session)
        campaign = _build_campaign_with_leads(session, ws.id, num_leads=5)

        summary = await run_campaign_outreach_batch(
            session=session,
            connector=_StubConnector(),
            workspace=ws,
            campaign=campaign,
            max_count=5,
            respect_quota=True,
            now_local=_local(18),
        )

    assert summary.sent == 0
    assert summary.capped_by_window is True


async def test_no_send_at_midnight(fresh_db):
    """Activating a campaign at 11pm should not send until 8am."""

    with fresh_db() as session:
        ws = _make_workspace(session)
        campaign = _build_campaign_with_leads(session, ws.id, num_leads=5)

        summary = await run_campaign_outreach_batch(
            session=session,
            connector=_StubConnector(),
            workspace=ws,
            campaign=campaign,
            max_count=5,
            respect_quota=True,
            now_local=_local(23, 0),
        )

    assert summary.sent == 0
    assert summary.capped_by_window is True


# ---------------------------------------------------------------------------
# Pacing inside the window
# ---------------------------------------------------------------------------


async def test_pacing_at_window_start_caps_at_zero(fresh_db):
    """Exactly at 08:00 with no prior sends, ceil(quota * 0) = 0."""

    with fresh_db() as session:
        ws = _make_workspace(session)
        campaign = _build_campaign_with_leads(
            session, ws.id, num_leads=10, outreach_per_day=50
        )

        summary = await run_campaign_outreach_batch(
            session=session,
            connector=_StubConnector(),
            workspace=ws,
            campaign=campaign,
            max_count=2,
            respect_quota=True,
            now_local=_local(8, 0),
        )

    assert summary.sent == 0
    assert summary.capped_by_window is True


async def test_pacing_at_midpoint_allows_send_when_behind(fresh_db, monkeypatch):
    """At 12:30 with zero sends, target = 25; max_batch=2 means we attempt 2."""

    sent_calls: list[str] = []

    async def fake_outreach(*, session, connector, workspace, campaign, campaign_lead, lead):
        sent_calls.append(campaign_lead.id)

        class _R:
            sent = True
            reason = ""

        return _R()

    monkeypatch.setattr(
        "autosdr.scheduler.run_outreach_for_campaign_lead", fake_outreach
    )

    with fresh_db() as session:
        ws = _make_workspace(session)
        campaign = _build_campaign_with_leads(
            session, ws.id, num_leads=10, outreach_per_day=50
        )

        summary = await run_campaign_outreach_batch(
            session=session,
            connector=_StubConnector(),
            workspace=ws,
            campaign=campaign,
            max_count=2,
            respect_quota=True,
            now_local=_local(12, 30),
        )

    assert summary.attempted == 2
    assert summary.sent == 2


async def test_pacing_blocks_when_already_at_target(fresh_db):
    """If we've already hit today's pacing target, no further sends until time advances."""

    with fresh_db() as session:
        ws = _make_workspace(session)
        campaign = _build_campaign_with_leads(
            session, ws.id, num_leads=10, outreach_per_day=50
        )
        # Seed 25 contacts already opened "in window" — at 12:30 the
        # pacing target is 25, so we should not send any more this tick.
        cls = (
            session.query(CampaignLead)
            .filter(CampaignLead.campaign_id == campaign.id)
            .order_by(CampaignLead.queue_position.asc())
            .limit(5)
            .all()
        )
        # Inject 5 threads each with 5 distinct AI messages — but pacing
        # counts contacts (one per thread), so 5 threads = 5 contacts.
        # We want 25 contacts; instead create 25 threads with 1 AI each.
        # Re-do: clear and create 25 fresh threads.
        for cl in cls:
            cl.status = CampaignLeadStatus.CONTACTED
        session.flush()

        # Add 25 more leads + threads + an AI message each (in window).
        in_window_ts = _local(8, 30).astimezone(timezone.utc)
        for i in range(25):
            lead = Lead(
                workspace_id=ws.id,
                name=f"Already {i}",
                contact_uri=f"+6149999{i:04d}",
                contact_type="mobile",
                category="x",
                address="x",
                raw_data={},
                import_order=100 + i,
                source_file="x",
                status=LeadStatus.CONTACTED,
            )
            session.add(lead)
            session.flush()
            cl = CampaignLead(
                campaign_id=campaign.id,
                lead_id=lead.id,
                queue_position=100 + i,
                status=CampaignLeadStatus.CONTACTED,
            )
            session.add(cl)
            session.flush()
            thread = Thread(
                campaign_lead_id=cl.id,
                connector_type="file",
                status=ThreadStatus.ACTIVE,
                angle="x",
                tone_snapshot="x",
            )
            session.add(thread)
            session.flush()
            msg = Message(
                thread_id=thread.id,
                role=MessageRole.AI,
                content=f"sent {i}",
                metadata_={},
            )
            session.add(msg)
            session.flush()
            msg.created_at = in_window_ts
            session.flush()

        # Refresh campaign so the count helper sees the new rows.
        session.refresh(campaign)

        summary = await run_campaign_outreach_batch(
            session=session,
            connector=_StubConnector(),
            workspace=ws,
            campaign=campaign,
            max_count=2,
            respect_quota=True,
            now_local=_local(12, 30),
        )

    assert summary.sent == 0
    assert summary.capped_by_window is True


# ---------------------------------------------------------------------------
# Override semantics
# ---------------------------------------------------------------------------


async def test_disabled_window_short_circuits_pacing(fresh_db, monkeypatch):
    """``enabled=false`` is the escape hatch — no time gating, just 24h quota."""

    sent_calls: list[str] = []

    async def fake_outreach(*, session, connector, workspace, campaign, campaign_lead, lead):
        sent_calls.append(campaign_lead.id)

        class _R:
            sent = True
            reason = ""

        return _R()

    monkeypatch.setattr(
        "autosdr.scheduler.run_outreach_for_campaign_lead", fake_outreach
    )

    with fresh_db() as session:
        ws = _make_workspace(
            session,
            window_overrides={"enabled": False, "start_hour": 8, "end_hour": 17},
        )
        campaign = _build_campaign_with_leads(
            session, ws.id, num_leads=2, outreach_per_day=50
        )

        # Even at midnight, a disabled window shouldn't gate sends.
        summary = await run_campaign_outreach_batch(
            session=session,
            connector=_StubConnector(),
            workspace=ws,
            campaign=campaign,
            max_count=2,
            respect_quota=True,
            now_local=_local(0, 0),
        )

    assert summary.attempted == 2
    assert summary.sent == 2
    assert summary.capped_by_window is False


async def test_campaign_override_beats_workspace_default(fresh_db, monkeypatch):
    """A campaign-level disabled window beats an enabled workspace default."""

    sent_calls: list[str] = []

    async def fake_outreach(*, session, connector, workspace, campaign, campaign_lead, lead):
        sent_calls.append(campaign_lead.id)

        class _R:
            sent = True
            reason = ""

        return _R()

    monkeypatch.setattr(
        "autosdr.scheduler.run_outreach_for_campaign_lead", fake_outreach
    )

    with fresh_db() as session:
        # Workspace says "8–17 only"; campaign says "always".
        ws = _make_workspace(session)  # default 8–17 enabled
        campaign = _build_campaign_with_leads(
            session,
            ws.id,
            num_leads=2,
            outreach_per_day=50,
            outreach_window={"enabled": False, "start_hour": 8, "end_hour": 17},
        )

        summary = await run_campaign_outreach_batch(
            session=session,
            connector=_StubConnector(),
            workspace=ws,
            campaign=campaign,
            max_count=2,
            respect_quota=True,
            now_local=_local(23, 0),
        )

    assert summary.sent == 2
    assert summary.capped_by_window is False


async def test_kickoff_bypasses_window(fresh_db, monkeypatch):
    """Manual kickoff (``respect_quota=False``) ignores the window."""

    async def fake_outreach(*, session, connector, workspace, campaign, campaign_lead, lead):
        class _R:
            sent = True
            reason = ""

        return _R()

    monkeypatch.setattr(
        "autosdr.scheduler.run_outreach_for_campaign_lead", fake_outreach
    )

    with fresh_db() as session:
        ws = _make_workspace(session)
        campaign = _build_campaign_with_leads(session, ws.id, num_leads=3)

        summary = await run_campaign_outreach_batch(
            session=session,
            connector=_StubConnector(),
            workspace=ws,
            campaign=campaign,
            max_count=3,
            respect_quota=False,
            now_local=_local(23, 0),
        )

    assert summary.sent == 3
    assert summary.capped_by_window is False
    assert summary.capped_by_quota is False
