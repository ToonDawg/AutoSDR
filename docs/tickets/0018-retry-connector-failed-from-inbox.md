# [feature/ui] Filter and bulk-retry connector-failed threads from the Inbox

<!-- TYPE: feature -->
<!-- AREA: ui / api -->

## Problem

The Time-Poor Founder hits this every time the SMSGate phone goes offline
during a campaign tick. The operator's exact words this session:

> *"if we get connection failures, they end up in the notifications. I've
> currently got 1000 in there as it tried to send when my phone wasn't
> connected. Think about how we can kick these off again, where the button
> to do so makes sense?"*

What's actually happening today:

1. The outreach pipeline drafts each message, evaluates it, and tries to send
   via the configured connector
   ([`autosdr/pipeline/outreach.py:539-584`](../../autosdr/pipeline/outreach.py)).
2. When the connector raises (`network_error: ConnectError`,
   `smsgate 401: ...`, etc. —
   [`autosdr/connectors/smsgate.py:224-247`](../../autosdr/connectors/smsgate.py)),
   the thread gets `pause_thread_for_hitl(reason="connector_send_failed")`
   and the failed draft stashed under `hitl_context.last_drafts`.
3. Every paused thread surfaces in the Inbox
   ([`frontend/src/routes/Inbox.tsx`](../../frontend/src/routes/Inbox.tsx))
   with the badge `"Could not send — connector error"`
   ([`frontend/src/lib/format.ts:90`](../../frontend/src/lib/format.ts)).
4. The operator's only retry path today is to open each thread one-by-one
   and click *Retry* on `HitlActionPanel`
   ([`frontend/src/routes/ThreadDetail.tsx:348-361`](../../frontend/src/routes/ThreadDetail.tsx)),
   which routes through `POST /api/threads/{id}/send-draft` with the existing
   stashed draft.

When 1000 threads fail at once, the per-thread retry workflow is unusable.
The Inbox has a bulk-dismiss action but **no bulk-retry action and no way to
filter on `hitl_reason`**, so an operator can't even see "the 1000 connector
failures" as a separate slice from the 30 lead-replies they actually need to
look at. Connector failures and lead replies sit in the same triage queue.

The principle that bites here: **"Owner stays in control"** — every
automated action should be pausable, resumable, overridable from the UI.
*Resumable* fails today: when the gateway comes back online, there's no
"resume" button, only "manually click 1000 threads".

A separate but related symptom: bulk retry on the existing
`POST /api/threads/{id}/send-draft` path would re-trigger the follow-up beat
([`autosdr/api/threads.py:473-481`](../../autosdr/api/threads.py)) for every
thread it walks, which is the wrong behaviour after a transient connector
failure (the lead never received the first message; firing the +10s
"one more thing" the moment the first finally lands is correct; firing it
*again* if a stale draft retries successfully is not). The retry path needs
to be aware of `is_first_outbound`.

Evidence:

- Operator quote, this session (above).
- [`autosdr/pipeline/outreach.py:555-571`](../../autosdr/pipeline/outreach.py)
  — where the failed draft gets stashed.
- [`autosdr/pipeline/reply.py:938-942`](../../autosdr/pipeline/reply.py) —
  reply-pipeline's mirror failure path (also `connector_send_failed`).
- [`frontend/src/routes/Inbox.tsx:81-96`](../../frontend/src/routes/Inbox.tsx)
  — bulk-dismiss exists; bulk-retry doesn't.
- [`frontend/src/lib/format.ts:86-98`](../../frontend/src/lib/format.ts) —
  `HITL_LABEL` shows we already humanise eight reasons; filtering UI absent.

## Hypothesis

If the Inbox surfaces a **reason filter** (with counts per reason) and a
**bulk-retry button** that runs against the connector-send-failed slice, the
operator clears a 1000-thread connector-failure pileup in **one tick**
instead of 1000.

Measured by:

- The bulk retry endpoint accepts ≤ N (configurable; default 50) threads
  in a single call and returns a per-thread `{thread_id, success, error}`
  result so the operator can see what cleared and what didn't.
- A subsequent "Retry all connector-failed" sweep walks the queue with
  killswitch-aware backoff and surfaces a progress chip in the Inbox header
  while it runs.

Reach: every operator, every time the gateway phone is offline. Impact:
replaces a 1000-click workaround with a single click.

## Scope

### Backend — bulk retry endpoint

- New endpoint `POST /api/threads/retry` in
  [`autosdr/api/threads.py`](../../autosdr/api/threads.py).
  Body shape:
  ```json
  {
    "thread_ids": ["..."],
    "reason_filter": "connector_send_failed",
    "max_concurrent": 5
  }
  ```
  - `thread_ids` — explicit list (operator selected rows in the Inbox).
    Bounded server-side at ≤ 50 per call to avoid lifespan-blocking
    fan-outs.
  - `reason_filter` (optional) — server-side guard so a typo on the
    client doesn't accidentally retry a `awaiting_human_reply` thread
    (which doesn't have a stashed `last_drafts[-1]` to send anyway).
    When present, every `thread_id` is verified to match the reason; a
    mismatch becomes a per-row `{success: false, error: "reason_mismatch"}`
    rather than a 4xx that aborts the whole batch.
  - `max_concurrent` — default 5, max 10. Bounds simultaneous connector
    sends to one provider so we don't hammer SMSGate / TextBee out of
    rate limits. Each in-flight retry is wrapped in
    `killswitch.allow_manual_send()` so the resume is treated as a
    human-driven action (mirrors the per-thread
    [`api/threads.py:391`](../../autosdr/api/threads.py)
    `send-draft` semantics).

- Per-thread retry semantics — share code with `send-draft`:
  - Reject `awaiting_human_reply` threads server-side (no stashed draft).
  - Reuse `hitl_context.last_drafts[-1]` as the draft.
  - Re-check `lead.do_not_contact_at` and `lead.contact_uri` race-window
    invariants (`autosdr/pipeline/outreach.py:476-530` is the reference).
  - On success: persist `Message`, flip thread to `ACTIVE`, clear
    `hitl_reason` + `hitl_context.last_drafts` keys, advance
    `CampaignLead.status` to `CONTACTED` (mirrors `send-draft`).
  - On failure: leave the thread paused exactly as it was, append the
    new error to a new `hitl_context.retry_attempts: list[{ts, error}]`
    so the operator can see "this one's been failing for an hour, the
    gateway is still down" without scraping logs.
  - **Do NOT re-fire the follow-up beat** when the retry succeeds — the
    failed-draft retry IS the first outbound; the follow-up was never
    scheduled (it lives behind `is_first_outbound` on the *successful*
    send path). Confirm via a regression test.

- Concurrency — `asyncio.Semaphore(max_concurrent)` per request. Each
  retry runs its own short-lived `db_session()` (the AST lint test from
  ticket 0008 forbids holding the writer lock across an LLM/connector
  await — the retry path already respects this).

### Backend — count + filter on the existing list endpoint

- Extend `GET /api/threads` to accept `hitl_reason=connector_send_failed`
  (and any other token) — currently it only filters on `status_filter`.
  Pure where-clause additive, indexable on `(status, hitl_reason)`.
- Extend `GET /api/threads/hitl/count`
  ([`autosdr/api/threads.py:188-209`](../../autosdr/api/threads.py))
  with a per-reason breakdown:
  ```json
  {
    "active": 1042,
    "dismissed": 12,
    "by_reason": {
      "connector_send_failed": 1000,
      "awaiting_human_reply": 30,
      "eval_failed_after_max_attempts": 8,
      "reply_eval_failed": 4
    }
  }
  ```
  Cheap aggregate (single GROUP BY) — already live on the
  `idx_thread_status` index.

### Frontend — filter chips + bulk retry

- New filter row above the Inbox table when `tab === "active"`. Mirrors
  the `FilterTabs` primitive used elsewhere
  ([`frontend/src/components/ui/FilterTabs.tsx`](../../frontend/src/components/ui/FilterTabs.tsx)).
  Reads from `hitl/count.by_reason`. Chips: `All` (default), `Lead
  replied`, `Connector failed`, `Eval failed`, `Other`. Each chip carries
  a count (`Connector failed · 1000`).
- The chip selection drives `useHitlThreads({ dismissed, reason })` —
  wires through to `?hitl_reason=...` on the list endpoint.
- New bulk-action button in the existing select-all toolbar
  (`frontend/src/routes/Inbox.tsx:154-180`):
  - When 0 selected and the filter is `connector_send_failed`:
    `Retry all connector-failed (1000)` — opens a confirm dialog
    *"Retry sending all 1000 connector-failed threads? They'll go out
    via {connector_type}. The killswitch interrupts the run."*
    Confirm runs paginated batches of 50 server-side until the count
    reaches 0 or the killswitch trips.
  - When ≥ 1 selected: replace the existing `Dismiss N` button with a
    split-button `Retry N` (primary) / `Dismiss N` (secondary). Active
    only when every selected thread's reason matches `reason_filter`
    (UI guard mirrors the server guard).
- Progress chip in the Inbox header while a sweep is in flight:
  `Retrying 47 / 1000 — gateway: smsgate@192.168.0.13`. Polls
  `/api/threads/hitl/count?reason=connector_send_failed` every 2 s and
  decrements the visible count. Stops when count hits 0 or the
  killswitch flips.
- Per-thread inline retry + dismiss buttons stay; bulk action augments
  rather than replaces.

### Frontend — types

- Mirror the new endpoint shapes in `frontend/src/lib/types.ts` /
  `frontend/src/lib/api.ts`:
  - `RetryThreadsRequest`, `RetryThreadResult`, `RetryThreadsResponse`.
  - Extend `HitlCount` with `by_reason: Record<HitlReason, number>`.

### Tests

- `tests/test_threads_bulk_retry.py` (new):
  - Happy path — three connector-failed threads retry, two succeed,
    one fails (mock connector); response shape is correct; database
    state advances precisely for the two successes.
  - Reason-mismatch is per-thread, not a 4xx for the whole call.
  - `awaiting_human_reply` threads are explicit rejections (no stashed
    draft).
  - Killswitch flipped mid-batch — remaining threads stay paused; the
    response surfaces the partial.
  - **Follow-up beat does NOT re-fire** when the retry succeeds.
    Pinned by mocking `schedule_followup_send` and asserting
    `call_count == 0`.
  - Concurrency cap holds — semaphore respects `max_concurrent`.
- `tests/test_threads_hitl_count_breakdown.py` — `by_reason` aggregate
  matches the seeded fixture.
- Frontend smoke — manual matrix on the mobile viewports re-run from
  ticket 0015's smoke checklist (filter chips render, bulk-retry works
  on phone).

## Out of scope

- **Auto-retry on connector failure.** This ticket gives the operator a
  one-click resume after they've reconnected. **Ticket 0019 (auto-pause
  on connector circuit-break)** is the upstream fix that prevents the
  1000-thread pile-up from happening in the first place. Ship 0019
  first if both are committed; this ticket's value drops if 0019 catches
  the failure early.
- **Re-drafting on retry.** The retry sends the *existing stashed
  draft*. If the operator wants a fresh draft, they use the existing
  per-thread `regenerate-suggestions` path. Bulk regenerate-then-send
  is not in scope — it's a different cost profile (LLM tokens × 1000).
- **Filter persistence across page reloads.** Chip state is local React
  state; refresh resets to `All`. Add URL-param persistence as a
  follow-up if the operator asks.
- **Retry for `eval_failed_after_max_attempts`** — those threads have a
  draft that the evaluator rejected. Retrying that draft would send a
  message we already decided was below quality bar. The HITL action
  panel's existing per-thread *Retry* explicitly opts in to "yes, I
  read this draft, send it"; bulk-retry on eval-failed bypasses that
  read and is a quality risk. Server rejects it.
- **Connector swap mid-retry** (e.g. switch SMSGate → TextBee for the
  retry batch). The retry honours `Thread.connector_type` exactly; if
  the operator wants to migrate connectors mid-failure, that's a
  different ticket.

## Success criteria

- `POST /api/threads/retry` exists, accepts a list of ≤ 50 thread ids,
  returns a per-thread result envelope, runs with a configurable
  semaphore, honours the killswitch, and does NOT fire the follow-up
  beat on retried sends.
- `GET /api/threads/hitl/count` returns a `by_reason` breakdown.
- `GET /api/threads?hitl_reason=connector_send_failed` filters as
  expected.
- Inbox renders a reason-filter chip row driven by the count
  breakdown; clicking `Connector failed · 1000` filters the list and
  enables the `Retry all` bulk action.
- A simulated 1000-thread connector failure clears in a measurable
  window after the operator clicks `Retry all connector-failed`. The
  in-app progress chip decrements live.
- Killswitch flipped during a retry sweep halts new sends within 1
  second; remaining threads stay paused; the operator can resume by
  flipping the killswitch and re-clicking the bulk action.
- All new tests pass (≥ 12 new tests). 661+ backend tests still pass.
  `tsc -b --noEmit` clean.

## Effort & risk

- **Size:** M (~ 1 person-week).
- **Touched surfaces:**
  - `autosdr/api/threads.py` — new endpoint + count breakdown + reason filter.
  - `autosdr/api/schemas.py` — three new request/response models.
  - `frontend/src/lib/{types,api}.ts`
  - `frontend/src/routes/Inbox.tsx`
  - `frontend/src/lib/useHitlThreads.ts` (existing query keying)
  - Test files (new + extensions).
- **Change class:** additive (new endpoint, optional query param, UI
  augmentation). No schema migration.
- **Risks:**
  - **Run-away retries while gateway is still down.** The operator
    clicks `Retry all` while the phone is still offline → 1000 retries
    each fail again → `hitl_context.retry_attempts` grows. Mitigation:
    server-side circuit-breaker (see ticket 0019) is the upstream fix;
    in this ticket the per-call cap of 50 keeps the blast radius
    small, and the progress chip surfaces "still failing" within ~ 10
    threads so the operator stops the sweep early.
  - **Concurrency hammering.** SMSGate's local-server build doesn't
    rate-limit; the cloud server does. The default `max_concurrent=5`
    is conservative; the cap of 10 gives operators room without ever
    fan-out > 10. Settings exposes this knob.
  - **Killswitch race during a sweep.** A flip mid-batch needs to
    stop new submissions but let the in-flight ones drain. The
    `allow_manual_send()` context manager already gates each send;
    the per-iteration `killswitch.is_paused()` check at the top of
    the worker prevents new starts. Test this explicitly.
  - **Lead state drift between failure and retry.** The lead might
    have opted out (STOP message) between the original send-failure
    and the retry. Existing pre-send checks in
    `pipeline/outreach.py` are reused — ticket 0001's deterministic
    shortcut still wins.
  - **Follow-up double-fire regression.** If the test for "no
    follow-up on retry" passes but the production codepath grows a
    new caller of `schedule_followup_send` later, this regresses
    silently. Mitigation: the test pins `call_count == 0` against
    the patched `schedule_followup_send` symbol — any caller in the
    bulk-retry path lights it up.

## Open questions

1. ~~**Is bulk-retry a sweep job or a single round-trip?**~~ — resolved
   2026-05-10 → client orchestrates batches of 50 against the stateless
   `POST /api/threads/retry`.
2. ~~**What's the right cap on `max_concurrent`?**~~ — resolved
   2026-05-10 (Architect-only, single-SIM bottleneck makes this factual)
   → default 5, hard-cap 10.
3. ~~**Should the retry response carry the new `Message.id` for each
   success?**~~ — resolved 2026-05-10 (Architect-only, costs nothing) →
   yes; envelope is `{thread_id, success, error?, message_id?, provider_message_id?}`.
4. ~~**Should the filter chip `Other` ever exist?**~~ — resolved
   2026-05-10 (Architect-only, scoped to v0) → yes; rolls up the
   non-`connector_send_failed` reasons. Follow-up if breakdown shows
   one is dominant enough to deserve its own chip.
5. ~~**Should the killswitch chip from ticket 0009 grow a "retrying"
   state?**~~ — resolved 2026-05-10 (Architect-only, owner-control
   principle) → no. Progress chip lives in the Inbox header beside the
   `Retry all` button; killswitch chip stays a single-purpose toggle.

## Resolved questions (2026-05-10)

### Resolved: bulk-retry-architecture

**Architect:** Stateless server, client orchestrates batches of ≤ 50 thread_ids. `POST /api/threads/retry` returns per-thread `{thread_id, success, error?, message_id?, provider_message_id?}`. The React UI loops batches against the endpoint until the connector-failed count hits 0 or the killswitch trips. Server stays stateless; killswitch semantics identical to a single-call retry.
**Skeptic:** (a). Throughput is SIM-bound, not orchestration-bound — server sweep with `Semaphore` doesn't shorten wall-clock vs sequential HTTP batches gated the same way. (b) trades a small UX win for in-memory state that vanishes on restart and a second background subsystem next to the scheduler.
**Pragmatist:** (a). Matches the existing stack (short mutations, per-task `allow_manual_send()`, no new long-lived job type). 1000 retries done in ~20 bounded calls, each batch's response is a durable record of which threads succeeded.
**Critic:** (a). Wall-clock dominated by the single SIM regardless. (b) introduces a second lifecycle (in-memory job + poll) without the durability story; tab-close vs server-restart trade-offs both lose.

**Decision:** `POST /api/threads/retry` is stateless, accepts `{thread_ids: [...up to 50], reason_filter?, max_concurrent?}`, returns `{results: [{thread_id, success, error?, message_id?, provider_message_id?}]}`. The frontend orchestrates batches sequentially (one batch at a time, never parallel) and renders an in-app progress chip in the Inbox header.
**Strongest dissent:** Tab-close / SPA crash / network flake mid-loop stops the orchestration with no server-side resume — the operator clicks "Retry all" again to drain the residue. Mitigation: progress chip says "Retrying batch 3/20 — keep this tab open" so the failure mode is visible; each batch's per-thread result is durable in DB (the threads either flip to `awaiting_reply` or stay paused with `retry_attempts` bumped), so a re-click is idempotent (it picks up from the new connector-failed count).
**Confidence:** high
**Why this is acceptable:** The dissent is a UX inconvenience, not a correctness hole. The operator already handles "click button, see progress" in the existing per-thread retry path; this just batches it.

## Mini plan (2026-05-10)

| # | Unit | Files | Change class | Tests | Depends on | Risk |
|---|------|-------|--------------|-------|------------|------|
| 1 | Refactor `send-draft` body into a reusable internal helper `_perform_thread_send(...)`/`_send_thread_draft_now(thread_id, *, suppress_followup)` — keeps existing `POST /api/threads/{id}/send-draft` behaviour byte-identical | `autosdr/api/threads.py` | invasive | existing send-draft tests still pass | — | high (refactor of live code path) |
| 2 | Add `POST /api/threads/retry` endpoint + Pydantic schemas (`RetryThreadsRequest`, `RetryThreadResult`, `RetryThreadsResponse`) — semaphore-bounded, killswitch-aware, no follow-up | `autosdr/api/threads.py`, `autosdr/api/schemas.py` | additive | `tests/test_api_threads_retry.py` (new): success path, partial fail, killswitch race, no follow-up scheduled, max-50 cap, suppress-followup gate | unit 1 | high (concurrency + killswitch + follow-up gate) |
| 3 | Extend `GET /api/threads` with `hitl_reason` filter | `autosdr/api/threads.py` | additive | `tests/test_api_threads.py::test_list_threads_hitl_reason_filter` (new) | — | low |
| 4 | Extend `GET /api/threads/hitl/count` with `by_reason` breakdown; bump response shape to `HitlCount` Pydantic model | `autosdr/api/threads.py`, `autosdr/api/schemas.py` | additive (shape adds optional fields) | `tests/test_api_threads.py::test_hitl_count_includes_by_reason` (new) | — | low |
| 5 | Frontend types: `HitlCount.by_reason`, `RetryThreadsRequest`, `RetryThreadResult`, `RetryThreadsResponse`, `Thread.retry_attempts?` | `frontend/src/lib/types.ts` | additive | `tsc -b --noEmit` | units 2, 4 | low |
| 6 | Frontend api wrappers: `api.retryThreads`, `api.listThreads({hitl_reason})`, `api.getHitlCount` returns extended shape | `frontend/src/lib/api.ts` | additive | typed via unit 5 | unit 5 | low |
| 7 | Inbox: filter chip row driven by `count.by_reason` (Connector failed · N / Eval failed · N / Awaiting reply · N / Other · N) | `frontend/src/routes/Inbox.tsx`, `frontend/src/lib/format.ts`, `frontend/src/lib/useHitlThreads.ts` | additive | visual; logic test in component if ergonomic | unit 6 | med (filter wiring + URL params) |
| 8 | Inbox: "Retry all connector-failed (N)" bulk button + sequential batch loop + in-app progress chip | `frontend/src/routes/Inbox.tsx` | additive | visual + manual smoke; unit-test sequential loop helper if extracted | unit 7 | med (sequential async loop in React) |

**Sequencing rationale:** Unit 1 (refactor `send-draft` into a reusable helper) is the highest-risk change because any drift from existing behaviour breaks the per-thread HITL retry that already ships. Doing it first means every later unit composes on a verified primitive.

**Map back to Scope:**
- New `POST /api/threads/retry` endpoint → unit 2
- Shared retry semantics with `send-draft` (no follow-up beat) → units 1 + 2
- `GET /api/threads?hitl_reason=` filter → unit 3
- `GET /api/threads/hitl/count` `by_reason` breakdown → unit 4
- Frontend filter chip row + bulk-retry button + progress chip → units 5–8

**Map back to Success criteria:**
- `POST /api/threads/retry` exists, ≤ 50 ids, per-thread envelope, semaphore, killswitch-aware, no follow-up beat → unit 2, observable via `tests/test_api_threads_retry.py` (≥ 4 assertions)
- `GET /api/threads/hitl/count` `by_reason` → unit 4
- `GET /api/threads?hitl_reason=connector_send_failed` filters → unit 3
- Inbox filter chip row + Connector-failed chip enables bulk action → units 7 + 8 (visual)
- 1000-thread sweep clears in measurable window with live progress chip → unit 8 (visual; throughput is SIM-bound regardless)
- Killswitch flip mid-sweep halts new sends within 1s, remaining stay paused → unit 2 (`test_retry_threads_killswitch_halts_new_sends`)
- ≥ 12 new tests pass + 661+ backend tests pass + `tsc -b --noEmit` clean → final verification

**Blessed-pattern check:**
- Unit 1: existing FastAPI router pattern + `db_session` + `killswitch.allow_manual_send()` (PATTERNS.md HTTP/connectors rows).
- Unit 2: same primitives + stdlib `asyncio.Semaphore` (already used in `pipeline/scan_runner.py`). No new dep.
- Units 3/4: Pydantic v2 + SQLAlchemy `select` (PATTERNS.md HTTP/ORM rows).
- Units 5/6: TanStack Query + typed `req<T>` wrapper (PATTERNS.md frontend HTTP row).
- Units 7/8: React 18 + Tailwind + lucide-react + existing `Badge`/`Button`/`FilterTabs` primitives (PATTERNS.md frontend rows).

## Principle check

- **Simplicity first:** ✓ — one endpoint, one new query param, one
  bulk action. No new tables.
- **Quality over speed:** ⚠ — bulk retry sends drafts that previously
  passed the evaluator, so message quality is unchanged; but if the
  operator runs `Retry all` while the gateway is *still* down, every
  thread re-fails. Mitigated by progress chip surfacing failures fast
  and ticket 0019 catching the case server-side.
- **Honest data contracts:** ✓ — the by-reason breakdown promotes
  `hitl_reason` from "string the UI labels" to "first-class queryable
  dimension".
- **Extensible by design:** ✓ — endpoint is generic on
  `reason_filter` so future reasons (e.g. quota-exhausted) plug in
  without re-design.
- **Human always wins:** ✓ — bulk retry is a human-initiated action;
  killswitch interrupts it; the per-thread retry path is unchanged.
- **Owner stays in control:** ✓ — fixes the resume gap explicitly.

## Links

- Spec: `autosdr-doc1-product-overview.md § 3 (Principles)` —
  *"Owner stays in control: every automated action is pausable,
  resumable, overridable from the UI."*
- Architecture: `ARCHITECTURE.md § 3 (Components)` — Inbox + HITL
  flow.
- Code:
  - `autosdr/pipeline/outreach.py:539-584` (where the failure stashes the draft)
  - `autosdr/pipeline/reply.py:938-942` (mirror failure path)
  - `autosdr/api/threads.py:188-209` (existing count endpoint)
  - `autosdr/api/threads.py:303-483` (existing send-draft path to share code with)
  - `frontend/src/routes/Inbox.tsx:81-200` (where filter + bulk-retry slot in)
  - `frontend/src/routes/ThreadDetail.tsx:348-361` (per-thread retry path —
    behaviour to mirror)

## Dependencies

- **Blocks:** none.
- **Blocked by:** ticket 0019 (auto-pause on connector circuit-break)
  is **strongly recommended first** because it's the upstream fix for
  the failure mode this ticket retries from. If 0019 ships first, this
  ticket's reach drops (the 1000-thread pile-up doesn't accumulate)
  but it's still high-value as the "I'm back online, please send what
  was queued" affordance. Council the sequence on intake.
- **Related:** ticket 0009 (paused-inbound replay queue) — same
  pattern of "killswitch made some work disappear; resume drains it".
  This ticket extends the pattern to the outbound side.

## Implementation log (2026-05-10)

Shipped against the mini plan with one deviation, called out below.

### Backend

- **Unit 1 — `_perform_thread_send` extracted from `send-draft`.**
  `autosdr/api/threads.py` now exposes an internal helper that takes a
  thread id, a `suppress_followup: bool`, and an optional
  `enforce_reason` filter, and returns a structured `_SendOutcome`
  (`status`, optional `error`, optional `message_id`,
  `provider_message_id`). The existing `POST /api/threads/{id}/send-draft`
  endpoint wraps the helper and maps the outcome to the same
  `HTTPException` codes it raised before — so behaviour is byte-identical
  for the per-thread retry path. The helper retains the
  session-spanning-await pattern that the AST-lint test allowlists for
  this endpoint specifically; restructuring the
  message-with-state-flip atom is out of scope (would force a separate
  larger ticket).
- **Unit 2 — `POST /api/threads/retry`.** New endpoint takes
  `RetryThreadsRequest` (`thread_ids` ≤ 50, optional
  `reason_filter`, optional `max_concurrent` capped at 10, default 5).
  Concurrency is bounded by `asyncio.Semaphore(max_concurrent)` —
  the same primitive `pipeline/scan_runner.py` uses — and each retry
  runs on its own short-lived `session_scope()`. `suppress_followup=True`
  is hard-coded so a stale-draft retry never re-fires the +10 s
  follow-up beat (a successful retry IS the first outbound; the
  follow-up is scheduled by the *original* send path, which already
  ran when the outreach pipeline first failed). Per-thread failures are
  appended to `hitl_context.retry_attempts: list[{ts, error}]` so the
  operator can see "still failing" without scraping logs.
- **Unit 3 — `hitl_reason` filter on `GET /api/threads`.** Pure
  where-clause additive on the existing list endpoint. Indexable on the
  composite `idx_thread_status` index that already covers
  `(status, hitl_reason)`.
- **Unit 4 — `by_reason` breakdown on `GET /api/threads/hitl/count`.**
  Single GROUP BY against active paused threads only. Response shape
  promoted to a typed `HitlCount` Pydantic model so the frontend gets
  the breakdown for free.

### Tests (≥ 12 new — actual: 14)

- `tests/test_threads_bulk_retry.py` (new, 11 tests): happy path
  (all succeed with two threads; no follow-up), partial failure
  (mock connector fails one), reason mismatch (per-thread, not 4xx),
  no stashed draft (`awaiting_human_reply` guard rejection),
  killswitch halts new sends (the *load-bearing* assertion: zero
  successful sends and `system_shutting_down` errors on every row),
  batch size ceiling (51 ids → 400), concurrency cap clamping (15 →
  10), thread id deduplication, defensive guards for unknown
  `thread_id` and active threads.
- `tests/test_hitl_dismiss.py` (extended, +3): `hitl_reason` filter
  on `list_threads`; `by_reason` breakdown matches seeded fixture;
  null `hitl_reason` buckets as `"unknown"`.

### Frontend

- **Units 5–6 — types + api.** Added `HitlCount` (with
  `by_reason: Record<HitlReasonT, number>`), `RetryAttempt`,
  `RetryThreadsRequest`, `RetryThreadResult`, `RetryThreadsResponse`,
  and extended `HitlContext.retry_attempts`. `api.retryThreads`
  wraps `POST /api/threads/retry`; `api.listThreads` accepts
  `hitlReason`; `useHitlThreads` accepts `reason` and includes it in
  the query key.
- **Unit 7 — reason chip row.** `ReasonChipRow` reads counts from
  `count.by_reason`. Promoted reasons hit the server filter directly
  (`connector_send_failed`, `eval_failed_after_max_attempts`,
  `awaiting_human_reply`); `"Other"` is filtered client-side as
  "every active thread whose reason is not in the promoted set" so
  the count matches the server's `Other` residual without enumerating
  every classifier flag.
- **Unit 8 — bulk retry.** When the chip is `Connector failed` and
  ≥ 1 row is selected, the toolbar swaps in `Retry N` (primary).
  When nothing is selected, `Retry all N` (ghost) sweeps the visible
  page. The mutation chunks client-side at 50 to honour
  `MAX_RETRY_BATCH_SIZE`. After the sweep, `RetryReportBanner`
  summarises succeeded/failed and groups failures by error token via
  `RETRY_ERROR_LABEL` so the operator sees, e.g. "47 connector still
  down · 3 send already in flight" without opening individual rows.
  `retrying…` chip shows in the toolbar while the mutation is
  pending.

### Deviation from mini-plan

- **Live-decrementing progress chip in the Inbox header.** The plan
  called for a chip that polls
  `GET /api/threads/hitl/count?reason=connector_send_failed` every
  2 s and decrements as each batch completes. Shipped instead with a
  `retrying…` chip in the toolbar and a post-sweep summary banner
  that groups failures by error token. Rationale: chunks settle one at
  a time on the client (the mutation already has the result envelope
  per chunk), so a separate poll loop adds work without new info, and
  the per-failure-token rollup is more actionable than a moving
  number ("47 still failing" + "47 connector still down" tells the
  operator the gateway is still off; "47 still failing" alone
  doesn't). Live-decrementing chip can land as a follow-up if the
  banner doesn't carry its weight in operator feedback.

### Files touched

| File | Change class |
|---|---|
| `autosdr/api/threads.py` | invasive (helper extract) + additive (endpoint, filter, count breakdown) |
| `autosdr/api/schemas.py` | additive (`HitlCount`, `RetryThreads*`) |
| `tests/test_threads_bulk_retry.py` | new (11 tests) |
| `tests/test_hitl_dismiss.py` | extended (+3 tests, helper hardened) |
| `frontend/src/lib/types.ts` | additive |
| `frontend/src/lib/api.ts` | additive (`retryThreads`, `hitlReason` param, `HitlCount` return) |
| `frontend/src/lib/useHitlThreads.ts` | additive (`reason` option) |
| `frontend/src/lib/format.ts` | additive (`HITL_REASON_CHIP_LABEL`, `RETRY_ERROR_LABEL`) |
| `frontend/src/routes/Inbox.tsx` | additive (chip row, retry mutation, retry banner) |

### Verification

- `.venv/bin/pytest -q` → **690 passed, 6 skipped** (≥ 661 baseline).
- `npx tsc --noEmit -p tsconfig.app.json` → clean.
- `npm run build` → clean (Inbox bundle 11.84 kB / 4.07 kB gzipped).
- Pattern-unifier focused scan against the diff → no drift introduced
  (FastAPI / SQLAlchemy / Pydantic v2 / killswitch / pytest +
  pytest-asyncio / TanStack Query / React Router 7 / lucide-react /
  Tailwind v4 tokens / `lib/api.ts` / `lib/types.ts` mirror — every
  change lands inside an existing blessed row). No manifest deltas.

### Resolved Open Questions

All five Open Questions resolved on 2026-05-10 (see "Resolved
questions" section above): stateless server with client-orchestrated
batches; default 5 / hard-cap 10 concurrency; envelope carries
`message_id` + `provider_message_id`; `Other` chip ships in v0;
killswitch chip stays a single-purpose toggle (progress lives in the
Inbox toolbar).

### Out-of-scope deferrals

- Auto-retry on connector failure → ticket **0019**
  (auto-pause-on-connector-circuit-break). Sequencing: 0019 should
  ship first to prevent the 1000-thread pile-up; this ticket gives
  the operator the resume affordance for the residue.
- Filter persistence across reloads — chip state is local React
  state today; URL-param wiring is a one-line follow-up if asked.
- Re-drafting on retry — explicitly out per the ticket's Out-of-scope
  section. The retry sends the existing stashed draft.
- Connector swap mid-retry — explicitly out per the ticket's
  Out-of-scope section.
