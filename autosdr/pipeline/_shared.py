"""Primitives shared by the outreach and reply pipelines.

Both pipelines need (a) the rolling thread history, (b) the
generate-then-evaluate loop, and (c) a small set of bookkeeping helpers
for HITL escalation and outbound-message metadata. Keeping them in one
module prevents ``reply.py`` from reaching into ``outreach.py``'s private
internals (the previous approach aliased
``_generate_and_evaluate_for_reply = _generate_and_evaluate`` which
obscured the dependency).
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy.orm import Session

from autosdr.connectors.base import SendResult
from autosdr.llm import LlmCallContext, complete_json, complete_text
from autosdr.models import (
    Campaign,
    Lead,
    LlmCallPurpose,
    Message,
    Thread,
    ThreadStatus,
    Workspace,
)
from autosdr.prompts import evaluation, generation

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------


def read_loop_settings(workspace: Workspace) -> tuple[dict[str, Any], float, int]:
    """Read ``(settings_llm, eval_threshold, eval_max_attempts)`` from a workspace.

    Pulled out of the pipelines because outreach, reply, and suggestions
    all read the same blob with the same defaults — extracting it keeps
    them in sync when defaults change.
    """

    blob = workspace.settings or {}
    settings_llm = blob.get("llm") or {}
    eval_threshold = float(blob.get("eval_threshold", 0.85))
    eval_max_attempts = int(blob.get("eval_max_attempts", 3))
    return settings_llm, eval_threshold, eval_max_attempts


# ---------------------------------------------------------------------------
# HITL bookkeeping
# ---------------------------------------------------------------------------


def pause_thread_for_hitl(
    thread: Thread, *, reason: str, context: dict[str, Any]
) -> None:
    """Flip ``thread`` into HITL state with the given reason and context.

    Persistence is the caller's responsibility (``session.flush()`` or a
    commit at the end of the session scope) — hiding it here would make
    the write boundary harder to spot at call sites.
    """

    thread.status = ThreadStatus.PAUSED_FOR_HITL
    thread.hitl_reason = reason
    thread.hitl_context = context


def hitl_context_from_loop_failure(
    loop_result: dict[str, Any], **extra: Any
) -> dict[str, Any]:
    """Build the ``hitl_context`` payload for an eval-exhausted loop.

    ``extra`` is merged in alongside the common ``last_drafts`` /
    ``last_scores`` / ``attempts`` keys so callers can stash pipeline-
    specific context (incoming message, intent, confidence, etc.).
    """

    return {
        **extra,
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


def hitl_context_from_send_failure(
    *,
    draft: str,
    send_result: SendResult,
    loop_result: dict[str, Any],
    **extra: Any,
) -> dict[str, Any]:
    """Build the ``hitl_context`` payload when the connector rejected the send."""

    return {
        **extra,
        "last_drafts": [draft],
        "connector_error": send_result.error,
        "attempts": loop_result["attempts"],
    }


# ---------------------------------------------------------------------------
# Outbound metadata
# ---------------------------------------------------------------------------


def build_send_metadata(
    *,
    loop_result: dict[str, Any],
    settings_llm: dict[str, Any],
    send_result: SendResult,
    **extra: Any,
) -> dict[str, Any]:
    """Build the ``Message.metadata_`` blob for a successfully-sent AI draft.

    The common shape captures the loop verdict, model identity, prompt
    versions, the connector's provider id, and the per-attempt LLM call
    ids. Pipeline-specific extras (angle, intent, classification id) are
    merged in via ``extra``.
    """

    return {
        "eval_score": loop_result["overall"],
        "eval_attempts": loop_result["attempts"],
        "eval_scores_breakdown": loop_result["scores"],
        "model": settings_llm.get("model_main"),
        "eval_model": settings_llm.get("model_eval"),
        "prompt_version": generation.PROMPT_VERSION,
        "eval_prompt_version": evaluation.PROMPT_VERSION,
        "provider_message_id": send_result.provider_message_id,
        "gen_llm_call_ids": [a.get("gen_llm_call_id") for a in loop_result["drafts"]],
        "eval_llm_call_ids": [a.get("eval_llm_call_id") for a in loop_result["drafts"]],
        **extra,
    }


def thread_history(session: Session, thread: Thread, *, limit: int = 10) -> list[dict[str, str]]:
    """Return the most recent ``limit`` messages on ``thread``, oldest-first."""

    rows = (
        session.query(Message)
        .filter(Message.thread_id == thread.id)
        .order_by(Message.created_at.desc())
        .limit(limit)
        .all()
    )
    return [{"role": m.role, "content": m.content} for m in reversed(rows)]


async def generate_and_evaluate(
    *,
    settings_llm: dict[str, Any],
    eval_threshold: float,
    eval_max_attempts: int,
    workspace: Workspace,
    campaign: Campaign,
    lead: Lead,
    thread: Thread,
    angle: str,
    lead_short_name: str | None = None,
    message_history: list[dict[str, str]] | None = None,
    temperature_override: float | None = None,
) -> dict[str, Any]:
    """Generate-then-evaluate loop.

    Runs up to ``eval_max_attempts`` generate / evaluate cycles, incorporating
    evaluator feedback between attempts. Returns a dict describing whether a
    draft passed the threshold, plus the full per-attempt trace so the caller
    can persist it to ``thread.hitl_context`` on failure.
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

    base_temperature = float(settings_llm.get("temperature_main", 1.0))
    temperature = temperature_override if temperature_override is not None else base_temperature

    attempts: list[dict[str, Any]] = []
    feedback: str | None = None

    for attempt_num in range(1, eval_max_attempts + 1):
        gen_user = generation.build_user_prompt(
            business_data=workspace.business_data or {},
            business_dump=workspace.business_dump,
            campaign_goal=campaign.goal,
            angle=angle,
            lead_name=lead.name,
            lead_short_name=lead_short_name,
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
            temperature=temperature,
            context=gen_ctx,
        )
        draft = gen_result.text.strip()

        eval_user = evaluation.build_user_prompt(
            tone_snapshot=thread.tone_snapshot,
            campaign_goal=campaign.goal,
            angle=angle,
            draft=draft,
            lead_category=lead.category,
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
        "scores": attempts[-1]["scores"] if attempts else None,
        "last_feedback": feedback,
    }


__all__ = [
    "build_send_metadata",
    "generate_and_evaluate",
    "hitl_context_from_loop_failure",
    "hitl_context_from_send_failure",
    "pause_thread_for_hitl",
    "read_loop_settings",
    "thread_history",
]
