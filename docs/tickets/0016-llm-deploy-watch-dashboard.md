# [feature/ui+api] In-app LLM "deploy watch" surface (slice metrics + golden replay)

<!-- TYPE: feature -->
<!-- AREA: ui + api -->

## Problem

The 2026-05-02 prompt audit (`docs/prompt-audit-2026-05-02.md`) shipped
two read-only diagnostic CLIs that fundamentally changed how we
reason about the AI loop:

- **`scripts/llm_call_metrics.py`** — slice metrics by
  `prompt_version`: calls, errors, token averages, eval pass-rate,
  attempts-per-send, $/sent-thread, p10/p50/p90 of `overall` score.
  Read-only. Supports `--since 2026-05-02 --purpose evaluation
  --json`.
- **`scripts/replay_evaluator.py`** — golden-replay harness. Picks a
  diverse historical sample (mixes pass/fail × `angle_type`),
  reconstructs evaluator inputs from the DB, re-runs the *current*
  `evaluation.PROMPT_VERSION` against the same draft on a live LLM
  call, and prints OLD vs NEW score+pass+feedback side-by-side.

Together they're the regression harness this project lacked. They
already proved their value: under the audit's Phase 1, replaying 8
threads against `evaluation-v4.4` showed `pass_flips: 0/8`, `tokens_in
-92%`, `latency -61%`, validating the bug fix that recovered ~$8 of
otherwise-wasted spend. Phase 4's `evaluation-v4.7` (JSON-schema
response_format) replay showed the same: zero pass flips, ~108 avg
tokens_out, no self-heal retries.

The problem is operational, not technical:

1. **The CLI is a regression harness only the author runs.** The
   audit's recommended next step (Phase 2#6: *"watch Phase 1 deploy
   for 48h. Specifically: did attempts-per-send go up? Did HITL rate
   go up? Did $/thread go down (it should)?"*) requires the operator
   to remember to run a Python CLI on the server. The operator —
   the same Time-Poor Founder who's about to start running AutoSDR
   from a phone (per refined 0005 + 0015) — is not going to SSH in
   and run a metrics script.
2. **The cost data is paid-for and sitting unused.** The audit
   showed evaluation alone was wasting ~50% of total LLM spend on a
   foot-gun bug. That sort of regression now has machinery to
   detect, but only if someone's looking. The Dashboard's existing
   "estimated cost today" pill (from ticket 0006) is a single
   number; it doesn't surface per-prompt-version cuts, p50/p90 of
   `overall`, or attempts-per-send — exactly the dimensions the
   audit flagged as load-bearing.
3. **The `replay_evaluator` harness is the prompt-shrink unblocker.**
   Phase 3 (Phase 3 #7: dedup eval against generation; #8: tone
   block budget; #10: move franchise list into code) is *gated on
   "the v4.4-v4.7 deploy is stable for 1-2 weeks"*. Today, "stable"
   is a vibe. Tomorrow it could be a CTA in the Logs route that
   says: *"v4.7 is 8 days old; pass-rate 87%, attempts/send 1.2,
   $/thread $0.03 — 12% lower than v4.6's $0.034. Safe to ship the
   next prompt change."*

## Hypothesis

If we surface (a) per-`prompt_version` slice cuts in the operator
console, (b) a "deploy health" callout that reads the slice cuts and
flags regressions, and (c) a one-click "run golden replay against
the most recent N threads" button on the Logs route, then:

- The operator notices a regression in attempts-per-send or
  $/thread within hours instead of weeks.
- Phase 3 prompt shrink work becomes "the dashboard says v4.7 is
  stable; ship the next change" instead of "I forgot to run the
  CLI".
- The operator can answer *"is the AI loop healthy this week?"* from
  the phone without opening a terminal — directly composing with the
  mobile-responsive ticket 0015.

Measured by:

- The `/Logs` route exposes a "By prompt version" panel that mirrors
  `scripts/llm_call_metrics.py --purpose <X>` output. One row per
  active version, last-30-days window default; older rows in a
  "history" disclosure.
- A new `/api/llm/health` endpoint returns the structured payload
  the panel renders; the existing CLI is rewritten to consume the
  same handler so CLI/HTTP can't drift.
- A "Run golden replay" button on the Logs route fires
  `replay_evaluator.py`'s logic via a backend endpoint; results
  surface in-place (no terminal needed).

## Scope

### Part A — `/api/llm/health` endpoint (read-only)

```http
GET /api/llm/health?since_days=30&purpose=evaluation|generation|analysis|classification|all
```

Returns:

```jsonc
{
  "since": "2026-04-02T00:00:00Z",
  "now": "2026-05-02T07:00:00Z",
  "purpose_filter": "evaluation",
  "by_prompt_version": [
    {
      "prompt_version": "evaluation-v4.7",
      "purpose": "evaluation",
      "first_seen_at": "2026-05-02T03:00:00Z",
      "calls": 18,
      "errors": 0,
      "tokens_in_avg": 5394,
      "tokens_out_avg": 108,
      "latency_ms_avg": 1233,
      "cost_usd_total": 0.18,
      "evaluator_pass_rate": 0.875,
      "overall_p10": 0.86,
      "overall_p50": 0.92,
      "overall_p90": 0.97,
      "attempts_per_send_avg": 1.2,
      "hitl_rate": 0.05,
      "cost_per_sent_thread_usd": 0.031
    },
    { "prompt_version": "evaluation-v4.6", ... },
    { "prompt_version": "evaluation-v4.4", ... }
  ],
  "summary": {
    "active_version": "evaluation-v4.7",
    "active_version_age_hours": 4,
    "active_vs_previous": {
      "tokens_in_delta_pct": -92.4,
      "latency_ms_delta_pct": -61.0,
      "cost_per_sent_thread_delta_pct": -91.3,
      "pass_rate_delta": 0.005
    },
    "health_flags": [
      { "kind": "ok", "message": "tokens_in down 92%, pass-rate stable" }
    ]
  }
}
```

The `health_flags` taxonomy is closed:

- `"ok"` — green
- `"watch"` — yellow (e.g. "active version is < 24 hours old; sample
  size N=4")
- `"alert"` — red (e.g. "attempts_per_send up 40% vs previous
  prompt_version" — the audit's Critic-flagged loop-multiplication
  failure mode)

The handler is shared with `scripts/llm_call_metrics.py` — the CLI
becomes a thin caller of the same function in
`autosdr/api/llm_health.py`.

### Part B — `/Logs` "By prompt version" panel

New panel above the existing per-call list:

- Tab strip: *Evaluation / Generation / Analysis / Classification /
  All*. Default *Evaluation* (highest spend; the audit's primary
  surface).
- One row per active prompt_version (active = had ≥ 1 call in the
  window). Columns: version, age, calls, pass-rate (eval-only), avg
  tokens_in, avg latency, $/call, $/sent-thread.
- Health flag badge per row (green/yellow/red).
- "Compare to previous version" toggle — when on, each metric column
  shows ± delta vs the previous version row.
- Older rows (no calls in the window) collapsed into a "history" `<details>`.

Mobile (per ticket 0015): table → card list. One card per version.
Health flag is the card title-line accent colour.

### Part C — Dashboard "deploy health" callout

A small additive card on the Dashboard (above the existing "today's
LLM activity" stat).

- Renders the `summary.health_flags` from `/api/llm/health` for the
  *evaluation* purpose by default (since eval is the gate on every
  send).
- Green: collapsed one-liner. Yellow / red: expanded card with the
  flag message + a link to `/Logs?purpose=evaluation`.
- The operator gets *"AI loop healthy"* at-a-glance, or *"Eval
  attempts/send up 40% — investigate"* with one tap.

### Part D — Golden-replay endpoint + button

New `POST /api/llm/replay` (read-only intent — no DB writes by default):

```jsonc
{
  "purpose": "evaluation",
  "n_threads": 8,
  "stratify_by": "angle_type",       // also: "pass_flag", "campaign_id"
  "apply": false                     // when true, persist the new llm_call rows like the CLI's --apply flag
}
```

Returns a structured `ReplayResult` with the same shape as
`replay_evaluator.py`'s output (per-thread OLD vs NEW score+pass+
feedback, plus a `pass_flips` summary, plus token/cost deltas).

Frontend:

- "Run golden replay" button on `/Logs` (gated behind a confirm
  dialog when `apply=true` because it costs real $).
- Results render below the panel; a flat table (mobile: card list)
  with a row per thread, expand-to-see-feedback-text disclosure.
- Default `n_threads=8`, default `apply=false` (dry run = costs ~8 ×
  $0.001 ≈ $0.01 to run; affordable to spam-click during a deploy).

### Part E — Wire scripts/llm_call_metrics.py to the new handler

The audit's CLI becomes a thin shell over `autosdr/api/llm_health.py`.
Identical output today; one source of truth tomorrow. Same for
`scripts/replay_evaluator.py` against `/api/llm/replay`.

This is the same pattern used by ticket 0003 (per-campaign funnel
endpoint shared by CLI + frontend) and ticket 0006 (Gemini presets
exposed as both `/api/llm/presets` and consumed by the CLI). It's
not new architecture — just consistency.

### Out of scope

- **Spend caps / alerts.** The audit-deferred follow-up "spend caps
  and alerts when threshold crossed" — adjacent but separate. This
  ticket gives you the read; alerts are a follow-up.
- **Per-thread or per-lead drill-down from the panel.** The existing
  `/Logs?prompt_version=…` filter handles ad-hoc drill-down. v2 if
  asked-for.
- **Push notification on a "red" health flag.** Composes with 0005
  (PWA + Push) trivially once both are shipped — file a follow-up.
- **The actual Phase 3 prompt-shrink work.** This ticket *unblocks*
  Phase 3 by giving us the deploy-watch surface; the shrink itself
  is its own ticket sequence.

## Success criteria

- New `tests/test_llm_health_api.py` covers the four health
  scenarios (active < 24h → "watch", attempts/send up >20% →
  "alert", tokens_in down + pass-rate stable → "ok", no calls in
  window → empty payload).
- New `tests/test_llm_health_aggregation.py` pins the SQL aggregation
  per `prompt_version` against a synthetic dataset; covers the
  `attempts_per_send_avg` join (which threads the per-thread
  `tokens_in` against `Message.role='ai'` outbound count).
- New `tests/test_llm_replay_api.py` covers `/api/llm/replay` with
  `apply=false` (no `llm_call` rows persist) and `apply=true` (rows
  persist with the active prompt_version).
- The CLI `scripts/llm_call_metrics.py --since <D>` produces
  byte-identical output before and after the rewrite (golden file
  test).
- The Dashboard health callout renders for the same data set the
  audit was watching (`evaluation-v4.4` → `v4.7`) and shows ✓ green.
- The `/Logs` "By prompt version" panel renders, collapses on mobile
  per ticket 0015's CardList pattern, and the "compare to previous
  version" delta column is present.
- Frontend `tsc -b --noEmit` clean; backend tests green.

## Effort & risk

- **Size:** M (4-6 days). Most of the work is SQL aggregations and
  one wide JSON shape; the panels are CardList-fed lists; the
  replay endpoint is a thin async wrapper around the existing CLI's
  logic.
- **Touched surfaces:**
  - `autosdr/api/llm_health.py` (new) — the shared handler.
  - `autosdr/api/__init__.py` — register router.
  - `autosdr/api/schemas.py` — `LlmHealthOut`, `LlmReplayResult`.
  - `scripts/llm_call_metrics.py` — port to call the handler.
  - `scripts/replay_evaluator.py` — port to call the handler.
  - `frontend/src/routes/Logs.tsx` — new panel + replay UI.
  - `frontend/src/routes/Dashboard.tsx` — health callout.
  - `frontend/src/lib/types.ts` — TS types mirroring the schemas.
  - `tests/test_llm_health_*.py` (new), `tests/test_llm_replay_api.py`
    (new).
- **Change class:** additive (new endpoint, new UI panels). No
  schema changes. The CLI port is the only "rewrite an existing
  thing" risk; mitigated by the golden-file test.
- **Risks:**
  - **Aggregation latency.** Computing `attempts_per_send_avg`
    requires joining `llm_call` (millions of rows over time) to
    `Message` and `Thread`. Mitigate with a 30-day default window
    and an index on `(prompt_version, created_at)` if perf bites
    (defer the index — measure first).
  - **`apply=true` on `/api/llm/replay` costs real $.** Confirm
    dialog + a soft monthly cap (Open question; not in v0).
  - **Health flag thresholds are guesses today.** The audit gave us
    the *failure shapes* (loop multiplication on shrink, cost going
    up despite tokens going down) but not the exact percentage at
    which "watch" flips to "alert". Pick conservative defaults
    (>20% regression on attempts/send → alert; >10% → watch) and
    document so the operator can override.

## Open questions

- **OQ1.** Single `/api/llm/health` endpoint with a `purpose` filter,
  or one endpoint per purpose? Recommend single endpoint — keeps
  the CLI/HTTP surface small. Decision.
- **OQ2.** Should the Dashboard health callout default to *all*
  purposes or just *evaluation*? Recommend evaluation by default
  (it's the send gate; if eval is sick, sends are sick). Decision.
- **OQ3.** Persist health flags to a `llm_health_snapshot` table
  for history, or compute on read every time? Recommend compute on
  read (we have the data; same call as the audit CLI). Persist
  later if the dashboard ever needs to show "flag history". Decision.
- **OQ4.** Replay-on-resume: should the deploy-watch surface
  *automatically* run the golden replay when a new `prompt_version`
  is detected (i.e. version bump on push)? Recommend manual button
  for v0; auto-run is one bug from spending real $ in a loop.
  Decision.
- **OQ5.** Threshold-driven alerts (Critic's flag from the audit
  council): when an active version's `attempts_per_send_avg` is
  >20% above the previous, fire what? Today: a red badge on the
  Dashboard. Future (composes with 0005): a Web Push. Recommend
  badge-only for v0. Decision.

Resolve via council mini-round before implementation per the
ticket-implementer workflow. OQ4 is the most consequential (spend
risk).

## Principle check

- **Simplicity first.** ✓ — additive endpoint + two UI panels, no
  schema change.
- **Quality over speed.** ✓ — we're surfacing the regression-detection
  machinery the audit just built.
- **Honest data contracts.** ✓ — `LlmHealthOut` is a strict Pydantic
  schema with closed `health_flags` vocabulary; CLI and HTTP share
  the handler so they can't drift.
- **Extensible by design.** ✓ — `purpose` filter is the seam;
  adding new purposes (followup, classification subtypes) is one
  enum value.
- **Human always wins.** ✓ — the operator gets earlier visibility
  into a regression they'd otherwise notice via missed sends.
- **Owner stays in control.** ✓ — `apply=true` on the replay is
  gated; thresholds are documented; nothing fires automatically in
  v0.

## Links

- Audit doc: `docs/prompt-audit-2026-05-02.md` (Phase 2 #4, #5;
  this ticket is the in-app surface for those CLIs).
- CLI sources: `scripts/llm_call_metrics.py`,
  `scripts/replay_evaluator.py`,
  `scripts/replay_classifier_smoke.py`,
  `scripts/replay_outreach_loop.py`.
- Existing per-call surface: `frontend/src/routes/Logs.tsx`.
- Existing cost pill: `frontend/src/routes/Dashboard.tsx`
  (ticket 0006).
- Existing CLI/HTTP shared-handler precedent: ticket 0003 (campaign
  timeseries), ticket 0006 (`/api/llm/presets`).

## Dependencies

- **Blocks:** Phase 3 prompt-shrink work (audit doc § 7 Phase 3
  items #7, #8, #10). The deploy-watch surface is the gate on
  shipping the next prompt change confidently.
- **Blocked by:** nothing. Composes with 0015 (mobile responsive)
  for the on-the-go viewing case but is independently shippable.
- **Related:** 0005 (PWA + Push) — a "red flag" health alert is the
  natural second push event after HITL. File as follow-up once both
  ship.
