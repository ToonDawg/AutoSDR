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

import logging
from datetime import datetime, timezone
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import flag_modified

from autosdr import killswitch
from autosdr.api.deps import db_session, require_workspace
from autosdr.api.schemas import (
    CloseThreadRequest,
    HitlCount,
    MessageOut,
    RequeueThreadsRequest,
    RequeueThreadsResponse,
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
from autosdr.db import session_scope
from autosdr.pipeline._shared import thread_history
from autosdr.pipeline.followup import schedule_followup_send
from autosdr.pipeline.reply import HITL_AWAITING_HUMAN_REPLY
from autosdr.pipeline.suggestions import generate_reply_variants

logger = logging.getLogger(__name__)

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
        tone_register=thread.tone_register,
        tone_snapshot=thread.tone_snapshot,
        hitl_reason=thread.hitl_reason,
        hitl_context=thread.hitl_context,
        hitl_dismissed_at=thread.hitl_dismissed_at,
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
    lead_id: str | None = None,
    dismissed: bool | None = None,
    hitl_reason: Annotated[
        str | None,
        Query(
            description=(
                "Filter to threads whose ``hitl_reason`` matches exactly. "
                "Drives the Inbox filter chips (ticket 0018) — combined "
                "with ``status_filter=paused_for_hitl`` + "
                "``dismissed=false`` it returns the operator's actionable "
                "slice of one HITL bucket (e.g. ``connector_send_failed``)."
            ),
        ),
    ] = None,
    limit: int = 200,
    offset: int = 0,
) -> list[ThreadOut]:
    limit = max(1, min(int(limit), 1000))
    offset = max(0, int(offset))
    with db_session() as session:
        require_workspace(session)
        stmt = select(Thread)
        if status_filter:
            stmt = stmt.where(Thread.status == status_filter)
        if hitl_reason:
            stmt = stmt.where(Thread.hitl_reason == hitl_reason)
        if dismissed is True:
            stmt = stmt.where(Thread.hitl_dismissed_at.is_not(None))
        elif dismissed is False:
            stmt = stmt.where(Thread.hitl_dismissed_at.is_(None))
        if campaign_id or lead_id:
            filters = []
            if campaign_id:
                filters.append(CampaignLead.campaign_id == campaign_id)
            if lead_id:
                filters.append(CampaignLead.lead_id == lead_id)
            stmt = (
                stmt.join(CampaignLead, CampaignLead.id == Thread.campaign_lead_id)
                .where(*filters)
            )
        stmt = stmt.order_by(Thread.updated_at.desc()).offset(offset).limit(limit)
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


@router.get("/hitl/count", response_model=HitlCount)
def hitl_count() -> HitlCount:
    """Cheap counter for the sidebar/dashboard badges + Inbox filter chips.

    Replaces the older pattern of pulling the full HITL list just to read
    ``len(...)``: at scale that fan-outs into a JSON payload of every paused
    thread on every refresh tick, which we don't want.

    ``by_reason`` (ticket 0018) drives the Inbox filter chip row — one
    chip per ``hitl_reason`` token, with the count rendered alongside
    so the operator sees "connector_send_failed · 1000" before they
    bulk-retry. NULL reasons (legacy threads from before the column
    existed) are bucketed under ``"unknown"`` so the sum of
    ``by_reason.values()`` equals ``active``.
    """

    with db_session() as session:
        require_workspace(session)
        active = session.execute(
            select(func.count(Thread.id))
            .where(Thread.status == ThreadStatus.PAUSED_FOR_HITL)
            .where(Thread.hitl_dismissed_at.is_(None))
        ).scalar_one()
        dismissed = session.execute(
            select(func.count(Thread.id))
            .where(Thread.status == ThreadStatus.PAUSED_FOR_HITL)
            .where(Thread.hitl_dismissed_at.is_not(None))
        ).scalar_one()
        # Per-reason breakdown is a single GROUP BY on the same index
        # the active/dismissed counts already hit (``idx_thread_status``).
        # Cheap aggregate; runs once per Inbox refresh tick.
        breakdown_rows = session.execute(
            select(
                func.coalesce(Thread.hitl_reason, "unknown").label("reason"),
                func.count(Thread.id).label("n"),
            )
            .where(Thread.status == ThreadStatus.PAUSED_FOR_HITL)
            .where(Thread.hitl_dismissed_at.is_(None))
            .group_by("reason")
        ).all()
        by_reason = {str(row.reason): int(row.n) for row in breakdown_rows}
        return HitlCount(
            active=int(active),
            dismissed=int(dismissed),
            by_reason=by_reason,
        )


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

    # Phase 1: load context + release the SQLite write lock before any LLM
    # call. Holding the txn open while the parallel suggestion generations
    # run would deadlock against their ``LlmCall`` audit-row inserts (see
    # :mod:`autosdr.pipeline.suggestions` for the full anatomy). We only
    # need the txn long enough to read the thread/campaign/lead snapshot
    # plus the message history.
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
        history = thread_history(session, thread)
        thread_id_snapshot = thread.id
        # Detach the ORM objects so the helper code below can read scalar
        # attributes (workspace.settings, thread.angle, …) after the session
        # closes. ``expire_on_commit=False`` is configured on the
        # sessionmaker so attribute reads on detached instances continue to
        # return their cached values.
        session.expunge_all()

    suggestions = await generate_reply_variants(
        workspace=workspace,
        campaign=campaign,
        lead=lead,
        thread=thread,
        history=history,
        n=n,
    )

    # Phase 2: persist results in a fresh, short-lived transaction.
    with session_scope() as session:
        thread = _load_thread(session, thread_id_snapshot)
        existing = dict(thread.hitl_context or {})
        existing["suggestions"] = suggestions
        thread.hitl_context = existing
        flag_modified(thread, "hitl_context")
        if thread.status == ThreadStatus.ACTIVE:
            thread.status = ThreadStatus.PAUSED_FOR_HITL
            thread.hitl_reason = HITL_AWAITING_HUMAN_REPLY
        # Fresh suggestions = a new reason for the human to look. Re-surface
        # the thread if it was previously dismissed.
        thread.hitl_dismissed_at = None
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
        campaign = session.get(Campaign, campaign_lead.campaign_id)
        lead = session.get(Lead, campaign_lead.lead_id)
        if lead is None or not lead.contact_uri:
            raise HTTPException(
                status_code=400, detail={"error": "lead_missing_contact_uri"}
            )

        # Detect "this is the first outbound on the thread" — drives
        # whether the follow-up beat fires. Operator-driven sends after
        # a reply shouldn't schedule follow-ups; the beat only exists
        # to add texture to the cold open.
        prior_message_count = session.execute(
            select(func.count(Message.id)).where(Message.thread_id == thread.id)
        ).scalar_one()
        is_first_outbound = int(prior_message_count) == 0
        first_outbound_claimed = False
        first_outbound_contact_uri = (lead.contact_uri or "").strip()

        if is_first_outbound:
            if campaign_lead.status == CampaignLeadStatus.SENDING:
                raise HTTPException(
                    status_code=409, detail={"error": "send_in_progress"}
                )
            result = session.execute(
                update(CampaignLead)
                .where(
                    CampaignLead.id == campaign_lead.id,
                    CampaignLead.status.in_(
                        [
                            CampaignLeadStatus.QUEUED,
                            CampaignLeadStatus.PAUSED_FOR_HITL,
                        ]
                    ),
                )
                .values(status=CampaignLeadStatus.SENDING)
                .execution_options(synchronize_session=False)
            )
            if result.rowcount != 1:
                raise HTTPException(
                    status_code=409,
                    detail={"error": "first_outbound_not_sendable"},
                )
            session.commit()
            first_outbound_claimed = True
            session.refresh(thread)
            session.refresh(campaign_lead)
            session.refresh(lead)
            current_contact_uri = (lead.contact_uri or "").strip()
            if not current_contact_uri or current_contact_uri != first_outbound_contact_uri:
                campaign_lead.status = CampaignLeadStatus.PAUSED_FOR_HITL
                session.commit()
                raise HTTPException(
                    status_code=409,
                    detail={"error": "lead_contact_uri_changed"},
                )

        # Hold the DB session open across the send so the message row is
        # atomically persisted with the thread/state change.
        try:
            with killswitch.allow_manual_send():
                send_result = await connector.send(
                    OutgoingMessage(contact_uri=lead.contact_uri, content=draft)
                )
        except killswitch.KillSwitchTripped as exc:
            if first_outbound_claimed:
                campaign_lead.status = CampaignLeadStatus.PAUSED_FOR_HITL
                session.commit()
            raise HTTPException(
                status_code=409,
                detail={"error": "system_shutting_down"},
            ) from exc
        except Exception as exc:
            if first_outbound_claimed:
                campaign_lead.status = CampaignLeadStatus.PAUSED_FOR_HITL
                session.commit()
            raise HTTPException(
                status_code=502,
                detail={
                    "error": "connector_send_failed",
                    "reason": f"connector_exception:{exc}",
                },
            ) from exc
        if not send_result.success:
            if first_outbound_claimed:
                campaign_lead.status = CampaignLeadStatus.PAUSED_FOR_HITL
                session.commit()
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

        # Clear stashed suggestions and drafts — stale the instant we send.
        # Leave thread ACTIVE so the lead's next reply re-enters the pipeline.
        thread.status = ThreadStatus.ACTIVE
        thread.hitl_reason = None
        if thread.hitl_context:
            cleared = dict(thread.hitl_context)
            cleared.pop("suggestions", None)
            cleared.pop("last_drafts", None)
            thread.hitl_context = cleared
            flag_modified(thread, "hitl_context")
        thread.auto_reply_count += 1

        # Propagate CRM statuses so Leads / Campaigns counters move.
        if campaign_lead.status in {
            CampaignLeadStatus.QUEUED,
            CampaignLeadStatus.SENDING,
            CampaignLeadStatus.PAUSED_FOR_HITL,
        }:
            campaign_lead.status = CampaignLeadStatus.CONTACTED
        if lead.status == LeadStatus.NEW:
            lead.status = LeadStatus.CONTACTED

        session.flush()
        session.refresh(message)
        message_out = MessageOut.model_validate(message)
        parent_message_id = message.id
        campaign_followup = campaign.followup if campaign is not None else None
        contact_uri = lead.contact_uri
        lead_name = lead.name

    # Outside the session — only schedule a follow-up on the first outbound.
    if is_first_outbound:
        schedule_followup_send(
            campaign_followup=campaign_followup,
            thread_id=thread_id,
            parent_message_id=parent_message_id,
            contact_uri=contact_uri,
            lead_name=lead_name,
            connector=connector,
        )

    return message_out


@router.post("/requeue", response_model=RequeueThreadsResponse)
def requeue_threads(payload: RequeueThreadsRequest) -> RequeueThreadsResponse:
    """Re-queue connector-failed threads back into the normal scheduler flow.

    Flips ``Thread.status`` to ACTIVE and ``CampaignLead.status`` to QUEUED
    so the scheduler picks them up within the campaign's ``outreach_per_day``
    daily limit. The draft is regenerated by the outreach pipeline — this is
    intentional; the context may have changed since the original failure.
    """

    thread_ids = list(dict.fromkeys(payload.thread_ids))
    requeued = 0
    skipped = 0
    with db_session() as session:
        require_workspace(session)
        for thread_id in thread_ids:
            thread = session.get(Thread, thread_id)
            if thread is None or thread.status != ThreadStatus.PAUSED_FOR_HITL:
                skipped += 1
                continue
            campaign_lead = session.get(CampaignLead, thread.campaign_lead_id)
            if campaign_lead is None:
                skipped += 1
                continue
            thread.status = ThreadStatus.ACTIVE
            thread.hitl_reason = None
            # Keep last_drafts so the outreach pipeline can send the
            # already-generated message without calling the LLM again.
            ctx = dict(thread.hitl_context or {})
            ctx.pop("connector_error", None)
            thread.hitl_context = ctx or None
            thread.hitl_dismissed_at = None
            campaign_lead.status = CampaignLeadStatus.QUEUED
            requeued += 1
        session.flush()
    return RequeueThreadsResponse(requeued=requeued, skipped=skipped)


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
        thread.hitl_dismissed_at = None
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


@router.post("/{thread_id}/dismiss", response_model=ThreadOut)
def dismiss_thread(thread_id: str) -> ThreadOut:
    """Acknowledge a HITL thread without changing its outcome.

    The thread stays ``paused_for_hitl`` — the operator just doesn't want it
    nagging them in the inbox right now. A *new* HITL event (lead replies,
    eval fails again, take-over, regenerate) will clear the flag and the
    thread re-surfaces. See ``pause_thread_for_hitl``.
    """

    with db_session() as session:
        require_workspace(session)
        thread = _load_thread(session, thread_id)
        if thread.status != ThreadStatus.PAUSED_FOR_HITL:
            raise HTTPException(
                status_code=409,
                detail={"error": "thread_not_in_hitl_state"},
            )
        thread.hitl_dismissed_at = datetime.now(tz=timezone.utc)
        session.flush()
        session.refresh(thread)
        return _thread_to_out(session, thread)


@router.post("/{thread_id}/restore", response_model=ThreadOut)
def restore_thread(thread_id: str) -> ThreadOut:
    """Undo a previous dismiss — pull the thread back onto the inbox."""

    with db_session() as session:
        require_workspace(session)
        thread = _load_thread(session, thread_id)
        thread.hitl_dismissed_at = None
        session.flush()
        session.refresh(thread)
        return _thread_to_out(session, thread)


__all__ = ["router"]
