"""Slice metrics from the ``llm_call`` table — the regression harness.

Reports per-``prompt_version`` cuts of the metrics that matter when you
ship a new prompt: tokens, cost, latency, pass-rate, attempts-per-send,
HITL escalations. Read-only; never modifies state.

Use this BEFORE and AFTER any prompt change as the lightweight A/B
signal — pin a window with ``--since`` to compare the latest version
against the previous one in the same conditions.

Usage::

    .venv/bin/python scripts/llm_call_metrics.py
    .venv/bin/python scripts/llm_call_metrics.py --since 2026-04-25
    .venv/bin/python scripts/llm_call_metrics.py --purpose evaluation
    .venv/bin/python scripts/llm_call_metrics.py --json    # machine-readable
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from autosdr.db import session_scope
from autosdr.models import LlmCall, LlmCallPurpose, Message, Thread, ThreadStatus


# ---------------------------------------------------------------------------
# Aggregation helpers
# ---------------------------------------------------------------------------


def _percentile(values: list[float], pct: float) -> float | None:
    """Cheap percentile — sorts and indexes. Good enough for ad-hoc reports."""

    if not values:
        return None
    pct = max(0.0, min(100.0, pct))
    s = sorted(values)
    if pct == 100:
        return float(s[-1])
    rank = (pct / 100) * (len(s) - 1)
    lo = int(rank)
    hi = min(lo + 1, len(s) - 1)
    frac = rank - lo
    return float(s[lo] + (s[hi] - s[lo]) * frac)


def _per_version_call_stats(
    session: Session, *, since: datetime | None, purpose: str | None
) -> list[dict[str, Any]]:
    """One row per (purpose, prompt_version, model) with token / cost / latency."""

    stmt = select(
        LlmCall.purpose,
        LlmCall.prompt_version,
        LlmCall.model,
        func.count().label("calls"),
        func.sum(LlmCall.tokens_in).label("sum_tin"),
        func.sum(LlmCall.tokens_out).label("sum_tout"),
        func.avg(LlmCall.tokens_in).label("avg_tin"),
        func.avg(LlmCall.tokens_out).label("avg_tout"),
        func.avg(LlmCall.latency_ms).label("avg_latency_ms"),
        func.sum(LlmCall.cost_usd).label("sum_cost"),
        func.count(LlmCall.error).label("err_count"),
    ).group_by(LlmCall.purpose, LlmCall.prompt_version, LlmCall.model)

    if since is not None:
        stmt = stmt.where(LlmCall.created_at >= since)
    if purpose is not None:
        stmt = stmt.where(LlmCall.purpose == purpose)

    stmt = stmt.order_by(
        LlmCall.purpose, LlmCall.prompt_version, LlmCall.model
    )

    rows = []
    for r in session.execute(stmt).all():
        calls = int(r.calls or 0)
        rows.append(
            {
                "purpose": r.purpose,
                "prompt_version": r.prompt_version or "(null)",
                "model": r.model,
                "calls": calls,
                "errors": int(r.err_count or 0),
                "tokens_in_avg": int(r.avg_tin or 0),
                "tokens_in_sum": int(r.sum_tin or 0),
                "tokens_out_avg": int(r.avg_tout or 0),
                "latency_ms_avg": int(r.avg_latency_ms or 0),
                "cost_usd_total": round(float(r.sum_cost or 0.0), 4),
            }
        )
    return rows


def _eval_pass_rates(
    session: Session, *, since: datetime | None
) -> list[dict[str, Any]]:
    """Pass-rate per evaluation prompt_version.

    Reads ``response_parsed.pass`` on rows where the eval call succeeded.
    Pass-rate is a pre-derivation signal — the wrapper recomputes ``pass``
    in code from ``length_valid`` and ``overall >= threshold``, but for the
    purposes of detecting prompt regressions, the raw model output is the
    cleaner signal.
    """

    stmt = (
        select(
            LlmCall.prompt_version,
            LlmCall.response_parsed,
        )
        .where(LlmCall.purpose == LlmCallPurpose.EVALUATION)
        .where(LlmCall.error.is_(None))
        .where(LlmCall.response_parsed.is_not(None))
    )
    if since is not None:
        stmt = stmt.where(LlmCall.created_at >= since)

    by_version: dict[str, dict[str, Any]] = {}
    for prompt_version, parsed in session.execute(stmt).all():
        bucket = by_version.setdefault(
            prompt_version or "(null)",
            {"total": 0, "passed": 0, "overall": []},
        )
        bucket["total"] += 1
        if not isinstance(parsed, dict):
            continue
        if parsed.get("pass") is True:
            bucket["passed"] += 1
        try:
            bucket["overall"].append(float(parsed.get("overall") or 0.0))
        except (TypeError, ValueError):
            pass

    out: list[dict[str, Any]] = []
    for version, b in sorted(by_version.items()):
        total = b["total"]
        passed = b["passed"]
        overall = b["overall"]
        out.append(
            {
                "prompt_version": version,
                "evals": total,
                "passed": passed,
                "pass_rate": round(passed / total, 3) if total else None,
                "overall_p50": round(_percentile(overall, 50) or 0.0, 3),
                "overall_p10": round(_percentile(overall, 10) or 0.0, 3),
                "overall_p90": round(_percentile(overall, 90) or 0.0, 3),
            }
        )
    return out


def _attempts_per_send(
    session: Session, *, since: datetime | None
) -> dict[str, Any]:
    """For each thread, how many evaluator attempts before a send (or HITL)?

    Counts rows from the ``llm_call`` table where ``purpose=evaluation`` per
    thread. Cross-references ``thread.status`` so HITL-paused threads are
    reported separately. Eval attempts are the canonical "did the loop have
    to retry" signal — three attempts on the same thread = the gen drafted
    something the eval rejected twice.
    """

    eval_stmt = (
        select(
            LlmCall.thread_id,
            func.count().label("evals"),
            func.max(LlmCall.prompt_version).label("any_eval_version"),
        )
        .where(LlmCall.purpose == LlmCallPurpose.EVALUATION)
        .where(LlmCall.thread_id.is_not(None))
        .group_by(LlmCall.thread_id)
    )
    if since is not None:
        eval_stmt = eval_stmt.where(LlmCall.created_at >= since)

    rows = list(session.execute(eval_stmt).all())
    if not rows:
        return {
            "threads_with_evals": 0,
            "attempts_avg": None,
            "attempts_p50": None,
            "attempts_p90": None,
            "by_eval_version": [],
            "hitl_paused": 0,
        }

    thread_ids = [r.thread_id for r in rows]
    statuses = {
        tid: status
        for tid, status in session.execute(
            select(Thread.id, Thread.status).where(Thread.id.in_(thread_ids))
        ).all()
    }

    attempts_all: list[int] = []
    by_version: dict[str, list[int]] = {}
    hitl_paused = 0
    for r in rows:
        attempts_all.append(int(r.evals))
        by_version.setdefault(r.any_eval_version or "(null)", []).append(int(r.evals))
        if statuses.get(r.thread_id) == ThreadStatus.PAUSED_FOR_HITL:
            hitl_paused += 1

    return {
        "threads_with_evals": len(attempts_all),
        "attempts_avg": round(sum(attempts_all) / len(attempts_all), 3),
        "attempts_p50": _percentile([float(v) for v in attempts_all], 50),
        "attempts_p90": _percentile([float(v) for v in attempts_all], 90),
        "hitl_paused": hitl_paused,
        "by_eval_version": [
            {
                "eval_version": v,
                "threads": len(vals),
                "attempts_avg": round(sum(vals) / len(vals), 3),
                "attempts_p90": _percentile([float(x) for x in vals], 90),
            }
            for v, vals in sorted(by_version.items())
        ],
    }


def _send_economics(
    session: Session, *, since: datetime | None
) -> dict[str, Any]:
    """$/sent-message and $/HITL-thread, by eval prompt_version.

    A sent message is a row in ``message`` with ``role=ai`` (the AI draft
    actually shipped). Cost is summed across all llm_call rows on the same
    thread. The eval prompt_version is the version that gated the send —
    if the eval was non-deterministic across attempts within the same
    thread, we attribute to the most-recent.
    """

    sent_stmt = (
        select(Message.thread_id)
        .where(Message.role == "ai")
        .distinct()
    )
    if since is not None:
        sent_stmt = sent_stmt.where(Message.created_at >= since)
    sent_thread_ids = {tid for (tid,) in session.execute(sent_stmt).all()}

    if not sent_thread_ids:
        return {"sent_threads": 0, "by_eval_version": []}

    cost_stmt = (
        select(
            LlmCall.thread_id,
            func.sum(LlmCall.cost_usd).label("cost"),
            func.sum(LlmCall.tokens_in).label("tin"),
            func.sum(LlmCall.tokens_out).label("tout"),
        )
        .where(LlmCall.thread_id.in_(sent_thread_ids))
        .group_by(LlmCall.thread_id)
    )
    if since is not None:
        cost_stmt = cost_stmt.where(LlmCall.created_at >= since)
    thread_costs = {
        tid: {"cost": float(c or 0.0), "tin": int(tin or 0), "tout": int(tout or 0)}
        for tid, c, tin, tout in session.execute(cost_stmt).all()
    }

    eval_stmt = (
        select(LlmCall.thread_id, LlmCall.prompt_version, LlmCall.created_at)
        .where(LlmCall.purpose == LlmCallPurpose.EVALUATION)
        .where(LlmCall.thread_id.in_(sent_thread_ids))
        .order_by(LlmCall.thread_id, LlmCall.created_at.desc())
    )
    if since is not None:
        eval_stmt = eval_stmt.where(LlmCall.created_at >= since)

    last_eval_version: dict[str, str] = {}
    for tid, version, _ in session.execute(eval_stmt).all():
        last_eval_version.setdefault(tid, version or "(null)")

    by_version: dict[str, dict[str, Any]] = {}
    for tid in sent_thread_ids:
        version = last_eval_version.get(tid, "(unknown)")
        c = thread_costs.get(tid, {"cost": 0.0, "tin": 0, "tout": 0})
        bucket = by_version.setdefault(
            version, {"threads": 0, "cost": 0.0, "tin": 0, "tout": 0}
        )
        bucket["threads"] += 1
        bucket["cost"] += c["cost"]
        bucket["tin"] += c["tin"]
        bucket["tout"] += c["tout"]

    out_rows: list[dict[str, Any]] = []
    for version, b in sorted(by_version.items()):
        threads = b["threads"]
        out_rows.append(
            {
                "eval_version": version,
                "sent_threads": threads,
                "cost_per_thread": round(b["cost"] / threads, 4) if threads else None,
                "tokens_in_per_thread": int(b["tin"] / threads) if threads else 0,
                "tokens_out_per_thread": int(b["tout"] / threads) if threads else 0,
                "total_cost_usd": round(b["cost"], 4),
            }
        )

    return {"sent_threads": len(sent_thread_ids), "by_eval_version": out_rows}


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------


def _format_table(rows: list[dict[str, Any]], columns: list[tuple[str, str]]) -> str:
    """Tiny tabulator. ``columns`` is a list of (key, header).

    Numeric values right-align, strings left-align. Returns a single string.
    """

    if not rows:
        return "(no rows)"

    widths = {key: len(header) for key, header in columns}
    for row in rows:
        for key, _ in columns:
            v = row.get(key, "")
            widths[key] = max(widths[key], len(str(v) if v is not None else "—"))

    def render_cell(key: str, value: Any) -> str:
        text = "—" if value is None else str(value)
        right_align = isinstance(value, (int, float)) and not isinstance(value, bool)
        return text.rjust(widths[key]) if right_align else text.ljust(widths[key])

    header = "  ".join(h.ljust(widths[k]) for k, h in columns)
    sep = "  ".join("-" * widths[k] for k, _ in columns)
    body = "\n".join(
        "  ".join(render_cell(k, row.get(k)) for k, _ in columns) for row in rows
    )
    return f"{header}\n{sep}\n{body}"


def _print_human(out: dict[str, Any]) -> None:
    print(f"=== llm_call metrics  (window: {out['window']})  ===\n")

    print("--- per-call stats by purpose × prompt_version × model ---")
    print(_format_table(
        out["per_call_stats"],
        columns=[
            ("purpose", "purpose"),
            ("prompt_version", "version"),
            ("model", "model"),
            ("calls", "calls"),
            ("errors", "errs"),
            ("tokens_in_avg", "in_avg"),
            ("tokens_out_avg", "out_avg"),
            ("latency_ms_avg", "lat_ms"),
            ("cost_usd_total", "cost_usd"),
        ],
    ))

    print("\n--- evaluator pass-rate by prompt_version ---")
    print(_format_table(
        out["eval_pass_rates"],
        columns=[
            ("prompt_version", "version"),
            ("evals", "evals"),
            ("passed", "passed"),
            ("pass_rate", "pass_rate"),
            ("overall_p10", "p10"),
            ("overall_p50", "p50"),
            ("overall_p90", "p90"),
        ],
    ))

    a = out["attempts_per_send"]
    print(
        "\n--- attempts per send  ("
        f"threads_with_evals={a['threads_with_evals']}  "
        f"hitl_paused={a['hitl_paused']}  "
        f"avg={a['attempts_avg']}  p50={a['attempts_p50']}  "
        f"p90={a['attempts_p90']}) ---"
    )
    print(_format_table(
        a["by_eval_version"],
        columns=[
            ("eval_version", "version"),
            ("threads", "threads"),
            ("attempts_avg", "avg"),
            ("attempts_p90", "p90"),
        ],
    ))

    e = out["send_economics"]
    print(
        f"\n--- send economics  (sent_threads={e['sent_threads']}) ---"
    )
    print(_format_table(
        e["by_eval_version"],
        columns=[
            ("eval_version", "version"),
            ("sent_threads", "threads"),
            ("cost_per_thread", "$/thread"),
            ("tokens_in_per_thread", "tin/thread"),
            ("tokens_out_per_thread", "tout/thread"),
            ("total_cost_usd", "total_$"),
        ],
    ))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--since",
        default=None,
        help="ISO date or datetime (e.g. 2026-04-25). Default: all rows.",
    )
    parser.add_argument(
        "--purpose",
        default=None,
        choices=("analysis", "generation", "evaluation", "classification", "other"),
        help="Restrict per-call stats to one purpose.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit a single JSON document instead of human tables.",
    )
    args = parser.parse_args()

    since: datetime | None = None
    if args.since:
        try:
            since = datetime.fromisoformat(args.since)
            if since.tzinfo is None:
                since = since.replace(tzinfo=timezone.utc)
        except ValueError:
            print(f"!! invalid --since value: {args.since!r}", file=sys.stderr)
            return 2

    with session_scope() as session:
        out = {
            "window": (since.isoformat() if since else "all-time") + " → now",
            "per_call_stats": _per_version_call_stats(
                session, since=since, purpose=args.purpose
            ),
            "eval_pass_rates": _eval_pass_rates(session, since=since),
            "attempts_per_send": _attempts_per_send(session, since=since),
            "send_economics": _send_economics(session, since=since),
        }

    if args.json:
        json.dump(out, sys.stdout, indent=2, default=str)
        sys.stdout.write("\n")
    else:
        _print_human(out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
