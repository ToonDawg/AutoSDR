from __future__ import annotations

from datetime import datetime, timezone

import pytest
from fastapi import HTTPException

from autosdr.api.campaigns import (
    assign_leads,
    create_campaign,
    delete_campaign,
    get_campaign,
    patch_campaign,
    reset_send_count,
)
from autosdr.api.schemas import (
    CampaignAssignLeads,
    CampaignCreate,
    CampaignOut,
    CampaignPatch,
    OutreachWindowConfig,
)
from autosdr.models import (
    Campaign,
    CampaignLead,
    CampaignLeadStatus,
    CampaignStatus,
    Lead,
    LeadStatus,
    LlmCall,
    LlmCallPurpose,
    Message,
    MessageRole,
    Thread,
    ThreadStatus,
    Workspace,
)


_ALL_CAMPAIGN_LEAD_STATUSES = (
    CampaignLeadStatus.QUEUED,
    CampaignLeadStatus.SENDING,
    CampaignLeadStatus.PAUSED_FOR_HITL,
    CampaignLeadStatus.CONTACTED,
    CampaignLeadStatus.REPLIED,
    CampaignLeadStatus.WON,
    CampaignLeadStatus.LOST,
    CampaignLeadStatus.SKIPPED,
)


def test_campaign_out_exposes_every_status_bucket(fresh_db, workspace_factory):
    """``CampaignOut`` must expose one ``*_count`` per CampaignLeadStatus.

    This is the contract that the frontend Stat strips depend on — if a
    new bucket is added to ``CampaignLeadStatus`` without a matching
    field here, the UI silently undercounts the funnel.
    """

    ws_id = workspace_factory()
    with fresh_db() as session:
        ws = session.get(Workspace, ws_id)
        campaign = Campaign(
            workspace_id=ws.id,
            name="Bucket coverage",
            goal="Book a call",
            outreach_per_day=5,
            connector_type="file",
            status=CampaignStatus.ACTIVE,
        )
        session.add(campaign)
        session.flush()

        # One lead per bucket so each ``*_count`` is unambiguously 1.
        for idx, cl_status in enumerate(_ALL_CAMPAIGN_LEAD_STATUSES, start=1):
            lead = Lead(
                workspace_id=ws.id,
                name=f"Lead {cl_status}",
                contact_uri=f"+6140000{idx:04d}",
                contact_type="mobile",
                category="Retail",
                address="Brisbane",
                raw_data={},
                import_order=idx,
                source_file="seed",
                status=LeadStatus.NEW,
            )
            session.add(lead)
            session.flush()
            session.add(
                CampaignLead(
                    campaign_id=campaign.id,
                    lead_id=lead.id,
                    queue_position=idx,
                    status=cl_status,
                )
            )
        session.flush()
        campaign_id = campaign.id

    out: CampaignOut = get_campaign(campaign_id)

    expected_field_for = {
        CampaignLeadStatus.QUEUED: "queued_count",
        CampaignLeadStatus.SENDING: "sending_count",
        CampaignLeadStatus.PAUSED_FOR_HITL: "paused_for_hitl_count",
        CampaignLeadStatus.CONTACTED: "contacted_count",
        CampaignLeadStatus.REPLIED: "replied_count",
        CampaignLeadStatus.WON: "won_count",
        CampaignLeadStatus.LOST: "lost_count",
        CampaignLeadStatus.SKIPPED: "skipped_count",
    }
    # Every bucket maps to a real field on the public schema.
    schema_fields = set(CampaignOut.model_fields)
    for bucket, field_name in expected_field_for.items():
        assert field_name in schema_fields, f"missing field for {bucket}"
        assert getattr(out, field_name) == 1, f"{field_name} != 1 for {bucket}"

    # No double-counting: the eight buckets sum to lead_count.
    assert out.lead_count == 8
    assert (
        out.queued_count
        + out.sending_count
        + out.paused_for_hitl_count
        + out.contacted_count
        + out.replied_count
        + out.won_count
        + out.lost_count
        + out.skipped_count
        == out.lead_count
    )


def test_campaign_out_exposes_queued_priority_count(fresh_db, workspace_factory):
    """``queued_priority_count`` mirrors queued AND ``enrichment_status='not_found'``.

    Three queued leads (one ``not_found``, one ``ok``, one
    ``timeout``) plus one ``contacted`` lead with ``not_found``.
    Only the queued+not_found row counts — the contacted one is not
    queued, the ok/timeout rows are queued but not priority. Pins
    ticket 0013 SC: ``queued_priority_count == 1``.
    """

    ws_id = workspace_factory()
    with fresh_db() as session:
        ws = session.get(Workspace, ws_id)
        campaign = Campaign(
            workspace_id=ws.id,
            name="Priority bucket",
            goal="Book calls",
            outreach_per_day=10,
            connector_type="file",
            status=CampaignStatus.ACTIVE,
        )
        session.add(campaign)
        session.flush()

        fixtures = [
            ("queued", "not_found"),
            ("queued", "ok"),
            ("queued", "timeout"),
            ("contacted", "not_found"),  # NOT counted — not queued
        ]
        for idx, (cl_status, enrich) in enumerate(fixtures, start=1):
            lead_status = (
                LeadStatus.CONTACTED
                if cl_status == "contacted"
                else LeadStatus.NEW
            )
            lead = Lead(
                workspace_id=ws.id,
                name=f"Lead {idx}",
                contact_uri=f"+6140000{idx:04d}",
                contact_type="mobile",
                category="Retail",
                address="x",
                raw_data={},
                import_order=idx,
                source_file="seed",
                status=lead_status,
            )
            lead.enrichment_status = enrich
            session.add(lead)
            session.flush()
            session.add(
                CampaignLead(
                    campaign_id=campaign.id,
                    lead_id=lead.id,
                    queue_position=idx,
                    status=cl_status,
                )
            )
        session.flush()
        campaign_id = campaign.id

    out = get_campaign(campaign_id)
    assert out.queued_count == 3
    assert out.queued_priority_count == 1
    assert out.queued_priority_count <= out.queued_count


def test_campaign_out_queued_priority_count_zero_when_no_priority(
    fresh_db, workspace_factory,
):
    """Zero ``not_found`` leads → ``queued_priority_count == 0``.

    Two queued leads, both ``ok``. Pins the no-priority path.
    """

    ws_id = workspace_factory()
    with fresh_db() as session:
        ws = session.get(Workspace, ws_id)
        campaign = Campaign(
            workspace_id=ws.id,
            name="No priority",
            goal="Book calls",
            outreach_per_day=10,
            connector_type="file",
            status=CampaignStatus.ACTIVE,
        )
        session.add(campaign)
        session.flush()

        for idx in (1, 2):
            lead = Lead(
                workspace_id=ws.id,
                name=f"Lead {idx}",
                contact_uri=f"+6140002{idx:04d}",
                contact_type="mobile",
                category="Retail",
                address="x",
                raw_data={},
                import_order=idx,
                source_file="seed",
                status=LeadStatus.NEW,
            )
            lead.enrichment_status = "ok"
            session.add(lead)
            session.flush()
            session.add(
                CampaignLead(
                    campaign_id=campaign.id,
                    lead_id=lead.id,
                    queue_position=idx,
                    status=CampaignLeadStatus.QUEUED,
                )
            )
        session.flush()
        campaign_id = campaign.id

    out = get_campaign(campaign_id)
    assert out.queued_count == 2
    assert out.queued_priority_count == 0


def test_queued_priority_count_includes_social_websites(
    fresh_db, workspace_factory,
):
    """``queued_priority_count`` covers social-as-website too (ticket 0014).

    Mix:

    * 1× queued ``not_found`` (Australia phone) — counts via the
      0013 branch.
    * 2× queued ``ok`` with Facebook + LinkedIn URLs — counts via
      the 0014 branch.
    * 1× queued ``ok`` with a real corporate website — does NOT count.
    * 1× queued ``ok`` with a path-only mention of a platform
      (``acme.com/about-our-facebook``) — does NOT count (host-only
      match).

    Pins the OR semantics in :func:`_campaign_queued_priority_bulk`.
    """

    ws_id = workspace_factory()
    with fresh_db() as session:
        ws = session.get(Workspace, ws_id)
        campaign = Campaign(
            workspace_id=ws.id,
            name="Mixed priority",
            goal="Book calls",
            outreach_per_day=10,
            connector_type="file",
            status=CampaignStatus.ACTIVE,
        )
        session.add(campaign)
        session.flush()

        fixtures = [
            # (enrichment_status, website, counts_as_priority?)
            ("not_found", None, True),
            ("ok", "https://facebook.com/Acme", True),
            ("ok", "https://www.linkedin.com/company/acme", True),
            ("ok", "https://acme.com.au", False),
            ("ok", "https://acme.com/about-our-facebook", False),
        ]
        for idx, (enrich, website, _) in enumerate(fixtures, start=1):
            lead = Lead(
                workspace_id=ws.id,
                name=f"Lead {idx}",
                contact_uri=f"+6140003{idx:04d}",
                contact_type="mobile",
                category="Retail",
                address="x",
                website=website,
                raw_data={},
                import_order=idx,
                source_file="seed",
                status=LeadStatus.NEW,
            )
            lead.enrichment_status = enrich
            session.add(lead)
            session.flush()
            session.add(
                CampaignLead(
                    campaign_id=campaign.id,
                    lead_id=lead.id,
                    queue_position=idx,
                    status=CampaignLeadStatus.QUEUED,
                )
            )
        session.flush()
        campaign_id = campaign.id

    out = get_campaign(campaign_id)
    expected_priority = sum(1 for *_ , counts in fixtures if counts)
    assert out.queued_count == len(fixtures)
    assert out.queued_priority_count == expected_priority
    assert out.queued_priority_count <= out.queued_count


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

    assert result.sent_today == 0
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


def test_delete_campaign_cascades_to_threads_messages_llm_calls(
    fresh_db, workspace_factory
):
    """Deleting a campaign tears down everything that hung off it.

    Children to disappear: ``campaign_lead`` rows, every ``thread`` they
    own, every ``message`` in those threads, and the ``llm_call`` audit
    rows tagged with the campaign id. Survives the delete: leads (they
    can be re-assigned to another campaign) and any campaign / thread
    that wasn't part of this deletion.
    """

    ws_id = workspace_factory()

    with fresh_db() as session:
        ws = session.get(Workspace, ws_id)

        target = Campaign(
            workspace_id=ws.id,
            name="Goner",
            goal="Book calls",
            outreach_per_day=5,
            connector_type="file",
            status=CampaignStatus.ACTIVE,
        )
        survivor = Campaign(
            workspace_id=ws.id,
            name="Survivor",
            goal="Stay alive",
            outreach_per_day=5,
            connector_type="file",
            status=CampaignStatus.ACTIVE,
        )
        session.add_all([target, survivor])
        session.flush()

        lead_a = Lead(
            workspace_id=ws.id,
            name="A",
            contact_uri="+61400000001",
            contact_type="mobile",
            category="Retail",
            address="Brisbane",
            raw_data={},
            import_order=1,
            source_file="seed",
            status=LeadStatus.CONTACTED,
        )
        lead_b = Lead(
            workspace_id=ws.id,
            name="B",
            contact_uri="+61400000002",
            contact_type="mobile",
            category="Retail",
            address="Brisbane",
            raw_data={},
            import_order=2,
            source_file="seed",
            status=LeadStatus.NEW,
        )
        session.add_all([lead_a, lead_b])
        session.flush()

        cl_target = CampaignLead(
            campaign_id=target.id,
            lead_id=lead_a.id,
            queue_position=1,
            status=CampaignLeadStatus.CONTACTED,
        )
        cl_survivor = CampaignLead(
            campaign_id=survivor.id,
            lead_id=lead_b.id,
            queue_position=1,
            status=CampaignLeadStatus.QUEUED,
        )
        session.add_all([cl_target, cl_survivor])
        session.flush()

        target_thread = Thread(
            campaign_lead_id=cl_target.id,
            connector_type="file",
            status=ThreadStatus.ACTIVE,
        )
        survivor_thread = Thread(
            campaign_lead_id=cl_survivor.id,
            connector_type="file",
            status=ThreadStatus.ACTIVE,
        )
        session.add_all([target_thread, survivor_thread])
        session.flush()

        session.add_all(
            [
                Message(
                    thread_id=target_thread.id,
                    role=MessageRole.AI,
                    content="hi A",
                    metadata_={},
                ),
                Message(
                    thread_id=target_thread.id,
                    role=MessageRole.LEAD,
                    content="reply A",
                    metadata_={},
                ),
                Message(
                    thread_id=survivor_thread.id,
                    role=MessageRole.AI,
                    content="hi B",
                    metadata_={},
                ),
            ]
        )
        session.add_all(
            [
                LlmCall(
                    workspace_id=ws.id,
                    campaign_id=target.id,
                    thread_id=target_thread.id,
                    lead_id=lead_a.id,
                    purpose=LlmCallPurpose.GENERATION,
                    model="test-model",
                    response_format="text",
                ),
                LlmCall(
                    workspace_id=ws.id,
                    campaign_id=survivor.id,
                    thread_id=survivor_thread.id,
                    lead_id=lead_b.id,
                    purpose=LlmCallPurpose.GENERATION,
                    model="test-model",
                    response_format="text",
                ),
            ]
        )
        session.flush()

        target_id = target.id
        survivor_id = survivor.id
        target_thread_id = target_thread.id
        survivor_thread_id = survivor_thread.id
        cl_target_id = cl_target.id
        cl_survivor_id = cl_survivor.id
        lead_a_id = lead_a.id
        lead_b_id = lead_b.id

    response = delete_campaign(target_id)
    assert response.status_code == 204

    with fresh_db() as session:
        # Target campaign + everything hanging off it is gone.
        assert session.get(Campaign, target_id) is None
        assert session.get(CampaignLead, cl_target_id) is None
        assert session.get(Thread, target_thread_id) is None
        assert (
            session.query(Message)
            .filter(Message.thread_id == target_thread_id)
            .count()
            == 0
        )
        assert (
            session.query(LlmCall)
            .filter(LlmCall.campaign_id == target_id)
            .count()
            == 0
        )

        # Survivor campaign + its tree is untouched.
        assert session.get(Campaign, survivor_id) is not None
        assert session.get(CampaignLead, cl_survivor_id) is not None
        assert session.get(Thread, survivor_thread_id) is not None
        assert (
            session.query(Message)
            .filter(Message.thread_id == survivor_thread_id)
            .count()
            == 1
        )
        assert (
            session.query(LlmCall)
            .filter(LlmCall.campaign_id == survivor_id)
            .count()
            == 1
        )

        # Leads survive — they're workspace-scoped and may move to other
        # campaigns. Only their assignment to the deleted campaign is gone.
        assert session.get(Lead, lead_a_id) is not None
        assert session.get(Lead, lead_b_id) is not None


def test_delete_campaign_returns_404_when_missing(fresh_db, workspace_factory):
    workspace_factory()
    with pytest.raises(HTTPException) as exc:
        delete_campaign("does-not-exist")
    assert exc.value.status_code == 404
    assert exc.value.detail == {"error": "campaign_not_found"}


def test_delete_campaign_with_no_leads_or_threads(fresh_db, workspace_factory):
    """Deleting a draft campaign that never sent anything is a clean no-op cascade."""

    ws_id = workspace_factory()
    with fresh_db() as session:
        ws = session.get(Workspace, ws_id)
        campaign = Campaign(
            workspace_id=ws.id,
            name="Empty",
            goal="g",
            outreach_per_day=5,
            connector_type="file",
            status=CampaignStatus.DRAFT,
        )
        session.add(campaign)
        session.flush()
        campaign_id = campaign.id

    response = delete_campaign(campaign_id)
    assert response.status_code == 204

    with fresh_db() as session:
        assert session.get(Campaign, campaign_id) is None


# ---------------------------------------------------------------------------
# outreach_window — per-campaign override + effective resolution
# ---------------------------------------------------------------------------


def test_campaign_out_exposes_effective_outreach_window(fresh_db, workspace_factory):
    """Default workspace = 8–17 enabled; campaign with no override inherits it."""

    ws_id = workspace_factory()
    with fresh_db() as session:
        ws = session.get(Workspace, ws_id)
        campaign = Campaign(
            workspace_id=ws.id,
            name="C",
            goal="g",
            outreach_per_day=5,
            connector_type="file",
            status=CampaignStatus.DRAFT,
        )
        session.add(campaign)
        session.flush()
        campaign_id = campaign.id

    out = get_campaign(campaign_id)
    assert out.outreach_window is None  # no per-campaign override
    assert out.effective_outreach_window.enabled is True
    assert out.effective_outreach_window.start_hour == 8
    assert out.effective_outreach_window.end_hour == 17


def test_campaign_create_persists_outreach_window_override(fresh_db, workspace_factory):
    """Sending an outreach_window on POST stores the per-campaign override."""

    workspace_factory()
    payload = CampaignCreate(
        name="Evening only",
        goal="g",
        outreach_per_day=10,
        outreach_window=OutreachWindowConfig(
            enabled=True, start_hour=18, end_hour=22
        ),
    )
    out = create_campaign(payload)

    assert out.outreach_window is not None
    assert out.outreach_window.start_hour == 18
    assert out.outreach_window.end_hour == 22
    # Effective window equals the override.
    assert out.effective_outreach_window.start_hour == 18
    assert out.effective_outreach_window.end_hour == 22


def test_campaign_patch_clears_override_with_explicit_null(
    fresh_db, workspace_factory
):
    """``PATCH {outreach_window: null}`` clears the override → inherit workspace."""

    ws_id = workspace_factory()
    with fresh_db() as session:
        ws = session.get(Workspace, ws_id)
        campaign = Campaign(
            workspace_id=ws.id,
            name="C",
            goal="g",
            outreach_per_day=5,
            connector_type="file",
            status=CampaignStatus.DRAFT,
            outreach_window={"enabled": True, "start_hour": 6, "end_hour": 10},
        )
        session.add(campaign)
        session.flush()
        campaign_id = campaign.id

    # Reality check: starts with the override.
    pre = get_campaign(campaign_id)
    assert pre.outreach_window is not None
    assert pre.outreach_window.start_hour == 6

    # Mimic the client sending ``{"outreach_window": null}`` so
    # ``model_dump(exclude_unset=True)`` keeps the field visible.
    payload = CampaignPatch.model_validate({"outreach_window": None})
    patched = patch_campaign(campaign_id, payload)

    assert patched.outreach_window is None
    # Falls back to workspace default 8–17.
    assert patched.effective_outreach_window.start_hour == 8
    assert patched.effective_outreach_window.end_hour == 17


def test_campaign_patch_does_not_touch_window_when_field_omitted(
    fresh_db, workspace_factory
):
    """PATCH without ``outreach_window`` must leave the override alone."""

    ws_id = workspace_factory()
    with fresh_db() as session:
        ws = session.get(Workspace, ws_id)
        campaign = Campaign(
            workspace_id=ws.id,
            name="C",
            goal="g",
            outreach_per_day=5,
            connector_type="file",
            status=CampaignStatus.DRAFT,
            outreach_window={"enabled": True, "start_hour": 9, "end_hour": 18},
        )
        session.add(campaign)
        session.flush()
        campaign_id = campaign.id

    # PATCH only the name.
    payload = CampaignPatch.model_validate({"name": "Renamed"})
    patched = patch_campaign(campaign_id, payload)

    assert patched.name == "Renamed"
    assert patched.outreach_window is not None
    assert patched.outreach_window.start_hour == 9
    assert patched.outreach_window.end_hour == 18
