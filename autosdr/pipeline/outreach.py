"""First-contact outreach pipeline: analyse -> generate -> evaluate -> send."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from sqlalchemy.orm import Session

from autosdr.connectors.base import BaseConnector, OutgoingMessage
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
from autosdr.prompts import analysis

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
    """Return the thread for this campaign_lead; create if missing.

    We always commit before returning so the thread id is durable for the
    LLM call log and the outer scheduler loop can release SQLite's writer
    lock before we begin issuing LLM calls.
    """

    thread = (
        session.query(Thread)
        .filter(Thread.campaign_lead_id == campaign_lead.id)
        .one_or_none()
    )
    created = thread is None
    if created:
        thread = Thread(
            campaign_lead_id=campaign_lead.id,
            connector_type=campaign.connector_type,
            status=ThreadStatus.ACTIVE,
            tone_snapshot=workspace.tone_prompt,
        )
        session.add(thread)
        session.flush()

    session.commit()
    return thread, created


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
    temperature = float(settings_llm.get("temperature_main", 1.0))
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
    parsed.setdefault("owner_first_name", "")
    parsed.setdefault("owner_evidence", "")
    parsed.setdefault("confidence", 0.0)
    parsed.setdefault("lead_short_name", "")
    validated_name, validated_evidence = analysis.validate_owner_first_name(
        owner_first_name=parsed.get("owner_first_name"),
        owner_evidence=parsed.get("owner_evidence"),
        lead_name=lead.name,
    )
    parsed["owner_first_name"] = validated_name
    parsed["owner_evidence"] = validated_evidence
    parsed["_meta"] = {
        "model": result.model,
        "tokens_in": result.tokens_in,
        "tokens_out": result.tokens_out,
        "prompt_version": result.prompt_version,
        "raw_data_truncated": truncated,
        "llm_call_id": result.llm_call_id,
    }
    return parsed, truncated


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
    settings_llm, eval_threshold, eval_max_attempts = read_loop_settings(workspace)
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

    message_history = thread_history(session, thread)
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
        owner_first_name = str(analysis_result.get("owner_first_name") or "").strip()
        if owner_first_name:
            angle = f"Recipient owner's first name: {owner_first_name}\n\n{angle}"
        lead_short_name = str(analysis_result.get("lead_short_name") or "").strip() or None
        thread.angle = angle
        analysis_meta = {
            "model": analysis_result["_meta"]["model"],
            "tokens_in": analysis_result["_meta"]["tokens_in"],
            "tokens_out": analysis_result["_meta"]["tokens_out"],
            "prompt_version": analysis_result["_meta"]["prompt_version"],
            "signal": analysis_result.get("signal"),
            "owner_first_name": owner_first_name or None,
            "lead_short_name": lead_short_name,
            "confidence": analysis_result.get("confidence"),
            "raw_data_truncated": truncated,
            "llm_call_id": analysis_result["_meta"].get("llm_call_id"),
        }
        logger.info(
            "analysis thread=%s angle=%r owner=%r short_name=%r confidence=%s signal=%r truncated=%s",
            thread.id,
            angle[:120],
            owner_first_name or None,
            lead_short_name,
            analysis_result.get("confidence"),
            (analysis_result.get("signal") or "")[:120],
            truncated,
        )
    else:
        angle = thread.angle
        lead_short_name = None

    loop_result = await generate_and_evaluate(
        settings_llm=settings_llm,
        eval_threshold=eval_threshold,
        eval_max_attempts=eval_max_attempts,
        workspace=workspace,
        campaign=campaign,
        lead=lead,
        thread=thread,
        angle=angle,
        lead_short_name=lead_short_name,
        message_history=message_history if not created else None,
    )

    if loop_result["status"] != "pass":
        pause_thread_for_hitl(
            thread,
            reason="eval_failed_after_max_attempts",
            context=hitl_context_from_loop_failure(loop_result),
        )
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
        pause_thread_for_hitl(
            thread,
            reason="connector_send_failed",
            context=hitl_context_from_send_failure(
                draft=draft,
                send_result=send_result,
                loop_result=loop_result,
            ),
        )
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

    metadata = build_send_metadata(
        loop_result=loop_result,
        settings_llm=settings_llm,
        send_result=send_result,
        angle_used=angle,
    )
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


__all__ = ["OutreachResult", "run_outreach_for_campaign_lead"]
