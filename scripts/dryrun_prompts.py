"""One-off dry-run harness for reviewing prompt outputs without touching state.

Picks a sample of leads from the current workspace + campaign, runs the full
analyse -> generate -> evaluate loop against the LLM, and prints each draft
alongside the evaluator's scores / feedback.

Nothing is sent. Thread rows are not created. LLM calls are still logged to
llm_call / data/logs/llm-YYYYMMDD.jsonl by the client wrapper, which is fine
for after-the-fact review.

Usage::

    python scripts/dryrun_prompts.py                # 6 diverse leads, 1 draft each
    python scripts/dryrun_prompts.py --leads 29008212,b12303dc --attempts 3
"""

from __future__ import annotations

import argparse
import asyncio
import json
import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy.orm import Session

from autosdr.db import session_scope
from autosdr.llm import LlmCallContext, complete_json, complete_text
from autosdr.models import (
    Campaign,
    CampaignStatus,
    Lead,
    LlmCallPurpose,
    Workspace,
)
from autosdr.prompts import analysis, evaluation, generation


# ---------------------------------------------------------------------------
# A stand-in for the Thread row so we can reuse the prompt builders without
# creating any DB state. We only need `.id`, `.tone_snapshot` and `.angle`.
# ---------------------------------------------------------------------------


@dataclass
class _FauxThread:
    id: str
    tone_snapshot: str | None
    angle: str | None = None


# ---------------------------------------------------------------------------
# Analysis + generation + evaluation, inlined to avoid DB writes.
# ---------------------------------------------------------------------------


async def _run_analysis(
    *,
    settings_llm: dict[str, Any],
    workspace: Workspace,
    campaign: Campaign,
    lead: Lead,
    thread: _FauxThread,
    raw_data_size_limit_kb: int,
) -> dict[str, Any]:
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
    parsed.setdefault("owner_first_name", "")
    parsed.setdefault("owner_evidence", "")
    parsed.setdefault("confidence", 0.0)
    raw_owner = parsed.get("owner_first_name", "")
    raw_evidence = parsed.get("owner_evidence", "")
    validated_name, validated_evidence = analysis.validate_owner_first_name(
        owner_first_name=raw_owner,
        owner_evidence=raw_evidence,
        lead_name=lead.name,
    )
    parsed["owner_first_name"] = validated_name
    parsed["owner_evidence"] = validated_evidence
    parsed["_raw_owner_first_name"] = raw_owner
    parsed["_raw_owner_evidence"] = raw_evidence
    parsed["_raw_data_truncated"] = truncated
    parsed["_model"] = result.model
    return parsed


async def _generate_and_evaluate(
    *,
    settings_llm: dict[str, Any],
    eval_threshold: float,
    eval_max_attempts: int,
    workspace: Workspace,
    campaign: Campaign,
    lead: Lead,
    thread: _FauxThread,
    angle: str,
) -> dict[str, Any]:
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
            lead_category=lead.category,
        )
        eval_raw, _eval_result = await complete_json(
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
                "chars": len(draft),
                "scores": normalised["scores"],
                "overall": normalised["overall"],
                "pass": normalised["pass"],
                "feedback": normalised["feedback"],
            }
        )
        if normalised["pass"]:
            break
        feedback = normalised["feedback"]

    return {"attempts": attempts, "final": attempts[-1]}


# ---------------------------------------------------------------------------
# Lead picker — select a diverse sample by default.
# ---------------------------------------------------------------------------


_DEFAULT_SHORT_IDS = [
    "b12303dc",  # Green Wattle Sanctuary — old draft named "Ellen" (rule violation)
    "fcd24928",  # BreakFree Diamond Beach Broadbeach — named Danny + KB (rule violation)
    "1a55ec33",  # Caboolture Parklands — existing good output
    "98ea62be",  # Burpengary Pines — existing good output
    "1c864f52",  # RE/MAX Property Centre — non-healthcare
    "60e7d77d",  # Broadbeach Chempro Chemist — pharmacy (mixed register)
]


def _resolve_leads(session: Session, short_ids: list[str]) -> list[Lead]:
    leads: list[Lead] = []
    for short in short_ids:
        short = short.strip()
        if not short:
            continue
        row = session.query(Lead).filter(Lead.id.like(f"{short}%")).first()
        if row is None:
            print(f"!! lead not found: {short}")
            continue
        leads.append(row)
    return leads


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


async def _run(lead_short_ids: list[str], eval_max_attempts: int) -> None:
    with session_scope() as session:
        workspace = session.query(Workspace).first()
        if workspace is None:
            raise SystemExit("run `autosdr init` first")

        campaign = (
            session.query(Campaign)
            .filter(Campaign.workspace_id == workspace.id)
            .order_by(Campaign.created_at.desc())
            .first()
            or session.query(Campaign).first()
        )
        if campaign is None:
            raise SystemExit("no campaign yet — create one first")

        leads = _resolve_leads(session, lead_short_ids)
        if not leads:
            raise SystemExit("no leads resolved from the supplied ids")

        settings_blob = workspace.settings or {}
        settings_llm = settings_blob.get("llm") or {}
        eval_threshold = float(settings_blob.get("eval_threshold", 0.85))
        raw_data_size_limit_kb = int(settings_blob.get("raw_data_size_limit_kb", 50))

        # Detach everything from the session so awaits don't choke on autoflush.
        workspace_snapshot = workspace
        campaign_snapshot = campaign
        lead_snapshots = list(leads)

    results: list[dict[str, Any]] = []
    for lead in lead_snapshots:
        thread = _FauxThread(
            id=str(uuid.uuid4()),
            tone_snapshot=workspace_snapshot.tone_prompt,
        )

        print("\n" + "=" * 100)
        print(
            f"LEAD {lead.id[:8]}  {lead.name!r}"
            f"  category={lead.category!r}  address={lead.address!r}"
        )
        print("-" * 100)

        try:
            ana = await _run_analysis(
                settings_llm=settings_llm,
                workspace=workspace_snapshot,
                campaign=campaign_snapshot,
                lead=lead,
                thread=thread,
                raw_data_size_limit_kb=raw_data_size_limit_kb,
            )
        except Exception as exc:  # pragma: no cover
            import traceback
            print(f"ANALYSIS FAILED: {type(exc).__name__}: {exc!r}")
            traceback.print_exc()
            continue

        angle = str(ana.get("angle") or "").strip() or (
            f"{lead.category or 'business'} in {lead.address or 'the area'}"
        )
        owner_first_name = str(ana.get("owner_first_name") or "").strip()
        stored_angle = (
            f"Recipient owner's first name: {owner_first_name}\n\n{angle}"
            if owner_first_name
            else angle
        )
        thread.angle = stored_angle

        raw_owner = ana.get("_raw_owner_first_name", "")
        raw_evidence = ana.get("_raw_owner_evidence", "")
        owner_rejected = bool(raw_owner) and not owner_first_name
        print(f"angle_type    : {ana.get('angle_type')!r}")
        print(
            f"owner_first   : {owner_first_name!r}"
            + (
                f"   (REJECTED by validator; llm said {raw_owner!r} w/ evidence {raw_evidence[:80]!r})"
                if owner_rejected
                else ""
            )
        )
        print(f"confidence    : {ana.get('confidence')}")
        print(f"signal        : {ana.get('signal')}")
        print(f"angle         : {angle}")

        gen = await _generate_and_evaluate(
            settings_llm=settings_llm,
            eval_threshold=eval_threshold,
            eval_max_attempts=eval_max_attempts,
            workspace=workspace_snapshot,
            campaign=campaign_snapshot,
            lead=lead,
            thread=thread,
            angle=stored_angle,
        )
        for a in gen["attempts"]:
            print("-" * 100)
            print(f"attempt {a['attempt']}  chars={a['chars']}  overall={a['overall']}  pass={a['pass']}")
            print(f"  draft    : {a['draft']}")
            print(f"  scores   : {a['scores']}")
            if a["feedback"]:
                print(f"  feedback : {a['feedback']}")

        results.append(
            {
                "lead_id": lead.id,
                "name": lead.name,
                "category": lead.category,
                "angle_type": ana.get("angle_type"),
                "owner_first_name": owner_first_name,
                "confidence": ana.get("confidence"),
                "attempts": gen["attempts"],
                "final_draft": gen["final"]["draft"],
                "final_scores": gen["final"]["scores"],
                "final_overall": gen["final"]["overall"],
                "final_pass": gen["final"]["pass"],
                "final_feedback": gen["final"]["feedback"],
            }
        )

    print("\n" + "=" * 100)
    print("SUMMARY")
    print("=" * 100)
    for r in results:
        status = "PASS" if r["final_pass"] else "FAIL"
        print(
            f"{status}  {r['lead_id'][:8]}  ({r['category']!s:<25})  "
            f"overall={r['final_overall']}  owner={r['owner_first_name']!r}"
        )
        print(f"    {r['final_draft']}")

    out_path = "data/dryrun-results.json"
    with open(out_path, "w", encoding="utf-8") as fh:
        json.dump(results, fh, indent=2, ensure_ascii=False, default=str)
    print(f"\nfull results written to {out_path}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--leads",
        default=",".join(_DEFAULT_SHORT_IDS),
        help="Comma-separated short ids (8-char prefix).",
    )
    parser.add_argument(
        "--attempts",
        type=int,
        default=2,
        help="Max generate/evaluate attempts per lead.",
    )
    args = parser.parse_args()

    short_ids = [s for s in (args.leads or "").split(",") if s.strip()]
    asyncio.run(_run(short_ids, args.attempts))


if __name__ == "__main__":
    main()
