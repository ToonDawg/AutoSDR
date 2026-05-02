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

import json
import logging
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from fastapi import APIRouter, File, Form, HTTPException, Query, UploadFile, status
from pydantic import ValidationError
from sqlalchemy import func, or_, select

from autosdr.api.deps import db_session, require_workspace
from autosdr.api.schemas import (
    ImportCommitOut,
    ImportPreviewColumn,
    ImportPreviewOut,
    ImportPreviewRow,
    ImportPreviewSkipReason,
    LeadEnrichCandidateOut,
    LeadEnrichIn,
    LeadEnrichOut,
    LeadListOut,
    LeadOptOutIn,
    LeadOut,
    MappingConfigIn,
)
from autosdr.enrichment import enrich_lead, is_social_website, persist_enrichment
from autosdr.importer import import_file, preview_import_file
from autosdr.models import CampaignLead, Lead
from autosdr.pipeline.priority import is_priority_lead, priority_reason

router = APIRouter(prefix="/api/leads", tags=["leads"])
logger = logging.getLogger(__name__)


def _lead_to_out(lead: Lead) -> LeadOut:
    """Build the public lead schema, computing the priority fields.

    The ORM-direct ``LeadOut.model_validate(lead, from_attributes=True)``
    path doesn't pick up derived signals because the predicates
    live in :mod:`autosdr.pipeline.priority` and
    :mod:`autosdr.enrichment` rather than on the :class:`Lead`
    model itself (keeps the model thin and the pipeline-side
    concern out of the schema layer). Folding the derivation into
    one helper keeps every ``LeadOut`` response — list page,
    detail, opt-out, clear-opt-out — in sync.

    Three derived fields:

    * ``is_priority`` / ``priority_reason`` — fires on either
      ``not_found`` or ``social_profile_website`` (precedence:
      ``not_found`` first; ticket 0014).
    * ``is_social_website`` — informational platform token
      (``"facebook"``, etc.) when ``Lead.website`` is itself a
      social-profile URL. Independent of priority so a 404'd
      Facebook URL still tags as ``"facebook"``.
    """

    out = LeadOut.model_validate(lead, from_attributes=True)
    out.is_priority = is_priority_lead(lead)
    out.priority_reason = priority_reason(lead)
    out.is_social_website = is_social_website(lead.website)
    return out


def _parse_mapping_config(raw: str | None) -> dict[str, Any] | None:
    """Parse the JSON-encoded ``mapping_config`` form field (ticket 0004).

    Returns ``None`` when omitted (default importer behaviour). Raises an
    HTTP 422 on invalid JSON or invalid shape — never silently falls back to
    defaults, which would mask operator typos.
    """

    if raw is None or raw == "":
        return None
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "error": "invalid_mapping_config",
                "message": f"mapping_config is not valid JSON: {exc.msg}",
            },
        )
    try:
        validated = MappingConfigIn.model_validate(parsed)
    except ValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "error": "invalid_mapping_config",
                "message": "mapping_config did not match the expected shape",
                "errors": exc.errors(),
            },
        )
    return validated.model_dump()


def _search_predicate(q: str | None):
    """Lower-cased ``LIKE`` over the four free-text columns the operator
    searches by. Returns a SQLAlchemy clause (or ``None`` for no filter)
    so callers don't repeat themselves across the page / counts queries.
    """

    if not q:
        return None
    needle = f"%{q.strip().lower()}%"
    return or_(
        func.lower(Lead.name).like(needle),
        func.lower(Lead.category).like(needle),
        func.lower(Lead.contact_uri).like(needle),
        func.lower(Lead.address).like(needle),
    )


def _assignment_predicate(assignment: str | None):
    """Filter by campaign-membership status.

    * ``"in_campaign"`` — at least one ``CampaignLead`` row.
    * ``"unassigned"`` — zero ``CampaignLead`` rows.
    * ``None`` / ``"all"`` — no filter (returns ``None``).
    """

    if not assignment or assignment == "all":
        return None
    in_any = (
        select(CampaignLead.lead_id)
        .where(CampaignLead.lead_id == Lead.id)
        .exists()
    )
    return in_any if assignment == "in_campaign" else ~in_any


@router.get("", response_model=LeadListOut)
def list_leads(
    status_filter: str | None = None,
    q: str | None = None,
    assignment: str | None = Query(None, pattern="^(all|in_campaign|unassigned)$"),
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
) -> LeadListOut:
    """Paginated lead listing for the Leads page.

    The server owns pagination, search, and per-status counts — the UI
    only knows what's currently on screen. ``counts_by_status`` always
    reflects the *narrowing* filters (search + assignment) but not the
    status filter itself, so the filter tabs stay accurate as the
    operator narrows.

    Pseudo-statuses surfaced in ``counts_by_status``:

    * ``do_not_contact`` — selects leads with
      ``do_not_contact_at IS NOT NULL`` regardless of underlying
      ``status``. A DNC lead can be ``new`` / ``contacted`` / ``lost``,
      so this is a cross-cutting count not a status bucket.

    Optional ``assignment`` query param narrows by campaign-membership:

    * ``"in_campaign"`` — leads assigned to at least one campaign.
    * ``"unassigned"`` — leads not yet assigned anywhere (typical for a
      fresh import).
    """

    with db_session() as session:
        workspace = require_workspace(session)

        scope = select(Lead).where(Lead.workspace_id == workspace.id)
        search_clause = _search_predicate(q)
        if search_clause is not None:
            scope = scope.where(search_clause)
        assignment_clause = _assignment_predicate(assignment)
        if assignment_clause is not None:
            scope = scope.where(assignment_clause)

        counts_rows = session.execute(
            scope.with_only_columns(Lead.status, func.count(Lead.id)).group_by(
                Lead.status
            )
        ).all()
        counts_by_status: dict[str, int] = {s: int(n) for s, n in counts_rows}
        counts_by_status["all"] = sum(counts_by_status.values())
        counts_by_status["do_not_contact"] = int(
            session.execute(
                scope.with_only_columns(func.count(Lead.id)).where(
                    Lead.do_not_contact_at.is_not(None)
                )
            ).scalar_one()
        )

        page = scope
        if status_filter == "do_not_contact":
            page = page.where(Lead.do_not_contact_at.is_not(None))
        elif status_filter and status_filter != "all":
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
            leads=[_lead_to_out(r) for r in rows],
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
async def preview_import(
    file: UploadFile = File(...),
    mapping_config: str | None = Form(default=None),
) -> ImportPreviewOut:
    """Parse the file but don't write anything to the DB.

    Returns a summary of what *would* happen so the operator can eyeball
    the skip reasons before committing.

    Optional ``mapping_config`` form field (JSON string, see ``MappingConfigIn``):
    when supplied, the preview's ``would_import`` count and column suggestions
    apply the same mapping the commit will use — preview and commit cannot
    drift (resolved OQ1, ticket 0004).
    """

    parsed_mapping = _parse_mapping_config(mapping_config)
    path = _save_upload(file)
    try:
        with db_session() as session:
            workspace = require_workspace(session)
            region_hint = (workspace.settings or {}).get("default_region", "AU")

        preview = preview_import_file(
            path=path,
            region_hint=region_hint,
            mapping_config=parsed_mapping,
        )

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
            columns=[
                ImportPreviewColumn(
                    name=col.name,
                    sample_values=col.sample_values,
                    suggested_target=col.suggested_target,
                    suggestion_confidence=col.suggestion_confidence,
                    suggestion_reason=col.suggestion_reason,
                )
                for col in preview.columns
            ],
            social_website_hosts=dict(preview.social_website_hosts),
        )
    finally:
        try:
            path.unlink(missing_ok=True)
        except Exception:
            pass


@router.post("/import/commit", response_model=ImportCommitOut)
async def commit_import(
    file: UploadFile = File(...),
    mapping_config: str | None = Form(default=None),
) -> ImportCommitOut:
    """Actually write the file into the lead table.

    Optional ``mapping_config`` form field (JSON string, see ``MappingConfigIn``):
    when supplied, operator overrides the alias-map guesses, drops noisy
    columns from ``raw_data``, and decides which source columns are kept
    only as raw context.
    """

    parsed_mapping = _parse_mapping_config(mapping_config)
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
                mapping_config=parsed_mapping,
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


# ---------------------------------------------------------------------------
# Enrichment warm-up + manual opt-out — static paths before ``/{lead_id}``
# ---------------------------------------------------------------------------


def _select_candidates_for_enrich(
    session,
    *,
    workspace_id: str,
    since_days: int,
    limit: int,
) -> list[Lead]:
    """Leads with a website that are missing enrichment or older than ``since_days``."""

    cutoff = datetime.now(tz=timezone.utc) - timedelta(days=since_days)
    stmt = (
        select(Lead)
        .where(
            Lead.workspace_id == workspace_id,
            Lead.do_not_contact_at.is_(None),
            Lead.website.isnot(None),
            or_(
                Lead.enrichment_fetched_at.is_(None),
                Lead.enrichment_fetched_at < cutoff,
            ),
        )
        .order_by(Lead.import_order.asc())
        .limit(limit)
    )
    return list(session.scalars(stmt).all())


def _lead_last_fetched_iso(lead: Lead) -> str | None:
    if lead.enrichment_fetched_at is None:
        return None
    return lead.enrichment_fetched_at.isoformat()


@router.post("/enrich", response_model=LeadEnrichOut)
async def post_enrich_batch(body: LeadEnrichIn) -> LeadEnrichOut:
    """Pre-fetch website enrichment for leads whose cache is stale or empty."""

    with db_session() as session:
        workspace = require_workspace(session)
        candidates = _select_candidates_for_enrich(
            session,
            workspace_id=workspace.id,
            since_days=body.since_days,
            limit=body.limit,
        )

        if body.dry_run:
            cand_out = [
                LeadEnrichCandidateOut(
                    lead_id=str(lead.id),
                    name=lead.name,
                    website=lead.website,
                    last_fetched=_lead_last_fetched_iso(lead),
                )
                for lead in candidates
            ]
            return LeadEnrichOut(
                ok=0,
                failed=0,
                total=len(candidates),
                dry_run=True,
                candidates=cand_out,
            )

        ok = 0
        fail = 0
        for lead in candidates:
            try:
                result = await enrich_lead(
                    website_url=lead.website,
                    budget_s=4.0,
                    respect_robots=True,
                )
            except Exception:
                fail += 1
                logger.exception("enrich warm-up failed for lead %s", lead.id)
                continue

            persist_enrichment(lead, result)
            session.commit()
            if result.status == "ok":
                ok += 1
            else:
                fail += 1

        return LeadEnrichOut(
            ok=ok,
            failed=fail,
            total=len(candidates),
            dry_run=False,
            candidates=None,
        )


@router.post("/{lead_id}/opt-out", response_model=LeadOut)
def opt_out_lead(
    lead_id: str,
    body: LeadOptOutIn | None = None,
) -> LeadOut:
    """Flag a lead as do-not-contact (same guard as deterministic STOP replies)."""

    payload = body or LeadOptOutIn()

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
        if lead.do_not_contact_at is None:
            reason = payload.reason.strip() if payload.reason else "manual"
            if not reason:
                reason = "manual"
            lead.do_not_contact_at = datetime.now(timezone.utc)
            lead.do_not_contact_reason = reason
            session.flush()
        return _lead_to_out(lead)


@router.delete("/{lead_id}/opt-out", response_model=LeadOut)
def clear_lead_opt_out(lead_id: str) -> LeadOut:
    """Clear manual do-not-contact (operator mistake recovery)."""

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
        if lead.do_not_contact_at is not None:
            lead.do_not_contact_at = None
            lead.do_not_contact_reason = None
            session.flush()
        return _lead_to_out(lead)


# Static sub-paths ``/import/*``, ``/enrich``, and ``/{lead_id}/opt-out`` are
# registered before ``GET /{lead_id}`` below.
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
        return _lead_to_out(lead)


__all__ = ["router"]
