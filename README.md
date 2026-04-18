# AutoSDR

Autonomous SDR for small business owners. Self-hosted, open-source. You point
it at a lead list + a goal, it runs the outreach + reply loop on your behalf
over SMS (via an Android phone), and it hands the conversation back to you
when it gets stuck.

This repo is the **proof-of-concept (POC)** — single-process, SQLite, Gemini,
CLI only. The full v1 stack (Postgres, Redis/Celery, Next.js PWA with Web Push)
is described in the four spec docs in this directory; the POC implements the
core AI loop end-to-end so the hard part can be validated before the rest is
built.

Spec docs:

- [autosdr-doc1-product-overview.md](./autosdr-doc1-product-overview.md)
- [autosdr-doc2-data-architecture.md](./autosdr-doc2-data-architecture.md)
- [autosdr-doc3-ai-messaging.md](./autosdr-doc3-ai-messaging.md)
- [autosdr-doc4-onboarding-config.md](./autosdr-doc4-onboarding-config.md)

## What the POC does

1. Imports a CSV or NDJSON lead list, normalising phone numbers to E.164 and
   flagging non-mobile rows so they don't burn gateway attempts.
2. Analyses each lead with an LLM to extract a personalisation angle from
   whatever raw data the import carried (reviews, ratings, categories, etc.).
3. Drafts an outreach SMS in your tone and self-evaluates it against five
   criteria (tone, personalisation, goal alignment, length, naturalness).
   Rewrites up to 3 times; escalates to HITL if it can't pass.
4. Sends via your configured connector (TextBee Android gateway, or a local
   file-backed connector for dev / testing).
5. Classifies inbound replies and either auto-responds (positive / objection /
   question) or escalates (bot check, low confidence, max-replies reached,
   goal achieved, clear negative). Inbound is polled from TextBee — no public
   URL or tunnel is required.
6. Enforces a rolling 24-hour per-campaign send cap with a configurable
   inter-send delay.
7. Obeys a three-layer kill switch (Ctrl+C, flag file, CLI).
8. Persists every LLM call (system + user prompts, response, tokens, latency,
   error) to an `llm_call` table and to `data/logs/llm-YYYYMMDD.jsonl` so you
   can audit and refine prompts after the fact via `autosdr logs llm` and
   `autosdr logs thread <id>`.

## Requirements

- Python 3.11+
- A Google Gemini API key (the free tier is fine for the POC — get one at
  https://aistudio.google.com/app/apikey)
- For real SMS: an Android phone with the
  [TextBee](https://textbee.dev) app installed and a TextBee API key + device
  id. No tunnel / public URL required — the scheduler polls TextBee's REST API.
- For dev-only testing: nothing beyond Python + the Gemini key — a file-backed
  connector lets you drive the reply pipeline without a phone.

## Quickstart (dev — file connector, no phone needed)

```bash
git clone <this repo> && cd AutoSDR
python -m venv .venv && source .venv/bin/activate
pip install -e '.[dev]'

cp .env.example .env
# set GEMINI_API_KEY. leave CONNECTOR=file.

autosdr init \
  --business-dump "We run a staffing platform that helps aged-care facilities reduce agency spend and weekend understaffing. We onboard in under a week. We charge per shift filled, not a subscription." \
  --tone "Casual and direct. One idea per sentence. No corporate language. Open with a grounded observation about the recipient, close with a single low-pressure question."

autosdr import example-leads.json
autosdr campaign create --name "Pilot" --goal "Book a 15-minute call to walk through the staffing platform" --per-day 5
# the command above prints the campaign id — copy it

autosdr campaign assign <campaign-id> --all-unassigned
autosdr campaign activate <campaign-id>

autosdr run
# in another terminal: tail -f data/outbox.jsonl  to see drafted SMSes
# simulate a reply:
# autosdr sim inbound --from "+61400000001" --content "tell me more"
# Ctrl+C to stop. `autosdr pause` / `autosdr resume` to toggle without stopping.
```

## Switching to real SMS (TextBee)

1. Install the TextBee app on your Android phone (https://textbee.dev/download).
   Toggle "Receive SMS" on in its settings.
2. At https://textbee.dev/dashboard, register the device, copy the API key and
   the device id.
3. Edit `.env`:
   ```
   CONNECTOR=textbee
   TEXTBEE_API_KEY=...
   TEXTBEE_DEVICE_ID=...
   ```
4. Smoke-test: `autosdr test sms --to "+61400000000"` — sends to your own
   number. The CLI first validates the device is reachable.
5. `autosdr run`. The scheduler polls TextBee every `INBOUND_POLL_S` seconds
   (default 20 s) so replies flow back in without any public URL.

## Reviewing the AI loop

Every LLM call is persisted so you can audit and iterate on prompt quality:

```bash
# Last 20 calls in a compact table.
autosdr logs llm

# Full system/user/response for the last 5 generation calls.
autosdr logs llm --purpose generation --tail 5 --show-prompts

# Everything that happened on a single thread, inbound + outbound + every
# model call in between (chronological).
autosdr logs thread <thread-id>
autosdr logs thread <thread-id> --show-prompts
```

There's also `data/logs/llm-YYYYMMDD.jsonl` if you'd rather grep / pipe through
jq, and `data/logs/autosdr.log` (rotating) which captures the scheduler +
pipeline INFO stream.

## Kill switch

Three redundant ways to halt processing immediately:

| I want to... | Run this |
|---|---|
| Pause without stopping the process | `autosdr pause` (creates `data/.autosdr-pause`) |
| Resume | `autosdr resume` |
| Stop the process gracefully | `autosdr stop` (or `Ctrl+C` in the run terminal) |
| Check state | `autosdr status` |

Pause is checked before every LLM call, every connector send, and at every
scheduler tick. Webhooks still return 202 while paused so the gateway does not
retry; processing is dropped silently.

## Project layout

```
autosdr/
  config.py         # pydantic-settings; reads .env
  db.py             # SQLAlchemy engine/session; SQLite by default
  models.py         # ORM models for every table in Doc 2 + llm_call
  killswitch.py     # signals + flag file + hot-path guard
  llm/client.py     # LiteLLM wrapper; retries, kill-switch, persistent call log
  prompts/          # versioned analysis / generation / evaluation / classification
  importer.py       # CSV + NDJSON import, E.164 normalisation, mobile detection
  connectors/
    base.py         # BaseConnector ABC (send + parse_webhook + poll_incoming)
    file_connector.py  # dev/testing
    textbee.py         # real Android SMS gateway via TextBee REST
  pipeline/
    outreach.py     # analyse -> generate -> evaluate -> send
    reply.py        # classify -> route (won/lost/auto-reply/escalate)
  scheduler.py      # outreach tick + inbound poller; rolling-24h quota
  webhook.py        # FastAPI app: /healthz, /api/webhooks/sim
  cli.py            # typer CLI (init / import / campaign / run / logs / ...)

tests/              # pytest suite; LLM calls are mocked
```

## Running the tests

```bash
python -m pytest
```

Tests are hermetic — they mock the LLM and run everything against SQLite files
in a tmp dir, so they're free to run locally without hitting Gemini.

## What this POC does NOT include (deferred to v1)

- Any frontend / PWA / Web Push (HITL surfaces via `autosdr hitl list` and
  `autosdr logs thread`).
- Swipe-based tone calibration (you provide tone as text on `autosdr init`).
- Business-data extraction agent (the `business_dump` is used directly).
- Field-mapping agent during import (a fixed-column schema is used).
- Postgres / Redis / Celery (SQLite + `BackgroundTasks` + an asyncio scheduler
  cover the POC's ingress and throughput needs).
- Webhook-based inbound (polling is simpler for the POC; the `BaseConnector`
  ABC supports `parse_webhook` for anyone who wants to add a push path later).

Everything excluded is UI or scale. The AI loop — the hard part — is complete.
