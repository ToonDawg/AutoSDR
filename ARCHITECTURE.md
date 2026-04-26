# AutoSDR — Architecture Overview

This doc describes **what the code actually does** and **how the pieces fit
together**. It is the as-built companion to the four `autosdr-doc*` spec docs,
which describe the intended system; where this document and those disagree,
this one is the source of truth for the POC. For setup and CLI usage, see
`README.md`.

## 1. What AutoSDR is

AutoSDR is an autonomous SDR for small business owners. You give it:

- A short description of your business and your preferred tone of voice.
- A list of leads (CSV or NDJSON).
- A campaign goal (e.g. "Book a 15-minute call").

It then runs outreach end-to-end over SMS: drafting personalised first
contacts, sending them through an Android phone, classifying any replies, and
auto-responding when it's confident — while handing conversations back to the
owner when it isn't.

The POC is a single Python process: one FastAPI app with an async scheduler,
one SQLite database, a Google Gemini backend (LiteLLM), and a CLI. The design
is deliberately simple so the **AI loop** (the hard part) can be validated
before any distributed infrastructure is built.

## 2. The big picture

Once configured, AutoSDR runs two concurrent loops inside a single FastAPI
process:

- **An outreach loop** that periodically picks queued leads off each active
  campaign and walks them through the AI drafting pipeline before sending.
- **An inbound loop** that periodically pulls new replies from the SMS
  gateway and walks each one through the reply pipeline.

Both loops share a single SQLite database, a single SMS connector instance,
and a single kill switch. A third background task watches a flag file so the
owner can pause the system in under a second without killing the process.

The journey of a single lead looks like this:

1. **Import.** A row in the CSV becomes a `lead` record with its phone
   normalised to E.164 and any non-mobile number skipped up-front.
2. **Assign + activate.** The owner assigns leads to a campaign and
   activates it, which queues each `campaign_lead` for the scheduler.
3. **Analyse.** When the lead's turn comes, an analysis prompt reads the
   lead's raw data and picks the single strongest personalisation angle.
4. **Generate.** A generation prompt drafts a short SMS in the owner's
   tone, aimed at the campaign goal, using the angle from step 3.
5. **Evaluate.** An evaluation prompt scores the draft against five
   criteria. If it passes, we send. If it fails, we ask the generator to
   rewrite using the evaluator's feedback — up to three attempts before
   escalating to the owner.
6. **Send.** The approved draft goes through the configured connector
   (TextBee, SMSGate, or a local file for dev) and is persisted as an AI
   message on the thread.
7. **Classify replies.** When the lead texts back, a classification prompt
   labels the intent, and the system either auto-responds (via the same
   generate/evaluate loop) or escalates to human-in-the-loop.

Every LLM call at every step is persisted to the database and to a daily
JSONL log so the owner can review, filter, and refine prompts after the fact.

## 3. Component map

One-liner per module so you can navigate the code:

| Module | Role |
| --- | --- |
| `autosdr/config.py` | Environment- and database-backed settings. |
| `autosdr/db.py` | SQLAlchemy engine/session factory (SQLite today, Postgres-ready). |
| `autosdr/models.py` | ORM models plus centralised status vocabularies. |
| `autosdr/killswitch.py` | Three-layer pause/stop mechanism shared by every hot path. |
| `autosdr/importer.py` | CSV / NDJSON import with phone normalisation and contact-type detection. |
| `autosdr/llm/client.py` | LiteLLM wrapper: retries, kill-switch, usage counters, persistent call log. |
| `autosdr/compliance.py` | Deterministic STOP / opt-out keyword matcher (Spam Act / TCPA shortcut). Pure function — no I/O, no LLM. |
| `autosdr/prompts/` | Versioned system + user prompts for analysis, generation, evaluation, classification. |
| `autosdr/connectors/` | Pluggable SMS connectors plus a file-backed dev stub and an override wrapper. |
| `autosdr/pipeline/outreach.py` | The analyse → generate → evaluate → send pipeline for first contacts and replies. |
| `autosdr/pipeline/reply.py` | The classify → route pipeline for inbound messages. |
| `autosdr/scheduler.py` | The two async loops (outreach tick and inbound poll). |
| `autosdr/webhook.py` | FastAPI app, lifespan task wiring, and inbound HTTP endpoints. |
| `autosdr/cli.py` | Typer CLI: `init`, `import`, `campaign`, `run`, `logs`, etc. |
| `scripts/dryrun_prompts.py` | Offline prompt exerciser for iterating on prompt changes without the full pipeline. |
| `tests/` | Pytest suite; LLM and connector HTTP calls are mocked. |

## 4. Configuration and the single-workspace model

The POC is single-tenant: one row in the `workspace` table holds the
business name, a free-form business description, the tone snapshot, and a
JSON settings blob. The settings blob is the runtime source of truth for
things like the LLM model slots, evaluator threshold, per-campaign send
rate, and scheduler cadence.

Environment variables (`.env`) are used for two kinds of config:

- **Secrets and infrastructure** (API keys, database URL, connector choice)
  that should never live in the database.
- **Seed defaults** for the workspace settings blob, written once at
  `autosdr init` time. Changing these later in `.env` does **not** change
  the workspace — the DB wins at runtime, which keeps the agent's behaviour
  stable across restarts.

A handful of scheduler knobs (`SCHEDULER_TICK_S`, `MIN_INTER_SEND_DELAY_S`,
etc.) can override the DB values at runtime so dev and test environments
can dial cadence up or down without editing the workspace row.

## 5. Lead import

The importer accepts CSV, NDJSON, or a single JSON array. It recognises a
small fixed set of core columns (name, category, address, website, phone)
by exact match or common alias; anything else is preserved verbatim in a
per-lead JSON blob so the analysis prompt can use it as context later.

Two decisions happen up-front so bad data never costs tokens:

- **Phone normalisation.** Every phone string is parsed into E.164 using a
  region hint from the workspace. Unparseable numbers are recorded with a
  clear skip reason rather than silently dropped.
- **Contact-type gating.** Landlines, toll-free numbers, and unknowns are
  imported but flagged as skipped — the record stays for the owner to
  review, but the scheduler will never try to text them.

Re-importing the same file is safe: core fields are only filled where
they're currently empty, and the raw-data blob is merged at the key level.

## 6. The messaging abstraction

All outbound and inbound SMS goes through a thin `BaseConnector`
abstraction with three methods: send, parse a webhook payload, and poll for
new inbound messages. This keeps the AI loop and the thread model entirely
decoupled from whichever gateway is in use.

Three real connectors ship today:

- **TextBee** — a commercial Android SMS gateway. POC uses its polling API
  so no public URL / tunnel is needed.
- **SMSGate** — an open-source Android gateway that pushes inbound via
  webhook to a LAN-reachable endpoint.
- **FileConnector** — dev-only: outbound is appended to a JSONL file,
  inbound is driven from the CLI or a simulator endpoint.

Two wrappers compose on top of those:

- **Dry-run mode** forces the FileConnector regardless of the configured
  connector, so nothing touches the wire but the LLM still runs end-to-end
  for rehearsals.
- **Override mode** wraps any real connector to redirect every outbound
  message to a single phone number (typically the owner's own). Incoming
  messages from that number are rewritten back to the real lead's phone so
  the reply pipeline still routes correctly. This lets the owner
  dress-rehearse the entire loop against one device before pointing at real
  leads.

A single connector instance is built at process start and shared by the
scheduler, the inbound poller, and the webhook handler so in-memory state
(TextBee's seen-ids dedup set, override mode's last-target mapping) stays
consistent.

## 7. The AI loop

The heart of the system is four prompts, each defined in its own module
under `autosdr/prompts/` with a version string so rows in the `llm_call`
audit table can be correlated back to the exact prompt they ran against.

### 7.1 Analysis

For first-contact messages, the analysis prompt reads the lead's raw data
(reviews, categories, ratings, amenities, owner replies, etc.) and picks
the single strongest **personalisation angle** from a fixed menu: stale
info, weak online presence, signature amenity, point of difference, recent
review theme, brand voice, or a category-plus-location fallback. It
returns the angle, the evidence that supports it, and a confidence score.

Two details worth calling out because they shape downstream messages:

- The prompt also tries to extract the owner's first name so the draft
  can open with a casual greeting. The rules for when this is safe are
  strict (possessive in the business name, an owner-signed review reply,
  or an explicit ownership keyword). A code-level validator double-checks
  the model's output, because getting this wrong is worse than sending no
  greeting.
- The prompt extracts a short natural trading name to use in conversation
  when the full Google business name carries a database-style suffix.

The raw-data blob is truncated to a configurable byte ceiling before being
shown to the model, longest strings first, so a single verbose lead can't
blow the context window.

The resulting angle is stashed on the thread so subsequent reply turns use
the same personalisation thread without re-analysing.

### 7.2 Generation

The generation prompt takes the workspace tone snapshot, the business
description, the campaign goal, the angle, and any prior message history,
and drafts an SMS.

The prompt is deliberately opinionated. It codifies the target voice
(curious neighbour, not vendor pitch), forbids specific AI-speak openers,
specifies four valid opening patterns, enforces a mandatory credential
line so the recipient knows what the sender does, dictates the shape of
the call to action, and gives concrete turnaround language so offers feel
specific rather than vague. The version string increments every time we
meaningfully change the voice so audit rows remain meaningful.

For reply turns, the same prompt runs with the full message history
appended and a note that this is a follow-up, not first contact.

### 7.3 Evaluation

Before anything is sent, the draft is scored by a second LLM call against
five weighted criteria: tone match, personalisation, goal alignment,
length, and naturalness. The prompt instructs the evaluator to act like a
strict editor — it's calibrated to push back more often than not, because
a rewrite is cheaper than a bad send.

Three defensive steps are applied to the evaluator's output:

- Length validity is **recomputed from the draft itself** rather than
  trusted from the model, so a forgetful evaluator can't pass a
  three-segment message.
- The overall score is **recomputed** as a weighted average of the
  component scores rather than trusted from the model.
- The pass flag is **derived** from the overall score and length; the
  model doesn't get to self-certify.

If the draft fails, the evaluator's feedback is fed back into the
generation prompt for a rewrite. Up to three attempts are made. If all
three fail, the thread is marked for HITL with the full attempt history
(drafts, scores, and feedback) stashed in a JSON blob for the owner to
review.

### 7.4 Classification

When a reply arrives, a lightweight classification prompt labels its
intent as one of: positive, objection, question, negative, unclear,
bot_check, goal_achieved, or human_requested. It also returns a confidence
score.

Two rules are enforced in code rather than trusted to the prompt:

- The intent label is validated against the allowed set; anything unknown
  becomes `unclear`.
- The `requires_human` flag is **recomputed** from the intent and
  confidence, not taken from the model. Deterministic escalation rules
  are too important to leave to prompt drift.

## 8. Outreach pipeline

The outreach pipeline is the choreography that turns one queued
campaign-lead into either a sent message or a HITL-flagged thread. It:

- Creates (or fetches) the thread for this campaign-lead so every LLM call
  has a stable id to attach observability to.
- Runs the analysis step on the first turn only; subsequent reply turns
  re-use the angle already stored on the thread.
- Runs the generate-and-evaluate loop up to three times.
- On pass, sends via the connector and records an AI message on the thread
  with rich metadata (model, prompt versions, attempt counts, scores, the
  connector's provider id).
- On fail, flips the thread into HITL status and records the reason and
  context.
- On connector failure, does the same HITL flip but tags it as a send
  error rather than an eval failure, so the owner can distinguish "the AI
  couldn't write a good message" from "the phone's offline".

Status is propagated consistently: a successful send flips the
`campaign_lead` to `contacted` and the `lead` to `contacted` if it wasn't
already. HITL flips flow the thread into `paused_for_hitl`.

## 9. Reply pipeline

When an inbound message arrives (either polled from TextBee or pushed as
an SMSGate webhook), the reply pipeline:

- Normalises the sender's number to E.164 and looks up the lead. Unknown
  senders are recorded in `unmatched_webhook` rather than silently
  dropped, so the owner can trace stray messages later.
- Resolves which thread to route to. When a lead has multiple active
  threads (same person, multiple campaigns), the one with the most recent
  outbound message wins. Closed threads are ignored.
- Acquires a row-level lock on the thread before proceeding. Under
  Postgres this uses `SELECT ... FOR UPDATE`; under SQLite we rely on the
  WAL writer lock. The point is that two inbound messages for the same
  thread cannot race through classification and double-reply.
- Records the inbound as a message on the thread.
- **Deterministic STOP / opt-out shortcut.** Before any LLM call, the
  pipeline runs `autosdr.compliance.match_opt_out` against the inbound
  body. On a hit (default keywords: `STOP`, `STOP ALL`, `UNSUBSCRIBE`,
  `UNSUB`, `REMOVE ME`, `OPT OUT`, `CANCEL`, `END`, `QUIT`; word-boundary
  match with a small third-party denylist), the pipeline flags the lead
  with `do_not_contact_at` + `do_not_contact_reason="opt_out:<KEYWORD>"`,
  closes the thread lost, writes a sentinel `LlmCall` audit row
  (`model="(deterministic-opt-out)"`, `purpose=other`,
  `tokens=latency=0`) so the timeline in `autosdr logs thread` stays
  intact, and exits with `action=closed_opt_out`. The classifier never
  runs. The flag is permanent: outreach, scheduler, importer, and
  `assign_leads` all honour it; clearing requires manual DB intervention
  or a future Settings → Compliance card.
- Runs the classification prompt and routes by intent:
  - **Negative** and **goal_achieved** close the thread (lost/won
    respectively) and propagate the status down to `campaign_lead` and
    `lead`.
  - **bot_check**, **human_requested**, **unclear**, low-confidence, and
    "max auto-replies hit" all escalate to HITL with a specific reason.
  - Everything else runs the same generate-and-evaluate loop used for
    outreach, with the full thread history as context, and sends the
    approved reply.

A hard cap on auto-replies per thread keeps a runaway conversation from
consuming tokens indefinitely.

Threads already in HITL capture the inbound as a message but don't do
anything else — the owner is in the driver's seat from that point on.

## 10. The scheduler

The scheduler is two cooperating asyncio tasks, both driven by the
FastAPI lifespan.

**The outreach tick** runs every `scheduler_tick_s` seconds. On each tick
it loops over every active campaign and:

- Computes a **rolling 24-hour send count** for the campaign by counting
  AI messages on its threads in the last 24 hours. This is the campaign's
  quota budget for this tick.
- Takes the next N queued campaign-leads in queue order, capped by the
  remaining quota and a per-tick batch size so no single campaign starves
  the others.
- Runs the outreach pipeline on each, respecting a minimum inter-send
  delay between successful sends. The delay is awaited through the kill
  switch so a pause during a pacing wait returns instantly.

**The inbound poll** runs every `inbound_poll_s` seconds. It asks the
connector for any new inbound messages (a no-op for push-only providers)
and feeds each through the reply pipeline. Connector-level dedup means
polling an aggressive cadence doesn't cause duplicate processing.

Both loops check the kill switch before every outward-facing action and
await the shutdown event during their sleeps, so they wake instantly on
shutdown instead of finishing a 60-second tick wait first.

## 11. The LLM wrapper

Everything LLM-related goes through a thin wrapper around LiteLLM. On top
of LiteLLM's HTTP plumbing, the wrapper provides:

- **Kill-switch awareness.** Every call checks the switch before
  dispatch, so a pause aborts cleanly instead of burning tokens.
- **Retries with exponential backoff** for transient HTTP failures (5xx,
  429, timeouts). Non-retryable errors raise a domain-specific exception.
- **In-memory usage counters** (per-model call count and token totals)
  exposed via `autosdr status` for a quick cost pulse.
- **JSON-with-self-heal helper.** Structured prompts return through a
  helper that tries to parse the model's response as JSON and, on
  failure, runs a one-shot retry that feeds the broken response back with
  a "respond again with valid JSON" nudge. Callers never see raw text for
  structured prompts.
- **Persistent audit log.** Every attempt — including failures and
  self-heal retries — is written to the `llm_call` table and to a daily
  JSONL file. Each row records the system/user prompts, the response,
  token counts, latency, the prompt version, and the workspace / campaign
  / thread / lead ids that requested the call. Persistence is best-effort
  and never fails the call; a DB write error just means a single missing
  audit row.

There is also a one-off TLS patch at the top of the client module that
loads `certifi` roots and tolerates corporate MITM certificates. This is
pragmatic rather than elegant, but it's necessary to run on fresh Homebrew
Python or behind a proxy.

## 12. Kill switch and test modes

The kill switch has three layers, deliberately redundant because halting
processing in-flight matters more than elegance:

1. **POSIX signals.** `SIGINT` and `SIGTERM` flip a shared asyncio event;
   a second signal forces an immediate exit.
2. **A flag file.** A file at a configured path pauses all processing
   within about a second. Webhooks keep returning 202 so the gateway
   doesn't retry; the scheduler tick idles; LLM and connector hot paths
   raise a kill-switch exception and unwind the current pipeline.
3. **CLI wrappers.** `autosdr pause` / `resume` / `stop` manipulate the
   flag file or send a signal to the PID recorded at startup.

Two orthogonal test modes let the owner rehearse safely:

- **Dry-run** (`DRY_RUN=true` or `--dry-run`) forces the file connector
  so no SMS is sent. The LLM still runs, so you can audit prompts without
  risk.
- **Override** (`SMS_OVERRIDE_TO=+…` or `--override-to`) wraps the real
  connector so every outbound goes to a single phone, typically the
  owner's. Both modes compose: dry-run with override writes to the outbox
  with the override number as the recipient.

## 13. Observability

Three artefacts are written for every run so the owner can review what
happened:

- **`llm_call` rows + `data/logs/llm-YYYYMMDD.jsonl`.** One record per
  LLM attempt. Two CLI commands browse them: `autosdr logs llm` (tabular
  with filters) and `autosdr logs thread <id>` (all messages and LLM
  calls for a single thread, stitched in chronological order).
- **`data/outbox.jsonl`.** Every file-connector send; mostly for
  dev/testing.
- **`data/logs/autosdr.log`.** Rotating file handler capturing the
  scheduler + pipeline INFO stream. Useful for post-mortem on a run that
  already scrolled off the terminal.

`autosdr status` summarises the live state: paused or not, process PID,
active connector, in-memory LLM usage, and a per-campaign 24-hour send
count with remaining quota.

## 14. What's out of scope for the POC

The POC intentionally defers these pieces to v1, on the assumption that
the AI loop is the risky part and the rest is well-understood work:

- Any frontend or PWA. HITL surfaces via the CLI.
- Swipe-based tone calibration (tone is provided verbatim at `init`
  time).
- A business-data extraction agent (the raw business description is used
  as-is).
- A field-mapping agent at import time (a fixed column schema is used).
- Postgres / Redis / Celery. The SQLite + asyncio design covers POC
  throughput.
- Push-based inbound from TextBee. Polling is simpler and works without a
  public URL; the connector abstraction already supports webhooks, so
  anyone wanting to add a push path later has the seam.

Everything excluded is UI or scale. The AI loop — the hard part — is
complete.
