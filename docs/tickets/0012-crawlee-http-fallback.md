# [feature/scraper] HTTP-based Crawlee fallback for lead enrichment (deferred)

<!-- TYPE: investigation -->
<!-- AREA: scraper -->
<!-- STATUS: deferred — see Phase 1 evidence -->

## Problem

After the inline lead-enrichment fetcher (ticket 0011) shipped, we ran a
live test pass against 20 real operator leads to see whether `httpx`
was hitting block rates that would justify swapping the fetcher.

Command:

```bash
uv run autosdr leads enrich --since-days 365 --limit 20 --report --no-persist
```

Captured 2026-04-28, full output in
[`data/enrichment-live-report-20260428.md`](../../data/enrichment-live-report-20260428.md):

| status   | count | share  |
|----------|-------|--------|
| ok       |    13 | 65.0%  |
| timeout  |     5 | 25.0%  |
| error    |     2 | 10.0%  |

- p50 latency = 2236 ms, p95 = 3227 ms, max = 4222 ms (capped at 4 s budget)
- `block_rate = (blocked + timeout) / total = 5 / 20 = 25.0%`
- **Zero `blocked` (robots / 403 / anti-bot) results.** Every failure is one
  of: a slow upstream that exceeded the 4 s budget (5×) or a connection-level
  error (2× — one DNS miss for `kingsblockeddrain.solutions`, one connection
  failure on `picklespdg.com.au`).

The plan's threshold (`> 20%`) was tripped numerically but the diagnosis
matters: Crawlee's value-add is robots cache + identifiable UA rotation +
queue + exponential backoff + session pool. **None of those address slow
upstreams or dead DNS.**

## Decision

**Defer.** Don't swap the fetcher today. Two cheaper remediations are
available if the timeout floor turns into a real angle-quality regression:

1. Bump `workspace.settings.enrichment.budget_s` from `4.0` → `6.0`. The
   p95 of 3227 ms says we're well under the budget on average, but the
   max of 4222 ms says some sites genuinely need a touch longer. A 6 s
   budget would have flipped at least 2-3 of the 5 timeouts to `ok`
   based on the latency distribution.
2. Accept the timeout floor. The scan worker now runs in the background
   (ticket plan from 2026-04-28); a 25% timeout rate on the worker path
   does not block outreach because outreach reads cached blobs only.
   Worst case, the angle for those 5 leads falls back to `fallback` /
   `weak_presence` — already handled.

The seam to slot in a second fetcher cleanly is already in place from
the same plan:

- `_meta.connector` and `_meta.connector_version` on the persisted
  envelope.
- The scan worker's `_is_envelope_fresh` check invalidates whenever
  either of those changes, so adding a `connector="website_crawlee"`
  variant automatically marks every existing v1/website_static blob
  as stale.

## Triggers to revisit

Re-open this ticket if any of the following holds for a future cohort:

- `blocked` (robots-disallowed / 403 / 429 / explicit anti-bot HTML)
  exceeds 5% of a sample of ≥ 100 leads.
- A specific lead segment we want to enter (eg. high-end e-commerce)
  consistently produces `blocked` rather than `timeout`.
- We add a second connector that needs a richer fetch primitive
  (request queueing, retry policy) and `httpx` becomes load-bearing
  for two callers.

## Sketch (when revisited)

- New module `autosdr/enrichment/website_crawlee.py` exposing the same
  `enrich_lead()` signature and returning the same `EnrichmentResult`.
- `crawlee` Python's `HttpCrawler` (or `BeautifulSoupCrawler` if we want
  the parser inside the request loop) — wraps `httpx`, no Playwright.
- Workspace setting `enrichment.fetcher: "httpx" | "crawlee"` to gate
  selection at runtime; the scan worker reads it before constructing
  the per-pass dispatcher.
- A/B run: leave `httpx` writing `connector="website_static"`,
  `crawlee` writing `connector="website_crawlee"`. The angle-funnel can
  already stratify by enrichment status; we'd add a connector-level
  split to compare reply-rate across the two fetchers honestly.

## Out of scope

- Switching to Playwright / Chromium-based scraping. We deliberately do
  not want a headless browser on the operator's laptop.
- Per-lead retry loops. The 4 s wall-clock budget is a feature, not a
  bug — extending it to 6 s is the correct knob.

## Evidence link

- [`data/enrichment-live-report-20260428.md`](../../data/enrichment-live-report-20260428.md)
- `autosdr/cli.py` — the `--report` flag on `leads enrich` is a
  permanent fixture so the operator can re-run this measurement at any
  time.
