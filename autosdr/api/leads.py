"""Lead list + CSV/NDJSON import.

Two kinds of work happen here:

* ``GET /api/leads`` — paginated list for the Leads page, with optional
  status / campaign filters.
* ``POST /api/leads/import/preview`` + ``POST /api/leads/import/commit`` —
  the two-step upload flow: preview first so the operator can confirm the
  skip reasons, then commit.

We lean on ``autosdr.importer.import_file`` for the actual parsing, so the
web UI and the old CLI path behave identically.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from fastapi import APIRouter, File, HTTPException, Query, UploadFile, status
from sqlalchemy import func, or_, select

from autosdr.api.deps import db_session, require_workspace
from autosdr.api.schemas import (
    ImportCommitOut,
    ImportPreviewOut,
    ImportPreviewRow,
    ImportPreviewSkipReason,
    LeadListOut,
    LeadOut,
)
from autosdr.importer import import_file, preview_import_file
from autosdr.models import Lead

router = APIRouter(prefix="/api/leads", tags=["leads"])


@router.get("", response_model=LeadListOut)
def list_leads(
    status_filter: str | None = None,
    q: str | None = None,
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
) -> LeadListOut:
    """Paginated lead listing for the Leads page.

    A single regional scrape can produce tens of thousands of rows, so
    the server owns pagination, search, and per-status counts — the UI
    only knows what's currently on screen. ``counts_by_status`` always
    reflects the *filtered* set (i.e. search is applied, status filter
    is not), so the filter tabs stay accurate as the operator searches.
    """

    with db_session() as session:
        workspace = require_workspace(session)

        base = select(Lead).where(Lead.workspace_id == workspace.id)
        if q:
            needle = f"%{q.strip().lower()}%"
            base = base.where(
                or_(
                    func.lower(Lead.name).like(needle),
                    func.lower(Lead.category).like(needle),
                    func.lower(Lead.contact_uri).like(needle),
                    func.lower(Lead.address).like(needle),
                )
            )

        counts_stmt = select(Lead.status, func.count(Lead.id)).where(
            Lead.workspace_id == workspace.id
        )
        if q:
            needle = f"%{q.strip().lower()}%"
            counts_stmt = counts_stmt.where(
                or_(
                    func.lower(Lead.name).like(needle),
                    func.lower(Lead.category).like(needle),
                    func.lower(Lead.contact_uri).like(needle),
                    func.lower(Lead.address).like(needle),
                )
            )
        counts_rows = list(session.execute(counts_stmt.group_by(Lead.status)))
        counts_by_status: dict[str, int] = {s: int(n) for s, n in counts_rows}
        counts_by_status["all"] = sum(counts_by_status.values())

        page = base
        if status_filter and status_filter != "all":
            page = page.where(Lead.status == status_filter)

        total = int(
            session.execute(
                select(func.count()).select_from(page.subquery())
            ).scalar_one()
        )

        rows = list(
            session.execute(
                page.order_by(Lead.import_order.asc()).limit(limit).offset(offset)
            ).scalars()
        )
        return LeadListOut(
            leads=[LeadOut.model_validate(r, from_attributes=True) for r in rows],
            total=total,
            limit=limit,
            offset=offset,
            counts_by_status=counts_by_status,
        )


def _save_upload(upload: UploadFile) -> Path:
    """Persist the upload to a temp file so the importer can seek it."""

    suffix = Path(upload.filename or "upload.csv").suffix or ".csv"
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=suffix)
    try:
        data = upload.file.read()
        tmp.write(data)
        tmp.flush()
    finally:
        tmp.close()
    return Path(tmp.name)


@router.post("/import/preview", response_model=ImportPreviewOut)
async def preview_import(file: UploadFile = File(...)) -> ImportPreviewOut:
    """Parse the file but don't write anything to the DB.

    Returns a summary of what *would* happen so the operator can eyeball
    the skip reasons before committing.
    """

    path = _save_upload(file)
    try:
        with db_session() as session:
            workspace = require_workspace(session)
            region_hint = (workspace.settings or {}).get("default_region", "AU")

        preview = preview_import_file(path=path, region_hint=region_hint)

        return ImportPreviewOut(
            filename=file.filename or path.name,
            file_type=preview.file_type,
            total_rows=preview.total_rows,
            would_import=preview.would_import,
            would_skip=[
                ImportPreviewSkipReason(reason=reason, count=count)
                for reason, count in preview.would_skip
            ],
            sample=[
                ImportPreviewRow(
                    name=row.name,
                    phone=row.phone,
                    normalised_phone=row.normalised_phone,
                    contact_type=row.contact_type,
                    skip_reason=row.skip_reason,
                )
                for row in preview.sample
            ],
        )
    finally:
        try:
            path.unlink(missing_ok=True)
        except Exception:
            pass


@router.post("/import/commit", response_model=ImportCommitOut)
async def commit_import(file: UploadFile = File(...)) -> ImportCommitOut:
    """Actually write the file into the lead table."""

    path = _save_upload(file)
    try:
        with db_session() as session:
            workspace = require_workspace(session)
            region_hint = (workspace.settings or {}).get("default_region", "AU")
            summary = import_file(
                session=session,
                workspace_id=workspace.id,
                path=path,
                region_hint=region_hint,
            )
            return ImportCommitOut(
                job_id=summary.job_id,
                row_count=summary.row_count,
                imported_count=summary.imported_count,
                skipped_count=summary.skipped_count,
                error_count=summary.error_count,
                errors=summary.errors,
            )
    except FileNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"error": "file_not_found", "message": str(exc)},
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"error": "invalid_payload", "message": str(exc)},
        )
    finally:
        try:
            path.unlink(missing_ok=True)
        except Exception:
            pass


# Defined after the import routes so ``/api/leads/import/preview`` and
# ``/api/leads/import/commit`` take priority over the ``/{lead_id}`` match.
@router.get("/{lead_id}", response_model=LeadOut)
def get_lead(lead_id: str) -> LeadOut:
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
        return LeadOut.model_validate(lead, from_attributes=True)


__all__ = ["router"]
