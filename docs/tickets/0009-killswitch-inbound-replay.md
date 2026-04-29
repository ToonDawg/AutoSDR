# [hardening/killswitch] Killswitch must not silently drop inbound webhooks

<!-- TYPE: hardening -->
<!-- AREA: webhooks / killswitch -->
<!-- SEVERITY: prod-push blocker -->

## Problem

`autosdr/api/webhooks.py:45-47`:

```python
async def _process_in_background(
    *, connector: BaseConnector, workspace_id: str, incoming: Any
) -> None:
    if killswitch.is_paused():
        logger.info("dropping inbound while paused: %s", incoming.contact_uri)
        return
```

When the operator hits the killswitch ("pause everything; I'm
going to lunch") every inbound SMS that lands in the pause window
is *gone*:

- No `Message` row.
- No `unmatched_webhook` row (we don't even reach
  `connector.parse_webhook` for the killswitch case … actually
  we do — re-read: `parse_webhook` runs in the request handler
  before the background task, so we have an `IncomingMessage`,
  but we throw it away).
- No audit log.
- No replay path on resume.

The HITL operator's mental model — "I always own the next reply;
the killswitch is a *send* pause" — is silently violated.

We hit this directly during the 2026-04-27 evening rehearsal:
the killswitch was on per the test-isolation plan, the operator
replied from their phone, the SMSGate device delivered the
webhook, the API responded 202, and then the message vanished.
The operator's confusion ("I replied. Nothing looks to show in
this thread?") triggered a half-hour of debugging through
SMSGate's API to figure out why the webhook hadn't arrived —
when in fact it had, and we'd thrown it away.

This is also a *compliance* issue: the inbound might contain
"STOP" / "UNSUBSCRIBE", which ticket 0001 promises is a
deterministic shortcut. Today, "STOP sent during pause" is
silently lost; the lead remains opt-in; the next outreach beat
sends to a person who has unambiguously asked us to stop. This
is the single failure mode 0001 was meant to make impossible.

## Hypothesis

If we (a) persist *every* inbound to a durable queue regardless
of killswitch state, and (b) replay the queue on resume through
the same `process_incoming_message` path, then:

- Pause becomes a *processing* pause, not a *durability* pause.
- The 0001 STOP-shortcut promise is honoured even across a
  pause window.
- The HITL contract ("you always see the reply") is true.

Measured by:

- New `tests/test_killswitch_inbound_durability.py` cases:
  1. With killswitch ON, a `POST /api/webhooks/sim` results in
     a row in the new `paused_inbound` table within 1 s.
  2. The same test, then `POST /api/status/resume`, results in
     the inbound flowing through `process_incoming_message`
     and the thread reaching the expected paused-for-HITL
     state.
  3. With killswitch ON, an inbound containing "STOP" results
     in the lead being marked `do_not_contact=True` *as soon
     as the killswitch resumes*, not on next inbound.
- New `GET /api/status` includes
  `paused_inbound_pending_count`. Frontend killswitch banner
  renders this count.

## Scope

### Part A — `paused_inbound` durable queue

New table in `autosdr/models.py`:

```python
class PausedInbound(Base):
    __tablename__ = "paused_inbound"
    __table_args__ = (
        Index("idx_paused_inbound_workspace", "workspace_id", "created_at"),
    )

    id: Mapped[str] = mapped_column(primary_key=True, default=uuid_str)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspace.id"))
    connector_type: Mapped[str]                   # "smsgate" | "textbee" | …
    contact_uri: Mapped[str]
    content: Mapped[str]
    provider_message_id: Mapped[str | None]
    raw_payload: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    replayed_at: Mapped[datetime | None] = mapped_column(nullable=True)
```

Additive migration via `_ADDITIVE_TABLE_MIGRATIONS` (or whatever
the existing pattern is — match `unmatched_webhook`).

### Part B — Webhook handler change

Replace the silent-drop in `_process_in_background` with a
persist-then-skip:

```python
if killswitch.is_paused():
    _persist_paused_inbound(workspace_id, connector, incoming, payload)
    logger.info(
        "killswitch on; queued inbound for replay: %s",
        incoming.contact_uri,
    )
    return
```

Where `_persist_paused_inbound` writes one `PausedInbound` row in
its own short transaction (≤ 100 ms — these are small inserts and
should never compete with the reply pipeline's long writer).

The inbound `Message` row is *not* created at this point — the
thread doesn't exist yet, lead resolution hasn't happened. The
queue is the source-of-truth until replay.

### Part C — Resume-time replay

Wire `POST /api/status/resume` to drain the queue:

```python
def resume() -> StatusOut:
    killswitch.unpause()
    asyncio.create_task(_drain_paused_inbounds())
    return current_status()
```

`_drain_paused_inbounds` reads every unreplayed
`PausedInbound`, oldest-first, and for each:

- Reconstructs the `IncomingMessage`.
- Looks up the matching connector (must match the row's
  `connector_type`; if the active connector has changed since
  the row was queued, log a warning and skip — operator can
  decide to switch connector back, or DELETE the row by hand).
- Calls `process_incoming_message`.
- Stamps `replayed_at` on success; leaves untouched on failure
  (so the next resume retries).

Drain runs serially (one inbound at a time) to keep the reply
pipeline's existing single-writer assumption intact, and to
avoid surprise-bursting the LLM API on resume after a long
pause.

### Part D — Surface the queue depth

- `GET /api/status` adds
  `paused_inbound: { pending_count: int, oldest_pending_at: str | null }`.
- Frontend killswitch banner adds a count badge: "12 inbound
  messages waiting for resume". Clicking the badge opens a
  small modal listing the pending rows with `contact_uri`,
  `content`, `created_at`. No actions on the modal yet — read-
  only for v1.
- `autosdr status` CLI: extend the existing summary to print
  the pending count.

### Out of scope

- Per-row "drop this paused inbound" UI action. If a row is
  problematic (the operator looks at it and decides "no, don't
  reply"), they can DELETE it from the DB directly. v2 if
  asked-for.
- Replaying inbounds *while* the killswitch is on (e.g. "let
  me handle this one inbound during pause"). Adds operator
  cognitive load for a use case we don't actually have.
- Auto-expiring rows after N days. Today's volume doesn't
  justify; revisit if the table ever grows.

## Success criteria

- `paused_inbound` table exists; additive migration tested.
- Webhook handler queues instead of dropping when paused;
  unit test covers both `/api/webhooks/sms` and
  `/api/webhooks/sim` paths.
- `POST /api/status/resume` triggers a drain that walks the
  queue oldest-first; the drain doesn't block the response
  (`asyncio.create_task`).
- `tests/test_killswitch_inbound_durability.py` (new) green —
  three cases above.
- `tests/test_opt_out_during_pause.py` (extend existing
  test_opt_out_keywords) — STOP during pause results in
  `lead.do_not_contact=True` on resume.
- `GET /api/status` exposes `paused_inbound.pending_count`.
- Frontend killswitch banner renders the count.
- Backend test suite green; frontend `tsc -b --noEmit` clean.

## Effort & risk

- **Size:** M (½–1 day). Mostly mechanical: new table, new
  table writer, drain function, status field, tiny UI badge.
- **Touched surfaces:**
  `autosdr/models.py` (new table),
  `autosdr/db.py` (additive migration),
  `autosdr/api/webhooks.py` (queue instead of drop),
  `autosdr/api/status.py` (resume hook + status field),
  `autosdr/killswitch.py` (only if we move the inbound check
  into the killswitch module — defer; keep the logic in
  webhooks.py for now),
  `tests/test_killswitch_inbound_durability.py` (new),
  `tests/test_api_smoke.py` (status field),
  `frontend/src/components/.../KillswitchBanner.tsx` (badge),
  `frontend/src/lib/types.ts` (StatusOut type),
  `ARCHITECTURE.md` (killswitch semantics note).
- **Risk:** Replay re-uses `process_incoming_message`, so all
  the issues from ticket 0008 (transaction across awaits) apply
  on the replay path too. **Sequence: ship 0008 first, then
  0009.** The replay drain is the primary place we'd want to
  send three inbounds in a row through the pipeline; without
  0008 it would deadlock the dev server immediately.
- **Reversibility:** Pure additive — new table, new code path
  guarded by `killswitch.is_paused()`. Reverts cleanly.

## Open questions

- **OQ1.** What should happen if the active connector at
  resume-time is different from the connector that captured the
  inbound? Default position: log a warning, skip the row,
  leave `replayed_at` NULL. The operator can either switch
  connector back to drain, or delete by hand. Other option:
  attempt the replay anyway since `IncomingMessage` is
  connector-agnostic at the pipeline boundary; the connector
  type only matters for outbound.
- **OQ2.** Should `POST /api/status/resume` block on the drain
  or fire-and-forget? Recommend fire-and-forget (`create_task`)
  + status endpoint exposes the count so the operator can see
  progress. Blocking would tie up the resume request for as
  long as the queue takes to drain.
- **OQ3.** Should the simulator (`/api/webhooks/sim`) also
  honour the killswitch and queue? Default position: yes,
  same code path — the simulator's whole purpose is to mimic
  real inbound behaviour. Means the queue accumulates from
  `autosdr sim inbound` too, which is the right behaviour
  during rehearsals.
- **OQ4.** Audit-log row for the killswitch drop: do we keep
  the existing `logger.info("dropping inbound while paused")`
  line for grep-compat, or change the message? Change to
  "queued inbound for replay" since that's now true. Update
  any test that asserts the old string.

Resolve via council mini-round before implementation.

## Principle check

- **Owner stays in control.** Today the killswitch silently
  loses messages — the opposite of control. This makes the
  killswitch a pause, not a delete. ✓ ✓
- **Honest contracts.** Status endpoint surfaces pending
  count; killswitch banner shows it. ✓
- **Human always wins.** The HITL operator's "I see every
  reply" promise is restored. ✓ ✓
- **AI loop is the moat.** No AI-surface change. ✓
- **Cheap before grand.** Tiny table + tiny drain function;
  defers per-row UI actions and auto-expiry to v2. ✓

## Decisions log

(Empty — populated during implementation.)
