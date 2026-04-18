"""Reply pipeline: inbound webhook -> classify -> route.

Multi-campaign resolution (Doc 2 §4, Doc 3 §5.1):

- Resolve lead by (workspace_id, contact_uri) after E.164 normalisation.
- If the lead has multiple active threads, route to the thread whose most-recent
  outbound message is the most recent. Ties broken by thread.id.
- If no active thread exists, write to ``unmatched_webhook``.

Concurrency (Doc 2 §4, Doc 3 §5):

- The reply processor acquires a per-thread lock before classifying. SQLite gets
  this via ``BEGIN IMMEDIATE`` (serialises writers); Postgres gets it via
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
from autosdr.pipeline.outreach import (
    _generate_and_evaluate_for_reply,
    _thread_history_for_reply,
)
from autosdr.prompts import classification

logger = logging.getLogger(__name__)


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
    """Find the target thread per Doc 3 §5.1 routing rule."""

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


async def process_incoming_message(
    *,
    connector: BaseConnector,
    workspace_id: str,
    incoming: IncomingMessage,
) -> ReplyResult:
    """Main entry point — called from the webhook background task or poller."""

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

        region_hint = (workspace.settings or {}).get("default_region", "AU")
        normalised = _resolve_contact(incoming.contact_uri, region_hint)
        if normalised is None:
            logger.warning(
                "inbound unparseable sender=%r — dropping to unmatched_webhook",
                incoming.contact_uri,
            )
            session.add(
                UnmatchedWebhook(
                    workspace_id=workspace_id,
                    connector_type=connector.connector_type,
                    sender_uri=incoming.contact_uri,
                    reason="unparseable_sender",
                    raw_payload=incoming.raw_payload or {},
                )
            )
            return ReplyResult(action="unmatched", detail="unparseable_sender")

        thread = _resolve_thread(session, workspace_id, normalised)
        if thread is None:
            logger.info(
                "inbound unmatched normalised=%s — no active thread",
                normalised,
            )
            session.add(
                UnmatchedWebhook(
                    workspace_id=workspace_id,
                    connector_type=connector.connector_type,
                    sender_uri=normalised,
                    reason="no_matching_thread",
                    raw_payload=incoming.raw_payload or {},
                )
            )
            return ReplyResult(action="unmatched", detail="no_matching_thread")

        thread = _lock_thread(session, thread.id) or thread

        if thread.status == ThreadStatus.PAUSED_FOR_HITL:
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

        history = _thread_history_for_reply(session, thread)

        campaign_lead = session.get(CampaignLead, thread.campaign_lead_id)
        campaign = session.get(Campaign, campaign_lead.campaign_id)
        lead = session.get(Lead, campaign_lead.lead_id)

        settings_blob = workspace.settings or {}
        settings_llm = settings_blob.get("llm") or {}
        max_auto_replies = int(settings_blob.get("max_auto_replies", 5))

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
        intent = cls["intent"]
        confidence = cls["confidence"]
        reason = cls["reason"]

        logger.info(
            "classification thread=%s intent=%s confidence=%.2f requires_human=%s reason=%r",
            thread.id,
            intent,
            confidence,
            cls["requires_human"],
            (reason or "")[:160],
        )

        if intent == "negative":
            _close_thread(thread, campaign_lead, lead, won=False)
            logger.info(
                "reply route thread=%s action=closed_lost intent=negative", thread.id
            )
            return ReplyResult(
                action="closed_lost",
                thread_id=thread.id,
                intent=intent,
                confidence=confidence,
                detail=reason,
            )

        if intent == "goal_achieved":
            _close_thread(thread, campaign_lead, lead, won=True)
            logger.info(
                "reply route thread=%s action=closed_won intent=goal_achieved",
                thread.id,
            )
            return ReplyResult(
                action="closed_won",
                thread_id=thread.id,
                intent=intent,
                confidence=confidence,
                detail=reason,
            )

        if (
            cls["requires_human"]
            or thread.auto_reply_count >= max_auto_replies
        ):
            hitl_reason = _hitl_reason_for(
                intent=intent,
                confidence=confidence,
                auto_reply_count=thread.auto_reply_count,
                max_auto_replies=max_auto_replies,
            )
            thread.status = ThreadStatus.PAUSED_FOR_HITL
            thread.hitl_reason = hitl_reason
            thread.hitl_context = {
                "intent": intent,
                "confidence": confidence,
                "reason": reason,
                "incoming_message": incoming.content,
            }
            session.flush()
            logger.warning(
                "reply escalated thread=%s reason=%s intent=%s confidence=%.2f",
                thread.id,
                hitl_reason,
                intent,
                confidence,
            )
            return ReplyResult(
                action="escalated_hitl",
                thread_id=thread.id,
                intent=intent,
                confidence=confidence,
                detail=hitl_reason,
            )

        eval_threshold = float(settings_blob.get("eval_threshold", 0.85))
        eval_max_attempts = int(settings_blob.get("eval_max_attempts", 3))

        loop_result = await _generate_and_evaluate_for_reply(
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
            thread.status = ThreadStatus.PAUSED_FOR_HITL
            thread.hitl_reason = "reply_eval_failed"
            thread.hitl_context = {
                "intent": intent,
                "confidence": confidence,
                "incoming_message": incoming.content,
                "last_drafts": [a["draft"] for a in loop_result["drafts"]],
                "last_scores": [
                    {
                        "overall": a["overall"],
                        "breakdown": a["scores"],
                        "feedback": a["feedback"],
                    }
                    for a in loop_result["drafts"]
                ],
                "attempts": loop_result["attempts"],
            }
            session.flush()
            logger.warning(
                "reply escalated thread=%s reason=reply_eval_failed attempts=%d",
                thread.id,
                loop_result["attempts"],
            )
            return ReplyResult(
                action="escalated_hitl",
                thread_id=thread.id,
                intent=intent,
                confidence=confidence,
                detail="reply_eval_failed",
            )

        draft = loop_result["draft"]

        send_result = await connector.send(
            OutgoingMessage(contact_uri=lead.contact_uri, content=draft)
        )
        if not send_result.success:
            thread.status = ThreadStatus.PAUSED_FOR_HITL
            thread.hitl_reason = "connector_send_failed"
            thread.hitl_context = {
                "intent": intent,
                "confidence": confidence,
                "incoming_message": incoming.content,
                "last_drafts": [draft],
                "connector_error": send_result.error,
            }
            session.flush()
            logger.error(
                "reply send failed thread=%s error=%s", thread.id, send_result.error
            )
            return ReplyResult(
                action="escalated_hitl",
                thread_id=thread.id,
                intent=intent,
                confidence=confidence,
                detail="connector_send_failed",
            )

        session.add(
            Message(
                thread_id=thread.id,
                role=MessageRole.AI,
                content=draft,
                metadata_={
                    "intent": intent,
                    "confidence": confidence,
                    "eval_score": loop_result["overall"],
                    "eval_attempts": loop_result["attempts"],
                    "eval_scores_breakdown": loop_result["scores"],
                    "model": settings_llm.get("model_main"),
                    "eval_model": settings_llm.get("model_eval"),
                    "classification_model": settings_llm.get(
                        "model_classification", settings_llm["model_main"]
                    ),
                    "classification_llm_call_id": cls_result.llm_call_id,
                    "gen_llm_call_ids": [
                        a.get("gen_llm_call_id") for a in loop_result["drafts"]
                    ],
                    "eval_llm_call_ids": [
                        a.get("eval_llm_call_id") for a in loop_result["drafts"]
                    ],
                    "provider_message_id": send_result.provider_message_id,
                },
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
            intent,
            confidence,
            loop_result["overall"],
            len(draft),
        )
        return ReplyResult(
            action="sent",
            thread_id=thread.id,
            intent=intent,
            confidence=confidence,
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
