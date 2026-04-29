# [feature/ai-loop] Enrich leads with website signal before analysis

<!-- TYPE: feature -->
<!-- AREA: ai-loop -->

## Problem

The analysis prompt at [`autosdr/prompts/analysis.py`](../../autosdr/prompts/analysis.py)
(`analysis-v3.4`) is starved of website-side signal. The prompt itself
spells out the constraint:

> A `website` URL alone tells you NOTHING about the site's design, mobile
> experience, speed, layout, photos, copy, or whether it "matches" their
> reputation. You did not visit the site. Do not assert it is bad, dated,
> slow, hard to read on mobile, or any other quality judgment.
> Empty `reviewDetails` / null `webResults` / null `plusCode` / missing
> photos = no signal. Do not invent one.

The Apify Google-Maps dump that produced the QLD lead set
(`tests/fixtures/apify_qld_excerpt.ndjson`) returns `webResults: null` for
every row — the slot is reserved for scraped web content but nothing is
populated. The result is that every cold-outreach angle the LLM picks
collapses into one of three "thin signal" buckets — `weak_presence`,
`fallback`, or a `review_theme` regurgitating one of two review snippets.
Angles like *"their 200-page WordPress site still uses the same hero
image as the listing was rebranded off"* are physically impossible: the
LLM has no way to know.

The operator (single SMB owner running AutoSDR on their laptop) wants
**richer per-lead context that the existing analysis prompt can already
consume**, fetched from the lead's public website during the outreach
pipeline — without bolting a third background service onto the runtime.

Evidence:

- [`autosdr/prompts/analysis.py:74-90`](../../autosdr/prompts/analysis.py)
  — the prompt's "Truthfulness" block. The LLM is correctly refusing to
  invent; the gap is missing input, not a prompt bug.
- [`tests/fixtures/apify_qld_excerpt.ndjson`](../../tests/fixtures/apify_qld_excerpt.ndjson)
  — every row has `webResults: null`. Real operator data.
- [`docs/tickets/0002-reply-rate-by-angle.md`](0002-reply-rate-by-angle.md)
  — the angle-funnel that lets us measure whether enrichment moves
  outcomes is already shipped (`Thread.angle_type`, `/api/stats/angle-funnel`).
- Operator request, 2026-04-28: *"Can you create a feature that is all
  about getting more valuable information for the leads to get better,
  more personalised information... we might scrape the website and grab
  the sitemap, check if its wordpress, check the h1 sections."*

## Hypothesis

If we fetch a small, deterministic set of public-website signals
(`<title>`, meta description, first H1, generator meta + URL-pattern
CMS detection, viewport tag, og:* tags, robots.txt, `sitemap.xml` URL
count + latest lastmod) immediately before the analysis LLM call, and
fold them into `Lead.raw_data['enrichment']`, then:

- The analysis prompt's `angle_type` distribution shifts away from
  `weak_presence` / `fallback` and toward `signature_detail` /
  `differentiator` / `review_theme` for leads with a usable website
  (measurable on `/api/stats/angle-funnel`, stratified by a new
  `enrichment_status` slice).
- Reply-rate per angle on enriched leads is **at minimum no worse**
  than the un-enriched baseline; the bet is a positive lift on the
  enriched cohort within 4 weeks of stratified data.

We are not promising a reply-rate number — we have no baseline yet.
The deliverable is the data pipeline that lets us measure it honestly,
plus the angle-funnel slice that shows it.

## Scope

### Backend

- New module `autosdr/enrichment.py` with:
  - `enrich_lead(*, website_url: str | None, http_client: httpx.AsyncClient,
    budget_s: float) -> EnrichmentResult` — pure async function, no DB
    side effects. Returns a structured envelope with `status`, `signals`,
    `_meta` (see "Enrichment envelope" below).
  - `EnrichmentStatus` Literal: `"ok" | "no_url" | "timeout" | "blocked"
    | "empty_shell" | "not_found" | "error" | "killswitch_aborted"`.
    Mirrors the LLM-cost-tracking precedent from ticket 0006 of using a
    closed vocabulary so downstream surfaces don't drift.
  - Hard caps inside the budget:
    - **≤ 3 HTTP requests per lead** (root URL once, `/robots.txt` once,
      one sitemap URL discovered from robots or the conventional path).
    - **≤ 1.5s per request**, **≤ 4s total budget per lead** (configurable
      via `workspace.settings.enrichment.budget_s`, default 4.0).
    - Request body size cap: **≤ 256 KB per response**, parser truncates
      anything larger (configurable; large WordPress homepages do exist).
    - Single user-agent string: `AutoSDR/x.y (+https://github.com/.../autosdr)`
      — honest, identifiable, blockable.
    - Robots.txt is fetched first when budget allows; a `Disallow: /` for
      our user-agent shortcuts the run with `status="blocked"`.
- New Pydantic `EnrichmentConfig` block on `workspace.settings`:
  - `enabled: bool` (default `true`)
  - `budget_s: float` (default `4.0`, min `1.0`, max `15.0`)
  - `cache_ttl_days: int` (default `30`)
  - `respect_robots: bool` (default `true` — operator can disable for
    aggressive scrape but the default is the polite path)
  - Lives next to `OutreachWindowConfig` in `autosdr/api/schemas.py`.
- Wire into [`autosdr/pipeline/outreach.py::_run_analysis`](../../autosdr/pipeline/outreach.py)
  **before** the existing `analysis.build_user_prompt(...)` call:
  - If `lead.raw_data.get("enrichment", {}).get("_meta", {}).get("fetched_at")`
    is within `cache_ttl_days`, **skip the fetch** and reuse the cached
    blob — re-running outreach for the same lead (e.g. after a HITL
    take-over) must not rescrape.
  - Otherwise, share a workspace-level `httpx.AsyncClient` (created in
    the FastAPI lifespan, similar to the connector wiring) and call
    `enrich_lead(...)`.
  - Fold the result into `lead.raw_data['enrichment']`, commit the
    Lead row before the LLM call so the new signal is durable even
    if analysis crashes downstream.
  - Log `enrichment thread=%s lead=%s status=%s latency_ms=%d cms=%s
    sitemap_count=%s` at info level. Mirror the existing analysis log
    style.
- Killswitch coverage:
  - Wrap each HTTP call in `await killswitch.await_shutdown_or_timeout(...)`-
    style cancellation. A trip during enrichment returns
    `status="killswitch_aborted"` and bubbles `KillSwitchTripped` like
    every other long-running call.
- Bump analysis prompt to `analysis-v3.5` to teach the LLM:
  - That an `enrichment` block may be present in `raw_data`, with a
    documented schema.
  - That `enrichment._meta.status != "ok"` means the website signal is
    **absent**, not "the site is bad" — preserves the existing
    "do not invent" discipline.
  - That `enrichment.signals.cms == "wordpress"` etc. is a fact about
    the lead's stack, not a problem on its own.
  - One worked example for `cms: "wordpress"` + low `sitemap_count`
    (small WP brochure) → `signature_detail` or `differentiator` is
    fair game; high sitemap_count + old lastmod is an honest
    `signature_detail` hook.

### Stats / observability

- Extend `/api/stats/angle-funnel` ([`autosdr/api/stats.py`](../../autosdr/api/stats.py))
  with an optional `?enrichment=enriched|unenriched|all` filter
  (default `all`) so the operator can stratify "enriched-vs-not" reply
  rates without a schema change. Implementation reads `Thread.angle_type`
  joined against `Message.metadata->>"$.analysis.enrichment_status"`
  (already populated by the outreach pipeline's analysis_meta dict).
  - Avoids a `Thread.enrichment_status` column on day one — Critic's
    "blurs immutable import facts" concern is addressed by keeping the
    enrichment status on the message metadata where the analysis
    decision was actually made.
- New `Lead`-detail panel field showing the latest enrichment block
  (status + fetched_at + the human-readable summary: `"WordPress, 12
  pages, last update 2024-08"`).

### Frontend

- `frontend/src/lib/types.ts`: mirror the new `EnrichmentConfig` block
  on `WorkspaceSettings` and a small `LeadEnrichment` shape on the
  Lead detail response.
- `BehaviourCard.tsx` (Settings → Behaviour): new "Lead enrichment"
  subsection — toggle `enabled`, slider for `budget_s`, helper text
  spelling out the per-host budget and the polite-default robots
  policy.
- `LeadDetail.tsx`: small "Enrichment" card — `status` badge,
  `fetched_at`, the parsed `signals` blob (CMS, sitemap count, last
  modified, title, H1).
- Angle-funnel panel on `/Logs` and `/CampaignDetail` gains an
  "Enriched / Unenriched / All" segmented control (URL-param
  driven, same pattern as the existing campaign filter).

### CLI

- `autosdr leads enrich --since-days 30 [--limit N] [--dry-run]` — a
  one-shot operator command that runs the same enrichment over leads
  whose `raw_data.enrichment._meta.fetched_at` is missing or older
  than the cache TTL. Useful for a freshly-imported batch that the
  operator wants pre-warmed before the first scheduler tick. Skips
  leads with `do_not_contact_at` set.
- Surface `enrichment.status` as a column in `autosdr leads list`
  (next to `do_not_contact`).

### Migrations

- **No new columns.** Enrichment lives in the existing
  `lead.raw_data` JSON column under a versioned envelope. This avoids
  a `_ADDITIVE_COLUMN_MIGRATIONS` entry and mirrors how `tone_snapshot`
  and `business_data` already work — JSON sub-blobs carrying their
  own version field.

### Enrichment envelope (the contract)

```jsonc
"raw_data": {
  // ... existing Apify fields ...
  "enrichment": {
    "_meta": {
      "version": 1,
      "status": "ok",                       // or one of the EnrichmentStatus values
      "fetched_at": "2026-04-29T03:14:22Z",
      "final_url": "https://example.com.au/", // post-redirects
      "http_status": 200,
      "latency_ms": 842,
      "user_agent": "AutoSDR/0.x (...)",
      "robots_respected": true
    },
    "signals": {
      "title": "Example Plumbing — 24/7 Brisbane plumbers",
      "meta_description": "Family-run plumbing in north Brisbane since 2008.",
      "h1": "24/7 Plumbing in Brisbane",
      "cms": "wordpress",                   // "wordpress" | "wix" | "squarespace" | "shopify" | "webflow" | "duda" | "godaddy" | "custom" | "unknown"
      "cms_evidence": "<meta name=\"generator\" content=\"WordPress 6.5\">",
      "viewport_present": true,
      "is_https": true,
      "og_image_present": true,
      "favicon_present": true,
      "sitemap_count": 12,
      "sitemap_last_modified": "2024-08-12",
      "robots_present": true,
      "external_links_to_socials": ["facebook.com/exampleplumbing"]
    }
  }
}
```

Empty / failed runs still write the `_meta` block (with `status` and
`fetched_at`) so the cache TTL works against attempted-but-failed
fetches the same way as successful ones — preventing the "every tick
re-tries the dead site" failure mode.

### Tests

- `tests/test_enrichment.py` — pure-function tests against an
  in-process httpx mock router:
  - WordPress homepage (generator meta + `/wp-content/` link) → `cms: "wordpress"`.
  - Wix / Squarespace / Shopify / Webflow / Duda / GoDaddy fingerprints.
  - Empty SPA shell (`<div id="app"></div>` only) → `status: "empty_shell"`.
  - 404 / 410 → `status: "not_found"`.
  - Per-request timeout → status `"timeout"`, partial signals preserved
    if the root fetched but sitemap timed out.
  - `Disallow: /` for our user-agent → `status: "blocked"`,
    `_meta.robots_respected: true`.
  - 5xx → `status: "error"`.
  - Sitemap with 12 `<url>` entries + a recent `<lastmod>` → counts
    correctly; sitemap-index (nested) → counts entries from the first
    referenced sitemap, capped at 1 fetch.
  - 4 MB junk response → truncated at 256 KB without exception.
- `tests/test_outreach_pipeline.py` extension: enriched analysis call
  receives the enrichment block in its user prompt (asserted against
  the persisted `LlmCall.user_prompt`).
- `tests/test_outreach_pipeline.py`: re-running outreach within
  `cache_ttl_days` re-uses the existing blob — no second HTTP call.
- `tests/test_stats_angle_funnel.py` extension: filter
  `?enrichment=enriched` returns only threads whose first AI
  message carries `metadata.analysis.enrichment_status == "ok"`.
- `tests/test_cli_leads_enrich.py`: the new CLI command runs against
  a stubbed enrichment function and updates `raw_data` for matched
  leads.

## Out of scope

- **Lead prioritisation / reordering by enrichment quality.** The
  Skeptic's strongest dissent. Filed as a follow-up — see
  *Dependencies* — and depends on this ticket landing first to
  produce the angle-funnel data that would justify the order
  change.
- **Background enrichment worker (asyncio task #3).** Framing B from
  the brainstorm. Defer until tail latency on the inline path proves
  problematic in operator rehearsal.
- **Operator-tunable quality score with weights in Settings.**
  Framing C from the brainstorm. Carries an "AI lead scoring /
  prioritisation" non-goal smell even though deterministic; defer
  until there's an explicit operator request.
- **JS-rendered SPA support.** No headless browser, no Playwright.
  Static HTML only. SPAs return `status: "empty_shell"` and the
  prompt knows to ignore the absent signal.
- **Sub-page crawl.** No fetching `/about`, `/services`, individual
  product pages. Root + robots + sitemap only — three requests per
  lead, hard cap.
- **Full-text page extraction.** We're after structural / metadata
  signals, not body copy. The LLM does not need the homepage's
  hero paragraph; it needs the title and the H1.
- **Re-enrichment / freshness scheduling.** One-shot per outreach,
  cached by TTL. A "re-enrich every N weeks" loop is a follow-up.
- **External enrichment APIs** (Apollo, Clay, BuiltWith). Off-strategy
  — explicitly principle-filtered out at brainstorm.
- **AI-driven lead scoring.** Explicit non-goal in
  `autosdr-doc1 § 5`.
- **Email / phone enrichment.** This ticket is about *website* signal
  for a lead that already has a contact. A separate ticket would
  handle missing contacts.
- **Server-side caching beyond the per-lead TTL** (no Redis,
  no shared HTTP cache). The `httpx.AsyncClient` lives at the
  workspace level for connection pooling only.

## Success criteria

- `enrich_lead(...)` returns within `budget_s` for any URL — verified
  by `tests/test_enrichment.py::test_total_budget_is_a_hard_cap` using
  a deliberately-slow mock server.
- Outreach pipeline run against a lead with `website_url` and a 200
  response writes `lead.raw_data["enrichment"]["_meta"]["status"] ==
  "ok"` and the resulting `LlmCall.user_prompt` contains the title and
  H1 — verified by `tests/test_outreach_pipeline.py::test_outreach_runs_enrichment`.
- Outreach pipeline run against a lead whose website returns a hang
  (mock 30s response with our `budget_s = 4`) writes `status:
  "timeout"` and **still calls analysis** — verified by
  `tests/test_outreach_pipeline.py::test_outreach_proceeds_on_enrichment_timeout`.
- Re-running outreach for the same lead inside the cache window does
  not issue a second HTTP fetch — verified by
  `tests/test_outreach_pipeline.py::test_enrichment_cache_hit`.
- A `Disallow: /` robots.txt for our user-agent results in
  `status: "blocked"` and zero further requests to that host —
  verified by `tests/test_enrichment.py::test_blocked_by_robots`.
- `/api/stats/angle-funnel?enrichment=enriched` returns only threads
  whose first AI message carries `metadata.analysis.enrichment_status
  == "ok"` — verified by `tests/test_stats_angle_funnel.py`.
- The analysis prompt's audit row shows the enrichment block in the
  `user_prompt` field on the dashboard `/Logs` page (no special
  rendering — JSON in the existing payload).
- Killswitch trip during a 4-fetch enrichment burst aborts cleanly
  (`status: "killswitch_aborted"`) and the outer outreach pipeline
  bubbles `KillSwitchTripped` — verified by
  `tests/test_enrichment.py::test_killswitch_aborts_mid_fetch`.
- Frontend `LeadDetail` shows the enrichment card; `BehaviourCard`
  shows the toggle. `tsc -b --noEmit` clean.

## Effort & risk

- **Size:** L (~1.5–2 weeks). RICE-derived M was challenged by the
  Skeptic and accepted as undersized given polite-fetch ergonomics,
  CMS-fingerprint test matrix, prompt-version bump, and the
  enrichment-stratified angle-funnel work.
- **Touched surfaces:**
  - `autosdr/enrichment.py` (new)
  - `autosdr/pipeline/outreach.py::_run_analysis`
  - `autosdr/prompts/analysis.py` (`v3.4 → v3.5`)
  - `autosdr/api/schemas.py` (new `EnrichmentConfig`)
  - `autosdr/api/stats.py` (`enrichment=` filter)
  - `autosdr/cli.py` (new `leads enrich` subcommand)
  - `autosdr/workspace_settings.py` (merge default for `enrichment` block)
  - `frontend/src/lib/types.ts`, `BehaviourCard.tsx`,
    `LeadDetail.tsx`, the angle-funnel panel.
- **Change class:** additive (no schema changes; new module; new
  prompt version; opt-out via the `enabled` toggle).
- **Risks:**
  - **Hot-path latency.** Mitigated by `budget_s`, per-request cap,
    `enabled` toggle, and cache TTL. Honestly: 4s of synchronous wait
    inside the outreach pipeline is the cost of not adding a third
    asyncio task. If rehearsal shows tail-latency problems we promote
    Framing B.
  - **Polite-fetch ergonomics.** robots.txt parsing edge cases,
    weird redirect chains, sites that 200 with a CDN block page.
    Tests cover the obvious ones; production will surface more.
  - **Bimodal signal.** Not all websites yield useful structure. The
    `fetch_status` taxonomy plus the angle-funnel `enrichment=`
    filter make this measurable instead of opaque.
  - **Prompt-version compatibility.** `analysis-v3.4` rows continue to
    appear in `LlmCall` history. The "By angle" panel already handles
    historical NULL `angle_type`; nothing breaks.
  - **Killswitch coverage.** Three new HTTP I/O calls become hot
    paths; explicit test required.
  - **Privacy / IP shape.** AutoSDR egress IP becomes visible to the
    lead's web server. Documented in the README under "Network
    behaviour"; the user-agent identifies us. This is the same
    posture as any cold-outreach tool that follows a tracking link.

## Open questions

1. **Should we move enrichment to import-time pre-fetch instead of
   outreach-time?** The pre-fetch path eliminates hot-path latency
   entirely — the cost is up-front (operator imports 1k leads,
   waits ~5min while we enrich the batch). A `bd:`-style background
   worker is **deferred** (Framing B), but the
   importer-runs-the-fetch variant is a real third option. The
   Pragmatist + Critic verdict: ship the inline path first because
   it composes with the cache TTL — pre-fetch can layer on later
   without a schema change. Not gating; will revisit if rehearsal
   shows a problem.
2. **Cache TTL default — 30 days right?** Websites change slowly for
   the Time-Poor Founder persona's typical lead pool (SMBs, not
   tech blogs). 30 days means we re-enrich on the second pass of a
   long-running campaign. Could be 7 or 90 — operator-overridable.
   No principled answer; defaulting to 30 with a settings knob.
3. **Sitemap depth.** A sitemap-index at the root lists multiple
   sub-sitemaps. Do we follow one (counts the first), all (expensive),
   or none (just the count of *index* entries)? **Council-resolved
   to "follow the first referenced sitemap once"** — that's still
   3 fetches max (`/`, `/robots.txt`, the first sitemap), bounded
   and cheap, and gives a useful page count for most SMB sites.
4. **What about leads with no website URL?** Today `Lead.website` is
   already nullable; `enrichment.status` becomes `"no_url"` and the
   prompt knows to fall back. Confirmed: no special-case in the
   pipeline beyond the status code.
5. **Should the `enabled: false` workspace setting also strip
   existing `raw_data.enrichment` blobs from new analysis prompts?**
   Default: **no** — the operator disabled future fetching, but
   data already on the lead is honest historical signal. The
   prompt continues to consume it. This matches the principle
   "Owner stays in control" — the operator who flips the switch
   can also clear the blobs via the CLI if they want.

## Council verdict

**Architect:** Ship Framing A. RICE wins by >2x; B/C depend on
operator-pain evidence we don't yet have.
**Skeptic:** Pushes for B (worker isolation) — *FIFO wastes every
downstream improvement on the wrong leads; RICE undervalues retries
and polite robots*.
**Pragmatist:** A, with hard timeouts + observability — *cost of wrong
order is low for a single-operator box; multiplying failure modes
without a measured signal is optimism tax*.
**Critic:** A is defensible but not low-risk — *synchronous network
I/O ties throughput to strangers' servers; signal is bimodal; needs
explicit fetch_status taxonomy and clear separation between import
facts and fetched blobs*.

**Decision:** Framing A, modified by all three voices. Specifically:
the `EnrichmentStatus` taxonomy (Critic) is in scope; per-lead
budget caps (Pragmatist + Critic) are in scope; the versioned
envelope under `_meta.version: 1` (Critic) is in scope. The
Skeptic's worker-isolation push is filed as ticket 0012, blocked on
this ticket producing the stratified angle-funnel data that would
justify it.
**Strongest dissent:** Skeptic — "FIFO wastes downstream improvement"
in the limit. Accepted as a follow-up rather than rejected.
**Confidence:** medium-high.

## Principle check

- **Simplicity first:** ⚠ — adds 3 HTTP calls per outreach. Justified:
  the alternative (background worker + new schema + ordering changes)
  is substantially less simple, and the synchronous path can layer
  pre-fetch later without rework.
- **Quality over speed:** ✓ — explicit "60-second message that
  resonates" trade-off, paid in 4s of pre-fetch budget.
- **Honest data contracts:** ✓ — `EnrichmentStatus` taxonomy is
  closed; the `_meta` block timestamps every fetch; `fetch_status !=
  "ok"` is surfaced in the UI rather than papered over. The prompt
  bump explicitly teaches the LLM how to read absent signal.
- **Extensible by design:** ✓ — the `enrich_lead(...)` function is
  pure-async with a typed result; a future LinkedIn / Companies-House
  enricher slots in next to it. Operator-controlled via settings.
- **Human always wins:** ✓ — analysis still runs even on enrichment
  failure (`status != "ok"` does not block the LLM call). Killswitch
  preempts mid-fetch.
- **Owner stays in control:** ✓ — workspace toggle, `budget_s` knob,
  cache TTL, robots.txt opt-out, CLI dry-run mode. Disabling
  enrichment does not break legacy data.

## Links

- Spec: `autosdr-doc1-product-overview.md § 5` (non-goal: "Website
  scraping / lead enrichment agents") — explicitly promoted off the
  non-goals list per operator request 2026-04-28.
- Architecture: [`ARCHITECTURE.md § 3`](../../ARCHITECTURE.md) (component
  map; `enrichment.py` will be added alongside `pacing.py`).
- Code: [`autosdr/pipeline/outreach.py:150-209`](../../autosdr/pipeline/outreach.py)
  (`_run_analysis` is the integration point);
  [`autosdr/prompts/analysis.py:74-90`](../../autosdr/prompts/analysis.py)
  (the truthfulness rule the new prompt-bump must preserve);
  [`autosdr/api/stats.py`](../../autosdr/api/stats.py) (the
  `enrichment=` filter goes here).
- Brainstorm session: `agent-transcripts` (PM session 2026-04-28).
- Council mini-round: full verdict block above; logged in
  `docs/ROADMAP.md` Decisions log.

## Dependencies

- **Blocks:**
  - `0012-lead-prioritisation-by-enrichment` (the Skeptic's pushed
    framing — Background enrichment + tier ordering. Sized after
    this ticket's funnel data lands).
  - `0013-import-time-prefetch` (Open Question 1 — pre-fetch on
    import to eliminate hot-path latency entirely).
- **Blocked by:** none.
- **Related:**
  - `0002-reply-rate-by-angle.md` — the angle-funnel surface this
    ticket extends with a `?enrichment=` filter.
  - `0004-import-field-mapping.md` — provides the import path that
    a future `0013` would hook into.
  - `0001-stop-opt-out-keywords.md` — `do_not_contact` leads are
    skipped by the new `autosdr leads enrich` CLI subcommand.

## Implementation log (2026-04-28)

**Status:** done

| # | Unit | Outcome | Evidence |
|---|------|---------|----------|
| 0 | `docs/PATTERNS.md` outbound-HTTP boundary update | done | `docs/PATTERNS.md:51` (boundary now lists `autosdr/enrichment.py`); decisions log entry 2026-04-28. |
| 1 | `autosdr/enrichment.py` module + tests | done | New module; `tests/test_enrichment.py` 16 tests passing (CMS matrix, robots, timeout, killswitch, sitemap-index follow). |
| 2 | `EnrichmentConfig` schema + workspace defaults | done | `autosdr/api/schemas.py` `EnrichmentConfig`; `autosdr/config.py` `DEFAULT_WORKSPACE_SETTINGS["enrichment"]`. |
| 3 | Wire enrichment into outreach + tests | done | `autosdr/pipeline/outreach.py::_run_enrichment_phase`; tests `test_outreach_runs_enrichment` / `test_outreach_proceeds_on_enrichment_timeout` / `test_enrichment_cache_hit_skips_http` / `test_enrichment_disabled_short_circuits` green. |
| 4 | Bump analysis prompt to `analysis-v3.5` | done | `autosdr/prompts/analysis.py:8` constant; new "Website signal block" section in `SYSTEM_PROMPT`; existing audit tests asserting the version pass. |
| 5 | `/api/stats/angle-funnel?enrichment=` filter | done | `autosdr/api/stats.py` correlated EXISTS over `Message.metadata_["analysis"]["enrichment_status"]`; `tests/test_stats_angle_funnel.py` three new tests. |
| 6 | CLI `leads enrich` + `leads list` | done | `autosdr/cli.py::leads_enrich` + `leads_list`; `tests/test_cli_leads_enrich.py` 6 tests covering dry-run, opt-out skip, persistence, and cache TTL. |
| 7 | Frontend types/Behaviour/LeadDetail/AngleFunnelPanel | done | `frontend/src/lib/types.ts` (EnrichmentConfig, LeadEnrichment, EnrichmentStatus, EnrichmentFilter); `BehaviourCard.tsx` enrichment subsection; `LeadDetail.tsx` enrichment card; `AngleFunnelPanel.tsx` segmented control wired through `Logs.tsx` (URL param) and `CampaignDetail.tsx` (local state). `tsc -b --noEmit` clean. |

**Final state of success criteria:**
- `enrich_lead` returns within `budget_s` on a slow mock — ✓ via `tests/test_enrichment.py::test_total_budget_is_a_hard_cap`.
- Enriched lead → `raw_data.enrichment._meta.status == "ok"` and analysis prompt carries title + H1 — ✓ via `tests/test_outreach_pipeline.py::test_outreach_runs_enrichment`.
- Hung site → `status: "timeout"` and analysis still runs — ✓ via `tests/test_outreach_pipeline.py::test_outreach_proceeds_on_enrichment_timeout`.
- Cache hit inside TTL — no second HTTP fetch — ✓ via `tests/test_outreach_pipeline.py::test_enrichment_cache_hit_skips_http`.
- `Disallow: /` → `status: "blocked"` — ✓ via `tests/test_enrichment.py::test_blocked_by_robots`.
- `?enrichment=enriched` returns only `metadata.analysis.enrichment_status == "ok"` threads — ✓ via `tests/test_stats_angle_funnel.py::test_enrichment_filter_enriched_returns_only_ok`.
- Killswitch trip mid-fetch surfaces `status: "killswitch_aborted"` and re-raises — ✓ via `tests/test_enrichment.py::test_killswitch_aborts_mid_fetch`.
- `LeadDetail` enrichment card + `BehaviourCard` toggle, `tsc -b --noEmit` clean — ✓.
- The audit row shows the enrichment block in `user_prompt` on `/Logs` — ✓ (the analysis user prompt JSON-renders `lead.raw_data` which now folds in `enrichment`).

**Principle check after implementation:**
- Simplicity first: ⚠ — three new HTTP calls per outreach. Pre-flagged in the ticket; mitigated by `budget_s`, per-request cap, `enabled` toggle, and cache TTL. No regression vs. ticket guarantees.
- Quality over speed: ✓ — explicit "60-second message that resonates" trade-off, paid in 4s of pre-fetch budget.
- Honest data contracts: ✓ — closed `EnrichmentStatus` vocabulary; `_meta` block timestamps every fetch; failures surface (timeout / blocked / disabled) rather than papered over. Prompt v3.5 explicitly teaches the LLM how to read absent signal.
- Extensible by design: ✓ — `enrich_lead` is pure-async with a typed result; LinkedIn / Companies-House enricher slots in next to it.
- Human always wins: ✓ — analysis still runs on enrichment failure (`status != "ok"` does not block the LLM call). Killswitch preempts mid-fetch.
- Owner stays in control: ✓ — workspace toggle, `budget_s` knob, cache TTL, robots-respect toggle, CLI `--dry-run`. Disabling enrichment does not strip existing blobs.

**Tests run:** full backend suite (446 passed); frontend `tsc -b --noEmit` (0 errors).

**Pattern-unifier (diff-only):** No new ⚠ or ✗ introduced. `httpx` lifecycle / type-only references in `webhook.py`, `scheduler.py`, `pipeline/outreach.py`, and `cli.py` are explicitly OK under the updated `docs/PATTERNS.md` outbound-HTTP boundary note.

**Follow-ups raised:** (none new — `0012` and `0013` were already filed when the ticket was scoped.)

**Open questions still unresolved:** (none — all five resolved during pre-flight; verdicts are recorded above.)
