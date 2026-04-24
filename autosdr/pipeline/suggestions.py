"""Generate multiple candidate reply drafts for the human-in-the-loop UI.

Used when ``auto_reply_enabled`` is false (the default): after the lead
replies we don't auto-send — instead we generate N draft variants, run each
through the evaluator, and stash them on ``thread.hitl_context.suggestions``
so the operator can pick one in the UI with a single click.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from sqlalchemy.orm import Session

from autosdr.models import Campaign, Lead, Thread, Workspace
from autosdr.pipeline._shared import (
    generate_and_evaluate,
    read_loop_settings,
    thread_history,
)

logger = logging.getLogger(__name__)


# Temperature spread used when we need multiple distinct drafts. Duplicate
# generation prompts at different temperatures produce meaningfully different
# phrasings without having to ask the prompt to "be different".
_VARIANT_TEMPERATURES: tuple[float, ...] = (0.7, 1.0, 1.2)


async def generate_reply_variants(
    *,
    session: Session,
    workspace: Workspace,
    campaign: Campaign,
    lead: Lead,
    thread: Thread,
    n: int = 3,
) -> list[dict[str, Any]]:
    """Return ``n`` draft suggestions for the next outbound on ``thread``.

    Each suggestion has the best draft from a generate/evaluate loop at a
    specific temperature, along with its score, feedback, and the LLM call
    ids so the UI can link to them. Generations run in parallel.
    """

    if n <= 0:
        return []

    settings_llm, eval_threshold, eval_max_attempts = read_loop_settings(workspace)

    history = thread_history(session, thread)
    angle = thread.angle or ""

    temperatures = [
        _VARIANT_TEMPERATURES[i % len(_VARIANT_TEMPERATURES)] for i in range(n)
    ]

    # Kick them off concurrently. Each attempt writes its own LlmCall rows so
    # there's no shared mutable state to worry about.
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
