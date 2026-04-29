# [hardening/reply-pipeline] Don't hold the SQLite write transaction across LLM API awaits

<!-- TYPE: hardening -->
<!-- AREA: pipeline / db -->
<!-- SEVERITY: prod-push blocker -->

## Problem

`autosdr/pipeline/reply.py:184` opens one `session_scope()` block
that wraps the *entire* `process_incoming_message` body, including
every `await _classify_reply(...)` and any subsequent generation
calls (`_park_with_suggestions` /
`_run_auto_reply`):

```python
with session_scope() as session:               # line 184
    workspace = session.get(Workspace, workspace_id)
    ...
    cls = await _classify_reply(...)           # ~5–15s of remote LLM
    ...
    return await _park_with_suggestions(...)   # 2–3 more LLM calls
```

That `session_scope()` is a write transaction (it ultimately
inserts the inbound `Message` row, may update the `Thread`, etc.)
and SQLite — even with WAL — serialises writers. The transaction
stays open for the *whole* duration of the LLM API round-trips
(10–60s end-to-end on a flaky network).

While that single transaction is open, three things go wrong, in
order of severity:

### 1. The async event loop is blocked, not awaiting

The LLM client's audit logger (`autosdr/llm/client.py:327`,
function `_log_call`) opens a *separate* SQLAlchemy session to
insert one `llm_call` row per call:

```python
session.flush()    # <-- synchronous SQLite INSERT
```

This is invoked from inside the async pipeline — *not* via
`loop.run_in_executor`. Because the parent transaction is still
open in the same process, this writer hits SQLite's "busy"
state and waits. With `busy_timeout=120000`
(`autosdr/db.py:68`) the synchronous `cursor.execute` blocks for
the *full two minutes* before raising `OperationalError`. The
asyncio event loop is dead during this window — no other request
can be served.

Symptom verified during the 2026-04-27 evening rehearsal:
`GET /api/status` timed out for 45+ seconds while a single
inbound was "processing". The dev-server log showed only
`POST /api/webhooks/sim 202 Accepted` and nothing else for the
duration — no log lines, no progress. Three ESTABLISHED TCP
connections to Google's IP range confirmed the LLM API calls
were in flight; the worker was alive but the event loop was
starved.

### 2. WAL never checkpoints; disk usage explodes

Long-held read transactions (e.g. the operator polling
`/api/threads/<id>/messages` from the UI every 3–10 s while the
inbound is processing) prevent SQLite from checkpointing the WAL.
Combined with the writer contention above, every retry adds
WAL frames without ever truncating them.

Measured on the 2026-04-27 rehearsal: `data/autosdr.db-wal`
ballooned to **365 MB** in one session (DB itself is 377 MB).
Recovered cleanly only after killing the dev server and running
`PRAGMA wal_checkpoint(TRUNCATE)` manually.

### 3. Persistence failures cascade as silent classification loss

The classification *result* is correct (we confirmed
`{"intent": "question", "requires_human": true, "confidence":
0.95}` for a real reply). But the `_log_call` insert fails with
`OperationalError: database is locked`, and the LLM client
swallows this as a "non-fatal log failure" (it *is* non-fatal for
the in-memory pipeline, but the audit row is gone). Any
downstream consumer that joins on `llm_call` (the `/Logs` page,
the cost-tracking endpoint from ticket 0006, the per-angle stats
from ticket 0002) silently loses rows.

## Hypothesis

If we (a) commit and close the parent transaction *before* every
`await` to a remote service, and (b) move the audit-log write off
the request's event-loop thread, then:

- Single-inbound processing no longer blocks the event loop.
- `_log_call` writes don't compete with the parent transaction
  for the SQLite writer lock.
- WAL has the headroom to checkpoint between writes.

Measured by:

- `tests/test_reply_pipeline_concurrency.py::test_status_endpoint_responsive_during_inbound_processing`
  — fires a `POST /api/webhooks/sim`, then in the same event
  loop polls `GET /api/status` every 100 ms for 60 s. Asserts
  every poll returns `200` within 500 ms. Currently fails (the
  status endpoint times out for 30–60 s mid-inbound).
- `tests/test_reply_pipeline_concurrency.py::test_concurrent_inbounds_do_not_lock`
  — fires three inbound sims in parallel (different leads,
  same campaign). Asserts all three complete with the expected
  thread state and no `database is locked` errors in the dev-
  server log.
- After a full rehearsal session that processes ≥ 5 inbounds,
  `data/autosdr.db-wal` is < 32 MB (one auto-checkpoint
  threshold) at the end.

## Scope

### Part A — Split `process_incoming_message` into "read snapshot → await → write outcome"

The current function does
`read → write inbound message → await classify → write thread state`
in one transaction. Restructure to:

1. **Read phase** (own transaction): load workspace, settings,
   resolve lead/thread, persist the inbound `Message` row,
   commit. This is the *durability* boundary: once we return
   202 to the webhook, the inbound is on disk regardless of
   what happens next. Today this is the case only because the
   transaction usually commits at the end; under contention it
   doesn't, and a server crash mid-LLM-call loses the inbound.
2. **Decision phase** (no DB session): `await _classify_reply`,
   `await _generate_suggestions`. Pure async work over the
   network. The session is closed; the writer lock is free.
3. **Write phase** (own transaction): apply the classification
   to the thread (status / paused_reason / suggested_replies),
   commit. Short — milliseconds.

Rules-of-thumb that fall out of this restructure (write down in
the function docstring and in `ARCHITECTURE.md`):

- A `with session_scope()` block must not contain `await`. Add a
  `tests/test_no_await_in_session.py` AST-level lint test that
  walks the codebase and fails if any `with session_scope()`
  has an `Await` node inside its body. Cheap, prevents
  regression.
- A pipeline function that needs to "remember" a row across
  awaits should hold the *id*, not the ORM object. The write
  phase re-loads by id. (Already the pattern in
  `autosdr/pipeline/outreach.py` for `claim_lead`; pipeline/reply
  hasn't been ported.)

### Part B — Move `_log_call` writes off the event loop

Two cheaper options before considering a worker queue:

- **B1 (preferred):** wrap the `_log_call` body in
  `loop.run_in_executor(None, _log_call_sync, payload)` and
  fire-and-forget. The audit row lands on the default thread
  pool; the request thread doesn't wait. If `_log_call` ever
  raises, log the error but don't fail the parent. Tests assert
  the audit row appears within 1 s of the LLM call returning
  (use `await asyncio.sleep(1)` + a poll loop in tests, no flaky
  unbounded waits).
- **B2:** keep `_log_call` synchronous but tighten its session:
  `BEGIN IMMEDIATE` + commit in ≤ 100 ms. With the parent
  session restructured per Part A, the writer lock is
  available, and the call returns quickly. This is the
  minimal change but still puts a sync DB call in an async
  context — defensible for one-row inserts only.

Recommend implementing B1; B2 is the rollback path if the
executor approach causes test-flake.

### Part C — Tighten `busy_timeout`

`busy_timeout=120000` (2 minutes) is too long for a request
context — it converts a transient lock into a request-killer.
Drop to `5000` (5 s) for the API workers and keep the comment
explaining why. The audit-log retry path (Part B) handles the
"actually busy" case via the executor pool, where a 5 s wait
is fine because the request thread isn't blocked.

### Out of scope

- A real worker queue (Celery / RQ / NATS). The volume isn't
  there; the executor pool is enough and lets the rest of the
  app stay sync-SQLite.
- Switching to Postgres. Single-operator volume doesn't
  justify the migration cost; this fix preserves SQLite.
- Persisting the inbound *before* the connector's
  `parse_webhook` runs. That's a separate durability question
  (see ticket 0009 for the killswitch case).

## Success criteria

- `process_incoming_message` no longer contains `await` inside a
  `session_scope()` block. AST lint test passes.
- `_log_call` writes are issued via the loop's default executor;
  unit test asserts no event-loop blocking on a 250 ms sleep
  injected into `_log_call`.
- `tests/test_reply_pipeline_concurrency.py` (new) passes, both
  the responsive-status and concurrent-inbounds cases.
- During a manual rehearsal that processes 5 inbounds back-to-
  back, `data/autosdr.db-wal` stays under 32 MB.
- `ARCHITECTURE.md` has a "Concurrency rules" subsection that
  states the no-await-in-session-scope invariant.
- Backend test suite green.

## Effort & risk

- **Size:** M (½–1 day). Part A is the only design work
  (deciding the read/decide/write seam); B and C are
  mechanical.
- **Touched surfaces:**
  `autosdr/pipeline/reply.py` (restructure),
  `autosdr/llm/client.py` (`_log_call` executor wrap),
  `autosdr/db.py` (busy_timeout),
  `tests/test_reply_pipeline_concurrency.py` (new),
  `tests/test_no_await_in_session.py` (new),
  `ARCHITECTURE.md` (concurrency rules note).
- **Risk:** Part A's restructure has to keep the existing
  reply-pipeline behaviour exact-equivalent — the existing
  `tests/test_outreach_pipeline.py` and any reply tests are
  the regression net. The fire-and-forget audit log (B1) loses
  errors; mitigation is `logger.exception` inside the executor
  wrapper + a metric for "audit-log queue depth" (tracked as a
  follow-up, not in scope here).
- **Reversibility:** Each part is independently revertable.

## Open questions

- **OQ1.** Part A: should the inbound-`Message`-row write happen
  in the read phase (commit before any LLM call) or stay in the
  write phase (commit after classification)? Trade-off:
  durability across server crashes vs. having to reconcile a
  recorded-but-unclassified message on restart. Default
  position: commit in the read phase + tag the row with a
  `pending_classification=True` boolean we don't have today.
  Reconciliation is "on startup, find pending rows and re-
  classify"; this is also exactly what ticket 0009's killswitch-
  replay path needs. **Recommend deciding alongside 0009.**
- **OQ2.** Part B: do we need a bounded executor (its own
  `ThreadPoolExecutor(max_workers=4)`) or is the default
  `loop.run_in_executor(None, ...)` fine? Default is unbounded-
  ish (defaults to `min(32, os.cpu_count() + 4)`); for this app
  that's plenty. Stick with the default unless we ever see audit
  rows backed up.
- **OQ3.** Part C: are there callers other than the API request
  path that depend on the 2-minute timeout? Migrations / one-
  shot CLI imports might. Audit those callers; if any need a
  longer timeout, set it per-session via
  `connection.execute("PRAGMA busy_timeout=…")` rather than the
  engine default.

Resolve OQ1–OQ3 via a council mini-round before implementation
per the ticket-implementer workflow.

## Principle check

- **Owner stays in control.** The fix removes a class of
  pathological "the dashboard froze for 2 minutes" failures
  that the operator can't reason about. ✓
- **Honest contracts.** `_log_call` no longer silently drops
  audit rows under contention. ✓
- **Human always wins.** Inbound durability improves: the
  message row is on disk before the LLM call. ✓
- **AI loop is the moat.** No AI-surface changes. ✓
- **Cheap before grand.** Explicitly defers a worker-queue
  migration. The smallest-fix-that-closes-the-bug is exactly
  the executor wrap + transaction split. ✓

## Decisions log

(Empty — populated during implementation.)

## Reference: failure trace from 2026-04-27 evening rehearsal

```
[api] INFO: 127.0.0.1:63993 - "POST /api/webhooks/sim HTTP/1.1" 202 Accepted
[api] failed to persist LLM call to database
[api] sqlalchemy.exc.OperationalError: (sqlite3.OperationalError) database is locked
[api] [SQL: INSERT INTO llm_call (id, created_at, workspace_id, campaign_id,
       thread_id, lead_id, purpose, model, prompt_version, ...)]
[api] [parameters: ('83338589-…', '2026-04-26 22:59:40', …,
       'classification', 'gemini/gemini-3.1-flash-lite-preview',
       'classification-v1', …,
       '{"intent": "question", "requires_human": true, "confidence": 0.95,
         "reason": "The lead expressed interest but is asking for clarification…"}',
       'null', 443, 64, 1040, None)]
```

The classification ran (`response_parsed` is correct); the
`INSERT` lost a 2-minute fight with the parent transaction and
left no audit row. The dev server's event loop was unresponsive
for the same duration. This ticket exists to make that pattern
structurally impossible.
