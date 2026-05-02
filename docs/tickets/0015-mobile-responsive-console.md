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

- **OQ1.** Visual-regression tooling: introduce Playwright now, or
  manual smoke + a checklist for v1? Recommend manual + checklist
  for v1 (don't add CI surface for one operator), and revisit if a
  second contributor lands. Decision.
- **OQ2.** Card-list design: dense (3 lines per card, ~88 px tall) or
  airy (5 lines, ~140 px)? Recommend dense — operator's most-common
  task is *scan a list and find the one that's been replied*, not
  *read a record's metadata at length*. They tap into the detail
  page for that. Decision.
- **OQ3.** Should `ComposeBar` on mobile auto-expand to the
  on-screen-keyboard offset (use `interactive-widget=resizes-content`
  via the `viewport` meta tag's `interactive-widget` setting), or
  let the keyboard cover it? Recommend let the browser's default
  behaviour handle it for v1; revisit if operators report missing
  the send button under the keyboard. Decision.
- **OQ4.** Killswitch banner: dismiss-on-mobile or always-visible
  while paused? Recommend always-visible; the banner *is* the "you
  are paused" affordance. Decision.

Resolve via council mini-round before implementation, per the
ticket-implementer workflow. (OQ1 is the only one with two credible
defaults; OQ2/3/4 are simple judgement calls.)

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
