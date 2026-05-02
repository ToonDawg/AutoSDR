# [feature/ui+api] PWA install + Web Push for HITL escalations

<!-- TYPE: feature -->
<!-- AREA: ui + api + ops -->

> **2026-05-02 refinement.** Added § *Remote-access architecture* (with
> council mini-round) and § *Mobile preconditions*. The original ticket
> assumed the operator was on the same LAN as AutoSDR; the actual
> persona is on a phone, on cellular, while AutoSDR is at home. That
> changes the network topology question from "PWA install + push" to
> "PWA install + push + how does my phone reach my PC and how does my PC
> reach my phone?" Both directions now have an explicit verdict.

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

There is a **second, network-topology problem** sitting alongside push
that was elided in the v1 ticket:

- **Phone → AutoSDR**. The operator wants to open the dashboard from
  their phone *while AutoSDR is at home on a PC and the phone is on
  cellular.* `localhost:8000` doesn't reach across the public internet,
  and most home ISPs CGNAT the connection so port-forwarding is not
  reliable.
- **AutoSDR → phone**. The scheduler needs to talk to the SMSGate app
  on the operator's phone (the SMS device). When the phone is on the
  home Wi-Fi this is the existing LAN path
  (`autosdr/connectors/smsgate.py:6 — "Device local-server mode"`).
  When the phone leaves the LAN, the path breaks.

Push notifications themselves are unaffected by either direction
(browser-vendor push gateways are public-internet endpoints), but **the
notification deep-link is unreachable** until the phone-to-AutoSDR
path is solved. So this ticket pulls in the network-topology decision
as a hard precondition.

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

## Remote-access architecture (council-resolved 2026-05-02)

This is the network-topology decision the original ticket avoided.
Resolved via a four-voice council mini-round; verdict is binding for
this ticket and documented for the operator.

### Options assessed

| ID | Pattern | Phone → AutoSDR | AutoSDR → phone | Cost | Public exposure of dashboard |
| --- | --- | --- | --- | --- | --- |
| **A** | **Tailscale on PC + phone**, SMSGate Local Server inside the tailnet | tailnet IP / MagicDNS | tailnet IP to phone:8080 | $0 (free tier ≤ 100 devices) | none |
| B | Cloudflare Tunnel for dashboard + SMSGate Cloud Server for SMS | `https://autosdr.example.com` (Cloudflare Access auth) | `api.sms-gate.app` | $0 (CF free) | yes (auth-gated) |
| C | Public IP + port-forward + DDNS + Let's Encrypt + SMSGate Cloud | `https://your-ddns.example.com` | `api.sms-gate.app` | $0–$10/mo (most AU ISPs are CGNAT, public IP often costs) | yes (auth-gated) |
| D | SMSGate Cloud only, no remote dashboard | n/a (laptop-only) | `api.sms-gate.app` | $0 | none |

### Council mini-round (Skeptic / Pragmatist / Critic / Architect)

**Architect:** A. Tailscale is the only option that solves both
directions without inventing a public hostname for a dashboard that
holds SMS PII + LLM API keys. `autosdr/connectors/smsgate.py` already
supports all three SMSGate deployment shapes, so the connector
*doesn't change* — only the URL the operator pastes does.

**Skeptic:** A, but the framing matters. "No public exposure" is
"tailnet-private", not "no third party" — Tailscale is itself a
control-plane SPOF. Compromised Tailscale account or sloppy ACLs
expose the same dashboard A is supposed to keep private. Hidden
assumption: the phone is a stable server on cellular; Android power
management makes this intermittent unless the operator accepts
always-on VPN. **Surprise: push notifications already require
public-internet origins** (browser-vendor push gateways) — "keep
everything off the clearnet" is partly theatre unless we treat the
notification URL as its own mini-threat-model.

**Pragmatist:** A, ships fastest. README path is two installs and one
sanity-check ("phone browser → http://[pc-tailnet]:8000 should work";
"home box can reach phone's 100.x"). Recovery is a familiar mental
model: VPN is flaky, restart it. **Surprise: install ergonomics aren't
symmetric.** The founder will install Tailscale on both devices in
two minutes; they will *not* intuit that **the PC's FastAPI must bind
to the tailnet interface (or all interfaces), not just localhost**,
and that mixed `localhost` URLs in env / CORS will strand the phone.
The doc wins by naming exact URL patterns and one sanity checklist.

**Critic:** A is the architecturally honest choice. **Failure modes
are loud** (MagicDNS doesn't resolve, Tailscale disconnected) versus B
which fails *silently* (Cloudflare Access policies drift, cloud SMS
queues degrade, threads look "fine" while delivery silently breaks).
Long-tail risk: phone-side Tailscale dies on cellular due to OEM
battery optimisation — works Monday, dead Thursday, blamed on
AutoSDR. **Surprise: 0005 itself collides with A's privacy posture.**
Web Push registers against a public origin; if we ship push naively,
we'll mix public origins (push) and private origins (dashboard) in
ways that undo why A was chosen.

### Decision

**A: Tailscale on PC + phone is the documented default**, with two
operator escape hatches:

1. **For the SMS direction**, the operator can keep SMSGate in
   *Local Server* mode inside the tailnet (one tool, recommended) OR
   switch to *Cloud Server* mode (two tools, decouples SMS from VPN
   reliability). Both are documented; both work today; the connector
   already supports both. The operator chooses based on whether
   phone-side Tailscale battery cost is acceptable. If they go cloud,
   the AutoSDR base URL is `https://api.sms-gate.app/3rdparty/v1` and
   the connector unchanged (`autosdr/connectors/smsgate.py:6`).
2. **For the dashboard direction**, A is canonical. B is documented as
   a deferred upgrade path if the operator ever wants a public-vanity
   URL (e.g. multi-user demo) — explicitly *not* the default.

**Strongest dissent (preserved):** Critic's loop multiplication concern
— push notifications mix public origins with the private dashboard
posture. Mitigation: notification payloads MUST NOT include lead PII
or message content (just thread id + lead first name + a short
generic body); deep-links MUST resolve to the tailnet hostname (which
is meaningless on the public internet — a leaked link is harmless to
anyone not on the tailnet). Embed in § *Push transport* below.

**Confidence:** medium-high. Free, scales to operator's actual usage,
fails loudly. The phone-on-cellular Tailscale battery question is the
unknown; document the SMSGate Cloud fallback so the operator can
sidestep it.

### What this means for the operator

The README's **"No tunnel / public URL — the scheduler polls"** line
(`README.md:50`) is true *for the LAN case*, false *for the away-from-
home case*. Add a *"Remote access"* subsection to the README under
*Networking* with the Tailscale walk-through and the SMSGate-cloud
escape hatch.

### What this means for AutoSDR's host binding

`HOST=0.0.0.0` is required when running with Tailscale (so the FastAPI
process listens on the tailnet interface, not just localhost). Today
the docs default to `HOST=127.0.0.1`. Add a config validator that
*warns* on `127.0.0.1` if Tailscale is detected (`tailscale status`
exits 0); document the security trade-off (binding 0.0.0.0 with
Tailscale ACLs is private; binding 0.0.0.0 without Tailscale on a
shared LAN is not).

This is the **PC-bind-interface footgun** the Pragmatist surfaced.
Naming it explicitly in the README + one config check at startup
removes a class of "I followed the docs and the phone can't connect"
support questions before they happen.

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

  ```sql
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
  - **Privacy posture (Critic-mandated, see *Remote-access architecture*).**
    Notification payload MUST be a fixed shape: `{title, body,
    thread_id, lead_first_name, hitl_reason, escalated_at}`. *No lead
    last name, phone, business name, message content, or LLM output.*
    A notification leaked off the tailnet (e.g. someone glances at the
    operator's lock-screen) reveals "thread X needs attention" — no
    more.
  - Deep-link URL is the operator's tailnet hostname (e.g.
    `http://autosdr-pc.tail-scale.ts.net:8000/inbox?thread=<id>` or
    whatever MagicDNS / Tailscale name the operator chose). The
    `/api/push/vapid-public` response includes a `dashboard_origin`
    field the SW reads, so the SW doesn't have to guess. Documented
    operator override for the SMSGate-cloud-only path:
    set the `dashboard_origin` in `workspace.settings.push.dashboard_origin`
    if it differs from the API origin (rare; the default is
    same-origin).
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
- **README + Settings → Networking copy** (operator-facing):
  - New README subsection *"Remote access (use AutoSDR from your phone)"*
    with the Tailscale walk-through, the `HOST=0.0.0.0`
    requirement, the SMSGate-mode trade-off, and the *"phone browser
    → http://[pc-tailnet]:8000 should load — sanity-check before
    setting up push"* checklist.
  - New Settings → Networking card (read-only) showing:
    - Detected dashboard origin (what the API is being served from).
    - Detected Tailscale state (parse `tailscale status` if installed).
    - The configured `dashboard_origin` for push deep-links.
    - A single *"How to reach AutoSDR from my phone"* link out to the
      README section.

## Mobile preconditions

This ticket's success criterion *"manual smoke: install the PWA on a
phone, run `autosdr sim inbound …`, see the notification appear within
10s"* requires the dashboard to be **usable on a 390-px-wide viewport
when the operator taps the notification.** Today, the dashboard isn't
responsive (`README.md:14` says laptop-only; the Leads / Threads /
Logs / Inbox / ThreadDetail routes all assume ≥1024 px and overflow
without a horizontal-scroll wrapper).

**Sequencing:** ticket **0015 (mobile-responsive operator console)**
must ship before 0005 to avoid landing push on a UI the operator
can't use. 0015 is sized M, ~4-6 days; 0005 is L, ~1-2 weeks. Total
sequence: *0008 → 0009 → 0015 → 0016 → 0005*.

If 0005 ships ahead of 0015 by accident, the smoke test will fail at
the *"can the operator actually triage from the phone"* step — push
will land, but the experience past that is poor. Sequence-or-it-fails.

## Out of scope

- **Mobile-responsive UI itself** — that's ticket 0015 and **must
  ship before this one** (see § *Mobile preconditions*).
- **Tailscale automation / install scripting**. The README walks
  through the operator-side install; AutoSDR doesn't ship a
  Tailscale wrapper. (If we later want a `Settings → Test
  remote-access` button that calls `tailscale ping` we can add it,
  but it's out of v0.)
- **Cloudflare Tunnel as a default**. Documented as an upgrade path
  for the operator who wants a public vanity URL; not the default,
  not built in.
- **A self-hosted push relay**. Web Push browser-vendor gateways are
  always public-internet; we don't need a relay.
- Native iOS/Android apps. PWA + Web Push gets us the iOS Safari case
  (since iOS 16.4 PWA push works for installed PWAs) and Android (full
  support).
- Push for non-HITL events (send-failure, quota exhausted, daily
  digest, LLM-deploy-watch alerts from ticket 0016). Land HITL first;
  add the surface to the `pause_thread_for_hitl` seam (and the new
  health-flag seam from 0016) so adding more events later is a few
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
- New `tests/test_push_payload_privacy.py` asserts the payload shape
  contains thread_id + lead_first_name + hitl_reason + escalated_at
  ONLY — no message content, no last name, no business name. (The
  Critic-mandated privacy posture from § *Remote-access architecture*.)
- New `tests/test_dashboard_origin_resolution.py` covers the
  `dashboard_origin` resolution: defaults to same-origin, honours
  the operator override at `workspace.settings.push.dashboard_origin`.
- **Manual smoke (mobile, on cellular)**: install the PWA on a phone
  *that is on Tailscale, on cellular* (not the home Wi-Fi), run
  `autosdr sim inbound --content "tell me more"` from the dev box,
  see the notification appear within 10s, tap it, land on the
  thread detail, see suggested replies. Asserts the full
  Tailscale + push + tailnet-deep-link path works end-to-end. **This
  is the load-bearing smoke** — anything else is a partial test.
- Operator can toggle notifications off / on without leaving the app.
- A subscription that's been deleted on the device side (Permission
  revoked) ends up in a dead state in the DB, NOT silently retried
  forever.
- README has the *Remote access* walk-through; Settings → Networking
  card renders the detected dashboard origin and Tailscale state.

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
- **OQ-Net1** (new). Detect Tailscale at startup and warn on
  `HOST=127.0.0.1`, or stay quiet and document only? Recommend
  warning *and* a Settings → Networking card that surfaces the
  actual bind state — operator-visible is better than a doc
  nobody reads. The Pragmatist's "PC bind interface footgun" is
  the failure mode this guards against. Decision.
- **OQ-Net2** (new). What's the SMSGate guidance? Today the connector
  supports all three modes; the README should default-recommend Local
  Server inside the tailnet (one tool, fewer third parties). Cloud
  Server is the documented escape hatch when phone-side Tailscale
  battery cost is a problem. Recommend ship both, default-document
  Local Server. Decision.
- **OQ-Net3** (new). Should `dashboard_origin` resolution rely on
  the request `Host` header at subscription time, or on a separate
  `workspace.settings.push.dashboard_origin` field? Recommend
  default-from-Host-header, override-via-setting. The setting is the
  escape hatch for the rare operator who has the API at one origin
  and the dashboard at another. Decision.

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

- **Blocks:** nothing; opens the door to "send-failed", "quota
  exhausted", and **"deploy health alert" (ticket 0016)** pushes as
  cheap follow-ups.
- **Blocked by:** **ticket 0015 (mobile-responsive operator console)**.
  See § *Mobile preconditions* — push without a usable mobile UI is a
  notification leading to a broken page. **Sequence: 0008 → 0009 →
  0015 → 0016 → 0005.**
- **Related:**
  - 0001 (a STOP-driven close could optionally push too — defer).
  - 0009 (`paused_inbound_pending_count` should be a push event in
    a future iteration — defer).
  - 0016 (LLM deploy-watch dashboard — its `health_flags: alert`
    composes with this seam).
