"""LLM call log browsing.

Powers the Logs page — the same data ``autosdr logs llm`` shows in the
CLI, just pageable.

Cost is now persisted at write time from LiteLLM's ``response_cost`` and
stored on ``llm_call.cost_usd`` so list/summary endpoints can aggregate
without replaying a pricing table. Legacy rows created before this column
was introduced may still have ``NULL`` cost; those are surfaced via
``unpriced_calls`` in the summary response.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter
from sqlalchemy import case, func, select

from autosdr.api.deps import db_session, require_workspace
from autosdr.api.schemas import LlmCallOut, LlmCallsSummaryOut
from autosdr.models import LlmCall

router = APIRouter(prefix="/api/llm-calls", tags=["llm_calls"])


def _to_out(row: LlmCall) -> LlmCallOut:
    return LlmCallOut.model_validate(row, from_attributes=True)


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

    Cost is reduced from the persisted ``llm_call.cost_usd`` field.
    ``unpriced_calls`` counts legacy rows where ``cost_usd`` is ``NULL``
    (pre-migration data).
    """

    with db_session() as session:
        workspace = require_workspace(session)
        stmt = (
            select(
                func.coalesce(func.sum(LlmCall.tokens_in), 0),
                func.coalesce(func.sum(LlmCall.tokens_out), 0),
                func.coalesce(func.sum(LlmCall.cost_usd), 0.0),
                func.coalesce(
                    func.sum(
                        case((LlmCall.cost_usd.is_(None), 1), else_=0)
                    ),
                    0,
                ),
                func.count(LlmCall.id),
            )
            .where(LlmCall.workspace_id == workspace.id)
        )
        stmt = _apply_filters(
            stmt,
            thread_id=thread_id,
            campaign_id=campaign_id,
            lead_id=lead_id,
            purpose=purpose,
            errors_only=errors_only,
        )

        row = session.execute(stmt).one()
        total_tokens_in = int(row[0] or 0)
        total_tokens_out = int(row[1] or 0)
        total_cost = float(row[2] or 0.0)
        unpriced_calls = int(row[3] or 0)
        total_calls = int(row[4] or 0)

    return LlmCallsSummaryOut(
        total_calls=total_calls,
        total_tokens_in=total_tokens_in,
        total_tokens_out=total_tokens_out,
        total_cost_usd=round(total_cost, 6),
        unpriced_calls=unpriced_calls,
    )


@router.get("/cost-by-purpose", response_model=dict[str, dict[str, Any]])
def cost_by_purpose(
    errors_only: bool = False,
) -> dict[str, dict[str, Any]]:
    """Aggregate persisted LLM costs by prompt purpose.

    Returns totals for each purpose bucket (ANALYSIS, GENERATION,
    EVALUATION, CLASSIFICATION) across the active workspace.
    """

    with db_session() as session:
        workspace = require_workspace(session)
        stmt = (
            select(
                LlmCall.purpose,
                func.count(LlmCall.id),
                func.coalesce(func.sum(LlmCall.tokens_in), 0),
                func.coalesce(func.sum(LlmCall.tokens_out), 0),
                func.coalesce(func.sum(LlmCall.cost_usd), 0.0),
                func.coalesce(
                    func.sum(
                        case((LlmCall.cost_usd.is_(None), 1), else_=0)
                    ),
                    0,
                ),
            )
            .where(LlmCall.workspace_id == workspace.id)
            .group_by(LlmCall.purpose)
        )
        if errors_only:
            stmt = stmt.where(LlmCall.error.is_not(None))

        out: dict[str, dict[str, Any]] = {}
        for purpose, total_calls, tokens_in, tokens_out, total_cost, unpriced in session.execute(
            stmt
        ).all():
            out[str(purpose)] = {
                "total_calls": int(total_calls or 0),
                "total_tokens_in": int(tokens_in or 0),
                "total_tokens_out": int(tokens_out or 0),
                "total_cost_usd": round(float(total_cost or 0.0), 6),
                "unpriced_calls": int(unpriced or 0),
            }
        return out


__all__ = ["router"]
