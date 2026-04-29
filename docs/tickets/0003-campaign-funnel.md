# [feature/api+ui] Per-campaign funnel: queued → sent → replied → won/lost

<!-- TYPE: feature -->
<!-- AREA: api + ui -->

## Problem

The Time-Poor Founder activates a campaign, comes back two days later, and
asks "is this working?". The Dashboard answers with a 14-day daily-send
sparkline (`autosdr/api/stats.py:18-50`) — useful for "is the scheduler
healthy" but useless for "is the campaign converting". `CampaignOut`
already exposes `lead_count`, `contacted_count`, `replied_count`,
`won_count`, `sent_24h` (`autosdr/api/campaigns.py:116-137`), but:

- `lost_count` and `paused_for_hitl_count` and `skipped_count` aren't
  exposed even though `_campaign_totals_bulk` (`autosdr/api/campaigns.py:87-113`)
  already computes them.
- There's no time-series — operator can't see "we sent 50 yesterday and
  got 6 replies; today we sent 50 and got 1" without inspecting the DB.
- The CampaignDetail UI surface (`frontend/src/routes/CampaignDetail.tsx`)
  shows a stat strip + thread table but no funnel viz.

The fix is mostly *expose what's already there*, plus one new endpoint
for daily counts. Sequenced after 0002 because the angle funnel + the
campaign funnel share an aggregation shape and we want to land them
consistently.

## Hypothesis

If we expose the full funnel + a 14-day time series per campaign, the
operator answers "is this working?" in < 5 seconds without leaving the
campaign page, and any decision to pause/iterate is grounded in
visible numbers rather than gut feel. Measured by:
- The `CampaignOut` response carries every status bucket the database
  has (`queued`, `sending`, `paused_for_hitl`, `contacted`, `replied`,
  `won`, `lost`, `skipped`).
- A new `/api/campaigns/{id}/timeseries?days=14` returns daily counts
  of (sends, replies, wins, losses) and renders as a stacked / grouped
  chart on `CampaignDetail.tsx`.

## Scope

- Extend `CampaignOut` (`autosdr/api/schemas.py:206` and the matching
  TS in `frontend/src/lib/types.ts`) with the missing buckets:
  `queued_count`, `sending_count`, `paused_for_hitl_count`, `lost_count`,
  `skipped_count`, `closed_opt_out_count` (the last one is contingent
  on 0001 landing — see Dependencies). Existing `_campaign_totals_bulk`
  already returns these.
- Recompute `contacted_count` / `replied_count` to NOT double-count
  closed states. Today (`autosdr/api/campaigns.py:133-134`):
  - `contacted_count = contacted + replied + won + lost` — this rolls up
    "anything past first send", which makes "still contacted" indistinguishable
    from "won". Operator-visible rename or split — see Open questions.
- New endpoint `GET /api/campaigns/{id}/timeseries?days=14`:

  ```json
  {
    "days": 14,
    "buckets": [
      {"date": "2026-04-13", "sent": 50, "replied": 4, "won": 1, "lost": 2},
      …
    ]
  }
  ```

  Implementation: one query against `Message` joined to `Thread` joined to
  `CampaignLead` filtered by `campaign_id`, grouped by date. "Sent" =
  `MessageRole.AI`. "Replied" = first `MessageRole.LEAD` per thread per
  day. "Won" / "Lost" = `Thread.updated_at` filtered to terminal
  status (status doesn't carry timestamps today — see Risks).
- New funnel viz on `CampaignDetail.tsx`:
  - Horizontal stacked bar showing the proportions: `queued | sending |
    paused_hitl | contacted | replied | won | lost | skipped`.
  - 14-day grouped bar chart underneath: send vs reply vs won vs lost.
  - Mobile-style "stat strip" stays for the headline numbers.
- CLI: extend `autosdr status` to optionally render a per-campaign
  funnel: `autosdr status --campaign <id>`.

## Out of scope

- Cost tracking ($/lead, $/reply). The LLM call log has token counts
  (`autosdr/models.py:359-361`) but no cost; pricing is per-model and
  we don't track that. Follow-up.
- Per-day-of-week / per-hour-of-day "best send time" heatmap. The
  audit log has the data but this is feature-creep on a "is it
  working" view.
- Cohort analysis (e.g. "import batch A vs batch B"). Single-operator;
  not worth the schema cost yet.
- Goal-specific KPIs (e.g. for a "Book a call" goal, count actual
  bookings). The goal is freeform text today (`Campaign.goal`); won/lost
  is the deepest signal we can measure without integrations.

## Success criteria

- `tests/test_campaign_api.py` updated to assert every status bucket
  is exposed and matches the underlying DB.
- New `tests/test_campaign_timeseries.py` covers:
  - Empty campaign → 14 zero rows.
  - Replies and wins on the same day are both counted (one isn't
    double-attributed).
  - Date boundaries: a message at 23:59 UTC vs 00:01 UTC lands on the
    correct day.
- `CampaignOut` is byte-stable for downstream consumers (only adds new
  optional fields; doesn't rename existing).
- The funnel bar is visually correct when totals = 0 (gracefully empty,
  not a 100%-wide "skipped" bar).
- The 14-day chart deep-links from a click on a day → filtered
  `/Logs?campaign=<id>&date=YYYY-MM-DD` (decision needed — see Open
  questions).

## Effort & risk

- **Size:** S (1–2 days)
- **Touched surfaces:** `autosdr/api/schemas.py`, `autosdr/api/campaigns.py`
  (additive), `autosdr/api/stats.py` or new endpoint group (decision
  below), `autosdr/cli.py`, `frontend/src/lib/types.ts`,
  `frontend/src/lib/api.ts`, `frontend/src/routes/CampaignDetail.tsx`.
- **Change class:** additive on the API; the
  `contacted_count`/`replied_count` semantics change is a breaking
  read for any consumer who relied on the rolled-up shape — see Open
  questions.
- **Risks:**
  - "Won at" / "Lost at" timestamps are not stored separately —
    `Thread.updated_at` is the proxy. If a thread is touched after
    closing (e.g. an inbound message captured against a paused
    thread) `updated_at` shifts. Acceptable for v0; document.
  - The 14-day query joins three tables — index on
    `Message(thread_id, created_at)` already exists
    (`autosdr/models.py:305`). Confirm `EXPLAIN` is sane on a workspace
    with 10k messages before shipping.
  - Stacked-bar UX with 8 statuses can get messy. Reuse the colour
    semantics of `ThreadStatusBadge` in
    `frontend/src/components/domain/ThreadStatusBadge.tsx` for
    consistency.

## Open questions

- ~~Where does `timeseries` live — `/api/stats/...` or
  `/api/campaigns/{id}/timeseries`?~~ **Resolved 2026-04-26 → resource-bound** — see Resolved questions below.
- ~~Rename `contacted_count` to `active_contacted_count` (only
  `CampaignLeadStatus.CONTACTED`) and add a separate `engaged_count`
  for "anything past first send"?~~ **Resolved 2026-04-26 → rename in place, drop rollups** — see Resolved questions.
- ~~Day-click drill-down: navigate to `/Logs?campaign=…&date=…`…~~ **Resolved 2026-04-26 → tooltip-only, no click nav in v0** — see Resolved questions.
- ~~Should we count a "queued" lead in funnel proportions, or only "ever attempted"?~~ **Resolved 2026-04-26 → include queued** — pipeline health.

## Resolved questions (2026-04-26)

### Resolved: timeseries-endpoint-location

**Architect:** Bind the endpoint to the campaign resource — it has no workspace-level meaning.
**Skeptic / Pragmatist / Critic:** (no council — obvious answer once you read the linked code).

**Decision:** `GET /api/campaigns/{campaign_id}/timeseries?days=14`. Lives in `autosdr/api/campaigns.py`, not `autosdr/api/stats.py`.
**Strongest dissent:** None.
**Confidence:** high
**Why this is acceptable:** Resource-bound URLs are the standard for "data scoped to one entity"; `stats.py` is reserved for workspace-level aggregates.

### Resolved: contacted-count-semantics

**Architect (initial):** Add new precise buckets but keep `contacted_count`/`replied_count` as legacy rolled-up fields for backward compat — minimise blast radius.
**Skeptic:** Names are the contract — keeping misleading aliases hardens tech debt; "no API versioning" is an argument FOR fixing now, not against.
**Pragmatist:** Cost is bounded; one consumer, one diff. The trap is additive-only — it leaves the misleading names as the path of least resistance.
**Critic:** Half-measures ship a yellow principle; one known consumer is the cheapest moment to fix English semantics.

**Decision:** Replace the rollups with bucket-precise fields. `CampaignOut` exposes every `CampaignLeadStatus` bucket using its literal name: `queued_count`, `sending_count`, `paused_for_hitl_count`, `contacted_count` (= only the `CONTACTED` bucket), `replied_count` (= only the `REPLIED` bucket), `won_count`, `lost_count`, `skipped_count`. No `engaged_count` rollup — UI sums on demand. Frontend `Stat` strip migrates in the same diff.
**Strongest dissent:** None — three voices aligned against the initial additive-only position. Architect updated.
**Confidence:** high
**Why this is acceptable:** Single bundled frontend consumer; renames coordinated in one diff; principle "honest contracts" flips ⚠ → ✓ exactly because the misleading rollups are gone, not aliased.

### Resolved: day-click-drill-down

**Architect:** Don't deep-link day clicks in v0; tooltip per bar group ("4 sent, 2 replied, 1 won, 0 lost"). File a follow-up for a campaign+date messages/activity view.
**Skeptic:** `/Logs` shows LLM calls, not messages — deep-linking there is a category error.
**Pragmatist:** Tooltips answer "is this working?" without committing to a half-right navigation story.
**Critic:** Adding `?date=` to `/api/llm-calls` front-loads the wrong abstraction; calendar-day filtering on LLM calls drags timezone semantics in too.

**Decision:** Tooltip-only. No click navigation in this ticket. Bar groups expose the per-day breakdown via `title=` so a hover yields the absolute counts. Follow-up ticket recorded.
**Strongest dissent:** "If you must ship a link, use B (`?campaign=…` only)" — rejected because a tooltip is strictly better than a link that lands on the wrong content type.
**Confidence:** high
**Why this is acceptable:** The right drill-down surface (campaign+date-scoped messages/activity) doesn't exist yet; better to build it correctly later than wire a misleading shortcut now.

### Resolved: queued-in-funnel-proportions

**Decision:** Include queued. The horizontal stacked bar is "pipeline health" — showing how much runway the campaign has left is a feature, not noise.
**Confidence:** high

### Out-of-scope follow-up: closed_opt_out_count

The original Scope listed `closed_opt_out_count` as contingent on 0001 landing a `CampaignLeadStatus.CLOSED_OPT_OUT` bucket. 0001 shipped with the DNC flag at the `Lead` level (`Lead.do_not_contact_at`), not as a new `CampaignLeadStatus`. Computing an opt-out count would require joining `CampaignLead → Lead.do_not_contact_at IS NOT NULL`. Filed as a follow-up rather than expanding scope.

## Mini plan (2026-04-26)

| # | Unit | Files | Change class | Tests | Depends on | Risk |
|---|------|-------|--------------|-------|------------|------|
| 1 | Add `GET /api/campaigns/{id}/timeseries?days=14` returning per-day `{sent, replied, won, lost}` rows | `autosdr/api/campaigns.py`, `autosdr/api/schemas.py` | additive | new `tests/test_campaign_timeseries.py` (empty, replies+wins same day, date boundary) | — | high |
| 2 | Replace rolled-up `contacted_count`/`replied_count` with bucket-precise fields; expose every `CampaignLeadStatus` bucket on `CampaignOut` | `autosdr/api/schemas.py`, `autosdr/api/campaigns.py` | breaking (single consumer) | extend `tests/test_campaign_api.py` to assert every bucket round-trips | unit 1 | med |
| 3 | Mirror the schema change in TypeScript + migrate `CampaignDetail` `Stat` strip to the new fields | `frontend/src/lib/types.ts`, `frontend/src/routes/CampaignDetail.tsx` | invasive | `tsc --noEmit` clean | unit 2 | low |
| 4 | New `CampaignTimeseriesPanel` component on `CampaignDetail` (horizontal stacked-bar funnel + 14-day grouped bar chart with per-day tooltips) | new `frontend/src/components/domain/CampaignTimeseriesPanel.tsx`, `frontend/src/lib/api.ts`, `frontend/src/routes/CampaignDetail.tsx` | additive | `tsc --noEmit` clean | unit 1, 3 | med |
| 5 | Extend `autosdr status` with `--campaign <id>` to render the per-campaign funnel; reuse the API handler so CLI/HTTP can't drift | `autosdr/cli.py` | additive | new `tests/test_cli_status_campaign.py` | unit 2 | low |

**Sequencing rationale:** Unit 1 is the highest-risk piece — the timeseries query joins `Message → Thread → CampaignLead` and has to handle date-boundary semantics (UTC), terminal-status timestamps (`Thread.updated_at`), and the "replies and wins on the same day are both counted" requirement. If the query shape doesn't work cleanly, the rest of the plan changes. Schema-precise renames (unit 2) come before consumers (units 3-5) because we want one breaking diff, not a stuttering one. Frontend types follow schema (unit 3). The chart UI is the most visible piece and goes last so it can mount on a stable contract.

**Map back to Scope:**
- "Extend `CampaignOut` with the missing buckets" → unit 2
- "Recompute `contacted_count` / `replied_count` to NOT double-count" → unit 2 (going further: replace with bucket-precise)
- "New endpoint `GET /api/campaigns/{id}/timeseries?days=14`" → unit 1
- "New funnel viz on `CampaignDetail.tsx`" → unit 4
- "CLI: extend `autosdr status` to optionally render a per-campaign funnel" → unit 5

**Map back to Success criteria:**
- "tests/test_campaign_api.py updated to assert every status bucket is exposed and matches the underlying DB" → unit 2, observable via test name `test_campaign_out_exposes_every_bucket`
- "New tests/test_campaign_timeseries.py covers: empty, replies+wins same day, date boundaries" → unit 1, observable via test names in that file
- "`CampaignOut` is byte-stable for downstream consumers (only adds new optional fields; doesn't rename existing)" — **revised by OQ2 council**: this success criterion is explicitly overridden — we DO rename. The principle check now flips to ✓ on "honest contracts" *because* the rename happens. Frontend migrates in same diff.
- "The funnel bar is visually correct when totals = 0 (gracefully empty, not a 100%-wide 'skipped' bar)" → unit 4, observable via empty-state branch in component
- "The 14-day chart deep-links from a click on a day → filtered `/Logs?…`" — **revised by OQ3 council**: tooltip instead of deep-link. Observable via `title=` attribute on each bar group rendering the per-day counts.

**Blessed patterns each unit conforms to** (per `docs/PATTERNS.md`):
- Unit 1: FastAPI router + Pydantic schemas + SQLAlchemy 2.0 (`select` + `case`) + pytest (existing baselines).
- Unit 2: Pydantic v2 + SQLAlchemy 2.0 + pytest.
- Unit 3: Mirror `schemas.py` in `lib/types.ts` (mandatory). Tailwind-only styling. `cn` util.
- Unit 4: TanStack Query for fetching, `lib/api.ts` for the surface, `domain/` for the component. Tailwind. Inline `style={{width: …%}}` only for runtime-dynamic bar widths (allowed by PATTERNS).
- Unit 5: Typer (existing baseline) + rich.Table.

No new dependencies needed. No new cross-cutting concerns introduced. `pattern-unifier` pre-pick mode not required.

## Principle check

- Simplicity first: ✓ (mostly exposing what exists)
- Quality over speed: ✓ (operator iteration loop)
- Honest data contracts: ⚠ (the rename is the only soft-breaking
  change; mitigated by version note)
- Extensible by design: ✓ (timeseries endpoint shape can grow)
- Human always wins: ✓ (read-only)
- Owner stays in control: ✓

## Links

- Spec: `autosdr-doc1-product-overview.md § 9` — start/stop controls
  reference per-campaign visibility.
- Architecture: `ARCHITECTURE.md § 13` — observability surfaces.
- Code: `autosdr/api/campaigns.py:87-137`,
  `autosdr/api/stats.py:18-50`, `autosdr/models.py:74-82` (status
  vocab), `frontend/src/routes/CampaignDetail.tsx`.
- Roadmap: `docs/ROADMAP.md` → Next → row 3.

## Dependencies

- Blocks: nothing immediately; useful pre-req for any future "campaign
  health alerts".
- Blocked by: 0001 if you want `closed_opt_out_count` exposed (the bucket
  doesn't exist until that ticket lands; ship without it and add later
  if 0001 isn't ready).
- Related: 0002 (angle funnel) — same aggregation shape; do them in
  this order.

## Implementation log (2026-04-26)

**Status:** done

| # | Unit | Outcome | Evidence |
|---|------|---------|----------|
| 1 | `GET /api/campaigns/{id}/timeseries?days=14` returning per-day `{sent, replied, won, lost}` rows | done | `tests/test_campaign_timeseries.py` — 8 cases pass (`test_empty_campaign_returns_days_zero_rows`, `test_replies_and_wins_on_same_day_both_counted`, `test_date_boundary_2359_vs_0001_lands_on_correct_day`, plus reply-dedup, cross-campaign isolation, `days` clamping, unknown-id 404). Handler at `autosdr/api/campaigns.py` `campaign_timeseries`. |
| 2 | Replace rolled-up `contacted_count`/`replied_count` with bucket-precise `CampaignOut` fields (one per `CampaignLeadStatus`) | done | `tests/test_campaign_api.py::test_campaign_out_exposes_every_status_bucket` seeds one lead per bucket and asserts each `*_count == 1` and `lead_count == 8`. Schema: `autosdr/api/schemas.py` `CampaignOut`; mapper: `autosdr/api/campaigns.py` `_build_out`. |
| 3 | Mirror schema in `frontend/src/lib/types.ts` + migrate `Campaigns.tsx` and `CampaignDetail.tsx` Stat strip to compute rollups on demand | done | `npx tsc --noEmit` clean. `Campaigns.tsx::CampaignRow` + `CampaignDetail.tsx` Stat strip both use the new `*_count` fields and rollup math is inlined at the call sites. |
| 4 | New `CampaignTimeseriesPanel` (horizontal stacked-bar funnel + 14-day grouped bar chart, per-day `<title>` tooltips) wired into `CampaignDetail` | done | `frontend/src/components/domain/CampaignTimeseriesPanel.tsx`. Uses TanStack Query via `api.getCampaignTimeseries`. Empty-state branch (`totals === 0`) renders a flat empty bar instead of a 100%-wide skipped slice. CSS-variable colours (no hex), inline `style` only for runtime-dynamic widths/colours (PATTERNS-allowed). `tsc --noEmit` clean. |
| 5 | `autosdr status --campaign <id> [--days 14]` reuses the API handler so CLI/HTTP can't drift on the funnel maths | done | `tests/test_cli_status_campaign.py` — 3 cases pass (`test_status_default_workspace_summary_unchanged`, `test_status_campaign_flag_renders_funnel_table`, `test_status_campaign_unknown_id_exits_nonzero`). Implementation: `autosdr/cli.py` `status` + `_render_campaign_timeseries`. |

**Final state of success criteria:**
- `tests/test_campaign_api.py` asserts every status bucket: ✓ — `test_campaign_out_exposes_every_status_bucket` walks every value of `CampaignLeadStatus` and matches it to a field on `CampaignOut`.
- `tests/test_campaign_timeseries.py` covers empty / replies+wins same day / date boundary: ✓ — 8 cases pass; date-boundary test uses 23:59:30 UTC and 00:00:30 UTC and asserts they land on adjacent days.
- `CampaignOut` byte-stable: ✗ — **revised by OQ2 council**. We renamed `contacted_count` (rollup) → `contacted_count` (bucket-precise) and added the missing buckets, with frontend migrated in the same diff. Honest-contracts principle flips ⚠ → ✓.
- Funnel bar visually correct when totals = 0: ✓ — `CampaignTimeseriesPanel.tsx` empty-state branch returns "No leads assigned yet…" instead of any segments.
- 14-day chart deep-links on day click: ✗ — **revised by OQ3 council**. Tooltip-only via `<title>` on each day group. Follow-up filed for a campaign+date messages/activity view.

**Principle check after implementation:**
- Simplicity first: ✓ — no new deps, exposed fields that already existed; reused the API handler in the CLI.
- Quality over speed: ✓ — schema-precise contract; tests cover the boundary cases the ticket called out.
- Honest data contracts: ✓ (was ⚠ in the original ticket — flipped because misleading rollups are gone, not aliased).
- Extensible by design: ✓ — `CampaignTimeseriesOut` shape can grow (more series, custom windows, rolling windows).
- Human always wins: ✓ — read-only views; no behaviour change on outreach.
- Owner stays in control: ✓ — read-only views; no behaviour change on outreach.

**Pattern-unifier diff scan:** no new ⚠ or ✗ introduced (checked the changed files for raw `fetch` outside `lib/api.ts`, `axios`, alt routers, alt date libs, alt icon libs, alt state libs, hex colours in the new component, ORM/HTTP/CLI imports outside their boundaries — all clean).

**Follow-ups raised:**
- `closed_opt_out_count` on `CampaignOut`. The original ticket made it contingent on 0001 introducing a `CampaignLeadStatus.CLOSED_OPT_OUT` bucket; 0001 instead implemented DNC at the `Lead` level (`Lead.do_not_contact_at`). Computing the count requires a `CampaignLead → Lead.do_not_contact_at IS NOT NULL` join. Not in scope here.
- Per-day drill-down view (campaign + date scoped messages/activity). The right surface for OQ3's deep-link doesn't exist yet — `/Logs` shows LLM calls, not messages. File a ticket once the Inbox/Threads UI gains a date-scoped filter.

**Open questions still unresolved:** (none)

**Caveat — dirty working tree at start.** This session began with the 0002 changes still uncommitted (per the operating-loop pre-flight). Files touched by 0003 also touched by 0002: `autosdr/api/schemas.py`, `autosdr/api/campaigns.py` (the new endpoint sits below 0002's angle-funnel handler), `autosdr/cli.py`, `frontend/src/lib/types.ts`, `frontend/src/lib/api.ts`, `frontend/src/routes/CampaignDetail.tsx`. The two ticket diffs are layered in the working tree; the user will want to separate them at commit time. Files unique to 0003 (no overlap with 0002): `frontend/src/components/domain/CampaignTimeseriesPanel.tsx`, `frontend/src/routes/Campaigns.tsx`, `tests/test_campaign_timeseries.py`, `tests/test_campaign_api.py` (extension), `tests/test_cli_status_campaign.py`.

