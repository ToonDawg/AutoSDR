from __future__ import annotations

from datetime import datetime, timezone

from autosdr.api.campaigns import assign_leads, reset_send_count
from autosdr.api.schemas import CampaignAssignLeads
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


def test_assign_leads_excludes_do_not_contact(fresh_db, workspace_factory):
    """``all_eligible`` and ``lead_ids`` must both skip leads with ``do_not_contact_at``."""

    ws_id = workspace_factory()
    with fresh_db() as session:
        ws = session.get(Workspace, ws_id)
        campaign = Campaign(
            workspace_id=ws.id,
            name="DNC test",
            goal="Book a call",
            outreach_per_day=5,
            connector_type="file",
            status=CampaignStatus.ACTIVE,
        )
        session.add(campaign)
        session.flush()

        clean_lead = Lead(
            workspace_id=ws.id,
            name="Clean",
            contact_uri="+61400000001",
            contact_type="mobile",
            category="Retail",
            address="Brisbane",
            raw_data={},
            import_order=1,
            source_file="x",
            status=LeadStatus.NEW,
        )
        opted_out_lead = Lead(
            workspace_id=ws.id,
            name="Opted Out",
            contact_uri="+61400000002",
            contact_type="mobile",
            category="Retail",
            address="Brisbane",
            raw_data={},
            import_order=2,
            source_file="x",
            status=LeadStatus.NEW,
            do_not_contact_at=datetime.now(timezone.utc),
            do_not_contact_reason="opt_out:STOP",
        )
        session.add_all([clean_lead, opted_out_lead])
        session.flush()
        campaign_id = campaign.id
        clean_id = clean_lead.id
        opted_id = opted_out_lead.id

    # all_eligible mode — DNC lead is excluded, surfaced in skipped_lead_ids.
    result = assign_leads(campaign_id, CampaignAssignLeads(all_eligible=True))
    assert result.skipped_lead_ids == [opted_id]
    assert result.skipped_reason == "do_not_contact"

    with fresh_db() as session:
        cls = (
            session.query(CampaignLead)
            .filter(CampaignLead.campaign_id == campaign_id)
            .all()
        )
        assigned_lead_ids = {cl.lead_id for cl in cls}
        assert assigned_lead_ids == {clean_id}
        assert opted_id not in assigned_lead_ids

    # Explicit lead_ids mode — DNC still excluded.
    result = assign_leads(
        campaign_id, CampaignAssignLeads(lead_ids=[clean_id, opted_id])
    )
    assert opted_id in result.skipped_lead_ids
    with fresh_db() as session:
        cls = (
            session.query(CampaignLead)
            .filter(
                CampaignLead.campaign_id == campaign_id,
                CampaignLead.lead_id == opted_id,
            )
            .all()
        )
        assert cls == []
