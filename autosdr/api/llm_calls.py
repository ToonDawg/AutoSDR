"""LLM call log browsing.

Powers the Logs page — the same data ``autosdr logs llm`` shows in the
CLI, just pageable.

Per ticket 0006, ``cost_usd`` is computed at serialisation time from
:func:`autosdr.llm.pricing.cost_for` rather than persisted on the row.
This means a pricing-table edit retroactively reprices all historical
rows; the trade-off is documented on the ticket and surfaced as
"estimated cost" in the UI.
"""

from __future__ import annotations

from fastapi import APIRouter
from sqlalchemy import func, select

from autosdr.api.deps import db_session, require_workspace
from autosdr.api.schemas import LlmCallOut, LlmCallsSummaryOut
from autosdr.llm.pricing import cost_for
from autosdr.models import LlmCall

router = APIRouter(prefix="/api/llm-calls", tags=["llm_calls"])


def _to_out(row: LlmCall) -> LlmCallOut:
    out = LlmCallOut.model_validate(row, from_attributes=True)
    out.cost_usd = cost_for(row.model, row.tokens_in, row.tokens_out)
    return out


def _apply_filters(
    stmt,
    *,
    thread_id: str | None,
    campaign_id: str | None,
    lead_id: str | None,
    purpose: str | None,
    errors_only: bool,
):
    if thread_id:
        stmt = stmt.where(LlmCall.thread_id == thread_id)
    if campaign_id:
        stmt = stmt.where(LlmCall.campaign_id == campaign_id)
    if lead_id:
        stmt = stmt.where(LlmCall.lead_id == lead_id)
    if purpose:
        stmt = stmt.where(LlmCall.purpose == purpose)
    if errors_only:
        stmt = stmt.where(LlmCall.error.is_not(None))
    return stmt


@router.get("", response_model=list[LlmCallOut])
def list_llm_calls(
    thread_id: str | None = None,
    campaign_id: str | None = None,
    lead_id: str | None = None,
    purpose: str | None = None,
    limit: int = 100,
    errors_only: bool = False,
) -> list[LlmCallOut]:
    limit = max(1, min(int(limit), 500))
    with db_session() as session:
        workspace = require_workspace(session)
        stmt = (
            select(LlmCall)
            .where(LlmCall.workspace_id == workspace.id)
            .order_by(LlmCall.created_at.desc())
            .limit(limit)
        )
        stmt = _apply_filters(
            stmt,
            thread_id=thread_id,
            campaign_id=campaign_id,
            lead_id=lead_id,
            purpose=purpose,
            errors_only=errors_only,
        )
        rows = list(session.execute(stmt).scalars())
        return [_to_out(r) for r in rows]


@router.get("/summary", response_model=LlmCallsSummaryOut)
def llm_calls_summary(
    thread_id: str | None = None,
    campaign_id: str | None = None,
    lead_id: str | None = None,
    purpose: str | None = None,
    errors_only: bool = False,
) -> LlmCallsSummaryOut:
    """Aggregate every LLM call in the workspace into a single cost figure.

    Powers the "total spend" stat above the Logs table. The list endpoint
    caps at 500 rows for UI virtualisation, so summing the visible rows
    on the client would silently underreport for any workspace older
    than a couple of days. Aggregating server-side avoids that.

    Cost is computed by reducing ``(model, tokens_in, tokens_out)``
    triples through :func:`cost_for` — same code path as the per-row
    ``cost_usd`` so the total always matches the rows. Rows whose model
    has no rate card contribute zero (and bump the
    ``unpriced_calls`` counter so the UI can surface the gap).
    """

    with db_session() as session:
        workspace = require_workspace(session)
        stmt = (
            select(
                LlmCall.model,
                func.coalesce(func.sum(LlmCall.tokens_in), 0),
                func.coalesce(func.sum(LlmCall.tokens_out), 0),
                func.count(LlmCall.id),
            )
            .where(LlmCall.workspace_id == workspace.id)
            .group_by(LlmCall.model)
        )
        stmt = _apply_filters(
            stmt,
            thread_id=thread_id,
            campaign_id=campaign_id,
            lead_id=lead_id,
            purpose=purpose,
            errors_only=errors_only,
        )

        total_cost = 0.0
        total_calls = 0
        total_tokens_in = 0
        total_tokens_out = 0
        unpriced_calls = 0
        for model, t_in, t_out, n in session.execute(stmt).all():
            n = int(n or 0)
            t_in = int(t_in or 0)
            t_out = int(t_out or 0)
            total_calls += n
            total_tokens_in += t_in
            total_tokens_out += t_out
            row_cost = cost_for(model, t_in, t_out)
            if row_cost is None:
                unpriced_calls += n
            else:
                total_cost += row_cost

    return LlmCallsSummaryOut(
        total_calls=total_calls,
        total_tokens_in=total_tokens_in,
        total_tokens_out=total_tokens_out,
        total_cost_usd=round(total_cost, 6),
        unpriced_calls=unpriced_calls,
    )


__all__ = ["router"]
