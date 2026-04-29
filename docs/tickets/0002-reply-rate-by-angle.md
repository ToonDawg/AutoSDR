# [feature/api+ui] Surface reply-rate per personalisation angle

<!-- TYPE: feature -->
<!-- AREA: api + ui -->

## Problem

The Time-Poor Founder runs a campaign, opens `/Logs`, sees 200 generation
calls, and has no way to answer the only question that matters:
**"Which kind of opener actually gets replies?"**

The data already exists. Every thread persists the analysis-stage
personalisation angle in `Thread.angle` (`autosdr/models.py:283`); it's
populated on first contact in `autosdr/pipeline/_shared.py:180-249` and
re-used on subsequent turns. The angle vocabulary is fixed (per
`autosdr/prompts/analysis.py`): `stale_info`, `weak_online_presence`,
`signature_amenity`, `point_of_difference`, `recent_review_theme`,
`brand_voice`, `category_location_fallback`. Reply detection is also
already in the model — `Message.role = "lead"` on a thread = a reply,
and `CampaignLead.status = "replied"` is propagated downstream (per
`autosdr/pipeline/reply.py:651`).

What's missing is the join + aggregation surface and a UI to read it.
This is the cheapest demo of the audit-log moat called out in
`competitive-landscape.md` ("AutoSDR's moat — already shipped; consider
exposing more of it in the UI").

## Hypothesis

If we expose `(angle, threads_sent, threads_replied, reply_rate)` per
campaign and per workspace, the operator iterates on which angle they
want the analysis prompt to favour, and `autosdr-doc1 § 6` "message
quality ≥ 85%" becomes evidence-backed instead of self-eval-only.
Measured by:
- A working chart on `/Logs` (or a new Insights tab — see Open
  questions) on day 1.
- Within 2 weeks of shipping, the operator either pins this view in
  their dashboard or files a follow-up ticket asking for finer
  groupings (the operator pain is the validation).

## Scope

- New endpoint `GET /api/stats/angle-funnel` on `autosdr/api/stats.py`
  (extending the existing 14-day sparkline file). Query params:
  `campaign_id` (optional), `since` (ISO date, default 30 days ago).
  Response shape:

  ```json
  {
    "since": "2026-03-27T00:00:00Z",
    "campaign_id": "…",
    "rows": [
      {"angle": "stale_info",          "threads": 42, "replied": 7, "won": 1, "lost": 5},
      {"angle": "weak_online_presence","threads": 31, "replied": 4, "won": 0, "lost": 3},
      …
    ]
  }
  ```
- The aggregation is one SQL: `SELECT thread.angle, COUNT(*),
  COUNT(DISTINCT thread.id WHERE replied), …` joined to
  `campaign_lead` for campaign filtering. A single query, no N+1.
  Treat NULL `angle` as `"unknown"` (defensive; legacy threads).
- New `AngleFunnelOut` Pydantic schema in `autosdr/api/schemas.py`,
  mirrored as a TS type in `frontend/src/lib/types.ts`.
- New panel on `/Logs` route (`frontend/src/routes/Logs.tsx`): a
  collapsible "By angle" section above the call list. Renders the rows
  as a horizontal bar chart (use existing chart primitives — repo has
  Recharts? — see Open questions) with `replied / threads` ratio and
  the absolute counts on hover. Default time window: last 30 days.
- Campaign-scoped variant: the same panel embedded on
  `frontend/src/routes/CampaignDetail.tsx` filtered to that campaign.
- CLI: `autosdr logs angles [--campaign <id>] [--since DAYS]` — same
  data, plain text. (Cheap; one helper in `autosdr/cli.py`.)

## Out of scope

- Drafting / generation-level scoring (eval-pass-rate per angle). That's
  a different aggregation against `LlmCall` and adds value but
  expands scope; do as a follow-up ticket if the angle funnel proves
  useful.
- Per-prompt-version comparison (`generation@v6` vs `v7`). Useful but
  needs prompt-version stamping on `Thread`, which doesn't exist
  today. Follow-up.
- Statistical significance tests (chi-squared etc.). N is small for a
  single-operator workspace — comparing 4-of-31 to 7-of-42 visually is
  enough. Don't pretend to do statistics.
- Surfacing the *evidence text* the analysis prompt cited per angle.
  Useful for prompt iteration; do as a Logs-row expansion item later.

## Success criteria

- New `tests/test_stats_angle_funnel.py`:
  - Empty workspace → empty `rows`.
  - Mixed angles + a NULL angle → NULL bucketed as `"unknown"`.
  - Replies counted via `MessageRole.LEAD` existence on the thread, not
    via `CampaignLead.status` alone (the former is the more honest
    signal — status can lag).
  - Campaign-scoped query excludes other campaigns.
- `/Logs` page renders the panel without an extra round-trip per row
  (single endpoint call).
- The panel honours the existing thread/campaign URL params (deep-link
  from `ThreadDetail.tsx → /logs?campaign=...` shows the campaign-scoped
  funnel).
- The chart is readable when one angle dominates (e.g. 90% threads
  → other bars don't disappear). Use a min-bar-width.

## Effort & risk

- **Size:** S (1–2 days)
- **Touched surfaces:** `autosdr/api/stats.py`, `autosdr/api/schemas.py`
  (additive), `autosdr/cli.py` (one new subcommand),
  `frontend/src/lib/types.ts`, `frontend/src/lib/api.ts`,
  `frontend/src/routes/Logs.tsx`, `frontend/src/routes/CampaignDetail.tsx`.
- **Change class:** additive everywhere.
- **Risks:**
  - SQL: SQLite's `COUNT(DISTINCT … WHERE …)` syntax differs slightly
    from Postgres. Use a sub-select for the "replied" count to keep
    behaviour identical across both. (Cheap.)
  - Chart library: if the frontend doesn't already have one (search
    the package.json — see Open questions), don't add Recharts/Victory
    just for this; render a CSS bar.
  - Cardinality: angle vocabulary is fixed (~ 7 values), so no group
    explosion.

## Open questions

- ~~Where does the panel live? `/Logs` audit-log tab vs. a new
  `/Insights` route.~~ → resolved: `/Logs`.
- ~~Chart primitive: does the frontend have Recharts / Visx / `<svg>`
  already?~~ → resolved: package.json has no chart lib; render with
  `<div>` + width percentages.
- ~~Time window default: 30 days vs. campaign-lifetime.~~ → resolved:
  30 days workspace-scoped, no time filter when scoped to a campaign
  (campaign-lifetime).
- ~~Do we count threads currently in HITL as "replied"?~~ → resolved:
  yes — counted by `MessageRole.LEAD` existence on the thread, not by
  `CampaignLead.status`.
- **NEW (raised at implementation)** ~~Where does the discrete
  `angle_type` value live for `GROUP BY`? `Thread.angle` actually stores
  the freeform 2-3 sentence angle text, not the enum. Two paths:
  (A) add `Thread.angle_type` column + populate at first-contact analysis;
  (B) extract from `LlmCall.response_parsed` via `json_extract` per
  query.~~ → resolved: (A). See Resolved questions block below.

## Resolved questions (2026-04-26)

### Resolved: angle_type-storage

**Architect:** Add nullable `Thread.angle_type` populated next to
`thread.angle` in outreach. Aggregation becomes a portable `GROUP BY`.
Backfill is unnecessary — legacy NULL rows bucket as `"unknown"` per
the ticket's existing rule.
**Skeptic:** Start with (B) — the enum is already on `LlmCall`;
denormalising creates a divergence failure mode. Only add the column if
profiling justifies it.
**Pragmatist:** (A). Funnel grain is the thread; the column freezes
the bucket at the point of personalisation. (B) depends on an
English-only invariant ("one analysis call per thread") that the DB
doesn't enforce — re-analysis or retries break it silently.
**Critic:** (A). Fix the model (the ticket conflated `angle` with
`angle_type`); don't paper over the bug with a clever extract that
hides the same semantic mistake behind a join.

**Decision:** (A) — additive `Thread.angle_type` column populated at
write time. No historical backfill: legacy rows become `"unknown"`.
**Strongest dissent:** Skeptic's "single source of truth on
`LlmCall`". Real concern, but Pragmatist + Critic both observed that
the 1:1 invariant is not DB-enforced and would silently break the
funnel under future re-analysis.
**Confidence:** medium-high.
**Why this is acceptable:** Schema cost is one nullable column +
one entry in `_ADDITIVE_COLUMN_MIGRATIONS`. Drift mitigated by writing
`angle_type` at the same site as `angle` (single write path) plus a
test asserting both fields persist.

## Mini plan (2026-04-26)

| # | Unit | Files | Change class | Tests | Depends on | Risk |
|---|------|-------|--------------|-------|------------|------|
| 1 | Add `Thread.angle_type` column + additive migration | `autosdr/models.py`, `autosdr/db.py` | additive (schema) | extend `tests/test_outreach_pipeline.py` (covers persistence in unit 2) | — | high |
| 2 | Populate `thread.angle_type` at first-contact analysis | `autosdr/pipeline/outreach.py` | additive | extend `tests/test_outreach_pipeline.py` | unit 1 | med |
| 3 | New `AngleFunnelOut` schema + `GET /api/stats/angle-funnel` | `autosdr/api/stats.py`, `autosdr/api/schemas.py` | additive | new `tests/test_stats_angle_funnel.py` | unit 1 | med |
| 4 | CLI `autosdr logs angles [--campaign] [--since DAYS]` | `autosdr/cli.py` | additive | new `tests/test_cli_logs_angles.py` | unit 3 | low |
| 5 | TS types + api client + `/Logs` "By angle" panel | `frontend/src/lib/types.ts`, `frontend/src/lib/api.ts`, `frontend/src/routes/Logs.tsx` | additive | `tsc --noEmit` | unit 3 | low |
| 6 | Embed campaign-scoped panel on `CampaignDetail.tsx` | `frontend/src/routes/CampaignDetail.tsx` | additive | `tsc --noEmit` | unit 5 | low |

**Sequencing rationale:** schema migration is the highest-risk unit — if
the additive migration breaks an existing dev DB, everything else is
moot. Ship that first; data-population, API, CLI, and UI fan out from
there.

**Map back to Scope:**
- Endpoint `GET /api/stats/angle-funnel` → unit 3
- Single-SQL aggregation with NULL → `"unknown"` → unit 3
- `AngleFunnelOut` Pydantic + TS mirror → unit 3 + unit 5
- Panel on `/Logs` → unit 5
- Campaign-scoped variant on `CampaignDetail.tsx` → unit 6
- CLI `autosdr logs angles` → unit 4

**Map back to Success criteria:**
- SC1 (test cases — empty/mixed/NULL/MessageRole.LEAD/campaign-scoped)
  → unit 3, observable via `tests/test_stats_angle_funnel.py`.
- SC2 (single round-trip on `/Logs`) → unit 5, observable via the
  `useQuery` call shape in the panel component.
- SC3 (panel honours `thread`/`campaign` URL params) → unit 5,
  observable via the `useSearchParams()` plumbing already on `/Logs`.
- SC4 (chart readable when one angle dominates) → unit 5, observable
  via the bar-width clamp (min 4 %).

## Principle check

- Simplicity first: ✓
- Quality over speed: ✓ (visibility for prompt iteration)
- Honest data contracts: ✓ (NULL → `"unknown"`, no synthetic angles)
- Extensible by design: ✓ (endpoint shape generalises to per-prompt
  comparisons in v2)
- Human always wins: ✓ (read-only)
- Owner stays in control: ✓

## Links

- Spec: `autosdr-doc1-product-overview.md § 6` — message quality
  metric (currently self-eval-only).
- Spec: `autosdr-doc3-ai-messaging.md § analysis` (per
  `autosdr/prompts/analysis.py`) — angle vocabulary.
- Architecture: `ARCHITECTURE.md § 7.1` — analysis stage.
- Code: `autosdr/models.py:283` (`Thread.angle`),
  `autosdr/pipeline/_shared.py:180-249` (angle persistence),
  `autosdr/api/stats.py` (current sparkline endpoint),
  `frontend/src/routes/Logs.tsx`.
- Competitor: `competitive-landscape.md` — "Audit log of every AI
  decision" row.
- Roadmap: `docs/ROADMAP.md` → Next → row 2.

## Dependencies

- Blocks: future "A/B compare two angles per lead" ticket (Later
  list).
- Blocked by: nothing.
- Related: 0003 (per-campaign funnel) — share the time-series
  endpoint shape for consistency.

## Implementation log (2026-04-26)

**Status:** done

| # | Unit | Outcome | Evidence |
|---|------|---------|----------|
| 1 | Add `Thread.angle_type` column + additive migration | done | `autosdr/models.py` (`Thread.angle_type`), `autosdr/db.py` (`_ADDITIVE_COLUMN_MIGRATIONS` row); existing 263 tests pass against fresh + migrated SQLite |
| 2 | Populate `thread.angle_type` at first-contact analysis | done | `autosdr/pipeline/outreach.py` (`thread.angle_type = …` next to `thread.angle`); `tests/test_outreach_pipeline.py::test_outreach_happy_path` (asserts `"review_theme"`) + `…::test_outreach_persists_fallback_angle_type_when_llm_omits_it` |
| 3 | New `AngleFunnelOut` schema + `GET /api/stats/angle-funnel` | done | `autosdr/api/stats.py::angle_funnel`, `autosdr/api/schemas.py::AngleFunnelOut/Row`; `tests/test_stats_angle_funnel.py` (8 cases — empty/mixed/NULL→"unknown"/MessageRole.LEAD/campaign-scoped/won-lost/since_days/unknown campaign) |
| 4 | CLI `autosdr logs angles [--campaign] [--since DAYS]` | done | `autosdr/cli.py::logs_angles`; `tests/test_cli_logs_angles.py` (3 cases — empty/workspace/campaign-scoped) |
| 5 | TS types + api client + `/Logs` "By angle" panel | done | `frontend/src/lib/types.ts` (`AngleFunnel*`), `frontend/src/lib/api.ts::getAngleFunnel`, `frontend/src/components/domain/AngleFunnelPanel.tsx`, `frontend/src/routes/Logs.tsx` (panel embedded above filter tabs); `tsc --noEmit` clean |
| 6 | Embed campaign-scoped panel on `CampaignDetail.tsx` | done | `frontend/src/routes/CampaignDetail.tsx` (panel slotted between stat strip and Manual kick-off); `tsc --noEmit` clean |

**Final state of success criteria:**
- SC1 (test cases — empty / mixed / NULL→"unknown" / `MessageRole.LEAD` / campaign-scoped):
  ✓ — `tests/test_stats_angle_funnel.py` covers every case explicitly,
  including the campaign-scoped exclusion test and the
  `MessageRole.LEAD` vs `CampaignLeadStatus` distinction.
- SC2 (single round-trip on `/Logs`):
  ✓ — `AngleFunnelPanel` issues exactly one `useQuery` (`api.getAngleFunnel`)
  per scope; the panel doesn't fan out per-row.
- SC3 (panel honours `thread`/`campaign` URL params): ✓ — `Logs.tsx`
  passes `campaignFilter` from `useSearchParams()` straight into
  `<AngleFunnelPanel campaignId={…} />`. Deep-link from
  `ThreadDetail → /logs?campaign=…` shows the campaign-scoped funnel.
- SC4 (chart readable when one angle dominates): ✓ —
  `AngleFunnelPanel.clampWidth` floors any non-zero ratio at 4 % so
  long-tail bars never collapse to 1 px. True 0 % stays 0.

**Principle check after implementation:**
- Simplicity first: ✓ — no chart lib added; bars are CSS
  `<div style={{ width: '%' }}>`. One additive nullable column.
- Quality over speed: ✓ — operator can finally answer "which opener
  works?" with one number per bucket.
- Honest data contracts: ✓ — NULL bucketed as `"unknown"`; LLM-omitted
  `angle_type` defaulted to `"fallback"` (a real bucket from the
  analysis vocabulary, not a synthetic label); `replied` counted from
  `Message.role = lead` not `CampaignLead.status` (the more honest
  signal).
- Extensible by design: ✓ — endpoint shape (`{since, campaign_id, rows}`)
  generalises to per-prompt-version comparisons in v2 without breaking
  the wire.
- Human always wins: ✓ — read-only.
- Owner stays in control: ✓.

**Follow-ups raised:** (none) — out-of-scope items
(eval-pass-rate per angle, per-prompt-version compare, evidence-text
expansion) remain in the ticket's "Out of scope" section as documented
follow-up candidates.

**Open questions still unresolved:** (none) — all five resolved during
pre-flight (four anchored, one councilled — see Resolved questions
block above).
