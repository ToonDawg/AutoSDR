# AutoSDR Roadmap

**Last updated:** 2026-05-10 — **Shipped 0018 (bulk-retry + reason filter on Inbox)** alongside 0017 in the same operator session. New `POST /api/threads/retry` endpoint (≤ 50 ids per call, semaphore-bounded, killswitch-aware, **does not re-fire the follow-up beat** because the retry IS the first outbound); `GET /api/threads/hitl/count` now carries a `by_reason` breakdown; `GET /api/threads?hitl_reason=…` filters the list. Inbox grows a reason-chip row driven by the breakdown plus a `Retry all N` / `Retry N` bulk action when the `Connector failed` chip is active, and a post-sweep summary banner that groups failures by error token (operator sees "47 connector still down · 3 send already in flight" without opening individual rows). 690 backend tests pass; 14 new tests on this ticket. Live-decrementing progress chip in the header dropped in favour of the per-chunk summary banner — chunks settle one at a time on the client, so an extra poll loop adds work without new info; can ship as a follow-up if operator feedback warrants. Sequencing note: 0019 (upstream circuit-breaker) was the listed prerequisite but the user explicitly requested 0017 + 0018 first; the ticket still adds value as the resume affordance for the legacy 1000-thread pile-up regardless of 0019's order. **Earlier same day: shipped 0017 (occupation-aware tone register).** Mid-implementation re-council swapped the original code-driven seed-map design for the analysis LLM picking `tone_register` as a structured enum field. Net: ~700 LOC deleted, no Settings UI surface to build, `Thread.tone_register` populated by the analysis call, generation prompt swaps in the matching voice block. Re-read the ticket's "Re-council (2026-05-10)" section for the rationale. — Operator-driven planning round: filed four new tickets in response to a single-session ask. **0017** ([occupation-aware tone register](tickets/0017-occupation-aware-tone-register.md)) lifts the prose `CATEGORY CALIBRATION` block out of `prompts/generation.py` and replaces it with a deterministic `Lead.category → register` map (`tradie | professional | hospitality | retail | personal_services | aged_care | unknown`) — solves the operator's *"can't say 'hey mate' to a nail salon, lawyer, or clinic"* problem by promoting category from "freeform LLM context" to "structured input that drives the prompt". **0018** ([retry connector-failed from inbox](tickets/0018-retry-connector-failed-from-inbox.md)) is the resume-affordance for the operator's 1000-thread connector-failure pile-up: filter chip + bulk-retry button on the Inbox + new `POST /api/threads/retry` endpoint. **0019** ([auto-pause on connector circuit-break](tickets/0019-auto-pause-on-connector-circuit-break.md)) is the upstream fix for that same pile-up — promotes `BaseConnector.consecutive_failures` from a write-only counter into a real circuit breaker that gates the scheduler tick + surfaces a banner + fires a single Web Push event when the gateway phone goes offline. **0020** ([business-longevity signal](tickets/0020-business-longevity-signal.md)) is the operator-requested new scraper angle — extracts `founded_year`/`years_in_business` from "since YYYY" / "established YYYY" / "30+ years in business" patterns alongside the existing `copyright_year` / ABN / ACN regex set, opens a new `angle_type=longevity` for the positive-pivot path, no new dependencies (slots into the same `extract_signals_from_soup` the operator's working `scripts/enrich_leads_httpx.py` already runs). Sequence in *Next* is now **0019 → 0020 → 0016 → 0005-followups → 0007 → docs sync** (0017 + 0018 done 2026-05-10). Earlier (2026-05-02): tickets 0005 + 0008 + 0009 + 0015 shipped, prompt-audit Phase 1/2/3#9/4 #11+#12+#13 landed, ticket 0016 filed for the in-app deploy-watch surface.
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

Sequenced by **risk-first + dependency chain**: 0019 (connector circuit-
breaker) is the upstream fix for the connector-failure pile-up that 0018
(shipped 2026-05-10) currently cleans up. 0020 (longevity scraper signal)
is the cheap operator-requested scraper-angle add-on, gated on Open
Question 4 (measure incidence on the QLD dump first). Then the in-app
LLM deploy-watch surface (0016) which unblocks Phase 3 prompt-shrink.
After that, prod-push hardening (0007) and the long-overdue docs sweep.
Within unblocked groups, ranked by RICE.

| # | Title | Problem | RICE | Status | Link |
| - | --- | --- | --- | --- | --- |
| 1 | **[Connectors/Scheduler] Auto-pause campaigns when connector circuit-breaks** (NEW 2026-05-10) | Operator's gateway phone went offline; scheduler kept ticking; 1000 leads ended up in `connector_send_failed` HITL state before they noticed. `BaseConnector.consecutive_failures` already counts but nothing reads it. Promote it to a real circuit breaker on the ABC, gate the scheduler outreach tick on `should_attempt_send()`, fan-out a single trip-event Web Push, surface a banner + Settings → Connector health card, auto-resume on probe success. **Upstream fix for 0018's pain** — 0018 (shipped 2026-05-10) added the resume-affordance for legacy pile-ups; 0019 prevents new ones. | 8.0 | ready | [`docs/tickets/0019-auto-pause-on-connector-circuit-break.md`](tickets/0019-auto-pause-on-connector-circuit-break.md) |
| 2 | **[AI/Data] Extract business-longevity signal as new positive angle** (NEW 2026-05-10) | Operator hint: *"Might be worth a new addition to the scraper to get a new angle idea?"* Today `extract_signals_from_soup` pulls `copyright_year` (weak proxy for site age) but doesn't extract business longevity — the *"Established 1995"* / *"trading since '95"* / *"30+ years in business"* / *"two decades of service"* patterns that AU SMB sites love to put on their homepage. Adds 4 fields to the envelope (`founded_year`, `years_in_business`, `longevity_evidence`, `longevity_source`), bumps `ENVELOPE_VERSION 3 → 4`, adds `longevity` to `angle_type` closed vocab, adds one worked example to `generation`. Pure regex — no new deps; inherits via the shared extractor in both the crawlee worker and the operator's working httpx script. **Open Question 4 measures incidence on QLD dump before commit.** | 1.25 | spike-first | [`docs/tickets/0020-business-longevity-signal.md`](tickets/0020-business-longevity-signal.md) |
| 3 | [UI/API] In-app LLM "deploy watch" surface | The 2026-05-02 prompt audit shipped `scripts/llm_call_metrics.py` + `scripts/replay_evaluator.py` as the regression harness this project lacked, but they're CLIs only — the operator who's about to be on a phone won't SSH in to read them. Surface per-`prompt_version` slice metrics + a "deploy health" callout + a one-click golden-replay button in the operator console. **Unblocks Phase 3 prompt-shrink work** (audit § 7 Phase 3 #7/#8/#10) which is gated on "the v4.4-v4.7 deploy is stable for 1-2 weeks". | 6.0 | ready | [`docs/tickets/0016-llm-deploy-watch-dashboard.md`](tickets/0016-llm-deploy-watch-dashboard.md) |
| 4 | [Hardening] Override safety + connector E.164 guard + `autosdr e2e` rehearsal CLI | OverrideConnector's single-slot mapping can cross-talk under concurrent sends (real customer's thread receives the rehearsal reply); BaseConnector trusts `contact_uri` verbatim with no E.164 guard; pre-prod-push rehearsal is ~12 manual UI/curl steps. Identified during the 2026-04-27 prod-push rehearsal — see Addendum for findings 3–6 (SMSGate transport, DB bloat, simulator CLI). | — | ready | [`docs/tickets/0007-prod-hardening-override-and-e2e.md`](tickets/0007-prod-hardening-override-and-e2e.md) |
| 5 | [Docs] Sync `ARCHITECTURE.md` with as-built | § 14 still says "Any frontend or PWA" is out of scope (frontend has shipped). § 3 component map omits `pipeline/followup.py`, `pipeline/suggestions.py`, `quota.py`, `workspace_settings.py`. The PM skill's forecasts assume this doc is accurate. **Prompt audit added `enrichment_vocab.py`, `enrichment_extract.py`, `pipeline/priority.py`, `prompts/_tone.py`, the `pricing.py` cost map, and the new `scripts/replay_*` + `scripts/llm_call_metrics.py` diagnostic CLIs — sweep all in one pass.** | 2.5 | ready | _(self-contained chore — inline)_ |
| 6 | [Repo] Actually ignore `all_results_qld.json` | Last commit (`470345c`) message claims it added the file to `.gitignore`; it didn't (`/.gitignore` reviewed 2026-04-26). 323 MB of real lead data sitting untracked → one `git add .` from being committed. | — | ready | _(one-line fix; rolled into Docs sync)_ |

---

## Later — high-confidence, not yet committed

| Title | Problem | RICE | Status | Link |
| --- | --- | --- | --- | --- |
| [AI/Prompts] Phase 3 prompt shrink — dedup eval against generation | Audit doc § 7 Phase 3 #7. Today's eval prompt re-explains the anti-patterns and worked examples that generation already encodes. Cut eval system prompt to ~5K chars by reducing it to JSON schema + scoring anchors + anti-pattern checklist; cut 8 worked-feedback examples → 2-3; cut category calibration prose to 4 lines. **Blocked on 1-2 weeks of stable v4.7 metrics from ticket 0016.** | 5.0 | spike-first | _(blocked by 0016 + 1-2 weeks of deploy data)_ |
| [AI/Prompts] Phase 3 #8 — Cap tone block at ~1500 chars | Audit doc § 7 Phase 3 #8. The tone snapshot is 3,276 chars and gets injected into both `generation.build_system_prompt()` AND `evaluation.build_user_prompt()`. Current text repeats itself across "Voice" and "Avoid" sections. Cap and document. | 3.0 | not-scored | _(blocked by 0016)_ |
| [AI/Prompts] Phase 3 #10 — Move franchise/ownership lists code-only | Audit doc § 7 Phase 3 #10. The 28-item franchise prefix list + 9 ownership keywords are checked again in `validate_owner_first_name`. The prompt only needs to teach the SHAPE of the rule. Saves ~600 chars from `analysis` system prompt. | 2.5 | not-scored | _(blocked by 0016)_ |
| [AI/Prompts] Phase 4 #13 — analysis reasoning-effort smoke | Audit's classification smoke flipped expectations (default was already "off"; setting "low" inflated tokens 3× without accuracy gain). Analysis is the biggest reasoning consumer left (1,516 avg `tokens_out` per audit § 3) — needs the same smoke. | 2.0 | spike-first | _(small; do after 0016 surfaces the data)_ |
| [AI/Prompts] Phase 4 #14 — bump LM Studio context | Audit doc § 7 Phase 4 #14. LM Studio's `loaded_context_length` is 4,096 today; current production prompts can't fit (analysis needs 4,358; eval 4,677; generation 6,293). Bump to ≥ 16K so the local fallback can actually run today's prompts. | 1.5 | not-scored | _(operator-side config; doc + verify)_ |
| [AI/Scheduler] Lead prioritisation by enrichment quality | Skeptic's pushed framing from the 0011 brainstorm: FIFO wastes every downstream improvement on the wrong leads. Background enrichment worker + a binary "enriched-first" tier on `_next_queued_leads`. Sized after 0011 ships and the angle-funnel data shows whether enrichment-vs-not actually moves reply rate. | — | not-scored | _(blocked by 0011 — needs 2+ weeks of stratified angle-funnel data)_ |
| [Imports] Pre-fetch enrichment at import time | Open Question 1 from 0011: move enrichment off the outreach hot path entirely by running it during the importer's commit step. Eliminates per-send latency at the cost of an upfront wait at import. Composes with 0011's cache TTL — no schema change. | — | not-scored | _(blocked by 0011 — wait for rehearsal latency data)_ |
| [Onboarding] Swipe-based tone calibration | Spec'd in doc4; success metric "≥ 10 swipe decisions compile a `tone_prompt` without manual editing". Currently `tone_prompt` is a free-text field on the Setup wizard. Risk: voice goes generic at scale. | 0.8 | spike-first | _(do a 1-day prompt-design spike before sizing the L build)_ |
| [Imports] Streaming NDJSON / large-file ingest | The 323 MB QLD file would currently load fully into memory in `importer.py`. Once the field-mapping ticket lands, large-file mode is the obvious next concern. | 3.75 | scoping | _(blocked by Field-mapping)_ |
| [AI] A/B compare two personalisation angles per lead | Logs already record angle, draft, score. Doubling the analysis call to pick from two angles before generation would let the evaluator compare and surface "which angle wins" data over time. | 4.0 | spike-first | _(uses existing audit log; needs prompt-design spike)_ |
| [Connectors] Push-based inbound for TextBee | Today TextBee is poll-only; SMSGate already pushes. Sub-second reply latency matters more once Web Push lands (otherwise the notification beats the message into the DB). | 1.0 | spike-first | _(blocked by TextBee API surface; spike)_ |
| [Connectors] Delivery-receipt support on `BaseConnector` | Operator can't currently tell "did the SMS actually deliver?" — only that the connector accepted the send. Needed before any "after N days, follow up" automation. | 1.0 | spike-first | — |
| [AI] Business-data extraction agent at setup | Doc4 spec'd; today the operator's free-text business description is shoved into every generation prompt verbatim. A structured extract (offers, credentials, geographies, signature line) would tighten generation. | — | not-scored | _(score when promoting)_ |
| [Ops] LLM spend caps + alerts | Follow-up from ticket 0006 + audit Phase 2#6. Today the operator can *see* spend (Dashboard pill, Logs cost column, llm_call_metrics.py CLI). They can't *cap* it. Soft cap with a warning push, hard cap that flips killswitch. Composes with ticket 0016's health-flag taxonomy and ticket 0005's push transport. | — | not-scored | _(blocked by 0005 + 0016)_ |

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
| ~~[UI] Mobile / responsive layout below 1024px~~ | ~~README explicitly says "laptop UI". Reasonable trade-off until PWA + Push lands; revisit then.~~ | — | **Promoted 2026-05-02** to Next as ticket 0015 — operator confirmed they want phone access, AND it's the precondition for 0005's value. |

</details>

---

## Done — last 90 days

Most-recent first.

| Title | Date | Ref | Note |
| --- | --- | --- | --- |
| **0018 — Filter and bulk-retry connector-failed threads from the Inbox** | 2026-05-10 | [ticket](tickets/0018-retry-connector-failed-from-inbox.md) | New `POST /api/threads/retry` endpoint — accepts ≤ 50 thread ids, optional `reason_filter` (server-side guard so a typo doesn't retry an `awaiting_human_reply` row), optional `max_concurrent` (default 5, hard-cap 10, single-SIM bottleneck). Concurrency bounded by `asyncio.Semaphore`; each retry runs on its own short-lived `session_scope()`; killswitch checked per-row so a mid-batch flip stops new sends within ms. Critically, **`suppress_followup=True` is hard-coded** so a stale-draft retry never re-fires the +10 s follow-up beat — the retry IS the first outbound; the follow-up is owned by the *original* send path which already ran when the outreach pipeline first failed. Pinned by mocking `schedule_followup_send` and asserting `call_count == 0` across the success path. Per-thread failures append to `hitl_context.retry_attempts: list[{ts, error}]` so "still failing for an hour" is a one-glance read instead of a log scrape. `send_draft` body extracted into a reusable internal `_perform_thread_send(thread_id, *, suppress_followup, enforce_reason)` returning a structured `_SendOutcome`; the existing `POST /api/threads/{id}/send-draft` endpoint wraps it and maps to identical `HTTPException` codes — per-thread retry behaviour is byte-identical. The session-spanning-await pattern is preserved (it's allowlisted on the AST lint test for atomic message-with-state-flip semantics; restructuring is a separate larger ticket). `GET /api/threads/hitl/count` promoted to a typed `HitlCount` Pydantic model with a `by_reason: Record<HitlReasonT, number>` breakdown (single GROUP BY against active paused threads). `GET /api/threads` accepts `hitl_reason=…` as a where-clause additive on the existing list endpoint; indexable on the composite `idx_thread_status` index. Frontend: new `ReasonChipRow` on the Inbox reads counts from `count.by_reason` — promoted reasons (`connector_send_failed`, `eval_failed_after_max_attempts`, `awaiting_human_reply`) hit the server filter directly; `Other` is filtered client-side as "everything not in the promoted set" so the count matches the server's `Other` residual without enumerating every classifier flag. When the chip is `Connector failed` and ≥ 1 row is selected, the toolbar swaps in `Retry N` (primary); when nothing is selected, `Retry all N` (ghost) sweeps the visible page. The mutation chunks client-side at 50 to honour `MAX_RETRY_BATCH_SIZE`. After the sweep, a `RetryReportBanner` summarises succeeded/failed and groups failures by error token via a new `RETRY_ERROR_LABEL` map (operator sees, e.g. "47 connector still down · 3 send already in flight" without opening individual rows). `retrying…` chip in the toolbar surfaces in-flight state. **Deviation from the mini plan:** the live-decrementing progress chip in the Inbox header was dropped in favour of the per-chunk summary banner — chunks settle one at a time on the client (the mutation already has per-chunk results), so a separate poll loop adds work without new info, and the per-failure-token rollup is more actionable than a moving number ("47 still failing" + "47 connector still down" tells the operator the gateway is still off; "47 still failing" alone doesn't). Live chip can land as a follow-up if the banner doesn't carry its weight. 14 new tests (11 in `test_threads_bulk_retry.py`: happy path with no follow-up scheduled, partial failure, reason mismatch, no stashed draft, killswitch halts new sends, batch-size 51 → 400, concurrency 15 → clamped to 10, dedup, defensive guards for unknown / active threads; +3 in `test_hitl_dismiss.py`: filter + by-reason + null bucketing). 690 backend tests pass; `tsc -b --noEmit` clean; `vite build` clean (Inbox bundle 11.84 kB / 4.07 kB gzipped). No new deps, no schema migration, no manifest deltas. Pattern-unifier focused scan against the diff: ✓ no drift introduced (every change lands inside an existing PATTERNS row — FastAPI / SQLAlchemy 2.0 / Pydantic v2 / killswitch / pytest+asyncio / TanStack Query / React Router 7 / lucide-react / Tailwind v4 tokens / `lib/api.ts` / `lib/types.ts` mirror). Sequencing note: 0019 (upstream circuit-breaker) was the listed prerequisite but the user explicitly requested 0017 + 0018 first; 0018 still adds value as the resume affordance for the legacy 1000-thread pile-up regardless of 0019's order. Out-of-scope deferrals (per ticket): auto-retry on connector failure → ticket 0019; URL-param filter persistence (one-line follow-up if asked); re-drafting on retry; connector swap mid-retry. |
| **0017 — Tone register adapts to lead occupation/category** | 2026-05-10 | [ticket](tickets/0017-occupation-aware-tone-register.md) | Re-councilled mid-implementation: the analysis LLM now picks `tone_register` as a structured enum field on its JSON output (one of `tradie | professional | hospitality | retail | personal_services | aged_care | unknown`), instead of a code-driven `Lead.category → register` substring map. ~700 LOC deleted (no `autosdr/tone_register.py`, no `tone_registers` workspace settings, no Settings UI override card, no kill switch); ~150 LOC added across `autosdr/prompts/analysis.py` (new `_RULES_TONE_REGISTER` block + `tone_register` schema field, bumped `analysis-v3.6 → v3.7`), `autosdr/prompts/generation.py` (inlined `_REGISTER_INSTRUCTIONS` + `ToneRegisterT`, bumped `generation-v8 → v9`, fixed three "DO use" → "Do NOT use" prose typos in the `professional` / `personal_services` / `aged_care` register blocks), `autosdr/pipeline/outreach.py` (closed-vocab guard at the persistence boundary collapses unknown / invalid LLM responses to NULL on `Thread.tone_register`), `autosdr/pipeline/_shared.py::generate_and_evaluate` (reads register off the thread, passes to `build_system_prompt(register=…)` so reply / regenerate flows reuse the original outreach decision). Schema: `Thread.tone_register VARCHAR(32) NULL` (additive migration via `_ADDITIVE_COLUMN_MIGRATIONS`). API: `ThreadOut.tone_register` round-trips on `GET /api/threads` + `GET /api/threads/{id}`; `/api/stats/angle-funnel` extended with `?dimension=register|angle_register` (existing `dimension=angle` default preserves the pre-0017 contract). Frontend: new `RegisterChip` component (mustard / forest / teal / rust / oxblood / neutral by register, single-source labels + tooltips inline, mirrors `PriorityBadge` pattern), new `ToneRegister` literal in `frontend/src/lib/types.ts`, chip rendered next to the angle on `ThreadDetail` right rail and inline on `LeadDetail`'s per-thread row. Tests: 6 new in `test_prompts.py` (analysis schema advertises field, generation injects / omits block per register, prose still under compose budget, defensive fallback on junk tokens, no `CATEGORY CALIBRATION` regression); 3 new in `test_outreach_pipeline.py` (analysis output persists, `unknown` collapses to NULL, invalid token collapses to NULL); 3 new in `test_hitl_dismiss.py` (parametrized `tone_register` round-trip on the API). 675 backend tests pass; `tsc -b --noEmit` clean. **Deferred / dropped from original ticket scope:** Settings UI "Tone register" card (no overrides if LLM picks); workspace `tone_registers` block + kill switch (no overrides means nothing to configure; revert is `analysis-v3.7 → v3.6`); `LeadsImport.tsx` per-register count preview (no deterministic mapper to preview); `AngleFunnelPanel` frontend dimension toggle (backend ready — frontend can add when there's data to read). Cohort opener-shape audit (≥ 90% per register) deferred to ticket 0016 deploy-watch. **Owner control trade-off**: no per-workspace operator-tunable register override; recourse for a wrong-register draft is the existing HITL take-over flow. Recoverable later if a per-workspace skew emerges. |
| **0005 — PWA install + Web Push for HITL escalations** | 2026-05-02 | [ticket](tickets/0005-pwa-web-push.md) | The operator console is now an installable PWA. New `vite-plugin-pwa` (injectManifest) emits `dist/manifest.webmanifest` + a custom `dist/sw.js` (`frontend/src/sw/sw.ts`) with bespoke `push` + `notificationclick` handlers and a NetworkFirst cache for `/api/`. Service worker registration ships in `frontend/src/sw/register.ts` and is called from `main.tsx` (no-op in dev). Backend: new `autosdr/push.py` owns VAPID keypair lifecycle (`ensure_vapid_keys`, generated on first lifespan boot, persisted to `workspace.settings.push.vapid_*`), the privacy-strict payload builder (`build_hitl_payload` — only `{title, body, thread_id, lead_first_name, hitl_reason, escalated_at, url}` — and the dataclass shape itself is the contract), and the killswitch-aware fan-out (`fanout_hitl_push`, run via `asyncio.to_thread` so a slow gateway never blocks the reply pipeline; HTTP 410 hard-deletes dead subs). New `autosdr/api/push.py` exposes `GET /api/push/vapid-public`, `POST /api/push/subscribe` (upsert on `endpoint`), `DELETE /api/push/subscribe` (idempotent), `GET /api/push/subscriptions`, `POST /api/push/test`. New `autosdr/networking.py` shells out to `tailscale status` (best-effort; PATH/timeout/exception failures collapse to `not_detected` so we never blame the operator for the probe), surfaces a startup `WARNING` log when `HOST=127.0.0.1` *and* Tailscale is up (the *PC-bind-interface footgun* the Pragmatist flagged), and powers a new `GET /api/status/networking` endpoint. The HITL escalation seam (`autosdr.pipeline._shared.schedule_hitl_push`) wires up to four `pause_thread_for_hitl` call sites (outreach×2, reply×2) — fires the fanout *after* `session.flush()` succeeds, no-ops cleanly outside an event loop. New `push_subscription` table (`autosdr/models.py`) with unique-on-`endpoint` constraint + workspace+created_at index. Frontend: new Settings → Notifications card (subscribe / unsubscribe / list devices / test fire / HITL toggle / dashboard-origin override) and Settings → Networking card (HOST bind state + Tailscale probe + resolved deep-link origin + warning surface). 39 new tests across 6 files (`test_push_subscription_model`, `test_push_vapid_lifecycle`, `test_push_subscriptions_api`, `test_push_transport`, `test_push_payload_privacy`, `test_dashboard_origin_resolution`, `test_pause_thread_for_hitl_push`, `test_networking_probe`). 661 backend tests pass; `tsc -b --noEmit` clean; `vite build` clean (54-entry precache, dist/sw.js with all three SW handlers verified). PATTERNS.md gains 3 new rows (`vite-plugin-pwa`, `pywebpush`, `cryptography`) + 2 decisions-log entries. README has a new "Remote access (use AutoSDR from your phone)" subsection walking through the Tailscale + `HOST=0.0.0.0` flow + SMSGate Local-vs-Cloud trade-off. **Operator-side mobile smoke (Tailscale install on PC + phone, full end-to-end notification on cellular) deferred to home PC** — the work-PC constraint blocks Tailscale install; everything else is covered by the test suite. Follow-ups raised: vulnerability scanner noise from build-time-only deps (workbox-build chain, glob CLI), Vite-8 peer-dep mismatch with vite-plugin-pwa@1.2.0 (installed via --legacy-peer-deps; works in practice), VAPID rotation story (v0 ships keypair-forever), `push_event` telemetry table (deferred), send-failure / quota-exhausted push events (cheap follow-up — one new caller of `schedule_hitl_push` per event class). |
| **0008 — Reply pipeline must not hold the SQLite write transaction across LLM API awaits** | 2026-05-02 | [ticket](tickets/0008-reply-pipeline-tx-across-await.md) | Three-part fix for the rehearsal-day "status endpoint times out for 30-60s during inbound processing" pathology. **Part A (phased pipeline)** — `process_incoming_message` was already split into Phase 1 *read+persist-inbound*, Phase 2 *async LLM work*, Phase 3 *write outcome*; the contract is now codified by a new AST lint test (`tests/test_no_await_in_session.py`) that walks every `.py` file under `autosdr/` and fails on `await` inside `with session_scope()` / `with db_session()`. Pre-existing violations in `scheduler.py`, `api/threads.py`, `pipeline/followup.py`, `api/campaigns.py`, `api/leads.py`, `api/scans.py` are explicitly allowlisted with explanatory notes pointing to follow-up tickets — `reply.py` is *not* on the allowlist. **Part B (audit log off the loop)** — `_log_call` is now `async`; the synchronous body (`_persist_audit_row_sync`) runs on `loop.run_in_executor(None, ...)` and we `await` the future. `_update_parsed_json` mirrors the same pattern. Awaiting (rather than blocking) frees the loop to service other coroutines while SQLite holds the writer lock; sync-context fallback persists inline for CLI scripts. **Part C (busy_timeout)** — `PRAGMA busy_timeout` dropped 120 000 ms → 5 000 ms now that no caller holds a session across an LLM await; a real 5 s wait now means a genuine writer-on-writer contention bug, not a pipeline patiently queuing. **Tests** — new `tests/test_reply_pipeline_concurrency.py` (2 cases): a competing `UnmatchedWebhook` INSERT during a 0.4 s simulated LLM await completes with median < 50 ms (was waiting full busy_timeout pre-fix); two concurrent inbounds finish in ~0.2 s vs ≥ 5 s pre-fix. **610 backend tests pass.** New `ARCHITECTURE.md § 12 "Database concurrency rules"` section explains the no-await-in-session invariant, the three-part fix, and the runtime test that asserts it. Follow-ups raised: porting the six allowlisted paths to the phased pattern; manual-rehearsal smoke for WAL ≤ 32 MB; a "claim/lease/complete" enum or 0009-style queue if a process crash mid-LLM ever strands an inbound in practice. |
| **0009 — Killswitch must not silently drop inbound webhooks** | 2026-05-02 | [ticket](tickets/0009-killswitch-inbound-replay.md) | Pause is now a *processing* pause, not a *durability* pause. New `paused_inbound` table (`autosdr/models.py:PausedInbound`) with composite indexes on `(workspace_id, created_at)` and `(replayed_at, created_at)` — `Base.metadata.create_all` is idempotent so legacy DBs pick it up on next boot. Webhook handler (`autosdr/api/webhooks.py:_process_in_background`) replaced the silent drop with `_persist_paused_inbound` — log line is now `"killswitch on; queued inbound for replay: %s"` (truthful — was `"dropping inbound while paused: %s"`); both `/api/webhooks/sms` and `/api/webhooks/sim` honour the killswitch identically. New `autosdr/pipeline/replay.py:drain_paused_inbounds` walks unreplayed rows oldest-first, dispatches each through `process_incoming_message`, stamps `replayed_at` on success, leaves NULL on failure / connector-mismatch (operator can swap connector back or DELETE by hand). `POST /api/status/resume` is now async and fires the drain via `asyncio.create_task` (fire-and-forget — operator watches `pending_count` drop on `/api/status`). New `paused_inbound: { pending_count, oldest_pending_at }` block on `SystemStatusOut` (cheap aggregate via the new index). Frontend: `SystemStatus` type mirrors the schema; `KillSwitch.tsx` renders a mustard-soft `N inbound(s) waiting` chip next to the toggle when `pending_count > 0`, with a tooltip surfacing `oldest_pending_at` for stale-queue detection. **Six new tests in `tests/test_killswitch_inbound_durability.py`**: queue-not-drop on `/sim` and `/sms`, drain-on-resume reaches expected thread state, **STOP-during-pause flips `lead.do_not_contact_at` on resume** (closing the ticket-0001 deterministic-shortcut promise across pause windows), connector-mismatch is a skip-not-replay, status endpoint surfaces `pending_count`. 610 backend tests pass; `npx tsc -b --noEmit` clean. **Sequence requirement honoured**: 0009 ships *after* 0008 because the replay drain re-uses `process_incoming_message`; without the phased pipeline the drain would deadlock the dev server on the second inbound. Follow-ups raised: list-modal opened from the badge (needs `GET /api/paused-inbounds` — file when queue routinely > 10 entries); CLI `autosdr status` extension when the CLI lands. |
| **0015 — Mobile-responsive operator console** | 2026-05-02 | [ticket](tickets/0015-mobile-responsive-console.md) | Below `md:` (768 px) the persistent sidebar collapses to a hamburger drawer (`MobileDrawer`), the topbar stays sticky with the killswitch always pinned, and every primary list route (Leads, Threads, Logs, Inbox, Scans, LeadsImport-mapping, plus campaign-detail's threads list) now renders a new `CardList`/`CardListItem` primitive instead of an overflowing table. `Inbox`'s HITL row stacks vertically on phones with min-44 px dismiss/restore buttons. `ThreadDetail` switches from `grid grid-cols-12` to `flex flex-col lg:grid` so the right rail (angle / LLM trail / stats) drops below the timeline on mobile, the `ComposeBar` is wrapped in `sticky bottom-0 z-20 bg-paper`, and the `LlmTrail` collapses to a `<details>` disclosure (`<lg`) while keeping the inline rail (`≥lg`). `Button` mobile-first sizes (`h-11 md:h-7|h-9`), `Input` `min-h-11 md:min-h-0`, `KillSwitch` `h-11 min-w-[88px]`, and an `index.css` `@media (max-width: 767px)` rule sets `input/textarea/select` font-size to 16 px to suppress iOS Safari's auto-zoom-on-focus. `CampaignDetail` and `Dashboard` headers stack vertically with responsive stat grids (`grid-cols-2 md:grid-cols-5`); two-column form grids inside CampaignDetail are now `grid-cols-1 sm:grid-cols-2`. Two new files (`MobileDrawer.tsx`, `CardList.tsx`); twelve modified routes/components; `README.md` + `ARCHITECTURE.md § 15` rewritten to reflect mobile-in-scope. No new deps, no API change, no schema migration — purely Tailwind/layout work. `tsc -b --noEmit` clean, `vite build` clean (new `CardList-*.js` is 0.58 KB gzipped, well inside the 5 KB-gzipped allowance). 601 backend tests pass. Pattern-unifier diff scan introduced no new drift; CardList sits in `ui/`, MobileDrawer in `layout/`, both consistent with the component-layout row in `docs/PATTERNS.md`. Re-run the smoke-checklist viewport matrix when ticket 0005 ships. |
| **Prompt audit + bug fix + observability — `evaluation.py` foot-gun (Phase 1), JSON schema response_format at v4.7 (Phase 4 #11/#12), classification reasoning-effort knob (Phase 4 #13), prompt module split (Phase 3 #9), shared tone module, LLM cost backfill, four diagnostic CLIs** | 2026-05-02 | [audit](prompt-audit-2026-05-02.md) | **Phase 1 (the bug fix).** `autosdr/prompts/evaluation.py:335` had a Python implicit-string-concat foot-gun that multiplied the entire BACKGROUND CONTEXT block by 60 — every eval call shipped ~63K input tokens instead of ~1.5K. Half of all LLM spend on the DB was wasted bug. Hoisted the separator to a local (`separator = "=" * 60`), bumped `evaluation.PROMPT_VERSION` to `evaluation-v4.4`, added 3 regression tests (`tests/test_prompts.py::{test_evaluation_user_prompt_does_not_repeat_background_context,test_evaluation_user_prompt_size_is_bounded,test_evaluation_user_prompt_includes_separator_once}`). Live-replayed 8 historical threads against real Gemini Flash-Lite — `pass_flips: 0/8`, `tokens_in -92%` (~63,400 → ~5,394), `latency -61%` (~2.8s → ~1.1s), `$/eval -91%` (~$0.011 → ~$0.001). **Phase 2 (regression harness).** New `scripts/llm_call_metrics.py` slice-metrics CLI per `prompt_version` (calls, errors, tokens, eval pass-rate, p10/p50/p90 of `overall`, attempts-per-send, `$/sent-thread`); new `scripts/replay_evaluator.py` golden-replay harness; new `scripts/replay_classifier_smoke.py` and `scripts/replay_outreach_loop.py` for end-to-end smokes. Default dry-run; `--apply` persists. **Phase 3 #9 (refactor).** Split `evaluation.py` / `generation.py` / `analysis.py` into named module-level constants (`_RULES`, `_WORKED_EXAMPLES`, `_OUTPUT_SCHEMA`, `_REFERENCE_EXAMPLES`, `_RULES_OWNER_FIRST_NAME`, …); `tests/test_prompts.py::test_rendered_prompts_are_byte_stable` pins SHA-256 of every rendered prompt so future drift is loud. Pure refactor, no `PROMPT_VERSION` bump. New `autosdr/prompts/_tone.py` shared tone module dedupes the tone block across generation + evaluation + follow-up. **Phase 4 #11/#12 (JSON schema response_format).** Bumped `evaluation-v4.7`. New `EVALUATION_RESPONSE_SCHEMA` with `additionalProperties: false`, threaded `json_schema=` through `complete_json`, capability detection via `litellm.supports_response_schema(model=...)`, three-tier fallback (`json_schema` → `json_object` → text). 8-thread golden audit: 0 pass flips, -2.25 avg `Δoverall` (within Flash-Lite noise), `eval_tokens_out_total` 861 (~108 avg, sharply down from ~120-180 historical). **Phase 4 #13 (reasoning budget).** Added `reasoning_classification` setting + `reasoning_effort` kwarg on `complete_json`. Bumped `classification-v1.1`. **Plot twist from the smoke**: Flash-Lite was already skipping thinking by default for classification — setting `"low"` *enabled* thinking that wasn't happening, inflating tokens 3× and latency 2× and flipping a thumbs-up reply from `objection` to `negative`. Default flipped to `"disable"` (no-op against today's defaults but a guard against future provider changes). **Cost backfill.** New `scripts/backfill_llm_call_costs.py` walks historical `llm_call` rows and writes `cost_usd` using the current `pricing.py` map. **530/530 backend tests pass.** Phase 3 #7 (eval-vs-generation dedup), #8 (tone block budget), #10 (move franchise list code-only), and Phase 4 #14 (bump LM Studio context) tracked in *Later* — gated on 1-2 weeks of stable v4.7 metrics from ticket 0016. |
| **Campaign quota counter resets at midnight (was rolling-24h)** | 2026-04-30 | _(inline — operator request)_ | `autosdr/quota.py` cutoff switched from `now() - 24h` to today's server-local midnight (UTC-converted). Functions renamed to `count_outreach_contacts_today{,_bulk,_per_category_today}`; injectable `now_local` for deterministic tests. Public field renamed `sent_24h` → `sent_today` on `CampaignOut` and `CampaignQuota`; frontend types and labels followed (Dashboard "messages today", Campaigns row "TODAY", CampaignDetail "Today's send count" + "Resets automatically at midnight" hint). `Campaign.quota_reset_at` retained as the manual mid-day reset path. Scheduler reads a single `clock = datetime.now().astimezone()` per tick so the daily-quota cutoff and working-window pacer can't disagree across a midnight rollover. Working-hours pacer (default 8am–5pm) bounds any "midnight burst" risk that the old design called out. New `test_count_outreach_contacts_today_resets_at_local_midnight` drives the rollover deterministically; 526/526 backend tests pass; frontend `tsc -b --noEmit` clean. ARCHITECTURE.md § 10, README, `autosdr-doc2-data-architecture.md` §3.4 + design-decisions table all updated. |
| **0014 — Treat social-profile-as-website as a priority signal** | 2026-04-30 | [ticket](tickets/0014-social-profile-as-website.md) | Predicate widened: a lead with `Lead.website` on a tracked social platform (`facebook`, `instagram`, `linkedin`, `twitter`, `x`, `tiktok`, `youtube`) joins the priority tier even when the scan returned `ok`. Shared vocab now lives in `autosdr/enrichment_vocab.py::SOCIAL_HOSTS` so the homepage extractor regex (`autosdr/enrichment_extract.py::_SOCIAL_RE`) and the `Lead.website` predicate (`autosdr.enrichment.is_social_website`) cannot drift; new test pins the equivalence. `priority_reason` gains a second token `"social_profile_website"` with deterministic precedence (`not_found` outranks). New informational `LeadOut.is_social_website` (platform token) drives a new `SocialProfileTag` chip rendered next to the lead name on the Leads list and LeadDetail header — independent of the priority badge so a 404'd Facebook URL shows both signals. `_campaign_queued_priority_bulk` SQL now ORs `Lead.enrichment_status = 'not_found'` with case-insensitive `LIKE` clauses against all seven platforms (host-only — path mentions don't match). Import preview returns `social_website_hosts: {platform: count}` so the operator sees how many leads will land in the priority tier *before* committing; new `LeadsImport` callout renders a per-platform line above the import stats grid when the dict is non-empty. `CampaignDetail` priority callout copy widened: "broken website or social profile in lieu of website". 23 new tests (Python truth tables + scheduler tier inclusion + bulk-count OR semantics + import preview + LeadOut precedence); 525/525 backend tests pass; frontend `tsc -b --noEmit` clean. No schema migration — predicate is computed on read. Pattern-unifier diff scan: ✓ no new drift. |
| **0013 — Prioritise leads with high-confidence broken-website signals** | 2026-04-30 | [ticket](tickets/0013-broken-website-priority.md) | New priority tier on `_next_queued_leads`: leads whose `Lead.enrichment_status = "not_found"` (404/410 from the website scan) drain before normal-tier leads, while preserving today's category-mix rotation *within* each tier. Picker carries `last_sent_cat` and `intra_batch` state across the tier boundary so the cross-tier rotation looks identical to today's (no double-P, no double-E). New `autosdr/pipeline/priority.py` module owns the predicate (`is_priority_lead`, `priority_reason`, `PRIORITY_REASON_NOT_FOUND`) so the picker and the API serialiser stay in lockstep without dragging in the LLM/prompt stack from `pipeline/_shared.py`. `LeadOut` exposes `is_priority` + `priority_reason`; `CampaignOut` exposes `queued_priority_count`. New `PriorityBadge` React component renders an oxblood "Website 404" chip with tooltip on Leads + LeadDetail. New `priority.enabled` workspace setting (default `true`) wires through Settings → Behaviour as a toggle; off collapses to the pre-0013 single-pass picker — pinned as a regression bar. Bulk SQL count uses the existing `idx_lead_enrichment_status` composite index (no migration). 30 new/extended Python tests + 1 visual / type-check pass on the frontend. Follow-ups raised: 0014 (this row) — extends the predicate to social-profile-as-website. |
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

- **2026-05-10 (post-ship)** — **0018 shipped same session as 0017,
  ahead of 0019.** The roadmap had 0019 (auto-pause on connector
  circuit-break) sequenced first because it's the upstream fix for
  the 1000-thread connector-failure pile-up that 0018 cleans up — but
  the user explicitly asked for 0017 + 0018 in the same pass, so we
  shipped against that order. 0018 still earns its place: the resume
  affordance is the operator-facing UX for whatever pile-up is
  already in the inbox (legacy or otherwise), and 0019's value is
  *upstream prevention* of new pile-ups, not cleanup of the existing
  one. The two compose cleanly; sequence inversion costs nothing.
  All five Open Questions resolved at intake (architect-led, no
  council mini-round needed): stateless server with client-
  orchestrated batches of ≤ 50 ids; default `max_concurrent=5`,
  hard-cap 10 (single-SIM bottleneck makes this factual); response
  envelope carries `message_id` + `provider_message_id`; `Other`
  filter chip ships in v0; killswitch chip stays a single-purpose
  toggle (progress lives in the Inbox toolbar). One deviation from
  the mini plan worth pinning: the **live-decrementing progress
  chip in the Inbox header was dropped** in favour of a
  per-chunk `RetryReportBanner` that groups failures by error
  token. Rationale: chunks settle one at a time on the client (the
  mutation already returns the per-chunk envelope), so a separate
  2 s poll loop adds work without new info, and "47 connector still
  down · 3 send already in flight" is more actionable than "47 still
  failing". Promotable to a live chip later if operator feedback
  warrants. Architecture call worth pinning: **`send_draft` body
  was extracted into a reusable `_perform_thread_send` helper that
  preserves the session-spanning-await pattern**, even though the
  AST lint test from ticket 0008 forbids it in general. The
  pattern is allowlisted for this endpoint specifically because the
  message-with-state-flip atom is load-bearing (a crash mid-await
  must leave the thread in a recoverable state — splitting the
  session would create a window where the thread is `SENDING`
  with no `Message` row); restructuring is a separate larger
  ticket. The bulk retry endpoint reuses the helper but handles
  follow-up scheduling separately (`suppress_followup=True`
  hard-coded — the retry IS the first outbound, so the
  `is_first_outbound` follow-up beat schedule was already fired by
  the original send path). Pinned by mocking
  `schedule_followup_send` and asserting `call_count == 0`. Net
  diff: +526 LOC in `autosdr/api/threads.py` (helper extract +
  endpoint + by-reason count + filter), +135 LOC in
  `autosdr/api/schemas.py` (4 new Pydantic models), +421 LOC in
  `frontend/src/routes/Inbox.tsx` (chip row + retry mutation +
  banner), 14 new tests, 0 deps added, 0 schema migrations. 690
  backend tests pass.
- **2026-05-10 (post-ship)** — **0017 ship pivot, mid-implementation.**
  Original plan had a code-driven `Lead.category → register` substring
  map (`autosdr/tone_register.py`, 504 LOC) + per-workspace overrides
  + Settings UI override card + kill switch. Operator pushed back
  mid-build: "the tone registry is over the top. We have an angle
  LLM right? We can in that agent choose a tone right?" Re-councilled
  the seed-map-location question with that fresh framing. The
  original council had anchored on "the **generation** model is bad
  at picking register from prose mid-write" — which is true but
  misframed the problem. The **analysis** model already does enum-
  typed JSON output (it picks `angle_type` from a closed 7-token
  vocabulary every call); adding `tone_register` as an 8th enum
  field is qualitatively the same problem the analysis model already
  solves well, with the bonus that it sees website + reviews +
  signals — not just the freeform `Lead.category` string the
  substring map would have been keyed on. Decision: drop the seed
  map, the workspace overrides, the Settings card, and the kill
  switch. Net: ~700 LOC deleted, ~150 LOC added. Strongest dissent:
  no operator-tunable per-workspace override; recourse for a wrong-
  register draft is HITL take-over (existing flow). Confidence:
  high. Revert path: bump `analysis-v3.7 → v3.6` (drops the schema
  field) — same cost as any prompt-version revert. Lesson logged
  here: when the council's argument structure depends on **which
  LLM call** is doing the work, double-check that the planner
  framed the right LLM call as the chooser.
- **2026-05-10** — Operator-driven planning round, single session.
  Three asks landed: (a) tone-by-occupation, (b) retry the 1000
  connector-failed threads from the notifications, (c) "create one
  to three new features you think would be helpful, consider QoL
  and the general outreach mindset." Resolved as **four tickets,
  not three**, because (b) had a clear upstream cause that should
  be fixed before its symptom: filed **0019 (auto-pause on
  connector circuit-break)** as the upstream fix and **0018 (bulk
  retry from inbox)** as the legacy-pile-up clean-up + future
  resume-affordance. **0017 (occupation-aware tone register)** is
  the highest-RICE ticket in the queue (12.0) — every send, every
  campaign — and addresses exactly the *"can't send 'hey mate' to
  a nail salon, lawyer, or clinic"* problem the operator described
  by replacing prose-driven LLM-judgement category calibration
  with a deterministic register map. **0020 (business-longevity
  signal)** picks up the operator's explicit hint about adding a
  new scraper angle; pure regex extraction in the shared
  `extract_signals_from_soup` so it inherits into both the
  production crawlee worker and the operator's working httpx
  script (the script the operator named as their working tool).
  Sequenced **0019 → 0018 → 0017 → 0020** in *Next* on risk-first
  + RICE: 0019 prevents the symptom that 0018 cleans up (so 0018's
  reach drops once 0019 ships, but its value as the resume-
  affordance for the legacy pile-up justifies it standing in
  Next); 0017 is RICE 12.0 and unblocked; 0020 is RICE 1.25 and
  spike-first (Open Question 4 asks for an incidence-rate spike on
  the QLD dump before committing — if < 4% of leads carry a
  longevity signal, it goes back to *Considered, not committed*).
  Three architectural calls worth pinning: (1) **Tone register is
  generation-only, not evaluator-aware** in v0 (Open Question 2 in
  0017); the evaluator scores against the always-rules so
  register-block can soften the rules invisibly without inflating
  the eval prompt-version bumps. Risk: a clean professional-
  register draft scoring lower for "too formal"; mitigation is a
  golden-replay smoke against
  `scripts/replay_evaluator.py` and a register-stratified
  pass-rate panel in 0016's deploy-watch UI. Council if pass-flips
  > 2/8. (2) **Circuit breaker lives on `BaseConnector`, not as a
  scheduler-side counter**, so SMSGate, TextBee, Override, File
  all wear it identically (Override and File pin
  `failure_threshold = inf`); the existing
  `consecutive_failures` counter on SMSGate gets migrated, and
  TextBee gets a counter for the first time (gap closure).
  Probe loop runs in the FastAPI lifespan only while tripped,
  zero-cost when healthy. Per-thread `send-draft` honours
  `allow_manual_send()` — the operator can always override the
  breaker. (3) **Longevity is a peer angle of `signature_detail`/
  `review_theme`/`brand_voice`, not a sub-case of either** (Open
  Question 5 in 0020); the `angle_type` vocab grows by one
  closed token, the analysis prompt teaches the precedence ladder
  (`not_found > stale_info > signature_detail > review_theme >
  longevity > differentiator > weak_presence > brand_voice >
  fallback`), generation gains exactly one new worked example
  (Example 7). Bumps `analysis-v3.5 → v3.6` and `generation-v8 →
  v9` (+ `→ v10` if 0017 ships first). Open Question 1 in 0020
  flags an existing drift the operator named in this session: the
  production scan worker
  (`autosdr/pipeline/scans.py:35`) imports the **crawlee**
  fetcher, but the operator runs `scripts/enrich_leads_httpx.py`
  manually because crawlee is failing on AU SMB sites. **This
  ticket does NOT migrate the worker** — flagged as a separate
  follow-up — but means 0020's production reach is gated on the
  worker actually fetching pages. Council if the user wants the
  migration in scope. None of the four tickets ran a full council
  mini-round at intake — every Open Question that needed council
  is embedded in the ticket for the implementer to resolve at
  pickup time per the [ticket-implementer skill's pre-flight](
  ../.claude/skills/ticket-implementer/SKILL.md). Confidence:
  high on 0017 + 0019 (operator pain is direct + measurable);
  medium on 0018 (exact concurrency cap and `Retry all`-vs-
  per-batch shape are open); medium-low on 0020 (incidence-rate
  spike is gating). Strongest preserved dissent: the Critic-style
  worry that **0017's register block lengthens the gen prompt by
  ~ 5%** and stacks under an already-bumped tone-cap budget — if
  the Phase 3 #8 tone-block-cap work in *Later* lands, re-measure
  the composed prompt size before merging 0017.
- **2026-05-02** — Shipped tickets 0008 + 0009 back-to-back, in
  that order. 0008 (reply pipeline TX-across-await) was the harder
  problem and the strict prerequisite for 0009 (killswitch inbound
  replay), since the replay drain re-uses `process_incoming_message`
  — without the phased pipeline the second drained inbound would
  deadlock on the writer lock. 0008 OQ1 (the `pending_classification`
  flag) ran a full four-voice council; the chosen path keeps the
  Phase 1 commit as-is and *doesn't* introduce the flag, leaving the
  rare crash-mid-LLM-call window as a self-healing edge that surfaces
  via the operator's Inbox view and a future "claim/lease/complete"
  reconciliation ticket if the case is observed in practice. The
  Skeptic's stronger staging-row alternative was dismissed for
  scope, the Critic's "single bit can't distinguish never-classified
  from Phase-3-failed" pin remains a real risk medium-confidence-
  flagged in the resolution. 0008 OQ2 (executor bounding) and OQ3
  (busy_timeout audit) were resolved without council per the
  "obvious answer once you read the linked code" guidance: default
  unbounded executor, drop the timeout to 5 s globally. 0009's four
  OQs all accepted documented defaults — connector mismatch logs +
  skips + leaves NULL; resume fires `asyncio.create_task` not
  blocking; simulator honours the killswitch identically to /sms;
  log line rewritten to "queued inbound for replay" since dropping
  is no longer truthful. Implementation honoured the strict
  sequence: 0008 unit 1 (AST lint with allowlist for the six pre-
  existing violations) → unit 2 (`_log_call` + `_update_parsed_json`
  on the executor) → unit 3 (busy_timeout) → unit 4 (concurrency
  tests) → unit 5 (ARCHITECTURE.md § 12 written) → 0009 unit 1
  (PausedInbound model) → unit 2 (webhook persists not drops) →
  unit 3 (`drain_paused_inbounds` in `pipeline/replay.py`) → unit 4
  (`/api/status.paused_inbound` aggregate) → unit 5 (frontend types
  + KillSwitch chip) → unit 6 (six durability tests including
  STOP-survives-resume). Six new follow-ups raised: porting the
  scheduler/threads/followup/campaigns/leads/scans paths to the
  phased pattern (each carries its own allowlist note); manual WAL
  smoke after a real 5-inbound rehearsal; potential
  reconciliation-on-restart ticket if the crash window ever bites;
  paused-inbound list-modal endpoint when queue length routinely
  exceeds 10; CLI `autosdr status` extension when the CLI lands;
  per-row delete UI (already on the original "out of scope" list).
  Sequence in `Next` is now **0016 → 0005 → 0007 → docs sync**.
- **2026-05-02** — Shipped ticket 0015 (mobile-responsive operator
  console) the same day it was promoted out of *Considered*. The
  ticket was effectively the precondition for 0005's value (PWA push
  → tap → broken-on-mobile UI = wasted notification), so taking it
  before 0005 is the correct sequencing the project-manager skill had
  already locked in. Implementation followed the council-resolved OQs
  exactly: manual smoke + checklist (no Playwright dep), dense
  card-list (1 title + ≤2 secondary lines + badge row), browser-
  default keyboard handling with a sticky `ComposeBar` as the load-
  bearing fix, and an always-visible killswitch banner pinned to the
  topbar. Two architectural calls worth pinning: (a) the layout split
  is `Sidebar` (`hidden md:flex`) + a brand-new `MobileDrawer`
  controlled by `AppShell` state — `useEffect` on
  `location.pathname` closes the drawer on navigation so the back
  button doesn't leave it open mid-content; (b) the `Button` /
  `Input` primitives use mobile-first sizing classes
  (`h-11 md:h-7|h-9`, `min-h-11 md:min-h-0`) so existing dense
  laptop forms aren't bloated and every tappable affordance hits the
  44 px floor automatically without per-call-site overrides. The new
  `CardList` primitive landed in `ui/` (single-file, ~95 LoC, no new
  deps); every list route now renders both a `hidden md:block` table
  and a `md:hidden CardList`, which keeps each diff per-route
  reviewable rather than coupling them through a polymorphic
  component. `index.css` got the iOS auto-zoom rule (16 px font on
  inputs `<md`) and `.page` / `.page-narrow` were given responsive
  padding (`px-4 py-6` mobile, `md:px-8 md:py-8` desktop). Bundle-
  size impact is negligible (`CardList-*.js` 0.58 KB gzipped). 601
  backend tests still pass — no backend changes were touched.
  README's "deliberately not included" + ARCHITECTURE § 15 both
  rewrote the laptop-only line into a description of the responsive
  shell. **Smoke matrix re-run is the operator-controlled regression
  bar**: when 0005 ships, walk the same 360/390/768/1024 × 7 routes
  matrix from the implementation log to make sure the push deep-link
  lands on a clean view. The single follow-up the Skeptic flagged
  during OQ resolution ("manual decays") is intentionally not
  blocking — promote to Playwright if a second contributor lands.
  Sequence in `Next` is now **0008 → 0009 → 0016 → 0005 → 0007 →
  docs sync.** (0008 + 0009 shipped 2026-05-02 same day; new
  sequence is **0016 → 0005 → 0007 → docs sync**.)
- **2026-05-02** — Refined ticket 0005 (PWA + Web Push) with a
  council-resolved remote-access architecture, AND filed two new
  tickets to sequence in front of it: 0015 (mobile-responsive
  console) and 0016 (in-app LLM deploy-watch surface). The
  council ran on the question *"how does the operator's phone reach
  AutoSDR at home, and how does AutoSDR reach the operator's phone
  for SMS?"* — a question the original 0005 ticket elided by
  assuming LAN. **All three subagents (Skeptic, Pragmatist, Critic)
  independently picked Option A: Tailscale on PC + phone, with
  SMSGate Local Server inside the tailnet (or SMSGate Cloud as the
  operator-choice escape hatch).** Two strong dissents survived
  synthesis and are embedded in the ticket: (1) Pragmatist's *"PC
  bind interface footgun"* — operators install Tailscale, paste the
  README, and find their phone can't reach the dashboard because
  FastAPI is bound to `127.0.0.1`, not the tailnet interface;
  resolved by a startup config-validator warning + a
  Settings → Networking card surfacing the actual bind state. (2)
  Critic's *"push notifications collide with the privacy posture"*
  — Web Push registers against a public origin and the deep-link
  could leak; resolved by a strict notification payload shape
  (thread_id + lead_first_name + hitl_reason + escalated_at,
  *no* message content, *no* lead PII), tailnet-hostname deep
  links, and a unit test pinning the contract. Cloudflare Tunnel
  documented as a deferred upgrade path (public vanity URL),
  port-forward + DDNS rejected as default (most AU home ISPs
  CGNAT). Confidence: medium-high. **The two new tickets address
  what the 2026-05-02 prompt audit work just landed:** 0015
  (mobile-responsive console) is the explicit operator ask AND the
  precondition for 0005's value — pushes that land on a
  laptop-only UI are wasted. 0016 (in-app deploy-watch) surfaces
  the new `scripts/llm_call_metrics.py` + `scripts/replay_evaluator.py`
  CLIs in the operator console so the audit's Phase 2#6 watching
  period actually happens — and unblocks Phase 3 prompt-shrink
  work which is gated on "v4.4-v4.7 deploy is stable for 1-2
  weeks". Sequence in `Next` is now **0008 → 0009 → 0015 → 0016 →
  0005 → 0007 → docs sync.** *Considered, not committed* row for
  "Mobile / responsive layout" struck through and pointed at 0015.
- **2026-05-02** — Shipped the prompt audit
  (`docs/prompt-audit-2026-05-02.md`). The headline was a one-line
  bug fix that recovered ~$8 of wasted spend — `evaluation.py:335`
  had a Python implicit-string-concat foot-gun multiplying the
  background-context block by 60 (every eval call shipped ~63K
  input tokens instead of ~1.5K; half of all LLM spend on the DB
  was wasted bug). Live-replay against real Gemini Flash-Lite
  validated 0 pass flips on 8 historical threads while cutting
  tokens_in 92% and latency 61%. Three things landed alongside the
  bug fix that are arguably more strategic than the bug itself:
  (a) **the regression harness this project never had** — slice
  metrics CLI by `prompt_version` (calls, pass-rate, p10/p50/p90
  of `overall`, attempts-per-send, `$/sent-thread`) plus a golden-
  replay harness that re-runs the *current* prompt against
  historical inputs on a real LLM; default dry-run, `--apply` to
  persist; (b) **prompt module split into named composable constants**
  — `_RULES`, `_WORKED_EXAMPLES`, `_OUTPUT_SCHEMA`,
  `_REFERENCE_EXAMPLES`, `_RULES_OWNER_FIRST_NAME` etc., with a
  byte-stable rendered-prompt SHA-256 lock so any future drift
  fails loudly and forces a deliberate `PROMPT_VERSION` bump; this
  enables ablation experiments (drop `_REFERENCE_EXAMPLES` without
  touching `_RULES`) that weren't possible against monolithic
  triple-quoted strings; and (c) **JSON-schema `response_format`
  at evaluation-v4.7** with three-tier capability detection
  (`json_schema` → `json_object` → text), validated via 8-thread
  golden audit (0 pass flips, ~108 avg `tokens_out` sharply down
  from ~120-180 historical, no self-heal retries). The
  classification reasoning-budget work surfaced **a plot twist
  worth pinning**: Flash-Lite was already skipping thinking by
  default for classification, so the audit's premise ("burning
  reasoning budget on a 60-token output") didn't hold up — setting
  `"low"` *enabled* thinking that wasn't happening, inflated
  tokens 3× and latency 2× and flipped a thumbs-up reply from
  `objection` to `negative`. Default flipped to `"disable"` as a
  no-op-today / guard-against-future-provider-change. Phase 3
  prompt-shrink work (#7 eval-vs-generation dedup, #8 tone block
  cap, #10 franchise list code-only) and Phase 4 #14 (LM Studio
  context bump) deferred to *Later*; all gated on 1-2 weeks of
  stable v4.7 metrics from ticket 0016 (which is itself the in-app
  surface for the audit's CLIs). The audit's own steer was
  explicit: "don't pair the bug fix with a lean-prompts sweep" —
  attribute the next regression cleanly. Critic's strongest
  preserved warning: **the loop can multiply on shrink** —
  smaller prompt → worse draft → eval rejects more → 3× retries
  per send → total spend can RISE even with smaller per-call
  tokens. Watch attempts-per-send, not just tokens dropped. That
  warning is now a `health_flags: alert` rule embedded in ticket
  0016's design.
- **2026-04-30** — Shipped tickets 0013 (broken-website priority)
  and 0014 (social-profile-as-website) back-to-back. The operator's
  ask was "prioritise leads we're confident on (404s, Facebook
  profile as website)" — the project-manager skill's RICE round
  factored that into three tickets (0013 high-confidence 404s, 0014
  social-as-website, 0015 deferred timeout/blocked promotion once a
  scrape-confidence score lands). 0013 introduced the priority-tier
  *machinery*: a predicate module
  (`autosdr/pipeline/priority.py`), a workspace toggle
  (`priority.enabled`, default true), and a careful refactor of
  `_next_queued_leads` that drains the priority bucket map first
  while *carrying* `last_sent_cat` and `intra_batch` state across
  the tier boundary so the cross-tier category-mix rotation is
  byte-identical to the pre-0013 single-tier behaviour. 0014
  *widened* the predicate without touching the picker shape:
  centralised `SOCIAL_HOSTS` vocab in a new
  `autosdr/enrichment_vocab.py` (so the existing extractor regex
  and the new `Lead.website` predicate share one source of truth —
  test pins the equivalence), added a second `priority_reason`
  token (`social_profile_website`) with deterministic precedence
  (`not_found` outranks for the badge, both fire the predicate),
  surfaced the platform token as the *informational*
  `LeadOut.is_social_website` so a 404'd Facebook URL shows both
  signals separately. Bulk SQL count widened to OR the social-host
  prefix `LIKE`s against `Lead.website` (compute-on-read; no
  migration; the ticket explicitly accepted denormalisation as a
  future trade-off if campaigns ever paginate to thousands).
  Import preview returns `social_website_hosts: {platform: count}`
  so operators see priority-tier impact *before* committing.
  Frontend got a new `SocialProfileTag` chip (mustard, distinct
  from the oxblood `PriorityBadge`) plus a per-platform callout on
  `LeadsImport`. 525/525 backend tests, frontend `tsc -b
  --noEmit` clean, pattern-unifier diff scan introduced no new
  drift. Two architectural calls worth pinning: (a) the predicate
  module landed under `autosdr/pipeline/` rather than `autosdr/`
  to keep it in the picker's dependency neighbourhood while staying
  out of the LLM-heavy `pipeline/_shared.py`; (b) the SQL bulk
  count is an *estimator* of the Python predicate (host-only via
  `LIKE`, doesn't mirror urlparse's hostname normalisation), so
  drift is bounded to "we under-count" — never wrong send order.
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
