"""Golden replay harness for the evaluator.

Picks a diverse sample of historical threads, reconstructs the evaluator
inputs (tone / goal / angle / draft / category) from the DB, and re-runs
``complete_json`` with the CURRENT evaluator prompt code (whatever
``evaluation.PROMPT_VERSION`` currently is). Prints the OLD stored
``response_parsed`` next to the FRESH response so you can eyeball the
behaviour delta of a prompt change.

Use this immediately after every evaluator-prompt edit. It is the
counterpart to ``scripts/llm_call_metrics.py`` — that tells you what
the population stats look like after a deploy; this tells you whether
specific known-tricky threads still get scored the way you expect.

Caveats:

- The evaluator's job is to score a draft; the draft itself is held
  constant from the historical run, so any score delta is attributable
  to the prompt change (modulo Flash-Lite non-determinism, which we
  pin via ``temperature=0`` for the eval call).
- Replays still hit the live LLM and will incur a small cost (~$0.001
  per thread on Flash-Lite). Default sample is six.

Usage::

    .venv/bin/python scripts/replay_evaluator.py
    .venv/bin/python scripts/replay_evaluator.py --threads <id1>,<id2>
    .venv/bin/python scripts/replay_evaluator.py --apply   # persist llm_call rows
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from autosdr.config import get_settings
from autosdr.db import session_scope
from autosdr.llm import LlmCallContext, complete_json
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
from autosdr.prompts import evaluation


@dataclass
class _Frozen:
    """Inputs to ``evaluation.build_user_prompt`` reconstructed from the DB."""

    thread_id: str
    lead_short_id: str
    lead_name: str
    lead_category: str | None
    campaign_goal: str
    angle: str
    draft: str
    tone_snapshot: str | None
    workspace_id: str
    campaign_id: str
    lead_id: str
    historical: dict[str, Any] | None
    historical_prompt_version: str | None


# ---------------------------------------------------------------------------
# Selection
# ---------------------------------------------------------------------------


def _pick_default_threads(session: Session, *, limit: int = 6) -> list[str]:
    """Return a roughly-diverse sample of thread ids that have a sent draft.

    Tries to mix pass / fail and a few angle_types so the user sees
    interesting behaviour, not just "they all pass". Falls back to
    most-recent if the join can't yield enough variety.
    """

    # Threads with a sent AI message AND a stored eval response.
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
    chosen_set: set[str] = set()
    for tid, angle_type, parsed in session.execute(stmt).all():
        if not isinstance(parsed, dict):
            continue
        passed = bool(parsed.get("pass") is True)
        key = ((angle_type or "(unknown)"), passed)
        if key in seen_pair or tid in chosen_set:
            continue
        seen_pair[key] = tid
        chosen_set.add(tid)

    chosen = list(seen_pair.values())[:limit]
    if len(chosen) < limit:
        # Top up with most-recent threads we haven't picked yet.
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
    return chosen


def _freeze_inputs(session: Session, thread_id: str) -> _Frozen | None:
    """Reconstruct the evaluator inputs for ``thread_id``.

    Returns ``None`` if the thread has no sent AI draft yet, since the
    evaluator only ran on drafts the system actually produced.
    """

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

    # The draft is the most-recent AI message on this thread (we score what
    # actually shipped). For threads that escalated to HITL with no send,
    # fall back to the last AI draft persisted in hitl_context.last_drafts.
    draft_msg = (
        session.query(Message)
        .filter(Message.thread_id == thread.id)
        .filter(Message.role == MessageRole.AI)
        .order_by(Message.created_at.asc())
        .first()
    )
    if draft_msg is not None:
        draft = draft_msg.content
    else:
        ctx = thread.hitl_context or {}
        last_drafts = ctx.get("last_drafts") or []
        if not last_drafts:
            return None
        draft = str(last_drafts[-1])

    historical = (
        session.query(LlmCall)
        .filter(LlmCall.thread_id == thread.id)
        .filter(LlmCall.purpose == LlmCallPurpose.EVALUATION)
        .filter(LlmCall.error.is_(None))
        .order_by(LlmCall.created_at.desc())
        .first()
    )

    return _Frozen(
        thread_id=thread.id,
        lead_short_id=lead.id[:8],
        lead_name=lead.name or "(unnamed)",
        lead_category=lead.category,
        campaign_goal=campaign.goal,
        angle=thread.angle or "",
        draft=draft,
        tone_snapshot=thread.tone_snapshot,
        workspace_id=workspace.id,
        campaign_id=campaign.id,
        lead_id=lead.id,
        historical=historical.response_parsed if historical else None,
        historical_prompt_version=historical.prompt_version if historical else None,
    )


# ---------------------------------------------------------------------------
# Replay
# ---------------------------------------------------------------------------


async def _replay_one(frozen: _Frozen, *, model: str, persist: bool) -> dict[str, Any]:
    """Re-run the evaluator on ``frozen`` using the live LLM.

    Returns a dict with both old and new score views so the caller can
    diff them.
    """

    system = evaluation.build_system_prompt()
    user = evaluation.build_user_prompt(
        tone_snapshot=frozen.tone_snapshot,
        campaign_goal=frozen.campaign_goal,
        angle=frozen.angle,
        draft=frozen.draft,
        lead_category=frozen.lead_category,
    )

    if not persist:
        # Disable DB / JSONL logging for this dry replay only.
        os.environ["LLM_LOG_ENABLED"] = "false"
        get_settings().llm_log_enabled = False

    context = LlmCallContext(
        purpose=LlmCallPurpose.EVALUATION,
        workspace_id=frozen.workspace_id,
        campaign_id=frozen.campaign_id,
        thread_id=frozen.thread_id if persist else None,
        lead_id=frozen.lead_id,
    )
    parsed, result = await complete_json(
        system=system,
        user=user,
        model=model,
        prompt_version=evaluation.PROMPT_VERSION,
        temperature=0.0,
        context=context,
    )
    normalised = evaluation.evaluate_result(parsed, draft=frozen.draft)

    return {
        "thread_id": frozen.thread_id,
        "lead_short_id": frozen.lead_short_id,
        "lead_name": frozen.lead_name,
        "category": frozen.lead_category,
        "draft_chars": len(frozen.draft),
        "draft": frozen.draft,
        "angle_chars": len(frozen.angle),
        "tone_chars": len(frozen.tone_snapshot or ""),
        "user_prompt_chars": len(user),
        "system_prompt_chars": len(system),
        "tokens_in": result.tokens_in,
        "tokens_out": result.tokens_out,
        "latency_ms": result.latency_ms,
        "old": {
            "prompt_version": frozen.historical_prompt_version,
            "scores": (frozen.historical or {}).get("scores"),
            "overall": (frozen.historical or {}).get("overall"),
            "pass": (frozen.historical or {}).get("pass"),
            "feedback": (frozen.historical or {}).get("feedback"),
        },
        "new": {
            "prompt_version": evaluation.PROMPT_VERSION,
            "scores": normalised["scores"],
            "overall": normalised["overall"],
            "pass": normalised["pass"],
            "feedback": normalised["feedback"],
        },
    }


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------


def _diff_summary(replay: dict[str, Any]) -> str:
    old = replay["old"]
    new = replay["new"]
    old_pass = old.get("pass")
    new_pass = new.get("pass")
    old_overall = old.get("overall")
    new_overall = new.get("overall")
    flip = ""
    if old_pass is not None and old_pass != new_pass:
        flip = "  ⇒ PASS FLIPPED"
    delta = ""
    if isinstance(old_overall, (int, float)) and isinstance(new_overall, (int, float)):
        delta = f"  Δoverall={new_overall - old_overall:+.3f}"
    return (
        f"old: pass={old_pass} overall={old_overall} ({old.get('prompt_version')})\n"
        f"new: pass={new_pass} overall={new_overall} ({new.get('prompt_version')})"
        f"{delta}{flip}"
    )


def _print_replay(r: dict[str, Any], *, full: bool) -> None:
    print("\n" + "=" * 100)
    print(
        f"thread {r['thread_id'][:8]}  lead {r['lead_short_id']} {r['lead_name']!r}"
        f"  category={r['category']!r}"
    )
    print(
        f"  draft_chars={r['draft_chars']}  angle_chars={r['angle_chars']}  "
        f"tone_chars={r['tone_chars']}  prompt_chars={r['system_prompt_chars']}+"
        f"{r['user_prompt_chars']}  tokens_in={r['tokens_in']}  "
        f"tokens_out={r['tokens_out']}  latency_ms={r['latency_ms']}"
    )
    print(_diff_summary(r))
    if full:
        print(f"\n  draft   : {r['draft']}")
        print(f"\n  old.scores: {json.dumps(r['old'].get('scores'), default=str)}")
        print(f"  new.scores: {json.dumps(r['new']['scores'], default=str)}")
        print(f"\n  old.feedback: {r['old'].get('feedback')!r}")
        print(f"  new.feedback: {r['new']['feedback']!r}")


async def _run(
    *,
    thread_short_ids: list[str] | None,
    n: int,
    apply_persist: bool,
    full: bool,
    model_override: str | None,
) -> None:
    settings = get_settings()
    settings.llm_log_enabled = True if apply_persist else False

    with session_scope() as session:
        workspace = session.query(Workspace).first()
        if workspace is None:
            raise SystemExit("no workspace — run `autosdr init` first")
        apply_llm_provider_keys(workspace.settings or {})
        eval_model = model_override or (workspace.settings or {}).get("llm", {}).get(
            "model_eval", "gemini/gemini-3.1-flash-lite-preview"
        )
        print(f"replaying evaluator with model={eval_model}, "
              f"persist={apply_persist}, prompt_version={evaluation.PROMPT_VERSION}")

        if thread_short_ids:
            chosen: list[str] = []
            for short in thread_short_ids:
                short = short.strip()
                if not short:
                    continue
                row = (
                    session.query(Thread.id)
                    .filter(Thread.id.like(f"{short}%"))
                    .first()
                )
                if row is None:
                    print(f"!! thread not found: {short}")
                    continue
                chosen.append(row[0])
        else:
            chosen = _pick_default_threads(session, limit=n)

        if not chosen:
            raise SystemExit("no threads selected")

        frozen: list[_Frozen] = []
        for tid in chosen:
            f = _freeze_inputs(session, tid)
            if f is None:
                print(f"!! could not freeze inputs for thread {tid[:8]} — skipping")
                continue
            frozen.append(f)

    if not frozen:
        raise SystemExit("no threads frozen — nothing to replay")

    replays: list[dict[str, Any]] = []
    flips = 0
    deltas: list[float] = []
    for f in frozen:
        try:
            r = await _replay_one(f, model=eval_model, persist=apply_persist)
        except Exception as exc:
            print(f"!! replay failed for thread {f.thread_id[:8]}: "
                  f"{type(exc).__name__}: {exc}")
            continue
        replays.append(r)
        _print_replay(r, full=full)

        old_pass = (r["old"] or {}).get("pass")
        new_pass = r["new"]["pass"]
        if old_pass is not None and bool(old_pass) != bool(new_pass):
            flips += 1
        old_overall = (r["old"] or {}).get("overall")
        new_overall = r["new"]["overall"]
        if isinstance(old_overall, (int, float)) and isinstance(new_overall, (int, float)):
            deltas.append(float(new_overall) - float(old_overall))

    print("\n" + "=" * 100)
    print(
        f"REPLAY SUMMARY  threads={len(replays)}  pass_flips={flips}"
        + (
            f"  Δoverall: avg={sum(deltas) / len(deltas):+.3f}  "
            f"min={min(deltas):+.3f}  max={max(deltas):+.3f}"
            if deltas
            else ""
        )
    )

    out_path = "data/replay-evaluator-results.json"
    with open(out_path, "w", encoding="utf-8") as fh:
        json.dump(replays, fh, indent=2, ensure_ascii=False, default=str)
    print(f"full payload -> {out_path}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--threads",
        default=None,
        help="Comma-separated short ids (8-char prefix). Default: auto-pick a diverse sample.",
    )
    parser.add_argument(
        "-n",
        "--num",
        type=int,
        default=6,
        help="Number of threads to auto-pick (ignored when --threads is set).",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Persist the new llm_call rows to the DB. Default is dry-run (no DB writes).",
    )
    parser.add_argument(
        "--full",
        action="store_true",
        help="Print the full draft + scores + feedback for each replay.",
    )
    parser.add_argument(
        "--model",
        default=None,
        help="Override the eval model (default: workspace.settings.llm.model_eval).",
    )
    args = parser.parse_args()

    thread_short_ids = (
        [s for s in args.threads.split(",") if s.strip()] if args.threads else None
    )
    asyncio.run(
        _run(
            thread_short_ids=thread_short_ids,
            n=args.num,
            apply_persist=args.apply,
            full=args.full,
            model_override=args.model,
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
