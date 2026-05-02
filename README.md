# AutoSDR

Autonomous SDR for small business owners. Self-hosted, open-source. You point
it at a lead list + a goal, and it sends the first outreach SMS for you. When
a lead replies, it drafts 2–3 candidate responses, scores them against your
tone, and parks the thread — you pick which one goes out (or write your own).
No auto-replies, no runaway spam. One human in the loop.

Single process: FastAPI serves the API *and* the built React UI from the same
port. SQLite for storage. Gemini (or any LiteLLM-supported provider) for the
language model. TextBee / SMSGate on an Android phone for SMS, or a local
file-backed connector for dev.

---

## What it does

1. Import a CSV / JSON lead list. Phones normalise to E.164; landlines and
   toll-free numbers are flagged so the scheduler never texts them.
2. For each queued lead, the pipeline analyses the raw record, picks a
   personalisation angle, drafts an SMS in your tone, and self-evaluates it
   against five criteria (tone, personalisation, goal, length, naturalness).
   Rewrites up to three times; escalates to HITL if it can't pass.
3. Sends via your configured connector.
4. When a lead replies, the classifier reads intent. Clear negatives and
   goal-reached conversations close automatically. Everything else pauses the
   thread, generates two or three AI-drafted candidate replies, and waits for
   you to pick one, edit one, or type your own.
5. Enforces a per-campaign daily send cap that resets at server-local midnight.
6. Persists every LLM call — prompts, response, tokens, latency, error — to
   the `llm_call` table and `data/logs/llm-YYYYMMDD.jsonl`, so you can audit
   and iterate on prompts after the fact.
7. Honours a kill switch: pause from the UI or a pause flag file (`PAUSE_FLAG_PATH`).

Configuration lives in the database (`workspace.settings`), not in `.env`.
The only environment variables the server reads are pure infrastructure:
`DATABASE_URL`, `HOST`, `PORT`, `PAUSE_FLAG_PATH`, `LOG_DIR`. Everything else — LLM keys,
model slugs, connector credentials, scheduler intervals, evaluator thresholds,
rehearsal mode — is set from the Settings page and hot-reloaded at runtime.

---

## Requirements

- Python 3.11+
- Node 20+ (to build the UI)
- A Google Gemini API key (free tier fine for the POC —
  <https://aistudio.google.com/app/apikey>)
- For real SMS: an Android phone running [TextBee](https://textbee.dev) or
  [SMSGate](https://sms-gate.app). No tunnel / public URL on the LAN path —
  the scheduler polls the gateway's REST API. (For phone-on-cellular ⇄
  PC-at-home, see [Remote access](#remote-access-use-autosdr-from-your-phone).)
- For local testing: nothing extra. The file connector writes outbound SMS
  to `data/outbox.jsonl`; use **Settings → Connector → Simulate inbound**
  (after saving `connector.type=file`) to drive the reply pipeline.

---

## Quickstart

```bash
git clone <this-repo> && cd AutoSDR

cd frontend && npm install && npm run build && cd ..
uv sync                          # or: pip install -e '.[dev]'

uv run uvicorn autosdr.webhook:app --host 127.0.0.1 --port 8000
```

Open <http://localhost:8000>. The app lands on `/setup` because no workspace
exists yet. Walk through the three-step wizard:

1. **Business** — name, a short description, your outreach tone.
2. **LLM** — paste your Gemini API key, pick a model (default
   `gemini/gemini-2.5-flash`).
3. **Connector** — pick `file` (dev), `textbee`, or `smsgate`. For TextBee
   you need the API key and device id; for SMSGate the endpoint + basic-auth
   credentials.

Submit. The server creates the workspace row, seeds the default settings,
wires up the connector, and drops you on the Dashboard. From there:

- **Leads → Import** — drag a CSV / JSON / NDJSON file in; preview shows what
  will import vs. skip; commit.
- **Campaigns → New campaign** — name, goal, sends-per-day. Save, then
  **Activate** and **Assign all eligible leads**.
- The scheduler starts sending on the next tick. Watch the Dashboard for
  outbound volume; reply-triggered threads land in **Inbox**.

Auto-reply is off by default — the "first-message-only" mode. You can flip it
back on from Settings → AI behaviour if you trust the loop, but the whole
product is designed around keeping a human on every reply.

---

## Simulating a reply (file connector)

With the file connector active (saved in **Settings**), there's no real SMS going out. To exercise
the reply pipeline, open **Settings → Connector** and use **Simulate inbound** after saving
connector type **File**, or call `POST /api/dev/sim-inbound` with `{ "contact_uri": "+614…", "content": "…" }`.

AutoSDR classifies the intent, generates candidate drafts, parks the thread
as "Needs you", and the UI's Inbox will surface it within a few seconds.

---

## Remote access (use AutoSDR from your phone)

By default AutoSDR binds to `127.0.0.1:8000`, which means *only* the PC
running it can reach the dashboard. That's the right default for a
laptop-only operator. If you want to triage HITL pushes (ticket 0005) from
your phone *while AutoSDR is at home and your phone is on cellular*, the
canonical pattern is:

1. **Install Tailscale on the PC and on the phone.** Sign in with the
   same account on both. Free for up to 100 devices —
   <https://tailscale.com/download>. Tailscale gives the PC a stable
   `100.x` address and a MagicDNS hostname like
   `autosdr-pc.tail-scale.ts.net`; the phone can reach it from any
   network as long as Tailscale is connected on both ends.
2. **Bind AutoSDR to all interfaces.** In your `.env` (next to
   `DATABASE_URL`), set `HOST=0.0.0.0` and restart uvicorn. Without
   this, FastAPI only listens on the loopback interface and the phone
   can't reach it even on the tailnet. Settings → Networking shows
   you whether you got this right; if AutoSDR is bound to localhost
   *and* Tailscale is up, it logs a warning and surfaces an amber
   chip.
3. **Open the dashboard from the phone.** In the phone browser, go to
   `http://[your-pc-tailnet-name]:8000` (e.g.
   `http://autosdr-pc.tail-scale.ts.net:8000`). On iOS Safari /
   Chrome → Add to Home Screen to install it as a PWA. On Android →
   the browser will offer Install. iOS 16.4+ requires the PWA to be
   installed before it can receive push notifications.
4. **Subscribe this device.** Open Settings → Notifications → Enable
   on this device, accept the permission prompt, and fire a test
   notification to confirm the round-trip.
5. **Decide on the SMS path.** SMSGate has two modes:
   - **Local Server (recommended for one tool).** Keep the phone on
     Tailscale; AutoSDR talks to `http://[phone-tailnet]:8080` over
     the tailnet. One VPN, one URL.
   - **Cloud Server (decouples SMS from the VPN).** Switch the phone
     to SMSGate Cloud Server mode and point AutoSDR's connector at
     `https://api.sms-gate.app/3rdparty/v1`. Useful if Tailscale-on-
     phone burns too much battery or the cellular network is flaky.
   In both cases the AutoSDR connector is the same — only the URL
   the operator pastes changes (see `autosdr/connectors/smsgate.py`).

What this is *not*: a public dashboard. There's no `https://`
hostname, no Cloudflare Tunnel, and no inbound port-forward involved.
The dashboard is private to your tailnet; a notification deep-link
shared off the tailnet is harmless because the URL doesn't resolve.

If something doesn't work, the order of things to check is:

1. `tailscale status` on both ends — both connected?
2. From the phone: `http://[your-pc-tailnet-name]:8000/healthz` —
   does it return `{"status": "ok"}`? If not, almost always
   `HOST=127.0.0.1` (Settings → Networking will tell you).
3. From the PC: `curl http://[phone-tailnet]:8080/health` (SMSGate
   Local Server) — does the SMSGate API respond?
4. Settings → Notifications → "Send test notification" — does the
   notification arrive? If "gone" > 0 the device side dropped the
   subscription; tap Enable on this device again.

---

## Kill switch

Three ways to halt everything immediately:

| I want to…                          | Do this                               |
| ----------------------------------- | ------------------------------------- |
| Pause without stopping the process  | **Pause** button (top-right of the UI), or `POST /api/status/pause` |
| Resume                              | **Resume** button, or `POST /api/status/resume` |
| Stop the process                    | `Ctrl+C` in the uvicorn / `./scripts/dev.sh` terminal |
| Check state                         | **Dashboard** pill, or `GET /api/status` / `GET /healthz` |

Pause is checked before every LLM call, every connector send, and on every
scheduler tick. Inbound webhooks still return 202 so gateways don't retry;
processing is skipped silently.

---

## Reviewing the AI loop

Every LLM call is persisted. There are two principal ways to look at them:

- **UI** — the **Logs** route is a filterable table of every analysis,
  generation, evaluation and classification call. Deep-links from a thread
  show just that thread's calls.
- **Disk** — `data/logs/llm-YYYYMMDD.jsonl` (grep / jq -friendly) and the
  rotating `data/logs/autosdr.log` which captures scheduler + pipeline INFO from
  uvicorn.

---

## Project layout

```
autosdr/
  api/              # FastAPI routers (setup, workspace, campaigns, leads, threads, ...)
    schemas.py      # Pydantic request/response models; mirrors frontend TS types
    deps.py         # db_session + require_workspace dependencies
  config.py         # pydantic-settings; infrastructure-only env vars
  db.py             # SQLAlchemy engine/session; SQLite by default
  models.py         # ORM models for every table
  killswitch.py     # signals + flag file + hot-path guard
  llm/
    client.py       # LiteLLM wrapper + persistent call log
  prompts/          # versioned analysis / generation / evaluation / classification
  importer.py       # CSV + NDJSON import, E.164 normalisation, mobile detection
  connectors/
    base.py         # BaseConnector ABC (send + parse_webhook + poll_incoming)
    file_connector.py
    textbee.py
    smsgate.py
  pipeline/
    _shared.py      # generate_and_evaluate + thread_history helpers
    outreach.py     # analyse → generate → evaluate → send
    reply.py        # classify → close / park / (if auto-reply on) respond
    suggestions.py  # generate_reply_variants(n=2-3) for the HITL card
  scheduler.py      # outreach tick + inbound poller; calendar-day quota (midnight reset)
  webhook.py        # FastAPI app: mounts routers + serves frontend/dist + logging

frontend/           # React 19 + Vite 8 + Tailwind v4 operator console
tests/              # pytest suite; LLM calls are mocked
```

---

## Development

Two-process dev is optional — the backend serves the built frontend from
`frontend/dist`, so you can rebuild the UI whenever you like and refresh.
If you want HMR, the easiest path is the bundled dev script, which starts
uvicorn (with `--reload`) and Vite together, prefixes their output, and
stops both cleanly on Ctrl+C:

```bash
./scripts/dev.sh
```

Then open <http://localhost:5173>. The `/api/*` calls are proxied to
`:8000`, so there's still exactly one backend in play. Override the ports
via `BACKEND_PORT` / `FRONTEND_PORT` if you need to.

If you'd rather drive the two processes by hand:

```bash
uv run uvicorn autosdr.webhook:app --reload --port 8000
# in another terminal:
cd frontend && npm run dev      # Vite on 5173, proxies /api -> 8000
```

Tests:

```bash
uv run pytest
```

The suite is hermetic — LLM calls are mocked, the DB is a tmp SQLite file.
No network.

---

## Deployment

One process, one port. On the server:

```bash
git pull
cd frontend && npm install && npm run build && cd ..
uv sync
DATABASE_URL=sqlite:///data/autosdr.db HOST=0.0.0.0 PORT=8000 uv run uvicorn autosdr.webhook:app
```

Put it behind nginx / Caddy / Tailscale as you prefer. There's no built-in
auth — this is a single-operator tool; put it on a trusted network or behind
your own authentication layer.

---

## What's deliberately not included

- Multi-user auth. Single-operator by design.
- A second LLM provider out of the box (LiteLLM can talk to any of them —
  OpenAI, Anthropic, local via Ollama — just put the key in Settings and
  change the model slug).
- A self-hosted push relay. Web Push uses the browser-vendor public
  push gateways (FCM, APNS via Apple's push gateway, Mozilla autopush).
  Payloads are privacy-strict — only the lead's first name + thread id +
  a short generic body — so a leaked notification reveals "thread X
  needs attention" and nothing more. See ticket 0005 for the
  threat-model write-up.
- A native mobile app. The operator console is a single SPA that adapts
  down to ~360 px wide: hamburger drawer below `md:`, card-list fallback
  for tables, master-detail collapse on Inbox, sticky compose bar and
  collapsed LLM trail in `ThreadDetail`. Tap targets are ≥ 44 px on
  mobile.
