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

- Where does the panel live? `/Logs` audit-log tab vs. a new
  `/Insights` route. **Recommend `/Logs`** — keeps the audit-log moat
  surface coherent. Decision.
- Chart primitive: does the frontend have Recharts / Visx / `<svg>`
  already? If yes, reuse. If not, render with `<div>` + width
  percentages — no new dep.
- Time window default: 30 days vs. campaign-lifetime. Recommend
  campaign-lifetime when scoped to a campaign, 30 days when scoped to
  the workspace.
- Do we count threads currently in HITL as "replied"? The lead *did*
  reply, even though we haven't sent back yet. Recommend yes — the
  funnel is "did the message land" not "did we close".

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
