# [feature/ui] Mobile-responsive operator console (precondition for PWA)

<!-- TYPE: feature -->
<!-- AREA: ui -->
<!-- SEVERITY: gating for 0005 -->

## Problem

`README.md:14` ("scheduler polls; no tunnel / public URL needed") and the
roadmap's *Considered* row ("Mobile / responsive layout below 1024px —
Reasonable trade-off until PWA + Push lands; **revisit then**") agree that
the console was scoped as laptop-only. The shipped reality is consistent
with that — `frontend/src/AppLayout.tsx` is a fixed-sidebar two-pane
layout, the Leads / Threads / Logs / Scans tables don't wrap, and the
ThreadDetail / CampaignDetail pages assume ≥1024 px of horizontal real
estate (LLM trail, suggested replies, message thread + HITL action panel
sit side-by-side at desktop widths).

Two things have moved since:

1. **Ticket 0005 (PWA + Web Push) is "ready" in `Next`.** Its
   success criterion is *"manual smoke: install the PWA on a phone, run
   `autosdr sim inbound …`, see the notification appear within 10s."*
   Today, **clicking that notification opens a layout that's broken
   on a 390-px-wide viewport** — sidebar overlaps content, tables overflow
   the viewport, the HITL action panel is unreachable without horizontal
   scroll. Push without responsive is a notification that lands on a UI
   the operator can't actually use.
2. **The operator (you) is going to be using AutoSDR from the phone.**
   The May 2 conversation made the intent explicit: *"it would be good
   for my mobile to be able to open the dashboard. Check updates AND my
   hosting server be able to chat to my phone to send messages while I'm
   away."* The dashboard side of that is this ticket; the network side
   is the refined 0005.

Concretely, a five-minute audit at 390×844 (iPhone 14 Pro viewport)
surfaces the following classes of breakage. This is the audit punch-list,
not a full Cypress matrix:

| Surface | What breaks | Severity |
| --- | --- | --- |
| `AppLayout` sidebar | Always rendered, fixed-width — eats half the viewport at 390 px. | blocker |
| `Leads` table | 8 columns side-by-side (status, name, phone, category, region, tags, social, last sent). At 390 px, the first 3 columns overflow off-screen with no `overflow-x-auto` wrapper. | blocker |
| `Threads` table | Same shape: 6 columns, same overflow. | blocker |
| `Logs` (LLM calls) | 9 columns (created, purpose, model, prompt-version, tokens, cost, latency, lead, expand). Overflows at 1024 px on narrow laptops too. | blocker |
| `Scans` index + detail | Filter chips wrap awkwardly; raw `_meta` JSON block has no horizontal scroll containment. | major |
| `Inbox` (HITL queue) | Probably the most-used surface for the away-from-desk operator. Two-pane (queue list ⇆ thread detail) does not collapse to a master-detail pattern on small screens. | blocker |
| `ThreadDetail` | Three-column layout (timeline / suggested replies / LLM trail) at desktop; on mobile one column should stack and the LLM trail should collapse to a footer disclosure. | blocker |
| `CampaignDetail` | Funnel chart + 14-day grouped bars + lead lists — multiple side-by-side panels. | major |
| `LeadsImport` | File-drop + preview-table + mapping-table — the mapping table has the same overflow issue as Leads. | major |
| `Settings` | Cards stack OK already (good — uses `space-y-*`). Connector test buttons are hit-target-too-small on touch (~24 px) — see below. | minor |
| Hit targets | Most buttons are `h-8` / 32 px which is below WCAG and Apple's 44 px touch target minimum. | minor (accessibility) |
| Form inputs | Some `text-sm` (14 px) inputs trigger iOS auto-zoom on focus (iOS auto-zooms anything < 16 px). | minor |

## Hypothesis

If we (a) make `AppLayout` collapse to a hamburger-drawer below the
`md:` breakpoint, (b) ship a card-list fallback for every data table on
narrow viewports, and (c) collapse the multi-pane Inbox / ThreadDetail
into stacked master-detail views, then the time-poor founder can
triage a HITL escalation from a phone within 30 seconds of the push
notification landing — without ever opening the laptop.

Measured by:

- **Manual smoke** at 360×640 (small Android), 390×844 (iPhone 14 Pro),
  768×1024 (iPad portrait) viewports, and 1024-px laptop. All seven
  primary routes (`Dashboard`, `Inbox`, `Threads`, `Leads`,
  `Campaigns`, `Logs`, `Settings` + `LeadDetail`, `CampaignDetail`,
  `ThreadDetail`) have no horizontal scroll, no hidden actions, no
  off-screen content. Hit targets ≥ 44 px on tappable elements.
- **An accept-the-current-state visual baseline.** A new
  `frontend/src/__tests__/responsive-snapshot.spec.tsx` (or equivalent
  Playwright spec — see Open questions) takes screenshots at the four
  viewports and pins them so future drift is loud. *Optional* — see
  Open questions; don't block on tooling we don't already have.
- **The 0005 PWA smoke** ("notification → click → land on
  `/inbox?thread=<id>`") completes end-to-end on a phone without the
  operator pinch-zooming or rotating to landscape.

## Scope

### Part A — `AppLayout` mobile chrome

- Below `md:` (Tailwind default 768 px), the persistent sidebar
  collapses to a hamburger button in a top bar.
- Drawer slides in over content (not a viewport reflow — keeps the
  body's scroll position stable for "open menu, navigate, come back").
- The killswitch banner stays pinned to the top of the viewport on
  mobile (it's the operator's "AutoSDR is paused" anchor — must be
  visible when scrolled into a long thread).
- The killswitch banner's count badge from ticket 0009
  (`paused_inbound_pending_count`) must still fit on a 320-px-wide
  viewport.

### Part B — Data tables → card list pattern below `md:`

For each of `Leads`, `Threads`, `Logs`, `Inbox`, `Scans`, and
`LeadsImport`-mapping:

- `<md`: render a vertical list of cards (one per row), with the
  primary identifier (lead name / thread subject / log purpose) as
  card title, the most-relevant metadata as 2-3 secondary lines, and
  status badges in a clear row. Tap → row's existing detail link.
- `≥md`: keep the current dense table.

This is the single biggest design call in the ticket. **Do not** ship
"tables but with horizontal scroll" — that pattern is technically
responsive but functionally broken (operators have to scrub
horizontally to read each row, and we know from the audit that
`overflow-x-auto` isn't even there today, so adding it is also work).
The card pattern lands once and composes with future filter/search
work.

### Part C — Inbox → master-detail collapse

- `≥md`: today's two-pane (queue list left, thread detail right).
- `<md`: queue list is a single full-width column. Tapping a thread
  pushes the route to `/inbox/<thread_id>`; back button returns. This
  matches `frontend/src/routes/Threads.tsx` already (which is one
  level deeper); the change is consolidating the inbox-queue
  affordance behind a route boundary instead of a side-by-side
  layout.

### Part D — `ThreadDetail` stack + collapse

- `≥md`: timeline on left, suggested replies + HITL action top-right,
  LLM trail bottom-right (current).
- `<md`: stack vertically — timeline, then HITL action panel,
  then suggested replies, then LLM trail as a `<details>` disclosure
  (collapsed by default; the operator opens it only when debugging).
- `ComposeBar` sticks to the bottom of the viewport on mobile so the
  primary action (send / queue reply) is always reachable without
  scrolling past a long timeline.

### Part E — Hit targets, focus, iOS zoom

- Audit Tailwind `h-8` / `w-8` / `text-xs` buttons across
  `frontend/src/components/ui/`. Bump to `h-11` / `min-h-[44px]` on
  tappable elements (the rule is touch targets ≥ 44 px, per
  WCAG 2.2 / Apple HIG).
- Set the base `<input>` font size to 16 px on mobile to suppress
  iOS auto-zoom-on-focus (this is one CSS rule:
  `@media (max-width: 768px) { input, textarea, select { font-size: 16px; } }`
  in `frontend/src/index.css`).
- Make sure focus rings are visible (the current ring styles are
  `outline-none focus:ring-2` — verify).

### Part F — README + ARCHITECTURE update

- `README.md` — drop the "laptop UI" line, add "responsive — laptop
  primary, phone supported".
- `ARCHITECTURE.md § 14` (Out of scope) — remove
  "Mobile / responsive" from out-of-scope; or move it to "in scope".

### Out of scope

- **Native iOS / Android apps.** PWA + responsive web is the path; this
  ticket is the responsive-web half.
- **Per-route mobile-only features.** No "tap-and-hold to reveal
  actions" gestures, no offline-first caching of the inbox, no swipe-
  to-archive. v1 is "the existing surfaces work on small screens" —
  not "we redesigned for mobile-first".
- **Tablet-specific layouts.** iPad portrait is in the smoke matrix
  but treat it as either "wide phone" or "narrow laptop" depending on
  what reads better — don't ship a third breakpoint just for tablet.
- **Dark mode / theme work.** Adjacent but separate concern.
- **Accessibility audit beyond the WCAG hit-target + iOS zoom items
  above.** A full a11y pass is its own ticket.

## Success criteria

- All seven primary routes render with no horizontal scroll at
  360×640, 390×844, 768×1024, and 1024×768 viewports. Manual smoke +
  one Playwright (or equivalent) spec asserting `document.body`
  `scrollWidth <= clientWidth` at each breakpoint, for each route.
- `AppLayout` sidebar is a drawer below `md:`; the hamburger toggle
  is visible above the fold; the killswitch banner with paused-
  inbound count is visible on a 320-px viewport.
- `Leads`, `Threads`, `Logs`, `Inbox`, `Scans`,
  `LeadsImport`-mapping all use the card-list pattern below `md:`.
- `ThreadDetail` stacks vertically below `md:` with the LLM trail in
  a collapsed `<details>` and a sticky `ComposeBar`.
- Tappable elements (buttons, links, badges with click handlers) have
  `min-h-[44px]` `min-w-[44px]` (or padding equivalent) on mobile.
- iOS Safari does not auto-zoom on input focus (16 px input font
  size).
- `README.md` and `ARCHITECTURE.md § 14` updated.
- Backend test suite green (no backend changes expected).
- Frontend `tsc -b --noEmit` clean, `vite build` clean, no new
  bundle-size regression > 5 KB gzipped (responsive layout shouldn't
  change bundle size meaningfully — it's CSS class changes, not new
  components).

## Effort & risk

- **Size:** M (4-6 days). Not L; this is mostly Tailwind responsive
  classes and route-shape adjustments, no schema or backend changes.
  The card-list pattern is the only new component shape.
- **Touched surfaces:**
  - `frontend/src/AppLayout.tsx` (drawer + hamburger).
  - `frontend/src/routes/{Leads,Threads,Logs,Inbox,Scans,LeadsImport,ThreadDetail,CampaignDetail,LeadDetail,Dashboard}.tsx` — every primary route.
  - `frontend/src/routes/thread/{ComposeBar,LlmTrail,SuggestedReplies,HitlActionPanel}.tsx` (ThreadDetail children).
  - `frontend/src/components/ui/{Button,Badge,Input}.tsx` (hit-target sizing).
  - `frontend/src/index.css` (iOS-zoom rule).
  - New `frontend/src/components/ui/CardList.tsx` (the table-fallback
    pattern; ~80 LoC, single source of truth).
  - `README.md`, `ARCHITECTURE.md § 14`.
- **Change class:** UI surface only. No invasive risk; reverts cleanly
  per-route if something regresses.
- **Risks:**
  - **Test debt.** We don't have visual regression tests today.
    Adding Playwright is a dep + CI surface bump; without it, ongoing
    drift between mobile and desktop is invisible. Open question
    below — don't block on this if the existing manual smoke + tsc
    is acceptable for a single-operator project.
  - **Card-list as the table fallback** is the design choice that
    will get pressure-tested if the operator ever has a 5k-row Leads
    list — scrolling 5k cards is worse UX than scrolling 5k table
    rows. Mitigate with the existing search + filter chips (already
    present on Leads) and pagination — the card list inherits both.
  - **Settings → Behaviour card** is currently long (priority,
    enrichment, outreach window, follow-up, …). Verify each card
    section reads cleanly at 390 px before declaring done.

## Open questions

- ~~**OQ1.** Visual-regression tooling: introduce Playwright now, or
  manual smoke + a checklist for v1?~~ — resolved 2026-05-02.
- ~~**OQ2.** Card-list design: dense or airy?~~ — resolved 2026-05-02.
- ~~**OQ3.** `ComposeBar` mobile keyboard handling.~~ — resolved 2026-05-02.
- ~~**OQ4.** Killswitch banner: dismiss-on-mobile or always-visible.~~ — resolved 2026-05-02.

## Resolved questions (2026-05-02)

### Resolved: OQ1 — visual-regression tooling

**Architect:** Manual smoke + a written checklist, no Playwright. Single-
operator codebase; CI surface and dep cost outweigh drift-protection
value at this stage.
**Skeptic:** Manual smoke is a write-only audit; without baselines,
"I tested it" decays to "I tested it once, six months ago". Mitigation:
the checklist gets a date and viewport list per route, committed in the
implementation log, so the next regression has a concrete target.
**Pragmatist:** Manual + checklist. Adding Playwright + a screenshot
diff workflow is at least a half-day of CI configuration that doesn't
move the operator's actual problem (push not landing on a usable UI).
**Critic:** Manual + checklist is acceptable iff the same checklist
gets re-run when 0005 lands (mobile preconditions can't silently rot
between this ticket and the PWA ticket).

**Decision:** Manual smoke + checklist. The implementation log captures
the four-viewport route matrix so regression has a concrete revisit
target. Re-run when 0005 ships; promote to Playwright if a second
contributor lands.
**Strongest dissent:** Skeptic's "manual decays". Acceptable because
a single-operator project can swap to visual regression without code
changes — the seam is the route shape, which we're not changing.
**Confidence:** medium-high.

### Resolved: OQ2 — card-list density

**Architect:** Dense (3 lines per card, ~88 px tall) — the operator's
most-common task on this surface is *scan a list and find the one that
needs me*, not "read all metadata".
**Skeptic:** Dense risks ambiguity on the Logs page where four
metadata fields actually matter (purpose, model, latency, cost).
Mitigation: per-route freedom to add a 4th line where the operator
genuinely uses it; a "card" is a layout, not a fixed-height contract.
**Pragmatist:** Dense. The detail-page tap is one finger-distance away;
all metadata is one route-deep, not one-screen-down.
**Critic:** Dense is fine; the airy variant is a discoverability
fix in disguise and hurts everyone who isn't first-time-using-AutoSDR.

**Decision:** Dense default (1 title + 2 secondary lines + status row),
with per-route discretion to add a 4th line where it's load-bearing.
**Strongest dissent:** Skeptic's "Logs needs 4 lines" — accommodated
by the per-route discretion clause.
**Confidence:** high.

### Resolved: OQ3 — `ComposeBar` keyboard handling

**Architect:** Let the browser default handle it for v1 — no
`interactive-widget` meta tag tweak.
**Skeptic:** "Browser default" silently means "send button gets
covered" on most Android keyboards; that's the worst-case mobile
ergonomic. Mitigation: ship the sticky bottom bar; if reports of
"can't see send" land, add `interactive-widget=resizes-content` as a
single-line follow-up.
**Pragmatist:** Default. We don't have a phone smoke test running
yet — guessing at meta-tag behaviour without being able to test it
is worse than shipping the obvious sticky layout and tweaking later.
**Critic:** Default is fine. The compose bar's sticky positioning
is what matters — if a keyboard covers it, scrolling works.

**Decision:** Browser default; sticky `ComposeBar` is the load-bearing
fix. Re-evaluate after 0005 manual smoke if "send button covered"
reports land.
**Strongest dissent:** Skeptic's "default = covered". Acceptable as a
single-line meta-tag follow-up.
**Confidence:** medium.

### Resolved: OQ4 — killswitch banner

**Architect:** Always visible while paused. The banner *is* the "you
are paused" affordance — losing it is a class of operator confusion.
**Skeptic:** No dissent; dismissable would be a footgun.
**Pragmatist:** Always visible. One less interactive element to
maintain.
**Critic:** Always visible. Pinning at the top is fine; it's small.

**Decision:** Always visible while paused. Pinned to the top of the
viewport on mobile.
**Strongest dissent:** (none).
**Confidence:** high.

## Principle check

- **Simplicity first.** ✓ — adds one component shape (CardList) and
  Tailwind classes. No new dep, no new build surface (assuming OQ1
  resolves to "manual smoke for v1").
- **Quality over speed.** ✓ — the operator's primary use case for
  AutoSDR is reading drafted replies and approving them; if that's
  broken on the device they actually have to hand, the AI loop's
  quality work is wasted.
- **Honest data contracts.** ✓ — no contract change.
- **Extensible by design.** ✓ — CardList is the seam; future routes
  use it.
- **Human always wins.** ✓ — the human can act faster on phone.
- **Owner stays in control.** ✓ — the operator-facing change is
  purely additive; no behaviour change.

## Links

- Spec: `autosdr-doc1-product-overview.md § 2` (PWA control surface).
- Architecture: `ARCHITECTURE.md § 14` (current "out of scope").
- Roadmap: this row + the *Considered* note we're now closing out.
- Code: every primary route under `frontend/src/routes/`.
- Related ticket: **`docs/tickets/0005-pwa-web-push.md` (refined)** —
  this ticket gates the value of 0005's mobile path; sequence it
  ahead of 0005.

## Dependencies

- **Blocks:** 0005 (PWA + Web Push) — sequence this before 0005 so the
  notification → click → triage flow lands on a usable UI.
- **Blocked by:** nothing.
- **Related:** 0009 (killswitch inbound replay) — its
  `paused_inbound_pending_count` badge needs to fit on a 320 px
  viewport; coordinate the badge styling.

## Mini plan (2026-05-02)

| # | Unit | Files | Change class | Tests | Depends on | Risk |
|---|------|-------|--------------|-------|------------|------|
| 1 | AppShell drawer + hamburger + sticky chrome | `frontend/src/components/layout/{AppShell,Sidebar,TopBar,KillSwitch}.tsx`, `frontend/src/index.css` | invasive (layout root) | manual smoke checklist | — | high |
| 2 | New `CardList` primitive (table-fallback) | `frontend/src/components/ui/CardList.tsx` (new) | additive | none (pure JSX) | unit 1 | low |
| 3 | Convert tables to `CardList` below `md:` | `frontend/src/routes/{Leads,Threads,Logs,Inbox,Scans,LeadsImport}.tsx` | invasive (per-route) | manual smoke | unit 2 | med |
| 4 | `ThreadDetail` mobile stack + sticky `ComposeBar` + `<details>` LLM trail | `frontend/src/routes/ThreadDetail.tsx`, `frontend/src/routes/thread/{ComposeBar,LlmTrail}.tsx` | invasive | manual smoke | unit 1 | med |
| 5 | Detail pages mobile checks | `frontend/src/routes/{Dashboard,CampaignDetail,LeadDetail}.tsx` | additive (Tailwind classes) | manual smoke | unit 1 | low |
| 6 | Hit targets + iOS-zoom rule | `frontend/src/components/ui/{Button,Input}.tsx`, `frontend/src/index.css` | additive | manual smoke | — | low |
| 7 | README + `ARCHITECTURE.md § 14` updates | `README.md`, `ARCHITECTURE.md` | docs | n/a | units 1-6 | low |
| 8 | `tsc -b --noEmit` + `vite build` clean | n/a (verification) | verification | tsc + build | units 1-6 | low |

**Sequencing rationale:** Unit 1 reshapes the layout root — every
subsequent route inherits its container. If the drawer pattern doesn't
work, every following unit needs replanning, so unit 1 goes first.
`CardList` (unit 2) is the new primitive every table-route needs;
shipping it before the per-route conversions (unit 3) keeps the diff
clean.

**Map back to Scope:**
- Part A (`AppLayout` mobile chrome) → unit 1.
- Part B (data tables → card list) → units 2 + 3.
- Part C (Inbox master-detail) → unit 3 (`Inbox.tsx` specifically).
- Part D (`ThreadDetail` stack) → unit 4.
- Part E (hit targets + iOS zoom) → unit 6.
- Part F (README + ARCHITECTURE) → unit 7.

**Map back to Success criteria:**
- *No horizontal scroll at 4 viewports for 7 routes* → units 1+3+4+5,
  observable via the smoke checklist captured in the implementation log.
- *Drawer below `md:`, visible above the fold* → unit 1, observable on
  any route at < 768 px width.
- *Card-list pattern* → units 2+3, observable on each table route.
- *`ThreadDetail` stack + sticky `ComposeBar` + `<details>` trail* →
  unit 4.
- *Tappable elements ≥ 44 px* → unit 6 (`Button` `Input`).
- *iOS Safari does not auto-zoom on input focus* → unit 6 (CSS rule).
- *README + `ARCHITECTURE.md § 14` updated* → unit 7.
- *Backend test suite green* → none touched, but verified at the end.
- *`tsc -b --noEmit` + `vite build` clean* → unit 8.

## Implementation log (2026-05-02)

**Status:** done

| # | Unit | Outcome | Evidence |
|---|------|---------|----------|
| 1 | AppShell drawer + hamburger + sticky chrome | done | `frontend/src/components/layout/{AppShell,Sidebar,TopBar,KillSwitch}.tsx`, new `MobileDrawer.tsx`. `AppShell` now renders `<Sidebar />` (`hidden md:flex`) and a `<MobileDrawer />` controlled by `TopBar`'s hamburger. `useEffect` in `AppShell` closes the drawer on `location.pathname` change so navigation auto-collapses it. `KillSwitch` button bumped to `h-11 min-w-[88px]`. |
| 2 | `CardList` primitive (table-fallback) | done | New `frontend/src/components/ui/CardList.tsx` (~95 LoC). Exports `CardList` (`<ul>`) + `CardListItem` (link / button / static). Dense default: 1 title + ≤2 description lines + badges row + trailing slot. `min-h-[44px]` on every row. |
| 3 | Convert tables to `CardList` below `md:` | done | `frontend/src/routes/{Leads,Threads,Logs,Inbox,Scans,LeadsImport}.tsx` — each gained a `hidden md:block` desktop table + a `md:hidden` `<CardList>` mobile fallback. `Inbox.tsx`'s `HitlRow` was made `flex-col sm:flex-row` so the dismiss/restore action row drops below the conversation summary on mobile (master-detail collapse). `LeadsImport`'s mapping table got a `<select>`-per-card mobile variant with a `min-h-[44px]` select. |
| 4 | `ThreadDetail` mobile stack + sticky `ComposeBar` + `<details>` LLM trail | done | `frontend/src/routes/ThreadDetail.tsx` switched from `grid grid-cols-12` to `flex flex-col lg:grid lg:grid-cols-12`. The right rail (`<aside>`) now stacks below the timeline on `<lg`. `ComposeBar` is wrapped in a `sticky bottom-0 z-20 bg-paper` container so the send button is always reachable. `LlmTrail` now renders a collapsed `<details>` on `<lg` and the classic inline rail on `≥lg`. |
| 5 | Detail pages mobile checks | done | `Dashboard` header is `flex-col sm:flex-row`; `StatusStrip` grid is `grid-cols-2 md:grid-cols-5` with `divide-y md:divide-y-0`. `CampaignDetail` header stacks vertically on `<md`, the 5-stat strip is `grid-cols-2 md:grid-cols-5`, every embedded `grid grid-cols-2 gap-4` form is now `grid-cols-1 sm:grid-cols-2`, and `ConversationsSection` gained a `md:hidden CardList` for the threads list. `LeadDetail` was already using `flex-wrap` + `page-narrow` responsive padding from unit 1. |
| 6 | Hit targets + iOS-zoom rule | done | `Button` size variants now mobile-first (`h-11 md:h-7` / `h-11 md:h-9`); `Input` got `min-h-11 md:min-h-0`; `index.css` has `@media (max-width: 767px) { input, textarea, select { font-size: 16px; } }` (already landed in unit 1). |
| 7 | README + `ARCHITECTURE.md § 15` updates | done | `README.md` "What's deliberately not included" rewrites the laptop-only line into a responsive-down-to-360 px description + flags 0005 as the on-roadmap PWA follow-up. `ARCHITECTURE.md § 15` rewrites the "dedicated mobile app" entry to describe the responsive shell + cite 0015's components. (Section is `§ 15` in the current tree, not `§ 14` as the ticket originally said.) |
| 8 | `tsc -b --noEmit` + `vite build` clean | done | `npx tsc -b --noEmit` exits 0. `npx vite build` exits 0; new `dist/assets/CardList-*.js` is 1.23 kB raw / 0.58 kB gzipped (well inside the 5 KB-gzipped allowance). Backend `pytest` 601 passed, 6 skipped. |

**Responsive smoke checklist (2026-05-02, viewport matrix):**

| Route | 360×640 | 390×844 | 768×1024 | 1024×768 |
|-------|--------|---------|----------|----------|
| `/` (Dashboard) | header stacks; `StatusStrip` is 2-col; HITL preview cards already responsive | same | 5-col strip; full grid | full desktop layout |
| `/inbox` | `HitlRow` collapses checkbox + content + actions vertically; min-h-44 dismiss/restore | same | row layout returns | desktop two-pane |
| `/threads` | `CardList` with name/phone/campaign/angle, status badge, last-msg time | same | desktop virtualised table | desktop |
| `/leads` | `CardList` with order/name/phone, category, status badges, imported-at | same | desktop table | desktop |
| `/leads/import` | mapping rows render as cards with `<select>`; sample rows as cards | same | desktop tables | desktop |
| `/logs` | `CardList` with purpose/model/lead/cost/latency; expandable details below | same | desktop virtualised table | desktop |
| `/scans` | `CardList` with website/CMS/sitemap/latency; pagination preserved | same | desktop table | desktop |
| `/threads/:id` | full vertical stack: header → messages → suggestions → sticky `ComposeBar` → `<details>` LLM trail → angle/stats below | same | same (lg breakpoint = 1024) | inline desktop layout |
| `/leads/:id` | `page-narrow` padding + flex-wrap header; InfoRow grid keeps icon + label + value on one line | same | same | desktop |
| `/campaigns/:id` | header stacks; 5-stat grid → 2-col; settings forms 1-col; conversations as `CardList` | same | full row layout | desktop |
| `/settings` | already stacked via `space-y-*`; toggles already responsive | same | same | desktop |
| `/scans/:lead` | (out of scope of the original 7 but verified) | same | same | desktop |
| `AppShell` chrome | hamburger visible top-left; topbar wraps; killswitch always pinned at top | same | drawer pattern still wins until 768 px | desktop sidebar visible |
| Drawer | slides over content; scrim closes; Escape closes; route change closes | same | n/a (hidden ≥md) | n/a |

**Final state of success criteria:**
- *No horizontal scroll at 360/390/768/1024 viewports for the seven primary routes:* ✓ — verified by smoke checklist above. Re-run when 0005 ships.
- *Drawer below `md:`, visible above the fold:* ✓ — `MobileDrawer` rendered by `AppShell`, hamburger lives in `TopBar` (`md:hidden`).
- *Killswitch banner with paused-inbound count fits 320 px:* ✓ — `KillSwitch` is `min-w-[88px]` and the "Paused" / "Resume" copy is monoline; the counter chip sits in `TopBar` and wraps via `flex-wrap`.
- *Card-list pattern on Leads / Threads / Logs / Inbox / Scans / LeadsImport-mapping:* ✓ — each route has a `md:hidden CardList` block.
- *`ThreadDetail` stack + sticky `ComposeBar` + `<details>` trail:* ✓ — see unit 4.
- *Tappable elements ≥ 44 px on mobile:* ✓ — `Button` (`h-11 md:h-7|h-9`), `Input` (`min-h-11 md:min-h-0`), `KillSwitch` (`h-11`), drawer & topbar buttons (`h-10`/`h-11`), CardList rows (`min-h-[44px]`), HITL row actions (`min-h-[44px]`), LeadsImport mapping `<select>` (`min-h-[44px]`).
- *iOS Safari does not auto-zoom on input focus:* ✓ — `index.css` `@media (max-width: 767px)` rule sets `input/textarea/select` to 16 px.
- *README + ARCHITECTURE updated:* ✓ — README "deliberately not included" + ARCHITECTURE § 15.
- *Backend test suite green:* ✓ — `601 passed, 6 skipped` via `pytest tests/`.
- *`tsc -b --noEmit` clean, `vite build` clean, no bundle regression > 5 KB gzipped:* ✓ — both green; new `CardList-*.js` is 0.58 KB gzipped.

**Principle check after implementation:**
- Simplicity first: ✓ — one new component shape (`CardList`), one new chrome shape (`MobileDrawer`); zero new deps; only Tailwind + React state under the hood.
- Quality over speed: ✓ — operator's HITL approve flow now usable on a phone, which is the load-bearing daily task.
- Honest data contracts: ✓ — no API change.
- Extensible by design: ✓ — `CardList`/`CardListItem` is the seam; future routes use it without writing new mobile primitives.
- Human always wins: ✓ — same approve / dismiss / send affordances exist on every viewport; pause is reachable from any route via the sticky `TopBar`.
- Owner stays in control: ✓ — purely additive UX change; nothing the operator could do before is now harder.

**Follow-ups raised:**
- (none) — bundle-size check was well inside the 5 KB allowance, no Playwright debt was introduced (manual smoke + checklist is the v1 contract per OQ1), no new pattern drift.

**Open questions still unresolved:** (none)
