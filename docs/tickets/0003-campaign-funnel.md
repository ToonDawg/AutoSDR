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

- Where does `timeseries` live — `/api/stats/...` or
  `/api/campaigns/{id}/timeseries`? Recommend
  `/api/campaigns/{id}/timeseries` so it's discoverable from the
  resource it belongs to. Decision.
- Rename `contacted_count` to `active_contacted_count` (only
  `CampaignLeadStatus.CONTACTED`) and add a separate `engaged_count`
  for "anything past first send"? **Recommend split** — the rolled-up
  shape mis-leads operators. This is a soft-breaking change for
  whoever reads the API; acceptable since the only consumer is the
  bundled frontend.
- Day-click drill-down: navigate to `/Logs?campaign=…&date=…` (already
  in scope on the Logs page if we extend its filters), or open a
  threads-list filter. Recommend Logs deep-link.
- Should we count a "queued" lead in funnel proportions, or only "ever
  attempted"? Recommend including (the queued bar shows pipeline
  health).

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
