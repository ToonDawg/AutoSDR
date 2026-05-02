# [feature/scheduler] Prioritise leads with high-confidence broken-website signals

<!-- TYPE: feature -->
<!-- AREA: scheduler -->

## Problem

The Time-Poor Founder runs AutoSDR with limited daily send budget
(`Campaign.outreach_per_day`) and a category-mixing picker
([`autosdr/scheduler.py:138-221`](../../autosdr/scheduler.py))
that interleaves business categories but otherwise sends in
`queue_position` order — i.e. import-order. Some leads in any
given queue have **unambiguously broken websites**: the scan worker
got a 404/410 from the actual server, recorded in
`Lead.enrichment_status == "not_found"` (closed vocabulary at
[`autosdr/enrichment.py:53-62`](../../autosdr/enrichment.py)).
For these leads, the operator's pitch ("we'll get you a working
site") has its sharpest hook — but they sit at random positions in
the send queue.

The operator's framing (2026-04-30): *"prioritise things we are
confident on (404's on the scan should be pretty reliable I think,
or facebook profile as their website link?)"*. This ticket scopes
the **404 case only** — the highest-confidence signal — and defers
social-profile-as-website to ticket 0014 because that signal lives
on a different surface (hostname check on `Lead.website`, not an
emitted scan status).

Evidence:

- [`autosdr/scheduler.py:138-221`](../../autosdr/scheduler.py)
  `_next_queued_leads` — the picker we extend.
- [`autosdr/enrichment.py:53-62`](../../autosdr/enrichment.py),
  [`autosdr/enrichment.py:263-266`](../../autosdr/enrichment.py)
  — `not_found` is set strictly on a 404/410 from the upstream
  server, after crawlee's retry loop. High confidence; not produced
  on local network errors.
- [`autosdr/models.py:148-155`](../../autosdr/models.py) —
  `idx_lead_enrichment_status` already indexes
  `(workspace_id, enrichment_status)`. SQL filtering is cheap.
- [`docs/ROADMAP.md`](../../docs/ROADMAP.md) Later table —
  `[AI/Scheduler] Lead prioritisation by enrichment quality`
  (broader sibling, blocked on 2 weeks of stratified angle-funnel
  data). This ticket is a narrower, more-confident split that can
  ship without that data because we are not betting on enrichment
  vs. not — only on "404 leads are uniquely good fits for the
  pitch".
- [`docs/tickets/0011-lead-enrichment.md:256-260`](0011-lead-enrichment.md)
  — explicitly carved priority/reordering out of 0011 as
  follow-up work.

## Hypothesis

If `_next_queued_leads` returns leads where
`enrichment_status == "not_found"` before normal-tier leads — while
preserving today's category-mix interleave **within each tier** —
then the operator's first-message budget will be spent first on
leads where the pitch lands strongest. Measurable on
`/api/stats/angle-funnel` over the next ~2 weeks: the
`signature_detail` and `weak_presence` angle buckets should accrue
priority-tier sends ahead of the rest. Magnitude unknown — we are
not promising a reply-rate number; the deliverable is the order
change plus the visibility the operator needs to see it working.

## Scope

### Backend

- New module-level helper `is_priority_lead(lead: Lead) -> bool` in
  a focused module
  [`autosdr/pipeline/priority.py`](../../autosdr/pipeline/priority.py)
  (deviated from the original `_shared.py` plan — `_shared.py` is
  for outreach/reply pipeline primitives that drag in the LLM/prompt
  stack; the priority predicate is consumed by both the scheduler
  picker and the API serialiser, so a leaf module keeps the
  dependency direction clean). Predicate, today:
  ```python
  return lead.enrichment_status == "not_found"
  ```
  Module placement deliberately keeps it pipeline-adjacent rather
  than on `Lead` itself — the model stays thin and the predicate
  composes cleanly when ticket 0014 widens it to include
  `is_social_website(lead.website)`.
- Extend `_next_queued_leads` in
  [`autosdr/scheduler.py:138-221`](../../autosdr/scheduler.py)
  to bucket candidates into **priority** and **normal** tiers,
  drain priority first, then normal. Within each tier the
  existing 4-tuple `score(cat)` (anti-consecutive,
  untouched-categories, least-recently-sent, FIFO tiebreak) runs
  unchanged. The candidate SQL stays as it is — the index
  `idx_lead_enrichment_status` exists; we just sort the in-memory
  buckets by tier first.
- New workspace setting `priority.enabled: bool` (default `true`)
  on `workspace.settings`. When `false`, `_next_queued_leads`
  collapses to today's single-tier behaviour (regression fixture
  guarded). No per-campaign override on day one — defer to 0014
  when there's a credible second signal worth toggling per
  campaign.
- `CampaignOut` (`autosdr/api/schemas.py:246-297`) gains
  `queued_priority_count: int = 0` next to `queued_count`. Counted
  via a single SQL aggregate joined to `Lead.enrichment_status`,
  rolled into the existing `_campaign_totals_bulk` query in
  [`autosdr/api/campaigns.py:142-168`](../../autosdr/api/campaigns.py).
- `LeadOut` (`autosdr/api/schemas.py:398-413`) gains:
  - `is_priority: bool = False` — derived from
    `is_priority_lead(lead)`.
  - `priority_reason: str | None = None` — the literal token that
    fired the predicate (today: `"not_found"` or `None`). Reserved
    vocabulary; widens with 0014 (`"social_profile_website"`).
  Both computed at serialisation time in
  [`autosdr/api/leads.py`](../../autosdr/api/leads.py); no
  schema change.

### Frontend

- `frontend/src/lib/types.ts`: add `is_priority` and
  `priority_reason` to `Lead`; add `queued_priority_count` to
  `Campaign`; add a `PriorityConfig` block to `WorkspaceSettings`.
- New small primitive: `PriorityBadge` in
  `frontend/src/components/domain/` rendering `Priority` with the
  `priority_reason` tooltip (`"Website returns 404"` for
  `not_found`). Style mirrors the existing enrichment-status
  chips on `LeadDetail` for visual consistency (Tailwind
  utility classes only — blessed pattern in
  [`docs/PATTERNS.md`](../../docs/PATTERNS.md)).
- `LeadDetail.tsx`: render `<PriorityBadge>` above the existing
  enrichment card when `lead.is_priority`.
- `Leads.tsx` row: render the same badge inline next to the lead's
  name when `is_priority`.
- `CampaignDetail.tsx`: under the existing queued-count display,
  add `"X of these are priority"` when
  `queued_priority_count > 0`. Plain text, no chart — the
  per-campaign funnel from ticket 0003 stays the home for
  rich slicing.
- Settings → Behaviour: add a "Send priority" toggle bound to
  `workspace.settings.priority.enabled` via the existing
  `usePatchForm` hook (blessed in
  [`docs/PATTERNS.md`](../../docs/PATTERNS.md)). Helper text
  spells out the predicate.

### Migrations

- **No new columns.** The predicate is computed over the existing
  `Lead.enrichment_status` column. The settings block is a JSON
  sub-tree on `workspace.settings`, deep-merged via
  `merge_workspace_settings` in
  [`autosdr/config.py`](../../autosdr/config.py).

### Tests

- `tests/test_scheduler_priority.py` (new):
  - **Priority before normal, single category.** Queue [N1, P1,
    N2] where P1 is `enrichment_status="not_found"`. Limit 3 →
    order is [P1, N1, N2]. (Verifies tier dominates queue
    position.)
  - **Category mix preserved within priority tier.** Queue
    [P-plumber-1, P-plumber-2, P-electrician-1] cold start.
    Limit 2 → categories `["plumber", "electrician"]` (or
    equivalent — the existing rotation rules apply).
  - **Category mix preserved within normal tier.** Mirror of the
    existing `test_avoids_consecutive_same_category` but with a
    drained priority tier first.
  - **Toggle off restores today's behaviour.** With
    `priority.enabled = false`, the picker output is identical
    to the run with no priority leads — pin against an existing
    fixture from `tests/test_scheduler_category_mix.py` to keep
    the regression honest.
  - **Empty priority tier degenerates.** No `not_found` leads in
    the queue → picker is byte-identical to today.
  - **Cross-tick continuity.** Priority tier drained on tick 1;
    tick 2 with no fresh priority leads picks normal-tier and
    does not crash on the empty-priority bucket map.
- `tests/test_campaigns_api.py` extension:
  - `CampaignOut.queued_priority_count` counts only
    `CampaignLead.status == queued AND Lead.enrichment_status ==
    "not_found"`.
  - Equals zero on a campaign with no `not_found` leads.
- `tests/test_leads_api.py` extension:
  - `LeadOut.is_priority == true` and
    `priority_reason == "not_found"` for a `not_found` lead.
  - `is_priority == false` on `enrichment_status == "ok"` /
    `null` / `error`.

## Out of scope

- **Social-profile-as-website detection.** Whole signal class —
  ticket 0014.
- **Promoting `timeout` / `blocked` / `error` / `no_url` /
  `empty_shell` into the priority tier.** Confidence too low
  today — operator explicitly flagged
  `data/crawlee-test-report-20260429.md` shows blocked is often
  a Cloudflare interstitial, not a dead site. Ticket 0015 will
  add a `scrape_confidence` envelope field to make these
  promotable.
- **Re-sorting `CampaignLead.queue_position` at assignment
  time.** The tier is dynamic — re-evaluated each tick from
  `enrichment_status` so a lead that newly enriches into
  `not_found` mid-campaign jumps the queue without an operator
  action.
- **Per-campaign priority override.** No operator asking; defer
  to 0014.
- **Priority-aware angle-funnel slicing.** The existing
  `?enrichment=enriched|unenriched|all` filter on
  `/api/stats/angle-funnel` is enough to cut the data after the
  fact. No new filter dimension on day one.
- **Audit-log breadcrumb on a "this send was elevated" event.**
  The angle-funnel + the new `queued_priority_count` give the
  operator the visibility they need; an extra column on
  `LlmCall` would be cost without value.

## Success criteria

- `_next_queued_leads` returns priority leads first when both
  tiers are non-empty — verified by
  `tests/test_scheduler_priority.py::test_priority_before_normal_within_a_category`.
- Category mix is preserved within each tier — verified by
  `tests/test_scheduler_priority.py::test_priority_tier_still_rotates_categories`
  and the existing `tests/test_scheduler_category_mix.py` suite
  passing unchanged.
- Toggle `priority.enabled = false` on `workspace.settings`
  reproduces today's exact picker output — verified by
  `tests/test_scheduler_priority.py::test_priority_toggle_off_is_byte_identical`.
- `GET /api/campaigns/{id}` returns
  `queued_priority_count` matching a `SELECT COUNT(*)` over
  `CampaignLead.status='queued' AND Lead.enrichment_status='not_found'`
  for that campaign — verified by
  `tests/test_campaigns_api.py::test_campaign_out_exposes_queued_priority_count`.
- `GET /api/leads/{id}` returns
  `is_priority=true, priority_reason="not_found"` for a 404 lead —
  verified by
  `tests/test_leads_api.py::test_lead_out_marks_not_found_as_priority`.
- Priority badge renders on `/leads/:id` and on the row in `/leads`
  for a `not_found` lead — verified by `tsc -b --noEmit` clean and
  visual check (no UI test infra today).
- Toggle on Settings → Behaviour persists to
  `workspace.settings.priority.enabled` — verified by
  `tsc -b --noEmit` clean and the existing settings-persistence
  pattern.

## Effort & risk

- **Size:** S (~0.4 person-weeks).
- **Touched surfaces:**
  - `autosdr/pipeline/_shared.py` (new helper)
  - `autosdr/scheduler.py:138-221` (`_next_queued_leads` tier dimension)
  - `autosdr/api/schemas.py` (`CampaignOut.queued_priority_count`,
    `LeadOut.is_priority`, `LeadOut.priority_reason`,
    `PriorityConfig`)
  - `autosdr/api/campaigns.py:142-168` (`_campaign_totals_bulk`)
  - `autosdr/api/leads.py` (serialiser)
  - `autosdr/config.py` (`DEFAULT_WORKSPACE_SETTINGS["priority"]`)
  - `frontend/src/lib/types.ts`, `LeadDetail.tsx`, `Leads.tsx`,
    `CampaignDetail.tsx`, `BehaviourCard.tsx`,
    `components/domain/PriorityBadge.tsx` (new)
- **Change class:** additive (no schema migrations; settings sub-tree
  is deep-merged; default-on toggle preserves operator expectation).
- **Risks:**
  - **Picker regression.** The 4-key score is precision-tuned for
    cross-tick rotation. The fix: tier dimension is layered *outside*
    the existing scorer; the scorer itself is untouched. Tests pin
    "single tier degenerates to today" against the existing fixtures.
  - **Operator surprise.** Operators who haven't read the changelog
    may notice the queue order changed. Mitigated by the
    `queued_priority_count` surface ("3 of 47 queued are priority")
    and the toggle in Settings.
  - **Stale enrichment.** A lead that was `not_found` last week
    but now resolves still pops as priority until the next scan
    cycle. Acceptable: scan worker is decoupled and re-scans as
    part of normal operation; the predicate is dynamic, so it
    self-heals on the next tick after a re-scan.

## Open questions

1. **Settings location — `priority.enabled` vs nesting under
   `outreach.priority_enabled`?** The latter groups it next to the
   outreach window (also a scheduler-side setting); the former gives
   it room for the `0014` social-profile knobs without nesting two
   levels deep. Lean: **top-level `priority` block** so 0014 can add
   `priority.social_platforms` without a rename. Council below.
2. **Should `LeadOut.priority_reason` be a closed Literal type?**
   Adds a Pydantic-side guarantee but means we have to bump the
   schema in 0014. Lean: **plain `str | None` for now**, document
   the vocab in the field docstring; convert to Literal in 0014
   when the second value lands. Council below.
3. **`PriorityBadge` placement on `LeadDetail` — above or beside the
   enrichment card?** UI-level user preference; surface to operator
   rather than decide. Lean: **above the enrichment card** so the
   priority signal is the first thing the operator sees.

## Resolved questions (2026-04-30)

### Resolved: settings-location

**Architect:** Top-level `priority` block on `workspace.settings`.
Gives 0014 room (`priority.social_platforms`) without a rename.
**Skeptic:** Disagrees mildly — putting it next to `outreach_window`
under a future `outreach.*` namespace would be more discoverable.
**Pragmatist:** Top-level is fine; we don't have an `outreach.*`
namespace today, inventing one for one toggle is over-engineering.
**Critic:** Top-level is more honest — priority is a global send-
order concern, not specifically an outreach-window concern.

**Decision:** Top-level `priority: {enabled: bool}` on
`workspace.settings`.
**Strongest dissent:** Skeptic's "discoverability via grouping"
point — accepted as an open path; if/when an `outreach.*`
namespace lands, this can move under it cleanly.
**Confidence:** high.
**Why this is acceptable:** Renaming a JSON sub-tree later is a
deep-merge migration entry, not a schema change.

### Resolved: priority-reason-type

**Architect:** Plain `str | None` for v1, document vocab in
docstring; convert to closed `Literal` in 0014.
**Skeptic:** Closed `Literal` would catch a typo today;
defending strings on a value type is the smell.
**Pragmatist:** With one value (`"not_found"`) in the vocabulary
the Literal is performative — convert when there's a second value.
**Critic:** The risk is downstream consumers writing `if reason ==
"not-found"` (typo) and silently failing. Tests cover this.

**Decision:** `priority_reason: str | None` for 0013; convert to
`Literal["not_found", "social_profile_website"]` in 0014 when the
vocab grows.
**Strongest dissent:** Skeptic's typo-catch — accepted as a real
risk; mitigated by the test that asserts the exact literal value
on a `not_found` lead.
**Confidence:** medium-high.
**Why this is acceptable:** Single-value Literals are noise.

### Resolved: priority-badge-placement

User-preference call. Default: above the enrichment card on
`LeadDetail`. Operator can flip in a follow-up if it reads wrong.

## Principle check

- **Simplicity first:** ✓ — adds one tier dimension to a Python
  loop that was already buckets-by-category. No new modules
  beyond a single helper. No schema change.
- **Quality over speed:** ✓ — the operator's pitch is sharper on
  priority leads; sending them first is exactly the
  "60-second message that resonates" trade-off.
- **Honest data contracts:** ✓ — `priority_reason` exposes the
  literal token that fired (`"not_found"`); the toggle exposes
  whether priority is on. No magic in the picker behaviour.
- **Extensible by design:** ✓ — `is_priority_lead(lead)` is one
  predicate the next ticket widens with one more `or` clause.
- **Human always wins:** ✓ — picker change does not affect the
  killswitch, the analysis call, or HITL routing; it only
  reorders within the existing batch limit.
- **Owner stays in control:** ✓ — Settings toggle, no per-campaign
  override needed yet, queue-state visibility via
  `queued_priority_count`.

## Links

- Spec: `autosdr-doc1-product-overview.md § 5` — explicit non-goal
  on AI lead scoring; this is **deterministic** prioritisation
  (a hostname/status predicate), so the principle filter survives.
- Architecture: [`ARCHITECTURE.md § 3`](../../ARCHITECTURE.md)
  (component map; the change is contained to the scheduler tier).
- Code:
  [`autosdr/scheduler.py:138-221`](../../autosdr/scheduler.py)
  (picker),
  [`autosdr/enrichment.py:53-62`](../../autosdr/enrichment.py)
  (vocab),
  [`autosdr/models.py:148-155`](../../autosdr/models.py) (index).
- Roadmap: [`docs/ROADMAP.md`](../../docs/ROADMAP.md) — Later
  table item this ticket replaces.
- Plan: [`.cursor/plans/broken-website_lead_priority_27ffae7e.plan.md`](../../.cursor/plans/broken-website_lead_priority_27ffae7e.plan.md)

## Dependencies

- **Blocks:**
  - `0014-social-profile-as-website.md` (extends the predicate
    with `is_social_website(lead.website)`).
- **Blocked by:** none.
- **Related:**
  - `0011-lead-enrichment.md` — produces the
    `enrichment_status` column the predicate reads.
  - `0010-outreach-business-hours.md` — pacing composes
    cleanly with priority (pacing is a rate cap on the picker's
    output; the order change is upstream).

## Implementation log (2026-04-30)

**Status:** done

| # | Unit | Outcome | Evidence |
|---|------|---------|----------|
| 1 | `is_priority_lead` predicate + reason vocab in `autosdr/pipeline/priority.py` | done | `tests/test_priority_lead.py` 10 tests passing (truth table over closed `EnrichmentStatus` vocab + literal-token pin). |
| 2 | Tier dimension on `_next_queued_leads` + `priority` workspace setting + tests | done | `autosdr/scheduler.py:_next_queued_leads` now buckets candidates into priority/normal tiers and drains in order; `tests/test_scheduler_priority.py` 6 tests pinning tier dominance, within-tier rotation, cross-tier `last_sent_cat` continuity, toggle-off byte-identity, empty-priority degeneracy, and cross-tick continuity. Existing `tests/test_scheduler_category_mix.py` 6 tests still pass unchanged. |
| 3 | `CampaignOut.queued_priority_count` + bulk count helper | done | `autosdr/api/schemas.py` `queued_priority_count` field; `autosdr/api/campaigns.py::_campaign_queued_priority_bulk` single-query aggregate; `tests/test_campaign_api.py::test_campaign_out_exposes_queued_priority_count` and `…_zero_when_no_priority` green. |
| 4 | `LeadOut.is_priority` + `priority_reason` serialiser | done | `autosdr/api/schemas.py` two new fields + docstring; `autosdr/api/leads.py::_lead_to_out` helper used by all four `LeadOut` call sites (list / get / opt-out / clear-opt-out); `tests/test_lead_priority_api.py` 9 tests covering the not_found case, the seven non-priority statuses + `NULL`, and the list-page contract. |
| 5 | Frontend types, `PriorityBadge`, LeadDetail/Leads/CampaignDetail/BehaviourCard | done | `frontend/src/lib/types.ts` (`Lead.is_priority`, `Lead.priority_reason`, `Campaign.queued_priority_count`, `WorkspaceSettings.priority`, `PriorityConfig`); new `frontend/src/components/domain/PriorityBadge.tsx`; `LeadDetail.tsx` header chip + `Leads.tsx` row badge + `CampaignDetail.tsx` priority-queue note in the manual kick-off card + `BehaviourCard.tsx` "Send priority leads first" toggle bound through `usePatchForm`. `npx tsc -b --noEmit` clean. |

**Final state of success criteria:**
- `_next_queued_leads` returns priority leads first when both tiers are non-empty — ✓ via `tests/test_scheduler_priority.py::test_priority_before_normal_within_a_category`.
- Category mix preserved within each tier — ✓ via `tests/test_scheduler_priority.py::test_priority_tier_still_rotates_categories` plus existing `tests/test_scheduler_category_mix.py` suite green unchanged.
- `priority_enabled = false` reproduces today's exact picker output — ✓ via `tests/test_scheduler_priority.py::test_priority_toggle_off_is_byte_identical` and `…test_empty_priority_tier_is_byte_identical`.
- `GET /api/campaigns/{id}` returns matching `queued_priority_count` — ✓ via `tests/test_campaign_api.py::test_campaign_out_exposes_queued_priority_count`.
- `GET /api/leads/{id}` returns `is_priority=true, priority_reason="not_found"` — ✓ via `tests/test_lead_priority_api.py::test_lead_out_marks_not_found_as_priority`.
- Priority badge renders on `/leads/:id` and `/leads` — ✓ via `tsc -b --noEmit` clean and the `PriorityBadge` import wired into both files (visual check by the operator on dev server).
- Settings → Behaviour persists `priority.enabled` — ✓ via the `usePatchForm` derive/save cycle now threading `priority` through `api.patchWorkspaceSettings`; `tsc -b --noEmit` clean.

**Principle check after implementation:**
- Simplicity first: ✓ — single new ~70-line module + one new top-level for-loop wrapper around the existing scoring loop. No schema migration; one new public API field.
- Quality over speed: ✓ — broken-site leads land first, where the operator's "we'll get you a working site" pitch is sharpest.
- Honest data contracts: ✓ — `queued_priority_count` and `priority_reason` expose the literal token; the toggle exposes whether priority is on. No magic in the ordering.
- Extensible by design: ✓ — `is_priority_lead(lead)` is one predicate that ticket 0014 widens with one more `or` clause; bucket loop and bulk SQL stay the same shape.
- Human always wins: ✓ — picker change does not affect killswitch, analysis, or HITL routing; only reorders inside the existing batch limit.
- Owner stays in control: ✓ — `priority.enabled` toggle in Settings → Behaviour, queued count + priority breakdown surfaced on Campaign detail, badges everywhere a priority lead appears.

**Tests run:** full backend suite (475 passed); frontend `tsc -b --noEmit` (0 errors).

**Pattern-unifier (diff-only):** No new ⚠ or ✗ introduced by this ticket.
- Backend: SQLAlchemy 2.0 (`select` + `func.count` joined query) ✓; Pydantic v2 fields on existing models ✓; pytest fixtures matching `tests/test_scheduler_category_mix.py` ✓; closed-vocabulary literal kept in code (will move to `Literal` in 0014).
- Frontend: existing `Badge` primitive reused for `PriorityBadge` ✓; `usePatchForm` for the new toggle ✓; Tailwind tokens via existing `bg-oxblood-soft` / `border-oxblood/30` ✓; no new dependencies.

**Follow-ups raised:** (none — `0014` and `0015` were already filed as siblings of this ticket.)

**Open questions still unresolved:** (none — all three resolved during pre-flight; verdicts are recorded above.)
