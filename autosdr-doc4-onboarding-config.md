# AutoSDR — Onboarding & Config Spec

**Status**: Draft  
**Version**: 0.3  
**Last Updated**: 2026-04-18  
**Depends on**: Doc 1 — Product Overview, Doc 2 — Data Architecture Spec,
               Doc 3 — AI & Messaging Spec

---

## 1. Overview

This document defines the full setup journey for a first-time AutoSDR user — from
cloning the repo to sending their first outreach message. It also covers ongoing
configuration: updating workspace settings, recalibrating tone, managing API keys,
and the PWA install flow that enables Web Push notifications.

The setup journey is designed to be completable in under 15 minutes by a non-developer
who can follow a README and paste API keys.

**Two onboarding tiers are defined:**

- **POC mode** (§3.0 below) — CLI-driven, minimal, for the single-user local setup
  that proves the system works. No frontend, no swipe flow, no PWA.
- **v1 mode** (§3.1 onwards) — the full 6-step wizard with business extraction,
  swipe tone calibration, and PWA install.

The POC is shipping first. v1 is the target state once the AI loop is proven.

---

## 2. Self-Hosted Architecture Requirements

AutoSDR is self-hosted. The owner runs the application on their own infrastructure.

**POC mode requirements:**

| Service | Minimum Spec | Notes |
|---|---|---|
| Python backend (FastAPI + scheduler) | Python 3.11+; 512MB RAM | Single process; `autosdr run` |
| SQLite | File on disk | Zero setup; SQLAlchemy abstracts the type |
| Android SMS gateway | One Android phone running **TextBee** *or* **SmsGate** (`capcom6/android-sms-gateway`) | TextBee = hosted, API-key only, poll-based; SmsGate = self-hosted, Docker + LAN, push-based. Either works end-to-end without a public URL |
| LLM API key | Google Gemini API key (free tier works) | Configured via `GEMINI_API_KEY` env var |

**v1 mode additions:**

| Service | Minimum Spec |
|---|---|
| Next.js frontend | Node.js 18+; built as static PWA |
| PostgreSQL | v14+; can be local or managed (e.g. Supabase free tier) |
| Redis | v7+; can be local or managed (e.g. Upstash free tier) |
| Webhook tunnel | ngrok or Cloudflare Tunnel | Only needed if v1 opts into TextBee webhook push at scale or exposes SmsGate to a non-LAN network |

A `docker-compose.yml` for the v1 stack is provided that spins up all services
locally with a single command. Cloud deployment docs (Fly.io, Railway) are
provided as supplementary guides.

**Why no tunnel is needed for the POC:**

TextBee's REST API exposes `GET /api/v1/gateway/devices/{device_id}/get-received-sms`
which lists unread inbound messages and authenticates with just the account
API key. The scheduler calls this every `INBOUND_POLL_S` seconds (default 20 s)
so replies flow back in without exposing a public endpoint.

SmsGate takes a different route to the same outcome: the Android device POSTs
`sms:received` webhooks to a LAN address (the Docker server, running on the
same machine as AutoSDR), hitting `POST /api/webhooks/sms` directly. Because
both AutoSDR and the gateway live on the owner's LAN, no public tunnel is
required either. The push path is on by default; `parse_webhook` normalises
payloads into the same `IncomingMessage` shape the poller produces.

---

## 3.0 POC Setup Flow (CLI)

The POC replaces the frontend wizard with a short sequence of CLI commands. A
fresh clone to first outbound SMS looks like:

```bash
# 1. Install
git clone <repo> AutoSDR && cd AutoSDR
python -m venv .venv && source .venv/bin/activate
pip install -e .

# 2. Configure env
cp .env.example .env
# Fill in at minimum:
#   GEMINI_API_KEY=...        (from https://aistudio.google.com/app/apikey)
#
# Pick ONE of the three connectors:
#
#   a) File (dev / no phone / dry-runs):
#      CONNECTOR=file
#
#   b) TextBee (hosted, API key only):
#      CONNECTOR=textbee
#      TEXTBEE_API_KEY=...       (from https://textbee.dev/dashboard)
#      TEXTBEE_DEVICE_ID=...     (from the dashboard after registering device)
#
#   c) SmsGate (self-hosted via capcom6/android-sms-gateway):
#      CONNECTOR=smsgate
#      SMSGATE_API_URL=http://localhost:3000/api/3rdparty/v1
#      SMSGATE_USERNAME=...      (from the SmsGate server dashboard)
#      SMSGATE_PASSWORD=...

# 3. Initialise the workspace (business info + tone, one shot)
autosdr init \
  --business-dump "We run a staffing platform that helps aged care facilities reduce agency spend and weekend understaffing. We onboard in under a week. We charge per shift filled, not a subscription." \
  --tone "Casual and direct. One idea per sentence. No corporate language. Open with a grounded observation about the recipient, close with a single low-pressure question. A touch of dry humour is fine."

# 4. Import leads
autosdr import path/to/leads.json         # NDJSON (one object per line)
# or
autosdr import path/to/leads.csv          # CSV

# 5. Create and activate a campaign
autosdr campaign create \
  --name "Aged Care Pilot" \
  --goal "Book a 15-minute call to walk through the staffing platform" \
  --per-day 20
autosdr campaign assign <campaign-id> --all-unassigned
autosdr campaign activate <campaign-id>

# 6. Smoke-test the SMS path (sends to your own number via the gateway)
autosdr test sms --to "+614..."

# 7. Run
autosdr run
# → starts webhook server on :8000 (healthz + sim + /api/webhooks/sms)
# → starts outreach scheduler (every 60s by default)
# → starts inbound poller (TextBee every 20s; no-op for SmsGate push)
# → streams pipeline logs to stdout and to data/logs/autosdr.log
# → Ctrl+C for graceful shutdown
#
# Rehearsing a run before pointing at real leads:
#
#   autosdr run --dry-run
#     All outbound goes to data/outbox.jsonl regardless of CONNECTOR.
#     LLM calls still run against your real Gemini key, so prompts / tone /
#     classification are fully exercised.
#
#   autosdr run --override-to "+614XXXXXXXX"
#     Uses the real connector (TextBee / SmsGate) but redirects every
#     outgoing SMS to the override number. Inbound replies from that
#     number are remapped back to the lead the last outbound targeted,
#     so the thread history stays coherent.
#
#   autosdr run --dry-run --override-to "+614XXXXXXXX"
#     OverrideConnector wraps the FileConnector. Useful for checking the
#     override rewiring itself without any real SMS traffic.
#
# Same flags work on the SMS smoke test:
#
#   autosdr test sms --to "+614..." --dry-run       # file-write only
#   autosdr test sms --to "+614..." --override-to …  # redirect the test SMS
```

**Reviewing what the AI did (key POC workflow):**

```bash
# Last 20 LLM calls across all threads, in a compact table
autosdr logs llm

# Full system + user + response for the last 5 generation calls
autosdr logs llm --purpose generation --tail 5 --show-prompts

# One thread end-to-end: every message and every LLM call, chronological
autosdr logs thread <thread-id> --show-prompts

# Only failed calls (e.g. parse failures, rate-limit retries exhausted)
autosdr logs llm --errors
```
The same records live in `data/logs/llm-YYYYMMDD.jsonl` for grep/jq use.

**What POC mode skips vs v1:**
- No environment-check wizard (CLI surfaces missing env vars at `autosdr run`).
- No LLM picker UI (Gemini by default; swap via env vars).
- No business-data extraction agent (the `business_dump` is used directly; a
  `business_data` JSON can still be set manually if the owner wants structured
  extraction).
- No swipe-based tone calibration (owner writes the tone as text via
  `--tone` on `autosdr init`).
- No PWA install, no Web Push, no VAPID (HITL surfaces as CLI log lines and in
  `autosdr hitl list`).

**Why this is enough:** everything skipped is a UI concern. The AI loop being
proved — analyse / generate / evaluate / classify / reply — is identical in both
tiers.

---

## 3.1 v1 First-Time Setup Flow (Frontend Wizard)

The v1 setup flow is a linear wizard in the frontend. It cannot be skipped or
completed out of order. Once complete, the owner lands on the main dashboard.

```
Step 1: Environment check
Step 2: LLM configuration
Step 3: SMS connector setup
Step 4: Business information
Step 5: Tone calibration
Step 6: Complete — create first campaign
```

### Step 1 — Environment Check

The frontend calls a backend health endpoint that verifies all required services
are reachable before proceeding.

**Checks performed:**
- PostgreSQL connection: can connect and run a test query
- Redis connection: can set and retrieve a test key
- LLM API: not checked yet (API key entered in Step 2)
- SMS gateway: not checked yet (configured in Step 3)

Any failing check surfaces an inline error with a plain-English description and a
link to the relevant section of the README. Setup cannot proceed past Step 1 until
all checks pass.

---

### Step 2 — LLM Configuration

The owner selects their LLM provider and enters their API key. AutoSDR then runs a
test generation call to confirm the key works before saving it.

**Provider options presented in the UI:**

| Option | Notes |
|---|---|
| OpenAI | API key from platform.openai.com; recommended for most users |
| Anthropic | API key from console.anthropic.com |
| Local (Ollama) | Ollama must be running locally; owner selects from available models |
| Other (OpenAI-compatible) | For any provider with an OpenAI-compatible API; owner enters base URL + key |

**Fields collected:**
- Provider selection
- API key (or base URL + key for custom)
- Model name (pre-filled with sensible default per provider; editable)
- Eval model name (defaults to same as main model; editable)

**Test call:** A short system prompt asking the model to respond with the word
"ready" is sent. If the response contains "ready", the check passes. Failure shows
the raw error from LiteLLM so the owner can diagnose (e.g. invalid key, rate limit,
model not found).

**Storage:** API keys are stored in the backend's environment variables (`.env`),
not in the database. The frontend never receives or displays the key after initial
entry — only a masked confirmation (e.g. `sk-...ab3f`) is shown in settings.

---

### Step 3 — SMS Connector Setup

The owner sets up their Android SMS gateway. Two shipping connectors are
supported side-by-side — **TextBee** (hosted, poll-based, API-key only) and
**SmsGate** (self-hosted via `capcom6/android-sms-gateway`, Docker + LAN,
push-based). Both operate end-to-end without a public URL for the POC. The
UI lets the owner pick one; the `BaseConnector` interface means everything
downstream (campaigns, threads, classification) is identical.

**Instructions presented in the UI (TextBee path — hosted default):**

1. Install the TextBee app on an Android phone from textbee.dev.
2. Sign in on the TextBee dashboard, generate an API key, and register the device.
3. Paste the API key into the "TextBee API Key" field below.
4. Paste the device ID (shown on the dashboard next to the registered device)
   into the "Device ID" field.
5. No webhook / tunnel / public URL is required. AutoSDR polls TextBee every
   `INBOUND_POLL_S` seconds for new inbound SMS.

**Instructions presented in the UI (SmsGate path — self-hosted):**

1. Start the SmsGate server on the owner's machine:

   ```bash
   docker run -d --name sms-gateway-server \
     -p 3000:3000 capcom6/sms-gateway-server
   ```

2. Install the SmsGate app on an Android phone from Play Store / F-Droid /
   the project's releases.
3. In the app, choose **Private server** mode and point it at
   `http://<owner-machine-ip>:3000`. Confirm the phone appears in the
   server's dashboard (http://localhost:3000).
4. Create a user in the SmsGate server dashboard; copy the username and
   password into the "SmsGate Username" and "SmsGate Password" fields below.
5. Leave "SmsGate API URL" on its default (`http://localhost:3000/api/3rdparty/v1`)
   unless the server is running somewhere else on the LAN.
6. No tunnel is required — SmsGate pushes `sms:received` webhooks to
   AutoSDR's `/api/webhooks/sms` endpoint directly over the LAN.

**Instructions presented in the UI (httpSMS path — advanced, optional):**

1. Install the httpSMS app on an Android phone from httpsms.com.
2. Open the app and sign in to get an API key.
3. Paste the API key into the field below.
4. Expose AutoSDR's webhook endpoint via a tunnel (e.g. `ngrok http 8000`) and
   paste the resulting public URL into the "Public URL" field; the UI builds the
   full webhook URL and guides the owner to register it with httpSMS.

**Connectivity test:** After the owner completes the gateway setup, a "Send test
SMS" button is shown. The owner enters their own phone number; the system sends a
short test message via the selected connector. The owner confirms they received
it. This confirms both outbound sending and that the gateway app is correctly
installed.

**Incoming test:** After confirming the test SMS, the UI prompts the owner to
reply to the test message from their phone. For TextBee the backend polls the
gateway; for SmsGate the phone pushes the reply as a webhook. In both cases the
UI surfaces the inbound message with a green confirmation, confirming the full
inbound path without any public tunnel.

**Dry-run rehearsal (CLI only, optional):** In POC mode an owner can rehearse
the end-to-end flow before touching a gateway by running
`autosdr run --dry-run` — the LLM pipeline runs against real Gemini credentials
but every outbound SMS is written to `data/outbox.jsonl`, giving a preview of
exactly what the agent *would* have sent. This complements, rather than
replaces, the real connectivity test above.

---

### Step 4 — Business Information

The owner provides context about their business. This is used in every outreach
generation call to inform the angle and the message.

**Input method:** A large free-text area labelled "Tell us about your business".
Guidance copy explains what to include:

```
Paste or type anything about your business: what you do, who you help, what
problems you solve, your pricing (rough is fine), what makes you different,
any common questions or objections you hear. The more you include, the better
the AI will understand your offering.

You can paste from your website About page, a pitch deck, an email signature,
or just write it freehand. Minimum 100 words.
```

**Minimum length:** 100 words. A live word count is shown. The Next button is
disabled below the minimum.

**LLM extraction:** On submit, the backend sends the dump to the analysis agent
which extracts key structured fields into `workspace.business_data`:

```json
{
  "business_name": "...",
  "services": ["...", "..."],
  "target_customer": "...",
  "key_benefits": ["...", "..."],
  "location": "...",
  "pricing_signals": "...",
  "usp": "..."
}
```

The extraction result is shown to the owner as a summary card: "Here's what we
understood about your business." Each extracted field is editable inline. The owner
confirms or adjusts before proceeding.

Both `business_dump` (verbatim) and `business_data` (extracted) are saved to the
workspace.

---

### Step 5 — Tone Calibration

The owner completes the swipe-based calibration flow described in Doc 3 Section 3.

**UI behaviour:**
- Cards are presented one pair at a time, full-screen on mobile.
- Left card = one style; right card = another style.
- Swipe right (or tap right card) = preferred. Swipe left (or tap left card) =
  rejected.
- A progress indicator shows how many decisions have been made. The minimum of 10
  is marked clearly; the owner can continue beyond 10 for a stronger result.
- A "Skip this pair" option is available if neither feels right. Skipped pairs are
  not counted toward the 10-decision minimum.
- A "Finish" button appears after 10 decisions. The owner can tap it at any point
  after that.

**Sample pair generation:** The backend generates 20 pairs before the flow starts,
using the `business_data` from Step 4 to make the samples feel relevant. All 20
are generated upfront so there is no loading between cards.

**Compilation:** On "Finish", the decisions are sent to the tone compilation prompt
(Doc 3 Section 3.2). The resulting `tone_prompt` is shown to the owner as a
readable summary. They can accept it, regenerate (reruns the compilation with the
same decisions), or go back and add more swipe decisions.

---

### Step 6 — Complete

A confirmation screen summarises what was set up. The owner is prompted to:

1. **Install the PWA** — see Section 5.
2. **Create their first campaign** — a shortcut button opens the new campaign modal.

Setup is marked complete. The owner lands on the main dashboard on all future visits.

---

## 4. Campaign Creation

Campaigns are created from the dashboard at any time after setup is complete.

**Fields in the new campaign modal:**

| Field | Type | Notes |
|---|---|---|
| Name | Text | Internal label only; not shown to leads |
| Goal | Text (long) | e.g. "Book a 15-minute discovery call". Required. |
| Outreach per day | Integer | Default: 50. Min: 1. Max: 500. |
| Connector | Select | MVP: Android SMS only |
| Lead source | File upload or existing list | CSV / JSON upload, or select a previously imported list |

On save, the campaign is created in `draft` status. The owner reviews the lead
mapping (if a new file was uploaded) and then sets the campaign to `active` to
begin outreach.

---

## 5. PWA Installation

**v1 only.** The POC has no frontend and therefore no PWA — HITL events surface
as CLI log lines and in `autosdr hitl list`. The section below describes the v1
target state.

AutoSDR is built as a Next.js PWA using `next-pwa`. The manifest and service worker
are generated at build time.

**Manifest configuration:**
```json
{
  "name": "AutoSDR",
  "short_name": "AutoSDR",
  "start_url": "/",
  "display": "standalone",
  "background_color": "#ffffff",
  "theme_color": "#000000",
  "icons": [...]
}
```

**Install prompt:** After Step 6 of setup, the frontend checks if the app is
already installed (`window.matchMedia('(display-mode: standalone)').matches`). If
not, it shows an install prompt with platform-specific instructions:

| Platform | Instructions shown |
|---|---|
| Chrome (desktop) | "Click the install icon in the address bar" with a screenshot |
| Chrome (Android) | "Tap the menu → Add to Home Screen" |
| Safari (iOS) | "Tap the Share icon → Add to Home Screen" |
| Firefox | "AutoSDR works best when installed — use Chrome or Edge for the install prompt" |

The install prompt can be dismissed and recalled from the Settings page at any time.

**Web Push setup:** Push notifications require the owner to grant permission. A
permission request is triggered immediately after the PWA is installed (or on first
dashboard load if already installed). If permission is denied, a persistent banner
in the dashboard explains that HITL notifications will not work and provides a
button to re-request permission.

**VAPID keys:** The backend generates a VAPID key pair on first startup and stores
it in the environment. The public key is served via an API endpoint consumed by the
service worker during push subscription registration.

---

## 6. Ongoing Configuration

All workspace settings are accessible from the Settings page after setup.

### 6.1 Business Information

The owner can update the business dump at any time. On save, the extraction agent
re-runs and `business_data` is updated. Existing threads are not affected — they
use the `tone_snapshot` stored at thread creation time (see Doc 3 Section 9).

### 6.2 Tone Recalibration

The owner can re-run the full swipe flow from Settings → Tone. On completion, the
new `tone_prompt` overwrites the old one. A confirmation dialog warns: "Existing
conversations will continue using the previous tone. New conversations will use the
updated tone."

### 6.3 API Key Rotation

**POC:** API keys live in `.env`. To rotate, edit `.env` and restart
`autosdr run`. Hot-reload is out of scope for POC.

**v1:** LLM and SMS gateway API keys can be updated from Settings →
Integrations. Each key field has a "Test connection" button that re-runs the
relevant connectivity check from setup. The key is updated in the environment
and the backend is instructed to reload it without a full restart.

### 6.4 Campaign Settings

`goal` and `outreach_per_day` on any campaign can be updated at any time from the
campaign detail page. Changes take effect on the next cron cycle. Pausing and
resuming campaigns is done via the status controls on the campaign card in the
dashboard.

### 6.5 Default Settings

The following workspace-level defaults can be adjusted from Settings → Advanced:

| Setting | Default | Notes |
|---|---|---|
| `max_auto_replies` | 5 | Maximum AI replies per thread before HITL escalation |
| `eval_threshold` | 0.85 | Minimum self-evaluation score for a message to be sent |
| `eval_max_attempts` | 3 | Maximum generation attempts before HITL escalation |
| `raw_data_size_limit_kb` | 50 | Maximum raw_data size sent to analysis agent |

---

## 7. Unmatched Webhook Handling

Two cases route to `unmatched_webhook`:

1. **Sender not known** — the SMS arrives from a number that does not match any
   lead's `contact_uri` (after E.164 normalisation).
2. **Known lead but no active thread** — the sender matches a lead but all of
   that lead's threads have been closed (`won` / `lost` / `skipped`) or no
   thread exists yet (the reply arrived before outreach was sent).

In both cases, the system:

1. Writes the payload to the `unmatched_webhook` table (see schema below).
2. Takes no further action. No notification is sent to the owner in the POC; v1
   may surface a badge count in the dashboard.
3. The record is retained for 90 days then purged.

```sql
CREATE TABLE unmatched_webhook (
  id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  workspace_id  UUID NOT NULL REFERENCES workspace(id),
  connector_type TEXT NOT NULL,
  sender_uri    TEXT,              -- phone number or email of the unmatched sender
  raw_payload   JSONB NOT NULL,    -- full webhook payload
  received_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_unmatched_webhook_workspace ON unmatched_webhook(workspace_id, received_at);
```

This handles internal team SMS traffic that may transit the same Android phone used
as the gateway, without surfacing noise in the owner's dashboard.

---

## 8. Setup Completion Checklist

Checks are run at the top of every scheduler tick. If any required item is
missing, the tick is skipped and a warning is printed (POC) or surfaced in the
dashboard (v1).

**POC checklist:**

- [ ] SQLite database file exists and schema is initialised (`autosdr init`)
- [ ] `GEMINI_API_KEY` (or the configured provider's key) is set
- [ ] `CONNECTOR` env var is set (`textbee` / `smsgate` for production; `file` for dev)
- [ ] If `CONNECTOR=textbee`: `TEXTBEE_API_KEY` and `TEXTBEE_DEVICE_ID` are set
      and `autosdr test sms` passed
- [ ] If `CONNECTOR=smsgate`: `SMSGATE_API_URL`, `SMSGATE_USERNAME`, and
      `SMSGATE_PASSWORD` are set, the SmsGate server is reachable (its dashboard
      shows the paired Android device online), and `autosdr test sms` passed
- [ ] If rehearsing: `--dry-run` and/or `--override-to <number>` are used
      intentionally and will not ship to real leads
- [ ] `workspace.business_dump` and `workspace.tone_prompt` are set
- [ ] At least one `active` campaign exists with at least one `queued` lead
- [ ] Kill-switch flag file (`data/.autosdr-pause`) is absent
- [ ] `data/logs/` directory is writeable (LLM call log + pipeline log)

**v1 additions:**

- [ ] PostgreSQL and Redis are reachable
- [ ] LLM API key test call succeeded
- [ ] Android SMS gateway two-way test passed
- [ ] Business information is entered and extraction confirmed
- [ ] Tone calibration is complete and `tone_prompt` is saved

---

## 8.5 System-Wide Kill Switch

The POC ships with the three-layer kill switch defined in Doc 1 §9. Summary of
operational commands:

| Action | Command | Effect |
|---|---|---|
| Pause immediately | `autosdr pause` | Creates `data/.autosdr-pause`; all processing halts within 1s |
| Resume | `autosdr resume` | Removes the flag file; next scheduler tick picks up normally |
| Graceful stop | `autosdr stop` (or `Ctrl+C` in the `autosdr run` terminal) | Sends SIGTERM; in-flight work drains up to 10s; process exits |
| Check state | `autosdr status` | Reports: paused Y/N, last tick, in-flight LLM calls, quota usage, LLM spend |

**What pause guarantees:**
- No new connector send after pause trips.
- No new LLM call dispatched after pause trips.
- Webhook endpoints still return 202 (so the SMS gateway does not retry); the
  payload is acknowledged but processing is dropped.
- State is consistent — pause is checked *before* DB writes that would advance
  state, so nothing is half-written.

**What pause does not guarantee:**
- An LLM call already dispatched to the provider completes server-side; its
  result is discarded but tokens are still billed.
- An SMS send already in-flight to the gateway completes; its result is recorded
  normally in the `message` table.

The pause-flag path is configurable via `PAUSE_FLAG_PATH` in the env (default
`data/.autosdr-pause`).

---

## 9. Document Map

| Doc | Title | Status |
|---|---|---|
| Doc 1 | Product Overview | Draft |
| Doc 2 | Data Architecture Spec | Draft |
| Doc 3 | AI & Messaging Spec | Draft |
| Doc 4 | **Onboarding & Config Spec** (this document) | Draft |
