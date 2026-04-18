"""Scheduler — rolling 24h quota enforcement."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

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
from autosdr.scheduler import _count_ai_messages_last_24h, _next_queued_leads


def _build_fixture(session, ws_id: str, num_leads: int) -> str:
    campaign = Campaign(
        workspace_id=ws_id, name="C", goal="g", outreach_per_day=10,
        connector_type="android_sms", status=CampaignStatus.ACTIVE,
    )
    session.add(campaign)
    session.flush()

    for i in range(num_leads):
        lead = Lead(
            workspace_id=ws_id, name=f"Lead {i}", contact_uri=f"+6140000000{i}",
            contact_type="mobile", category="x", address="x",
            raw_data={}, import_order=i + 1, source_file="x", status=LeadStatus.NEW,
        )
        session.add(lead)
        session.flush()
        cl = CampaignLead(
            campaign_id=campaign.id, lead_id=lead.id,
            queue_position=i + 1, status=CampaignLeadStatus.QUEUED,
        )
        session.add(cl)

    session.flush()
    return campaign.id


def test_count_ai_messages_last_24h_includes_only_ai_role(fresh_db, workspace_factory):
    ws_id = workspace_factory()
    with fresh_db() as session:
        cid = _build_fixture(session, ws_id, 1)
        cl = session.query(CampaignLead).first()
        thread = Thread(
            campaign_lead_id=cl.id, connector_type="android_sms",
            status=ThreadStatus.ACTIVE, angle="x", tone_snapshot="x",
        )
        session.add(thread)
        session.flush()

        session.add_all(
            [
                Message(thread_id=thread.id, role=MessageRole.AI, content="1", metadata_={}),
                Message(thread_id=thread.id, role=MessageRole.AI, content="2", metadata_={}),
                Message(thread_id=thread.id, role=MessageRole.LEAD, content="l", metadata_={}),
            ]
        )
        session.flush()

        count = _count_ai_messages_last_24h(session, cid)
        assert count == 2


def test_count_ai_messages_last_24h_excludes_old_messages(fresh_db, workspace_factory):
    ws_id = workspace_factory()
    with fresh_db() as session:
        cid = _build_fixture(session, ws_id, 1)
        cl = session.query(CampaignLead).first()
        thread = Thread(
            campaign_lead_id=cl.id, connector_type="android_sms",
            status=ThreadStatus.ACTIVE, angle="x", tone_snapshot="x",
        )
        session.add(thread)
        session.flush()

        now = datetime.now(tz=timezone.utc)
        recent = Message(thread_id=thread.id, role=MessageRole.AI, content="new", metadata_={})
        old = Message(thread_id=thread.id, role=MessageRole.AI, content="old", metadata_={})
        session.add_all([recent, old])
        session.flush()
        old.created_at = now - timedelta(hours=25)
        session.flush()

        count = _count_ai_messages_last_24h(session, cid)
        assert count == 1


def test_next_queued_leads_orders_by_queue_position(fresh_db, workspace_factory):
    ws_id = workspace_factory()
    with fresh_db() as session:
        cid = _build_fixture(session, ws_id, 5)

        got = _next_queued_leads(session, cid, limit=3)
        assert len(got) == 3
        positions = [cl.queue_position for cl, _lead in got]
        assert positions == sorted(positions)


def test_next_queued_leads_zero_limit_returns_empty(fresh_db, workspace_factory):
    ws_id = workspace_factory()
    with fresh_db() as session:
        cid = _build_fixture(session, ws_id, 5)
        assert _next_queued_leads(session, cid, limit=0) == []
        assert _next_queued_leads(session, cid, limit=-1) == []
