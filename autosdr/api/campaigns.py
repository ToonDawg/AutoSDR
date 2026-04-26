"""Campaign CRUD + lead assignment.

Everything the former ``autosdr campaign`` CLI subcommands did now lives
here, so the operator can build a campaign end-to-end without shelling in.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from autosdr import killswitch
from autosdr.api.deps import db_session, require_workspace
from autosdr.api.schemas import (
    CampaignAssignLeads,
    CampaignCreate,
    CampaignKickoffRequest,
    CampaignKickoffResult,
    CampaignOut,
    CampaignPatch,
    FollowupConfig,
)
from autosdr.connectors import ConnectorError, get_connector
from autosdr.models import (
    Campaign,
    CampaignLead,
    CampaignLeadStatus,
    CampaignStatus,
    Lead,
    LeadStatus,
)
from autosdr.pipeline.followup import DEFAULT_FOLLOWUP_TEMPLATE
from autosdr.quota import count_ai_messages_last_24h_bulk
from autosdr.scheduler import _count_queued_leads, run_campaign_outreach_batch

router = APIRouter(prefix="/api/campaigns", tags=["campaigns"])

_CAMPAIGN_LEAD_STATUSES = (
    "queued",
    "sending",
    "paused_for_hitl",
    "contacted",
    "replied",
    "won",
    "lost",
    "skipped",
)


def _followup_for_out(raw: dict | None) -> FollowupConfig:
    """Coerce a raw ``campaign.followup`` blob to the response schema.

    ``None`` is treated as "feature off, defaults elsewhere" — same
    defaults the Pydantic model would apply. Enabled rows with blank
    templates inherit the backend default so the UI reflects what will send.
    """

    if not raw:
        return FollowupConfig()
    cfg = FollowupConfig.model_validate(raw)
    if cfg.enabled and not cfg.template.strip():
        cfg.template = DEFAULT_FOLLOWUP_TEMPLATE
    return cfg


def _followup_to_storage(value: FollowupConfig | None) -> dict | None:
    """Normalise an inbound ``FollowupConfig`` for persistence.

    ``None`` (operator didn't touch the field) is stored as ``None`` —
    equivalent to "disabled, use defaults if we ever flip it on". A
    provided object always persists as a full dict so downstream readers
    don't have to cope with half-filled rows.
    """

    if value is None:
        return None
    data = value.model_dump()
    if data.get("enabled") and not str(data.get("template") or "").strip():
        data["template"] = DEFAULT_FOLLOWUP_TEMPLATE
    return data


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
        followup=_followup_for_out(campaign.followup),
        quota_reset_at=campaign.quota_reset_at,
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
            followup=_followup_to_storage(payload.followup),
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
    # ``exclude_unset`` so only operator-touched fields overwrite the DB —
    # otherwise a PATCH of just ``{name: ...}`` would clobber followup
    # back to defaults.
    updates = payload.model_dump(exclude_unset=True)
    with db_session() as session:
        require_workspace(session)
        campaign = session.get(Campaign, campaign_id)
        if campaign is None:
            raise HTTPException(status_code=404, detail={"error": "campaign_not_found"})
        if "followup" in updates:
            campaign.followup = _followup_to_storage(payload.followup)
            updates.pop("followup")
        if "outreach_per_day" in updates and updates["outreach_per_day"] is not None:
            updates["outreach_per_day"] = max(1, int(updates["outreach_per_day"]))
        if "name" in updates and updates["name"] is not None:
            updates["name"] = updates["name"].strip()
        if "goal" in updates and updates["goal"] is not None:
            updates["goal"] = updates["goal"].strip()
        for field_name, value in updates.items():
            if value is None:
                continue
            setattr(campaign, field_name, value)
        session.flush()
        session.refresh(campaign)
        return _to_out(session, campaign)


@router.post("/{campaign_id}/reset-send-count", response_model=CampaignOut)
def reset_send_count(campaign_id: str) -> CampaignOut:
    """Start a fresh quota window for this campaign without altering history."""

    with db_session() as session:
        require_workspace(session)
        campaign = session.get(Campaign, campaign_id)
        if campaign is None:
            raise HTTPException(status_code=404, detail={"error": "campaign_not_found"})
        campaign.quota_reset_at = datetime.now(tz=timezone.utc)
        session.flush()
        session.refresh(campaign)
        return _to_out(session, campaign)


@router.post("/{campaign_id}/kickoff", response_model=CampaignKickoffResult)
async def kickoff_campaign(
    campaign_id: str, payload: CampaignKickoffRequest
) -> CampaignKickoffResult:
    """Manually send the next N queued leads for a campaign.

    This is an explicit operator action, so it bypasses the pause flag and the
    campaign's rolling quota. Shutdown still aborts the send path.
    """

    with db_session() as session:
        workspace = require_workspace(session)
        campaign = session.get(Campaign, campaign_id)
        if campaign is None:
            raise HTTPException(status_code=404, detail={"error": "campaign_not_found"})
        if campaign.status == CampaignStatus.COMPLETED:
            raise HTTPException(status_code=409, detail={"error": "campaign_completed"})

        try:
            connector = get_connector()
        except ConnectorError as exc:
            raise HTTPException(
                status_code=409,
                detail={"error": "connector_unavailable", "detail": str(exc)},
            ) from exc

        try:
            with killswitch.allow_manual_send():
                batch = await run_campaign_outreach_batch(
                    session=session,
                    connector=connector,
                    workspace=workspace,
                    campaign=campaign,
                    max_count=payload.count,
                    respect_quota=False,
                )
        except killswitch.KillSwitchTripped as exc:
            raise HTTPException(
                status_code=409, detail={"error": "system_shutting_down"}
            ) from exc

        session.refresh(campaign)
        return CampaignKickoffResult(
            requested=payload.count,
            attempted=batch.attempted,
            sent=batch.sent,
            failed=batch.failed,
            remaining_queued=_count_queued_leads(session, campaign.id),
            campaign=_to_out(session, campaign),
        )


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
