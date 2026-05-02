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

- ~~`vite-plugin-pwa` vs hand-rolled SW.~~ — resolved 2026-05-02.
- ~~Subscription colocated with workspace settings vs dedicated table.~~ — resolved 2026-05-02.
- ~~Notification click URL.~~ — resolved 2026-05-02.
- ~~Per-event filtering granularity for v0.~~ — resolved 2026-05-02.
- ~~Soft-delete vs hard-delete on `DELETE /api/push/subscribe`.~~ — resolved 2026-05-02.
- ~~Telemetry: record push delivery to disk?~~ — resolved 2026-05-02.
- ~~**OQ-Net1.** Tailscale-detect warning vs quiet doc-only.~~ — resolved 2026-05-02.
- ~~**OQ-Net2.** SMSGate guidance.~~ — resolved 2026-05-02.
- ~~**OQ-Net3.** `dashboard_origin` resolution strategy.~~ — resolved 2026-05-02.

## Resolved questions (2026-05-02)

### Resolved: vite-plugin-pwa vs hand-rolled SW

**Architect:** `vite-plugin-pwa` (Workbox-based). Saves a week of SW
boilerplate, supports manifest + precache out of the box, integrates
with the existing Vite build.
**Skeptic:** Plugin lock-in is real — Workbox dictates the SW shape.
Mitigation: we own a tiny custom-SW stub that imports the
plugin-generated cache wiring and adds our own `push` /
`notificationclick` handlers. Plugin handles cache; we handle push.
**Pragmatist:** Plugin. Bundle cost is < 5 KB gzipped (plugin emits the
SW; consumer code is a tiny `registerSW` call).
**Critic:** Plugin. Hand-rolled SW means owning HTTP cache invalidation
forever — that's a bug-magnet for a single-operator team.

**Decision:** `vite-plugin-pwa` with `injectManifest` strategy so we
own a custom SW file (`frontend/src/sw/sw.ts`) that imports Workbox
precache wiring and adds bespoke `push` + `notificationclick` handlers.
**Strongest dissent:** Skeptic's lock-in concern. Acceptable because
the SW file is < 100 LoC and could be rewritten in a day if the plugin
ever becomes a problem.
**Confidence:** high.

### Resolved: subscription storage shape

**Decision:** Dedicated `push_subscription` table — accept the ticket's
recommended path. Workspace-settings JSON would conflate device-state
mutations with config writes; a real table makes per-sub last-seen /
last-error trivial. Confidence: high.

### Resolved: notification click URL

**Decision:** `/inbox/<thread_id>` (path segment, not query). The
mobile master-detail collapse from ticket 0015 routes by path
(`/inbox/:threadId`), so deep-links must be a path. Confidence: high.

### Resolved: per-event filter v0

**Decision:** HITL escalation only. Adding more events is one extra
seam each; ship the seam, gate with `workspace.settings.push.hitl_escalations` (default
`true`). Send-failure / quota-exhausted are tracker entries on the
roadmap as cheap follow-ups. Confidence: high.

### Resolved: subscription delete strategy

**Decision:** Hard-delete on `DELETE /api/push/subscribe`. Nothing
joins on push_subscription rows; a tombstone would be dead weight.
Dead subs (HTTP 410 from the push gateway) are auto-pruned by the
transport. Confidence: high.

### Resolved: push-event telemetry

**Decision:** Defer. Log every push attempt to `logging.info` /
`logging.warning` (the existing log dir captures these for ad-hoc
grep). A `push_event` table is filed as a follow-up ticket if "did
this fire?" becomes a real ops question. Confidence: medium — the
ticket explicitly allows this deferral.

### Resolved: OQ-Net1 — Tailscale-detect warning

**Architect:** Warn on `HOST=127.0.0.1` if `tailscale status` exits 0,
*and* surface the bind state on a Settings → Networking card. Doc-only
loses to operator-visible; the validator runs at startup so the
warning lands in the log next to the boot banner.
**Skeptic:** Detection via shelling out to `tailscale status` is
fragile (PATH issues, sandboxes, Windows). Mitigation: best-effort
detection — if probing fails, *don't* warn (no false positives), and
the Settings card shows "Tailscale: not detected" so the operator knows
the probe ran.
**Pragmatist:** Warn + card. The footgun is real ("I followed the docs
and the phone can't connect" is the canonical failure).
**Critic:** Warn + card, but the warn must be informational, not
blocking — never refuse to start because Tailscale is detected.

**Decision:** Best-effort detection (`tailscale status` exit code,
2-second timeout, never block startup); log a `WARNING` if HOST is
127.0.0.1 and Tailscale is detected; surface bind state +
detected-Tailscale state on Settings → Networking. Never refuse to
boot. Confidence: high.

### Resolved: OQ-Net2 — SMSGate guidance

**Decision:** Ship both modes, default-recommend Local Server inside
the tailnet in the README (one tool, fewer third parties); document
Cloud Server as the escape hatch. Connector code already supports
both — only the operator-facing copy changes. Confidence: high.

### Resolved: OQ-Net3 — dashboard_origin resolution

**Decision:** Default from request `Host` header at subscription time;
operator override via `workspace.settings.push.dashboard_origin`. The
setting overrides if non-empty; otherwise the SW reads the Host header
the API saw at subscribe-time and uses that as the deep-link origin.
Same-origin is the common case. Confidence: high.

## Mini plan (2026-05-02)

Risk-first sequence. Schema before consumers; transport before the
hot-path hook; each unit ships with the test it needs.

| # | Unit | Files | Change class | Tests | Depends on | Risk |
|---|------|-------|--------------|-------|------------|------|
| 1 | `push_subscription` model + additive migration | `autosdr/models.py`, `autosdr/db.py` | invasive (schema add) | new `tests/test_push_subscription_model.py` | — | high |
| 2 | `PushConfig` block on workspace settings (vapid keys, hitl_escalations, dashboard_origin); first-run keygen via `cryptography` | `autosdr/workspace_settings.py`, `autosdr/api/schemas.py`, `autosdr/api/workspace.py`, `pyproject.toml` (`cryptography` already vendored — confirm) | additive | extend `tests/test_workspace_settings*.py` | unit 1 | med |
| 3 | `/api/push/*` routes + Pydantic schemas | `autosdr/api/push.py` (new), `autosdr/api/__init__.py`, `autosdr/api/schemas.py` | additive | `tests/test_push_subscriptions.py` | unit 2 | med |
| 4 | `pywebpush` transport + dead-sub cleanup + killswitch guard, `asyncio.to_thread` | `autosdr/push.py` (new), `pyproject.toml` (`pywebpush`) | additive | new `tests/test_push_transport.py` (mocked `webpush`) | unit 3 | med |
| 5 | `pause_thread_for_hitl` fires push (privacy-strict payload) | `autosdr/pipeline/_shared.py` | invasive (HITL hot path) | new `tests/test_push_payload_privacy.py` + extend `tests/test_hitl_dismiss.py` (or new `tests/test_pause_thread_for_hitl_push.py`) | unit 4 | high |
| 6 | `vite-plugin-pwa` + manifest + SW skeleton + `registerSW` in `main.tsx`; PWA icons | `frontend/vite.config.ts`, `frontend/package.json`, `frontend/index.html`, `frontend/src/main.tsx`, `frontend/src/sw/sw.ts` (new), `frontend/public/icon-192.png` + `frontend/public/icon-512.png` (new) | additive | manual smoke (`npm run build` + `python -m autosdr.webhook`) | — | med |
| 7 | SW `push` + `notificationclick` handlers + Settings → Notifications card | `frontend/src/sw/sw.ts`, `frontend/src/lib/push.ts` (new), `frontend/src/lib/api.ts` (push methods), `frontend/src/lib/types.ts`, `frontend/src/routes/settings/NotificationsCard.tsx` (new), `frontend/src/routes/Settings.tsx` | additive | manual smoke + frontend `tsc -b --noEmit` | units 3 + 6 | med |
| 8 | Tailscale-detect best-effort warning + Settings → Networking card | `autosdr/networking.py` (new), `autosdr/webhook.py` (lifespan probe), `autosdr/api/status.py` (expose `networking`), `frontend/src/routes/settings/NetworkingCard.tsx` (new), `frontend/src/routes/Settings.tsx` | additive | new `tests/test_networking_probe.py` (mocked subprocess) | unit 7 | low |
| 9 | README *Remote access* walk-through (no PC install on this machine) + ARCHITECTURE update | `README.md`, `ARCHITECTURE.md` | docs | n/a | units 1-8 | low |
| 10 | `tsc -b --noEmit` + `vite build` + full backend `pytest` clean | n/a | verification | tsc + build + pytest | units 1-9 | low |

**Sequencing rationale:** Unit 5 is the highest-risk single unit
(invasive change to a hot path that already had a transaction-across-await
bug fixed in 0008). Units 1-4 are the *prerequisites* that have to land
in order — schema → settings → API → transport — so unit 5 can then
plug in. Unit 6 is the parallel frontend foundation; it can land at
any point once the API surface from unit 3 is stable. Unit 7 closes
the loop. Unit 8 is the loud-failure-mode safeguard the council
mandated. Units 9-10 are the documentation + verification gate.

**Map back to Scope:**
- *Service worker + manifest* → units 6 + 7.
- *VAPID keys* → unit 2.
- *New `push_subscription` table* → unit 1.
- *Endpoints* → unit 3.
- *Push transport (privacy-strict payload)* → units 4 + 5.
- *Hook into HITL escalation* → unit 5.
- *Settings → Notifications card* → unit 7.
- *Killswitch coverage* → unit 4 (guarded inside the transport).
- *README + Settings → Networking copy* → units 8 + 9.

**Map back to Success criteria:**
- *`tests/test_push_subscriptions.py` covers subscribe / unsubscribe /
  test-fire* → unit 3.
- *Pause-for-HITL triggers a push attempt; failed sends mark dead but
  don't crash* → units 4 + 5.
- *`tests/test_push_payload_privacy.py` asserts the privacy-strict
  payload* → unit 5.
- *`tests/test_dashboard_origin_resolution.py` covers the origin
  defaulting* → unit 3 (resolution is in the API surface).
- *Manual mobile smoke (cellular + Tailscale + push + tap-into-thread)*
  → operator-side smoke, deferred per the user's "no Tailscale on this
  PC" constraint. Units 1-8 leave the system **smoke-ready**; the
  end-to-end verification happens on the home PC (see *Operator-side
  verification* below in the implementation log).
- *Toggle notifications off/on* → unit 7 (Settings card).
- *Permission-revoked subscription ends up dead, not retried forever*
  → unit 4 (HTTP 410 → hard-delete).
- *README Remote-access walk-through + Settings → Networking card* →
  units 8 + 9.

**Operator-side verification (this PC: work PC, no Tailscale install):**

The user-mandated constraint is *"don't install any tailscale tunneling
on this PC. It's a work PC and I can't. That will be done on my home
PC at a later date."* Implementation respects this in two ways:

1. **No Tailscale CLI is invoked from this session.** The startup
   probe under `autosdr/networking.py` is unit-tested with a mocked
   `subprocess.run`; the real binary is never executed here.
2. **The end-to-end mobile smoke is the operator's, not the
   implementer's.** Units 1-8 leave the system in a state where the
   smoke can be executed verbatim from the README on the home PC. The
   smoke checklist in unit 9's README section is the
   reproducible artefact; this implementation log will tick the
   *"smoke-ready"* state, not the *"smoke-passed"* state.

**Why this is acceptable:** the rest of the ticket — schema,
transport, hot-path hook, privacy posture, payload contract,
killswitch coverage, deep-link resolution, frontend SW + UI, networking
card — is fully testable on this PC via `pytest` + `tsc -b` +
`vite build` + a mocked push gateway. The only thing that *requires*
Tailscale is asserting the *"phone-on-cellular notification arrives
within 10s"* timing, and the architectural decision to route via
browser-vendor push gateways means there's no AutoSDR-side latency
contribution beyond the one HTTP call we already test in unit 4.

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

## Implementation log (2026-05-02)

**Status:** done (server-side + frontend; end-to-end mobile smoke
deferred to operator's home PC — see *Operator-side verification*).

| # | Unit | Outcome | Evidence |
|---|------|---------|----------|
| 1 | `push_subscription` model + indexes | done | `tests/test_push_subscription_model.py` (3/3 passing) |
| 2 | VAPID keygen + `workspace.settings.push` block + lifespan call | done | `tests/test_push_vapid_lifecycle.py` (4/4 passing); lifespan invocation at `autosdr/webhook.py:194` |
| 3 | `/api/push/{vapid-public, subscribe, subscriptions, test}` routes + DELETE | done | `tests/test_push_subscriptions_api.py` (10/10 passing) |
| 4 | `pywebpush` transport (sync + `asyncio.to_thread` fanout, killswitch-aware, dead-sub cleanup) | done | `tests/test_push_transport.py` (9/9 passing) |
| 5 | `schedule_hitl_push` seam wired into `pause_thread_for_hitl` (4 escalation sites: outreach×2, reply×2) | done | `tests/test_pause_thread_for_hitl_push.py` (3/3 passing); diff: `autosdr/pipeline/_shared.py` + `outreach.py:441,550` + `reply.py:780,856` |
| 6 | `vite-plugin-pwa` (injectManifest) + manifest + bespoke `frontend/src/sw/sw.ts` + `registerServiceWorker` shim | done | `vite build` emits `dist/manifest.webmanifest` + `dist/sw.js` (push, notificationclick, NetworkFirst /api/) — verified via `grep -o "notificationclick\|registration\.showNotification\|hitl-" dist/sw.js` returning all three. PNG icons generated via `@vite-pwa/assets-generator` from existing `public/icon.svg`. |
| 7 | Settings → Notifications card (subscribe/unsubscribe/test/HITL toggle/dashboard-origin override) | done | `frontend/src/routes/settings/NotificationsCard.tsx`; type-check + build clean. |
| 8 | `autosdr/networking.py` (best-effort `tailscale status` probe) + boot warning + `/api/status/networking` + Settings → Networking card | done | `tests/test_networking_probe.py` (9/9 passing); `frontend/src/routes/settings/NetworkingCard.tsx`; warning emitted at `autosdr/webhook.py:202` |
| 9 | README → "Remote access (use AutoSDR from your phone)" + ARCHITECTURE § 15 update + PATTERNS rows for vite-plugin-pwa, pywebpush, cryptography | done | `README.md:104-176`; `ARCHITECTURE.md:543-579`; `docs/PATTERNS.md` (3 new rows + 2 decisions-log entries) |
| 10 | Privacy + dashboard-origin success-criterion test files | done | `tests/test_push_payload_privacy.py` (5/5), `tests/test_dashboard_origin_resolution.py` (8/8) |

**Final state of success criteria:**

- ✓ `tests/test_push_subscriptions.py` (named `test_push_subscriptions_api.py` to match the existing `*_api.py` convention) covers subscribe / unsubscribe / test-fire endpoints; pywebpush is mocked. 10/10 passing.
- ✓ `tests/test_pause_thread_for_hitl.py` equivalent shipped as `test_pause_thread_for_hitl_push.py` (3/3 passing) — asserts paused-for-HITL events trigger a fan-out, that fanout failures are swallowed, and that sync-context callers no-op cleanly.
- ✓ `tests/test_push_payload_privacy.py` (5/5 passing) — pins the field set to `{title, body, thread_id, lead_first_name, hitl_reason, escalated_at, url}` and asserts last-name / message-content never leak.
- ✓ `tests/test_dashboard_origin_resolution.py` (8/8 passing) — covers SW endpoint, Settings list endpoint, and the server-side fanout resolver. Override > snapshot > Host > None ordering.
- ⚠ Manual smoke (mobile, on cellular, with PWA installed): **deferred** — see *Operator-side verification* below. The work PC constraint means the operator will run the full Tailscale-on-cellular smoke from their home PC at a later date. All server-side and code-path coverage is in.
- ✓ Operator can toggle notifications off / on without leaving the app — Settings → Notifications card unsubscribes via `pushManager.getSubscription()` + `unsubscribe()` then `DELETE /api/push/subscribe`; subscribe re-runs the dance.
- ✓ A device-side-revoked subscription ends up *gone* in the DB (HTTP 410 → hard-delete). Pinned by `test_fanout_hard_deletes_gone_subscriptions`.
- ✓ README has the *Remote access* walk-through; Settings → Networking card renders the detected dashboard origin and Tailscale state — both shipped, both type-checked, both build-clean.

**Principle check after implementation:**

- ✓ Simplicity first: net surface is one new module per concern (`autosdr.push`, `autosdr.networking`, `autosdr.api.push`) + one new SW + one settings card pair. No new framework, no new state-management lib, no new background-job system.
- ✓ Quality > breadth: test coverage 39 new tests across 6 files (+9 in networking probe) and every test names a contract a future change would want to break.
- ✓ Honest contracts: notification payload shape is named in code (`HitlPushPayload` dataclass), in docs (`PATTERNS.md` row), and in test (`tests/test_push_payload_privacy.py::EXPECTED_FIELDS`). No third place can drift.
- ✓ Extensible: HITL-only filter is a single boolean (`workspace.settings.push.hitl_escalations`); adding "send-failure" / "quota-exhausted" later is a new caller of `schedule_hitl_push` plus a sibling boolean.
- ✓ Human always wins: no auto-reply path was changed. Push is *additive notification on an existing HITL state transition* — flipping push off (toggle, killswitch, no-VAPID, no-subscriptions, missing event loop) leaves the HITL queue itself untouched.
- ✓ Owner control: every secret stays on the workspace row. VAPID private never crosses the API boundary. Operator override for `dashboard_origin` lives at `workspace.settings.push.dashboard_origin`, surfaced editable on Settings → Notifications. Tailscale probe is best-effort + read-only.

**Pattern-unifier diff scan:** ran against the staged diff. Three new
PATTERNS.md rows landed *before* the code that needed them
(`vite-plugin-pwa`, `pywebpush`, `cryptography`). No new ⚠/✗ rows
introduced; no existing blessed choice bypassed. The `axios`,
`requests`, `moment`, alternate-router, alternate-state-lib,
direct-LLM-SDK forbidden list still holds.

**Operator-side verification (deferred to home PC):** the full mobile-
on-cellular smoke (Tailscale install on PC + phone, `HOST=0.0.0.0`,
phone browser → tailnet hostname → Add to Home Screen → Settings →
Notifications → Enable → fire a real HITL event → notification
arrives < 10s) cannot run on the work PC where Tailscale install is
disallowed. Operator will run this from their home PC at a later
date. All other paths are covered by the test suite; the mobile
smoke is a confirmation, not a discovery.

**Follow-ups raised:**

- **Vulnerability-scanner noise from build-time deps** (`workbox-build`
  → `@rollup/plugin-terser` → `serialize-javascript`, `glob` → CLI
  command-injection). These run only during `vite build` and never
  process untrusted input, so they're not a runtime risk; flagged
  here so a future security-review ticket can tackle them as a
  group.
- **Vite 8 peer-dep mismatch** with `vite-plugin-pwa@1.2.0` (declared
  range tops out at Vite 7). Installed via `--legacy-peer-deps`;
  works in practice because the plugin uses the stable Rollup-plugin
  surface. Re-evaluate when `vite-plugin-pwa` ships a Vite-8-
  declaring release.
- **VAPID rotation story.** v0 ships keypair generated once and
  persisted forever; rotation requires re-subscribing every device.
  Documented in *Effort & risk*. Open as a follow-up when push gets
  more than a single-operator install.
- **Push-event telemetry table** (`push_event`). v0 logs to
  `logging.info`/`warning`. File a ticket if "did this fire?" becomes
  an ops question.
- **Send-failure / quota-exhausted push events.** Cheap follow-up;
  one new caller of `schedule_hitl_push` per event class plus a
  sibling boolean on `workspace.settings.push`.

**Open questions still unresolved:** (none)
