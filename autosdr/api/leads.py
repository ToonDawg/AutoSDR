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

from fastapi import APIRouter, File, HTTPException, UploadFile, status
from sqlalchemy import select

from autosdr.api.deps import db_session, require_workspace
from autosdr.api.schemas import (
    ImportCommitOut,
    ImportPreviewOut,
    ImportPreviewRow,
    ImportPreviewSkipReason,
    LeadOut,
)
from autosdr.importer import import_file, preview_import_file
from autosdr.models import Lead

router = APIRouter(prefix="/api/leads", tags=["leads"])


@router.get("", response_model=list[LeadOut])
def list_leads(
    status_filter: str | None = None,
    limit: int = 500,
) -> list[LeadOut]:
    limit = max(1, min(int(limit), 5000))
    with db_session() as session:
        workspace = require_workspace(session)
        stmt = (
            select(Lead)
            .where(Lead.workspace_id == workspace.id)
            .order_by(Lead.import_order.asc())
            .limit(limit)
        )
        if status_filter:
            stmt = stmt.where(Lead.status == status_filter)
        rows = list(session.execute(stmt).scalars())
        return [LeadOut.model_validate(r, from_attributes=True) for r in rows]


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


__all__ = ["router"]
