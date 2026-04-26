"""CSV / NDJSON lead importer.

The POC uses a fixed column schema — the field-mapping agent from Doc 2 is a
v1 feature. Core fields are mapped by exact name (case-insensitive); everything
else is preserved in ``lead.raw_data`` so the LLM can use it as personalisation
context.

Core fields (all optional except ``phone``):

    name | category | address | website | phone | notes

Phone numbers are normalised to E.164 and classified as mobile / landline /
toll-free / unknown. Non-mobile contacts are imported with ``status='skipped'``
and a clear ``skip_reason`` — SMS won't reach them but the record is preserved
so the owner can override manually.
"""

from __future__ import annotations

import csv
import json
import logging
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

import phonenumbers
from sqlalchemy import select
from sqlalchemy.orm import Session

from autosdr.models import (
    ContactType,
    ImportJob,
    ImportJobStatus,
    Lead,
    LeadStatus,
    next_import_order,
)

logger = logging.getLogger(__name__)


_CORE_FIELDS = {"name", "category", "address", "website", "phone"}
_CORE_ALIASES = {
    # map of aliases -> canonical field name
    "business_name": "name",
    "biz_name": "name",
    "company": "name",
    "company_name": "name",
    "contact_name": "name",
    "business_type": "category",
    "industry": "category",
    "location": "address",
    "url": "website",
    "web": "website",
    "phone_number": "phone",
    "mobile": "phone",
    "tel": "phone",
    "telephone": "phone",
}


@dataclass
class ImportRowResult:
    action: str  # 'inserted' | 'updated' | 'skipped' | 'error'
    reason: str | None = None
    lead_id: str | None = None


@dataclass
class ImportSummary:
    job_id: str
    row_count: int = 0
    imported_count: int = 0
    updated_count: int = 0
    skipped_count: int = 0
    error_count: int = 0
    errors: list[dict[str, Any]] = field(default_factory=list)

    def record(self, row_number: int, outcome: ImportRowResult) -> None:
        if outcome.action == "inserted":
            self.imported_count += 1
        elif outcome.action == "updated":
            self.updated_count += 1
            self.imported_count += 1  # count as imported per spec
        elif outcome.action == "skipped":
            self.skipped_count += 1
            if outcome.reason:
                self.errors.append({"row": row_number, "reason": outcome.reason})
        elif outcome.action == "error":
            self.error_count += 1
            if outcome.reason:
                self.errors.append({"row": row_number, "reason": outcome.reason})


# ---------------------------------------------------------------------------
# Phone normalisation
# ---------------------------------------------------------------------------


_TYPE_MAP = {
    phonenumbers.PhoneNumberType.MOBILE: ContactType.MOBILE,
    phonenumbers.PhoneNumberType.FIXED_LINE: ContactType.LANDLINE,
    phonenumbers.PhoneNumberType.FIXED_LINE_OR_MOBILE: ContactType.UNKNOWN,
    phonenumbers.PhoneNumberType.TOLL_FREE: ContactType.TOLL_FREE,
    phonenumbers.PhoneNumberType.PREMIUM_RATE: ContactType.TOLL_FREE,
    phonenumbers.PhoneNumberType.SHARED_COST: ContactType.TOLL_FREE,
}


def normalise_phone(raw: str | None, region_hint: str = "AU") -> tuple[str | None, str]:
    """Return (E.164 string, contact_type).

    Returns ``(None, 'unknown')`` if the value cannot be parsed.
    """

    if not raw:
        return None, ContactType.UNKNOWN

    cleaned = str(raw).strip()
    if not cleaned:
        return None, ContactType.UNKNOWN

    try:
        parsed = phonenumbers.parse(cleaned, region_hint)
    except phonenumbers.NumberParseException:
        return None, ContactType.UNKNOWN

    if not phonenumbers.is_valid_number(parsed):
        return None, ContactType.UNKNOWN

    e164 = phonenumbers.format_number(parsed, phonenumbers.PhoneNumberFormat.E164)
    number_type = phonenumbers.number_type(parsed)
    contact_type = _TYPE_MAP.get(number_type, ContactType.UNKNOWN)
    return e164, contact_type


# ---------------------------------------------------------------------------
# File parsing
# ---------------------------------------------------------------------------


def _detect_file_type(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix == ".csv":
        return "csv"
    if suffix in {".json", ".jsonl", ".ndjson"}:
        return "json"
    raise ValueError(
        f"Unsupported file extension {suffix!r}; expected .csv, .json, .jsonl, or .ndjson"
    )


def _iter_csv(path: Path) -> Iterable[dict[str, Any]]:
    with path.open("r", encoding="utf-8-sig", newline="") as fh:
        reader = csv.DictReader(fh)
        for row in reader:
            yield {k: v for k, v in row.items() if v not in (None, "")}


def _iter_ndjson(path: Path) -> Iterable[dict[str, Any]]:
    """Read either NDJSON (one object per line) or a JSON array."""

    text = path.read_text(encoding="utf-8")
    stripped = text.lstrip()
    if stripped.startswith("["):
        data = json.loads(stripped)
        if not isinstance(data, list):
            raise ValueError("JSON root must be an array or newline-delimited objects")
        yield from (obj for obj in data if isinstance(obj, dict))
        return

    for line_num, line in enumerate(text.splitlines(), start=1):
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"line {line_num}: invalid JSON ({exc.msg})") from exc
        if isinstance(obj, dict):
            yield obj


# ---------------------------------------------------------------------------
# Field mapping (POC: exact-match only, with a few common aliases)
# ---------------------------------------------------------------------------


def _canonical_key(key: str) -> str:
    return key.strip().lower().replace(" ", "_").replace("-", "_")


def _split_core_and_raw(row: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """Partition a row into core-field values + raw_data blob.

    Note: core fields are NOT stripped from raw_data; the LLM uses raw_data
    as its primary context, so it needs the full source record.
    """

    core: dict[str, Any] = {}
    raw: dict[str, Any] = dict(row)

    for key, value in row.items():
        if value in (None, ""):
            continue
        canon = _canonical_key(key)
        target = _CORE_ALIASES.get(canon, canon if canon in _CORE_FIELDS else None)
        if target is not None and target not in core:
            core[target] = value

    return core, raw


# ---------------------------------------------------------------------------
# Row processing
# ---------------------------------------------------------------------------


def _merge_raw_data(existing: dict[str, Any], incoming: dict[str, Any]) -> dict[str, Any]:
    """Key-level merge: new keys added, existing keys overwritten, none deleted."""

    merged = dict(existing)
    merged.update(incoming)
    return merged


def _process_row(
    *,
    session: Session,
    workspace_id: str,
    source_file: str,
    region_hint: str,
    row: dict[str, Any],
) -> ImportRowResult:
    core, raw = _split_core_and_raw(row)

    raw_phone = core.get("phone")
    if not raw_phone:
        return ImportRowResult("skipped", reason="no_contact_uri")

    e164, contact_type = normalise_phone(raw_phone, region_hint=region_hint)
    if not e164:
        return ImportRowResult("skipped", reason="invalid_phone_format")

    # Look up existing lead for this contact_uri in this workspace.
    existing = session.execute(
        select(Lead).where(
            Lead.workspace_id == workspace_id, Lead.contact_uri == e164
        )
    ).scalar_one_or_none()

    # Default status + skip_reason based on contact_type.
    is_mobile = contact_type == ContactType.MOBILE
    default_status = LeadStatus.NEW if is_mobile else LeadStatus.SKIPPED
    default_skip_reason = None if is_mobile else f"not_a_mobile_number:{contact_type}"

    if existing is None:
        lead = Lead(
            workspace_id=workspace_id,
            name=core.get("name"),
            contact_uri=e164,
            contact_type=contact_type,
            category=core.get("category"),
            address=core.get("address"),
            website=core.get("website"),
            raw_data=raw,
            import_order=next_import_order(session, workspace_id),
            source_file=source_file,
            status=default_status,
            skip_reason=default_skip_reason,
        )
        session.add(lead)
        session.flush()
        return ImportRowResult("inserted", lead_id=lead.id)

    # Merge logic per Doc 2 §5: non-null core fields don't overwrite existing
    # non-null values. raw_data merges at the key level.
    changed = False
    for field_name in ("name", "category", "address", "website"):
        incoming = core.get(field_name)
        if incoming is None:
            continue
        if getattr(existing, field_name) in (None, ""):
            setattr(existing, field_name, incoming)
            changed = True

    merged_raw = _merge_raw_data(existing.raw_data or {}, raw)
    if merged_raw != (existing.raw_data or {}):
        existing.raw_data = merged_raw
        changed = True

    # Refine contact_type when we now know it more precisely, and reconcile
    # status + skip_reason for leads that have never been engaged. Without
    # this, a lead initially classified MOBILE (or UNKNOWN) that turns out
    # to be a LANDLINE/TOLL_FREE could still sit at ``status=new`` and get
    # assigned to a campaign — we'd then text a landline in production.
    # Only touch pre-engagement states (``new`` and ``skipped``); never
    # clobber ``contacted``/``replied``/``won``/``lost``. Leads flagged
    # ``do_not_contact`` are also exempt — Spam Act 2003 / TCPA require we
    # honour the opt-out across re-imports, so we never reset their status
    # or skip_reason.
    if (
        existing.contact_type != contact_type
        and contact_type != ContactType.UNKNOWN
        and existing.do_not_contact_at is None
    ):
        existing.contact_type = contact_type
        changed = True

        if existing.status in (LeadStatus.NEW, LeadStatus.SKIPPED):
            if is_mobile:
                # Promote skipped-for-non-mobile back into the queue.
                was_non_mobile_skip = (
                    existing.status == LeadStatus.SKIPPED
                    and (existing.skip_reason or "").startswith("not_a_mobile_number")
                )
                if was_non_mobile_skip:
                    existing.status = LeadStatus.NEW
                    existing.skip_reason = None
            else:
                # Demote queued leads whose number turned out to be non-mobile.
                if existing.status == LeadStatus.NEW:
                    existing.status = LeadStatus.SKIPPED
                    existing.skip_reason = default_skip_reason
                elif existing.skip_reason != default_skip_reason:
                    existing.skip_reason = default_skip_reason
    elif existing.contact_type != contact_type and contact_type != ContactType.UNKNOWN:
        # DNC lead: still record the refined contact_type for accurate UI,
        # but do not touch ``status`` or ``skip_reason``.
        existing.contact_type = contact_type
        changed = True

    if changed:
        session.flush()
        return ImportRowResult("updated", lead_id=existing.id)
    return ImportRowResult("skipped", reason="duplicate_no_new_data", lead_id=existing.id)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def import_file(
    *,
    session: Session,
    workspace_id: str,
    path: Path,
    region_hint: str = "AU",
) -> ImportSummary:
    """Import a CSV or NDJSON file into the lead table.

    Commits are the caller's responsibility — the function uses ``session.flush``
    throughout so the caller can wrap the whole import in a transaction.
    """

    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(path)

    file_type = _detect_file_type(path)
    job = ImportJob(
        workspace_id=workspace_id,
        filename=path.name,
        file_type=file_type,
        status=ImportJobStatus.PROCESSING,
    )
    session.add(job)
    session.flush()

    summary = ImportSummary(job_id=job.id)

    iterator = _iter_csv(path) if file_type == "csv" else _iter_ndjson(path)

    for row_number, row in enumerate(iterator, start=1):
        summary.row_count += 1
        try:
            outcome = _process_row(
                session=session,
                workspace_id=workspace_id,
                source_file=path.name,
                region_hint=region_hint,
                row=row,
            )
        except Exception as exc:  # defensive: one bad row does not kill the import
            logger.exception("import row %d failed", row_number)
            outcome = ImportRowResult("error", reason=str(exc))
        summary.record(row_number, outcome)

    job.row_count = summary.row_count
    job.imported_count = summary.imported_count
    job.skipped_count = summary.skipped_count
    job.error_count = summary.error_count
    job.errors = summary.errors
    job.status = ImportJobStatus.COMPLETE
    session.flush()

    return summary


# ---------------------------------------------------------------------------
# Preview: parse the file and report what *would* happen, without touching the DB.
# ---------------------------------------------------------------------------

_PREVIEW_SAMPLE_LIMIT = 20


@dataclass
class PreviewRow:
    """One row as it would be classified on commit."""

    name: str | None
    phone: str | None
    normalised_phone: str | None
    contact_type: str
    skip_reason: str | None


@dataclass
class ImportPreview:
    """The shape the ``POST /api/leads/import/preview`` endpoint returns.

    ``would_skip`` is ordered by frequency descending so the endpoint can
    render it directly. ``sample`` is capped at the first ``_PREVIEW_SAMPLE_LIMIT``
    rows — enough to eyeball without blowing up big-file previews.
    """

    file_type: str
    total_rows: int
    would_import: int
    would_skip: list[tuple[str, int]]
    sample: list[PreviewRow]


def preview_import_file(*, path: Path, region_hint: str = "AU") -> ImportPreview:
    """Dry-run version of :func:`import_file`.

    The web preview endpoint used to reach directly into ``_detect_file_type``,
    ``_iter_csv``/``_iter_ndjson``, and ``_split_core_and_raw``. Exposing one
    helper here means the parsing rules, alias resolution, and phone
    normalisation stay in a single module; callers just get a summary.
    """

    path = Path(path)
    file_type = _detect_file_type(path)
    iterator = _iter_csv(path) if file_type == "csv" else _iter_ndjson(path)

    total = 0
    would_import = 0
    skip_counter: Counter[str] = Counter()
    sample: list[PreviewRow] = []

    for row in iterator:
        total += 1
        core, _raw = _split_core_and_raw(row)
        raw_phone = core.get("phone")

        if not raw_phone:
            skip_counter["no_contact_uri"] += 1
            if len(sample) < _PREVIEW_SAMPLE_LIMIT:
                sample.append(
                    PreviewRow(
                        name=core.get("name"),
                        phone=None,
                        normalised_phone=None,
                        contact_type=ContactType.UNKNOWN,
                        skip_reason="no_contact_uri",
                    )
                )
            continue

        e164, contact_type = normalise_phone(raw_phone, region_hint=region_hint)
        if not e164:
            skip_reason: str | None = "invalid_phone_format"
            skip_counter[skip_reason] += 1
        elif contact_type != ContactType.MOBILE:
            skip_reason = f"not_a_mobile_number:{contact_type}"
            skip_counter[skip_reason] += 1
        else:
            would_import += 1
            skip_reason = None

        if len(sample) < _PREVIEW_SAMPLE_LIMIT:
            sample.append(
                PreviewRow(
                    name=core.get("name"),
                    phone=str(raw_phone),
                    normalised_phone=e164,
                    contact_type=contact_type,
                    skip_reason=skip_reason,
                )
            )

    return ImportPreview(
        file_type=file_type,
        total_rows=total,
        would_import=would_import,
        would_skip=list(skip_counter.most_common()),
        sample=sample,
    )
