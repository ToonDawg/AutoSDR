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

- Single endpoint or split? Today the importer has
  `/api/leads/import/preview` (per `autosdr/api/leads.py`) +
  presumably a commit. Decide between adding a new
  `/api/leads/import/commit-with-mapping` vs. accepting `mapping_config`
  on the existing commit. Recommend extending the existing endpoint
  with `mapping_config` optional (BC for callers who don't pass one).
- Detection threshold for sample-value heuristics: 80% non-null match
  rate is a guess. Consider 90% to be conservative. Decision.
- "Drop from raw_data" semantic: do we drop on commit only, or also
  retroactively prune existing rows on a re-import? Recommend
  commit-only; retroactive prune is destructive and surprising.
- Do we need a "save this mapping as a template" feature? Useful if
  the operator imports multiple Apify files. Defer to a follow-up
  unless the operator asks.

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
