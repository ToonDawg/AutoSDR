# AutoSDR — Product Overview

**Status**: Draft  
**Version**: 0.4  
**Last Updated**: 2026-04-18

---

## 1. Problem Statement

Small business owners cannot afford dedicated Sales Development Representatives and
lack the time to conduct manual outreach at scale. Existing automation tools produce
generic, template-driven messages that prospects recognise immediately, resulting in
low reply rates and damage to the sender's reputation. There is no open-source,
self-hosted solution that captures a business owner's authentic voice and autonomously
manages the full outreach lifecycle — from first contact through to a warm handoff.

---

## 2. Proposed Solution

AutoSDR is an open-source, event-driven application that acts as an autonomous SDR
for small business owners. The owner completes a one-time setup (business context,
API keys, tone calibration), creates a campaign with a clear goal and daily limit,
uploads structured lead data, and the system handles initial outreach, reply handling,
and escalation — autonomously — until a lead is ready for the human to take over.

The application is self-hosted and open-source. Users bring their own API keys and
infrastructure. There is no vendor lock-in to a specific LLM provider or messaging
channel.

The frontend is a Progressive Web App (PWA) that can be installed on desktop or
mobile. It is the single control surface for all configuration, monitoring, HITL
interaction, and campaign management. Web Push notifications alert the owner when
a thread needs attention, even when the app is not in the foreground.

---

## 3. Core Principles

These principles govern every design and build decision:

- **Simplicity first.** Avoid over-engineering. If a feature can be deferred without
  hurting the core workflow, defer it.
- **Quality over speed.** A message that takes 60 seconds to generate and actually
  resonates is better than a message generated in 2 seconds that reads like spam.
  Async processing is acceptable and expected.
- **Honest data contracts.** The system only accepts structured lead data (CSV, JSON).
  Unstructured text imports are explicitly out of scope. Do not pretend to support
  what cannot be reliably processed.
- **Extensible by design.** Connectors (SMS, email), LLM providers, and data enrichment
  agents must be addable without rewriting core logic.
- **Human always wins.** The system never sends a message when it is uncertain. It
  escalates cleanly and never blocks the human from taking over.
- **The owner stays in control.** Every automated action can be paused, resumed, or
  overridden from the frontend at any time.

---

## 4. Target Users

### Primary persona — The Time-Poor Founder

A small business owner (1–10 employees) who does their own sales. They have a list
of leads, a clear value proposition, and zero time to write 200 personalised messages.
They are comfortable setting up a self-hosted tool if the setup process is well
documented. They do not have a developer on staff but can follow a README.

**Goals:**
- Upload a batch of leads and have the system start outreach without further input.
- Be notified when a lead is warm or the AI is stuck — even when not at their desk.
- Trust that the messages sound like them, not like a robot.
- Stay in control: pause campaigns, skip leads, or take over a conversation at any point.

**Frustrations:**
- Generic outreach tools that blast templates and burn their domain reputation.
- Tools that require a CRM subscription or complex integrations before they are useful.
- Losing track of which leads have been contacted and what was said.
- Black-box automation with no way to intervene mid-campaign.

---

## 5. Non-Goals (MVP)

The following are explicitly out of scope for v0.1. They may be revisited in later
versions.

- **Unstructured text imports.** Raw paragraph dumps cannot be reliably parsed into
  lead records. CSV and JSON only.
- **Website scraping agents.** Agents that browse a lead's website to enrich context
  are a v1.0+ feature.
- **Multi-tenancy / SaaS billing.** AutoSDR is single-user, self-hosted.
- **iOS SMS integration.** Apple's ecosystem does not support programmatic SMS
  without enterprise agreements. Android gateway only for MVP.
- **Email connector.** Email outreach is v1.0. SMS via Android gateway is the MVP
  channel.
- **CRM integrations.** No HubSpot, Salesforce, or Pipedrive connectors in MVP.
- **AI lead scoring and prioritisation.** Lead ordering follows import order (FIFO).
  Owners control priority by sorting their CSV before uploading.
- **Chat interface for config updates.** A conversational UI for updating workspace
  settings mid-campaign is deferred post-MVP.
- **LLM fine-tuning.** The system uses prompting and swipe-based tone calibration.
  It does not fine-tune any model.

---

## 6. Success Criteria

These are the measurable outcomes that define a successful MVP:

| Metric | Target |
|---|---|
| Setup time | A first-time user completes workspace setup (business info + tone calibration + API keys + first campaign) in under 15 minutes |
| Tone calibration | The swipe-based onboarding flow collects at least 10 left/right decisions and compiles a `tone_prompt` without manual editing |
| HITL routing accuracy | The intent classifier correctly routes incoming replies to auto-reply vs. human escalation >= 90% of the time, measured on a labelled test set of 100 simulated replies |
| Webhook acknowledgement | Incoming reply webhooks are acknowledged within 2 seconds of receipt |
| Message quality | Self-evaluation pass: >= 85% of AI-generated messages score "acceptable" or above on a rubric covering tone match, personalisation signal, and call-to-action clarity |
| Lead import | A 1,000-row CSV or JSON file with mixed columns is fully ingested and field-mapped in under 60 seconds |
| PWA notifications | Web Push notifications are delivered to the installed PWA within 10 seconds of a HITL escalation event |

---

## 7. Campaign Model

A campaign is the primary unit of work in AutoSDR. It defines what the system is
trying to achieve, how aggressively it operates, and which leads it works through.

| Field | Description |
|---|---|
| **Goal** | Plain text description of the desired outcome (e.g. "Book a 15-minute discovery call"). The LLM reads this in every prompt to steer conversation and decide when a thread is won. Updatable at any time. |
| **Outreach per day** | Integer daily limit on new first-contact messages sent by this campaign. Default: 50. Updatable at any time from the dashboard. |
| **Connector** | Which channel to use for outreach (MVP: Android SMS gateway only). |
| **Lead source** | The imported lead list assigned to this campaign. |
| **Status** | One of: `draft`, `active`, `paused`, `completed`. |

Campaigns process leads in import order (FIFO). Owners control priority by ordering
their CSV or JSON before uploading. AI-based lead scoring is a post-MVP feature.

---

## 8. Human-in-the-Loop (HITL)

The system escalates a thread to the owner when any of the following conditions are
met:

- The intent classifier scores `requires_human: true`
- The intent classifier confidence falls below 0.80
- The lead explicitly asks to speak to a human
- The lead asks "are you a bot?" or a close variant
- The thread exceeds a configurable maximum auto-reply count (default: 5)

When a thread is escalated:

1. The thread status changes to `paused_for_hitl`
2. A Web Push notification is sent to the owner's installed PWA
3. The owner opens the chat panel in the frontend, reads the full conversation, and
   either types a reply (sent via the connector) or marks the thread as won/lost/skipped
4. The owner can hand back to the AI at any point, or keep the thread in manual mode

The system never sends an automated message to a thread in `paused_for_hitl` status.

---

## 9. Start / Stop Controls

The owner can control the system at three levels: system-wide, per-campaign, and
per-thread.

**System-wide kill switch:**

A global halt that stops all outreach and reply processing immediately, regardless
of campaign state. Three redundant layers, all equivalent in effect:

- **Signal (`Ctrl+C` / `SIGTERM`)** — sent to the running process. Handler stops
  accepting new work, drains in-flight pipelines up to 10 seconds, then exits
  cleanly.
- **Flag file** — any file at the configured pause-flag path (default
  `data/.autosdr-pause`) pauses all processing within one second of appearing.
  Removing the file resumes. Webhooks still return 202 while paused so the SMS
  gateway does not retry; processing is dropped silently.
- **CLI commands** — `autosdr pause` creates the flag file, `autosdr resume`
  removes it, `autosdr stop` sends SIGTERM to the running process.

Pause is checked before every LLM call, every connector send, and at the top of
every scheduler tick. No message is sent after a pause trips. In-flight LLM calls
that cannot be cancelled complete server-side but their results are discarded.

**Campaign level (frontend dashboard in v1; CLI in POC):**
- Pause a campaign (stops new outreach for that campaign; active threads continue
  until their next reply cycle then pause)
- Resume a paused campaign
- Stop a campaign permanently (no further outreach; threads remain readable)
- Update campaign goal or daily limit at any time (takes effect on the next cron cycle)

**Thread level:**
- Pause a single thread (AI stops replying; owner handles manually)
- Resume a paused thread (AI takes back over)
- Skip a lead (marks as skipped; no further contact)
- Mark as won / lost (closes the thread)

All status changes are logged with a timestamp and the actor (system or human).

---

## 10. Observability & POC Review

The POC is built to be *reviewed* — the point is to find out whether the AI
loop actually works end-to-end, not just to ship messages. Every meaningful
decision is captured so the owner can audit and refine prompts after the fact.

**LLM call log (`llm_call` table + `data/logs/llm-YYYYMMDD.jsonl`):**

One row per LLM invocation — including failed attempts and self-heal retries.
Each row records:

- `purpose` — `analysis` | `generation` | `evaluation` | `classification` | `other`
- `model`, `prompt_version`, `temperature`, `attempt`, `response_format`
- Full `system_prompt` and `user_prompt` (truncated to
  `LLM_LOG_MAX_PROMPT_CHARS`, default 16 000 chars)
- `response_text` and — for JSON calls — `response_parsed`
- `tokens_in`, `tokens_out`, `latency_ms`, `error`
- Context tags: `workspace_id`, `campaign_id`, `thread_id`, `lead_id`

**Pipeline logs (`data/logs/autosdr.log`, rotating):**

Structured INFO-level log lines at every decision point: angle extracted,
draft generated, eval score and pass/fail, intent classified, routing
decision (closed-won / closed-lost / HITL / auto-reply), escalation reasons.
Tailable while `autosdr run` is live.

**Review surfaces:**

- `autosdr logs llm [--tail N] [--thread ID] [--lead ID] [--campaign ID]
  [--purpose X] [--errors] [--show-prompts]` — browse the call log.
- `autosdr logs thread <id> [--show-prompts]` — chronological transcript of
  a single thread (messages + every LLM call that shaped them).
- `autosdr hitl list` / `autosdr hitl show <thread>` — inspect threads that
  escalated, with the rejected drafts and feedback captured at escalation time.

Turning this off: `LLM_LOG_ENABLED=false` skips DB + JSONL writes. The
in-memory token counter (`autosdr status`) keeps working.

---

## 11. High-Level Workflow

```
[Owner] → Setup (business info + tone calibration + API keys)
          ↓
[Owner] → Create campaign (goal + daily limit + connector)
          ↓
[Owner] → Upload leads (CSV / JSON)
          ↓
[System] → Field mapping (agent-assisted for ambiguous columns; UI confirmation step)
          ↓
[Cron]  → Each day: pick next N leads respecting outreach_per_day limit (FIFO)
          ↓
[System] → Analyse lead (raw_data blob → extract personalised angle via LLM)
          ↓
[System] → Draft message (angle + tone_prompt + campaign goal → outreach message)
          ↓
[System] → Self-evaluate (score message; regenerate if below threshold; max 3 attempts)
          ↓
[System] → Send via connector (Android SMS gateway)
          ↓
[Webhook] → Incoming reply received
          ↓
[System] → Classify intent
          ↓
      ┌─────────────────────────┬──────────────────────────────────┐
      │ Auto-reply              │ Escalate (HITL)                  │
      │ Draft + eval + send     │ Pause thread                     │
      │                         │ Web Push notification to owner   │
      │                         │ Owner replies via chat panel     │
      └─────────────────────────┴──────────────────────────────────┘
          ↓ (thread closed by system or owner)
[System] → Log outcome → Update lead and thread status
```

---

## 12. Key Concepts & Terminology

These terms are used consistently across all AutoSDR documentation.

| Term | Definition |
|---|---|
| **Workspace** | The owner's global configuration: business info, tone prompt, API keys, and default settings |
| **Lead** | A target business or contact imported from a CSV or JSON file |
| **Campaign** | A named outreach effort with a goal, daily limit, connector, and assigned lead list |
| **Thread** | An ongoing conversation between AutoSDR and a single lead via a specific connector |
| **Connector** | An integration that sends and receives messages (MVP: Android SMS gateway) |
| **Tone prompt** | A compiled LLM system prompt segment derived from the owner's swipe-based calibration during setup |
| **raw_data** | A flexible JSON blob on the Lead record storing all imported columns not mapped to a core field |
| **HITL** | Human-in-the-Loop — the escalation state where the AI pauses and notifies the owner |
| **Intent classification** | Evaluating an incoming reply to determine the correct next action (auto-reply, escalate, or close) |
| **Self-evaluation** | An LLM pass that scores a drafted message against a rubric before it is sent |
| **FIFO** | First-in, first-out — the lead processing order within a campaign, determined by import order |

---

## 13. Tech Stack Overview

Full details are in the architecture document. High-level decisions recorded here
for cross-document consistency. Two tiers are defined: the **POC stack** (minimal,
single-process, what runs today) and the **v1 stack** (the full system described
across the four docs).

**POC stack:**

| Layer | Choice | Rationale |
|---|---|---|
| Entry surface | Python CLI (typer) | No frontend needed to prove the AI loop; fastest path to running |
| Backend | Python + FastAPI (single process) | Webhook ingress only; no separate API layer |
| Database | SQLite via SQLAlchemy | Zero infra; same ORM models swap to Postgres in v1 |
| Task queue | FastAPI `BackgroundTasks` + in-process asyncio scheduler | Ack webhooks in <2s without Redis; Celery is a v1 scale concern |
| LLM interface | LiteLLM | Single interface; Gemini by default for free-tier quotas |
| LLM default | `gemini/gemini-3-flash-preview` (generation), `gemini/gemini-3.1-flash-lite-preview` (eval/classification) | Free tier; splits heavy-volume deterministic calls onto the lite quota |
| SMS connectors | `FileConnector` (dev) + `TextBeeConnector` (poll-based, hosted) + `SmsGateConnector` (push-based, self-hosted) | Three shipped options covering zero-phone dev, zero-infra hosted, and fully self-hosted; all implement the same `BaseConnector` ABC |
| Inbound delivery | Poll connector = scheduler drains `poll_incoming()` each tick (TextBee); push connector = Android device POSTs to `/api/webhooks/sms` (SmsGate) | The two inbound shapes coexist — `connector.poll_incoming()` is a no-op for push connectors, and the webhook endpoint is inert for poll connectors |
| Test modes | `autosdr run --dry-run` (FileConnector regardless of `CONNECTOR`) + `autosdr run --override-to <number>` (real connector, all sends redirected to one phone) | The two rehearsals most likely before pointing the agent at real leads — mock end-to-end without a phone, and single-phone smoke test on real SMS |
| Observability | `llm_call` DB table + `data/logs/llm-YYYYMMDD.jsonl` + rotating `data/logs/autosdr.log` | Every LLM attempt (system + user prompts, response, tokens, latency, error) persisted for offline prompt review via `autosdr logs llm` / `autosdr logs thread` |

**v1 stack (additions beyond the POC):**

| Layer | Choice | Rationale |
|---|---|---|
| Frontend | Next.js + TypeScript + Tailwind + shadcn | Owner's preference; PWA support via next-pwa |
| Database | PostgreSQL | Relational core with JSONB for raw_data blob; production-grade concurrency |
| Task queue | Redis + Celery | Horizontal scaling; cron jobs; webhook queuing at scale |
| Push notifications | Web Push API (via service worker) | PWA-native; no third-party push service required |
| Webhook inbound (scale) | TextBee webhooks via ngrok/cloudflared tunnel, or SmsGate over the LAN | Push beats poll for sub-second reply latency once you operate many devices |
| Third connector | httpSMS (alternative Android gateway) | Drop-in alternative; same `BaseConnector` interface |

---

## 14. Document Map

This is Doc 1 of 4. The remaining documents go deeper on specific areas.

| Doc | Title | Status |
|---|---|---|
| Doc 1 | **Product Overview** (this document) | Draft v0.4 |
| Doc 2 | **Data Architecture Spec** — schemas, import logic, blob strategy, E.164 normalisation, multi-source merging, `llm_call` log | Draft |
| Doc 3 | **AI & Messaging Spec** — tone calibration, generation pipeline, self-evaluation, intent classification, connector interface (File / TextBee / SmsGate + override + dry-run) | Draft |
| Doc 4 | **Onboarding & Config Spec** — POC CLI flow, v1 wizard, API key setup, TextBee & SmsGate setup, dry-run / override test modes, PWA install, kill switch, log review | Draft |
