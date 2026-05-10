# [feature/connectors] Auto-pause campaigns when the connector circuit-breaks

<!-- TYPE: feature -->
<!-- AREA: connectors / scheduler -->

## Problem

The Time-Poor Founder describes the failure mode in their own words:

> *"I've currently got 1000 in there as it tried to send when my phone wasn't
> connected."*

What actually happened:

1. The SMSGate Android phone went offline (lock screen, low battery, walked
   out of WiFi range — pick one).
2. The scheduler kept ticking. Every campaign tick claimed up to
   `max_batch_per_tick` leads and tried to send.
3. Each `connector.send(...)` raised an `httpx.HTTPError` (`Connection
   refused`, `Read timeout`)
   ([`autosdr/connectors/smsgate.py:209-227`](../../autosdr/connectors/smsgate.py)).
4. Each failure parked the thread for HITL with reason
   `connector_send_failed`
   ([`autosdr/pipeline/outreach.py:555-571`](../../autosdr/pipeline/outreach.py))
   and the `consecutive_failures` counter on the connector instance ticked
   up
   ([`autosdr/connectors/smsgate.py:225,230,249`](../../autosdr/connectors/smsgate.py)).
5. The scheduler kept ticking. The campaign kept claiming. The pile grew.
6. Hours later the operator's Inbox is 1000 threads deep.

The connector already counts failures (`self.consecutive_failures`) but
**nothing reads that counter**. It increments and decrements forever, never
informs the scheduler, never pauses the campaign, never warns the operator.
By the time the operator notices "the Inbox is on fire", they're already in
ticket 0018's bulk-retry territory.

This is the upstream fix. Ticket 0018 is the resume affordance for after the
circuit blew. **0019 prevents the circuit from blowing in the first place.**

The principle that bites here is **"Owner stays in control"** *and*
**"Honest data contracts"** — the connector's health is a fact about the
system, but the system doesn't surface it as a fact. The scheduler thinks
campaigns are still healthy because nothing's told it otherwise.

A second principle is *"Simplicity first"* — the simplest fix that works:
the connector already knows; the scheduler already runs; we just need a
trip-wire between them.

Evidence:

- Operator's 2026-05-10 quote (above).
- [`autosdr/connectors/smsgate.py:169`](../../autosdr/connectors/smsgate.py)
  — `self.consecutive_failures = 0` initialised but never read outside
  the connector.
- [`autosdr/connectors/textbee.py`](../../autosdr/connectors/textbee.py) —
  no equivalent counter at all (gap).
- [`autosdr/scheduler.py`](../../autosdr/scheduler.py) — outreach tick has
  no concept of "the connector is down, skip this tick".
- [`autosdr/api/status.py`](../../autosdr/api/status.py) — `/api/status`
  surfaces connector type but not connector health.
- The connector probe `validate_config(...)`
  ([`autosdr/connectors/smsgate.py:436-488`](../../autosdr/connectors/smsgate.py))
  exists — used at Settings save time only, not as a runtime liveness probe.

## Hypothesis

If we add a tiny **circuit breaker** to the `BaseConnector` ABC that:

1. Tracks consecutive send failures and consecutive successes uniformly
   across SMSGate, TextBee, Override, and File.
2. Trips after **N consecutive failures** (default 3) into a `tripped`
   state — visible to the scheduler.
3. Causes the scheduler to **skip outreach ticks** while tripped (inbound
   poll keeps running — it has its own failure logic and replies still
   matter).
4. Surfaces a banner in the operator console *"SMSGate gateway unreachable —
   campaigns paused"* with a one-click "Test connection" / "Resume
   anyway" affordance.
5. Auto-resumes once a connector probe succeeds (probe runs every M
   seconds while tripped).

…then **the 1000-thread connector-failure pile-up cannot happen** by
construction. The breaker trips on failure 3, the scheduler stops, and the
remaining 997 leads stay queued — not paused-for-HITL.

Measured by:

- A simulated SMSGate outage during a 100-lead campaign produces ≤ 5
  `connector_send_failed` threads (3 to trip the breaker + ≤ 2 in-flight
  before the next scheduler tick reads the tripped state). Today: 100.
- Operator notices the outage within one tick (≤ 60 s) instead of "next
  time I open the laptop".

## Scope

### Backend — connector health on the ABC

- New `ConnectorHealth` dataclass in
  [`autosdr/connectors/base.py`](../../autosdr/connectors/base.py):
  ```python
  @dataclass(slots=True)
  class ConnectorHealth:
      tripped: bool
      consecutive_failures: int
      consecutive_successes: int
      last_failure_at: datetime | None
      last_failure_error: str | None
      last_success_at: datetime | None
      tripped_at: datetime | None
      probe_attempts: int
      probe_success_streak: int
  ```
- Mixin / base implementation `CircuitBreakerMixin` (or fold into
  `BaseConnector` directly — see Open Question 1):
  - `record_failure(error: str)` — increments counter, resets success
    streak. Trips at `consecutive_failures >= self.failure_threshold`
    (configurable, default 3).
  - `record_success()` — resets failure counter, increments success
    streak. After `consecutive_successes >= self.recovery_threshold`
    (default 1) clears `tripped`.
  - `health() -> ConnectorHealth` — read-only snapshot.
  - `should_attempt_send() -> bool` — returns False when tripped
    *unless* an explicit `force=True` (the bulk-retry path from 0018
    or per-thread `send-draft` from `api/threads.py`).
- Migrate SMSGate's existing `consecutive_failures` counter into the
  base; remove the duplicate. Wire success path
  ([`smsgate.py:249`](../../autosdr/connectors/smsgate.py)) to
  `self.record_success()`; failure paths
  ([`smsgate.py:225,230`](../../autosdr/connectors/smsgate.py)) to
  `self.record_failure(...)`.
- Same wiring on TextBee
  ([`autosdr/connectors/textbee.py`](../../autosdr/connectors/textbee.py))
  — currently has no counter at all. Failure paths get
  `record_failure(...)`; success path gets `record_success()`.
- Override + File connectors get the mixin too, but their
  `failure_threshold = float("inf")` so they never trip — they're for
  rehearsal/dev. Counters still increment so observability is uniform.

### Backend — scheduler honours the breaker

- [`autosdr/scheduler.py`](../../autosdr/scheduler.py) outreach tick
  reads `connector.should_attempt_send()` *before* claiming the next
  batch:
  - Tripped → log line
    `"connector tripped (smsgate); skipping outreach tick. tripped_at=...,
    consecutive_failures=3"`. Don't claim leads. Don't increment quota.
    Sleep until next tick.
  - Healthy → today's behaviour.
- Inbound poller keeps running irrespective of breaker state — replies
  arrive on a different code path (the breaker is on `send`).
- Reply-pipeline send paths (the auto-reply branch +
  `send-draft` + bulk-retry) get the same `should_attempt_send` guard.
  Per-thread `send-draft` remains a `force=True` operator action: an
  explicit human send is allowed even with the breaker tripped (mirrors
  ticket 0008's "human always wins" pattern), but the UI surfaces the
  warning before the send fires.

### Backend — probe loop

- New asyncio task in the FastAPI lifespan
  ([`autosdr/webhook.py`](../../autosdr/webhook.py)):
  `connector_probe_task`. Runs `connector.validate_config()` every
  `probe_interval_s` (default 60) **only while the breaker is tripped**.
  Idle (no probe) when healthy.
- A successful probe nudges `record_success()` directly so the
  recovery threshold counts probe successes alongside real send
  successes (otherwise a healthy probe gets shadowed by a single
  next-tick success).
- Probe failures don't increment the failure counter (already tripped;
  don't double-trip). They do update `probe_attempts` and `probe_success_streak`
  for observability.
- Fully respectful of the killswitch — paused state aborts the probe
  loop too.

### Backend — surface on `/api/status`

- Extend
  [`autosdr/api/status.py`](../../autosdr/api/status.py)
  with a new `connector` block:
  ```json
  {
    "connector": {
      "type": "smsgate",
      "tripped": true,
      "tripped_at": "2026-05-10T08:14:11Z",
      "consecutive_failures": 3,
      "last_failure_error": "network_error: ConnectError",
      "last_failure_at": "2026-05-10T08:13:47Z",
      "last_success_at": "2026-05-10T07:51:02Z",
      "probe_attempts": 47,
      "probe_success_streak": 0,
      "next_probe_in_s": 41
    }
  }
  ```
  Reads from the connector singleton's `health()` — one extra dict
  build per `/api/status` call.

### Backend — settings

- New `connector_circuit_breaker` block on `workspace.settings`:
  ```json
  {
    "connector_circuit_breaker": {
      "enabled": true,
      "failure_threshold": 3,
      "recovery_threshold": 1,
      "probe_interval_s": 60
    }
  }
  ```
- All four are Pydantic-validated; out-of-range values clamp to
  sensible bounds (`failure_threshold ∈ [1, 20]`,
  `recovery_threshold ∈ [1, 10]`, `probe_interval_s ∈ [15, 600]`).
- Hot-reload on settings save (the existing `workspace.settings`
  reader pattern).

### Frontend

- New top-level **breaker banner** above the topbar in
  `AppShell.tsx`. Renders when `status.connector.tripped === true`.
  Mustard-soft on the rust palette, distinct from the killswitch chip:
  > *"SMSGate gateway unreachable since 2 minutes ago — campaigns
  > auto-paused. AutoSDR is testing the connection every 60 s."*
  > `[Test now]` `[Resume anyway]` `[Settings →]`
  - `Test now` — calls a new `POST /api/connector/probe` endpoint
    that runs `validate_config(...)` synchronously and returns the
    result. A success advances the breaker.
  - `Resume anyway` — flips `connector_circuit_breaker.enabled`
    to `false` for the next ~ 5 mins (auto-re-enables, see Open
    Question 4) so the operator who *knows* the gateway is back can
    push past a stale probe.
  - `Settings →` — deep-links into Settings → Connector.
- Settings → Connector card grows a "Health" subsection mirroring
  `/api/status.connector` — error string, last success, probe streak.
- Dashboard hero pill — when tripped, the existing "Quota: 47/100
  today" pill grows a sibling oxblood pill `Connector down`
  ([`frontend/src/routes/Dashboard.tsx`](../../frontend/src/routes/Dashboard.tsx)).
- Web Push (ticket 0005) fires a single push event when the breaker
  trips: *"AutoSDR paused: gateway unreachable"*. New event class
  alongside the existing HITL push; uses the same payload contract.
  Push is single-shot per trip, not per probe.

### CLI

- `autosdr status` (assumes the existing CLI from ticket 0009 follow-ups)
  surfaces the breaker block. Non-zero exit code when tripped (so a
  cron / monitoring wrapper can alert).
- `autosdr connector test` runs a probe and prints the result.
- `autosdr connector resume` clears the breaker manually
  (operator-confirmed, equivalent to *Resume anyway*).

### Tests

- `tests/test_connector_breaker.py` (new):
  - 3 consecutive failures → `tripped == True`.
  - 1 success after a trip → `tripped == False` (with default
    `recovery_threshold=1`).
  - Failure during a probe doesn't double-trip.
  - `should_attempt_send` returns False when tripped, True otherwise.
  - Override + File connectors never trip even on 100 forced
    failures.
- `tests/test_scheduler_breaker.py`:
  - 100-lead simulated campaign with a connector that fails for the
    first 30 calls then recovers — assert ≤ 5 paused-for-HITL
    threads (the 3 trip-trigger + 2 in-flight) and the remaining 95
    leads stay queued.
  - Probe loop runs only while tripped; no probe calls when healthy.
- `tests/test_api_status_connector_health.py`:
  - `/api/status.connector` shape matches the contract.
  - `POST /api/connector/probe` returns synchronous probe result and
    advances the breaker.
- `tests/test_push_breaker_event.py`:
  - Trip event fires exactly one push; subsequent probes don't
    re-fire.

## Out of scope

- **Per-campaign breaker overrides.** All campaigns share one
  connector → one breaker. If the operator wants different campaigns
  to honour different connectors, that's a "connector pool" ticket and
  not a v0 problem.
- **Persisting the breaker state across restarts.** A restart resets
  the counter to 0 on a fresh process. That's deliberate — the breaker
  is a runtime safeguard, not an audit log. The next failure trips it
  again immediately if the gateway's still down. Tracked logs +
  `last_failure_at` survive restarts via `workspace.settings` if and
  when this becomes a real ask.
- **A second-tier slow-down before tripping.** "Throttle to 25% before
  fully tripping" is a sophistication that doesn't help the operator
  fix the underlying problem (offline phone). Default to binary trip /
  recovered.
- **Slack / email alerts on trip.** Web Push covers the "tell the
  operator now" channel. Other channels are out of scope for the
  single-operator self-hosted POC.
- **Auto-trip on inbound failure.** Inbound is poll-based; a poll
  failure doesn't strand outbound work. The poller has its own
  retry; reusing the breaker for inbound complicates the trip
  semantics for no win.

## Success criteria

- Simulated SMSGate outage during a 100-lead campaign produces
  ≤ 5 `connector_send_failed` threads (default `failure_threshold=3`
  + ≤ 2 in-flight before next tick reads the tripped state).
- Scheduler logs `connector tripped … skipping outreach tick` while
  the breaker is tripped. Inbound poll continues.
- `/api/status.connector` surfaces the full health dict.
- Frontend banner renders when tripped; `Test now` advances the
  breaker on real success; `Resume anyway` clears the trip for ≥ 5
  minutes.
- Web Push fires exactly one trip event per trip (no flapping under
  rapid trip/recover cycles within the 5-second debounce window).
- Override + File connectors never trip; smoke covers it.
- All new tests pass; 661+ backend tests still pass; `tsc -b
  --noEmit` clean.

## Effort & risk

- **Size:** M (~ 1 person-week).
- **Touched surfaces:**
  - `autosdr/connectors/base.py` (mixin + dataclass)
  - `autosdr/connectors/{smsgate,textbee,override,file}.py` (wire)
  - `autosdr/scheduler.py` (guard outreach tick)
  - `autosdr/webhook.py` (probe task in lifespan)
  - `autosdr/api/{status,connector,push}.py` (new probe endpoint, status field, push trip event)
  - `autosdr/api/schemas.py` (`ConnectorHealthOut`)
  - Frontend: `AppShell.tsx`, `Settings.tsx`, `Dashboard.tsx`, types, api.
- **Change class:** additive (new fields, new endpoint). No schema
  migration. Settings block is optional; absent-block path means
  default thresholds.
- **Risks:**
  - **False trips on a flaky connection.** A single ConnectError on
    a 99%-up gateway could pile up to threshold over a few hours and
    auto-pause the campaign. Mitigation: counter resets on every
    success, so a ratio of 1 fail per 1000 sends never trips. We're
    only at risk on streaks. Default threshold of 3 chosen to
    survive an isolated transient.
  - **Recovery-threshold edge.** With `recovery_threshold=1`, a
    flapping gateway (alternating success/failure) clears the trip
    after every probe. Mitigation: the probe interval (60 s default)
    rate-limits the flap; the ratio of probe success to next-tick
    failure is the real guard.
  - **Push-event spam.** A flapping breaker could fire one push per
    trip cycle. Mitigation: 5-second debounce on trip-event push;
    no push on recovery (silent OK).
  - **Operator overrides defeat the safety.** *Resume anyway* gives
    a 5-minute window where the breaker is silenced; if the
    operator clicks it without checking the gateway, they get the
    1000-pile-up back. Mitigation: re-arm after 5 minutes; a second
    resume-anyway click is needed to extend; the banner stays
    visible (with a "snoozed" badge) so the operator can't forget.
  - **Connector-singleton coupling.** Today `get_connector()` returns
    a process-global. The breaker state lives on that singleton. If
    a future test fixture builds a fresh connector mid-test, the
    breaker state resets. Mitigation: `health()` snapshot is
    additive, the test fixture path is well-isolated.

## Open questions

1. **Mixin or directly on `BaseConnector`?** Mixin keeps the breaker
   easy to disable per connector (e.g. File connector for tests
   passes a `NullCircuitBreakerMixin`). Direct is simpler. Default
   lean: direct on `BaseConnector` with a `failure_threshold = 0` or
   `disabled` flag for opt-out — same outcome, fewer types.
2. **Default `failure_threshold = 3`?** Three is "enough to absorb a
   blip without false-tripping; small enough to catch a real outage
   fast". Five is more conservative. Default lean: 3 with
   user-configurable in Settings.
3. **Should the operator's per-thread `send-draft` honour the breaker?**
   Two readings: (a) it's a *human* action so the breaker shouldn't
   block it (today's `allow_manual_send()` pattern), or (b) the
   connector is genuinely down and clicking send burns the operator's
   time on a known-failed action. Default lean: (a) — show a warning
   on the send button, but don't block. Council if the operator
   prefers (b).
4. **`Resume anyway` → 5 minutes? 30? Until next trip?** Five minutes
   is short enough that a forgotten override re-engages on its own;
   long enough to send a few rehearsal messages. Default lean: 5
   minutes, configurable in Settings.
5. **Does the breaker apply to the dry-run / override-to connector?**
   No. `OverrideConnector.failure_threshold = inf`. Same for
   `FileConnector`. Tests reflect this.
6. **Is the trip a `connector_tripped` HITL reason on existing
   threads, or a banner-only state?** The 1000 threads from the
   pre-0019 era still carry `connector_send_failed` reason — the
   breaker doesn't retro-classify them. Default lean: banner-only.
   Ticket 0018's bulk-retry handles the existing pile.

## Principle check

- **Simplicity first:** ✓ — connector-level boolean state + a
  scheduler guard is the minimal mechanism.
- **Quality over speed:** ✓ — fewer failed sends + fewer wasted
  drafts + fewer paused threads = cleaner audit trail; doesn't
  compromise message quality.
- **Honest data contracts:** ✓ — promotes connector health from
  "increment-only counter the rest of the system can't see" to a
  first-class field on `/api/status` and `Settings → Connector`.
- **Extensible by design:** ✓ — every connector (SMSGate, TextBee,
  Override, File, future Email connector) wears the breaker
  identically.
- **Human always wins:** ✓ — `Resume anyway` + per-thread
  `send-draft` both let the operator override the breaker.
- **Owner stays in control:** ✓ — banner, push, and Settings card
  all surface the state; thresholds are operator-tunable.

## Links

- Spec: `autosdr-doc1-product-overview.md § 3 (Principles)` —
  *"Owner stays in control"*.
- Architecture:
  - `ARCHITECTURE.md § 3 (Components)` — connector ABC + scheduler.
  - `ARCHITECTURE.md § 12 (Database concurrency rules)` — the
    breaker probe must not hold a session across an `httpx` await.
- Code:
  - `autosdr/connectors/base.py` — where the mixin lives.
  - `autosdr/connectors/smsgate.py:169,225,230,249` — counter to
    migrate.
  - `autosdr/connectors/textbee.py` — needs counter wired in.
  - `autosdr/scheduler.py` — outreach-tick guard.
  - `autosdr/webhook.py` — lifespan probe task.
  - `autosdr/api/status.py` — new connector block.
  - `autosdr/push.py` — new event class.
- Adjacent ticket: [`docs/tickets/0018-retry-connector-failed-from-inbox.md`](0018-retry-connector-failed-from-inbox.md) — the resume affordance.

## Dependencies

- **Blocks:** none (ticket 0018 is *adjacent*, not dependent — it
  provides the resume affordance for the legacy 1000-thread pile-up).
- **Blocked by:** ticket 0005 (PWA + Web Push) — the trip-event push
  re-uses the existing push transport; without 0005 the banner is the
  only operator-notification surface (still acceptable v0; the push is
  a small additive after).
- **Related:** ticket 0009 (paused-inbound replay) — same "queue work
  during a pause; resume drains it" pattern, applied to outbound.
  Ticket 0018 — composes; if both ship, 0018's `Retry all` path also
  benefits from the breaker (re-tripping mid-bulk halts the sweep
  fast).
