# AutoSDR — Product Context

The PM agent's mental model of the product. Read this every session. Update it when `README.md`, `ARCHITECTURE.md`, or `autosdr-doc1-product-overview.md` change in ways that affect strategy or principles.

If anything here disagrees with `ARCHITECTURE.md`, `ARCHITECTURE.md` wins for as-built behaviour and `autosdr-doc1-product-overview.md` wins for intended product direction. This file is a *summary* — go to source for any decision.

---

## 1. What AutoSDR is

An **autonomous SDR for small business owners**, self-hosted and open-source. The operator gives it a lead list, a tone, and a campaign goal. AutoSDR drafts the first SMS, sends via an Android phone gateway, classifies replies, and either auto-responds or hands the thread back to the operator.

Single FastAPI process. SQLite. LiteLLM (Gemini default). React/Vite/Tailwind UI served from the same port. Connectors: TextBee, SMSGate, or a local file connector for dev.

The currently-shipped default is **first-message-only**: AutoSDR sends the first outreach message, then every reply lands in HITL with 2–3 AI-drafted candidate replies. Auto-reply is a flag, not a default.

---

## 2. Persona

**The Time-Poor Founder.** Owner-operator of a 1–10-person business doing their own sales. Has a clear value prop, a list of leads, no time. Comfortable installing a self-hosted tool if the README is good. Not a developer; not running multi-tenant SaaS.

Goals: upload leads → outreach starts → notified when a thread needs them → trust the voice → can pause / take over / skip at any point.

Frustrations: generic blast tools that burn reputation, CRM-dependent stacks, black-box automations.

**Keep this persona in mind for every prioritization call.** "What does the time-poor founder need next?" is the right question. "What would an enterprise SDR team need?" is the wrong one.

---

## 3. Principles (the principle filter)

Every PM recommendation must pass these. When two principles conflict, name the trade-off; don't paper over it.

1. **Simplicity first.** Defer features that don't hurt the core workflow.
2. **Quality over speed.** A 60-second message that resonates beats a 2-second message that reads as spam. Async is fine.
3. **Honest data contracts.** Structured input only (CSV, JSON). Don't pretend to support what we can't reliably handle.
4. **Extensible by design.** New connectors, LLM providers, enrichment agents must drop in without rewriting core logic.
5. **Human always wins.** Never send when uncertain. Escalate cleanly. Never block the human from taking over.
6. **The owner stays in control.** Every automated action is pausable, resumable, overridable from the UI.

A feature that violates one of these is **not a candidate** — surface the conflict to the user instead of trying to make it fit.

---

## 4. Non-goals (POC) — pre-approved future work

These are explicitly out of scope today. They are **the natural candidate pool** when forecasting "what's next?"

- Unstructured-text lead imports.
- Website scraping / lead enrichment agents.
- Multi-tenancy / SaaS / billing.
- iOS SMS integration (Apple ecosystem doesn't allow it without enterprise).
- Email connector.
- CRM integrations (HubSpot, Salesforce, Pipedrive).
- AI lead scoring / prioritization (today: FIFO based on import order).
- Conversational config UI.
- LLM fine-tuning.

Anything outside this list that would be a *strategic* shift (multi-user, enterprise, voice, WhatsApp) is not a non-goal — it's a pivot. Flag it and ask before scoping.

---

## 5. Spec-vs-code drift (PM-relevant)

Things in the specs (`autosdr-doc{1..4}-*.md`) that are **not yet built** in the as-built `ARCHITECTURE.md`. These are forecast candidates with no decision needed — the spec already approved them:

- **PWA install + Web Push notifications.** Spec'd in doc1; current state is poll-based UI refresh.
- **Swipe-based tone calibration.** Spec'd in doc4; current state is verbatim tone snapshot at init.
- **Field-mapping agent at import time.** Spec'd; current state is a fixed column schema with aliases.
- **Business-data extraction agent.** Spec'd; current state uses the raw business description as-is.
- **Postgres / Redis / Celery.** Spec'd as scale path; current state is SQLite + asyncio.
- **Push-based inbound from TextBee.** Connector abstraction supports it; current state polls.

Always check the spec docs before scoping any of these — the original framing may already answer half the open questions.

---

## 6. Success metrics (from `autosdr-doc1` § 6)

Use these as the "ties to a metric" check when justifying tickets.

| Metric | Target |
| --- | --- |
| Setup time | < 15 min for first-time user (business + tone + keys + first campaign) |
| Tone calibration | ≥ 10 swipe decisions compile a `tone_prompt` without manual editing |
| HITL routing accuracy | Intent classifier ≥ 90% on a 100-reply labelled set |
| Webhook ack | < 2s |
| Message quality | ≥ 85% pass on self-eval rubric |
| Lead import | 1k-row CSV/JSON ingested + mapped in < 60s |
| PWA notifications | < 10s from HITL escalation event |

A roadmap item that moves one of these metrics is high-leverage. One that doesn't should be justified by something else specific (operator cognitive load, error rate, etc.) — not "polish".

---

## 7. Architecture cheat-sheet (for ticket scoping)

| Surface | Where | Notes |
| --- | --- | --- |
| FastAPI routers | `autosdr/api/` | Pydantic schemas mirror frontend TS in `schemas.py`. |
| ORM models | `autosdr/models.py` | Status vocabularies are centralised here. |
| LLM wrapper | `autosdr/llm/client.py` | Persistent audit log + kill-switch + retries. |
| Prompts | `autosdr/prompts/` | Versioned per purpose. Bump version on any meaningful change. |
| Connectors | `autosdr/connectors/` | `BaseConnector` ABC: `send`, `parse_webhook`, `poll_incoming`. |
| Pipelines | `autosdr/pipeline/` | `outreach.py`, `reply.py`, `_shared.py` (generate-and-evaluate). |
| Scheduler | `autosdr/scheduler.py` | Two asyncio tasks: outreach tick + inbound poll. Rolling-24h quota. |
| Killswitch | `autosdr/killswitch.py` | Three layers: signals + flag file + CLI. Hot-path guards. |
| CLI | `autosdr/cli.py` | Typer. `init` / `import` / `campaign` / `run` / `logs` / `pause` / `resume` / `status` / `sim`. |
| Frontend | `frontend/src/routes/*` | React 19 + Vite. Routes: Dashboard, Inbox, Threads, Leads, Campaigns, Logs, Settings. |

**Hot paths to be careful with** (ticket should call out backward-compat and migration):

- `models.py` schema (SQLite migrations are manual today).
- Prompt versions (audit rows reference them — don't silently rewrite).
- `BaseConnector` ABC (third parties may extend it).
- Settings JSON shape in `workspace.settings` (hot-reloaded; no migration story).
- Killswitch checkpoints (every LLM/connector hot path).

---

## 8. What "done" looks like for a feature

Use this as the implicit DoD when writing tickets:

1. Behaviour spec'd in the ticket.
2. Code change with prompt-version bump (if prompts changed).
3. Pytest coverage — LLM/connector mocked.
4. UI surface (or CLI surface) updated if the feature is operator-visible.
5. `ARCHITECTURE.md` and/or `autosdr-doc*` updated if the change is structural.
6. `data/logs/` capture is meaningful for the new behaviour (no silent paths).
7. Killswitch respected on any new long-running operation.

If a ticket can't satisfy these, it's not ready to start.

---

## 9. Anti-patterns to call out in tickets

These crop up often in roadmap brainstorms and are usually wrong for AutoSDR:

- **"Add AI to X."** AutoSDR's AI loop is the moat — adding more LLM calls without a measurable quality lift is cost. Always justify token spend.
- **"Auto-reply more."** Default is HITL. Pushing more conversations through auto-reply violates "human always wins" unless there's a clear classifier-confidence story.
- **"Add a CRM integration."** A non-goal for the POC; ask before scoping.
- **"Multi-user."** Non-goal; would force auth, RBAC, tenancy. Pivot territory.
- **"Self-tuning prompts."** Out of scope — fine-tuning and online learning aren't on the table.
- **"Real-time websockets."** The PWA/Push spec is the agreed path; revisit only if Push proves inadequate.
