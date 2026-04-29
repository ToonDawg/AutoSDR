# [feature/api+ui] Estimate LLM cost from token counts (Gemini-only) + model presets

<!-- TYPE: feature -->
<!-- AREA: api + ui -->

## Problem

The Time-Poor Founder watches `autosdr status` and the dashboard's "LLM
usage" pill grow `calls=…  tokens_in=…  tokens_out=…` and has no idea
what that costs. The status schema already has a placeholder field for
this — `LlmUsage.estimated_cost_today_usd: float = 0.0`
(`autosdr/api/schemas.py:140-144`) — but it is hardcoded to `0.0`
because we never wired a pricing table
(`autosdr/api/status.py:85-91`). Both ticket 0001's decisions log and
ticket 0003's "Out of scope" explicitly flagged "Cost tracking
($/lead, $/reply)" as a follow-up.

Consequence: an operator who wants to compare "MAX-quality every call"
vs. "Flash-Lite for cheap roles" has nothing to inform the choice
beyond gut feel. They also have no way to swap the four model roles
(`model_main` / `_analysis` / `_eval` / `_classification` — see
`autosdr/config.py:78-90`) atomically; the Settings LLM card is four
free-text inputs, every preset blend has to be hand-typed
(`frontend/src/routes/settings/LlmCard.tsx:75-107`).

This ticket gives the operator (a) a real cost number on the dashboard
and on every LLM call row, and (b) one-click presets for the four
model roles, scoped to **Gemini only** (the default and only-supported
provider on the Setup wizard — `frontend/src/routes/setup/LlmStep.tsx:9-13`).

## Hypothesis

If we surface `estimated_cost_today_usd` and a per-call `cost_usd`
column from a single canonical pricing table, and ship 3 named
Gemini presets (MAX / BALANCED / CHEAP) the operator can apply with
one click, then:

- The "is this expensive?" question gets a numeric answer in < 1 s
  on the dashboard and on the `/Logs` rows.
- Switching from MAX to CHEAP for a low-volume campaign is a single
  click in Settings, not four field edits.
- The principle "Owner stays in control" gains teeth — cost is part
  of the visible control surface, not a hidden side-effect.

Measured by:

- `LlmUsage.estimated_cost_today_usd > 0` after a real pipeline tick
  whose `model` is in the pricing table.
- Each row on `/Logs` carries a `cost_usd` cell (4-decimal USD or
  `—` for unknown models / zero-token sentinel rows).
- Settings → LLM card has three preset buttons that, on click, fill
  the four model-slug inputs with the preset's blend.

## Scope

- **New module `autosdr/llm/pricing.py`** — single source of truth
  for Gemini pricing + presets:
  - `GEMINI_PRICING: dict[str, ModelPrice]` mapping canonical
    LiteLLM slugs (e.g. `gemini/gemini-3-flash-preview`,
    `gemini/gemini-2.5-pro`, `gemini/gemini-2.5-flash-lite`) to
    `(input_per_1m_usd, output_per_1m_usd)` at the standard /
    paid-tier rate (text only — audio/image/batch out of scope).
  - `GEMINI_LATEST_ALIASES` — display alias map. Google's stable
    families ship `-latest` suffixes (`gemini-2.5-pro-latest` →
    `gemini-2.5-pro`); Gemini 3.x previews don't yet, so the
    `-preview` slug is itself the rolling target. The alias resolver
    is a pure dict normaliser — slugs that aren't in either map fall
    through unchanged.
  - `cost_for(model: str, tokens_in: int, tokens_out: int) -> float | None`
    — `None` for unknown slugs (so callers can render `—` instead of
    a misleading `$0.00`).
  - `LLM_PRESETS: dict[PresetId, Preset]` — three named blends:
    - **MAX** — `gemini/gemini-3.1-pro-preview` for all four roles.
    - **BALANCED** — Pro for `model_main`, Flash for `model_analysis`,
      Flash-Lite for `model_eval` and `model_classification`.
    - **CHEAP** — Flash-Lite for all four roles.
- **Wire cost into the in-memory usage counter**
  (`autosdr/llm/client.py:202-222`). `_record_usage` adds a
  `cost_usd` accumulator per model; `get_usage_snapshot()` returns
  `total_cost_usd` and a `cost_usd` field per `per_model` bucket.
- **Status API** — `autosdr/api/status.py:85-91` reads
  `total_cost_usd` from the snapshot instead of hardcoding `0.0`.
- **`LlmCallOut`** — add `cost_usd: float | None` computed from the
  pricing module on serialisation (no DB column — see Open
  questions / decision rationale).
- **Logs UI** — extra column on each row: `$<4dp>` or `—`, right-
  aligned, tabular-nums (`frontend/src/routes/Logs.tsx:154`,
  `:208-210`). Header label: "Cost".
- **Top-bar dashboard pill** — surface today's
  `estimated_cost_today_usd` next to tokens in the existing status
  pill (the consumer of `SystemStatus.llm_usage`).
- **Presets API** — `GET /api/llm/presets` returns the three preset
  blends with id / label / description / models / pricing snapshot.
- **Settings LLM card** — row of three preset buttons above the four
  model-slug inputs (`frontend/src/routes/settings/LlmCard.tsx:75`).
  Click a button → the four fields are populated from the preset.
  The existing dirty-tracking + `SaveRow` flow is untouched —
  applying a preset just sets the form state.
- **CLI `autosdr status`** — extra column on the per-model usage
  table: `cost_usd` (`autosdr/cli.py:289-302`).
- **Tests:**
  - `tests/test_llm_pricing.py` — pricing-map shape, alias
    resolution, `cost_for` rounding + unknown-slug `None`, preset
    completeness (every preset names all four roles, every named
    model appears in the pricing map).
  - Extension to `tests/test_status_api.py` (or wherever `LlmUsage`
    is asserted) covering `estimated_cost_today_usd` rises with
    fake calls.
  - `tests/test_llm_calls_api.py` extension asserting `cost_usd` on
    a known-model row and `null` on an unknown / zero-token row.

## Out of scope

- Non-Gemini providers (OpenAI, Anthropic). The Settings card still
  accepts the slugs and the API keys still funnel through, but
  presets and pricing are Gemini-only — the wizard only supports
  Gemini today (`frontend/src/routes/setup/LlmStep.tsx:9-13`) and
  the entire "AI loop is the moat" principle is currently
  Gemini-shaped.
- Persisting per-call cost on the `llm_call` row. We compute at read
  time (see Open questions). Trade-off: historical rows reprice if
  the table changes, which is fine for an operator-facing estimate
  and keeps schema migrations off the critical path.
- Cost budgets / spend caps / alerts. "Cost > X today → pause" is a
  natural follow-up but adds a new control surface; out of scope for
  this ticket.
- Batch / Flex / Priority tiers. The standard paid tier is the
  honest default; tier-aware pricing is a future ticket.
- LiteLLM's own `litellm.completion_cost(...)` helper. Useful for
  models LiteLLM tracks, but the AutoSDR defaults are Gemini 3.x
  preview slugs that LiteLLM's `model_cost` map lags on. Owning the
  Gemini table ourselves (~12 rows) is cheaper than fighting
  LiteLLM's release schedule. Revisit when 3.x stabilises.
- Cost-by-purpose / cost-by-campaign aggregations. They become
  trivial once `cost_usd` is on every row, but the UI surface to
  consume them isn't here yet — file as a follow-up.
- DB-persisted pricing table (vs. code-owned). Pricing changes
  rarely; a code edit + restart is the right authoring loop for the
  POC.

## Success criteria

- `tests/test_llm_pricing.py` covers: pricing-map types, alias
  resolution, `cost_for` known/unknown/zero-token paths, presets
  closed under the pricing map.
- After a successful test pipeline run, `GET /api/status` returns
  `llm_usage.estimated_cost_today_usd > 0` (asserted in
  `tests/test_status_api.py` extension with a stubbed
  `_record_usage`).
- `GET /api/llm-calls` returns `cost_usd` on every row — non-null
  for known Gemini slugs with `tokens_in + tokens_out > 0`, `null`
  otherwise.
- `GET /api/llm/presets` returns three rows with stable ids
  (`max`, `balanced`, `cheap`) and every model named is in the
  pricing map.
- `frontend/src/routes/Logs.tsx` renders a `Cost` column; existing
  `tsc --noEmit` is clean.
- `frontend/src/routes/settings/LlmCard.tsx` renders three preset
  buttons; clicking one mutates the four model fields.
- `autosdr status` per-model table prints a `cost_usd` column.
- Principle check (§ Principle check below) survives unchanged.

## Effort & risk

- **Size:** S (~half a day; mostly additive)
- **Touched surfaces:** `autosdr/llm/`, `autosdr/api/status.py`,
  `autosdr/api/llm_calls.py`, `autosdr/api/schemas.py`, new
  `autosdr/api/llm.py` (presets endpoint), `autosdr/cli.py`,
  `frontend/src/lib/api.ts`, `frontend/src/lib/types.ts`,
  `frontend/src/routes/Logs.tsx`,
  `frontend/src/routes/settings/LlmCard.tsx`,
  `frontend/src/components/layout/*` (wherever the dashboard cost
  pill lives).
- **Change class:** additive (no schema migration; no breaking API
  field — `estimated_cost_today_usd` already exists; `cost_usd` is a
  new optional field).
- **Risks:**
  - Pricing drift: Gemini list price changes occasionally. Mitigation:
    the pricing map is one file, has a "last verified" date in its
    docstring, and the cost field is labelled "estimated" everywhere.
  - 0001's `(deterministic-opt-out)` sentinel rows have
    `tokens_in=0, tokens_out=0`; cost is naturally `0.0`. We assert
    on the unknown-model path so the sentinel returns `0.0` not
    `None`.
  - Test infra: `_record_usage` is module-global. New tests must
    `reset_usage()` at setup, same way existing tests do.

## Open questions

1. **Persist per-call cost (DB column on `llm_call`) or compute at
   read time from the pricing module?**
   - Persist: locks historical cost to the price-as-of-call. Adds
     one column. Migration path: additive.
   - Compute: single source of truth (pricing module). No schema
     change. Past rows retroactively reprice if the table is fixed.
   - **Council pending.** See Council below.
2. **MAX preset: `gemini-3.1-pro-preview` (preview, may move) or
   `gemini-2.5-pro` (stable, slightly cheaper, smaller context
   window)?** User preference call — surfaced, not decided.
3. **Default preset on a fresh setup: keep today's
   blend (Flash main / Flash-Lite eval) or switch to BALANCED**?
   User preference; default left untouched in this ticket.
4. **Preset surface — buttons (apply once, fields stay editable)
   or radio (preset is sticky, free-typing breaks the bond)?**
   I lean buttons; the existing free-text inputs are a real
   feature for power users.

## Principle check

- Simplicity first: ✓ — one pricing module, ~12 entries, one new
  API endpoint, ~12 new test lines per area.
- Quality over speed: ✓ — adds a quality signal (cost) without
  changing send semantics.
- Honest data contracts: ✓ — `cost_usd: float | None` (not
  `0.0`-as-unknown); "estimated" label visible everywhere; pricing
  map carries a "last verified" date.
- Extensible by design: ✓ — pricing/preset modules are pure data;
  adding OpenAI/Anthropic later means a sibling map, no rewrite.
- Human always wins: ✓ — no behaviour change in pipelines; cost is
  read-only.
- Owner stays in control: ✓ — presets fill fields, they don't lock
  them; operator can edit after applying; settings save flow is
  unchanged.

## Links

- Spec: `autosdr-doc1-product-overview.md § 6` (success metrics —
  cost isn't a tracked target today, but PM-level "is this
  affordable?" is implicit).
- Architecture: `ARCHITECTURE.md § 3` (LLM client component map).
- Code:
  - `autosdr/llm/client.py:202-230` — usage counter to extend.
  - `autosdr/api/status.py:85-91` — hardcoded `0.0` to replace.
  - `autosdr/api/schemas.py:140-144,479-499` — `LlmUsage`,
    `LlmCallOut`.
  - `autosdr/config.py:78-90` — model-role defaults.
  - `frontend/src/routes/Logs.tsx:148-158, 208-210` — Logs grid.
  - `frontend/src/routes/settings/LlmCard.tsx:75-107` — model-slug
    inputs.
- Pricing reference: `https://ai.google.dev/gemini-api/docs/pricing`
  (snapshot taken 2026-04-27; see `autosdr/llm/pricing.py` docstring).

## Dependencies

- Blocks: future "cost-by-campaign" / "cost budgets" tickets.
- Blocked by: (none).
- Related: 0001 (sentinel rows must stay zero-cost — verified by
  test); 0003's roadmap note.

## Resolved questions (2026-04-27)

### Resolved: persist-vs-compute

**Architect:** Compute at read-time. Cost is an operator-facing
estimate, not a billing record; the pricing map is one ~12-row
file; volume is ~10-20 calls/day; schema simplicity (no migration,
no backfill, no per-write-site bug class) wins on "simplicity
first".
**Skeptic:** Persist (A). Tokens are facts, cost-at-read is not a
stable property of a row; you already accept denormalisation
(tokens, model) so cost is the same class of fact.
**Pragmatist:** Compute (B). Less surface area, volume is
trivial, one place to fix pricing including the sentinel
(`(deterministic-opt-out)` → `0.0`).
**Critic:** Persist (A). Temporal honesty: read-time recompute
applies *today's* policy to *past* events. "Honest data contracts"
forbids the same row ID showing a different number tomorrow.

**Decision:** B — compute at read-time from
`autosdr/llm/pricing.py`. `LlmCallOut.cost_usd` is computed on
serialisation; `_record_usage` accumulates `cost_usd` per model in
memory (so the dashboard pill is real-time without a query).
**Strongest dissent:** Critic's temporal-honesty argument. Same
row ID can show a different number after a pricing-map edit.
**Confidence:** medium.
**Why this is acceptable:** (1) Every cost surface is labelled
*estimated* and shows the pricing-snapshot date. (2) No audit /
billing / month-over-month consumer exists — the cost is read by
"is this expensive?", not "what did I spend in March?". (3)
Pricing edits are rare, deliberate operator actions; the
dashboard reflecting current rates is the desired behaviour. (4)
If a real audit consumer ever appears we can migrate to A as a
clean additive column with a `pricing_snapshot_at` companion —
the read-time path doesn't preclude it. The Critic's worst-case
("March call re-opened in September") has no current consumer.

### Resolved: MAX preset (open question 2)

User-preference; deferring to operator. Default MAX preset uses
`gemini/gemini-3.1-pro-preview` (matches the family of the current
defaults; user can swap to `gemini/gemini-2.5-pro` via the four
free-text inputs).

### Resolved: default preset on fresh setup (open question 3)

Out of scope. Default settings remain unchanged
(`autosdr/config.py:78-90`). Operator can apply BALANCED or CHEAP
explicitly from the Settings card.

### Resolved: preset surface (open question 4)

Buttons. Applying a preset fills the four model-slug inputs;
existing dirty-tracking + `SaveRow` flow is untouched. The
operator can edit any of the four fields after applying — the
"Owner stays in control" principle requires it.

## Mini plan (2026-04-27)

| # | Unit                                                                                                                  | Files                                                                       | Change class | Tests                                                                                                                | Depends on | Risk |
|---|-----------------------------------------------------------------------------------------------------------------------|-----------------------------------------------------------------------------|--------------|----------------------------------------------------------------------------------------------------------------------|------------|------|
| 1 | New `autosdr/llm/pricing.py` — Gemini pricing map, alias resolver, `cost_for`, `LLM_PRESETS`. Pure data + functions.  | `autosdr/llm/pricing.py` (new); `autosdr/llm/__init__.py`                   | additive     | `tests/test_llm_pricing.py` (new): map shape, aliases, `cost_for` known/unknown/zero, presets closed under map     | —          | high |
| 2 | Wire cost into `_record_usage` + `get_usage_snapshot()` so `total_cost_usd` and per-model `cost_usd` accumulate live. | `autosdr/llm/client.py:198-230`                                             | invasive (in-memory only; no persistence) | Extend `tests/test_llm_pricing.py` with a snapshot-counter assertion (or new `tests/test_llm_usage_cost.py`)         | unit 1     | med  |
| 3 | Plumb `total_cost_usd` into `GET /api/status` (replace hardcoded `0.0`).                                              | `autosdr/api/status.py:85-91`                                               | additive     | New / extended `tests/test_status_api.py`: status returns >0 after a stubbed `_record_usage`                         | unit 2     | low  |
| 4 | Add `cost_usd: float \| None` to `LlmCallOut`, computed via `cost_for` on serialisation.                              | `autosdr/api/schemas.py:479-499`; `autosdr/api/llm_calls.py`                | additive     | `tests/test_llm_calls_api.py` (extend or new): known model → numeric, sentinel/unknown → null                        | unit 1     | low  |
| 5 | New `GET /api/llm/presets` endpoint + router. Returns `[{id, label, description, models, pricing_snapshot}]`.         | `autosdr/api/llm.py` (new); `autosdr/api/__init__.py`; `autosdr/api/schemas.py` | additive     | `tests/test_llm_presets_api.py` (new): three presets, every named model in pricing map                               | unit 1     | low  |
| 6 | CLI: `autosdr status` per-model table gains a `cost_usd` column.                                                       | `autosdr/cli.py:289-302`                                                    | additive     | None (existing CLI tests unaffected; manual smoke OK — covered transitively by unit 2)                              | unit 2     | low  |
| 7 | Frontend: mirror `cost_usd` on `LlmCall`; new `Cost` column on `/Logs`.                                                | `frontend/src/lib/types.ts`; `frontend/src/routes/Logs.tsx`                 | additive     | `tsc --noEmit` clean; manual UI smoke                                                                                | unit 4     | low  |
| 8 | Frontend: `api.getLlmPresets()`; preset buttons on Settings → LLM card.                                                | `frontend/src/lib/api.ts`; `frontend/src/lib/types.ts`; `frontend/src/routes/settings/LlmCard.tsx` | additive     | `tsc --noEmit` clean; manual UI smoke (click MAX → fields update)                                                    | unit 5     | med  |

**Sequencing rationale:** Unit 1 (the pricing module) is the riskiest
because every other unit depends on its shape — pricing-map keys,
`cost_for` signature, `LLM_PRESETS` schema. If unit 1's design is
wrong, every later unit ripples. Ship + test it standalone before
anything else binds to it. Unit 2 is next because it's the only
in-memory state-mutation in the chain — getting it right means
units 3–8 are pure plumbing.

**Map back to Scope:**
- `autosdr/llm/pricing.py` → unit 1
- in-memory cost accumulator → unit 2
- Status API → unit 3
- `LlmCallOut.cost_usd` → unit 4
- Presets API → unit 5
- CLI status table → unit 6
- Logs cost column + dashboard pill → unit 7 (Logs); pill via
  unit 3 + the existing `SystemStatus` consumer
- Settings preset buttons → unit 8

**Map back to Success criteria:**
- SC1 (pricing tests) → unit 1
- SC2 (status returns >0) → unit 3, observable via `tests/test_status_api.py`
- SC3 (`cost_usd` on llm-calls rows) → unit 4
- SC4 (`/api/llm/presets` returns three rows) → unit 5
- SC5 (Logs has Cost column, tsc clean) → unit 7
- SC6 (Settings has preset buttons) → unit 8
- SC7 (CLI per-model table has cost) → unit 6
- SC8 (Principle check unchanged) → verified at wrap.

## Implementation log (2026-04-27)

**Status:** done

| # | Unit | Outcome | Evidence |
|---|------|---------|----------|
| 1 | `autosdr/llm/pricing.py` — Gemini pricing map, alias resolver, `cost_for`, `LLM_PRESETS` | done | `tests/test_llm_pricing.py` (18 cases) all pass |
| 2 | Wire cost into `_record_usage` + `get_usage_snapshot()` | done | `tests/test_llm_pricing.py::test_record_usage_accumulates_cost_into_total_and_per_model` |
| 3 | `GET /api/status` reads `total_cost_usd` from snapshot | done | `tests/test_api_smoke.py::test_status_estimated_cost_reflects_in_memory_counter` (`autosdr/api/status.py:88`) |
| 4 | `cost_usd` on `LlmCallOut` (computed at serialisation) | done | `tests/test_llm_calls_api.py` (3 cases: known model, unknown model → null, sentinel zero-token → 0) |
| 5 | `GET /api/llm/presets` endpoint + router | done | `tests/test_llm_presets_api.py` (4 cases: catalog completeness, snapshot date, every role priced, MAX uses one model) |
| 6 | CLI `autosdr status` per-model table gains `est cost (USD)` column; `autosdr logs llm` gains `cost` column | done | `tests/test_cli_llm_cost.py` (2 cases: status header + per-model table; logs llm dash-for-unknown) |
| 7 | Frontend: `LlmCall.cost_usd` mirrored, new `Cost` column on `/Logs`, dashboard LLM-today stat surfaces `est $N.NNNN` | done | `tsc -b --noEmit` clean; `frontend/src/routes/Logs.tsx:155` (column header), `:213-221` (per-row cell); `frontend/src/routes/Dashboard.tsx:259-262` |
| 8 | Frontend: `api.getLlmPresets()`, preset buttons on Settings → LLM card | done | `tsc -b --noEmit` clean; `frontend/src/routes/settings/LlmCard.tsx:84-110` (buttons row + active-state highlight) |

**Final state of success criteria:**
- SC1 (pricing tests): ✓ — `tests/test_llm_pricing.py` covers map shape, alias resolution, `cost_for` known/unknown/zero-token, presets closed under the pricing map.
- SC2 (status returns >0): ✓ — `tests/test_api_smoke.py::test_status_estimated_cost_reflects_in_memory_counter` asserts non-zero after a stubbed `_record_usage`.
- SC3 (`cost_usd` on llm-calls rows): ✓ — `tests/test_llm_calls_api.py` locks numeric for known Gemini, `null` for unknown, `0.0` for sentinel.
- SC4 (`/api/llm/presets` returns three rows with stable ids, every model priced): ✓ — `tests/test_llm_presets_api.py`.
- SC5 (Logs renders `Cost` column, `tsc --noEmit` clean): ✓ — column header `Cost`, per-row cell renders `$N.NNNN` or `—`; frontend typecheck clean.
- SC6 (Settings has three preset buttons; click mutates four model fields): ✓ — `LlmCard.tsx` `PresetButton` wires `form.patch({...preset.models})`; active preset is highlighted.
- SC7 (CLI per-model table prints cost): ✓ — `est cost (USD)` column on `autosdr status`; `cost` column on `autosdr logs llm`. Both backed by `cost_for`.
- SC8 (Principle check unchanged): ✓ — see below.

**Principle check after implementation:**
- Simplicity first: ✓ — one new pricing module (~230 lines incl. docstrings + tests for ~120 lines of code), one new ~45-line API router, no schema migration, no new dependencies.
- Quality over speed: ✓ — adds an honest cost signal without changing send semantics; every cost surface is labelled "estimated" and shows the snapshot date.
- Honest data contracts: ✓ — `cost_usd: float | None` (not `0.0`-as-unknown); zero-token sentinel rows return `0.0` deliberately so the column is summable; pricing map carries `PRICING_VERIFIED_AT`.
- Extensible by design: ✓ — pricing/preset modules are pure data; adding OpenAI/Anthropic later is a sibling map + extra preset entries, no rewrite.
- Human always wins: ✓ — no behaviour change in pipelines; cost is read-only.
- Owner stays in control: ✓ — presets fill the four model fields; operator can edit each field after applying; the `usePatchForm` dirty-tracking + `SaveRow` flow is untouched.

**Pattern-unifier diff-only check:** No new ⚠ / ✗ introduced. All new code uses the blessed choices from `docs/PATTERNS.md`: FastAPI + Pydantic v2 (router + schemas), SQLAlchemy 2.0 (read path on `LlmCall`), LiteLLM via `autosdr/llm/client.py` (no direct SDK calls added), TanStack Query (preset catalog fetch in `LlmCard`), `lib/api.ts` (single API surface), Tailwind v4 + tokens (preset buttons), `usePatchForm` (settings card), Typer + Rich (CLI). No new dependency added in `pyproject.toml` or `frontend/package.json`.

**Follow-ups raised:**
- Cost-by-purpose / cost-by-campaign aggregations (the data is now on every row; the UI surface to consume it isn't here yet — file when an operator asks).
- Cost budgets / spend caps / alerts. Natural next ticket; deliberately out of this scope.
- DB-persisted `cost_usd` column. Not needed today; revisit if a real audit / month-over-month consumer ever appears (additive migration, no read-path change required).
- OpenAI / Anthropic pricing maps + presets. Sibling-map extension when those providers come online in the wizard.

**Open questions still unresolved:** (none)

**Test counts:** 313 backend tests passing (8 new across `test_llm_pricing.py`, `test_llm_calls_api.py`, `test_llm_presets_api.py`, `test_cli_llm_cost.py`, `test_api_smoke.py`); frontend `tsc -b --noEmit` clean.
