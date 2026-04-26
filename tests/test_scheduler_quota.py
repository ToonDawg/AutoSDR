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
)
from autosdr.quota import count_ai_messages_last_24h
from autosdr.scheduler import _next_queued_leads, run_campaign_outreach_batch


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

        count = count_ai_messages_last_24h(session, cid)
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

        count = count_ai_messages_last_24h(session, cid)
        assert count == 1


def test_count_ai_messages_last_24h_respects_campaign_reset(fresh_db, workspace_factory):
    ws_id = workspace_factory()
    with fresh_db() as session:
        cid = _build_fixture(session, ws_id, 1)
        campaign = session.get(Campaign, cid)
        cl = session.query(CampaignLead).first()
        thread = Thread(
            campaign_lead_id=cl.id, connector_type="android_sms",
            status=ThreadStatus.ACTIVE, angle="x", tone_snapshot="x",
        )
        session.add(thread)
        session.flush()

        before_reset = Message(
            thread_id=thread.id, role=MessageRole.AI, content="before", metadata_={},
        )
        session.add(before_reset)
        session.flush()

        campaign.quota_reset_at = datetime.now(tz=timezone.utc)
        session.flush()

        after_reset = Message(
            thread_id=thread.id, role=MessageRole.AI, content="after", metadata_={},
        )
        session.add(after_reset)
        session.flush()

        assert count_ai_messages_last_24h(session, cid) == 1


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


def test_next_queued_leads_excludes_claimed_sending_rows(fresh_db, workspace_factory):
    ws_id = workspace_factory()
    with fresh_db() as session:
        cid = _build_fixture(session, ws_id, 3)
        first = (
            session.query(CampaignLead)
            .filter(CampaignLead.campaign_id == cid)
            .order_by(CampaignLead.queue_position.asc())
            .first()
        )
        first.status = CampaignLeadStatus.SENDING
        session.flush()

        got = _next_queued_leads(session, cid, limit=3)

    assert [cl.queue_position for cl, _lead in got] == [2, 3]


async def test_run_campaign_outreach_batch_stops_at_quota(
    fresh_db, workspace_factory
):
    ws_id = workspace_factory()
    with fresh_db() as session:
        cid = _build_fixture(session, ws_id, 2)
        campaign = session.get(Campaign, cid)
        campaign.outreach_per_day = 1
        cl = session.query(CampaignLead).first()
        thread = Thread(
            campaign_lead_id=cl.id,
            connector_type="android_sms",
            status=ThreadStatus.ACTIVE,
            angle="x",
            tone_snapshot="x",
        )
        session.add(thread)
        session.flush()
        session.add(
            Message(
                thread_id=thread.id,
                role=MessageRole.AI,
                content="already sent",
                metadata_={},
            )
        )
        session.flush()

        summary = await run_campaign_outreach_batch(
            session=session,
            connector=object(),
            workspace=None,
            campaign=campaign,
            max_count=2,
            respect_quota=True,
        )

    assert summary.requested == 2
    assert summary.attempted == 0
    assert summary.sent == 0
    assert summary.capped_by_quota is True
