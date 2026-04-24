"""Reply pipeline: inbound webhook -> classify -> route.

Routing strategy (first-message-only mode, the default):

- ``intent == "negative"``     → close lost.
- ``intent == "goal_achieved"`` → close won.
- anything else                → pause for human, stash N suggested replies.

If ``workspace.settings.auto_reply_enabled`` is flipped to ``true`` the old
classify → generate → evaluate → send loop is preserved for backwards
compatibility, but the default posture is "send the first message, then
hand the rest to a human".

Multi-campaign resolution:

- Resolve lead by (workspace_id, contact_uri) after E.164 normalisation.
- If the lead has multiple active threads, route to the thread whose
  most-recent outbound message is the most recent. Ties broken by thread.id.
- If no active thread exists, write to ``unmatched_webhook``.

Concurrency:

- The reply processor acquires a per-thread lock before classifying. SQLite
  gets this via ``BEGIN IMMEDIATE`` (serialises writers); Postgres via
  ``SELECT ... FOR UPDATE``. The second processor re-reads history after
  acquiring the lock so it sees the first inbound before classifying.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from autosdr.connectors.base import BaseConnector, IncomingMessage, OutgoingMessage
from autosdr.db import session_scope
from autosdr.importer import normalise_phone
from autosdr.llm import LlmCallContext, complete_json
from autosdr.models import (
    Campaign,
    CampaignLead,
    CampaignLeadStatus,
    Lead,
    LeadStatus,
    LlmCallPurpose,
    Message,
    MessageRole,
    Thread,
    ThreadStatus,
    UnmatchedWebhook,
    Workspace,
)
from autosdr.pipeline._shared import (
    build_send_metadata,
    generate_and_evaluate,
    hitl_context_from_loop_failure,
    hitl_context_from_send_failure,
    pause_thread_for_hitl,
    read_loop_settings,
    thread_history,
)
from autosdr.pipeline.suggestions import generate_reply_variants
from autosdr.prompts import classification

logger = logging.getLogger(__name__)


# HITL reason when we pause a thread because auto-reply is off and we're
# waiting for the operator to pick one of the suggested drafts.
HITL_AWAITING_HUMAN_REPLY = "awaiting_human_reply"


@dataclass
class ReplyResult:
    action: str  # sent | escalated_hitl | closed_won | closed_lost | unmatched | ignored
    thread_id: str | None = None
    intent: str | None = None
    confidence: float | None = None
    detail: str | None = None


def _resolve_contact(contact_uri: str, region_hint: str) -> str | None:
    normalised, _type = normalise_phone(contact_uri, region_hint=region_hint)
    return normalised


def _resolve_thread(session: Session, workspace_id: str, contact_uri: str) -> Thread | None:
    """Find the target thread per the multi-campaign routing rule."""

    lead = session.execute(
        select(Lead).where(
            Lead.workspace_id == workspace_id, Lead.contact_uri == contact_uri
        )
    ).scalar_one_or_none()
    if lead is None:
        return None

    active_threads = (
        session.query(Thread)
        .join(CampaignLead, Thread.campaign_lead_id == CampaignLead.id)
        .filter(
            CampaignLead.lead_id == lead.id,
            Thread.status.notin_(list(ThreadStatus.CLOSED)),
        )
        .all()
    )
    if not active_threads:
        return None
    if len(active_threads) == 1:
        return active_threads[0]

    def _latest_outbound_ts(t: Thread):
        last = (
            session.query(Message.created_at)
            .filter(Message.thread_id == t.id, Message.role == MessageRole.AI)
            .order_by(Message.created_at.desc())
            .first()
        )
        return (last[0] if last else t.created_at, t.id)

    active_threads.sort(key=_latest_outbound_ts, reverse=True)
    return active_threads[0]


def _lock_thread(session: Session, thread_id: str) -> Thread | None:
    """Acquire a row-level lock on a thread for reply processing."""

    dialect = session.bind.dialect.name if session.bind else "sqlite"
    if dialect in {"postgresql", "postgres"}:
        stmt = select(Thread).where(Thread.id == thread_id).with_for_update()
        return session.execute(stmt).scalar_one_or_none()
    return session.get(Thread, thread_id)


@dataclass
class _Classification:
    """Normalised classifier output, threaded through the reply pipeline."""

    intent: str
    confidence: float
    reason: str | None
    requires_human: bool
    llm_call_id: str | None


async def process_incoming_message(
    *,
    connector: BaseConnector,
    workspace_id: str,
    incoming: IncomingMessage,
) -> ReplyResult:
    """Main entry point — called from the webhook background task or poller.

    This is a thin coordinator: it owns the transaction and the "which
    mode are we in" decision tree. The actual work is in the helpers
    below so each mode can be understood (and eventually tested) on its
    own.
    """

    logger.info(
        "inbound received from=%s provider_id=%s chars=%d",
        incoming.contact_uri,
        incoming.provider_message_id,
        len(incoming.content),
    )

    with session_scope() as session:
        workspace = session.get(Workspace, workspace_id)
        if workspace is None:
            logger.error("workspace %s not found while processing inbound", workspace_id)
            return ReplyResult(action="ignored", detail="workspace_missing")

        settings_blob = dict(workspace.settings or {})

        resolved = _resolve_and_capture_inbound(
            session=session,
            connector=connector,
            workspace=workspace,
            incoming=incoming,
            settings_blob=settings_blob,
        )
        if isinstance(resolved, ReplyResult):
            return resolved
        thread, campaign_lead, campaign, lead = resolved

        history = thread_history(session, thread)
        settings_llm = settings_blob.get("llm") or {}

        cls = await _classify_reply(
            workspace=workspace,
            campaign=campaign,
            lead=lead,
            thread=thread,
            history=history,
            incoming=incoming,
            settings_llm=settings_llm,
        )

        terminal = _route_terminal_intent(
            thread=thread,
            campaign_lead=campaign_lead,
            lead=lead,
            classification=cls,
        )
        if terminal is not None:
            return terminal

        if not bool(settings_blob.get("auto_reply_enabled", False)):
            return await _park_with_suggestions(
                session=session,
                workspace=workspace,
                campaign=campaign,
                lead=lead,
                thread=thread,
                classification=cls,
                incoming=incoming,
                settings_blob=settings_blob,
            )

        return await _run_auto_reply(
            session=session,
            connector=connector,
            workspace=workspace,
            campaign=campaign,
            campaign_lead=campaign_lead,
            lead=lead,
            thread=thread,
            history=history,
            classification=cls,
            incoming=incoming,
            settings_blob=settings_blob,
            settings_llm=settings_llm,
        )


# ---------------------------------------------------------------------------
# Stage 1: resolve the inbound to a thread and record the message
# ---------------------------------------------------------------------------


def _resolve_and_capture_inbound(
    *,
    session: Session,
    connector: BaseConnector,
    workspace: Workspace,
    incoming: IncomingMessage,
    settings_blob: dict[str, Any],
) -> tuple[Thread, CampaignLead, Campaign, Lead] | ReplyResult:
    """Find the target thread and record the inbound ``Message``.

    Returns either the resolved ``(thread, campaign_lead, campaign, lead)``
    tuple (caller continues) or a short-circuit ``ReplyResult`` for the
    unparseable / unmatched / paused-thread cases. Callers should treat
    a ``ReplyResult`` as terminal.
    """

    region_hint = settings_blob.get("default_region", "AU")
    normalised = _resolve_contact(incoming.contact_uri, region_hint)
    if normalised is None:
        logger.warning(
            "inbound unparseable sender=%r — dropping to unmatched_webhook",
            incoming.contact_uri,
        )
        session.add(
            UnmatchedWebhook(
                workspace_id=workspace.id,
                connector_type=connector.connector_type,
                sender_uri=incoming.contact_uri,
                reason="unparseable_sender",
                raw_payload=incoming.raw_payload or {},
            )
        )
        return ReplyResult(action="unmatched", detail="unparseable_sender")

    thread = _resolve_thread(session, workspace.id, normalised)
    if thread is None:
        logger.info("inbound unmatched normalised=%s — no active thread", normalised)
        session.add(
            UnmatchedWebhook(
                workspace_id=workspace.id,
                connector_type=connector.connector_type,
                sender_uri=normalised,
                reason="no_matching_thread",
                raw_payload=incoming.raw_payload or {},
            )
        )
        return ReplyResult(action="unmatched", detail="no_matching_thread")

    thread = _lock_thread(session, thread.id) or thread

    if thread.status == ThreadStatus.PAUSED_FOR_HITL:
        # Someone already parked this thread. Capture the message so it
        # shows up in the transcript but don't run any AI on it — the
        # human will see the new inbound and decide.
        logger.info(
            "inbound captured but paused thread=%s hitl_reason=%s",
            thread.id,
            thread.hitl_reason,
        )
        session.add(
            Message(
                thread_id=thread.id,
                role=MessageRole.LEAD,
                content=incoming.content,
                metadata_={"source": connector.connector_type, "paused_for_hitl": True},
            )
        )
        return ReplyResult(
            action="ignored",
            thread_id=thread.id,
            detail="thread_paused_for_hitl",
        )

    session.add(
        Message(
            thread_id=thread.id,
            role=MessageRole.LEAD,
            content=incoming.content,
            metadata_={
                "source": connector.connector_type,
                "provider_message_id": incoming.provider_message_id,
            },
        )
    )
    session.flush()

    campaign_lead = session.get(CampaignLead, thread.campaign_lead_id)
    campaign = session.get(Campaign, campaign_lead.campaign_id)
    lead = session.get(Lead, campaign_lead.lead_id)
    return thread, campaign_lead, campaign, lead


# ---------------------------------------------------------------------------
# Stage 2: classify
# ---------------------------------------------------------------------------


async def _classify_reply(
    *,
    workspace: Workspace,
    campaign: Campaign,
    lead: Lead,
    thread: Thread,
    history: list[dict[str, Any]],
    incoming: IncomingMessage,
    settings_llm: dict[str, Any],
) -> _Classification:
    """Run the classification LLM and return a normalised verdict.

    History is truncated to exclude the just-captured inbound because the
    classifier prompt expects the message under review to be supplied
    separately via ``build_user_prompt(incoming_message=...)``.
    """

    cls_raw, cls_result = await complete_json(
        system=classification.build_system_prompt(),
        user=classification.build_user_prompt(
            campaign_goal=campaign.goal,
            history=history[:-1],
            incoming_message=incoming.content,
        ),
        model=settings_llm.get("model_classification", settings_llm["model_main"]),
        prompt_version=classification.PROMPT_VERSION,
        temperature=float(settings_llm.get("temperature_eval", 0.0)),
        context=LlmCallContext(
            purpose=LlmCallPurpose.CLASSIFICATION,
            workspace_id=workspace.id,
            campaign_id=campaign.id,
            thread_id=thread.id,
            lead_id=lead.id,
        ),
    )
    cls = classification.normalise_classification(cls_raw)

    logger.info(
        "classification thread=%s intent=%s confidence=%.2f requires_human=%s reason=%r",
        thread.id,
        cls["intent"],
        cls["confidence"],
        cls["requires_human"],
        (cls["reason"] or "")[:160],
    )
    return _Classification(
        intent=cls["intent"],
        confidence=cls["confidence"],
        reason=cls["reason"],
        requires_human=bool(cls["requires_human"]),
        llm_call_id=cls_result.llm_call_id,
    )


# ---------------------------------------------------------------------------
# Stage 3a: terminal intents (close won/lost)
# ---------------------------------------------------------------------------


def _route_terminal_intent(
    *,
    thread: Thread,
    campaign_lead: CampaignLead,
    lead: Lead,
    classification: _Classification,
) -> ReplyResult | None:
    """Close the thread on ``negative`` / ``goal_achieved``. Returns None otherwise."""

    if classification.intent == "negative":
        _close_thread(thread, campaign_lead, lead, won=False)
        logger.info("reply route thread=%s action=closed_lost intent=negative", thread.id)
        return ReplyResult(
            action="closed_lost",
            thread_id=thread.id,
            intent=classification.intent,
            confidence=classification.confidence,
            detail=classification.reason,
        )

    if classification.intent == "goal_achieved":
        _close_thread(thread, campaign_lead, lead, won=True)
        logger.info("reply route thread=%s action=closed_won intent=goal_achieved", thread.id)
        return ReplyResult(
            action="closed_won",
            thread_id=thread.id,
            intent=classification.intent,
            confidence=classification.confidence,
            detail=classification.reason,
        )

    return None


# ---------------------------------------------------------------------------
# Stage 3b: first-message-only mode (default)
# ---------------------------------------------------------------------------


async def _park_with_suggestions(
    *,
    session: Session,
    workspace: Workspace,
    campaign: Campaign,
    lead: Lead,
    thread: Thread,
    classification: _Classification,
    incoming: IncomingMessage,
    settings_blob: dict[str, Any],
) -> ReplyResult:
    """Pause the thread for HITL review, stashing N drafted reply variants."""

    suggestions_n = int(settings_blob.get("suggestions_count", 3))
    try:
        suggestions = await generate_reply_variants(
            session=session,
            workspace=workspace,
            campaign=campaign,
            lead=lead,
            thread=thread,
            n=suggestions_n,
        )
    except Exception:
        # Never let a suggestion failure drop the inbound on the floor —
        # the human still needs to see the message and be able to reply
        # manually.
        logger.exception(
            "reply suggestions failed thread=%s — parking without drafts",
            thread.id,
        )
        suggestions = []

    pause_thread_for_hitl(
        thread,
        reason=HITL_AWAITING_HUMAN_REPLY,
        context={
            "intent": classification.intent,
            "confidence": classification.confidence,
            "reason": classification.reason,
            "incoming_message": incoming.content,
            "classification_llm_call_id": classification.llm_call_id,
            "suggestions": suggestions,
        },
    )
    session.flush()
    logger.info(
        "reply parked thread=%s reason=%s intent=%s suggestions=%d",
        thread.id,
        HITL_AWAITING_HUMAN_REPLY,
        classification.intent,
        len(suggestions),
    )
    return ReplyResult(
        action="escalated_hitl",
        thread_id=thread.id,
        intent=classification.intent,
        confidence=classification.confidence,
        detail=HITL_AWAITING_HUMAN_REPLY,
    )


# ---------------------------------------------------------------------------
# Stage 3c: legacy auto-reply mode (opt-in)
# ---------------------------------------------------------------------------


async def _run_auto_reply(
    *,
    session: Session,
    connector: BaseConnector,
    workspace: Workspace,
    campaign: Campaign,
    campaign_lead: CampaignLead,
    lead: Lead,
    thread: Thread,
    history: list[dict[str, Any]],
    classification: _Classification,
    incoming: IncomingMessage,
    settings_blob: dict[str, Any],
    settings_llm: dict[str, Any],
) -> ReplyResult:
    """Run the classify → generate → evaluate → send loop inline.

    Only used when ``auto_reply_enabled`` is true. Short-circuits to
    HITL on the first of: classifier wanted a human, auto-reply ceiling
    hit, evaluator never passed, or the connector send failed.
    """

    max_auto_replies = int(settings_blob.get("max_auto_replies", 5))
    if classification.requires_human or thread.auto_reply_count >= max_auto_replies:
        hitl_reason = _hitl_reason_for(
            intent=classification.intent,
            confidence=classification.confidence,
            auto_reply_count=thread.auto_reply_count,
            max_auto_replies=max_auto_replies,
        )
        pause_thread_for_hitl(
            thread,
            reason=hitl_reason,
            context={
                "intent": classification.intent,
                "confidence": classification.confidence,
                "reason": classification.reason,
                "incoming_message": incoming.content,
            },
        )
        session.flush()
        logger.warning(
            "reply escalated thread=%s reason=%s intent=%s confidence=%.2f",
            thread.id,
            hitl_reason,
            classification.intent,
            classification.confidence,
        )
        return ReplyResult(
            action="escalated_hitl",
            thread_id=thread.id,
            intent=classification.intent,
            confidence=classification.confidence,
            detail=hitl_reason,
        )

    _, eval_threshold, eval_max_attempts = read_loop_settings(workspace)

    loop_result = await generate_and_evaluate(
        settings_llm=settings_llm,
        eval_threshold=eval_threshold,
        eval_max_attempts=eval_max_attempts,
        workspace=workspace,
        campaign=campaign,
        lead=lead,
        thread=thread,
        angle=thread.angle or "",
        message_history=history,
    )

    if loop_result["status"] != "pass":
        pause_thread_for_hitl(
            thread,
            reason="reply_eval_failed",
            context=hitl_context_from_loop_failure(
                loop_result,
                intent=classification.intent,
                confidence=classification.confidence,
                incoming_message=incoming.content,
            ),
        )
        session.flush()
        logger.warning(
            "reply escalated thread=%s reason=reply_eval_failed attempts=%d",
            thread.id,
            loop_result["attempts"],
        )
        return ReplyResult(
            action="escalated_hitl",
            thread_id=thread.id,
            intent=classification.intent,
            confidence=classification.confidence,
            detail="reply_eval_failed",
        )

    draft = loop_result["draft"]

    send_result = await connector.send(
        OutgoingMessage(contact_uri=lead.contact_uri, content=draft)
    )
    if not send_result.success:
        pause_thread_for_hitl(
            thread,
            reason="connector_send_failed",
            context=hitl_context_from_send_failure(
                draft=draft,
                send_result=send_result,
                loop_result=loop_result,
                intent=classification.intent,
                confidence=classification.confidence,
                incoming_message=incoming.content,
            ),
        )
        session.flush()
        logger.error(
            "reply send failed thread=%s error=%s", thread.id, send_result.error
        )
        return ReplyResult(
            action="escalated_hitl",
            thread_id=thread.id,
            intent=classification.intent,
            confidence=classification.confidence,
            detail="connector_send_failed",
        )

    session.add(
        Message(
            thread_id=thread.id,
            role=MessageRole.AI,
            content=draft,
            metadata_=build_send_metadata(
                loop_result=loop_result,
                settings_llm=settings_llm,
                send_result=send_result,
                intent=classification.intent,
                confidence=classification.confidence,
                classification_model=settings_llm.get(
                    "model_classification", settings_llm["model_main"]
                ),
                classification_llm_call_id=classification.llm_call_id,
            ),
        )
    )
    thread.auto_reply_count += 1
    thread.status = ThreadStatus.ACTIVE
    campaign_lead.status = CampaignLeadStatus.REPLIED
    lead.status = LeadStatus.REPLIED

    session.flush()
    logger.info(
        "reply sent thread=%s intent=%s confidence=%.2f score=%.3f chars=%d",
        thread.id,
        classification.intent,
        classification.confidence,
        loop_result["overall"],
        len(draft),
    )
    return ReplyResult(
        action="sent",
        thread_id=thread.id,
        intent=classification.intent,
        confidence=classification.confidence,
    )


def _hitl_reason_for(
    *, intent: str, confidence: float, auto_reply_count: int, max_auto_replies: int
) -> str:
    if intent == "bot_check":
        return "bot_check"
    if intent == "human_requested":
        return "human_requested"
    if intent == "unclear":
        return "unclear"
    if confidence < 0.80:
        return "low_confidence"
    if auto_reply_count >= max_auto_replies:
        return "max_auto_replies_reached"
    return "escalated"


def _close_thread(
    thread: Thread, campaign_lead: CampaignLead, lead: Lead, *, won: bool
) -> None:
    """Apply status propagation for terminal intents."""

    if won:
        thread.status = ThreadStatus.WON
        campaign_lead.status = CampaignLeadStatus.WON
        lead.status = LeadStatus.WON
    else:
        thread.status = ThreadStatus.LOST
        campaign_lead.status = CampaignLeadStatus.LOST
        lead.status = LeadStatus.LOST


__all__ = [
    "HITL_AWAITING_HUMAN_REPLY",
    "ReplyResult",
    "process_incoming_message",
]
