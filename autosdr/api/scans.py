"""Lead-scan (website enrichment) browse + manual trigger routes."""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from fastapi import APIRouter, HTTPException, Query, status
from sqlalchemy import func, literal, or_, select

from autosdr.api.deps import db_session, require_workspace
from autosdr.api.schemas import (
    SCAN_STATUS_NEVER,
    ScanDetailOut,
    ScanListOut,
    ScanRowOut,
    ScanRunRequest,
    ScanRunResult,
    ScanSummaryOut,
)
from autosdr.models import CampaignLead, Lead, Workspace
from autosdr.pipeline.scans import get_scan_state_snapshot, scan_one_lead, start_scan, stop_scan

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/scans", tags=["scans"])


_KNOWN_STATUSES: tuple[str, ...] = (
    "ok",
    "blocked",
    "timeout",
    "error",
    "not_found",
    "empty_shell",
    "no_url",
    "killswitch_aborted",
    SCAN_STATUS_NEVER,
)


def _row_from_tuple(row: Any) -> ScanRowOut:
    """Hydrate one :class:`ScanRowOut` from a column-only result row.

    The list query selects scalar columns + portable JSON-path
    extracts (see :func:`_list_row_columns`) so we never round-trip
    the multi-KB ``raw_data`` blob just to render a table row.
    """

    return ScanRowOut(
        lead_id=row.lead_id,
        lead_name=row.lead_name,
        website=row.website,
        status=row.enrichment_status or SCAN_STATUS_NEVER,
        fetched_at=row.enrichment_fetched_at,
        latency_ms=_int_or_none(row.latency_ms_raw),
        cms=_str_or_none(row.cms_raw),
        sitemap_count=_int_or_none(row.sitemap_count_raw),
    )


def _list_row_columns():
    """Column expressions for the lean list-page select.

    Uses SQLAlchemy's portable JSON indexed access (``json_extract``
    on SQLite, ``->>`` on Postgres) so the row-level query never
    pulls ``raw_data`` itself off disk — only the three small JSON
    leaves the table actually displays.
    """

    enrichment = Lead.raw_data["enrichment"]
    return (
        Lead.id.label("lead_id"),
        Lead.name.label("lead_name"),
        Lead.website.label("website"),
        Lead.enrichment_status.label("enrichment_status"),
        Lead.enrichment_fetched_at.label("enrichment_fetched_at"),
        enrichment["_meta"]["latency_ms"].as_string().label("latency_ms_raw"),
        enrichment["signals"]["cms"].as_string().label("cms_raw"),
        enrichment["signals"]["sitemap_count"].as_string().label("sitemap_count_raw"),
    )


def _int_or_none(v: Any) -> int | None:
    if v is None:
        return None
    if isinstance(v, bool):
        return None
    if isinstance(v, int):
        return v
    if isinstance(v, str):
        cleaned = v.strip()
        if cleaned and cleaned.lstrip("-").isdigit():
            return int(cleaned)
    return None


def _str_or_none(v: Any) -> str | None:
    if isinstance(v, str) and v != "":
        return v
    return None


def _envelope_for(lead: Lead) -> dict[str, Any] | None:
    raw = lead.raw_data or {}
    blob = raw.get("enrichment")
    return blob if isinstance(blob, dict) else None


def _scoped_query(session, *, workspace_id: str, include_unassigned: bool):
    stmt = select(Lead).where(Lead.workspace_id == workspace_id)
    if not include_unassigned:
        assigned_subq = (
            select(CampaignLead.lead_id)
            .where(CampaignLead.lead_id == Lead.id)
            .exists()
        )
        stmt = stmt.where(assigned_subq)
    return stmt


def _apply_search(stmt, q: str | None):
    if not q:
        return stmt
    needle = f"%{q.strip().lower()}%"
    return stmt.where(
        or_(
            func.lower(Lead.name).like(needle),
            func.lower(Lead.website).like(needle),
        )
    )


def _snapshot_scan_summary(
    session,
    *,
    workspace: Workspace,
    include_unassigned: bool,
) -> ScanSummaryOut:
    status_expr = func.coalesce(
        Lead.enrichment_status, literal(SCAN_STATUS_NEVER)
    ).label("status")

    base = _scoped_query(
        session,
        workspace_id=workspace.id,
        include_unassigned=include_unassigned,
    )

    bucket_rows = session.execute(
        base.with_only_columns(status_expr, func.count(Lead.id)).group_by(status_expr)
    ).all()
    buckets: dict[str, int] = {bucket: int(n) for bucket, n in bucket_rows}

    last_run_at = session.execute(
        base.with_only_columns(func.max(Lead.enrichment_fetched_at))
    ).scalar_one_or_none()

    killswitch_abort = buckets.pop("killswitch_aborted", 0)
    error_bucket = killswitch_abort + buckets.get("error", 0)

    total_leads = _count_scannable(
        session, workspace=workspace, include_unassigned=include_unassigned
    )

    runner_snap = get_scan_state_snapshot()
    runner_started_at: datetime | None = None
    raw_rs = runner_snap.get("runner_started_at")
    if isinstance(raw_rs, str) and raw_rs:
        try:
            runner_started_at = datetime.fromisoformat(raw_rs.replace("Z", "+00:00"))
        except ValueError:
            runner_started_at = None

    return ScanSummaryOut(
        total_leads=total_leads,
        ok=buckets.get("ok", 0),
        blocked=buckets.get("blocked", 0),
        timeout=buckets.get("timeout", 0),
        error=error_bucket,
        not_found=buckets.get("not_found", 0),
        empty_shell=buckets.get("empty_shell", 0),
        no_url=buckets.get("no_url", 0),
        never_scanned=buckets.get(SCAN_STATUS_NEVER, 0),
        last_run_at=last_run_at,
        runner_running=bool(runner_snap.get("runner_running")),
        runner_total=int(runner_snap.get("runner_total") or 0),
        runner_done=int(runner_snap.get("runner_done") or 0),
        runner_ok=int(runner_snap.get("runner_ok") or 0),
        runner_failed=int(runner_snap.get("runner_failed") or 0),
        runner_started_at=runner_started_at,
    )


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.get("", response_model=ScanListOut)
def list_scans(
    status_filter: str | None = None,
    q: str | None = None,
    include_unassigned: bool = Query(False),
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
) -> ScanListOut:
    with db_session() as session:
        workspace = require_workspace(session)
        status_expr = func.coalesce(
            Lead.enrichment_status, literal(SCAN_STATUS_NEVER)
        ).label("status")

        base = _scoped_query(
            session,
            workspace_id=workspace.id,
            include_unassigned=include_unassigned,
        )
        base = _apply_search(base, q)

        counts_rows = session.execute(
            base.with_only_columns(status_expr, func.count(Lead.id)).group_by(
                status_expr
            )
        ).all()
        counts: dict[str, int] = {bucket: int(n) for bucket, n in counts_rows}
        counts["all"] = sum(counts.values())
        for bucket in _KNOWN_STATUSES:
            counts.setdefault(bucket, 0)

        page_stmt = base
        if status_filter and status_filter != "all":
            page_stmt = page_stmt.where(status_expr == status_filter)

        total = int(
            session.execute(
                select(func.count()).select_from(page_stmt.subquery())
            ).scalar_one()
        )

        # Lean column-only select: small scalars + three JSON-path
        # extracts. Never hydrates the ``raw_data`` blob — the list
        # payload was hauling multi-KB JSON per row before this.
        rows = session.execute(
            page_stmt.with_only_columns(*_list_row_columns())
            .order_by(
                Lead.enrichment_fetched_at.desc().nulls_last(),
                Lead.import_order.asc(),
            )
            .limit(limit)
            .offset(offset)
        ).all()

        return ScanListOut(
            scans=[_row_from_tuple(row) for row in rows],
            total=total,
            limit=limit,
            offset=offset,
            counts_by_status=counts,
        )


@router.get("/summary", response_model=ScanSummaryOut)
def scans_summary(include_unassigned: bool = Query(False)) -> ScanSummaryOut:
    with db_session() as session:
        workspace = require_workspace(session)
        return _snapshot_scan_summary(
            session,
            workspace=workspace,
            include_unassigned=include_unassigned,
        )


@router.post("/run", response_model=ScanRunResult)
async def run_scans(payload: ScanRunRequest) -> ScanRunResult:

    if payload.lead_id:
        with db_session() as session:
            workspace = require_workspace(session)
            lead = session.execute(
                select(Lead).where(
                    Lead.workspace_id == workspace.id, Lead.id == payload.lead_id
                )
            ).scalar_one_or_none()
            if lead is None:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail={"error": "lead_not_found", "lead_id": payload.lead_id},
                )

            sts = await scan_one_lead(session=session, lead=lead)
            logger.info(
                "scan manual lead=%s status=%s",
                lead.id,
                sts,
            )
            snap = _snapshot_scan_summary(
                session, workspace=workspace, include_unassigned=False
            )
            return ScanRunResult(
                **snap.model_dump(),
                started=True,
                lead_id=lead.id,
                status=sts,
            )

    if payload.enabled is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "error": "invalid_body",
                "message": "provide lead_id or enabled (bool)",
            },
        )

    started = False
    if payload.enabled:
        started = bool(start_scan())
    else:
        stop_scan()

    with db_session() as session:
        workspace = require_workspace(session)
        snap = _snapshot_scan_summary(
            session, workspace=workspace, include_unassigned=False
        )
        return ScanRunResult(**snap.model_dump(), started=started)


@router.get("/{lead_id}", response_model=ScanDetailOut)
def get_scan(lead_id: str) -> ScanDetailOut:
    with db_session() as session:
        workspace = require_workspace(session)
        lead = session.execute(
            select(Lead).where(
                Lead.workspace_id == workspace.id, Lead.id == lead_id
            )
        ).scalar_one_or_none()
        if lead is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={"error": "lead_not_found", "lead_id": lead_id},
            )

        bucket = lead.enrichment_status or SCAN_STATUS_NEVER

        return ScanDetailOut(
            lead_id=lead.id,
            lead_name=lead.name,
            website=lead.website,
            status=bucket,
            enrichment=_envelope_for(lead),
        )


def _scannable_filter(stmt, *, workspace_id: str, include_unassigned: bool):
    stmt = (
        stmt.where(Lead.workspace_id == workspace_id)
        .where(Lead.do_not_contact_at.is_(None))
        .where(Lead.website.isnot(None))
    )
    if not include_unassigned:
        stmt = stmt.where(
            select(CampaignLead.lead_id)
            .where(CampaignLead.lead_id == Lead.id)
            .exists()
        )
    return stmt


def _count_scannable(
    session,
    *,
    workspace: Workspace,
    include_unassigned: bool,
) -> int:
    stmt = _scannable_filter(
        select(func.count(Lead.id)),
        workspace_id=workspace.id,
        include_unassigned=include_unassigned,
    )
    return int(session.execute(stmt).scalar_one())


__all__ = ["router"]
