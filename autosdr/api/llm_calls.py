"""LLM call log browsing.

Powers the Logs page — the same data ``autosdr logs llm`` shows in the
CLI, just pageable.
"""

from __future__ import annotations

from fastapi import APIRouter
from sqlalchemy import select

from autosdr.api.deps import db_session, require_workspace
from autosdr.api.schemas import LlmCallOut
from autosdr.models import LlmCall

router = APIRouter(prefix="/api/llm-calls", tags=["llm_calls"])


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
        rows = list(session.execute(stmt).scalars())
        return [LlmCallOut.model_validate(r, from_attributes=True) for r in rows]


__all__ = ["router"]
