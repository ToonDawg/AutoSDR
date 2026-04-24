"""Thread list + details + human-in-the-loop actions.

This is where the new "first-message-only + suggested replies" flow lives
on the wire:

* ``POST /api/threads/{id}/regenerate-suggestions`` — re-run the variant
  generator and stash the results on the thread, no send.
* ``POST /api/threads/{id}/send-draft`` — send one of the suggested drafts
  (or a freely typed one) via the real connector, then leave the thread
  paused for HITL again so the next inbound doesn't auto-reply.
* ``POST /api/threads/{id}/take-over`` / ``close`` — classic HITL
  terminators.
"""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import flag_modified

from autosdr.api.deps import db_session, require_workspace
from autosdr.api.schemas import (
    CloseThreadRequest,
    MessageOut,
    SendDraftRequest,
    TakeOverRequest,
    ThreadOut,
)
from autosdr.connectors import get_connector
from autosdr.connectors.base import OutgoingMessage
from autosdr.models import (
    Campaign,
    CampaignLead,
    CampaignLeadStatus,
    Lead,
    LeadStatus,
    Message,
    MessageRole,
    Thread,
    ThreadStatus,
)
from autosdr.pipeline.reply import HITL_AWAITING_HUMAN_REPLY
from autosdr.pipeline.suggestions import generate_reply_variants

router = APIRouter(prefix="/api/threads", tags=["threads"])


def _last_message_at(session: Session, thread_id: str) -> datetime | None:
    ts = session.execute(
        select(func.max(Message.created_at)).where(Message.thread_id == thread_id)
    ).scalar_one()
    return ts


def _build_thread_out(
    thread: Thread,
    campaign: Campaign | None,
    lead: Lead | None,
    last_message_at: datetime | None,
) -> ThreadOut:
    return ThreadOut(
        id=thread.id,
        campaign_id=campaign.id if campaign else "",
        campaign_name=campaign.name if campaign else "",
        lead_id=lead.id if lead else "",
        lead_name=lead.name if lead else None,
        lead_phone=lead.contact_uri if lead else None,
        lead_category=lead.category if lead else None,
        lead_address=lead.address if lead else None,
        connector_type=thread.connector_type,
        status=thread.status,
        auto_reply_count=thread.auto_reply_count,
        angle=thread.angle,
        tone_snapshot=thread.tone_snapshot,
        hitl_reason=thread.hitl_reason,
        hitl_context=thread.hitl_context,
        last_message_at=last_message_at or thread.created_at,
        created_at=thread.created_at,
    )


def _thread_to_out(session: Session, thread: Thread) -> ThreadOut:
    campaign_lead = session.get(CampaignLead, thread.campaign_lead_id)
    campaign = (
        session.get(Campaign, campaign_lead.campaign_id) if campaign_lead else None
    )
    lead = session.get(Lead, campaign_lead.lead_id) if campaign_lead else None
    return _build_thread_out(
        thread, campaign, lead, _last_message_at(session, thread.id)
    )


@router.get("", response_model=list[ThreadOut])
def list_threads(
    status_filter: str | None = None,
    campaign_id: str | None = None,
    limit: int = 200,
) -> list[ThreadOut]:
    limit = max(1, min(int(limit), 1000))
    with db_session() as session:
        require_workspace(session)
        stmt = select(Thread).limit(limit)
        if status_filter:
            stmt = stmt.where(Thread.status == status_filter)
        if campaign_id:
            stmt = (
                stmt.join(CampaignLead, CampaignLead.id == Thread.campaign_lead_id)
                .where(CampaignLead.campaign_id == campaign_id)
            )
        stmt = stmt.order_by(Thread.updated_at.desc())
        rows = list(session.execute(stmt).scalars())
        if not rows:
            return []

        # Batch the three per-thread lookups that used to fire in a loop:
        # campaign_lead → campaign+lead, plus max(created_at) per thread.
        thread_ids = [t.id for t in rows]
        cl_ids = {t.campaign_lead_id for t in rows if t.campaign_lead_id}
        campaign_leads: dict[str, CampaignLead] = {}
        if cl_ids:
            campaign_leads = {
                cl.id: cl
                for cl in session.execute(
                    select(CampaignLead).where(CampaignLead.id.in_(cl_ids))
                ).scalars()
            }

        campaign_ids = {cl.campaign_id for cl in campaign_leads.values()}
        lead_ids = {cl.lead_id for cl in campaign_leads.values()}
        campaigns = (
            {
                c.id: c
                for c in session.execute(
                    select(Campaign).where(Campaign.id.in_(campaign_ids))
                ).scalars()
            }
            if campaign_ids
            else {}
        )
        leads = (
            {
                ld.id: ld
                for ld in session.execute(
                    select(Lead).where(Lead.id.in_(lead_ids))
                ).scalars()
            }
            if lead_ids
            else {}
        )

        last_at_rows = session.execute(
            select(Message.thread_id, func.max(Message.created_at))
            .where(Message.thread_id.in_(thread_ids))
            .group_by(Message.thread_id)
        ).all()
        last_at: dict[str, datetime] = {tid: ts for tid, ts in last_at_rows}

        out: list[ThreadOut] = []
        for t in rows:
            cl = campaign_leads.get(t.campaign_lead_id) if t.campaign_lead_id else None
            campaign = campaigns.get(cl.campaign_id) if cl else None
            lead = leads.get(cl.lead_id) if cl else None
            out.append(_build_thread_out(t, campaign, lead, last_at.get(t.id)))
        return out


def _load_thread(session: Session, thread_id: str) -> Thread:
    thread = session.get(Thread, thread_id)
    if thread is None:
        raise HTTPException(status_code=404, detail={"error": "thread_not_found"})
    return thread


@router.get("/{thread_id}", response_model=ThreadOut)
def get_thread(thread_id: str) -> ThreadOut:
    with db_session() as session:
        require_workspace(session)
        thread = _load_thread(session, thread_id)
        return _thread_to_out(session, thread)


@router.get("/{thread_id}/messages", response_model=list[MessageOut])
def list_messages(thread_id: str) -> list[MessageOut]:
    with db_session() as session:
        require_workspace(session)
        _load_thread(session, thread_id)
        rows = (
            session.query(Message)
            .filter(Message.thread_id == thread_id)
            .order_by(Message.created_at.asc())
            .all()
        )
        return [MessageOut.model_validate(m) for m in rows]


@router.post("/{thread_id}/regenerate-suggestions", response_model=ThreadOut)
async def regenerate_suggestions(thread_id: str) -> ThreadOut:
    """Re-run the variant generator for a paused thread.

    Cheaper than asking the operator to fake an inbound — useful when the
    first batch of suggestions all feel wrong and they want another spin
    of the dice.
    """

    with db_session() as session:
        workspace = require_workspace(session)
        thread = _load_thread(session, thread_id)
        campaign_lead = session.get(CampaignLead, thread.campaign_lead_id)
        if campaign_lead is None:
            raise HTTPException(
                status_code=400, detail={"error": "thread_has_no_campaign_lead"}
            )
        campaign = session.get(Campaign, campaign_lead.campaign_id)
        lead = session.get(Lead, campaign_lead.lead_id)
        n = int((workspace.settings or {}).get("suggestions_count", 3))

        suggestions = await generate_reply_variants(
            session=session,
            workspace=workspace,
            campaign=campaign,
            lead=lead,
            thread=thread,
            n=n,
        )

        existing = dict(thread.hitl_context or {})
        existing["suggestions"] = suggestions
        thread.hitl_context = existing
        flag_modified(thread, "hitl_context")
        if thread.status == ThreadStatus.ACTIVE:
            thread.status = ThreadStatus.PAUSED_FOR_HITL
            thread.hitl_reason = HITL_AWAITING_HUMAN_REPLY
        session.flush()
        session.refresh(thread)
        return _thread_to_out(session, thread)


@router.post("/{thread_id}/send-draft", response_model=MessageOut)
async def send_draft(thread_id: str, payload: SendDraftRequest) -> MessageOut:
    """Send a draft (suggested or manually typed) out via the connector.

    The thread goes back to ``ACTIVE`` on success, but we intentionally do
    *not* prime a fresh set of suggestions — the whole point of first-
    message-only mode is that the next inbound regenerates them.
    """

    draft = (payload.draft or "").strip()
    if not draft:
        raise HTTPException(status_code=400, detail={"error": "empty_draft"})

    connector = get_connector()

    with db_session() as session:
        require_workspace(session)
        thread = _load_thread(session, thread_id)
        campaign_lead = session.get(CampaignLead, thread.campaign_lead_id)
        if campaign_lead is None:
            raise HTTPException(
                status_code=400, detail={"error": "thread_has_no_campaign_lead"}
            )
        lead = session.get(Lead, campaign_lead.lead_id)
        if lead is None or not lead.contact_uri:
            raise HTTPException(
                status_code=400, detail={"error": "lead_missing_contact_uri"}
            )

        # Hold the DB session open across the send so the message row is
        # atomically persisted with the thread/state change.
        send_result = await connector.send(
            OutgoingMessage(contact_uri=lead.contact_uri, content=draft)
        )
        if not send_result.success:
            raise HTTPException(
                status_code=502,
                detail={
                    "error": "connector_send_failed",
                    "reason": send_result.error,
                },
            )

        role = MessageRole.HUMAN if payload.source == "manual" else MessageRole.AI
        message = Message(
            thread_id=thread.id,
            role=role,
            content=draft,
            metadata_={
                "source": payload.source,
                "human_sent_at": datetime.now(tz=timezone.utc).isoformat(),
                "provider_message_id": send_result.provider_message_id,
            },
        )
        session.add(message)

        # Clear any stashed suggestions — they're stale the instant we
        # actually hit send. Leave thread ACTIVE so the lead's next reply
        # re-enters the pipeline.
        thread.status = ThreadStatus.ACTIVE
        thread.hitl_reason = None
        if thread.hitl_context:
            cleared = dict(thread.hitl_context)
            cleared.pop("suggestions", None)
            thread.hitl_context = cleared
            flag_modified(thread, "hitl_context")
        thread.auto_reply_count += 1

        # Propagate CRM statuses so Leads / Campaigns counters move.
        if campaign_lead.status == CampaignLeadStatus.QUEUED:
            campaign_lead.status = CampaignLeadStatus.CONTACTED
        if lead.status == LeadStatus.NEW:
            lead.status = LeadStatus.CONTACTED

        session.flush()
        session.refresh(message)
        return MessageOut.model_validate(message)


@router.post("/{thread_id}/take-over", response_model=ThreadOut)
def take_over(thread_id: str, payload: TakeOverRequest) -> ThreadOut:
    """Human explicitly pauses the thread — AI stops touching it."""

    with db_session() as session:
        require_workspace(session)
        thread = _load_thread(session, thread_id)
        thread.status = ThreadStatus.PAUSED_FOR_HITL
        thread.hitl_reason = "taken_over_by_human"
        ctx = dict(thread.hitl_context or {})
        if payload.note:
            ctx["note"] = payload.note
        thread.hitl_context = ctx
        flag_modified(thread, "hitl_context")
        session.flush()
        session.refresh(thread)
        return _thread_to_out(session, thread)


@router.post("/{thread_id}/close", response_model=ThreadOut)
def close_thread(thread_id: str, payload: CloseThreadRequest) -> ThreadOut:
    with db_session() as session:
        require_workspace(session)
        thread = _load_thread(session, thread_id)
        campaign_lead = session.get(CampaignLead, thread.campaign_lead_id)
        lead = session.get(Lead, campaign_lead.lead_id) if campaign_lead else None

        if payload.outcome == "won":
            thread.status = ThreadStatus.WON
            if campaign_lead:
                campaign_lead.status = CampaignLeadStatus.WON
            if lead:
                lead.status = LeadStatus.WON
        else:
            thread.status = ThreadStatus.LOST
            if campaign_lead:
                campaign_lead.status = CampaignLeadStatus.LOST
            if lead:
                lead.status = LeadStatus.LOST

        session.flush()
        session.refresh(thread)
        return _thread_to_out(session, thread)


__all__ = ["router"]
