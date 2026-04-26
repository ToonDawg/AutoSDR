from __future__ import annotations

import pytest
from fastapi import HTTPException

from autosdr import killswitch
from autosdr.api.campaigns import kickoff_campaign
from autosdr.api.schemas import CampaignKickoffRequest
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
from autosdr.pipeline.outreach import OutreachResult


def _build_campaign(
    session,
    workspace_id: str,
    *,
    status: str = CampaignStatus.ACTIVE,
    outreach_per_day: int = 1,
    lead_count: int = 4,
) -> str:
    campaign = Campaign(
        workspace_id=workspace_id,
        name="Kickoff",
        goal="Book calls",
        outreach_per_day=outreach_per_day,
        connector_type="file",
        status=status,
    )
    session.add(campaign)
    session.flush()

    for i in range(lead_count):
        lead = Lead(
            workspace_id=workspace_id,
            name=f"Lead {i}",
            contact_uri=f"+6140000000{i}",
            contact_type="mobile",
            category="Retail",
            address="Brisbane",
            raw_data={},
            import_order=i + 1,
            source_file="x",
            status=LeadStatus.NEW,
        )
        session.add(lead)
        session.flush()
        session.add(
            CampaignLead(
                campaign_id=campaign.id,
                lead_id=lead.id,
                queue_position=lead_count - i,
                status=CampaignLeadStatus.QUEUED,
            )
        )

    session.flush()
    return campaign.id


def _add_existing_send(session, campaign_id: str, workspace_id: str) -> None:
    lead = Lead(
        workspace_id=workspace_id,
        name="Already Sent",
        contact_uri="+61400009999",
        contact_type="mobile",
        category="Retail",
        address="Brisbane",
        raw_data={},
        import_order=99,
        source_file="x",
        status=LeadStatus.CONTACTED,
    )
    session.add(lead)
    session.flush()

    campaign_lead = CampaignLead(
        campaign_id=campaign_id,
        lead_id=lead.id,
        queue_position=99,
        status=CampaignLeadStatus.CONTACTED,
    )
    session.add(campaign_lead)
    session.flush()

    thread = Thread(
        campaign_lead_id=campaign_lead.id,
        connector_type="file",
        status=ThreadStatus.ACTIVE,
    )
    session.add(thread)
    session.flush()
    session.add(
        Message(thread_id=thread.id, role=MessageRole.AI, content="existing", metadata_={})
    )
    session.flush()


@pytest.fixture
def fake_outreach(monkeypatch):
    sent_leads: list[str] = []

    async def _fake_run(
        *,
        session,
        connector,
        workspace,
        campaign,
        campaign_lead,
        lead,
    ):
        sent_leads.append(lead.name or "")
        thread = Thread(
            campaign_lead_id=campaign_lead.id,
            connector_type=connector.connector_type,
            status=ThreadStatus.ACTIVE,
        )
        session.add(thread)
        session.flush()
        session.add(
            Message(
                thread_id=thread.id,
                role=MessageRole.AI,
                content=f"hello {lead.name}",
                metadata_={},
            )
        )
        campaign_lead.status = CampaignLeadStatus.CONTACTED
        lead.status = LeadStatus.CONTACTED
        session.flush()
        return OutreachResult(sent=True, reason="sent", thread_id=thread.id, message_id=None)

    monkeypatch.setattr("autosdr.scheduler.run_outreach_for_campaign_lead", _fake_run)
    return sent_leads


async def test_kickoff_sends_next_n_in_queue_order(
    fresh_db, workspace_factory, fake_outreach
):
    ws_id = workspace_factory()
    with fresh_db() as session:
        campaign_id = _build_campaign(session, ws_id, outreach_per_day=1, lead_count=4)

    result = await kickoff_campaign(campaign_id, CampaignKickoffRequest(count=3))

    assert result.requested == 3
    assert result.attempted == 3
    assert result.sent == 3
    assert result.failed == 0
    assert result.remaining_queued == 1
    assert fake_outreach == ["Lead 3", "Lead 2", "Lead 1"]


async def test_kickoff_bypasses_quota_but_counts_afterward(
    fresh_db, workspace_factory, fake_outreach
):
    ws_id = workspace_factory()
    with fresh_db() as session:
        campaign_id = _build_campaign(session, ws_id, outreach_per_day=1, lead_count=3)
        _add_existing_send(session, campaign_id, ws_id)

    result = await kickoff_campaign(campaign_id, CampaignKickoffRequest(count=3))

    assert result.sent == 3
    assert result.campaign.sent_24h == 4
    assert result.campaign.outreach_per_day == 1


async def test_kickoff_works_for_paused_campaign_and_global_pause(
    fresh_db, workspace_factory, fake_outreach
):
    ws_id = workspace_factory()
    with fresh_db() as session:
        campaign_id = _build_campaign(
            session, ws_id, status=CampaignStatus.PAUSED, lead_count=2
        )

    killswitch.touch_flag()
    assert killswitch.is_paused()

    result = await kickoff_campaign(campaign_id, CampaignKickoffRequest(count=2))

    assert killswitch.is_paused()
    assert result.sent == 2
    assert fake_outreach == ["Lead 1", "Lead 0"]


async def test_kickoff_rejects_completed_campaign(fresh_db, workspace_factory):
    ws_id = workspace_factory()
    with fresh_db() as session:
        campaign_id = _build_campaign(
            session, ws_id, status=CampaignStatus.COMPLETED, lead_count=1
        )

    with pytest.raises(HTTPException) as excinfo:
        await kickoff_campaign(campaign_id, CampaignKickoffRequest(count=1))

    assert excinfo.value.status_code == 409
    assert excinfo.value.detail == {"error": "campaign_completed"}


async def test_kickoff_returns_409_when_shutting_down(
    fresh_db, workspace_factory, fake_outreach
):
    ws_id = workspace_factory()
    with fresh_db() as session:
        campaign_id = _build_campaign(session, ws_id, lead_count=1)

    killswitch.mark_shutting_down()

    with pytest.raises(HTTPException) as excinfo:
        await kickoff_campaign(campaign_id, CampaignKickoffRequest(count=1))

    assert excinfo.value.status_code == 409
    assert excinfo.value.detail == {"error": "system_shutting_down"}
    assert fake_outreach == []

    with fresh_db() as session:
        assert session.query(Message).count() == 0
