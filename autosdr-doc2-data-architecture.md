# AutoSDR — Data Architecture Spec

**Status**: Draft  
**Version**: 0.3  
**Last Updated**: 2026-04-18  
**Depends on**: Doc 1 — Product Overview

---

## 1. Overview

This document defines the data layer for AutoSDR: all database schemas, the lead
import pipeline, field mapping logic, the raw_data blob strategy, and how additional
imports merge with existing data without creating duplicates or losing conversation
history.

The guiding principle is **rigid where it matters, flexible where it doesn't**. Core
fields that drive routing, messaging, and deduplication use typed columns with
constraints. Everything else lives in a JSONB blob that the AI can read freely.

---

## 2. Database

PostgreSQL is the target datastore for v1. For the POC, SQLite is used with the
same SQLAlchemy models. JSONB columns (Postgres) map to `JSON` columns (SQLite);
queries that rely on JSON path operators are avoided in application code so the
swap is lossless. All tables use UUID primary keys (stored as `TEXT` on SQLite).

---

## 3. Schemas

### 3.1 Workspace

One workspace per installation. Stores global configuration, compiled tone prompt,
and business context.

```sql
CREATE TABLE workspace (
  id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  business_name TEXT NOT NULL,
  business_dump TEXT NOT NULL,       -- raw text drop of business info from setup
  business_data JSONB,               -- key fields extracted from the dump by LLM
                                     -- e.g. { "services": [...], "location": "...", "usp": "..." }
  tone_prompt   TEXT,                -- compiled from swipe calibration (v1) or owner-authored text (POC)
  settings      JSONB NOT NULL DEFAULT '{}',
                                     -- per-workspace defaults; see below
  created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
```

**`settings` shape:**

```json
{
  "max_auto_replies": 5,
  "eval_threshold": 0.85,
  "eval_max_attempts": 3,
  "raw_data_size_limit_kb": 50,
  "default_region": "AU",
  "scheduler_tick_s": 60,
  "min_inter_send_delay_s": 30,
  "max_batch_per_tick": 2,
  "llm": {
    "model_main": "gemini/gemini-3-flash-preview",
    "model_eval": "gemini/gemini-3.1-flash-lite-preview",
    "model_analysis": "gemini/gemini-3-flash-preview",
    "model_classification": "gemini/gemini-3.1-flash-lite-preview",
    "temperature_main": 0.7,
    "temperature_eval": 0.0
  }
}
```

Settings are loaded into memory at process start and re-read at the top of every
scheduler tick — so changes take effect on the next tick without a restart.

**Notes:**
- `business_dump` is stored verbatim so it can be re-processed if the extraction
  agent is improved.
- `business_data` holds the LLM-extracted structured fields (services offered, USP,
  location, pricing signals). The LLM reads both `business_data` and `tone_prompt`
  on every outreach generation call. In the POC, business extraction is optional —
  the raw dump is used directly if `business_data` is not populated.
- `settings` stores workspace-level defaults that campaigns can override.
- `default_region` is the region hint for `phonenumbers` parsing during import
  (ISO 3166-1 alpha-2). Default `AU` since the POC lead fixture is Australian;
  owners outside AU should update this before their first import.
- Only one row will exist in MVP (single-user). The schema supports future
  multi-tenancy by making all other tables reference `workspace_id`.

---

### 3.2 Lead

A target contact imported from a CSV or JSON file.

```sql
CREATE TABLE lead (
  id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  workspace_id  UUID NOT NULL REFERENCES workspace(id),
  name          TEXT,                -- extracted: business or contact name
  contact_uri   TEXT,                -- extracted: phone in E.164 (SMS MVP) or email
  contact_type  TEXT,                -- mobile | landline | toll_free | unknown | email
  category      TEXT,                -- extracted: business type / industry
  address       TEXT,                -- extracted: physical address if present
  website       TEXT,                -- extracted: website URL if present
  raw_data      JSONB NOT NULL DEFAULT '{}',
                                     -- all columns not mapped to core fields
                                     -- e.g. reviews, ratings, notes, custom columns
  import_order  INTEGER NOT NULL,    -- global sequence across all imports; drives FIFO
  source_file   TEXT,                -- filename of the originating import
  status        TEXT NOT NULL DEFAULT 'new',
                                     -- new | contacted | replied | won | lost | skipped
  skip_reason   TEXT,                -- populated when status='skipped' at import time
                                     -- e.g. 'not_a_mobile_number', 'no_contact_uri'
  created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),

  CONSTRAINT lead_contact_uri_workspace_unique UNIQUE (workspace_id, contact_uri)
);

CREATE INDEX idx_lead_workspace_status ON lead(workspace_id, status);
CREATE INDEX idx_lead_import_order ON lead(workspace_id, import_order);
```

**Notes:**
- `contact_uri` is the deduplication key. Within a workspace, two leads cannot share
  the same `contact_uri`. This prevents duplicate outreach to the same number or
  email. See section 5 (Re-import & Merge) for how conflicts are handled.
- **Phone numbers are normalised to E.164 at import time** (e.g. `(07) 5495 4233`
  with region hint `AU` becomes `+61754954233`). Normalisation happens before
  deduplication so equivalent numbers in different formats collapse correctly.
  Rows whose `contact_uri` cannot be parsed into E.164 are skipped with reason
  `'invalid_phone_format'`.
- `contact_type` is detected at import via `phonenumbers.number_type`. Only
  `mobile` numbers are eligible for SMS outreach in the MVP — landlines, toll-free,
  and unknown types are imported with `status='skipped'` and `skip_reason`
  populated. The owner can override by changing `status` to `new` manually if their
  gateway supports landline delivery.
- `import_order` is a monotonically increasing integer assigned at import time.
  Campaigns process leads in `import_order` ascending. Owners control priority by
  ordering rows in their CSV before uploading.
- `raw_data` is the AI's context window for personalisation. It stores everything
  not mapped to a core field — review text, ratings, extra columns, enrichment
  data added by future agents.
- Core fields (`name`, `contact_uri`, etc.) may be NULL if the import file did not
  contain mappable data. A lead without a `contact_uri` cannot be contacted and will
  be flagged during import validation.

---

### 3.3 ImportJob

Tracks the lifecycle of a single file import. Stores the column mapping so it can
be reused for future imports of files with similar structure.

```sql
CREATE TABLE import_job (
  id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  workspace_id    UUID NOT NULL REFERENCES workspace(id),
  filename        TEXT NOT NULL,
  file_type       TEXT NOT NULL,     -- csv | json
  status          TEXT NOT NULL DEFAULT 'pending',
                                     -- pending | awaiting_confirmation | processing
                                     --   | complete | failed
  mapping_config  JSONB,             -- column-to-field mapping decided during this import
                                     -- saved for reuse on similar future imports
  row_count       INTEGER,           -- total rows parsed from file
  imported_count  INTEGER DEFAULT 0, -- rows successfully written to lead table
  skipped_count   INTEGER DEFAULT 0, -- rows skipped (duplicate contact_uri, no contact)
  error_count     INTEGER DEFAULT 0, -- rows that failed with a parsing error
  errors          JSONB DEFAULT '[]', -- array of { row, reason } for failed rows
  created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
```

**Notes:**
- The import flow pauses at `awaiting_confirmation` while the owner reviews and
  confirms the field mapping in the UI. Processing does not begin until confirmed.
- `mapping_config` is used by the field mapping agent as prior art when a new file
  is uploaded with similar column names. See section 4.3.
- Errors are stored as a JSON array so the frontend can display a per-row error
  report after import.

---

### 3.4 Campaign

A named outreach effort with a goal, daily limit, and assigned connector.

```sql
CREATE TABLE campaign (
  id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  workspace_id      UUID NOT NULL REFERENCES workspace(id),
  name              TEXT NOT NULL,
  goal              TEXT NOT NULL,   -- plain text; e.g. "Book a 15-minute discovery call"
  outreach_per_day  INTEGER NOT NULL DEFAULT 50,
  connector_type    TEXT NOT NULL DEFAULT 'android_sms',
                                     -- android_sms | email (future)
  status            TEXT NOT NULL DEFAULT 'draft',
                                     -- draft | active | paused | completed
  created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);
```

**Daily quota enforcement (rolling 24h):**

`outreach_per_day` is enforced as a rolling 24-hour window, not a calendar day.
On every scheduler tick, the system queries:

```sql
SELECT COUNT(*)
  FROM message m
  JOIN thread t           ON t.id = m.thread_id
  JOIN campaign_lead cl   ON cl.id = t.campaign_lead_id
 WHERE cl.campaign_id = :campaign_id
   AND m.role = 'ai'
   AND m.created_at >= now() - INTERVAL '24 hours';
```

The scheduler sends at most `outreach_per_day - sent_last_24h` new first-contact
messages per campaign per tick, subject to the per-tick batch cap and the
minimum inter-send delay defined in `workspace.settings`. Reply messages are not
counted toward the daily quota — only first-contact outreach is rate-limited.

Rolling 24h is chosen over calendar-day to avoid midnight burst behaviour and to
make the limit predictable regardless of when the owner activates the campaign.

---

### 3.5 CampaignLead

Join table between a campaign and its leads. Tracks the per-campaign status of each
lead independently of the lead's global status.

```sql
CREATE TABLE campaign_lead (
  id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  campaign_id     UUID NOT NULL REFERENCES campaign(id),
  lead_id         UUID NOT NULL REFERENCES lead(id),
  queue_position  INTEGER NOT NULL,  -- copied from lead.import_order at assignment time
                                     -- determines processing order within this campaign
  status          TEXT NOT NULL DEFAULT 'queued',
                                     -- queued | contacted | replied | won | lost | skipped
  created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),

  CONSTRAINT campaign_lead_unique UNIQUE (campaign_id, lead_id)
);

CREATE INDEX idx_campaign_lead_status ON campaign_lead(campaign_id, status, queue_position);
```

**Notes:**
- A lead can exist in multiple campaigns (e.g. an SMS campaign and a future email
  campaign) with independent statuses.
- The cron job queries `campaign_lead` for `status = 'queued'` ordered by
  `queue_position ASC`, limited to the campaign's `outreach_per_day` count.

---

### 3.6 Thread

An ongoing conversation between AutoSDR and a single lead via a specific connector.

```sql
CREATE TABLE thread (
  id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  campaign_lead_id    UUID NOT NULL REFERENCES campaign_lead(id),
  connector_type      TEXT NOT NULL,
  status              TEXT NOT NULL DEFAULT 'active',
                                     -- active | paused | paused_for_hitl | won | lost | skipped
  auto_reply_count    INTEGER NOT NULL DEFAULT 0,
  angle               TEXT,          -- personalisation hook extracted at thread creation;
                                     -- reused verbatim for all replies in this thread unless
                                     -- the owner triggers a refresh
  tone_snapshot       TEXT,          -- copy of workspace.tone_prompt at thread creation time;
                                     -- guarantees tone consistency if the owner updates tone
                                     -- mid-campaign
  hitl_reason         TEXT,          -- populated when escalated; e.g. "low_confidence"
  hitl_context        JSONB,         -- additional HITL payload:
                                     --   eval failure: { last_drafts: [...], last_feedback: ..., attempts: 3 }
                                     --   classifier:   { intent, confidence, reason, incoming_message }
  created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_thread_status ON thread(status);
CREATE INDEX idx_thread_campaign_lead ON thread(campaign_lead_id);
```

**Notes:**
- One thread per `campaign_lead`. A lead in two campaigns will have two separate threads.
- `auto_reply_count` is incremented on every AI-generated reply. When it reaches
  the workspace `max_auto_replies` setting, the thread is escalated to HITL.
- `angle` is written once, at thread creation, by the lead analysis agent. It is
  not regenerated on follow-up messages unless the owner explicitly requests a
  refresh from the thread panel. Storing the angle on the thread means reply
  generation does not re-hit the analysis agent.
- `tone_snapshot` is a copy of `workspace.tone_prompt` taken at thread creation.
  Threads already in progress when the owner recalibrates tone continue to use
  the tone they were started with — this prevents jarring mid-conversation tone
  shifts. New threads pick up the new tone.
- `hitl_reason` is a short machine-readable code (e.g. `'low_confidence'`,
  `'bot_check'`, `'eval_failed_after_3_attempts'`). `hitl_context` carries the
  payload needed for the owner to act — for eval failures, the rejected drafts
  and the evaluator's feedback; for classifier escalations, the raw classifier
  output.
- **Multi-campaign reply routing rule:** When an incoming message's `contact_uri`
  matches a lead that exists in multiple campaigns (and therefore has multiple
  threads), the reply is routed to the thread whose most-recent outbound
  (`message.role='ai'`) `created_at` is the most recent. If no outbound message
  has been sent on any thread, the reply is routed to the oldest thread. If no
  active thread exists, the message is written to `unmatched_webhook`.
- **Concurrency:** Reply processing acquires a thread-level lock (Postgres
  `SELECT ... FOR UPDATE`; SQLite `BEGIN IMMEDIATE`) before classification. If a
  second inbound message arrives while the first is being processed, it waits for
  the lock and then re-reads the full message history — which now includes the
  first inbound — before classifying. This prevents double-replying to the same
  lead.

---

### 3.7 Message

An immutable log of every message in a thread. Never updated, only appended.

```sql
CREATE TABLE message (
  id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  thread_id   UUID NOT NULL REFERENCES thread(id),
  role        TEXT NOT NULL,         -- ai | human | lead
                                     -- ai: generated by AutoSDR
                                     -- human: typed by the owner in the chat panel
                                     -- lead: received from the lead via connector webhook
  content     TEXT NOT NULL,
  metadata    JSONB NOT NULL DEFAULT '{}',
                                     -- ai messages: { tokens_used, eval_score, eval_attempts,
                                     --                angle_used, model }
                                     -- lead messages: { raw_webhook_payload, received_at }
                                     -- human messages: { sent_via_connector: bool }
  created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_message_thread ON message(thread_id, created_at ASC);
```

**Notes:**
- Messages are never soft-deleted or updated. The full conversation history is always
  preserved for auditing, HITL context, and future model evaluation.
- `metadata` on AI messages stores the self-evaluation score and the number of
  generation attempts. This data feeds future quality reporting.
- The `angle_used` field in metadata stores the personalisation hook the LLM
  extracted from `raw_data`. This is useful for understanding why a particular
  message was drafted.
- AI and auto-reply messages also carry `llm_call_id` (and `llm_call_eval_id`
  for generations) so each outbound SMS is traceable to the exact prompts that
  produced it via a join against `llm_call`.
- Inbound (`role='lead'`) messages store the connector's
  `provider_message_id` in metadata so the poller can detect replays if the
  connector ever re-delivers an already-processed SMS.

---

### 3.8 LlmCall

Persistent log of every LLM invocation — successful, failed, or retried. One
row per attempt. This is the primary review surface for POC iteration and is
the data behind `autosdr logs llm` / `autosdr logs thread`.

```sql
CREATE TABLE llm_call (
  id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),

  -- context: which pipeline event produced this call
  workspace_id      UUID,
  campaign_id       UUID,
  thread_id         UUID,
  lead_id           UUID,

  -- call shape
  purpose           TEXT NOT NULL,
                    -- analysis | generation | evaluation | classification | other
  model             TEXT NOT NULL,       -- e.g. "gemini/gemini-3-flash-preview"
  prompt_version    TEXT,                -- e.g. "generation-v1"
  temperature       REAL,
  attempt           INTEGER NOT NULL DEFAULT 1,
  response_format   TEXT NOT NULL DEFAULT 'text',  -- text | json

  -- prompts + response (truncated to LLM_LOG_MAX_PROMPT_CHARS)
  system_prompt     TEXT,
  user_prompt       TEXT,
  response_text     TEXT,
  response_parsed   JSONB,

  -- telemetry
  tokens_in         INTEGER NOT NULL DEFAULT 0,
  tokens_out        INTEGER NOT NULL DEFAULT 0,
  latency_ms        INTEGER NOT NULL DEFAULT 0,
  error             TEXT
);

CREATE INDEX idx_llm_call_created_at ON llm_call(created_at);
CREATE INDEX idx_llm_call_thread     ON llm_call(thread_id, created_at);
CREATE INDEX idx_llm_call_purpose    ON llm_call(purpose, created_at);
```

**Notes:**
- Context IDs (`workspace_id`, `campaign_id`, `thread_id`, `lead_id`) are
  nullable because some calls — e.g. a standalone analysis call that happens
  before any thread exists — are only partially scoped.
- `response_parsed` is written for successful JSON calls; it is backfilled
  after parsing so the row exists for audit even if parsing initially failed
  and a retry happened.
- Every row is mirrored to `data/logs/llm-YYYYMMDD.jsonl` (one NDJSON line
  per call) so the owner can grep/jq the log without DB access. Set
  `LLM_LOG_ENABLED=false` to disable both sinks; in-memory token counters
  (`autosdr status`) are unaffected.
- `attempt` differentiates self-heal retries. A generation that took three
  tries produces three rows with `purpose='generation'` and `attempt` 1/2/3.
- `error` is populated on exceptions (provider error, parse failure after
  retries, quota exhaustion). Non-null `error` rows with null `response_text`
  are the "failed call" set surfaced by `autosdr logs llm --errors`.

---

## 4. Lead Import Pipeline

### 4.1 Supported Formats

| Format | Notes |
|---|---|
| CSV | Standard comma-separated; UTF-8 encoding expected; headers required in first row |
| JSON | Array of objects `[{...}, {...}]` or newline-delimited JSON (one object per line) |

Unstructured text (plain paragraphs, PDFs, copied web content) is not supported.
The import endpoint will return a 400 error with a clear message if a non-structured
file is uploaded.

### 4.2 Import Flow

```
[Owner uploads file via frontend]
          ↓
[Backend] Parse file → extract headers + sample rows (first 5 rows)
          ↓
[Backend] Validate: does the file have at least one column that could be a contact?
          If not → reject with error before agent call
          ↓
[Agent]  Field mapping pass (see section 4.3)
          ↓
[Frontend] Display mapping for owner confirmation
           Owner adjusts any incorrect mappings
           Owner confirms
          ↓
[Backend] ImportJob status → processing
          ↓
[Backend] For each row:
           1. Extract core fields using confirmed mapping
           2. Build raw_data blob from remaining columns
           3. Check for existing lead with same contact_uri
              → New lead: insert with next import_order value
              → Existing lead: merge raw_data (see section 5)
           4. Update imported_count / skipped_count / error_count
          ↓
[Backend] ImportJob status → complete
[Frontend] Show import summary (imported, skipped, errors)
```

### 4.3 Field Mapping Agent

The mapping agent is a fast LLM call that analyses the file's column names and
sample values to suggest which columns map to core lead fields.

**Input to agent:**
```json
{
  "core_fields": ["name", "contact_uri", "category", "address", "website"],
  "columns": [
    { "name": "biz_name",   "samples": ["Arcare Caboolture", "BlueCare Caloundra", "Regis Redlynch"] },
    { "name": "phone",      "samples": ["(07) 5490 0100", "(07) 5490 5198", "1300 998 100"] },
    { "name": "category",   "samples": ["Aged Care Service", "Retirement community", "Nursing home"] },
    { "name": "rating",     "samples": [4, 4.1, 4.9] },
    { "name": "reviewText", "samples": ["Staff are wonderful...", "Great facility...", "Mum loves it..."] }
  ],
  "prior_mappings": [...]  -- mapping_config from previous similar imports, if any
}
```

**Required output (strict JSON):**
```json
{
  "mappings": [
    { "column": "biz_name",   "maps_to": "name",        "confidence": 0.97 },
    { "column": "phone",      "maps_to": "contact_uri", "confidence": 0.99 },
    { "column": "category",   "maps_to": "category",    "confidence": 0.95 },
    { "column": "rating",     "maps_to": null,           "confidence": 1.0  },
    { "column": "reviewText", "maps_to": null,           "confidence": 1.0  }
  ]
}
```

**Mapping rules:**
- A column with `maps_to: null` goes into `raw_data` — this is not an error.
- Any column with `confidence < 0.80` is flagged in the UI as needing owner review.
  Pre-filled with the agent's suggestion but highlighted for attention.
- Columns with `confidence >= 0.80` are pre-filled and shown as confirmed, but the
  owner can still change them before submitting.
- If two columns are suggested for the same core field (e.g. both `phone` and `mobile`
  map to `contact_uri`), the higher-confidence mapping wins and the other goes to
  `raw_data`. The owner can override this.
- The confirmed mapping is saved as `mapping_config` on the ImportJob for reuse.

### 4.4 import_order Assignment

`import_order` is a per-workspace monotonically increasing integer. It is assigned
at row insertion time using a simple sequence, not derived from the file row number.

This means:
- The first import of 1,000 leads gets positions 1–1,000.
- A second import of 500 leads gets positions 1,001–1,500.
- Leads from the second import are always processed after leads from the first.
- Owners who want the second batch prioritised over the first should archive the
  first campaign and create a new one using only the second import.

---

## 5. Re-import & Merge Logic

Owners may upload a second file that contains leads already in the system — for
example, enriched data from a different source, or an updated export with new review
counts.

Deduplication is on `(workspace_id, contact_uri)`. For this to work across sources
that format phone numbers differently (e.g. `(07) 5495 4233` vs `07 5495 4233` vs
`+61754954233`), all incoming phone numbers are **normalised to E.164 before the
deduplication check**:

```
Raw value                Normalised (region hint: AU)
(07) 5495 4233      →    +61754954233
07 5495 4233        →    +61754954233
+61 7 5495 4233     →    +61754954233
0754954233          →    +61754954233
1800 692 273        →    +611800692273
```

Normalisation uses `phonenumbers.parse(value, region_hint)` followed by
`phonenumbers.format_number(..., E164)`. The region hint defaults to the workspace
setting `default_region` (default: `AU`). Rows whose value cannot be parsed are
skipped with reason `'invalid_phone_format'` and recorded in the import job's
error array.

**When a row's `contact_uri` matches an existing lead:**

1. Core fields (`name`, `category`, `address`, `website`) are updated if the new
   value is non-null and the existing value is null. Existing non-null core fields
   are not overwritten. This prevents a bad import from clobbering clean data.
2. `raw_data` is merged at the key level. New keys are added. Existing keys are
   updated with the new value. No keys are deleted.
3. Thread history is never touched. All messages are preserved.
4. `import_order` is not changed. The lead keeps its original queue position.
5. The ImportJob records this row as `skipped_count` if no fields changed, or
   `imported_count` if at least one field was updated.

**When a row has no `contact_uri`:**
- The row cannot be deduplicated and cannot be contacted.
- It is always recorded in `skipped_count` with reason `"no_contact_uri"`.
- The import summary in the frontend displays these rows so the owner can investigate.

---

## 6. raw_data Blob Strategy

`raw_data` is a JSONB column on the `lead` table. It is the primary source of
personalisation context for the AI.

**What goes in:**
- All columns from the import file not mapped to a core field.
- Enrichment data added by future agents (e.g. website scrape summary, LinkedIn data).
- Additional structured data from subsequent imports of the same lead.

**Example:**
```json
{
  "rating": 4.0,
  "review_count": 12,
  "reviews": [
    { "author": "Judy Vella", "rating": 2, "text": "The food is disgusting..." },
    { "author": "corinna deVeth", "rating": 5, "text": "Mum spent her last months here..." }
  ],
  "plus_code": "WXF6+PQ Caboolture",
  "search_query": "Aged Care Facility Caboolture QLD"
}
```

**How the AI reads it:**

The full `raw_data` blob is serialised to a JSON string and included in the lead
analysis prompt. The analysis agent extracts a single `angle` — a 1–3 sentence
personalisation hook — which is then passed to the message generation agent.

This two-step approach (analyse then generate) keeps the generation prompt focused
and prevents the full blob from inflating every message generation call.

**Size limit:**

`raw_data` blobs larger than 50KB are truncated before being sent to the analysis
agent. Truncation removes the longest string values first (e.g. full review text is
summarised). A truncation flag is stored in `message.metadata` when this occurs.

---

## 7. Entity Relationship Summary

```
workspace
  └── lead (workspace_id)
        └── campaign_lead (lead_id)
              └── thread (campaign_lead_id)
                    └── message (thread_id)

workspace
  └── campaign (workspace_id)
        └── campaign_lead (campaign_id)

workspace
  └── import_job (workspace_id)

llm_call  (soft-referenced by workspace_id / campaign_id / thread_id / lead_id)
unmatched_webhook  (workspace_id)
```

Key relationships:
- A `lead` belongs to one `workspace`.
- A `lead` can be in many `campaigns` via `campaign_lead`.
- Each `campaign_lead` has at most one `thread`.
- A `thread` has many `messages`.
- An `import_job` is a workspace-scoped record of a single file upload lifecycle.
- `llm_call` rows carry soft references (no FKs) to the owning entities so the
  log survives thread / campaign / lead deletion for retrospective analysis.

---

## 8. Data Decisions Log

Decisions recorded here so future contributors understand the reasoning.

| Decision | Rationale |
|---|---|
| UUID primary keys everywhere | Avoids sequential ID leakage; safe for future multi-tenant or distributed use |
| Deduplication on `contact_uri` not `name` | Names are inconsistent across sources; phone/email is authoritative |
| `raw_data` as JSONB not separate table | Avoids schema migrations every time a new data source is added; JSONB is queryable if needed |
| Core fields not overwritten on re-import | Prevents a low-quality second import clobbering clean data from a first import |
| `import_order` is global, not per-file | Ensures FIFO ordering is consistent across multiple imports without gaps or resets |
| Messages are immutable (no soft delete) | Full audit trail; safe HITL handoff; needed for future model evaluation |
| `mapping_config` saved on ImportJob | Allows agent to reuse prior mappings for similar files; reduces confirmation friction over time |
| CSV and JSON only | Unstructured text has no reliable schema extraction path; honest constraint |
| Daily quota is rolling 24h, not calendar day | Avoids midnight burst behaviour; predictable regardless of when the campaign starts; a single query against `message` is the source of truth (no separate counter to drift) |
| Phone numbers stored in E.164 only | One canonical representation; deduplication survives input-format differences; `phonenumbers.number_type` lets us detect landline vs mobile deterministically |
| Non-mobile leads skipped at import by default | Landlines and toll-free numbers burn gateway quota and time without succeeding; surfaces the problem in the import summary where the owner can act on it |
| Reply routing prefers most-recent outbound | When a lead is in multiple campaigns, the owner's latest touch is the most likely context for the reply; deterministic tie-break on thread.id |
| Thread-level lock on reply processing | Prevents double-replies under concurrent inbound messages; the second processor re-reads history including the first inbound before classifying |
| SQLite for POC, Postgres for v1 | Same SQLAlchemy models; zero infra for POC; swap is a URL change |
| `llm_call` log has soft FKs | The log is an audit surface that must survive lead/thread deletion; hard FKs would cascade the history away |
| One `llm_call` row per attempt | Self-heal retries need to be reviewable independently; aggregating loses the "why did we retry" signal that prompt refinement depends on |
| JSONL mirror of `llm_call` | Flat file is trivially grep/jq-able; DB row enables cross-thread aggregation via SQL; both are cheap |

---

## 9. Document Map

| Doc | Title | Status |
|---|---|---|
| Doc 1 | Product Overview | Draft |
| Doc 2 | **Data Architecture Spec** (this document) | Draft |
| Doc 3 | AI & Messaging Spec — tone calibration, generation pipeline, self-evaluation, intent classification, connector interface | Draft |
| Doc 4 | Onboarding & Config Spec — POC CLI flow, v1 wizard, API key setup, gateway setup, PWA install, kill switch | Draft |
