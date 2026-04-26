from __future__ import annotations

from autosdr.api.campaigns import reset_send_count
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


def test_reset_send_count_starts_fresh_quota_window(fresh_db, workspace_factory):
    ws_id = workspace_factory()
    with fresh_db() as session:
        ws = session.get(Workspace, ws_id)
        campaign = Campaign(
            workspace_id=ws.id,
            name="C",
            goal="Book calls",
            outreach_per_day=5,
            connector_type="file",
            status=CampaignStatus.ACTIVE,
        )
        session.add(campaign)
        session.flush()

        lead = Lead(
            workspace_id=ws.id,
            name="Lead",
            contact_uri="+61400000001",
            contact_type="mobile",
            category="Retail",
            address="Brisbane",
            raw_data={},
            import_order=1,
            source_file="x",
            status=LeadStatus.CONTACTED,
        )
        session.add(lead)
        session.flush()

        campaign_lead = CampaignLead(
            campaign_id=campaign.id,
            lead_id=lead.id,
            queue_position=1,
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

        session.add(Message(thread_id=thread.id, role=MessageRole.AI, content="sent", metadata_={}))
        session.flush()
        campaign_id = campaign.id

    result = reset_send_count(campaign_id)

    assert result.sent_24h == 0
    assert result.quota_reset_at is not None
