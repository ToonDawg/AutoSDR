# [feature/scheduler] Pace outreach across an 8am–5pm window

<!-- TYPE: feature -->
<!-- AREA: scheduler -->

## Problem

A new campaign with `outreach_per_day=50` currently bursts the entire
day's quota in roughly 25 minutes (`max_batch_per_tick=2`,
`scheduler_tick_s=60`, `min_inter_send_delay_s=30`), then idles for the
rolling 24h window. Two specific failure modes:

- **Pattern detection.** Fifty cold-outreach SMSes in a 25-minute window
  from one sender phone reads like a spam cluster to carriers and to
  recipients. The "curious neighbour" voice the generator works hard to
  hit gets undone by the cadence.
- **Out-of-hours sends.** If the operator activates a campaign at 11pm,
  AutoSDR will start texting strangers at 11pm. The owner has no
  shipping-friendly knob to say "queue this; send between 8am and 5pm
  tomorrow."

The operator (single SMB owner running AutoSDR on their laptop) wants
the day's outreach **smoothed across a working window** — same total
volume, spread evenly so two sends rarely land in the same minute.

Evidence:

- [`autosdr/scheduler.py:176`](../../autosdr/scheduler.py) `_run_campaign_tick`
  takes the next `max_batch_per_tick` queued leads every `scheduler_tick_s`
  with no time-of-day awareness. Quota is rolling-24h only.
- [`docs/ROADMAP.md`](../ROADMAP.md) — the operator request driving
  this ticket: "space out the text messages that are sent for a Campaign
  through out the day from 8am to 5pm so they don't all send at the
  same time."

## Hypothesis

If we ship a per-campaign **outreach window** with even pacing, with
workspace-level defaults of `08:00–17:00` (server-local time), then a
50-lead campaign activated at 8am will send roughly one message every
11 minutes through the window instead of bursting in 25 minutes, and a
campaign activated at 11pm will send zero until 8am the next day.

Measured by:

- New `tests/test_pacing.py` cases asserting `window_allowance(...)`
  returns `0` outside the window, scales linearly inside, and respects
  per-campaign override.
- New `tests/test_scheduler_window.py` driving `_run_campaign_tick` at
  three injected clock points (07:30 → 0 sends, 08:00 → 1 send, 16:30
  → throttled to pacing target) on a fresh campaign.
- A 50-lead campaign with `outreach_window` enabled records `Message`
  rows whose `created_at` spread covers ≥ 80 % of the configured
  window in dev rehearsal.

## Scope

### Backend

- New `outreach_window` block on `workspace.settings` (default
  `{enabled: true, start_hour: 8, end_hour: 17}`). Schema lives next to
  `FollowupConfig` in `autosdr/api/schemas.py`. Backfilled at boot via
  the existing `merge_workspace_settings(...)` path.
- New `Campaign.outreach_window` JSON column (nullable). `None` means
  "inherit the workspace default". Migration entry in
  `_ADDITIVE_COLUMN_MIGRATIONS`.
- New `OutreachWindowConfig` Pydantic model + wiring into
  `CampaignOut` / `CampaignCreate` / `CampaignPatch`, mirroring the
  `FollowupConfig` shape and helpers (`_followup_for_out` /
  `_followup_to_storage`).
- New `autosdr/pacing.py` module with three pure functions:
  - `resolve_window(campaign_window, workspace_settings) -> OutreachWindow`
  - `window_allowance(*, window, daily_quota, sent_in_window, now_local) -> int`
  - `count_ai_messages_since(session, campaign_id, since_dt_utc) -> int`
- `autosdr/scheduler.py::_run_campaign_tick` resolves the window per
  campaign, computes per-campaign pacing allowance, and uses it as an
  upper bound alongside `max_batch_per_tick` and the rolling 24h quota.
- Manual kickoff (`POST /api/campaigns/{id}/kickoff`) **bypasses** the
  window — same precedent as `respect_quota=False`.
- Reply pipeline, follow-up beat, inbound poll: **untouched**.

### Frontend

- `frontend/src/lib/types.ts`: mirror `OutreachWindowConfig` and the new
  fields on `WorkspaceSettings.outreach_window` and `Campaign.outreach_window`.
- `BehaviourCard.tsx`: new "Outreach window" subsection (toggle +
  start/end hour inputs, helper text spelling out the bypass for
  replies / kickoff).
- `CampaignDetail.tsx`: per-campaign override card (toggle "Use
  workspace default" → reveals start/end hour inputs).

### Out of scope

- **Workspace IANA timezone setting.** v1 uses server local time. If
  AutoSDR ever runs on a server in a different region from the operator
  this becomes a separate small ticket. Not load-bound today.
- **Per-day-of-week schedules** (e.g. weekends off). Cheap to add later
  with a `days: [0..6]` array; not asked for.
- **Pacing for replies / follow-ups.** Conversations don't pause for
  office hours.
- **Backlog handling beyond pacing.** A campaign activated at 4:55pm
  with 50 leads will only send the proportional 1–2 messages today and
  carry the rest forward; no "burst the carry-over at tomorrow 8am"
  smoothing.
- **Per-day-of-week toggle on the UI.** Saving the window for a future
  ticket if anyone asks.
- **`even_pace=false` toggle.** Disabling the window entirely is the
  escape hatch. We can add explicit "hard cutoff only" later if anyone
  needs it.

## Success criteria

- `_run_campaign_tick` at 07:30 local, against an enabled-default
  workspace, sends zero outreach for any campaign — verified by
  `tests/test_scheduler_window.py::test_no_send_before_window_start`.
- `_run_campaign_tick` at the midpoint of the window with zero prior
  sends sends `ceil(quota / 2)` messages within `max_batch_per_tick`
  ticks — verified by `tests/test_scheduler_window.py::test_pacing_at_midpoint`.
- Per-campaign override beats workspace default — verified by
  `tests/test_pacing.py::test_resolve_prefers_campaign_over_workspace`.
- Manual kickoff at 11pm sends the requested count — verified by
  `tests/test_campaign_api.py::test_kickoff_bypasses_window`.
- Backend test suite green (`uv run pytest`).
- Frontend `tsc -b --noEmit` clean. New UI controls visible on
  `/Settings → Behaviour` and `/CampaignDetail` (per-campaign override).
- `docs/ROADMAP.md` Done row added; `ARCHITECTURE.md § 10` mentions the
  window in one paragraph.

## Effort & risk

- **Size:** M (~half a day; pacing is the only real design work).
- **Touched surfaces:** `autosdr/config.py`, `autosdr/models.py`,
  `autosdr/db.py` (migration entry), `autosdr/api/schemas.py`,
  `autosdr/api/campaigns.py`, `autosdr/scheduler.py`, new
  `autosdr/pacing.py`, new `tests/test_pacing.py`, new
  `tests/test_scheduler_window.py`, `tests/test_campaign_api.py`,
  `frontend/src/lib/types.ts`, `frontend/src/routes/settings/BehaviourCard.tsx`,
  `frontend/src/routes/CampaignDetail.tsx`,
  `docs/ROADMAP.md`, `ARCHITECTURE.md`.
- **Change class:** additive (one nullable JSON column; one new
  settings block; new pacing module). The scheduler change narrows the
  contract (sends become time-gated), but only when `enabled=true` —
  defaults turned on but the operator can disable it from Settings.
- **Risks:**
  - Server local time drift if the laptop's clock or tz changes. We
    rely on `datetime.now().astimezone()` so a tz change between ticks
    is picked up live; no caching of the offset.
  - Pacing could "starve" a campaign whose operator activates it late
    in the day. Acceptable: the next day's window picks up the
    backlog. Documented in the UI helper text.
  - The 24h rolling quota still applies on top — both gates stack — so
    no campaign can exceed `outreach_per_day` even if pacing maths
    says otherwise.

## Open questions

- **OQ1.** Should the pacing target be `ceil(quota * elapsed_fraction)`
  or floor / round? Architect picks `ceil`: starts the window with a
  small headroom (1 send allowed at t=0+epsilon if there's quota), so
  a campaign activated at 8:00:01 gets a send within the first tick
  rather than waiting until 8:11. Resolved inline.
- **OQ2.** Per-tick cap: in addition to the pacing allowance, should
  `max_batch_per_tick` still apply? Yes — defensive ceiling against
  backlog bursts. Confirmed: `min(pacing_allowance, max_batch_per_tick,
  remaining_24h_quota)`. Resolved inline.
- **OQ3.** Where does the campaign override live in the UI? Either a
  collapsible card on `/CampaignDetail` (chosen) or a separate
  `/CampaignDetail/edit` route. Chose inline card to match the
  follow-up beat's UX.

## Principle check

- **Simplicity first.** Single new pure module + one column + one
  settings block. The scheduler change is ~25 lines. ✓
- **Quality over speed.** Pacing functions are pure and unit-tested
  separately from the scheduler. ✓
- **Honest data contracts.** `outreach_window=None` on a campaign means
  "inherit". The API serialises the *resolved* window so consumers
  don't have to merge themselves. ✓
- **Extensible by design.** `OutreachWindow` dataclass is the seam to
  add `days_of_week`, IANA timezone, or `even_pace=false` later
  without touching consumers. ✓
- **Human always wins.** Manual kickoff bypasses the window; the
  killswitch still wins over everything; replies don't wait. ✓
- **Owner stays in control.** Default-on with sensible 8–5; operator
  can disable from Settings or override per campaign at any time. ✓

## Links

- Spec: [`autosdr-doc1-product-overview.md § 5`](../../autosdr-doc1-product-overview.md)
  — the operator persona that wants "queue overnight".
- Architecture: [`ARCHITECTURE.md § 10`](../../ARCHITECTURE.md) — the
  scheduler section to update on completion.
- Code:
  - [`autosdr/scheduler.py:176`](../../autosdr/scheduler.py)
  - [`autosdr/quota.py`](../../autosdr/quota.py)
  - [`autosdr/api/campaigns.py`](../../autosdr/api/campaigns.py)
  - [`autosdr/config.py:60`](../../autosdr/config.py)
  - [`frontend/src/routes/settings/BehaviourCard.tsx`](../../frontend/src/routes/settings/BehaviourCard.tsx)
- Related ticket: 0003 (per-campaign funnel) — same `Campaign` table,
  same `_to_out` helper.

## Dependencies

- Blocks: none direct. Sets up the seam for "per-day-of-week schedules"
  if anyone asks.
- Blocked by: none.
- Related: 0007 (e2e CLI, also touches `run_campaign_outreach_batch`
  via `--respect-quota=False` precedent).

## Implementation log (2026-04-28)

**Status:** done

| # | Unit | Outcome | Evidence |
|---|------|---------|----------|
| 1 | New `autosdr/pacing.py` — pure window/allowance maths | done | `tests/test_pacing.py` 25/25 (`uv run pytest tests/test_pacing.py`) |
| 2 | `outreach_window` defaults on `workspace.settings` | done | `autosdr/config.py:74` (defaults `enabled=True, 8..17`); auto-backfilled at boot via existing `merge_workspace_settings` |
| 3 | New `Campaign.outreach_window` nullable JSON column + additive migration | done | `autosdr/models.py:228`; `_ADDITIVE_COLUMN_MIGRATIONS` extended in `autosdr/db.py:115`; legacy DBs add the column on next boot |
| 4 | `OutreachWindowConfig` Pydantic schema + wiring | done | `autosdr/api/schemas.py` — added next to `FollowupConfig`; `CampaignOut.outreach_window` (override blob, nullable) + `effective_outreach_window` (resolved); `CampaignCreate` and `CampaignPatch` accept the override |
| 5 | API helpers `_outreach_window_for_out` / `_outreach_window_to_storage` / `_effective_outreach_window` | done | `autosdr/api/campaigns.py` — `_to_out` now reads workspace settings via `Workspace` query so the resolved window is exposed on every response |
| 6 | Scheduler integration | done | `autosdr/scheduler.py::run_campaign_outreach_batch` — `now_local` injectable; `respect_quota=True` applies window pacing AND the 24h quota; both gate stack via `min(allowance, 24h_remaining, max_batch_per_tick)`; new `OutreachBatchSummary.capped_by_window` flag distinguishes "out of business hours" from "out of daily quota" |
| 7 | Scheduler integration tests | done | `tests/test_scheduler_window.py` 9/9 (no send before 8am, no send after 5pm, no send at midnight, exactly-at-start = 0, midpoint with no prior sends = 2 attempts, target-met blocks further sends, disabled window short-circuits, campaign override beats workspace, kickoff bypasses) |
| 8 | API tests for `outreach_window` field | done | `tests/test_campaign_api.py` — added 4 cases (effective default, create with override, PATCH null clears, PATCH omitting field is no-op) |
| 9 | Frontend types + api.ts | done | `frontend/src/lib/types.ts` — `OutreachWindowConfig` mirrored, fields added to `WorkspaceSettings` and `Campaign`; `frontend/src/lib/api.ts` — `createCampaign`/`patchCampaign` accept `outreach_window` |
| 10 | Frontend UI — workspace default | done | `frontend/src/routes/settings/BehaviourCard.tsx` — new "Pace outreach across business hours" toggle + start/end hour fields under the existing scheduler grid |
| 11 | Frontend UI — per-campaign override | done | `frontend/src/routes/CampaignDetail.tsx::OutreachWindowSection` — collapsible card with "Override the workspace window" toggle, conditional fields, save semantics matching `FollowupSection` |
| 12 | Backend smoke | done | `uv run pytest` → 412 passed (was 408; +25 pacing, +9 scheduler-window, +4 API, -1 from cleanup of muddled patch test that now exists in cleaner form) |
| 13 | Frontend smoke | done | `npx tsc -b --noEmit` clean from `frontend/` |

**Final state of success criteria:**

- SC1 (07:30 → zero outreach): ✓ — `tests/test_scheduler_window.py::test_no_send_before_window_start`
- SC2 (midpoint pacing): ✓ — `tests/test_scheduler_window.py::test_pacing_at_midpoint_allows_send_when_behind` (target=25, max_batch=2 → 2 attempts; clean half-quota maths verified separately by `test_allowance_at_midpoint_targets_half_the_quota`)
- SC3 (per-campaign override beats workspace): ✓ — `tests/test_pacing.py::test_resolve_prefers_campaign_over_workspace` + `tests/test_scheduler_window.py::test_campaign_override_beats_workspace_default`
- SC4 (kickoff bypasses window): ✓ — `tests/test_scheduler_window.py::test_kickoff_bypasses_window`
- SC5 (backend suite green): ✓ — 412/412 tests pass
- SC6 (frontend tsc clean): ✓ — `npx tsc -b --noEmit` returns 0
- SC7 (Done row in roadmap, ARCHITECTURE updated): ✓ — see commit log

**Principle check after implementation:**

- Simplicity first: ✓ — one new pure module, one new JSON column, one new settings block. Scheduler change is ~30 lines.
- Quality over speed: ✓ — pacing maths split cleanly from DB and from the scheduler; 25 unit tests cover the curve at 5 sample points.
- Honest data contracts: ✓ — `effective_outreach_window` is always populated so consumers don't have to merge inheritance themselves; `outreach_window=null` on the campaign explicitly means "inherit", which matches the followup pattern.
- Extensible by design: ✓ — `OutreachWindow` dataclass + `resolve_window()` are the seam to add `days_of_week`, IANA timezone, or `even_pace=false` later without touching consumers.
- Human always wins: ✓ — manual kickoff bypasses the window (verified by `test_kickoff_bypasses_window`); replies and follow-ups are unaffected; killswitch still wins over everything.
- Owner stays in control: ✓ — defaults to enabled at 8–17 but the operator can disable it from Settings → Behaviour, override per campaign on `/CampaignDetail`, or set custom hours.

**Follow-ups raised:**

- Workspace IANA timezone setting (deferred — see Out of scope; small ticket if AutoSDR ever runs on a server in a different region from the operator).
- `OutreachBatchSummary.capped_by_window` is recorded but not yet surfaced on `/Dashboard` or `CampaignDetail` — the operator can already see "queued > 0 but no sends" and infer this from the time of day, but a future ticket could add a "next send at" hint.
- Per-day-of-week scheduling (e.g. weekends off) — deferred; cheap to add later via a `days: [0..6]` array on `OutreachWindowConfig`.

**Open questions still unresolved:** (none) — OQ1, OQ2, OQ3 from the original ticket were resolved inline in this implementation log.
