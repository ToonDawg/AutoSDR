"""HITL audit harness — replay the gen + eval outreach loop on a curated thread set.

The companion to ``scripts/llm_call_metrics.py`` (population stats post-deploy)
and ``scripts/replay_evaluator.py`` (eval-only golden replay). This script
replays the FULL generate-then-evaluate loop against a pinned set of
historical threads so you can eyeball whether a prompt change regressed
draft quality or scoring before you ship it to real recipients.

Workflow
--------

1. ``--init-golden-set`` once to populate ``data/audit/golden_threads.json``
   with a diverse 8-thread sample. Commit this file so every audit run hits
   the same threads.
2. Make a prompt change (e.g. shrink ``evaluation.py``) and bump its
   ``PROMPT_VERSION``.
3. ``./scripts/replay_outreach_loop.py --name evaluation-v4.5`` to replay
   the golden set against the live LLM and produce a markdown side-by-side
   report at ``data/audit/<name>/report.md``.
4. Read the report on your phone. Each thread shows OLD draft + scores
   (from the historical run) next to NEW draft + scores (from this run),
   plus an empty ``Reviewer notes`` section to fill in.
5. Compare two reports cheaply by diffing their ``report.json`` sidecars.

Modes
-----

- ``--render-only``: don't call the LLM at all; render prompts and dump
  byte counts. Use this to verify prompt-rendering changes deterministically
  before spending money. Always safe.
- ``--purpose gen|eval|both`` (default ``both``): which side of the loop to
  exercise. ``eval`` mode reuses the historical draft (so any score delta
  is attributable to the eval prompt change); ``gen`` mode generates a fresh
  draft and only re-evaluates if ``--purpose both``.
- ``--model gemini|local|<litellm-string>`` (default ``gemini``): provider
  to hit. ``local`` routes to LM Studio's OpenAI-compatible endpoint at
  ``http://localhost:1234/v1`` using whatever model is loaded; useful for
  cheap iteration but DO NOT trust the score numbers (different family).
- ``--apply``: persist the new ``llm_call`` rows. Default is dry-run (no
  DB writes) so a panicked Ctrl-C won't pollute production metrics.

Example::

    .venv/bin/python scripts/replay_outreach_loop.py --init-golden-set
    .venv/bin/python scripts/replay_outreach_loop.py --name baseline
    # ...edit evaluation.py, bump PROMPT_VERSION...
    .venv/bin/python scripts/replay_outreach_loop.py --name evaluation-v4.5
    diff data/audit/baseline/report.json data/audit/evaluation-v4.5/report.json
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
import sys
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from autosdr.config import get_settings
from autosdr.db import session_scope
from autosdr.llm import LlmCallContext, complete_json, complete_text
from autosdr.llm.client import apply_llm_provider_keys
from autosdr.models import (
    Campaign,
    CampaignLead,
    Lead,
    LlmCall,
    LlmCallPurpose,
    Message,
    MessageRole,
    Thread,
    Workspace,
)
from autosdr.prompts import evaluation, generation


# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------


DEFAULT_GOLDEN_PATH = Path("data/audit/golden_threads.json")
DEFAULT_AUDIT_DIR = Path("data/audit")
DEFAULT_GOLDEN_SIZE = 8
LM_STUDIO_API_BASE = "http://localhost:1234/v1"
LM_STUDIO_DEFAULT_MODEL = "google/gemma-4-31b"


# ---------------------------------------------------------------------------
# Frozen inputs — everything the gen + eval prompts need, reconstructed from DB
# ---------------------------------------------------------------------------


@dataclass
class FrozenThread:
    """Inputs to the generation + evaluation prompts for a single historical thread.

    All fields are populated from the DB so the prompts can be rendered
    without any new pipeline code paths. ``historical_draft`` is the AI
    message that was actually sent (or last-attempted, for HITL-paused
    threads); we reuse it for the eval-only replay path.
    """

    thread_id: str
    workspace_id: str
    campaign_id: str
    lead_id: str

    # Display / identification
    lead_name: str
    lead_category: str | None
    lead_address: str | None
    angle_type: str | None
    short_id: str  # 8-char prefix for UI

    # Generation inputs
    business_data: dict[str, Any]
    business_dump: str
    campaign_goal: str
    angle: str
    lead_short_name: str | None
    tone_snapshot: str | None

    # Evaluation reference draft (also used as fallback when --purpose eval only)
    historical_draft: str

    # Historical references for diffing
    historical_gen_prompt_version: str | None
    historical_eval_prompt_version: str | None
    historical_eval_scores: dict[str, Any] | None
    historical_eval_overall: float | None
    historical_eval_pass: bool | None
    historical_eval_feedback: str | None


# ---------------------------------------------------------------------------
# Selection
# ---------------------------------------------------------------------------


def _pick_diverse_threads(session: Session, *, limit: int) -> list[str]:
    """Return ``limit`` thread ids spread across (angle_type x pass/fail).

    Same selection logic as ``replay_evaluator._pick_default_threads`` —
    pulls the most recent 400 threads with a stored eval response, then
    bins them by (angle_type, pass) and picks one from each non-empty bin
    until ``limit`` is reached.
    """

    stmt = (
        select(Thread.id, Thread.angle_type, LlmCall.response_parsed)
        .join(LlmCall, LlmCall.thread_id == Thread.id)
        .where(LlmCall.purpose == LlmCallPurpose.EVALUATION)
        .where(LlmCall.error.is_(None))
        .where(LlmCall.response_parsed.is_not(None))
        .order_by(Thread.created_at.desc())
        .limit(400)
    )

    seen_pair: dict[tuple[str, bool], str] = {}
    chosen: list[str] = []
    for tid, angle_type, parsed in session.execute(stmt).all():
        if not isinstance(parsed, dict):
            continue
        passed = bool(parsed.get("pass") is True)
        key = ((angle_type or "(unknown)"), passed)
        if key in seen_pair or tid in chosen:
            continue
        seen_pair[key] = tid
        chosen.append(tid)
        if len(chosen) >= limit:
            break

    if len(chosen) < limit:
        # Top up with the newest threads we haven't already picked.
        recent_stmt = (
            select(Thread.id)
            .order_by(Thread.created_at.desc())
            .limit(limit * 4)
        )
        for (tid,) in session.execute(recent_stmt).all():
            if tid in chosen:
                continue
            chosen.append(tid)
            if len(chosen) >= limit:
                break
    return chosen[:limit]


def _freeze_thread(session: Session, thread_id: str) -> FrozenThread | None:
    """Reconstruct gen + eval inputs for ``thread_id``, or return None."""

    thread = session.get(Thread, thread_id)
    if thread is None:
        return None

    cl = session.get(CampaignLead, thread.campaign_lead_id)
    if cl is None:
        return None

    campaign = session.get(Campaign, cl.campaign_id)
    lead = session.get(Lead, cl.lead_id)
    if campaign is None or lead is None:
        return None

    workspace = session.get(Workspace, campaign.workspace_id)
    if workspace is None:
        return None

    # Pull the first AI message (i.e. the original outreach draft) — this is
    # what the historical eval scored. For HITL-paused threads with no send,
    # fall back to the last persisted draft in hitl_context.last_drafts.
    draft_msg = (
        session.query(Message)
        .filter(Message.thread_id == thread.id)
        .filter(Message.role == MessageRole.AI)
        .order_by(Message.created_at.asc())
        .first()
    )
    if draft_msg is not None:
        historical_draft = draft_msg.content
        analysis_meta = (draft_msg.metadata_ or {}).get("analysis") or {}
        lead_short_name = analysis_meta.get("lead_short_name") or None
    else:
        ctx = thread.hitl_context or {}
        last_drafts = ctx.get("last_drafts") or []
        if not last_drafts:
            return None
        historical_draft = str(last_drafts[-1])
        lead_short_name = None

    historical_eval = (
        session.query(LlmCall)
        .filter(LlmCall.thread_id == thread.id)
        .filter(LlmCall.purpose == LlmCallPurpose.EVALUATION)
        .filter(LlmCall.error.is_(None))
        .order_by(LlmCall.created_at.desc())
        .first()
    )
    historical_gen = (
        session.query(LlmCall)
        .filter(LlmCall.thread_id == thread.id)
        .filter(LlmCall.purpose == LlmCallPurpose.GENERATION)
        .filter(LlmCall.error.is_(None))
        .order_by(LlmCall.created_at.desc())
        .first()
    )

    parsed = historical_eval.response_parsed if historical_eval else None
    return FrozenThread(
        thread_id=thread.id,
        workspace_id=workspace.id,
        campaign_id=campaign.id,
        lead_id=lead.id,
        lead_name=lead.name or "(unnamed)",
        lead_category=lead.category,
        lead_address=lead.address,
        angle_type=thread.angle_type,
        short_id=thread.id[:8],
        business_data=workspace.business_data or {},
        business_dump=workspace.business_dump or "",
        campaign_goal=campaign.goal,
        angle=thread.angle or "",
        lead_short_name=lead_short_name,
        tone_snapshot=thread.tone_snapshot,
        historical_draft=historical_draft,
        historical_gen_prompt_version=historical_gen.prompt_version if historical_gen else None,
        historical_eval_prompt_version=historical_eval.prompt_version if historical_eval else None,
        historical_eval_scores=(parsed or {}).get("scores") if isinstance(parsed, dict) else None,
        historical_eval_overall=(parsed or {}).get("overall") if isinstance(parsed, dict) else None,
        historical_eval_pass=(parsed or {}).get("pass") if isinstance(parsed, dict) else None,
        historical_eval_feedback=(parsed or {}).get("feedback") if isinstance(parsed, dict) else None,
    )


# ---------------------------------------------------------------------------
# Golden set IO
# ---------------------------------------------------------------------------


def _load_golden_set(path: Path) -> list[str] | None:
    if not path.exists():
        return None
    payload = json.loads(path.read_text(encoding="utf-8"))
    threads = payload.get("threads") or []
    return [t["id"] for t in threads if t.get("id")]


def _write_golden_set(path: Path, frozen: list[FrozenThread]) -> None:
    """Persist the chosen thread set so subsequent runs hit the same threads."""

    payload = {
        "selected_at": datetime.now(timezone.utc).isoformat(),
        "selection_method": "auto-diverse-by-angle-type-and-pass",
        "comment": (
            "Curated set of historical threads used by "
            "scripts/replay_outreach_loop.py to audit prompt changes. "
            "Edit by hand to add/remove specific known-tricky threads. "
            "Keep ~8-12 entries — enough for diversity, few enough to "
            "eyeball in one sitting."
        ),
        "threads": [
            {
                "id": f.thread_id,
                "lead_name": f.lead_name,
                "category": f.lead_category,
                "angle_type": f.angle_type,
                "historical_pass": f.historical_eval_pass,
                "historical_overall": f.historical_eval_overall,
            }
            for f in frozen
        ],
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")


# ---------------------------------------------------------------------------
# Replay
# ---------------------------------------------------------------------------


@dataclass
class ReplayResult:
    """Per-thread output of the replay — both gen and eval halves."""

    frozen: FrozenThread
    # Generation
    gen_prompt_version: str | None = None
    gen_system_chars: int = 0
    gen_user_chars: int = 0
    gen_draft: str | None = None
    gen_tokens_in: int = 0
    gen_tokens_out: int = 0
    gen_latency_ms: int = 0
    gen_error: str | None = None
    # Evaluation
    eval_prompt_version: str | None = None
    eval_system_chars: int = 0
    eval_user_chars: int = 0
    eval_scores: dict[str, Any] | None = None
    eval_overall: float | None = None
    eval_pass: bool | None = None
    eval_feedback: str | None = None
    eval_tokens_in: int = 0
    eval_tokens_out: int = 0
    eval_latency_ms: int = 0
    eval_error: str | None = None
    # Whether the draft we evaluated was historical or freshly generated
    eval_target: str = "historical"  # or "fresh"


async def _replay_thread(
    frozen: FrozenThread,
    *,
    purpose: str,
    gen_model: str,
    eval_model: str,
    render_only: bool,
    persist: bool,
) -> ReplayResult:
    """Run one thread through whichever halves of the loop the caller asked for.

    ``--purpose eval`` uses the historical draft as the eval target so the
    score delta is attributable purely to the eval prompt change.
    ``--purpose gen`` produces a fresh draft and skips eval.
    ``--purpose both`` chains them: fresh draft → eval that draft.
    """

    result = ReplayResult(frozen=frozen)
    do_gen = purpose in {"gen", "both"}
    do_eval = purpose in {"eval", "both"}

    target_draft = frozen.historical_draft
    result.eval_target = "historical"

    if do_gen:
        gen_system = generation.build_system_prompt(frozen.tone_snapshot)
        gen_user = generation.build_user_prompt(
            business_data=frozen.business_data,
            business_dump=frozen.business_dump,
            campaign_goal=frozen.campaign_goal,
            angle=frozen.angle,
            lead_name=frozen.lead_name,
            lead_short_name=frozen.lead_short_name,
            lead_category=frozen.lead_category,
            lead_address=frozen.lead_address,
        )
        result.gen_prompt_version = generation.PROMPT_VERSION
        result.gen_system_chars = len(gen_system)
        result.gen_user_chars = len(gen_user)

        if not render_only:
            ctx = LlmCallContext(
                purpose=LlmCallPurpose.GENERATION,
                workspace_id=frozen.workspace_id,
                campaign_id=frozen.campaign_id,
                thread_id=frozen.thread_id if persist else None,
                lead_id=frozen.lead_id,
            )
            try:
                gen = await complete_text(
                    system=gen_system,
                    user=gen_user,
                    model=gen_model,
                    prompt_version=generation.PROMPT_VERSION,
                    temperature=1.0,
                    context=ctx,
                )
                result.gen_draft = gen.text.strip()
                result.gen_tokens_in = gen.tokens_in
                result.gen_tokens_out = gen.tokens_out
                result.gen_latency_ms = gen.latency_ms
                if do_eval:
                    target_draft = result.gen_draft
                    result.eval_target = "fresh"
            except Exception as exc:
                result.gen_error = f"{type(exc).__name__}: {exc}"

    if do_eval:
        eval_system = evaluation.build_system_prompt()
        eval_user = evaluation.build_user_prompt(
            tone_snapshot=frozen.tone_snapshot,
            campaign_goal=frozen.campaign_goal,
            angle=frozen.angle,
            draft=target_draft,
            lead_category=frozen.lead_category,
        )
        result.eval_prompt_version = evaluation.PROMPT_VERSION
        result.eval_system_chars = len(eval_system)
        result.eval_user_chars = len(eval_user)

        if not render_only:
            ctx = LlmCallContext(
                purpose=LlmCallPurpose.EVALUATION,
                workspace_id=frozen.workspace_id,
                campaign_id=frozen.campaign_id,
                thread_id=frozen.thread_id if persist else None,
                lead_id=frozen.lead_id,
            )
            try:
                parsed, ev = await complete_json(
                    system=eval_system,
                    user=eval_user,
                    model=eval_model,
                    prompt_version=evaluation.PROMPT_VERSION,
                    temperature=0.0,
                    context=ctx,
                )
                normalised = evaluation.evaluate_result(parsed, draft=target_draft)
                result.eval_scores = normalised["scores"]
                result.eval_overall = normalised["overall"]
                result.eval_pass = normalised["pass"]
                result.eval_feedback = normalised["feedback"]
                result.eval_tokens_in = ev.tokens_in
                result.eval_tokens_out = ev.tokens_out
                result.eval_latency_ms = ev.latency_ms
            except Exception as exc:
                result.eval_error = f"{type(exc).__name__}: {exc}"

    return result


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------


def _git_sha() -> str:
    try:
        out = subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"], stderr=subprocess.DEVNULL
        )
        return out.decode().strip()
    except Exception:
        return "no-git"


def _fmt_score_block(scores: dict[str, Any] | None, overall: float | None) -> str:
    """Render scores as a one-line summary plus a per-criterion list."""

    if not scores:
        return "_(no scores)_"
    parts = [f"`overall={overall}`" if overall is not None else "_(no overall)_"]
    parts.append(
        " | ".join(
            f"{k}={scores.get(k, 'NA')}"
            for k in ("tone_match", "personalisation", "goal_alignment", "length_valid", "naturalness")
        )
    )
    return "  \n".join(parts)


def _fmt_delta(old: float | None, new: float | None) -> str:
    if not isinstance(old, (int, float)) or not isinstance(new, (int, float)):
        return ""
    return f" (Δ={new - old:+.3f})"


def _summarise(results: list[ReplayResult], *, render_only: bool) -> dict[str, Any]:
    """Roll the per-thread results up into headline stats for the report top."""

    summary: dict[str, Any] = {
        "threads": len(results),
        "render_only": render_only,
    }
    if render_only:
        summary["gen_system_chars_avg"] = (
            sum(r.gen_system_chars for r in results) // max(len(results), 1)
        )
        summary["gen_user_chars_avg"] = (
            sum(r.gen_user_chars for r in results) // max(len(results), 1)
        )
        summary["eval_system_chars_avg"] = (
            sum(r.eval_system_chars for r in results) // max(len(results), 1)
        )
        summary["eval_user_chars_avg"] = (
            sum(r.eval_user_chars for r in results) // max(len(results), 1)
        )
        return summary

    eval_results = [r for r in results if r.eval_overall is not None]
    pass_flips = 0
    deltas: list[float] = []
    for r in eval_results:
        old = r.frozen.historical_eval_pass
        new = r.eval_pass
        if old is not None and bool(old) != bool(new):
            pass_flips += 1
        old_o = r.frozen.historical_eval_overall
        new_o = r.eval_overall
        if isinstance(old_o, (int, float)) and isinstance(new_o, (int, float)):
            deltas.append(float(new_o) - float(old_o))

    summary.update(
        {
            "pass_flips": pass_flips,
            "delta_overall_avg": (
                sum(deltas) / len(deltas) if deltas else None
            ),
            "delta_overall_min": min(deltas) if deltas else None,
            "delta_overall_max": max(deltas) if deltas else None,
            "gen_tokens_in_total": sum(r.gen_tokens_in for r in results),
            "gen_tokens_out_total": sum(r.gen_tokens_out for r in results),
            "eval_tokens_in_total": sum(r.eval_tokens_in for r in results),
            "eval_tokens_out_total": sum(r.eval_tokens_out for r in results),
            "gen_latency_ms_avg": (
                sum(r.gen_latency_ms for r in results) // max(len(results), 1)
            ),
            "eval_latency_ms_avg": (
                sum(r.eval_latency_ms for r in results) // max(len(results), 1)
            ),
        }
    )
    return summary


def _write_markdown_report(
    out_path: Path,
    *,
    run_name: str,
    summary: dict[str, Any],
    results: list[ReplayResult],
    purpose: str,
    gen_model: str,
    eval_model: str,
    render_only: bool,
) -> None:
    """Render the side-by-side OLD / NEW report for the human reviewer."""

    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    sha = _git_sha()

    lines: list[str] = []
    lines.append(f"# Audit replay — {run_name}")
    lines.append("")
    lines.append(f"_Generated {now}, git {sha}, purpose=`{purpose}`, "
                 f"gen_model=`{gen_model}`, eval_model=`{eval_model}`, "
                 f"render_only={render_only}_")
    lines.append("")
    lines.append(f"_Current prompt versions: generation=`{generation.PROMPT_VERSION}`, "
                 f"evaluation=`{evaluation.PROMPT_VERSION}`_")
    lines.append("")

    lines.append("## Summary")
    lines.append("")
    for key, value in summary.items():
        lines.append(f"- **{key}**: `{value}`")
    lines.append("")

    if not render_only and purpose in {"eval", "both"}:
        # Quick at-a-glance pass/fail table.
        lines.append("## Pass-rate table")
        lines.append("")
        lines.append("| thread | lead | category | OLD pass | NEW pass | OLD overall | NEW overall | Δ |")
        lines.append("|---|---|---|---|---|---|---|---|")
        for r in results:
            old_p = r.frozen.historical_eval_pass
            new_p = r.eval_pass
            old_o = r.frozen.historical_eval_overall
            new_o = r.eval_overall
            delta = _fmt_delta(old_o, new_o).strip(" ()")
            flip = " 🔁" if (old_p is not None and bool(old_p) != bool(new_p)) else ""
            lines.append(
                f"| `{r.frozen.short_id}` | {r.frozen.lead_name} | "
                f"{r.frozen.lead_category or '—'} | "
                f"{old_p} | {new_p}{flip} | "
                f"{old_o} | {new_o} | {delta} |"
            )
        lines.append("")

    lines.append("## Per-thread")
    lines.append("")

    for idx, r in enumerate(results, start=1):
        f = r.frozen
        lines.append(f"### {idx}. {f.lead_name} — `{f.short_id}`")
        lines.append("")
        meta_bits = [
            f"category=**{f.lead_category or '—'}**",
            f"angle_type=**{f.angle_type or '—'}**",
        ]
        if f.lead_short_name:
            meta_bits.append(f"short_name=**{f.lead_short_name}**")
        lines.append("- " + " | ".join(meta_bits))
        lines.append("- prompt versions OLD: "
                     f"gen=`{f.historical_gen_prompt_version or '—'}`, "
                     f"eval=`{f.historical_eval_prompt_version or '—'}`")
        lines.append(f"- eval target this run: **{r.eval_target}** draft")
        lines.append("")

        # Angle (collapsed, can be long)
        lines.append("<details><summary>Angle</summary>")
        lines.append("")
        lines.append("```")
        lines.append((f.angle or "(no angle)").strip())
        lines.append("```")
        lines.append("</details>")
        lines.append("")

        # Drafts: OLD always; NEW only if we generated
        lines.append("**OLD draft (historical):**")
        lines.append("")
        lines.append("> " + (f.historical_draft or "_(no draft)_").replace("\n", "\n> "))
        lines.append("")

        if r.gen_draft is not None or r.gen_error:
            lines.append(f"**NEW draft (`{r.gen_prompt_version}`):**")
            lines.append("")
            if r.gen_error:
                lines.append(f"> ⚠️ ERROR: {r.gen_error}")
            else:
                lines.append("> " + (r.gen_draft or "").replace("\n", "\n> "))
            lines.append("")
            lines.append(
                f"_gen prompt sizes: system=`{r.gen_system_chars}` chars, "
                f"user=`{r.gen_user_chars}` chars; "
                f"tokens_in=`{r.gen_tokens_in}`, tokens_out=`{r.gen_tokens_out}`, "
                f"latency_ms=`{r.gen_latency_ms}`_"
            )
            lines.append("")

        # Eval: OLD if present, NEW if we evaluated
        if f.historical_eval_overall is not None:
            lines.append(f"**OLD eval (`{f.historical_eval_prompt_version}`):**")
            lines.append("")
            lines.append(_fmt_score_block(f.historical_eval_scores, f.historical_eval_overall))
            lines.append("")
            if f.historical_eval_feedback:
                lines.append(f"> _feedback:_ {f.historical_eval_feedback}")
                lines.append("")

        if r.eval_overall is not None or r.eval_error:
            lines.append(f"**NEW eval (`{r.eval_prompt_version}`):**")
            lines.append("")
            if r.eval_error:
                lines.append(f"> ⚠️ ERROR: {r.eval_error}")
            else:
                lines.append(_fmt_score_block(r.eval_scores, r.eval_overall))
                lines.append("")
                lines.append(f"- pass: **{r.eval_pass}**")
                if r.eval_feedback:
                    lines.append(f"- feedback: _{r.eval_feedback}_")
                lines.append(
                    f"- eval prompt sizes: system=`{r.eval_system_chars}` chars, "
                    f"user=`{r.eval_user_chars}` chars; "
                    f"tokens_in=`{r.eval_tokens_in}`, tokens_out=`{r.eval_tokens_out}`, "
                    f"latency_ms=`{r.eval_latency_ms}`"
                )
            lines.append("")

        lines.append("### Reviewer notes")
        lines.append("")
        lines.append("- thumbs: ☐ up  ☐ down  ☐ same  ☐ regression")
        lines.append("- notes: ")
        lines.append("")
        lines.append("---")
        lines.append("")

    out_path.write_text("\n".join(lines), encoding="utf-8")


def _write_json_report(
    out_path: Path,
    *,
    run_name: str,
    summary: dict[str, Any],
    results: list[ReplayResult],
    purpose: str,
    gen_model: str,
    eval_model: str,
    render_only: bool,
) -> None:
    payload = {
        "run_name": run_name,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "git_sha": _git_sha(),
        "purpose": purpose,
        "gen_model": gen_model,
        "eval_model": eval_model,
        "render_only": render_only,
        "current_prompt_versions": {
            "generation": generation.PROMPT_VERSION,
            "evaluation": evaluation.PROMPT_VERSION,
        },
        "summary": summary,
        "results": [
            {
                **{k: v for k, v in asdict(r).items() if k != "frozen"},
                "frozen": {
                    "thread_id": r.frozen.thread_id,
                    "short_id": r.frozen.short_id,
                    "lead_name": r.frozen.lead_name,
                    "lead_category": r.frozen.lead_category,
                    "angle_type": r.frozen.angle_type,
                    "lead_short_name": r.frozen.lead_short_name,
                    "historical_draft": r.frozen.historical_draft,
                    "historical_gen_prompt_version": r.frozen.historical_gen_prompt_version,
                    "historical_eval_prompt_version": r.frozen.historical_eval_prompt_version,
                    "historical_eval_scores": r.frozen.historical_eval_scores,
                    "historical_eval_overall": r.frozen.historical_eval_overall,
                    "historical_eval_pass": r.frozen.historical_eval_pass,
                    "historical_eval_feedback": r.frozen.historical_eval_feedback,
                },
            }
            for r in results
        ],
    }
    out_path.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False, default=str),
        encoding="utf-8",
    )


# ---------------------------------------------------------------------------
# Model resolution
# ---------------------------------------------------------------------------


def _resolve_models(
    *,
    workspace_settings: dict[str, Any],
    cli_model: str | None,
) -> tuple[str, str]:
    """Return ``(gen_model, eval_model)`` per the ``--model`` flag.

    - ``gemini`` (or unset): pull both from workspace settings, same as prod.
    - ``local``: route both to LM Studio's OpenAI-compatible endpoint via
      LiteLLM's native ``lm_studio/`` provider prefix. ``LM_STUDIO_MODEL``
      env var picks the loaded model id (default: gemma-4-31b).
      ``LM_STUDIO_API_BASE`` defaults to ``http://localhost:1234/v1``.
      ``complete_json`` knows to drop the ``json_object`` response_format
      for this provider (LM Studio rejects it).
    - anything else: pass through verbatim as a single model used for both.
    """

    llm_settings = workspace_settings.get("llm") or {}

    if cli_model in (None, "gemini"):
        return (
            llm_settings.get("model_main", "gemini/gemini-3-flash-preview"),
            llm_settings.get("model_eval", "gemini/gemini-3.1-flash-lite-preview"),
        )

    if cli_model == "local":
        local_model = os.environ.get("LM_STUDIO_MODEL", LM_STUDIO_DEFAULT_MODEL)
        os.environ.setdefault("LM_STUDIO_API_BASE", LM_STUDIO_API_BASE)
        os.environ.setdefault("LM_STUDIO_API_KEY", "lm-studio")
        full = f"lm_studio/{local_model}"
        return full, full

    return cli_model, cli_model


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------


async def _run(args: argparse.Namespace) -> None:
    settings = get_settings()
    settings.llm_log_enabled = bool(args.apply)

    audit_root = Path(args.out) if args.out else DEFAULT_AUDIT_DIR
    golden_path = Path(args.threads_file) if args.threads_file else DEFAULT_GOLDEN_PATH

    with session_scope() as session:
        workspace = session.query(Workspace).first()
        if workspace is None:
            raise SystemExit("no workspace — run `autosdr init` first")
        apply_llm_provider_keys(workspace.settings or {})
        gen_model, eval_model = _resolve_models(
            workspace_settings=workspace.settings or {},
            cli_model=args.model,
        )

        # Resolve which threads to replay
        if args.threads:
            short_ids = [s.strip() for s in args.threads.split(",") if s.strip()]
            chosen: list[str] = []
            for short in short_ids:
                row = (
                    session.query(Thread.id)
                    .filter(Thread.id.like(f"{short}%"))
                    .first()
                )
                if row is None:
                    print(f"!! thread not found: {short}")
                    continue
                chosen.append(row[0])
        elif args.init_golden_set or _load_golden_set(golden_path) is None:
            chosen = _pick_diverse_threads(session, limit=args.num)
        else:
            ids_from_file = _load_golden_set(golden_path) or []
            chosen = ids_from_file

        frozen: list[FrozenThread] = []
        for tid in chosen:
            f = _freeze_thread(session, tid)
            if f is None:
                print(f"!! could not freeze inputs for thread {tid[:8]} — skipping")
                continue
            frozen.append(f)

    if not frozen:
        raise SystemExit("no threads frozen — nothing to replay")

    # Init the golden set file (write + exit) so subsequent runs are repeatable.
    if args.init_golden_set:
        _write_golden_set(golden_path, frozen)
        print(f"wrote {len(frozen)} threads to {golden_path}")
        for f in frozen:
            print(
                f"  {f.short_id}  angle_type={f.angle_type or '—':<18} "
                f"category={f.lead_category or '—':<22} {f.lead_name}"
            )
        return

    # Auto-bootstrap: if the golden file didn't exist, write it now from the
    # diverse pick above so the next run is repeatable.
    if not args.threads and _load_golden_set(golden_path) is None:
        _write_golden_set(golden_path, frozen)
        print(f"(bootstrapped golden set at {golden_path} with "
              f"{len(frozen)} threads)")

    # Resolve output dir
    run_name = args.name or f"{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M')}"
    out_dir = audit_root / run_name
    out_dir.mkdir(parents=True, exist_ok=True)

    print(
        f"replay run '{run_name}' threads={len(frozen)} purpose={args.purpose} "
        f"gen_model={gen_model} eval_model={eval_model} "
        f"render_only={args.render_only} apply={args.apply}"
    )

    results: list[ReplayResult] = []
    started = time.monotonic()
    for f in frozen:
        print(f"  replaying {f.short_id} {f.lead_name!r:30s} "
              f"angle_type={f.angle_type or '—'}", flush=True)
        result = await _replay_thread(
            f,
            purpose=args.purpose,
            gen_model=gen_model,
            eval_model=eval_model,
            render_only=args.render_only,
            persist=args.apply,
        )
        results.append(result)
        if result.gen_error:
            print(f"    !! gen error: {result.gen_error}")
        if result.eval_error:
            print(f"    !! eval error: {result.eval_error}")
    elapsed_s = time.monotonic() - started

    summary = _summarise(results, render_only=args.render_only)
    summary["wall_clock_s"] = round(elapsed_s, 2)

    md_path = out_dir / "report.md"
    json_path = out_dir / "report.json"
    _write_markdown_report(
        md_path,
        run_name=run_name,
        summary=summary,
        results=results,
        purpose=args.purpose,
        gen_model=gen_model,
        eval_model=eval_model,
        render_only=args.render_only,
    )
    _write_json_report(
        json_path,
        run_name=run_name,
        summary=summary,
        results=results,
        purpose=args.purpose,
        gen_model=gen_model,
        eval_model=eval_model,
        render_only=args.render_only,
    )

    # Pin a copy of the golden set we used so the run is fully reproducible.
    _write_golden_set(out_dir / "golden_threads.json", [r.frozen for r in results])

    print()
    print(f"wrote markdown report -> {md_path}")
    print(f"wrote json report     -> {json_path}")
    print()
    print("SUMMARY:")
    for k, v in summary.items():
        print(f"  {k:30s} {v}")


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--name", default=None,
                   help="Run name; becomes data/audit/<name>/. Default is a UTC timestamp.")
    p.add_argument("--purpose", choices=("gen", "eval", "both"), default="both",
                   help="Which half of the loop to replay. Default 'both' "
                        "= generate fresh draft + evaluate it.")
    p.add_argument("--model", default=None,
                   help="'gemini' (default; uses workspace settings), 'local' (LM "
                        "Studio at :1234), or any LiteLLM model string applied to "
                        "both gen and eval.")
    p.add_argument("--render-only", action="store_true",
                   help="Render prompts only; no LLM calls. Always safe.")
    p.add_argument("--apply", action="store_true",
                   help="Persist the new llm_call rows. Default: dry-run, no DB writes.")
    p.add_argument("--threads-file", default=None,
                   help=f"Path to golden_threads.json. Default: {DEFAULT_GOLDEN_PATH}")
    p.add_argument("--threads", default=None,
                   help="Comma-separated short ids (8-char prefix), one-shot override.")
    p.add_argument("-n", "--num", type=int, default=DEFAULT_GOLDEN_SIZE,
                   help="How many threads to auto-pick (when no golden set exists "
                        "and --threads not set).")
    p.add_argument("--init-golden-set", action="store_true",
                   help="Pick a diverse set, write the golden file, and exit "
                        "without replaying.")
    p.add_argument("--out", default=None,
                   help=f"Output root. Default: {DEFAULT_AUDIT_DIR}")
    args = p.parse_args()

    asyncio.run(_run(args))
    return 0


if __name__ == "__main__":
    sys.exit(main())
