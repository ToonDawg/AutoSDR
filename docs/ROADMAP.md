# AutoSDR Roadmap

**Last updated:** 2026-04-26 (ticket 0001 shipped — STOP / opt-out keywords)
**Maintainer:** project-manager skill (see `.claude/skills/project-manager/SKILL.md`)

This is the canonical roadmap. Tickets get fleshed out below or in dedicated
files under `docs/tickets/`. Beads (`bd`) is supported as an optional graph
tracker — see `.claude/skills/project-manager/references/beads-integration.md`
— but this document is the source of truth.

> **How to read this:** items are grouped by horizon. Within each group they
> are ranked by RICE score (highest first). Each item: title • problem in one
> line • RICE (or "—") • status • link.

> **Release context:** as of 2026-04-26, the React/Vite operator console has
> shipped (`Dashboard`, `Inbox`, `Threads`, `Leads`, `Campaigns`, `Logs`,
> `Settings`, `Setup` wizard) plus the follow-up beat
> (`autosdr/pipeline/followup.py`). `ARCHITECTURE.md` § 14 still says "no
> frontend" — see Now/Doc-sync. The operator (you) is staging a 323 MB QLD
> Google-Maps NDJSON dump (`all_results_qld.json`, untracked) which is the
> proximate cause of the import-UX item below.

---

## Now — in progress (≤ 4 items)

| Title | Problem | RICE | Owner | Link |
| --- | --- | --- | --- | --- |
| _(none — pick the top of Next)_ | — | — | — | — |

---

## Next — committed for next quarter

Ranked by RICE.

| Title | Problem | RICE | Status | Link |
| --- | --- | --- | --- | --- |
| [Logs] Surface reply-rate per personalisation angle | Operators can't tell which angle (`stale_info`, `weak_online_presence`, `signature_amenity`, etc.) actually produces replies. `Thread.angle` is already persisted — this is one query + one chart. | 10.0 | ready | [`docs/tickets/0002-reply-rate-by-angle.md`](tickets/0002-reply-rate-by-angle.md) |
| [Campaigns] Per-campaign funnel: queued → sent → replied → won/lost | The Dashboard has a 14-day send sparkline only (`autosdr/api/stats.py`). Operators can't answer "is this campaign actually working?" without grepping the DB. | 10.0 | ready | [`docs/tickets/0003-campaign-funnel.md`](tickets/0003-campaign-funnel.md) |
| [Imports] Field-mapping helper for non-canonical lead files | The fixed alias map (`autosdr/importer.py:_CORE_ALIASES`) drops anything it doesn't recognise into `raw_data`. Real lead sources (e.g. the 323 MB Apify Google-Maps NDJSON in repo root) ship `plusCode`, `reviewDetails`, `webResults`, `searchQuery`, `scrapedAt` — none mapped, all hitting the per-lead byte ceiling in analysis. | 8.0 | ready | [`docs/tickets/0004-import-field-mapping.md`](tickets/0004-import-field-mapping.md) |
| [PWA] Install + Web Push for HITL escalations | Doc1 § 2 + § 6 both treat PWA + Web Push as the control surface; success metric: "< 10s from HITL escalation event". Reality is poll-based React on a laptop. Owner has to keep a tab open or miss escalations. | 8.0 | ready | [`docs/tickets/0005-pwa-web-push.md`](tickets/0005-pwa-web-push.md) |
| [Docs] Sync `ARCHITECTURE.md` with as-built | § 14 still says "Any frontend or PWA" is out of scope (frontend has shipped). § 3 component map omits `pipeline/followup.py`, `pipeline/suggestions.py`, `quota.py`, `workspace_settings.py`. The PM skill's forecasts assume this doc is accurate. | 2.5 | ready | _(self-contained chore — inline)_ |
| [Repo] Actually ignore `all_results_qld.json` | Last commit (`470345c`) message claims it added the file to `.gitignore`; it didn't (`/.gitignore` reviewed 2026-04-26). 323 MB of real lead data sitting untracked → one `git add .` from being committed. | — | ready | _(one-line fix; rolled into Docs sync)_ |

---

## Later — high-confidence, not yet committed

| Title | Problem | RICE | Status | Link |
| --- | --- | --- | --- | --- |
| [Onboarding] Swipe-based tone calibration | Spec'd in doc4; success metric "≥ 10 swipe decisions compile a `tone_prompt` without manual editing". Currently `tone_prompt` is a free-text field on the Setup wizard. Risk: voice goes generic at scale. | 0.8 | spike-first | _(do a 1-day prompt-design spike before sizing the L build)_ |
| [Imports] Streaming NDJSON / large-file ingest | The 323 MB QLD file would currently load fully into memory in `importer.py`. Once the field-mapping ticket lands, large-file mode is the obvious next concern. | 3.75 | scoping | _(blocked by Field-mapping)_ |
| [AI] A/B compare two personalisation angles per lead | Logs already record angle, draft, score. Doubling the analysis call to pick from two angles before generation would let the evaluator compare and surface "which angle wins" data over time. | 4.0 | spike-first | _(uses existing audit log; needs prompt-design spike)_ |
| [Connectors] Push-based inbound for TextBee | Today TextBee is poll-only; SMSGate already pushes. Sub-second reply latency matters more once Web Push lands (otherwise the notification beats the message into the DB). | 1.0 | spike-first | _(blocked by TextBee API surface; spike)_ |
| [Connectors] Delivery-receipt support on `BaseConnector` | Operator can't currently tell "did the SMS actually deliver?" — only that the connector accepted the send. Needed before any "after N days, follow up" automation. | 1.0 | spike-first | — |
| [AI] Business-data extraction agent at setup | Doc4 spec'd; today the operator's free-text business description is shoved into every generation prompt verbatim. A structured extract (offers, credentials, geographies, signature line) would tighten generation. | — | not-scored | _(score when promoting)_ |

---

## Considered, not committed

<details>
<summary>Click to expand (low-priority backlog)</summary>

| Title | Problem | RICE | Why deferred |
| --- | --- | --- | --- |
| [Connectors] Email connector | A second channel would unlock a second persona slice. | — | **Non-goal** for POC (`autosdr-doc1` § 5). Strategic shift; needs explicit user sign-off. |
| [Stack] Postgres / Redis / Celery scale path | Spec'd as the v1 scale stack. | — | Not load-bound today (single-operator, SQLite + asyncio handles current volume). Revisit when an operator hits real concurrency limits. |
| [AI] Lead scoring / prioritisation | Today: FIFO by import order. | — | **Non-goal** for POC. Operators currently sort their CSV. |
| [Connectors] httpSMS as a third Android gateway | Drop-in via `BaseConnector`. | — | No operator asking; TextBee + SMSGate cover both hosted and self-hosted. |
| [UI] Mobile / responsive layout below 1024px | README explicitly says "laptop UI". | — | Reasonable trade-off until PWA + Push lands; revisit then. |

</details>

---

## Done — last 90 days

Most-recent first.

| Title | Date | Ref | Note |
| --- | --- | --- | --- |
| **0001 — Honour STOP / opt-out keywords on inbound (deterministic)** | 2026-04-26 | [ticket](tickets/0001-stop-opt-out-keywords.md) | Inbound STOP / UNSUBSCRIBE / REMOVE ME / OPT OUT / CANCEL / END / QUIT (case-insensitive, word-boundary, third-party denylist) now short-circuits the LLM classifier, closes the thread lost, and flags the lead `do_not_contact` permanently. Outbound + assignment + importer all honour the flag. New `autosdr leads opt-out --yes` CLI for off-channel opt-outs. 252 backend tests pass; frontend `tsc --noEmit` clean. |
| Follow-up beat (second casual SMS, ~10s after first contact) | 2026-04-26 | `470345c` | Operator can configure a per-campaign template + delay; second message reads as "remembered one more thing", scheduled with kill-switch-aware backoff. |
| HITL inbox + Threads UI | 2026-04-26 | `470345c` | Inbox surfaces threads paused for human attention; HITL dismiss flow shipped (`tests/test_hitl_dismiss.py`). |
| LeadDetail + CampaignDetail routes | 2026-04-26 | `470345c` | Per-lead and per-campaign deep-link views land on the operator console. |
| Test-mode rehearsal: dry-run + override | _(initial)_ | `672c0a6` | `--dry-run` + `--override-to` for safe rehearsal against real LLM, fake (or one-recipient) connector. |
| Killswitch with three layers (signal / flag / CLI) | _(initial)_ | `672c0a6` | Pause < 1s; covered in `tests/test_killswitch.py`. |
| Audit log of every LLM call (DB + JSONL) | _(initial)_ | `672c0a6` | `llm_call` rows + `data/logs/llm-YYYYMMDD.jsonl`; viewable via `autosdr logs llm` and the `/Logs` route. |

---

## Decisions log

Append-only. One bullet per material call.

- **2026-04-26** — Initialised the roadmap document. Chose to use it as the
  source of truth (markdown) per the PM skill default; `bd` not adopted at
  this time. Rationale: single-operator project, no tracker friction
  acceptable. (Source: project-manager skill, this session.)
- **2026-04-26** — Treated the 323 MB `all_results_qld.json` Apify dump as
  evidence (not a one-off) for the import-UX item. Operator pain is real and
  current. Rationale: untracked-but-present file in repo root + non-canonical
  field shape (`reviewDetails`, `plusCode`, `webResults`).
- **2026-04-26** — Wrote tickets 0001 – 0005 against the top of `Next`.
  Sequencing in the Top-3 justification holds: 0001 first (compliance / risk),
  then 0002 + 0003 in either order (cheap, additive), then 0004 (operator
  pain), then 0005 (largest investment). Each ticket lists its open
  questions; resolving them is gating before implementation can begin.
- **2026-04-26** — Shipped ticket 0001 (STOP / opt-out keywords). Synthetic
  `LlmCall` row used as the audit surface (sentinel `model="(deterministic-opt-out)"`)
  rather than introducing a `routing_event` table — Pragmatist verdict from
  the council. Caveat: any future LLM cost aggregate must filter on the
  sentinel or migrate to a dedicated table; tracked as a follow-up to land
  with the delivery-receipt ticket. Recorded follow-ups: Settings →
  Compliance card, "Clear DNC flag" UI affordance.

---

## Out of scope (current POC)

Mirror of `autosdr-doc1-product-overview.md § 5`. Update when the source doc
updates. These are pre-approved future-work candidates; **moving an item from
here into Now/Next requires explicit user sign-off** because it's a strategy
shift, not a normal prioritization call.

- Unstructured-text lead imports
- Website scraping / lead enrichment agents
- Multi-tenancy / SaaS / billing
- iOS SMS integration
- Email connector
- CRM integrations
- AI lead scoring / prioritization
- Conversational config UI
- LLM fine-tuning
