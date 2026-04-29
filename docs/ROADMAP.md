# AutoSDR Roadmap

**Last updated:** 2026-04-28 (decoupled enrichment from outreach hot path → background scan worker + `/scans` UI; live-tested the scraper, deferred Crawlee fallback as ticket 0012)
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
| **[Hardening] Reply pipeline must not hold the SQLite write transaction across LLM API awaits** | `process_incoming_message` opens one `session_scope()` and does every `await _classify_reply(...)` inside it. The LLM call audit-log writer (`_log_call`) opens a separate writer session that then blocks for 120s on `busy_timeout`, blocking the asyncio event loop and serialising every inbound. WAL ballooned to 365 MB during one rehearsal session. **Prod-push blocker**, identified 2026-04-27 evening. | — | ready | [`docs/tickets/0008-reply-pipeline-tx-across-await.md`](tickets/0008-reply-pipeline-tx-across-await.md) |
| **[Hardening] Killswitch must not silently drop inbound webhooks** | `autosdr/api/webhooks.py:45-47` — when the killswitch is on, any inbound SMS that lands during that window is gone (no DB row, no audit, no replay). HITL operator's mental model ("I always own the next reply") is silently violated. **Prod-push blocker**, identified 2026-04-27 evening when a real reply vanished mid-rehearsal. | — | ready | [`docs/tickets/0009-killswitch-inbound-replay.md`](tickets/0009-killswitch-inbound-replay.md) |
| [PWA] Install + Web Push for HITL escalations | Doc1 § 2 + § 6 both treat PWA + Web Push as the control surface; success metric: "< 10s from HITL escalation event". Reality is poll-based React on a laptop. Owner has to keep a tab open or miss escalations. | 8.0 | ready | [`docs/tickets/0005-pwa-web-push.md`](tickets/0005-pwa-web-push.md) |
| [Hardening] Override safety + connector E.164 guard + `autosdr e2e` rehearsal CLI | OverrideConnector's single-slot mapping can cross-talk under concurrent sends (real customer's thread receives the rehearsal reply); BaseConnector trusts `contact_uri` verbatim with no E.164 guard; pre-prod-push rehearsal is ~12 manual UI/curl steps. Identified during the 2026-04-27 prod-push rehearsal — see Addendum for findings 3–6 (SMSGate transport, DB bloat, simulator CLI). | — | ready | [`docs/tickets/0007-prod-hardening-override-and-e2e.md`](tickets/0007-prod-hardening-override-and-e2e.md) |
| [Docs] Sync `ARCHITECTURE.md` with as-built | § 14 still says "Any frontend or PWA" is out of scope (frontend has shipped). § 3 component map omits `pipeline/followup.py`, `pipeline/suggestions.py`, `quota.py`, `workspace_settings.py`. The PM skill's forecasts assume this doc is accurate. | 2.5 | ready | _(self-contained chore — inline)_ |
| [Repo] Actually ignore `all_results_qld.json` | Last commit (`470345c`) message claims it added the file to `.gitignore`; it didn't (`/.gitignore` reviewed 2026-04-26). 323 MB of real lead data sitting untracked → one `git add .` from being committed. | — | ready | _(one-line fix; rolled into Docs sync)_ |

---

## Later — high-confidence, not yet committed

| Title | Problem | RICE | Status | Link |
| --- | --- | --- | --- | --- |
| [AI/Scheduler] Lead prioritisation by enrichment quality | Skeptic's pushed framing from the 0011 brainstorm: FIFO wastes every downstream improvement on the wrong leads. Background enrichment worker + a binary "enriched-first" tier on `_next_queued_leads`. Sized after 0011 ships and the angle-funnel data shows whether enrichment-vs-not actually moves reply rate. | — | not-scored | _(blocked by 0011 — needs 2+ weeks of stratified angle-funnel data)_ |
| [Imports] Pre-fetch enrichment at import time | Open Question 1 from 0011: move enrichment off the outreach hot path entirely by running it during the importer's commit step. Eliminates per-send latency at the cost of an upfront wait at import. Composes with 0011's cache TTL — no schema change. | — | not-scored | _(blocked by 0011 — wait for rehearsal latency data)_ |
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
| **Simplify enrichment, add Scans section, live-test the scraper** | 2026-04-28 | [plan](../.cursor/plans/simplify-enrichment-scans_2c62f109.plan.md) | Phase 1 — added `--report` flag to `autosdr leads enrich`, ran live against 20 real QLD plumbing leads: 65% ok / 25% timeout / 10% error, p50 2236 ms, p95 3227 ms, **zero `blocked` (robots / 403)**. Block-rate threshold tripped numerically (25%) but the diagnosis is "slow upstreams, not anti-bot" — Crawlee swap deferred (filed as ticket [0012](tickets/0012-crawlee-http-fallback.md)). Phase 2 — removed `_run_enrichment_phase` + `_existing_enrichment_meta` + `_is_cache_fresh` from `autosdr/pipeline/outreach.py`; outreach now reads `lead.raw_data['enrichment']._meta.status` and reports `"missing"` when the worker hasn't reached the lead yet. New `autosdr/pipeline/scans.py` (`select_next_stale_lead` ordered queued+active first, then everything else; `run_scan_pass`; `run_scan_worker` coroutine; cross-coroutine `asyncio.Event` for the manual trigger). FastAPI lifespan now spawns the scan worker alongside scheduler + inbound poller. New `autosdr/api/scans.py` router (`GET /api/scans` paginated + filtered, `GET /api/scans/{lead_id}` full envelope, `POST /api/scans/run` with optional `lead_id` for sync re-scan, `GET /api/scans/summary` for the header strip). Envelope bumped to `version: 2` with `_meta.connector = "website_static"` + `_meta.connector_version = "1.0"`; freshness check now invalidates on connector / version mismatch so the next fetcher migration is automatic. Frontend: new `Radar` sidebar entry between Leads and LLM calls, new `/scans` index (status filter chips, name/website search, paginated table, "Run scan now" button, header summary strip), new `/scans/:leadId` detail (parsed signals + raw `_meta` block + "Re-scan now" button + cross-links to LeadDetail and `/logs?lead=`), "View full scan" link added to the existing compact `EnrichmentCard` on `/leads/:id`. 28 new tests (14 in `test_scan_worker.py`, 14 in `test_scans_api.py`); 4 outreach pipeline tests flipped to assert no inline enrichment + new `"missing"` status row. **474/474 backend tests pass; frontend `tsc -b && vite build` clean.** Follow-up: ticket 0012 (Crawlee HTTP fallback, deferred — re-open if a future cohort shows real anti-bot blocks ≥ 5%). |
| **0011 — Enrich leads with website signal before analysis** | 2026-04-28 | [ticket](tickets/0011-lead-enrichment.md) | New `autosdr/enrichment.py` does a polite per-lead website fetch (root + robots + sitemap, ≤3 HTTP calls + 1 sub-sitemap, ≤1.5s per request, ≤4s total budget, identifiable user-agent) before the analysis LLM call and folds a versioned envelope into `Lead.raw_data['enrichment']`. Closed `EnrichmentStatus` vocabulary (`ok` / `no_url` / `timeout` / `blocked` / `empty_shell` / `not_found` / `error` / `killswitch_aborted`) plus pipeline-only `disabled` for the workspace-toggle case. New `EnrichmentConfig` Pydantic block on `workspace.settings` (`enabled`, `budget_s`, `cache_ttl_days`, `respect_robots`) with operator-tunable defaults (4s/30d/polite). Outreach pipeline (`run_outreach_for_campaign_lead`) accepts a workspace-shared `httpx.AsyncClient` (constructed in the FastAPI lifespan); cache hits short-circuit before opening a socket. Analysis prompt bumped to `analysis-v3.5` with a "Website signal block" subsection that teaches the LLM to read the new shape and preserves the existing truthfulness rule for non-`ok` statuses. `/api/stats/angle-funnel` gains `?enrichment=enriched|unenriched|all` (correlated EXISTS over `Message.metadata.analysis.enrichment_status`); CLI gains `autosdr leads list` (status column) and `autosdr leads enrich --since-days N [--limit N] [--dry-run]` for batch warm-ups. Frontend: `EnrichmentConfig` + `LeadEnrichment` + `EnrichmentStatus` + `EnrichmentFilter` types; new "Lead enrichment" card on Settings → Behaviour; new "Website enrichment" card on `/leads/:id` (status badge + signal summary + sitemap detail); segmented "All / Enriched / Unenriched" control on the angle-funnel panel (URL-param-driven on `/Logs`, local state on `/CampaignDetail`). 27 new/extended tests (16 in `test_enrichment.py`, 4 in `test_outreach_pipeline.py`, 3 in `test_stats_angle_funnel.py`, 6 in `test_cli_leads_enrich.py`). 446/446 backend tests pass; frontend `tsc -b --noEmit` clean. PATTERNS update: outbound-HTTP boundary widened to include `autosdr/enrichment.py` and explicitly allow lifecycle / type-only references in `webhook.py` / `scheduler.py` / `pipeline/outreach.py` / `cli.py`. Follow-ups already filed (0012 background-worker isolation; 0013 import-time prefetch). |
| **0010 — Pace outreach across an 8am–5pm window** | 2026-04-28 | [ticket](tickets/0010-outreach-business-hours.md) | New `outreach_window` block on `workspace.settings` (default `{enabled: true, start_hour: 8, end_hour: 17}`) plus a per-campaign override on `Campaign.outreach_window` (`null` = inherit). New `autosdr/pacing.py` module owns the maths: `resolve_window(...)` for inheritance, `window_allowance(*, window, daily_quota, sent_in_window, now_local)` returning `ceil(quota * elapsed_fraction) - sent_in_window`. Scheduler `run_campaign_outreach_batch` stacks pacing under the rolling 24h quota and `max_batch_per_tick`; manual kickoff (`respect_quota=False`) bypasses both. Reply pipeline, follow-up beat, inbound poll: untouched. New `OutreachBatchSummary.capped_by_window` flag distinguishes "out of business hours" from "out of daily quota". `CampaignOut` exposes `outreach_window` (override blob) + `effective_outreach_window` (resolved); `CampaignCreate`/`CampaignPatch` accept the override (PATCH `null` clears, omit for "no change"). Frontend: workspace default lives in Settings → Behaviour; per-campaign override is a new collapsible card on `/CampaignDetail`. 38 new/extended tests; 412/412 backend tests pass; frontend `tsc -b --noEmit` clean. Follow-ups: workspace IANA timezone (deferred; server-local works for the single-laptop POC), per-day-of-week toggle (cheap to add later), surface "next send at" hint on the dashboard. |
| **0004 — Field-mapping helper for non-canonical lead files** | 2026-04-27 | [ticket](tickets/0004-import-field-mapping.md) | Lead import preview now returns one `ColumnPreview` row per detected source column (name + sample values + `suggested_target` + tiered `suggestion_confidence` + `suggestion_reason`). Suggestion engine is rule-based and deterministic: exact / alias → `high`, Levenshtein ≤ 2 → `medium`, substring → `medium`, sample-value heuristics (E.164-able phones, http URLs, street/region keywords) tiered at ≥ 90% (high) / ≥ 80% (medium) with a `≥ 5 non-null support` floor. Operator can override per column via a new `mapping_config` form field on `/api/leads/import/{preview,commit}` (Pydantic-strict, 422 on bad JSON, BC for clients that don't pass it); `mapping_config` persists on `ImportJob.mapping_config` for audit. CLI `autosdr import` gains `--map canonical=source`, `--drop column`, `--raw-only column`. Frontend `LeadsImport.tsx` gets a column-mapping table after the preview with a per-column dropdown (core field / "Keep in raw_data only" / "Drop entirely"), helper text spelling out the commit-only drop semantic, and a "Drop all unsuggested" bulk action. 25 new field-mapping tests + 5 API tests + 5 CLI tests; full backend suite 341/341 green; preview measured 168ms on 5k rows, commit measured 546ms on 1k rows (SCs <1s / <60s). Follow-ups: LLM-assisted suggestions, "save mapping as template", true streaming NDJSON ingest (already on Later, now unblocked). |
| **0006 — LLM cost tracking + Gemini model presets** | 2026-04-27 | [ticket](tickets/0006-llm-cost-tracking.md) | New `autosdr/llm/pricing.py` is the single source of truth for Gemini text-tier pricing (3.x preview + 2.5 stable), `-latest` alias resolution, `cost_for(model, tokens_in, tokens_out) -> float \| None`, and three named blends (MAX / BALANCED / CHEAP). `_record_usage` accumulates `total_cost_usd` + per-model `cost_usd` in memory; `GET /api/status.llm_usage.estimated_cost_today_usd` is now real (was hardcoded `0.0`). `LlmCallOut.cost_usd` computed on serialisation (`null` for unknown slugs, `0.0` for zero-token sentinel rows from ticket 0001). New `GET /api/llm/presets` endpoint returns the catalog + `pricing_verified_at` snapshot date. CLI: `autosdr status` per-model table gains `est cost (USD)`; `autosdr logs llm` gains a `cost` column. Frontend: `Cost` column on `/Logs`, `est $N.NNNN` on the dashboard LLM-today stat, three one-click preset buttons on Settings → LLM (active preset highlighted; the four model-slug fields stay editable). 8 new tests; 313 backend tests pass; frontend `tsc -b --noEmit` clean. Follow-ups: cost-by-campaign aggregations, spend caps/alerts, OpenAI/Anthropic pricing maps. |
| **0003 — Per-campaign funnel: queued → sent → replied → won/lost** | 2026-04-26 | [ticket](tickets/0003-campaign-funnel.md) | `CampaignOut` now exposes one `*_count` per `CampaignLeadStatus` bucket (queued / sending / paused_for_hitl / contacted / replied / won / lost / skipped) — replacing the misleading rolled-up `contacted_count` / `replied_count` semantics; UI rolls up on demand. New `GET /api/campaigns/{id}/timeseries?days=14` returns daily `{sent, replied, won, lost}` rows. New `CampaignTimeseriesPanel` on `/CampaignDetail` renders a horizontal stacked-bar funnel + 14-day grouped bar chart with per-day `<title>` tooltips. New `autosdr status --campaign <id> [--days 14]` reuses the same handler so CLI/HTTP can't drift. 14 new/extended tests pass; frontend `tsc --noEmit` clean. Follow-ups: `closed_opt_out_count` (needs a `Lead.do_not_contact_at` join, deferred); per-day drill-down view (the right surface doesn't exist yet — `/Logs` shows LLM calls, not messages). |
| **0002 — Surface reply-rate per personalisation angle** | 2026-04-26 | [ticket](tickets/0002-reply-rate-by-angle.md) | New `Thread.angle_type` column (additive, nullable) populated at first-contact analysis with the discrete bucket (`stale_info`, `weak_presence`, `signature_detail`, `differentiator`, `review_theme`, `brand_voice`, `fallback`); legacy NULL → `"unknown"`. New `GET /api/stats/angle-funnel?campaign_id=…&since_days=…` returns `{angle, threads, replied, won, lost}` rows (single SQL, replies via `Message.role=lead` existence — more honest than `CampaignLead.status`). New "By angle" panel on `/Logs` (URL-param-aware) and `/CampaignDetail`, CSS `<div>` bars with 4 % minimum-width clamp. New `autosdr logs angles [--campaign] [--since]` CLI. 263 backend tests pass; frontend `tsc --noEmit` clean. |
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

- **2026-04-28** — Decoupled enrichment from the outreach hot path
  (plan `simplify-enrichment-scans`) and ran a live scraper test
  before committing to any architectural change. The 4 s inline
  pre-fetch in `_run_enrichment_phase` was making outreach decisions
  hostage to strangers' web servers; moved fetch responsibility to a
  new background scan worker (`autosdr/pipeline/scans.py`) wired into
  the FastAPI lifespan alongside the scheduler + inbound poller.
  Outreach now reads `lead.raw_data['enrichment']._meta.status` and
  reports `"missing"` for un-warmed leads (so the angle-funnel filter
  stays honest rather than silently treating them as "ok"). Manual
  trigger via `POST /api/scans/run` flips a shared `asyncio.Event`;
  with a `lead_id` it scans synchronously inside the request for the
  "Re-scan now" button. Envelope bumped to `version: 2` with
  `_meta.connector` + `_meta.connector_version` so the next fetcher
  drops in cleanly — older blobs auto-invalidate via the freshness
  check. Phase 1 evidence (20 real leads, 65% ok / 25% timeout / 0%
  blocked, p95 3227 ms) tripped the plan's > 20% threshold
  numerically but the diagnosis was "slow upstreams, not anti-bot" —
  filed [ticket 0012](tickets/0012-crawlee-http-fallback.md) as
  **deferred** with explicit re-open triggers (`blocked` exceeding 5%
  of any future cohort, or a segment we want to enter consistently
  produces `blocked` rather than `timeout`). The cheaper alternatives
  (bump `budget_s` 4 → 6, or accept the timeout floor since outreach
  is now decoupled) are documented in the ticket. Frontend got a new
  top-level `/scans` section (sidebar, index page with paper-card
  aesthetic + status filter chips + "Run scan now" + header strip,
  detail page with parsed signals + raw `_meta` + "Re-scan now") plus
  a "View full scan" link on the existing compact `EnrichmentCard`.
  474/474 tests; 28 new tests for the worker + API surface.
- **2026-04-28** — Shipped ticket 0011 (lead-website enrichment).
  Implementation followed the council-resolved Framing A: inline
  fetch immediately before the analysis LLM call, hard wall-clock
  caps via `asyncio.wait_for` (proved necessary when
  `httpx.MockTransport`'s built-in timeout did not enforce against
  `asyncio.sleep` in mock handlers), versioned `_meta.version: 1`
  envelope under `lead.raw_data['enrichment']`, no schema
  migration. Two design calls landed during implementation that
  are worth recording: (a) the Open Question about strict sitemap
  depth was resolved to "follow the first referenced sub-sitemap
  once" (i.e. an indexed root counts as a fourth fetch only on the
  index path) — the alternative ("count just the index entries")
  underestimated SMB-site page counts; (b) integration point
  flipped from `_run_analysis` to `run_outreach_for_campaign_lead`
  so the enrichment commit happens in the same session that owns
  the lead row, and the `httpx.AsyncClient` lifecycle stays in the
  webhook lifespan rather than being spawned per LLM call. Mock-LLM
  test harness extended to capture the `user` argument so the
  "analysis user_prompt carries title + H1" success criterion is
  verifiable without standing up a real LLM round-trip. PATTERNS
  rule for `httpx` widened to include `autosdr/enrichment.py` and
  to call out lifecycle / type-only references — fetches still
  happen only inside the bounded modules. Follow-ups are unchanged
  from the brainstorm: 0012 (background worker / tier ordering)
  remains blocked on 2+ weeks of stratified angle-funnel data
  before sizing; 0013 (import-time prefetch) remains optional and
  composes with the cache TTL.
- **2026-04-28** — **Promoted "Website scraping / lead enrichment
  agents" off the non-goals list** with explicit operator sign-off
  ("Can you create a feature that is all about getting more valuable
  information for the leads..."). Filed ticket 0011 — Framing A
  (inline-but-budgeted enrichment, no scheduler change) selected
  via four-voice council mini-round over Framing B (background
  worker + tier ordering) and Framing C (deterministic operator-tunable
  quality score). RICE: A 8.0 vs B 3.6 vs C 3.0 — RICE alone would
  pick A by >2x, but the council surfaced three real concerns:
  (1) Skeptic — *FIFO wastes every downstream improvement on the
  wrong leads, RICE undervalues retries + polite-fetch ergonomics*;
  (2) Pragmatist — *cost of wrong order is low for a single-operator
  box, multiplying failure modes without measured signal is optimism
  tax — but hot-path budget caps and observability are non-negotiable*;
  (3) Critic — *synchronous network I/O ties throughput to strangers'
  servers, signal is bimodal (SPAs / bot-blocked sites return empty
  shells), needs explicit `fetch_status` taxonomy and clear separation
  between immutable import facts and time-varying fetched blobs*.
  Decision: Framing A modified by all three voices — `EnrichmentStatus`
  closed vocabulary (Critic), per-lead 4s + per-request 1.5s + ≤ 3
  fetch hard caps (Pragmatist + Critic), versioned `_meta.version: 1`
  envelope under `lead.raw_data['enrichment']` (Critic), no new
  schema columns, prompt bump to `analysis-v3.5` to teach the LLM
  what to do with absent signal. Skeptic's worker-isolation push
  filed as ticket 0012 (blocked on 0011 producing the stratified
  angle-funnel data that would justify it); Open Question 1's
  import-time-prefetch variant filed as ticket 0013. Confidence:
  medium-high. Strongest dissent (Skeptic, FIFO-wastes) accepted as
  follow-up rather than rejected. **Also confirmed sub-decision** on
  the original Open Question "promote a non-goal?" — yes, with the
  caveat that the principle filter still applies: this enrichment
  ticket is deterministic (no LLM in the score), self-hosted (no
  Apollo / Clay API), and operator-controlled (toggle + budget +
  cache TTL knobs).
- **2026-04-28** — Shipped ticket 0010 (outreach business-hours
  pacing). Three open questions resolved inline rather than via
  council mini-round (each had an obvious answer once the code was
  read): (1) pacing target uses `ceil(quota * elapsed_fraction)` so
  a campaign activated at 8:00:01 gets its first send within the
  first tick rather than waiting until 8:11; (2) per-tick cap is
  `min(pacing_allowance, max_batch_per_tick, remaining_24h_quota)`
  — both the window and the rolling 24h gates apply
  defensively; (3) per-campaign override surface is a collapsible
  card on `/CampaignDetail`, not a separate edit route, mirroring
  the follow-up beat's UX. Workspace IANA timezone setting
  deliberately deferred — the POC runs on the operator's own
  laptop, so server-local time matches operator-local time. New
  `pacing.py` module + Pydantic `OutreachWindowConfig` + `Campaign.outreach_window`
  JSON column + `OutreachBatchSummary.capped_by_window` flag.
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
- **2026-04-26** — Shipped ticket 0003 (per-campaign funnel). The
  ticket's "Open Questions" went four-for-four through council, and
  one decision overrode a Success Criterion: OQ2
  (`contacted-count-semantics`) flipped from "additive only, keep the
  rolled-up names for backward compat" to "replace the rolled-up
  `contacted_count` / `replied_count` with bucket-precise fields and
  migrate the frontend in the same diff" — three voices (Skeptic,
  Pragmatist, Critic) all arguing that keeping misleading aliases
  hardens tech debt for a single bundled consumer. Trade-off accepted:
  one breaking diff vs. permanent yellow-flagged "honest contracts"
  principle. OQ3 (day-click drill-down) resolved as tooltip-only — the
  right drill-down surface (campaign + date scoped messages/activity)
  doesn't exist yet, and `/Logs` shows LLM calls, not messages, so
  deep-linking there would be a category error. Follow-up tickets
  filed: `closed_opt_out_count` exposure (needs a
  `Lead.do_not_contact_at` join because 0001 implemented DNC at the
  Lead level, not as a `CampaignLeadStatus` bucket as the original
  scope assumed) and a campaign+date messages/activity view.
- **2026-04-27** — Shipped ticket 0006 (LLM cost tracking + Gemini
  presets). Council mini-round on the only real open question
  (persist per-call cost vs. compute at read time): two voices
  (Skeptic, Critic) argued persist on temporal-honesty grounds; one
  voice (Pragmatist) and the Architect picked compute-at-read-time.
  Decision: compute at read-time — `LlmCallOut.cost_usd` is computed
  via `cost_for()` on serialisation; `_record_usage` accumulates
  `total_cost_usd` per model in memory so the dashboard pill is
  real-time. Critic's strongest dissent (a March call shows a
  different number in September after a price-map edit) is accepted
  as a known trade-off: every cost surface is labelled "estimated" +
  shows `pricing_verified_at`; no audit / billing consumer exists; if
  one ever appears we migrate to a nullable `llm_call.cost_usd`
  column with a `pricing_snapshot_at` companion (additive — the
  read-time path doesn't preclude it). MAX preset uses
  `gemini/gemini-3.1-pro-preview` (matches the family of current
  defaults); the four model-slug fields stay editable after applying
  a preset (Owner-stays-in-control). Recorded follow-ups: cost-by-
  purpose / cost-by-campaign aggregations, spend caps + alerts,
  OpenAI / Anthropic pricing maps when those providers come online.
- **2026-04-27** — Shipped ticket 0004 (import field-mapping helper).
  Council mini-round on all four open questions. (1) Endpoint shape:
  unanimous extend-the-existing-commit-with-an-optional-form-field
  over a separate endpoint — preview and commit share the same strict
  Pydantic validator (`MappingConfigIn`, `extra=forbid`) so a typo
  like `drop_form_raw` 422s rather than silently passing through. (2)
  Heuristic threshold: Architect's initial 80%-everywhere position
  changed under two-against-one pressure (Pragmatist + Critic) to a
  tiered model — ≥ 90% non-null match → `high`, ≥ 80% → `medium`,
  both gated by a ≥ 5 non-null support floor (Pragmatist's "tiny
  denominator" surprise). (3) `drop_from_raw` semantic: unanimous
  commit-only — filters incoming row payloads on this import, never
  retroactively prunes existing `raw_data`. The shared dissent across
  all three voices (legacy rows keep oversized `raw_data`
  indefinitely) is mitigated by UI helper text spelling the gap out
  rather than hiding it. (4) "Save mapping as template" — deferred,
  no operator asked. The `.gitignore` blocker for
  `all_results_qld.json` evaporated on inspection: the file was
  already absent from the working tree, so a synthetic
  `tests/fixtures/apify_qld_excerpt.ndjson` (20 rows mirroring the
  Apify schema) carries the regression test instead.
- **2026-04-26** — Shipped ticket 0002 (reply-rate per personalisation
  angle). At implementation we discovered the ticket conflated
  `Thread.angle` (freeform 2-3 sentence text) with the discrete bucket;
  the actual `angle_type` enum lived nested in `LlmCall.response_parsed`.
  Council picked path (A) — add a nullable `Thread.angle_type` column and
  populate it at the same write site as `Thread.angle` — over path (B),
  extracting from the JSON column at query time. Reasoning: (B) depends
  on a 1:1 invariant (one analysis call per thread) the DB doesn't
  enforce; future re-analysis would silently break the funnel. Trade-off
  accepted: one nullable column + one entry in
  `_ADDITIVE_COLUMN_MIGRATIONS`. Drift mitigated by a single write path
  plus persistence assertions in `tests/test_outreach_pipeline.py`.

---

## Out of scope (current POC)

Mirror of `autosdr-doc1-product-overview.md § 5`. Update when the source doc
updates. These are pre-approved future-work candidates; **moving an item from
here into Now/Next requires explicit user sign-off** because it's a strategy
shift, not a normal prioritization call.

- Unstructured-text lead imports
- ~~Website scraping / lead enrichment agents~~ — **promoted 2026-04-28** with operator sign-off; see ticket 0011 and the Decisions log entry. The principle-filtered scope is "deterministic, self-hosted, operator-controlled fetch of public website signals" — not third-party data APIs (Apollo / Clay) and not AI-driven scoring.
- Multi-tenancy / SaaS / billing
- iOS SMS integration
- Email connector
- CRM integrations
- AI lead scoring / prioritization
- Conversational config UI
- LLM fine-tuning
