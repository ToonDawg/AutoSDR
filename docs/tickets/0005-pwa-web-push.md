# [feature/ui+api] PWA install + Web Push for HITL escalations

<!-- TYPE: feature -->
<!-- AREA: ui + api -->

## Problem

`autosdr-doc1-product-overview.md § 2 + § 6` describes the operator's
control surface as **"a Progressive Web App that can be installed on
desktop or mobile"** with **"Web Push notifications [that] alert the
owner when a thread needs attention, even when the app is not in the
foreground."** The success metric in § 6 is concrete:

> PWA notifications: < 10s from HITL escalation event.

The shipped reality (as of `470345c`) is a desktop-only React/Vite
console with no manifest, no service worker, and no push transport.
HITL surfacing relies on `useHitlThreads` polling
(`frontend/src/lib/useHitlThreads.ts`) — the operator has to keep a tab
open and refresh.

This bites the persona. The Time-Poor Founder is "doing their own sales"
and is "frustrated by … losing track of which leads have been
contacted" (`autosdr-doc1 § 4`). The whole point of HITL is that the
operator *isn't* sitting at the dashboard; they're at a job site / on a
call / making coffee. Without push, an escalation that needs a
sub-minute response can sit until they happen to refresh.

## Hypothesis

If we ship installable PWA + Web Push, escalations reach the operator
< 10s after `pause_thread_for_hitl` is invoked, on any device they
installed the app on, regardless of whether the app is open. Measured
by:
- A test (manual + scripted) showing the wall-clock between
  `pause_thread_for_hitl` and an OS-level notification arriving on a
  subscribed device. Target P50 < 10s, P95 < 30s on a typical home
  internet link.
- Operator no longer needs to keep a browser tab open during a campaign
  to catch escalations.

## Scope

- **Service worker + manifest**:
  - Add `vite-plugin-pwa` (or hand-rolled equivalent — see Open
    questions) to `frontend/vite.config.ts`.
  - Manifest: `name`, `short_name="AutoSDR"`, theme colours matching
    the existing Tailwind palette, icons (192/512 PNG + maskable).
  - Service worker: precache the built shell + offline fallback +
    `push` and `notificationclick` handlers.
  - The dev path stays unchanged; PWA features only activate on the
    production-served build (`autosdr/webhook.py` already serves
    `frontend/dist`).
- **VAPID keys**:
  - Generate at first run if absent and persist on
    `workspace.settings.push.vapid_public` /
    `vapid_private` (the settings blob hot-reloads; see
    `autosdr/workspace_settings.py`). Never expose `vapid_private` via
    the API.
  - Operator-facing copy on Settings → Notifications: "AutoSDR uses
    your own VAPID keys; nothing leaves your network except the push
    payloads to the browser-vendor push gateway".
- **New table** `push_subscription`:
  ```
  id (uuid), workspace_id, endpoint TEXT UNIQUE, p256dh, auth,
  user_agent, created_at, last_seen_at, last_error TEXT NULL
  ```
  Manual SQLite migration on startup if the table doesn't exist.
- **Endpoints**:
  - `POST /api/push/subscribe` — body `{endpoint, keys: {p256dh,
    auth}, user_agent}` — upserts on `endpoint`.
  - `DELETE /api/push/subscribe` — body `{endpoint}` — soft-removes
    (or hard-removes; decision in Open questions).
  - `GET /api/push/vapid-public` — returns `{public_key}` so the SW
    can subscribe.
  - `POST /api/push/test` — fires a test notification to one or all
    of the operator's subscriptions; surfaces any "Gone" errors so
    the operator can clean up dead subs.
- **Push transport**:
  - Add `pywebpush` (or equivalent) to project deps. Synchronous
    library; wrap calls in `asyncio.to_thread` so the scheduler /
    HITL hot path doesn't block.
  - On HTTP 404 / 410 from the push service → mark subscription dead
    and stop sending (don't auto-delete; operator can clear from UI).
- **Hook into HITL escalation**:
  - In `autosdr/pipeline/_shared.py` `pause_thread_for_hitl` (the
    canonical seam — invoked from `pipeline/reply.py:_park_with_suggestions`
    and `pipeline/reply.py:_run_auto_reply` and outreach evaluator
    failure path), emit a push event after the DB flush succeeds.
  - Payload: `{title, body, thread_id, lead_name, hitl_reason,
    escalated_at}` — small (<= 4 KB to fit Web Push payload limits).
  - Notification click: navigate to `/inbox/<thread_id>` (decision
    needed if there's a path conflict — see Open questions).
- **Settings → Notifications card**
  (`frontend/src/routes/settings/`):
  - "Enable browser notifications" button → registers SW + subscribes.
  - List of registered devices with `user_agent` + last-seen.
  - "Send test notification" button.
  - Per-event filter for v1: HITL escalations on/off (default on);
    later, send-failures and quota-exhausted.
- **Killswitch coverage**: pushes from the scheduler hot path must
  honour the killswitch (`autosdr/killswitch.py`) — paused workspace
  doesn't fire pushes for retroactively-detected events.

## Out of scope

- Native iOS/Android apps. PWA + Web Push gets us the iOS Safari case
  (since iOS 16.4 PWA push works for installed PWAs) and Android (full
  support).
- Push for non-HITL events (send-failure, quota exhausted, daily
  digest). Land HITL first; add the surface to the
  `pause_thread_for_hitl` seam so adding more events later is a few
  lines.
- Multi-user push targeting. Workspace is single-operator
  (`autosdr-doc1 § 5`); subscriptions belong to the workspace, not to
  user accounts.
- E-mail or SMS fallback for missed notifications. Out of scope for v0;
  consider if push-delivery telemetry shows misses.
- Rich notifications with action buttons ("Reply A", "Reply B", "Skip")
  inline. Useful but each action button is its own SW handler with its
  own ergonomics — defer until v0 is stable.

## Success criteria

- New `tests/test_push_subscriptions.py` covers subscribe / unsubscribe
  / test-fire endpoints; mocks the HTTP push call.
- New `tests/test_pause_thread_for_hitl.py` (or extend
  `test_hitl_dismiss.py`) asserts that paused-for-HITL events trigger
  a push attempt to every active subscription; failed sends mark
  subscriptions dead but don't crash the pipeline.
- Manual smoke: install the PWA on a phone, run
  `autosdr sim inbound --content "tell me more"` from the dev box, see
  the notification appear within 10s.
- Operator can toggle notifications off / on without leaving the app.
- A subscription that's been deleted on the device side (Permission
  revoked) ends up in a dead state in the DB, NOT silently retried
  forever.

## Effort & risk

- **Size:** L (1–2 weeks)
- **Touched surfaces:**
  - `frontend/vite.config.ts`, new SW under `frontend/src/sw/`,
    `frontend/index.html`, `frontend/src/main.tsx` (registration),
    `frontend/src/routes/settings/`.
  - `autosdr/api/push.py` (new), `autosdr/api/__init__.py`,
    `autosdr/api/schemas.py`, `autosdr/api/deps.py`,
    `autosdr/models.py` (new table — invasive),
    `autosdr/pipeline/_shared.py` (hook), `autosdr/workspace_settings.py`,
    `pyproject.toml` (`pywebpush`).
- **Change class:** invasive (schema + new transport on the HITL
  hot-path).
- **Risks:**
  - Service-worker dev/prod parity: SW only activates on the
    production-served build (`autosdr/webhook.py` serves
    `frontend/dist`); the HMR `./scripts/dev.sh` flow needs a
    documented "build first" step to test push end-to-end.
  - VAPID key rotation: there's no rotation story in v0. Document.
  - Push payload size: 4 KB cap (after encryption). Don't include
    `lead.raw_data` snippets; just thread id + minimal context.
  - HITL hot-path latency: a slow push gateway shouldn't slow
    `pause_thread_for_hitl`. Wrap in `asyncio.to_thread` and
    fire-and-forget with logging.
  - iOS quirk: iOS 16.4+ requires the PWA to be installed via
    "Add to Home Screen" before push works — document for the operator.
  - Killswitch: pushes must respect the kill flag — do not push during
    a paused window. Cheap; add a guard.

## Open questions

- `vite-plugin-pwa` vs. hand-rolled SW. Recommend the plugin — saves a
  week of SW boilerplate and supports manifest + Workbox precaching out
  of the box. Audit its bundle / lock-in cost.
- Should we colocate the push-subscription record with workspace
  settings JSON (no schema change, just a list) or in a dedicated
  table? **Dedicated table** — subscriptions can be many, may grow
  with multi-device, and we want to track per-sub last-seen / dead
  state cleanly.
- Notification click URL: `/inbox/<thread_id>` or
  `/threads/<thread_id>`? Inbox is the HITL queue; recommend
  `/inbox?thread=<id>` to land on the queue with the thread
  pre-selected. Decision.
- Per-event filtering granularity for v0: just "HITL on/off", or
  "HITL + auto-reply-failure + connector-error"? Recommend HITL only
  for v0; tracker entry for the others.
- Soft-delete vs hard-delete on `DELETE /api/push/subscribe`. Recommend
  hard-delete; nothing depends on a tombstone.
- Telemetry: do we record push delivery success/failure to disk? Useful
  for "did this notification fire?" debugging. Recommend a row per
  attempt in a new `push_event` table — but defer to a follow-up if
  effort tightens. Decision.

## Principle check

- Simplicity first: ⚠ (it's a real-time transport on top of a polling
  app; complexity justified by the success metric in doc1 § 6 and the
  persona being absent-from-desk by definition)
- Quality over speed: ✓
- Honest data contracts: ✓ (clear schema, no magic)
- Extensible by design: ✓ (one seam, more event types to follow)
- Human always wins: ✓ (push *is* the human winning faster)
- Owner stays in control: ✓ (per-device toggle, kill-switch respected,
  test fire button)

## Links

- Spec: `autosdr-doc1-product-overview.md § 2` (PWA), § 6 (< 10s
  metric), § 8 (HITL flow).
- Spec: `autosdr-doc4-onboarding-config.md` — PWA install steps.
- Architecture: `ARCHITECTURE.md § 9` (reply pipeline / HITL),
  `ARCHITECTURE.md § 13` (observability — push events should be
  loggable).
- Code: `autosdr/pipeline/_shared.py:pause_thread_for_hitl` (seam),
  `autosdr/pipeline/reply.py:472-498` (HITL park),
  `autosdr/scheduler.py` (lifespan; SW build assumption),
  `autosdr/webhook.py` (serves `frontend/dist`),
  `frontend/src/lib/useHitlThreads.ts` (current poll-based UI),
  `frontend/src/routes/settings/`.
- Roadmap: `docs/ROADMAP.md` → Next → row 5.

## Dependencies

- Blocks: nothing; opens the door to "send-failed" and "quota
  exhausted" pushes as cheap follow-ups.
- Blocked by: nothing technically; sequenced after 0001 / 0002 / 0003 /
  0004 because those are smaller and unblock current operator work.
  This one is the bigger investment.
- Related: 0001 (a STOP-driven close could optionally push too — defer).
