"""Campaign CRUD + lead assignment.

Everything the former ``autosdr campaign`` CLI subcommands did now lives
here, so the operator can build a campaign end-to-end without shelling in.
"""

from __future__ import annotations

from collections.abc import Iterable

from fastapi import APIRouter, HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from autosdr.api.deps import db_session, require_workspace
from autosdr.api.schemas import (
    CampaignAssignLeads,
    CampaignCreate,
    CampaignOut,
    CampaignPatch,
)
from autosdr.models import (
    Campaign,
    CampaignLead,
    CampaignLeadStatus,
    CampaignStatus,
    Lead,
    LeadStatus,
)
from autosdr.quota import count_ai_messages_last_24h_bulk

router = APIRouter(prefix="/api/campaigns", tags=["campaigns"])

_CAMPAIGN_LEAD_STATUSES = ("queued", "contacted", "replied", "won", "lost", "skipped")


def _campaign_totals_bulk(
    session: Session, campaign_ids: Iterable[str]
) -> dict[str, dict[str, int]]:
    """Return per-campaign ``{status: count}`` for every id in one query.

    The list endpoint previously ran a GROUP BY per campaign, which is
    ``O(N)`` round-trips. Batching to a single grouped select keeps the
    whole list page to a constant number of queries.
    """

    ids = list(dict.fromkeys(campaign_ids))
    totals: dict[str, dict[str, int]] = {
        cid: {s: 0 for s in _CAMPAIGN_LEAD_STATUSES} for cid in ids
    }
    if not ids:
        return totals
    rows = session.execute(
        select(CampaignLead.campaign_id, CampaignLead.status, func.count(CampaignLead.id))
        .where(CampaignLead.campaign_id.in_(ids))
        .group_by(CampaignLead.campaign_id, CampaignLead.status)
    ).all()
    for campaign_id, status_name, count in rows:
        bucket = totals.setdefault(
            campaign_id, {s: 0 for s in _CAMPAIGN_LEAD_STATUSES}
        )
        bucket[status_name] = int(count)
    return totals


def _build_out(
    campaign: Campaign,
    totals: dict[str, int],
    sent_24h: int,
) -> CampaignOut:
    lead_count = sum(totals.values())
    return CampaignOut(
        id=campaign.id,
        name=campaign.name,
        goal=campaign.goal,
        outreach_per_day=campaign.outreach_per_day,
        connector_type=campaign.connector_type,
        status=campaign.status,
        created_at=campaign.created_at,
        lead_count=lead_count,
        contacted_count=totals["contacted"] + totals["replied"] + totals["won"] + totals["lost"],
        replied_count=totals["replied"] + totals["won"] + totals["lost"],
        won_count=totals["won"],
        sent_24h=sent_24h,
    )


def _to_out(session: Session, campaign: Campaign) -> CampaignOut:
    totals = _campaign_totals_bulk(session, [campaign.id])[campaign.id]
    sent_24h = count_ai_messages_last_24h_bulk(session, [campaign.id]).get(campaign.id, 0)
    return _build_out(campaign, totals, sent_24h)


@router.get("", response_model=list[CampaignOut])
def list_campaigns() -> list[CampaignOut]:
    with db_session() as session:
        require_workspace(session)
        rows = list(
            session.execute(
                select(Campaign).order_by(Campaign.created_at.desc())
            ).scalars()
        )
        if not rows:
            return []
        ids = [c.id for c in rows]
        totals_by_campaign = _campaign_totals_bulk(session, ids)
        sent_24h_by_campaign = count_ai_messages_last_24h_bulk(session, ids)
        return [
            _build_out(
                c,
                totals_by_campaign[c.id],
                sent_24h_by_campaign.get(c.id, 0),
            )
            for c in rows
        ]


@router.post("", response_model=CampaignOut, status_code=status.HTTP_201_CREATED)
def create_campaign(payload: CampaignCreate) -> CampaignOut:
    with db_session() as session:
        workspace = require_workspace(session)
        connector_type = (
            payload.connector_type
            or (workspace.settings or {}).get("connector", {}).get("type")
            or "file"
        )
        campaign = Campaign(
            workspace_id=workspace.id,
            name=payload.name.strip(),
            goal=payload.goal.strip(),
            outreach_per_day=max(1, int(payload.outreach_per_day)),
            connector_type=connector_type,
            status=CampaignStatus.DRAFT,
        )
        session.add(campaign)
        session.flush()
        session.refresh(campaign)
        return _to_out(session, campaign)


@router.get("/{campaign_id}", response_model=CampaignOut)
def get_campaign(campaign_id: str) -> CampaignOut:
    with db_session() as session:
        require_workspace(session)
        campaign = session.get(Campaign, campaign_id)
        if campaign is None:
            raise HTTPException(status_code=404, detail={"error": "campaign_not_found"})
        return _to_out(session, campaign)


@router.patch("/{campaign_id}", response_model=CampaignOut)
def patch_campaign(campaign_id: str, payload: CampaignPatch) -> CampaignOut:
    updates = payload.model_dump(exclude_unset=True)
    with db_session() as session:
        require_workspace(session)
        campaign = session.get(Campaign, campaign_id)
        if campaign is None:
            raise HTTPException(status_code=404, detail={"error": "campaign_not_found"})
        for field_name, value in updates.items():
            setattr(campaign, field_name, value)
        session.flush()
        session.refresh(campaign)
        return _to_out(session, campaign)


@router.post("/{campaign_id}/activate", response_model=CampaignOut)
def activate_campaign(campaign_id: str) -> CampaignOut:
    return _set_status(campaign_id, CampaignStatus.ACTIVE)


@router.post("/{campaign_id}/pause", response_model=CampaignOut)
def pause_campaign(campaign_id: str) -> CampaignOut:
    return _set_status(campaign_id, CampaignStatus.PAUSED)


@router.post("/{campaign_id}/complete", response_model=CampaignOut)
def complete_campaign(campaign_id: str) -> CampaignOut:
    return _set_status(campaign_id, CampaignStatus.COMPLETED)


def _set_status(campaign_id: str, new_status: str) -> CampaignOut:
    with db_session() as session:
        require_workspace(session)
        campaign = session.get(Campaign, campaign_id)
        if campaign is None:
            raise HTTPException(status_code=404, detail={"error": "campaign_not_found"})
        campaign.status = new_status
        session.flush()
        session.refresh(campaign)
        return _to_out(session, campaign)


@router.post("/{campaign_id}/assign-leads", response_model=CampaignOut)
def assign_leads(campaign_id: str, payload: CampaignAssignLeads) -> CampaignOut:
    """Push leads into the campaign queue.

    Two modes:

    * ``all_eligible=true`` assigns every ``status='new'`` lead not already
      in this campaign. This is the one-click flow after import.
    * ``lead_ids=[...]`` assigns a specific set — used by per-lead selection
      in the Leads page.
    """

    with db_session() as session:
        require_workspace(session)
        campaign = session.get(Campaign, campaign_id)
        if campaign is None:
            raise HTTPException(status_code=404, detail={"error": "campaign_not_found"})

        existing_lead_ids = set(
            session.execute(
                select(CampaignLead.lead_id).where(
                    CampaignLead.campaign_id == campaign.id
                )
            ).scalars()
        )
        base_position = int(
            session.execute(
                select(func.coalesce(func.max(CampaignLead.queue_position), 0)).where(
                    CampaignLead.campaign_id == campaign.id
                )
            ).scalar_one()
        )

        if payload.all_eligible:
            leads = list(
                session.execute(
                    select(Lead)
                    .where(
                        Lead.workspace_id == campaign.workspace_id,
                        Lead.status == LeadStatus.NEW,
                        ~Lead.id.in_(existing_lead_ids) if existing_lead_ids else True,
                    )
                    .order_by(Lead.import_order.asc())
                ).scalars()
            )
        else:
            leads = list(
                session.execute(
                    select(Lead).where(
                        Lead.workspace_id == campaign.workspace_id,
                        Lead.id.in_(payload.lead_ids or []),
                        ~Lead.id.in_(existing_lead_ids) if existing_lead_ids else True,
                    )
                ).scalars()
            )

        for idx, lead in enumerate(leads, start=1):
            session.add(
                CampaignLead(
                    campaign_id=campaign.id,
                    lead_id=lead.id,
                    queue_position=base_position + idx,
                    status=CampaignLeadStatus.QUEUED,
                )
            )
        session.flush()
        session.refresh(campaign)
        return _to_out(session, campaign)


__all__ = ["router"]
