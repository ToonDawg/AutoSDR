# [hardening/connectors+cli] Override safety + connector E.164 guard + `autosdr e2e` rehearsal CLI

<!-- TYPE: hardening -->
<!-- AREA: connectors / cli -->

## Problem

This ticket batches three follow-ups uncovered during the prod-push
rehearsal on 2026-04-27 (parent: chat
`prod-hardening-and-e2e_6849b752`). All three exist because the
"single-lead rehearsal" mode and the prod outreach path share state
they shouldn't, and there's no operator-facing rehearsal runbook other
than a chat thread.

### 1. Override single-slot mapping is fragile

[autosdr/connectors/override.py:49](../../autosdr/connectors/override.py)
keeps `_last_original` as a single in-process variable. It's
overwritten by every successful `send()` and reset on `--reload` /
process restart. Two specific failure modes:

- **Cross-talk under concurrent sends.** If `rehearsal.override_to`
  is on AND any non-rehearsal campaign also sends in the same
  process lifetime, `_last_original` is whatever the *last* send
  globally targeted. An inbound from the override number is then
  rewritten to that lead — possibly a real customer's thread — not
  the lead the rehearsal was about. The reply pipeline does
  cross-check the sender against `Lead.contact_uri`
  ([autosdr/pipeline/reply.py](../../autosdr/pipeline/reply.py)) and
  in the override-on case the resolved lead is *the override
  recipient itself*, so the routing decision falls to whichever
  `Thread` the resolver picks via "most recent active thread for
  this lead" — which won't necessarily be the rehearsal thread. The
  embarrassing case: an LLM-classified reply lands on a real
  customer's thread, with HITL suggestions composed against the
  wrong context.
- **No-op short-circuit when `lead.contact_uri == override_to`.**
  Lines 63-66 return early without setting `_last_original`. So a
  test where the lead's number IS the override number leaves stale
  `_last_original` from a previous *different-lead* send in place,
  and an inbound from the override number gets rewritten to the
  wrong original.

Mitigation today: only enable override when no other campaign is
ACTIVE. There is no UI guardrail that enforces this, no log warning,
no automated check. The docstring (lines 14-16) acknowledges
"intentionally a single-slot mapping — override mode is a one-lead
rehearsal" but the runtime doesn't actually enforce that intent.

### 2. No E.164 guarantee at the connector boundary

[autosdr/importer.py:113](../../autosdr/importer.py) normalises
phones to E.164 via `phonenumbers.parse(..., region_hint).format_number(E164)`.
But `BaseConnector.send` ([autosdr/connectors/base.py](../../autosdr/connectors/base.py))
trusts `OutgoingMessage.contact_uri` verbatim. If a future code path
writes a non-E.164 string to `Lead.contact_uri` (a manual DB edit, a
new lead-creation API that skips the importer, a connector swap that
exposes a different format), sends fail silently at the SMS provider
or — worse — succeed against the wrong number. Today this is latent
because everything in production goes through `import_file`, but it's
defensible-in-depth that's missing.

### 3. No commandable rehearsal flow

The 2026-04-27 prod-push rehearsal was driven by ~12 manual UI clicks
+ curl calls. The exact sequence is captured in plan
`prod-hardening-and-e2e_6849b752` (and in the runbook section of this
ticket). On every prod push the operator has to either redo the
sequence by hand, or trust their memory. The follow-up beat
([autosdr/pipeline/followup.py](../../autosdr/pipeline/followup.py))
defaults to disabled per-campaign, but the rehearsal needs an
explicit `enabled=False` in the create payload — easy to forget. The
killswitch + active-campaign-pause needs to be reverted in the right
order during teardown to avoid scheduler thrash. Both are
deterministic; both should be one command.

## Hypothesis

If we (a) make `OverrideConnector` thread-id-keyed so cross-talk is
impossible, (b) add an E.164 assertion on `BaseConnector.send`, and
(c) ship `autosdr e2e <setup|kickoff|teardown>` that drives the
prod-push rehearsal idempotently, then:

- The single embarrassing failure mode (rehearsal reply lands on a
  real customer's thread) becomes structurally impossible, not a
  documentation note.
- A future code path writing a malformed `contact_uri` produces a
  loud test/runtime failure instead of a silent provider reject.
- Pre-push rehearsal is one command; teardown is one command. The
  rehearsal becomes part of the release checklist, not a tribal
  artefact.

Measured by:

- New `tests/test_override_connector.py` cases asserting that two
  interleaved sends to two different leads produce a reply remap
  that matches the *intended* lead (currently impossible to test
  without the fix — the test would assert the bug today).
- New `tests/test_base_connector.py` (or extension to existing
  connector tests) asserting `send()` raises on a non-E.164
  `contact_uri`.
- `autosdr e2e setup --phone +61414603957` exits 0 and leaves the DB
  in the same state every time, regardless of prior runs.
- `autosdr e2e teardown` reverts every change `setup` made (lead
  DNC'd or removed, rehearsal campaign COMPLETED, prior ACTIVE
  campaigns re-ACTIVE'd, killswitch off if it was off pre-rehearsal).

## Scope

### Part A — `OverrideConnector` per-thread mapping

- Replace `_last_original: str | None` with
  `_pending_by_provider_id: dict[str, str]` keyed on
  `provider_message_id` from the inner connector's `SendResult`.
  When the override sends successfully, record
  `pending[result.provider_message_id] = original_contact_uri`.
- `_maybe_rewrite` looks up the inbound message's
  `in_reply_to_provider_id` (today's `IncomingMessage` has no such
  field — see scope item below) and rewrites the sender from the
  mapping if found. Falls back to logging a warning + leaving the
  message unchanged so the reply pipeline's lead-by-contact-uri
  resolution still gets a chance.
- Cap the mapping at N=64 entries (LRU). Override mode is a
  one-lead-at-a-time rehearsal; 64 is two orders of magnitude more
  headroom than any real rehearsal needs and bounds memory.
- **Inner-connector dependency:** SMSGate webhooks include the
  delivery-receipt's original message id; TextBee polls don't carry
  one today. Two options here, see Open questions.
- Remove the lines 63-66 short-circuit. If the test lead's number
  equals the override number, the send is *already* targeting the
  right place — but we still record the mapping (with original ==
  override) so inbound rewrite is a no-op rather than a stale
  rewrite.
- Update [autosdr/connectors/override.py](../../autosdr/connectors/override.py)
  docstring to reflect the new semantics ("up to N concurrent
  rehearsal leads, keyed on provider_message_id, falls through to
  lead-by-contact-uri when the inbound provider_id is unknown").

### Part B — E.164 assertion at the connector boundary

- Add `_E164_RE = re.compile(r"^\+[1-9]\d{6,14}$")` to
  [autosdr/connectors/base.py](../../autosdr/connectors/base.py).
- New `BaseConnector._validate_contact_uri(uri: str) -> None` that
  raises `ValueError("contact_uri must be E.164: got %r" % uri)` on
  miss. Called from each implementation's `send()` first line.
- Same guard in `OverrideConnector._maybe_rewrite` so a remap to a
  malformed `_pending` value can't poison downstream.
- Tests: parametrised cases over the existing TextBee, SmsGate,
  File, Override connectors that a non-E.164 send raises before the
  inner provider call.

### Part C — `autosdr e2e` CLI

New top-level `autosdr e2e` subcommand group in
[autosdr/cli.py](../../autosdr/cli.py) with three commands:

- `autosdr e2e setup --phone <e164> [--name <str>]` — idempotent:
  - Touches the killswitch flag (preserves it if already on).
  - Records every currently-ACTIVE campaign id in
    `data/rehearsal/state.json` and pauses each via
    `_set_status(..., PAUSED)`.
  - Clears `rehearsal.override_to` (records prior value in the same
    state file).
  - Imports a one-row CSV (auto-generated under
    `data/rehearsal/single_lead.csv`, gitignored — `data/` already
    is) for the given phone. Records the resulting lead id.
  - Creates a campaign "Prod rehearsal — manual e2e" in DRAFT with
    `followup={enabled: False, template: ""}` and assigns the
    rehearsal lead. Records the campaign id.
  - Prints a summary table: campaign id, lead id, thread id (none
    yet), what to do next.
  - Idempotency: if a state file already exists with live ids,
    re-use them; if any id is dangling (campaign deleted, lead
    DNC'd from a previous run), recreate the missing piece and
    update the state file.
- `autosdr e2e kickoff [--count 1]` — calls
  `run_campaign_outreach_batch(..., respect_quota=False)` against
  the rehearsal campaign in `state.json`. Prints
  `provider_message_id` + the AI message body so the operator can
  cross-check what landed on their phone vs. what the DB recorded.
- `autosdr e2e teardown [--keep-lead]` — reverses everything:
  campaign → COMPLETED, prior ACTIVE campaigns → ACTIVE,
  killswitch state restored, override restored. Lead is DNC'd
  unless `--keep-lead`. State file deleted on success.

The CLI uses the same `db_session` + API helper functions the
HTTP routes use (`_set_status`, `_to_out`, `import_file`,
`run_campaign_outreach_batch`) — no DB writes that bypass the
existing invariants.

### Out of scope

- Multi-lead rehearsal mode (sending to two different rehearsal
  phones concurrently). The override mapping change supports it but
  the CLI doesn't expose it; one phone per push covers every
  rehearsal use we've had.
- A UI shortcut for the rehearsal flow. The CLI is the right
  surface — the operator running the rehearsal is also the one
  pushing to prod, and they're in a terminal already.
- TextBee push-based receipts (separate roadmap item under Later /
  "Push-based inbound for TextBee"). The override mapping for
  TextBee will use the existing best-effort lead-by-contact-uri
  fallback until that ticket lands.
- Persisting `_pending_by_provider_id` across process restarts.
  Override mode is a session-scoped rehearsal; survival across
  `--reload` is a nice-to-have, not required.

## Success criteria

- `OverrideConnector` no longer has a `_last_original` attribute;
  new `_pending_by_provider_id` is asserted in the connector's
  unit tests.
- `tests/test_override_connector.py` has a regression case named
  `test_override_remap_with_interleaved_sends` that sends to leads
  A then B, then routes inbounds in B-then-A order, and asserts
  each inbound rewrites to its correct original. The test fails on
  current `main` and passes on the fix.
- `BaseConnector.send` raises `ValueError` on a non-E.164
  `contact_uri` — one parametrised test per connector
  implementation.
- `autosdr e2e setup --phone +61414603957 --dry-run` (a no-op flag
  that prints planned mutations without making them) returns the
  full plan. `autosdr e2e setup` then `autosdr e2e teardown`
  round-trips the workspace state to byte-equality on
  `data/rehearsal/state.json` deletion + DB diff (campaigns
  ACTIVE-status set unchanged, override_to unchanged from
  pre-setup).
- README / `docs/ROADMAP.md` Done row added; `ARCHITECTURE.md`
  rehearsal section updated to point at `autosdr e2e` instead of
  the manual runbook.
- Backend test suite green (`uv run pytest`). Frontend `tsc -b
  --noEmit` unaffected (no UI changes).

## Effort & risk

- **Size:** M (~1 day; Part A is the only real design work, Part B
  is 1 hour, Part C is the bulk of the LOC but mechanical).
- **Touched surfaces:** `autosdr/connectors/override.py`,
  `autosdr/connectors/base.py`, `autosdr/connectors/{textbee,smsgate}.py`
  (E.164 guard call site), `autosdr/cli.py` (new subcommand
  group), `tests/test_override_connector.py`,
  `tests/test_base_connector.py` (new),
  `tests/test_cli_e2e.py` (new), `docs/ROADMAP.md`,
  `ARCHITECTURE.md`.
- **Risk:** Part A's correctness depends on the inner connector
  surfacing a stable `provider_message_id` on incoming receipts.
  SMSGate does (verified during the 0001 / 0006 work); TextBee's
  poll-only path may not carry an `in_reply_to`. The fallback
  (lead-by-contact-uri resolution in the reply pipeline) preserves
  current behaviour for the TextBee path until push-based receipts
  land.
- **Reversibility:** Part C is purely additive. Parts A and B are
  behaviour changes but they only narrow the contract (the bug
  case becomes a loud failure); no caller relies on the bug.

## Open questions

- **OQ1.** TextBee inbound today: does `IncomingMessage` carry a
  `provider_message_id` for the inbound itself? If yes, we can
  match on the *outbound* provider_id only when the user's phone
  echoes it back (rare on SMS); more realistically we drop to the
  fallback. **Decision needed:** accept fallback for TextBee, or
  block this ticket on the push-receipts ticket?
- **OQ2.** Where should `data/rehearsal/state.json` live — under
  `data/` (gitignored, ephemeral) or under `~/.autosdr/`
  (per-user, survives a workspace clone)? Default suggestion:
  `data/rehearsal/state.json` for symmetry with `data/.autosdr-pause`
  and the SQLite DB.
- **OQ3.** `autosdr e2e teardown` lead disposition: DNC the lead
  (current scope default) or delete it? DNC is safer (nothing
  references a deleted lead), delete is cleaner. Default: DNC.
- **OQ4.** Should the E.164 guard in Part B be a hard ValueError
  or a logged warning + best-effort send? Initial scope is hard
  raise — but the importer guarantees E.164 today, so the only
  callers that would trip the guard are buggy. Hard raise turns
  bugs into immediate test failures.

Resolve OQ1-OQ4 via a council mini-round before implementation per
the ticket-implementer workflow.

## Principle check

- **Owner stays in control.** Part A removes a footgun the operator
  can step on by accident; Part C makes the rehearsal a
  deliberate, auditable command. Both reinforce control. ✓
- **Honest contracts.** Part B turns a silent provider miss into a
  loud `ValueError`. ✓
- **Human always wins.** No change. The rehearsal is the
  human-pilot dress rehearsal; the CLI is operator-driven. ✓
- **AI loop is the moat.** No AI surface affected. ✓
- **Cheap before grand.** Part A is the smallest fix that closes
  the cross-talk; we explicitly defer multi-lead rehearsal mode
  and persistence-across-restart. ✓

## Decisions log

(Empty — populated during implementation.)

## Addendum — findings from the 2026-04-27 (evening) follow-up rehearsal

When we re-ran the rehearsal on the *real* SMSGate transport (no
override) we never got past `verify-hitl`. The reasons were six
distinct issues, three of which are **prod-push blockers** and should
be split out into their own tickets ahead of this one. Capturing all
of them here so context isn't lost; this section is the source-of-
truth for the new tickets.

### Finding 1 — Reply pipeline holds the SQLite write transaction across LLM API awaits  *(prod blocker)*

[autosdr/pipeline/reply.py:184](../../autosdr/pipeline/reply.py)
opens `with session_scope() as session:` and the entire
`process_incoming_message` body — including every
`await _classify_reply(...)` and the suggestion-generation calls —
runs **inside** that transaction. Each inbound therefore holds the
SQLite writer for the duration of every LLM API call (10–60s).

While the transaction is open:

- The LLM client's `_log_call`
  ([autosdr/llm/client.py:327](../../autosdr/llm/client.py)) opens a
  *separate* session to insert `llm_call` rows. That second session
  is a writer; SQLite (even in WAL) serialises writers; the second
  session blocks. With `busy_timeout=120000`
  ([autosdr/db.py:68](../../autosdr/db.py)) it waits 2 minutes then
  raises `sqlite3.OperationalError: database is locked`. Verified
  twice tonight — see dev-server log around `INSERT INTO llm_call`.
- `_log_call` is invoked synchronously (not via
  `loop.run_in_executor`). When it sits on a `cursor.execute` for the
  full busy_timeout, the asyncio event loop is *blocked*, not
  awaiting. `GET /api/status` against the same uvicorn worker timed
  out repeatedly tonight while the inbound was "processing".
- `data/autosdr.db-wal` ballooned to **365 MB** in one rehearsal
  session (DB itself is 377 MB). The long-held read transaction
  (the rehearsal's polling loop hitting `/api/threads/<id>/messages`
  every 3–10s) prevented WAL checkpointing while writers piled up.
  Recovered cleanly via manual `PRAGMA wal_checkpoint(TRUNCATE)`
  after killing the server.

The classification call itself succeeded — log shows
`{"intent": "question", "requires_human": true, "confidence": 0.95,
"reason": "..."}` — and a generation call produced a coherent
suggestion. The pipeline *logic* is correct; the persistence layer
under contention is what's broken.

**Recommended fix shape (sketched, not in scope here):**
- Commit and close the parent transaction before each `await`. Open
  a fresh session for the *write* phase that follows the LLM result.
- OR (cheaper) move `_log_call` writes off the request thread —
  `loop.run_in_executor(None, _log_call, payload)`, fire-and-forget.
- Cap `busy_timeout` at something compatible with HTTP request
  timeouts (e.g. 5s) and surface the failure rather than blocking
  the loop for 2 minutes.

This is a separate ticket. **Suggested 0008.**

### Finding 2 — Killswitch silently drops inbound webhooks  *(prod blocker)*

[autosdr/api/webhooks.py:45-47](../../autosdr/api/webhooks.py):

```python
if killswitch.is_paused():
    logger.info("dropping inbound while paused: %s", incoming.contact_uri)
    return
```

When the operator hits the killswitch ("pause everything; I'm going
to lunch"), every SMS that lands during that window is *gone* — no
DB row, no audit trail, no replay path on resume. We hit this
directly tonight: the rehearsal's killswitch was on per the
test-isolation plan, the user replied from their phone, and the
reply silently vanished.

Mental model the operator has: "killswitch pauses *outbound*, so I
can manually drive the rehearsal." Reality: it also vacuums the
inbound side. The HITL UX promise — "you always own the next reply"
— is silently broken.

**Recommended fix shape:**
- Persist the inbound to a `paused_inbound` table (or reuse
  [autosdr/models.py:UnmatchedWebhook](../../autosdr/models.py))
  when the killswitch is on, with a `replay_pending=True` flag.
- On `POST /api/status/resume`, re-enqueue every `replay_pending`
  inbound through `_process_in_background`.
- UI surfaces a count of "paused inbounds awaiting replay" on the
  killswitch banner so the operator knows they exist.

This is a separate ticket. **Suggested 0009.**

### Finding 3 — SMSGate inbound transport doesn't work behind a corporate VPN  *(prod blocker for this operator's network)*

The SMSGate Android app on this device (build that returns
`{{VERSION}}` in its swagger spec) supports inbound exclusively
via webhook push — there are no `/inbox` or `/inbox/refresh`
endpoints (probed; both return 404 even with a JWT minted at
`all:any` scope). The webhook URL must be HTTPS, *or* literal
`http://127.0.0.1`.

The operator's laptop sits behind a `100.64.0.1` corporate VPN
interface (Jamf-managed; visible in `ifconfig`). Tonight:

- `cloudflared tunnel` (QUIC over UDP/7844): the VPN blocks 7844;
  every dial errored `failed to dial to edge with quic: timeout: no
  recent network activity`.
- `cloudflared --protocol http2` (TLS over TCP/7844): same — same
  port, same block (`TLS handshake with edge error: read tcp
  100.64.0.1:60710->198.41.200.53:7844: i/o timeout`).
- `npx localtunnel --port 8000` (HTTPS/443 to `localtunnel.me`):
  process started, never opened a network socket. Either DNS or
  `localtunnel.me` is in the VPN's deny-list.

The reliable transports remaining are:

a. **ADB reverse port forward + USB cable.** SMSGate posts to
   `http://127.0.0.1:8000/api/webhooks/sms` on the *phone*; ADB
   tunnels the TCP back to the laptop's `127.0.0.1:8000`. Works
   off the corporate network entirely. Documented by SMSGate as
   "Local Network Tips → Use 127.0.0.1 with ADB reverse port
   forwarding for local testing".
b. **SMSGate Private Webhook certificate** + a local TLS proxy
   (`stunnel` / `caddy`) terminating on `192.168.0.133:8443`.
   More setup, but works without a phone-cable.
c. **Update the SMSGate Android app** to a build that ships
   `/inbox`, then add `poll_incoming` to
   [autosdr/connectors/smsgate.py](../../autosdr/connectors/smsgate.py)
   and run via the existing scheduler. Eliminates the webhook
   transport question entirely.

**Recommended scope additions** (fold into Part C of this ticket
or a new ticket):

- New CLI: `autosdr connectors smsgate verify-webhook` —
  registers a temporary self-test webhook (random `id`), calls
  the SMSGate device's "send to self" path with a test payload,
  asserts the local server receives the matching webhook within
  30s, then deletes the registration. Exits non-zero with a
  pointer to one of (a)/(b)/(c) on failure. Run this as part of
  the prod-push checklist.
- `autosdr connectors smsgate add-poll` — option (c) above,
  gated on the SMSGate device returning a non-empty
  `/inbox` swagger entry. Falls back to webhook-only if absent.

### Finding 4 — `data/autosdr.db` size

377 MB; 63,609 leads (mostly Apify imports). Not a blocker, but
two follow-ups worth noting:

- The Apify importer dumps the *raw NDJSON record* into a JSON
  column on `lead`. Many fields we never read; we could prune to
  the fields the pipeline actually consumes.
- Deleted/DNC'd leads are kept in-table forever. A periodic
  archive job would help.

Not blocking; out of scope here. Possible separate ticket if it
ever causes pain.

### Finding 5 — No simulator from CLI for "send synthetic inbound for *this* lead"

`POST /api/webhooks/sim` exists but requires the operator to know
the lead's `contact_uri` and craft the payload. During the
rehearsal we ended up running ad-hoc `httpx` Python from the
shell. Fold a `autosdr e2e simulate-inbound --content "..."`
helper into Part C of this ticket — it can read the rehearsal
phone from `data/rehearsal/state.json` and post for you. Cheap.

### Finding 6 — Confirm: the inbound classification *did* work

For the avoidance of doubt: the simulator path proved the inbound
pipeline *logic* is correct.

```text
purpose=classification model=gemini/gemini-3.1-flash-lite-preview
  intent=question requires_human=true confidence=0.95
  reason="The lead expressed interest but is asking for clarification
          on the nature of the service, which requires a human to
          explain the specific value proposition."

purpose=generation model=gemini/gemini-3-flash-preview
  draft="basically I fix Google listings and build sites for local
         businesses. I can sort Tunoa today and get a site live in a
         week. want me to show you a design?"
```

The findings above are about *delivery and durability* of that
correct logic, not about whether the model classifies right.

### Net effect on the prod-push decision

Findings 1 and 2 should each be their own ticket and should land
*before* the next prod push. Finding 3 should land before the
operator pushes from a corporate-VPN network specifically (or the
operator should push from a non-VPN network, which is a runbook
item). Findings 4–6 are nice-to-haves.

The original Override + E.164 + e2e CLI scope of *this* ticket is
unchanged — it's still real; it's just no longer the most
urgent thing.

## Reference: prod-push rehearsal runbook (current state, pre-CLI)

For posterity, this is the manual sequence the CLI replaces:

```bash
# 1. Pre-flight
curl -s -X PATCH http://127.0.0.1:8000/api/workspace/settings \
  -H 'Content-Type: application/json' \
  -d '{"rehearsal": {"override_to": null}}'
curl -s -X POST http://127.0.0.1:8000/api/status/pause   # killswitch on
# Pause every ACTIVE campaign by hand:
for id in $(curl -s :8000/api/campaigns | jq -r '.[] | select(.status == "active") | .id'); do
  curl -s -X POST :8000/api/campaigns/$id/pause
done

# 2. Lead import (CSV in data/rehearsal/single_lead_e2e.csv, gitignored)
curl -s -X POST :8000/api/leads/import/commit \
  -F "file=@data/rehearsal/single_lead_e2e.csv"

# 3. Campaign create + assign
curl -s -X POST :8000/api/campaigns -H 'Content-Type: application/json' -d '{
  "name": "Prod rehearsal - manual e2e",
  "goal": "...",
  "outreach_per_day": 1,
  "followup": {"enabled": false, "template": ""}
}'
curl -s -X POST :8000/api/campaigns/<id>/assign-leads \
  -H 'Content-Type: application/json' -d '{"lead_ids": ["<lead-id>"]}'

# 4. Kickoff (kickoff bypasses the killswitch internally via allow_manual_send)
curl -s -X POST :8000/api/campaigns/<id>/kickoff \
  -H 'Content-Type: application/json' -d '{"count": 1}'

# 5. Operator replies from phone, verifies HITL flow in UI

# 6. Teardown
curl -s -X POST :8000/api/campaigns/<id>/complete
# Re-ACTIVE the campaigns that were paused in step 1.
```
