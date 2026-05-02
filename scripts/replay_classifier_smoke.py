"""Smoke test for classification reasoning-budget cap (Phase 4 #13).

Replays the N most recent classification calls from the ``llm_call``
table against the *current* `classification.PROMPT_VERSION` with
``reasoning_effort`` set to whatever ``settings.llm.reasoning_classification``
says (default ``"low"``). Prints OLD vs NEW intent / confidence / latency
/ tokens so you can eyeball whether capping the budget changed the
answer or just made it cheaper.

Caveats:
- Hits the live LLM (Gemini Flash-Lite by default). Cost is sub-cent per
  classification at production volume.
- Default is ``--apply false`` — the new ``llm_call`` rows are NOT
  persisted unless you opt in. The replay just reads.
- We can't replay against historical settings, so the OLD column is the
  stored historical row and the NEW column is whatever the current code
  + settings produce.

Usage::

    .venv/bin/python scripts/replay_classifier_smoke.py
    .venv/bin/python scripts/replay_classifier_smoke.py --limit 10
    .venv/bin/python scripts/replay_classifier_smoke.py --override disable
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from sqlalchemy import select

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
from autosdr.prompts import classification


async def main(*, limit: int, override: str | None) -> int:
    settings = get_settings()

    with session_scope() as session:
        ws_settings = (
            session.query(Workspace).order_by(Workspace.created_at.asc()).first()
        )
        if ws_settings is None:
            print("No workspace found.", file=sys.stderr)
            return 1
        apply_llm_provider_keys(ws_settings.settings or {})
        llm_settings = (ws_settings.settings or {}).get("llm") or {}

        rows = (
            session.execute(
                select(LlmCall)
                .where(LlmCall.purpose == LlmCallPurpose.CLASSIFICATION)
                .order_by(LlmCall.created_at.desc())
                .limit(limit)
            )
            .scalars()
            .all()
        )

        if not rows:
            print("No classification calls in llm_call.", file=sys.stderr)
            return 1

        cases: list[dict] = []
        for row in rows:
            thread = (
                session.query(Thread).filter(Thread.id == row.thread_id).one_or_none()
                if row.thread_id
                else None
            )
            if thread is None:
                continue
            cl = session.get(CampaignLead, thread.campaign_lead_id)
            campaign = session.get(Campaign, cl.campaign_id) if cl else None
            lead = session.get(Lead, cl.lead_id) if cl else None
            if campaign is None or lead is None:
                continue

            history = (
                session.execute(
                    select(Message)
                    .where(Message.thread_id == thread.id)
                    .order_by(Message.created_at.asc())
                )
                .scalars()
                .all()
            )
            if not history:
                continue
            last_inbound = next(
                (m for m in reversed(history) if m.role == MessageRole.LEAD),
                None,
            )
            if last_inbound is None:
                continue
            prior = [m for m in history if m.created_at < last_inbound.created_at]
            cases.append(
                {
                    "row": row,
                    "campaign": campaign,
                    "lead": lead,
                    "workspace_id": ws_settings.id,
                    "thread_id": thread.id,
                    "history": [
                        {"role": m.role, "content": m.content} for m in prior
                    ],
                    "incoming_content": last_inbound.content,
                }
            )

    if not cases:
        print("Found classification rows but none have replayable thread state.", file=sys.stderr)
        return 1

    reasoning = override or llm_settings.get("reasoning_classification") or "disable"
    model = llm_settings.get("model_classification") or llm_settings["model_main"]
    print()
    print(f"# Classification reasoning smoke")
    print(f"# model:             {model}")
    print(f"# reasoning_effort:  {reasoning}")
    print(f"# prompt_version:    {classification.PROMPT_VERSION}")
    print(f"# replays:           {len(cases)}")
    print()

    flips = 0
    drops_tokens = 0
    drops_latency = 0
    for case in cases:
        row = case["row"]
        old_parsed = row.response_parsed or {}
        old_intent = old_parsed.get("intent")
        old_conf = old_parsed.get("confidence")

        cls_raw, result = await complete_json(
            system=classification.build_system_prompt(),
            user=classification.build_user_prompt(
                campaign_goal=case["campaign"].goal,
                history=case["history"],
                incoming_message=case["incoming_content"],
            ),
            model=model,
            prompt_version=classification.PROMPT_VERSION,
            temperature=0.0,
            reasoning_effort=reasoning,
            context=LlmCallContext(
                purpose=LlmCallPurpose.CLASSIFICATION,
                workspace_id=case["workspace_id"],
                campaign_id=case["campaign"].id,
                thread_id=case["thread_id"],
                lead_id=case["lead"].id,
            ),
        )
        cls = classification.normalise_classification(cls_raw)
        new_intent = cls["intent"]
        new_conf = cls["confidence"]

        flipped = old_intent != new_intent
        if flipped:
            flips += 1
        d_tokens = (row.tokens_out or 0) - result.tokens_out
        d_latency = (row.latency_ms or 0) - result.latency_ms
        if d_tokens > 0:
            drops_tokens += 1
        if d_latency > 0:
            drops_latency += 1

        marker = "⚠ FLIP" if flipped else "  ok  "
        print(
            f"{marker} thread={case['thread_id'][:8]} "
            f"OLD intent={old_intent:<14} conf={old_conf:.2f} "
            f"tokens_out={row.tokens_out:>3} latency={row.latency_ms:>5}ms"
        )
        print(
            f"        "
            f"NEW intent={new_intent:<14} conf={new_conf:.2f} "
            f"tokens_out={result.tokens_out:>3} latency={result.latency_ms:>5}ms"
            f"  Δtokens={-d_tokens:+d} Δlatency={-d_latency:+d}ms"
        )
        if cls.get("reason"):
            print(f"        reason: {cls['reason'][:160]}")
        print()

    print(f"Summary: {flips}/{len(cases)} intent flips; "
          f"tokens_out dropped on {drops_tokens}/{len(cases)}; "
          f"latency dropped on {drops_latency}/{len(cases)}.")
    return 0


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--limit", type=int, default=5,
                   help="Replay the N most recent classification calls.")
    p.add_argument("--override", choices=("disable", "low", "medium", "high"),
                   default=None,
                   help="Override the workspace's reasoning_classification "
                        "setting for this run (does not persist).")
    args = p.parse_args()
    raise SystemExit(asyncio.run(main(limit=args.limit, override=args.override)))
