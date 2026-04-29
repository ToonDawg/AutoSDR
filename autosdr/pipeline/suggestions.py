"""Generate multiple candidate reply drafts for the human-in-the-loop UI.

Used when ``auto_reply_enabled`` is false (the default). After the lead
replies we don't auto-send — instead we generate N draft variants and
stash them on ``thread.hitl_context.suggestions`` so the operator can
pick one in the UI with a single click.

Two flows live here:

- :func:`_generate_outreach_style_variants` — the original generate-then-
  evaluate loop. Used only when the thread has *no* lead messages yet
  (e.g. operator clicked "regenerate suggestions" before the first
  inbound). The cold-outreach prompt + evaluator are appropriate here:
  the message we're drafting is structurally still a first-touch.

- :func:`_generate_followup_variants` — single-call-per-variant flow that
  uses the dedicated follow-up prompt (``autosdr.prompts.followup_reply``).
  No evaluator. Used whenever the thread already has at least one
  ``MessageRole.LEAD`` message, which is the case on every inbound-
  triggered park. This is the path that fires for the chat the operator
  actually sees in the HITL UI.

The split exists because the cold-outreach generator hard-codes a
mandatory credential line ("I build websites for a living") and treats
the recipient as someone who has never heard of the sender, which makes
the suggestions read as re-pitches once the lead has replied. The
follow-up prompt is a separate agent for a separate job.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from autosdr.llm import LlmCallContext, complete_text
from autosdr.models import Campaign, Lead, LlmCallPurpose, MessageRole, Thread, Workspace
from autosdr.pipeline._shared import (
    generate_and_evaluate,
    read_loop_settings,
)
from autosdr.prompts import followup_reply

logger = logging.getLogger(__name__)


# Temperature spread used when we need multiple distinct drafts. Duplicate
# generation prompts at different temperatures produce meaningfully different
# phrasings without having to ask the prompt to "be different".
_VARIANT_TEMPERATURES: tuple[float, ...] = (0.7, 1.0, 1.2)


def _has_lead_message(history: list[dict[str, Any]]) -> bool:
    """True when the thread already contains at least one inbound lead reply."""

    return any((m.get("role") == MessageRole.LEAD) for m in history)


async def generate_reply_variants(
    *,
    workspace: Workspace,
    campaign: Campaign,
    lead: Lead,
    thread: Thread,
    history: list[dict[str, Any]],
    n: int = 3,
) -> list[dict[str, Any]]:
    """Return ``n`` draft suggestions for the next outbound on ``thread``.

    Routes to the follow-up flow as soon as the lead has spoken at all on
    the thread (every inbound-triggered park lands here). Falls back to
    the cold-outreach generate-then-evaluate loop only for the rare case
    where the operator regenerates suggestions before any inbound — at
    that point the message under draft is still structurally a first
    touch and the evaluator's checks (mandatory credential, etc.) apply.

    Takes ``history`` as plain data (instead of a SQLAlchemy session) so
    the caller can release the SQLite write lock before this kicks off N
    parallel LLM calls. See :func:`_generate_outreach_style_variants` for
    the locking-deadlock anatomy that motivated the split.
    """

    if n <= 0:
        return []

    if _has_lead_message(history):
        return await _generate_followup_variants(
            workspace=workspace,
            campaign=campaign,
            lead=lead,
            thread=thread,
            history=history,
            n=n,
        )

    return await _generate_outreach_style_variants(
        workspace=workspace,
        campaign=campaign,
        lead=lead,
        thread=thread,
        history=history,
        n=n,
    )


async def _generate_followup_variants(
    *,
    workspace: Workspace,
    campaign: Campaign,
    lead: Lead,
    thread: Thread,
    history: list[dict[str, Any]],
    n: int,
) -> list[dict[str, Any]]:
    """Single-call-per-variant follow-up flow. No evaluator loop.

    Each variant is one ``complete_text`` call at a different temperature.
    We don't run the eval / retry loop because (a) the cold-outreach
    evaluator's contract (mandatory credential line, "first message"
    framing) doesn't fit follow-ups and would re-introduce the very
    re-pitch we're trying to avoid, and (b) the operator is already the
    quality gate — they read every draft before sending one.
    """

    settings_llm, _eval_threshold, _eval_max_attempts = read_loop_settings(workspace)
    base_temperature = float(settings_llm.get("temperature_main", 1.0))

    # Mix the workspace base temperature into the spread so a workspace
    # configured for low-creativity sticks closer to its setting.
    temperatures = [
        max(0.0, min(2.0, base_temperature + (t - 1.0)))
        for t in (_VARIANT_TEMPERATURES[i % len(_VARIANT_TEMPERATURES)] for i in range(n))
    ]

    system = followup_reply.build_system_prompt(thread.tone_snapshot)
    user = followup_reply.build_user_prompt(
        campaign_goal=campaign.goal,
        lead_short_name=lead.name,
        lead_category=lead.category,
        message_history=history,
    )

    ctx = LlmCallContext(
        purpose=LlmCallPurpose.GENERATION,
        workspace_id=workspace.id,
        campaign_id=campaign.id,
        thread_id=thread.id,
        lead_id=lead.id,
    )

    async def _one(temperature: float) -> dict[str, Any] | Exception:
        try:
            result = await complete_text(
                system=system,
                user=user,
                model=settings_llm["model_main"],
                prompt_version=followup_reply.PROMPT_VERSION,
                temperature=temperature,
                context=ctx,
            )
        except Exception as exc:
            return exc
        return {
            "draft": result.text.strip(),
            # No evaluator ran — these stay null so the UI can hide the
            # score chip and the consumer code can tell the two flows
            # apart by inspecting the suggestion shape alone.
            "overall": None,
            "scores": None,
            "feedback": None,
            "pass": None,
            "attempts": 1,
            "temperature": temperature,
            "gen_llm_call_id": result.llm_call_id,
            "eval_llm_call_id": None,
            "source": "followup",
        }

    raw = await asyncio.gather(*(_one(t) for t in temperatures), return_exceptions=False)

    suggestions: list[dict[str, Any]] = []
    for index, item in enumerate(raw, start=1):
        if isinstance(item, Exception):
            logger.warning(
                "follow-up suggestion variant %d failed thread=%s: %s",
                index,
                thread.id,
                item,
            )
            continue
        if not item.get("draft"):
            continue
        suggestions.append(item)

    logger.info(
        "generated %d/%d follow-up suggestions thread=%s",
        len(suggestions),
        n,
        thread.id,
    )
    return suggestions


async def _generate_outreach_style_variants(
    *,
    workspace: Workspace,
    campaign: Campaign,
    lead: Lead,
    thread: Thread,
    history: list[dict[str, Any]],
    n: int,
) -> list[dict[str, Any]]:
    """Cold-outreach generate-then-evaluate flow.

    Only runs when the thread has no inbound lead messages yet. Each
    suggestion has the best draft from a generate/evaluate loop at a
    specific temperature, along with its score, feedback, and the LLM
    call ids so the UI can link to them. Generations run in parallel.

    Takes ``history`` as plain data (instead of a SQLAlchemy session)
    so the caller can release the SQLite write lock before this kicks
    off N parallel LLM calls. Holding a write transaction here would
    serialise the parallel calls' ``LlmCall`` audit-row inserts against
    the outer transaction — which on SQLite is fatal: each audit row's
    ``BEGIN IMMEDIATE`` waits up to ``PRAGMA busy_timeout`` (2 min in
    our config) for the outer txn to commit, but the outer txn is
    itself awaiting these N calls to return. Decoupling history from
    the session is what breaks that cycle.
    """

    settings_llm, eval_threshold, eval_max_attempts = read_loop_settings(workspace)

    angle = thread.angle or ""

    temperatures = [
        _VARIANT_TEMPERATURES[i % len(_VARIANT_TEMPERATURES)] for i in range(n)
    ]

    tasks = [
        generate_and_evaluate(
            settings_llm=settings_llm,
            eval_threshold=eval_threshold,
            eval_max_attempts=eval_max_attempts,
            workspace=workspace,
            campaign=campaign,
            lead=lead,
            thread=thread,
            angle=angle,
            message_history=history,
            temperature_override=t,
        )
        for t in temperatures
    ]
    loop_results = await asyncio.gather(*tasks, return_exceptions=True)

    suggestions: list[dict[str, Any]] = []
    for index, (temp, result) in enumerate(zip(temperatures, loop_results), start=1):
        if isinstance(result, Exception):
            logger.warning(
                "suggestion variant %d failed thread=%s: %s",
                index,
                thread.id,
                result,
            )
            continue
        if not result.get("drafts"):
            continue
        # Align with the loop's canonical winner: on pass, `result["draft"]`
        # is the authoritative passing draft — look up its attempt entry so
        # scores / LLM call ids stay in lockstep. On fail, there's no
        # winner, so fall back to the last attempt.
        if result.get("status") == "pass" and result.get("draft") is not None:
            best = next(
                (a for a in result["drafts"] if a["draft"] == result["draft"]),
                result["drafts"][-1],
            )
        else:
            best = result["drafts"][-1]
        suggestions.append(
            {
                "draft": best["draft"],
                "overall": best["overall"],
                "scores": best["scores"],
                "feedback": best["feedback"],
                "pass": best["pass"],
                "attempts": result["attempts"],
                "temperature": temp,
                "gen_llm_call_id": best.get("gen_llm_call_id"),
                "eval_llm_call_id": best.get("eval_llm_call_id"),
                "source": "outreach",
            }
        )

    suggestions.sort(key=lambda s: (not s.get("pass"), -float(s.get("overall") or 0.0)))

    logger.info(
        "generated %d/%d suggestions thread=%s top_overall=%.3f",
        len(suggestions),
        n,
        thread.id,
        suggestions[0]["overall"] if suggestions else 0.0,
    )
    return suggestions


__all__ = ["generate_reply_variants"]
