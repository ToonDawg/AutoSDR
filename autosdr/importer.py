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
import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

import phonenumbers
from sqlalchemy import select
from sqlalchemy.orm import Session

from autosdr.enrichment import is_social_website
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
# Field mapping
# ---------------------------------------------------------------------------


def _canonical_key(key: str) -> str:
    return key.strip().lower().replace(" ", "_").replace("-", "_")


def _normalise_mapping_config(
    mapping_config: dict[str, Any] | None,
) -> tuple[dict[str, str], set[str], set[str]]:
    """Pull the three sub-fields out of an operator-supplied mapping config.

    Returns ``(canonical_to_source, drop_from_raw, include_in_raw_only)``.

    - ``canonical_to_source`` maps a canonical core field (``phone``, ``name``…)
      to the *source* column name in the row that should fill it. It overrides
      the alias map; it does **not** override an exact same-name match (e.g. an
      explicit ``"phone": "phone"`` is just the identity).
    - ``drop_from_raw`` is the set of source column names that must NOT be
      copied into ``raw_data``. They are filtered out of the incoming payload
      only — never retroactively pruned from existing ``raw_data`` (resolved
      OQ3, ticket 0004).
    - ``include_in_raw_only`` is the set of source column names the operator
      explicitly opted into ``raw_data`` only. These are *never* promoted to a
      core field even if the column name would otherwise alias-match — e.g. an
      operator who wants ``phone`` ignored as a contact URI and kept only as
      reference text.
    """

    if not mapping_config:
        return {}, set(), set()

    raw_mapping = mapping_config.get("mapping") or {}
    drop = mapping_config.get("drop_from_raw") or []
    include_only = mapping_config.get("include_in_raw_only") or []

    canonical_to_source: dict[str, str] = {}
    if isinstance(raw_mapping, dict):
        for canonical, source in raw_mapping.items():
            if canonical in _CORE_FIELDS and isinstance(source, str) and source:
                canonical_to_source[canonical] = source

    drop_set: set[str] = {s for s in drop if isinstance(s, str)}
    include_only_set: set[str] = {s for s in include_only if isinstance(s, str)}

    return canonical_to_source, drop_set, include_only_set


def _split_core_and_raw(
    row: dict[str, Any],
    mapping_config: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Partition a row into core-field values + raw_data blob.

    Without a ``mapping_config``, behaviour is unchanged: core fields are
    matched by exact lowercase name or via the ``_CORE_ALIASES`` table; every
    incoming key is preserved in ``raw_data`` (the LLM uses raw_data as its
    primary context, so it sees the full source record).

    With a ``mapping_config``:

    - Operator-supplied ``mapping`` (canonical → source column name) wins over
      the alias map. Operator can both correct a bad guess (``"phone":
      "mobile_e164"``) and explicitly bind the same name (``"phone": "phone"``).
    - ``include_in_raw_only`` columns are forced to stay out of ``core`` even
      if their name would alias-match. They still land in ``raw_data``.
    - ``drop_from_raw`` columns are filtered out of ``raw_data`` entirely —
      they will not be passed to the LLM nor stored.
    """

    canonical_to_source, drop_set, include_only_set = _normalise_mapping_config(
        mapping_config
    )

    core: dict[str, Any] = {}

    if canonical_to_source:
        for canonical, source_col in canonical_to_source.items():
            value = row.get(source_col)
            if value not in (None, ""):
                core[canonical] = value

    raw: dict[str, Any] = {k: v for k, v in row.items() if k not in drop_set}

    for key, value in row.items():
        if value in (None, ""):
            continue
        if key in include_only_set:
            continue
        canon = _canonical_key(key)
        if canon in canonical_to_source:
            continue
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
    mapping_config: dict[str, Any] | None = None,
) -> ImportRowResult:
    core, raw = _split_core_and_raw(row, mapping_config=mapping_config)

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
    mapping_config: dict[str, Any] | None = None,
) -> ImportSummary:
    """Import a CSV or NDJSON file into the lead table.

    Commits are the caller's responsibility — the function uses ``session.flush``
    throughout so the caller can wrap the whole import in a transaction.

    ``mapping_config`` is the operator-supplied per-import override (ticket
    0004). When supplied, it is persisted onto ``ImportJob.mapping_config`` so
    ``raw_data`` decisions on this run are auditable later. ``None`` is a fully
    backward-compatible default.
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
        mapping_config=mapping_config,
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
                mapping_config=mapping_config,
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

# Heuristic thresholds (resolved 2026-04-27 via council on ticket 0004 OQ2).
# Tiered: 90% match -> high, 80% match -> medium, with a minimum support
# floor so a column with one or two non-null cells does not claim
# certainty on a 100% rate.
_HEURISTIC_HIGH_RATIO = 0.9
_HEURISTIC_MEDIUM_RATIO = 0.8
_HEURISTIC_MIN_SUPPORT = 5


@dataclass
class PreviewRow:
    """One row as it would be classified on commit."""

    name: str | None
    phone: str | None
    normalised_phone: str | None
    contact_type: str
    skip_reason: str | None


@dataclass
class ColumnPreview:
    """One distinct source column observed in the preview sample.

    ``suggested_target`` is one of ``_CORE_FIELDS`` when the suggestion engine
    has an opinion, otherwise ``None``. ``suggestion_confidence`` follows the
    tiered scheme: ``high`` (exact match or strong heuristic with >= 5
    supporting cells), ``medium`` (alias / Levenshtein / substring / weaker
    heuristic), ``low`` (weakest signal kept), or ``none`` (no opinion).
    ``suggestion_reason`` is a short human-readable label that the UI shows
    next to the confidence badge.
    """

    name: str
    sample_values: list[Any]
    suggested_target: str | None
    suggestion_confidence: str  # "high" | "medium" | "low" | "none"
    suggestion_reason: str


@dataclass
class ImportPreview:
    """The shape the ``POST /api/leads/import/preview`` endpoint returns.

    ``would_skip`` is ordered by frequency descending so the endpoint can
    render it directly. ``sample`` is capped at the first ``_PREVIEW_SAMPLE_LIMIT``
    rows — enough to eyeball without blowing up big-file previews. ``columns``
    is the union of keys observed across the sampled rows, with one
    ``ColumnPreview`` entry per distinct name.

    ``social_website_hosts`` is a per-platform tally of rows whose
    ``website`` column is a social-profile URL (ticket 0014).
    Platforms with zero matches are omitted; an empty dict means the
    upload has no social-as-website rows. The frontend renders a
    callout from this so the operator sees how many leads will land
    in the priority tier before they commit.
    """

    file_type: str
    total_rows: int
    would_import: int
    would_skip: list[tuple[str, int]]
    sample: list[PreviewRow]
    columns: list[ColumnPreview] = field(default_factory=list)
    social_website_hosts: dict[str, int] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Column suggestion engine (deterministic, rule-based)
# ---------------------------------------------------------------------------


def _levenshtein(a: str, b: str) -> int:
    """Iterative Levenshtein distance.

    No external dep; the use case here is comparing two short column names so
    the cost is negligible. The smallest off-the-shelf alternative
    (``rapidfuzz``) would be a new top-level dependency for ~30 lines of
    arithmetic — not justified.
    """

    if a == b:
        return 0
    if not a:
        return len(b)
    if not b:
        return len(a)
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, start=1):
        curr = [i] + [0] * len(b)
        for j, cb in enumerate(b, start=1):
            cost = 0 if ca == cb else 1
            curr[j] = min(prev[j] + 1, curr[j - 1] + 1, prev[j - 1] + cost)
        prev = curr
    return prev[-1]


_HTTP_URL_HEAD = ("http://", "https://")


_PHONE_ALLOWED_CHARS = set("0123456789 +-().\t")


def _looks_like_phone(value: Any) -> bool:
    """A loose phone-shape check — used to gate ``phone`` heuristic suggestion.

    We do **not** call ``normalise_phone`` here because it depends on a
    region hint and would raise / log on unparseable values; the suggestion
    engine just needs "does this column smell like a phone column?" and the
    operator confirms.

    Phone strings are digits plus a tight punctuation set (``+ - ( ) . space``).
    Anything else (e.g. a ``T`` or ``Z`` in an ISO timestamp) disqualifies.
    """

    if value is None:
        return False
    s = str(value).strip()
    if not s:
        return False
    digits = sum(1 for c in s if c.isdigit())
    if digits < 7:
        return False
    return all(c in _PHONE_ALLOWED_CHARS for c in s)


def _looks_like_url(value: Any) -> bool:
    if value is None:
        return False
    s = str(value).strip().lower()
    return s.startswith(_HTTP_URL_HEAD)


_ADDRESS_WORD_RE = re.compile(
    r"\b("
    r"st|street|"
    r"rd|road|"
    r"ave|avenue|"
    r"dr|drive|"
    r"hwy|highway|"
    r"ln|lane|"
    r"ct|court|"
    r"pl|place|"
    r"tce|terrace|"
    r"cres|crescent|"
    r"blvd|boulevard|"
    r"qld|nsw|vic|tas|sa|wa|nt|act"
    r")\b",
    re.IGNORECASE,
)


def _looks_like_address(value: Any) -> bool:
    """Loose address heuristic: comma-separated, OR contains a street / region
    keyword on a word boundary. Tuned for AU + en-US shapes seen in scraped
    lead data.

    Earlier iterations of this rule also matched "starts-with-digit + has a
    space"; that was over-permissive — Google Plus Codes (``6F8X+9R Caboolture``)
    and AU phone strings (``0431 222 333``) tripped it. Real addresses in
    every observed sample have either a comma or a street keyword, so the
    weaker rule is dead weight.
    """

    if value is None:
        return False
    s = str(value).strip()
    if not s:
        return False
    if "," in s:
        return True
    if _ADDRESS_WORD_RE.search(s):
        return True
    return False


_HEURISTIC_TESTS = {
    "phone": _looks_like_phone,
    "website": _looks_like_url,
    "address": _looks_like_address,
}


def _suggest_column_target(
    column_name: str,
    sample_values: list[Any],
) -> tuple[str | None, str, str]:
    """Run the suggestion pyramid for a single column.

    Returns ``(suggested_target, confidence, reason)``. The pyramid is:

    1. Exact match against ``_CORE_FIELDS`` -> high.
    2. Alias map hit -> high.
    3. Levenshtein <= 2 against any core field -> medium.
    4. Substring match against any core field -> medium.
    5. Sample-value heuristic (per ``_HEURISTIC_TESTS``):
       * ratio >= 0.9 with >= 5 non-null support -> high
       * ratio >= 0.8 with >= 5 non-null support -> medium
       * (address weakens to ``low`` because the heuristic is loose.)
    6. Otherwise ``(None, "none", "no signal")``.
    """

    canon = _canonical_key(column_name)

    if canon in _CORE_FIELDS:
        return canon, "high", "exact column-name match"

    if canon in _CORE_ALIASES:
        return _CORE_ALIASES[canon], "high", f"alias map ('{canon}')"

    for core in sorted(_CORE_FIELDS):
        if _levenshtein(canon, core) <= 2 and len(core) >= 4:
            return core, "medium", f"close to '{core}' (typo distance)"

    for core in sorted(_CORE_FIELDS):
        if core in canon and core != canon:
            return core, "medium", f"name contains '{core}'"

    # Heuristics only make sense for scalar columns. A column whose values are
    # nested structures (lists / dicts — e.g. Apify's ``reviewDetails``) is
    # not an address even if the Python repr of the list happens to contain a
    # comma; surface as ``none`` instead of misleading the operator.
    scalar_non_null = [
        v
        for v in sample_values
        if v not in (None, "") and not isinstance(v, (list, dict))
    ]
    if len(scalar_non_null) >= _HEURISTIC_MIN_SUPPORT:
        for target, predicate in _HEURISTIC_TESTS.items():
            hits = sum(1 for v in scalar_non_null if predicate(v))
            ratio = hits / len(scalar_non_null)
            if ratio >= _HEURISTIC_HIGH_RATIO:
                base = "low" if target == "address" else "high"
                return target, base, f"{int(ratio * 100)}% of values look like {target}"
            if ratio >= _HEURISTIC_MEDIUM_RATIO:
                base = "low" if target == "address" else "medium"
                return target, base, f"{int(ratio * 100)}% of values look like {target}"

    return None, "none", "no signal"


def _build_columns_preview(
    sampled_rows: list[dict[str, Any]],
) -> list[ColumnPreview]:
    """Build one ``ColumnPreview`` per distinct column name across the sampled
    rows. Sample values are the first ``_PREVIEW_SAMPLE_LIMIT`` non-null cells
    (de-duplicated to keep the UI table compact)."""

    seen_names: list[str] = []
    samples_by_name: dict[str, list[Any]] = {}

    for row in sampled_rows:
        for key, value in row.items():
            if key not in samples_by_name:
                seen_names.append(key)
                samples_by_name[key] = []
            samples_by_name[key].append(value)

    out: list[ColumnPreview] = []
    for name in seen_names:
        values = samples_by_name[name][:_PREVIEW_SAMPLE_LIMIT]
        target, confidence, reason = _suggest_column_target(name, values)

        # Surface a *deduped*, readable sample. Long values truncated to keep
        # the JSON payload sane on a 50 KB reviewDetails blob.
        compact: list[Any] = []
        seen: set[str] = set()
        for v in values:
            if v in (None, ""):
                continue
            key = repr(v)[:200]
            if key in seen:
                continue
            seen.add(key)
            if isinstance(v, str) and len(v) > 200:
                compact.append(v[:200] + "…")
            else:
                compact.append(v)
            if len(compact) >= 5:
                break

        out.append(
            ColumnPreview(
                name=name,
                sample_values=compact,
                suggested_target=target,
                suggestion_confidence=confidence,
                suggestion_reason=reason,
            )
        )
    return out


# ---------------------------------------------------------------------------
# Preview entry point
# ---------------------------------------------------------------------------


def preview_import_file(
    *,
    path: Path,
    region_hint: str = "AU",
    mapping_config: dict[str, Any] | None = None,
) -> ImportPreview:
    """Dry-run version of :func:`import_file`.

    The web preview endpoint used to reach directly into ``_detect_file_type``,
    ``_iter_csv``/``_iter_ndjson``, and ``_split_core_and_raw``. Exposing one
    helper here means the parsing rules, alias resolution, and phone
    normalisation stay in a single module; callers just get a summary.

    When the operator supplies a ``mapping_config``, the same partition logic
    that the commit path will use is applied — so preview and commit cannot
    drift (resolved OQ1, ticket 0004).
    """

    path = Path(path)
    file_type = _detect_file_type(path)
    iterator = _iter_csv(path) if file_type == "csv" else _iter_ndjson(path)

    total = 0
    would_import = 0
    skip_counter: Counter[str] = Counter()
    sample: list[PreviewRow] = []
    sampled_rows: list[dict[str, Any]] = []
    # Tally of platform tokens for rows whose mapped ``website`` is a
    # social-profile URL (ticket 0014). Counted across the *entire*
    # file, not just the sample, so the operator's pre-commit
    # callout reflects the true priority-tier hit rate. Hostname-only
    # match (``acme.com/about-our-facebook`` does not count) — see
    # :func:`autosdr.enrichment.is_social_website`.
    social_website_counter: Counter[str] = Counter()

    for row in iterator:
        total += 1
        if len(sampled_rows) < _PREVIEW_SAMPLE_LIMIT:
            sampled_rows.append(row)
        core, _raw = _split_core_and_raw(row, mapping_config=mapping_config)
        platform = is_social_website(core.get("website"))
        if platform is not None:
            social_website_counter[platform] += 1
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
        columns=_build_columns_preview(sampled_rows),
        social_website_hosts=dict(social_website_counter),
    )
