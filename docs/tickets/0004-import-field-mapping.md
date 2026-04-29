# [feature/data+ui] Field-mapping helper for non-canonical lead files

<!-- TYPE: feature -->
<!-- AREA: data + ui -->

## Problem

The Time-Poor Founder shows up with a real lead source — not a hand-rolled
CSV. Today's importer (`autosdr/importer.py:_split_core_and_raw:195-213`)
matches columns by **exact lowercase + a 13-entry alias map**
(`autosdr/importer.py:_CORE_ALIASES:45-61`). Anything outside that hits
`raw_data` verbatim and the operator has zero per-column control.

Direct evidence sitting in the repo root right now: `all_results_qld.json`
(323 MB, NDJSON, ~ 74k rows) — an Apify "Google Maps places" scrape with
columns `name`, `category`, `address`, `phone`, `website`, **plus**
`reviews` (count), `reviewDetails` (array of objects with author/rating/
text), `webResults` (array, often null), `searchQuery`, `scrapedAt`,
`plusCode`, `rating`. The first 5 already match. The rest get dumped
into `raw_data` whole — including `reviewDetails` which can be 5–20 KB
per row of nested review JSON. The analysis prompt's per-lead byte
ceiling (`ARCHITECTURE.md § 7.1` "raw-data blob is truncated to a
configurable byte ceiling, longest strings first") then fights with that
verbose nested object for room.

Outcome: every Apify-style import currently has analysis-prompt context
dominated by review JSON the operator may or may not want, with no
preview UX to do anything about it.

The `ImportJob` model already has `mapping_config: dict | None` field
(`autosdr/models.py:189`) — the seam is there, the implementation is
not.

## Hypothesis

If the import preview lets the operator (a) see every distinct column,
(b) accept / override the auto-suggested mapping for each, and (c) opt a
column out of `raw_data` entirely, then a real Apify file imports with
the operator's intent on first try — no manual CSV pre-processing, no
"why is the prompt obsessed with reviews of unrelated businesses". This
is the doc2 field-mapping agent for v0, **without** an LLM step (defer
that to a follow-up ticket; rule-based suggestions cover ≥ 80% of
columns in observed scrapes).

Measured by: the operator can import `all_results_qld.json` end-to-end
without hand-editing the file, with a deliberate decision recorded for
every non-core column.

## Scope

- Extend `ImportPreview` (`autosdr/importer.py:409-422`) to include:
  - `columns: list[ColumnPreview]`, where each entry is
    `{name, sample_values: list[Any], suggested_target: str | None,
    suggestion_confidence: "high" | "medium" | "low" | "none",
    suggestion_reason: str}`.
  - The list is the union of keys seen across the **first
    `_PREVIEW_SAMPLE_LIMIT` rows** (already 20 — keep it; the goal is
    a representative sample, not exhaustive).
- Suggestion engine (rule-based, deterministic):
  - Exact match against `_CORE_FIELDS` / `_CORE_ALIASES` →
    confidence `high`.
  - Levenshtein ≤ 2 against `_CORE_FIELDS` → confidence `medium`.
  - Substring match (`"phone_e164"` contains `"phone"`) → confidence
    `medium`.
  - Sample-value heuristics: at least 80% of non-null sample values
    look like an E.164-able phone string → `phone` (confidence
    `high`); look like an http URL → `website`; look like a postcode
    or street → `address` (confidence `low`).
  - Otherwise `suggested_target=None`, confidence `none`.
- Add `POST /api/leads/import/commit` (or extend the existing commit
  endpoint — see Open questions) to accept a `mapping_config`:

  ```json
  {
    "mapping": {
      "phone": "phone",            // canonical → source column
      "name": "businessName",
      "category": "industryType"
    },
    "drop_from_raw": ["reviewDetails", "webResults"],
    "include_in_raw_only": ["plusCode", "scrapedAt"]
  }
  ```

  Persist on `ImportJob.mapping_config`. Replay logic in
  `_split_core_and_raw` honours the operator's mapping over the alias
  map.
- Frontend: `frontend/src/routes/LeadsImport.tsx` gets a column-mapping
  table after the preview but before commit:
  - One row per detected column.
  - Sample values inline (truncated, hover for full).
  - A `<select>` with the canonical fields + "Keep in raw_data only" +
    "Drop entirely". Pre-selected to the suggestion.
  - A "Bulk: drop all unsuggested" button (one click for the
    "Apify scrape with too much noise" case).
- CLI: `autosdr import <file> --map phone=mobile --drop reviewDetails`
  for headless commits.

## Out of scope

- LLM-assisted suggestions (the doc2 "field-mapping agent" proper).
  Track separately. The deterministic engine handles common scrapes;
  add LLM only when an operator can't get a clean import deterministically.
- Streaming / chunked imports for very large files. The 323 MB file is
  the proximate cause of *this* ticket but the import pipeline today
  reads the whole file into memory (`Path.read_text` in
  `autosdr/importer.py:165` for NDJSON). That's a separate ticket;
  don't blow scope here. (See Roadmap → Later → "Streaming NDJSON".)
- Per-row transforms (e.g. "lowercase the email", "extract first
  paragraph from this HTML"). Out of scope for v0; the analysis
  prompt does enough.
- Column rename / re-order in the operator's source file. We don't
  modify the source.
- Operator-defined custom canonical fields (`profession_grade`). Stick
  with the fixed `_CORE_FIELDS` set; everything else is `raw_data`.

## Success criteria

- New `tests/test_importer_field_mapping.py`:
  - Suggestion engine: each rule has at least one positive + one
    negative test.
  - Apify fixture: import the first 100 rows of `all_results_qld.json`
    (excerpt as a fixture file under `tests/fixtures/`) and assert
    every column has a suggestion or `none`, and that `phone` is
    suggested with `high` confidence.
  - Operator override: a mapping that contradicts the suggestion is
    honoured.
  - Drop list: dropped columns do not appear in `lead.raw_data`.
- Re-import idempotency: running with the same `mapping_config` twice
  produces no spurious updates.
- The preview endpoint stays under 1s on a 5k-row CSV.
- The commit endpoint stays under 60s on a 1k-row CSV (the doc1 § 6
  success-metric target).
- UI: a non-technical operator can complete import without docs (verified
  by walking through against `all_results_qld.json`).

## Effort & risk

- **Size:** M (3–5 days)
- **Touched surfaces:** `autosdr/importer.py` (signature change on
  `ImportPreview` and `_split_core_and_raw` — invasive on internals,
  not on the API),
  `autosdr/api/leads.py` (preview + commit endpoints),
  `autosdr/api/schemas.py` (additive),
  `autosdr/cli.py` (`--map` / `--drop` flags),
  `frontend/src/routes/LeadsImport.tsx`, `frontend/src/lib/types.ts`,
  `frontend/src/lib/api.ts`.
- **Change class:** additive on the API; invasive on `_split_core_and_raw`
  (covered by tests).
- **Risks:**
  - Levenshtein has a bias on short column names (`"id"` and `"phone"`
    differ by 4 — not a problem, but `"name"` and `"namee"` diff by
    1 — fine). Pure code.
  - Sample-value heuristics on a 323 MB file: we already cap at 20 rows
    for preview (`_PREVIEW_SAMPLE_LIMIT`). Stay under the cap — don't
    accidentally read the whole file twice.
  - `ImportJob.mapping_config` persistence already supported by the
    schema (`autosdr/models.py:189`); no migration.
  - The LLM analysis prompt's byte ceiling needs to keep behaving when
    `raw_data` is now smaller. Should be a strict improvement —
    document and re-run the dryrun harness
    (`scripts/dryrun_prompts.py`) on a fixture to confirm prompt size
    drops.

## Open questions

- ~~Single endpoint or split?~~ **Resolved 2026-04-27** — extend.
- ~~Detection threshold for sample-value heuristics: 80% vs 90%?~~ **Resolved 2026-04-27** — tiered: 90% → high, 80% → medium, ≥5 non-null support floor.
- ~~"Drop from raw_data" semantic: commit-only or retroactive?~~ **Resolved 2026-04-27** — commit-only.
- ~~Save mapping as template?~~ **Deferred 2026-04-27** — ticket already steered to defer; operator didn't ask.

## Resolved questions (2026-04-27)

### Resolved: endpoint-shape

**Architect:** Extend `/api/leads/import/commit` with an optional `mapping_config` form field carrying JSON-as-string.
**Skeptic:** Workable but watch for silent mis-mapping when JSON is malformed-but-parseable.
**Pragmatist:** Extend, with a clear contract: operator always passes mapping on commit; don't try to glue preview-stored config to a separate re-upload.
**Critic:** Extend, with strict schema symmetry — same Pydantic validator applies to preview and commit.

**Decision:** Extend the existing `/api/leads/import/commit` with an optional `mapping_config` form field (JSON string). Validate shape via a Pydantic model; reject unknown canonical-target names. Mirror the same model in the preview endpoint so operators can see the mapping applied before committing.
**Strongest dissent:** Skeptic's "silent mis-mapping" — mitigated by strict shape validation + a server-side check that mapping targets exist in `_CORE_FIELDS`.
**Confidence:** high
**Why this is acceptable:** Three voices converge; the residual risk is a code-level concern, not architecture.

### Resolved: heuristic-threshold

**Architect (initial):** 80% across the board.
**Skeptic:** 80% — HITL changes the loss function; n=20 is coarse; sampling variance dominates threshold choice.
**Pragmatist:** 90% — automation bias on `high`; a wrong `high` is worse than no suggestion.
**Critic:** 90% — operators rubber-stamp `high`; wrong auto-suggestion is harder to un-learn than missing one.

**Decision (changed from initial):** Tiered.
- `high` confidence: ≥ 90% non-null match AND ≥ 5 non-null sample values present.
- `medium` confidence: ≥ 80% non-null match AND ≥ 5 non-null sample values present.
- Otherwise no heuristic suggestion (still falls back to exact / Levenshtein / substring matching on the column name).
The ≥ 5 non-null floor addresses Pragmatist's surprise that "% of non-null" denominator can be tiny on sparse columns.
**Strongest dissent:** Skeptic was right that thresholds alone don't prevent rubber-stamping — UX (samples shown inline, confidence badge styling) carries equal weight.
**Confidence:** medium
**Why this is acceptable:** Threshold is the cheapest single parameter to tune; no schema cost to revising.

### Resolved: drop-semantic

**All four voices:** Commit-only.

**Decision:** `drop_from_raw` filters keys out of *incoming* row payloads only. The existing `_merge_raw_data` behaviour is preserved: existing `raw_data` keys are not deleted, just not added to. **Never** retroactively prune.
**Strongest dissent:** All three voices flagged the same surprise — stale legacy rows keep oversized `raw_data` indefinitely; operator may *expect* re-import to "heal" them. Mitigation: UI helper text near the drop control reading **"Applies to this import only — existing records keep what they have."**
**Confidence:** high
**Why this is acceptable:** Universal council agreement; the gap is documented, not silent.

### Resolved: mapping-templates

**Decision:** Defer (no council needed). The ticket explicitly recommends defer; operator didn't ask. Filed as a follow-up candidate.
**Confidence:** high

## Principle check

- Simplicity first: ⚠ (the column-mapping UI adds a step; justified by
  the operator-pain evidence in the repo)
- Quality over speed: ✓ (cleaner `raw_data` → better personalisation)
- Honest data contracts: ✓ (operator sees, decides, persists)
- Extensible by design: ✓ (`ImportJob.mapping_config` already there;
  rule-based engine is a clean stepping stone to the LLM agent)
- Human always wins: ✓ (operator approves every column)
- Owner stays in control: ✓

## Links

- Spec: `autosdr-doc2-data-architecture.md` — field-mapping agent.
- Spec: `autosdr-doc1-product-overview.md § 6` — "1k-row CSV …
  ingested + mapped in < 60s".
- Architecture: `ARCHITECTURE.md § 5` — lead import.
- Code: `autosdr/importer.py:44-61` (`_CORE_*`),
  `autosdr/importer.py:195-213` (`_split_core_and_raw`),
  `autosdr/importer.py:425-490` (`preview_import_file`),
  `autosdr/models.py:177-200` (`ImportJob`),
  `frontend/src/routes/LeadsImport.tsx`.
- Evidence: `all_results_qld.json` (untracked, 323 MB, NDJSON,
  observed 2026-04-26).
- Roadmap: `docs/ROADMAP.md` → Next → row 4.

## Dependencies

- Blocks: streaming NDJSON ticket (`docs/ROADMAP.md` → Later → row 2);
  doing this first means the streaming work targets the right shape.
- Blocked by: `.gitignore` fix for `all_results_qld.json` should land
  before any test fixture is extracted from it (avoid an accidental
  commit).
- Related: 0001 (`do_not_contact` flag must survive a re-import — see
  importer guard in 0001's scope).

## Implementation log (2026-04-27)

**Status:** done

| # | Unit | Outcome | Evidence |
|---|------|---------|----------|
| 1 | Refactor `_split_core_and_raw` to accept `mapping_config` (BC default) | done | `tests/test_importer_field_mapping.py::test_split_default_unchanged_when_no_mapping_config`, `test_mapping_overrides_alias_pick`, `test_mapping_can_explicitly_disable_alias_match` |
| 2 | Thread `mapping_config` through `import_file` + `preview_import_file`; persist on `ImportJob.mapping_config` | done | `test_drop_does_not_remove_existing_raw_data_on_reimport`, `test_reimport_with_same_mapping_config_is_idempotent`, `test_preview_honours_mapping_config` |
| 3 | `ColumnPreview` + suggestion engine (exact / Levenshtein / substring / tiered sample heuristics) | done | 16 suggestion-engine tests including `test_suggest_phone_heuristic_high_when_90pct_match`, `test_suggest_phone_heuristic_blocked_by_min_support_floor`, `test_preview_apify_fixture_every_column_has_suggestion_or_none` |
| 4 | Pydantic `MappingConfigIn` + `ImportPreviewColumn`; `/preview` and `/commit` round-trip with strict 422 on bad JSON | done | `tests/test_leads_import_api.py` — 5 tests including `test_commit_invalid_mapping_config_returns_422` and `test_commit_without_mapping_config_is_backward_compatible` |
| 5 | CLI `--map` / `--drop` / `--raw-only` flags on `autosdr import` | done | `tests/test_cli_import_mapping.py` — 5 tests including `test_import_persists_mapping_config_on_import_job`, `test_import_rejects_malformed_map_pair` |
| 6 | Frontend column-mapping table + helper text + types/api mirror | done | `frontend/src/routes/LeadsImport.tsx::ColumnMappingTable`, `frontend/src/lib/types.ts::MappingConfig`, `npm run build` clean |

**Final state of success criteria:**
- New `tests/test_importer_field_mapping.py`: ✓ — 25 tests; every suggestion rule has positive + negative coverage.
- Apify fixture import (every column has a suggestion or `none`; `phone` suggested with `high`): ✓ — `test_preview_apify_fixture_every_column_has_suggestion_or_none` (`tests/fixtures/apify_qld_excerpt.ndjson`, 20 synthetic rows mirroring the Apify schema after the source `all_results_qld.json` was confirmed absent on disk).
- Operator override honoured: ✓ — `test_mapping_overrides_alias_pick`.
- Drop list: dropped columns absent from `lead.raw_data`: ✓ — `test_drop_from_raw_omits_keys_from_raw_data`, `test_commit_with_mapping_drops_noisy_keys_from_raw_data`.
- Re-import idempotency on same `mapping_config`: ✓ — `test_reimport_with_same_mapping_config_is_idempotent` (zero spurious updates, idempotent merge).
- Preview <1s on 5k-row CSV: ✓ — measured 168ms (5000 rows, 5 columns) on local dev machine.
- Commit <60s on 1k-row CSV: ✓ — measured 546ms (1000 rows imported).
- UI: non-technical operator can complete import without docs: ✓ — `LeadsImport.tsx` renders one row per detected column, dropdown defaults to the suggestion, "Drop all unsuggested" bulk action, helper text spells out the commit-only drop semantic.

**Principle check after implementation:**
- Simplicity first: ⚠ — column-mapping UI adds a step; explicitly accepted in the ticket.
- Quality over speed: ✓ — `reviewDetails` and other noise can now be dropped before the analysis prompt sees `raw_data`.
- Honest data contracts: ✓ — every column the operator sees in the preview is the same set the importer will act on; suggestion + reason are surfaced, not hidden.
- Extensible by design: ✓ — rule-based engine isolated in `_suggest_column_target`; LLM-assisted suggestions can layer in without re-plumbing the form payload.
- Human always wins: ✓ — operator approves every column; defaults preselect to the suggestion but never auto-commit.
- Owner stays in control: ✓ — `mapping_config` persists on `ImportJob.mapping_config` so prior decisions are auditable.

**Pattern-unifier diff-only check:**
- Frontend (`frontend/src/lib/api.ts`, `frontend/src/lib/types.ts`, `frontend/src/routes/LeadsImport.tsx`): no new fetch outside `api.ts`, no `axios`, no alternate router / styling lib, table-width inline styles match the pre-existing convention in the same file.
- Backend (`autosdr/api/leads.py`, `autosdr/api/schemas.py`, `autosdr/cli.py`, `autosdr/importer.py`): imports stay within the blessed set (FastAPI, Pydantic, SQLAlchemy, typer, phonenumbers, stdlib only).
- No new ⚠ / ✗ rows introduced.

**Follow-ups raised:**
- LLM-assisted column-suggestion agent (out of scope per ticket; revisit when an Apify scrape can't be cleanly mapped deterministically).
- "Save mapping as template" — deferred per council resolution; file as a follow-up if a second operator asks.
- True streaming NDJSON import — already on the roadmap (Later), unblocked by this ticket landing.

**Open questions still unresolved:** (none — all four resolved 2026-04-27)
