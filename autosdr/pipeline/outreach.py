"""Outreach pipeline: analyse -> generate -> evaluate -> send."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from sqlalchemy.orm import Session

from autosdr.connectors.base import BaseConnector, OutgoingMessage
from autosdr.llm import LlmCallContext, complete_json, complete_text
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
    Workspace,
)
from autosdr.prompts import analysis, evaluation, generation

logger = logging.getLogger(__name__)


@dataclass
class OutreachResult:
    sent: bool
    reason: str
    thread_id: str | None = None
    message_id: str | None = None
    attempts: int = 0
    overall_score: float | None = None


def _ensure_thread(
    session: Session, campaign_lead: CampaignLead, workspace: Workspace, campaign: Campaign
) -> tuple[Thread, bool]:
    """Return the thread for this campaign_lead; create if missing."""

    thread = (
        session.query(Thread)
        .filter(Thread.campaign_lead_id == campaign_lead.id)
        .one_or_none()
    )
    if thread is None:
        thread = Thread(
            campaign_lead_id=campaign_lead.id,
            connector_type=campaign.connector_type,
            status=ThreadStatus.ACTIVE,
            tone_snapshot=workspace.tone_prompt,
        )
        session.add(thread)
        session.flush()
        return thread, True
    return thread, False


async def _run_analysis(
    *,
    settings_llm: dict[str, Any],
    workspace: Workspace,
    campaign: Campaign,
    lead: Lead,
    thread: Thread,
    raw_data_size_limit_kb: int,
) -> tuple[dict[str, Any], bool]:
    """Run the analysis agent for a lead. Returns (result_dict, raw_data_truncated)."""

    user_prompt, truncated = analysis.build_user_prompt(
        business_data=workspace.business_data or {},
        business_dump=workspace.business_dump,
        campaign_goal=campaign.goal,
        lead_name=lead.name,
        lead_category=lead.category,
        lead_address=lead.address,
        raw_data=lead.raw_data or {},
        raw_data_size_limit_kb=raw_data_size_limit_kb,
    )
    model = settings_llm.get("model_analysis", settings_llm["model_main"])
    temperature = float(settings_llm.get("temperature_main", 0.7))
    parsed, result = await complete_json(
        system=analysis.SYSTEM_PROMPT,
        user=user_prompt,
        model=model,
        prompt_version=analysis.PROMPT_VERSION,
        temperature=temperature,
        context=LlmCallContext(
            purpose=LlmCallPurpose.ANALYSIS,
            workspace_id=workspace.id,
            campaign_id=campaign.id,
            thread_id=thread.id,
            lead_id=lead.id,
        ),
    )
    parsed.setdefault("angle", "")
    parsed.setdefault("signal", "")
    parsed.setdefault("confidence", 0.0)
    parsed["_meta"] = {
        "model": result.model,
        "tokens_in": result.tokens_in,
        "tokens_out": result.tokens_out,
        "prompt_version": result.prompt_version,
        "raw_data_truncated": truncated,
        "llm_call_id": result.llm_call_id,
    }
    return parsed, truncated


async def _generate_and_evaluate(
    *,
    settings_llm: dict[str, Any],
    eval_threshold: float,
    eval_max_attempts: int,
    workspace: Workspace,
    campaign: Campaign,
    lead: Lead,
    thread: Thread,
    angle: str,
    message_history: list[dict[str, str]] | None,
) -> dict[str, Any]:
    """Run the generate/evaluate loop up to ``eval_max_attempts`` times.

    Returns a dict with: ``status`` (pass|fail), ``draft`` (final draft if pass),
    ``attempts`` (int), ``drafts`` (list of dicts per attempt), ``last_feedback``.
    """

    system_gen = generation.build_system_prompt(thread.tone_snapshot)
    system_eval = evaluation.build_system_prompt()

    gen_ctx = LlmCallContext(
        purpose=LlmCallPurpose.GENERATION,
        workspace_id=workspace.id,
        campaign_id=campaign.id,
        thread_id=thread.id,
        lead_id=lead.id,
    )
    eval_ctx = LlmCallContext(
        purpose=LlmCallPurpose.EVALUATION,
        workspace_id=workspace.id,
        campaign_id=campaign.id,
        thread_id=thread.id,
        lead_id=lead.id,
    )

    attempts: list[dict[str, Any]] = []
    feedback: str | None = None

    for attempt_num in range(1, eval_max_attempts + 1):
        gen_user = generation.build_user_prompt(
            business_data=workspace.business_data or {},
            business_dump=workspace.business_dump,
            campaign_goal=campaign.goal,
            angle=angle,
            lead_name=lead.name,
            lead_category=lead.category,
            lead_address=lead.address,
            previous_feedback=feedback,
            message_history=message_history,
        )
        logger.info(
            "generation attempt=%d thread=%s model=%s",
            attempt_num,
            thread.id,
            settings_llm["model_main"],
        )
        gen_result = await complete_text(
            system=system_gen,
            user=gen_user,
            model=settings_llm["model_main"],
            prompt_version=generation.PROMPT_VERSION,
            temperature=float(settings_llm.get("temperature_main", 0.7)),
            context=gen_ctx,
        )
        draft = gen_result.text.strip()

        eval_user = evaluation.build_user_prompt(
            tone_snapshot=thread.tone_snapshot,
            campaign_goal=campaign.goal,
            angle=angle,
            draft=draft,
        )
        eval_raw, eval_result = await complete_json(
            system=system_eval,
            user=eval_user,
            model=settings_llm["model_eval"],
            prompt_version=evaluation.PROMPT_VERSION,
            temperature=float(settings_llm.get("temperature_eval", 0.0)),
            context=eval_ctx,
        )
        normalised = evaluation.evaluate_result(
            eval_raw, draft=draft, threshold=eval_threshold
        )

        attempts.append(
            {
                "attempt": attempt_num,
                "draft": draft,
                "scores": normalised["scores"],
                "overall": normalised["overall"],
                "pass": normalised["pass"],
                "feedback": normalised["feedback"],
                "gen_tokens_in": gen_result.tokens_in,
                "gen_tokens_out": gen_result.tokens_out,
                "gen_llm_call_id": gen_result.llm_call_id,
                "eval_tokens_in": eval_result.tokens_in,
                "eval_tokens_out": eval_result.tokens_out,
                "eval_llm_call_id": eval_result.llm_call_id,
            }
        )

        logger.info(
            "evaluation attempt=%d thread=%s overall=%.3f pass=%s length=%d",
            attempt_num,
            thread.id,
            normalised["overall"],
            normalised["pass"],
            len(draft),
        )

        if normalised["pass"]:
            return {
                "status": "pass",
                "draft": draft,
                "attempts": attempt_num,
                "drafts": attempts,
                "overall": normalised["overall"],
                "scores": normalised["scores"],
                "last_feedback": normalised["feedback"],
            }

        feedback = normalised["feedback"]
        logger.info(
            "evaluation rejected attempt=%d thread=%s feedback=%r",
            attempt_num,
            thread.id,
            (feedback or "")[:200],
        )

    return {
        "status": "fail",
        "draft": None,
        "attempts": eval_max_attempts,
        "drafts": attempts,
        "overall": attempts[-1]["overall"] if attempts else 0.0,
        "last_feedback": feedback,
    }


def _thread_history(session: Session, thread: Thread, *, limit: int = 10) -> list[dict[str, str]]:
    """Load the most recent N messages for a thread, oldest-first."""

    rows = (
        session.query(Message)
        .filter(Message.thread_id == thread.id)
        .order_by(Message.created_at.desc())
        .limit(limit)
        .all()
    )
    return [{"role": m.role, "content": m.content} for m in reversed(rows)]


async def run_outreach_for_campaign_lead(
    *,
    session: Session,
    connector: BaseConnector,
    workspace: Workspace,
    campaign: Campaign,
    campaign_lead: CampaignLead,
    lead: Lead,
) -> OutreachResult:
    """Execute the outreach pipeline for a single campaign-lead assignment."""

    settings_blob = workspace.settings or {}
    settings_llm = settings_blob.get("llm") or {}
    eval_threshold = float(settings_blob.get("eval_threshold", 0.85))
    eval_max_attempts = int(settings_blob.get("eval_max_attempts", 3))
    raw_data_size_limit_kb = int(settings_blob.get("raw_data_size_limit_kb", 50))

    if campaign_lead.status != CampaignLeadStatus.QUEUED:
        return OutreachResult(sent=False, reason=f"campaign_lead_not_queued:{campaign_lead.status}")

    thread, created = _ensure_thread(session, campaign_lead, workspace, campaign)

    if thread.status != ThreadStatus.ACTIVE:
        return OutreachResult(sent=False, reason=f"thread_not_active:{thread.status}", thread_id=thread.id)

    logger.info(
        "outreach start lead=%s name=%r thread=%s campaign=%s",
        lead.id,
        (lead.name or "")[:60],
        thread.id,
        campaign.id,
    )

    # 1. Analysis (first-contact only; replies re-use thread.angle)
    message_history = _thread_history(session, thread)
    analysis_meta: dict[str, Any] = {}
    if created or not thread.angle:
        analysis_result, truncated = await _run_analysis(
            settings_llm=settings_llm,
            workspace=workspace,
            campaign=campaign,
            lead=lead,
            thread=thread,
            raw_data_size_limit_kb=raw_data_size_limit_kb,
        )
        angle = str(analysis_result.get("angle") or "").strip()
        if not angle:
            angle = f"{lead.category or 'business'} in {lead.address or 'the area'}"
        thread.angle = angle
        analysis_meta = {
            "model": analysis_result["_meta"]["model"],
            "tokens_in": analysis_result["_meta"]["tokens_in"],
            "tokens_out": analysis_result["_meta"]["tokens_out"],
            "prompt_version": analysis_result["_meta"]["prompt_version"],
            "signal": analysis_result.get("signal"),
            "confidence": analysis_result.get("confidence"),
            "raw_data_truncated": truncated,
            "llm_call_id": analysis_result["_meta"].get("llm_call_id"),
        }
        logger.info(
            "analysis thread=%s angle=%r confidence=%s signal=%r truncated=%s",
            thread.id,
            angle[:120],
            analysis_result.get("confidence"),
            (analysis_result.get("signal") or "")[:120],
            truncated,
        )
    else:
        angle = thread.angle

    # 2+3. Generate and evaluate
    loop_result = await _generate_and_evaluate(
        settings_llm=settings_llm,
        eval_threshold=eval_threshold,
        eval_max_attempts=eval_max_attempts,
        workspace=workspace,
        campaign=campaign,
        lead=lead,
        thread=thread,
        angle=angle,
        message_history=message_history if not created else None,
    )

    if loop_result["status"] != "pass":
        thread.status = ThreadStatus.PAUSED_FOR_HITL
        thread.hitl_reason = "eval_failed_after_3_attempts"
        thread.hitl_context = {
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
            "outreach escalated lead=%s thread=%s reason=eval_failed attempts=%d last_overall=%.3f",
            lead.id,
            thread.id,
            loop_result["attempts"],
            loop_result["overall"],
        )
        return OutreachResult(
            sent=False,
            reason="eval_failed",
            thread_id=thread.id,
            attempts=loop_result["attempts"],
            overall_score=loop_result["overall"],
        )

    # 4. Send
    draft: str = loop_result["draft"]
    logger.info(
        "outreach sending lead=%s thread=%s connector=%s chars=%d",
        lead.id,
        thread.id,
        connector.connector_type,
        len(draft),
    )
    send_result = await connector.send(
        OutgoingMessage(contact_uri=lead.contact_uri, content=draft)
    )

    if not send_result.success:
        thread.status = ThreadStatus.PAUSED_FOR_HITL
        thread.hitl_reason = "connector_send_failed"
        thread.hitl_context = {
            "last_drafts": [draft],
            "connector_error": send_result.error,
            "attempts": loop_result["attempts"],
        }
        session.flush()
        logger.error(
            "outreach connector failed lead=%s thread=%s error=%s",
            lead.id,
            thread.id,
            send_result.error,
        )
        return OutreachResult(
            sent=False,
            reason=f"connector_failed:{send_result.error}",
            thread_id=thread.id,
            attempts=loop_result["attempts"],
            overall_score=loop_result["overall"],
        )

    # 5. Append message + propagate statuses
    metadata = {
        "eval_score": loop_result["overall"],
        "eval_attempts": loop_result["attempts"],
        "eval_scores_breakdown": loop_result["scores"],
        "angle_used": angle,
        "model": settings_llm.get("model_main"),
        "eval_model": settings_llm.get("model_eval"),
        "prompt_version": generation.PROMPT_VERSION,
        "eval_prompt_version": evaluation.PROMPT_VERSION,
        "provider_message_id": send_result.provider_message_id,
        "gen_llm_call_ids": [a.get("gen_llm_call_id") for a in loop_result["drafts"]],
        "eval_llm_call_ids": [a.get("eval_llm_call_id") for a in loop_result["drafts"]],
    }
    if analysis_meta:
        metadata["analysis"] = analysis_meta

    msg = Message(
        thread_id=thread.id,
        role=MessageRole.AI,
        content=draft,
        metadata_=metadata,
    )
    session.add(msg)

    campaign_lead.status = CampaignLeadStatus.CONTACTED
    if lead.status == LeadStatus.NEW:
        lead.status = LeadStatus.CONTACTED

    session.flush()
    logger.info(
        "outreach sent lead=%s thread=%s score=%.3f attempts=%d chars=%d provider_id=%s",
        lead.id,
        thread.id,
        loop_result["overall"],
        loop_result["attempts"],
        len(draft),
        send_result.provider_message_id,
    )
    return OutreachResult(
        sent=True,
        reason="sent",
        thread_id=thread.id,
        message_id=msg.id,
        attempts=loop_result["attempts"],
        overall_score=loop_result["overall"],
    )


_generate_and_evaluate_for_reply = _generate_and_evaluate
_thread_history_for_reply = _thread_history


__all__ = [
    "OutreachResult",
    "run_outreach_for_campaign_lead",
    "_generate_and_evaluate_for_reply",
    "_thread_history_for_reply",
]
