# [feature/ai-loop] Extract business-longevity signal as a new positive angle

<!-- TYPE: feature -->
<!-- AREA: ai-loop / data -->

## Problem

The Time-Poor Founder asked for a new angle from the scraper:

> *"Might be worth a new addition to the scraper to get a new angle idea? …
> I think we have got the site age right? Note we only are using the httpx
> python script as its the fastest and works."*

Two facts the question turns on:

1. **The httpx fetcher is the operator's working tool.** They run
   `scripts/enrich_leads_httpx.py` because the crawlee-based fetcher under
   `autosdr/enrichment.py` has been retiring sessions too aggressively on
   AU SMB websites (the 2026-04-29 cohort hit 13/20 errors that were
   actually 403/503 anti-bot misclassifications). The httpx variant uses
   browser-like headers, an explicit concurrency cap, and works on the
   real cohort. New scraper signals must extract from the **same fetched
   HTML** that the httpx script already pulls — pure-Python signal
   extraction, no new dependencies.
2. **"Site age" is partially extracted.** The shared extractor at
   [`autosdr/enrichment_extract.py`](../../autosdr/enrichment_extract.py)
   already pulls `copyright_year` from `©` / `&copy;` / `(c)` /
   `Copyright` patterns
   ([line 91-93](../../autosdr/enrichment_extract.py)). What's missing is
   **business longevity** — the "trading since 1995" / "established 2003"
   / "family-owned for 30 years" / "in business 25 years" signal that AU
   SMB sites *love* to put on their homepage. Copyright year is a weak
   proxy for it (a footer's `© 2024` only tells us the site is current,
   not how long the business has existed).

Why does this matter for outreach?

- The current generation prompt has six worked examples spanning
  `stale_info`, `weak_presence`, `signature_detail`, `differentiator`,
  `review_theme`, `brand_voice`, `fallback`. **None of them lean on
  business longevity** — the closest is `differentiator` ("they have a
  signature thing") which is more about offering than tenure.
- The "POSITIVE-SIGNAL PIVOT" rule in
  [`autosdr/prompts/generation.py:202-214`](../../autosdr/prompts/generation.py)
  expects the model to lead with the positive when no problem-claim is
  evidenced. Today the only positives the audit knows are *good reviews*
  + *signature detail* + *brand voice*. Longevity ("Skybound have been
  running their café in West End for 32 years — a homepage that says so
  upfront builds trust faster than reviews") is a fourth positive
  archetype that the model can't lean on because the signal isn't
  surfaced.
- Aussie cold outreach mindset: a "20+ years in business" / "family
  owned since 1987" lead is high-trust → you can land a *softer*
  introduction without sounding sales-driven. Exactly the **"can't be
  salesy because they smell it"** rule the operator named in this
  session.

The principle that bites here is **"Honest data contracts"** — we already
fetch the HTML, we already extract 26 signal fields; not extracting
longevity means the LLM has to (a) infer it from the snippet, (b) decide
whether to lean on it, (c) get it right. Three points of LLM judgement we
can replace with a regex.

Evidence:

- Operator quote, 2026-05-10 (above).
- [`autosdr/enrichment_extract.py:91-96`](../../autosdr/enrichment_extract.py)
  — current `_COPYRIGHT_RE`, ABN, ACN extraction. New regex slots in
  alongside.
- [`scripts/enrich_leads_httpx.py:64-79`](../../scripts/enrich_leads_httpx.py)
  — the httpx fetcher the operator runs; emits the same envelope
  shape via the shared `extract_signals_from_soup`.
- [`autosdr/prompts/generation.py:204-214`](../../autosdr/prompts/generation.py)
  — POSITIVE-SIGNAL PIVOT rule the new angle leans on.
- [`autosdr/prompts/analysis.py`](../../autosdr/prompts/analysis.py) —
  picks `angle_type` based on what's in the enrichment envelope. Adding
  a longevity signal needs a corresponding angle type.

## Hypothesis

If we extract a structured **business longevity** signal from the same
fetched homepage HTML — `years_in_business: int | None` + `founded_year:
int | None` + `evidence_phrase: str` — and surface it through the analysis
prompt as a new `longevity` angle type, then:

- A measurable share of QLD leads (target ≥ 8% based on a sample of the
  user's 323 MB QLD dump — see Open Question 4) carries a longevity
  signal that today's prompt ignores.
- Drafts on those leads open with a longevity-aware archetype (e.g.
  `"hey mate, 30 years on the same patch in Stafford Heights — clearly
  doing something right…"`), measurable as: ≥ 80% of `angle_type=longevity`
  drafts contain a longevity phrase per the byte-stable example in the
  prompt.
- Reply-rate per `(angle_type, register)` — the angle-funnel grouping
  from ticket 0017 — gives us data on whether longevity beats fallback
  for thin-signal leads.

Magnitude: at the angle level this is additive — we expect a small but
real RICE win on the slice of leads where longevity is the strongest
positive. Cost: a regex extraction + ~ 1 prompt example.

## Scope

### Backend — extractor

- New constants and regex in
  [`autosdr/enrichment_extract.py`](../../autosdr/enrichment_extract.py):
  ```python
  _LONGEVITY_PHRASES = (
      # "established YYYY", "est. YYYY", "since YYYY", "founded YYYY",
      # "trading since YYYY", "serving X since YYYY"
      ...
  )
  _LONGEVITY_DURATION = (
      # "20 years", "thirty-five years", "for over 25 years",
      # "30+ years in business", "two decades"
      ...
  )
  _DECADE_WORDS = {"decade": 10, "decades": 10, "century": 100, ...}
  ```
- New helper `extract_longevity(body_text: str, *, current_year: int) ->
  dict`. Pure function, no I/O. Returns:
  ```python
  {
      "founded_year": 1995,        # int | None
      "years_in_business": 30,     # int | None — derived from founded_year OR direct phrase
      "evidence_phrase": "Family-owned and operated since 1995",  # str (≤ 120 chars, verbatim from page)
      "evidence_source": "since_year",  # one of: since_year | est_year | duration_phrase | decade_phrase | none
  }
  ```
  Quality rules:
  - Reject `founded_year < 1850` or `> current_year` (cap on plausibility).
  - When both `founded_year` and a direct duration phrase fire,
    prefer `founded_year` (more precise; year-based math beats
    fuzzy phrase math). Surface both in `evidence_phrase`.
  - `years_in_business` = `current_year - founded_year` when only
    `founded_year` fires; else parse the duration phrase
    (`"30 years"` → 30; `"two decades"` → 20; `"a quarter century"` →
    25). Cap at 200 — anything claimed bigger is page noise.
  - **Don't extract from page chrome.** Skip text inside
    `<footer>` / `<nav>` / `<aside>` blocks for the *direct duration*
    phrases (`"20 years experience"` in a footer is usually generic
    template copy). The `since YYYY` patterns are kept for footer
    too because that's where AU SMBs put them.
  - Verbatim phrase truncated to 120 chars (BC with the snippet field;
    operators see the actual line).
- Threaded through `extract_signals_from_soup`:
  ```python
  signals["founded_year"] = lon["founded_year"]
  signals["years_in_business"] = lon["years_in_business"]
  signals["longevity_evidence"] = lon["evidence_phrase"]
  signals["longevity_source"] = lon["evidence_source"]
  ```
- Bump `ENVELOPE_VERSION = 4`
  ([`autosdr/enrichment.py:67`](../../autosdr/enrichment.py)) so the
  scan worker auto-revalidates every cached envelope (mirrors the
  pattern from the 2026-04-28 connector swap). Cached envelopes
  without the new fields are re-fetched on next scan.
- The httpx variant in `scripts/enrich_leads_httpx.py` already calls
  the shared `extract_signals_from_soup`, so it inherits the new
  fields automatically. No script-side changes.

### Backend — analysis prompt

- Add a new `angle_type` token: `longevity` to the closed vocab in
  [`autosdr/prompts/analysis.py`](../../autosdr/prompts/analysis.py)
  (today: `stale_info | weak_presence | signature_detail |
  differentiator | review_theme | brand_voice | fallback`).
- Update analysis prompt's angle-selection rules:
  - When `signals.years_in_business >= 20` *and* no stronger
    signal (`stale_info`, `not_found`, `signature_detail`,
    `review_theme`) is present, prefer `longevity`.
  - When years < 20 or absent, longevity is *not* the angle —
    don't surface "since 2018" as a brag opener.
  - Worked example added — verbatim-extracted phrase as the
    citation, lead with the longevity in the angle text, paired
    with the relevant signal phrase.
- Bump `analysis-v3.5` → `analysis-v3.6` to track the new angle.
  Update the byte-stable SHA test deliberately; one new SHA.

### Backend — generation prompt

- New worked example in
  [`autosdr/prompts/generation.py:_REFERENCE_EXAMPLES`](../../autosdr/prompts/generation.py)
  — example 7 (longevity, café/retail):

  ```text
  Example 7 (longevity, café — 30 years on the same corner, no
  problem-claim because the data is overwhelmingly positive):

    "hey, 30 years in West End is a serious run — clearly doing
    something right. I build websites for a living, happy to put a
    web page together that leads with that local trust. Shoot a
    text if you'd like to see it."
  ```

- Update the SHAPE block to acknowledge `longevity` as a fourth
  positive-pivot pattern (alongside `signature_detail`,
  `review_theme`, `brand_voice`).
- Bump `generation-v8` → `generation-v9` (or `-v10` if 0017 has
  already bumped).
- The TRUTHFULNESS rule already covers this: "you may only assert
  longevity if the signal evidences it" is implicit in the existing
  rule about evidence-backed claims. No new rule text needed; just
  the example.

### Backend — angle-funnel slot

- Update
  [`autosdr/api/stats.py::angle_funnel`](../../autosdr/api/stats.py)
  to include `longevity` in the angle vocabulary returned. Today the
  endpoint groups on `Thread.angle_type`; the new value flows
  naturally without a query change. Frontend chip strip (the
  CSS-bar primitive) renders an oxblood-soft chip for longevity by
  default; theme it forest-soft instead because longevity is a
  positive-pivot angle (matches `signature_detail`'s tone).

### Frontend

- `frontend/src/lib/types.ts` — extend `LeadEnrichment.signals`:
  - `founded_year: number | null`
  - `years_in_business: number | null`
  - `longevity_evidence: string`
  - `longevity_source: "since_year" | "est_year" | "duration_phrase" | "decade_phrase" | "none"`
- "Website enrichment" card on `LeadDetail.tsx` gains a small line:
  > *"30 years in business — 'Family-owned since 1995' (from
  > homepage)"*
  Renders only when `years_in_business != null`.
- Angle-funnel chip in `Logs.tsx` and `CampaignDetail.tsx` adds the
  `longevity` token with a forest-soft tone.

### CLI

- `autosdr leads list` (already exists from ticket 0011) gets a
  `--longevity-min <years>` filter. *"Show me leads with 20+ years
  on file"* — useful for the operator to A/B a longevity-only
  campaign against a control. Also useful for `autosdr logs angles
  --filter longevity`.

### Tests

- `tests/test_longevity_extraction.py` (new):
  - `"Established 1995"` → `founded_year=1995, years_in_business=30
    (vs current_year=2026)`.
  - `"Trading since '95"` → `founded_year=1995` (two-digit year
    expansion is in scope; assume current century for ≤ current 2-
    digit year, prior century otherwise).
  - `"30+ years in business"` → `years_in_business=30,
    founded_year=None`.
  - `"Two decades of service"` → `years_in_business=20`.
  - `"Family-owned for over 25 years"` → 25.
  - **Negative cases:**
    - `"Open since 9 AM"` → none (since-time, not since-year).
    - `"Last updated 2024"` → none (footer chrome).
    - `"30+ years experience in plumbing"` (in a footer) → reject
      direct duration phrase; could still pick up a since-year
      elsewhere on the page.
    - `"founded 2030"` → reject (future year).
    - `"established 1750"` → reject (implausible cap).
  - Source-tagging — every positive case carries a
    `longevity_source` token that matches the regex family that
    fired.
- `tests/test_enrichment_envelope_v4.py`:
  - Cached `_meta.version: 3` envelope is invalidated on next scan
    (covers the version bump path).
  - Brand-new envelope contains the four longevity fields when
    present, and `null` / `""` when absent.
- `tests/test_prompts.py`:
  - `analysis-v3.6` worked example renders byte-stably.
  - `generation-v9/v10` worked example 7 renders byte-stably.
  - Longevity-bearing analysis output round-trips through
    `evaluate_result` without false-positive rejections (smoke
    against `scripts/replay_evaluator.py` golden set extended with
    2 longevity-eligible threads).
- `tests/test_stats_angle_funnel.py`:
  - `longevity` rows appear in `/api/stats/angle-funnel` output
    when seeded.
- `tests/test_outreach_pipeline.py`:
  - Lead with `years_in_business=30` and an `ok` enrichment status
    receives an `angle_type=longevity` thread (not `fallback`).
  - Lead with `years_in_business=null` and otherwise-thin signal
    falls back to today's `fallback` angle (no regression).

## Out of scope

- **About-page scraping.** The first-fetch budget is already 4 s for
  root + robots + sitemap. Adding an `/about` fetch doubles the
  budget for a marginal recall lift. If 0011's data shows ≥ 30% of
  longevity-bearing leads have it on `/about` not `/`, file a
  follow-up.
- **JSON-LD `foundingDate` extraction.** Schema.org has a structured
  `Organization.foundingDate` field. Pulling it would be a clean
  addition but only ~ 5% of AU SMBs ship JSON-LD. Out of scope for
  v0; revisit if the regex recall stalls.
- **WHOIS / domain-age lookup.** Domain age is a different signal
  (when did they buy the URL?) and a different code path (DNS / WHOIS
  protocol, separate budget). Different ticket if it ever matters.
- **Scaling longevity to be a *priority tier*.** Today longevity is
  a positive-pivot angle, not a priority signal. The priority tier
  (ticket 0013/0014) is for *broken* / *missing* sites where the
  pitch is sharpest. Treating "long-tenured business" as priority
  conflates two different operator goals.
- **Backfill of `Thread.angle_type` for legacy threads.** They stay
  on whatever they're on. Re-analysis from a fresh inbound (or a
  manual re-trigger) populates the new angle.
- **Translating verbose phrases.** *"trois décennies"* / *"dreißig
  Jahre"* — AU-English only. Garbage extraction on non-English text
  surfaces as `years_in_business=null`.

## Success criteria

- `extract_longevity(...)` covers ≥ 12 unit cases including the seven
  positive shapes (since-year, est.-year, est.-period, founded,
  trading-since, duration, decade-words) and five negatives.
- A 100-lead manual smoke against `scripts/enrich_leads_httpx.py
  --report` on the QLD dataset surfaces a longevity signal on
  ≥ 8% of leads with website. (See Open Question 4.)
- `signals.years_in_business` is `null` for legacy `_meta.version=3`
  envelopes; refetched envelopes carry the new field on a fresh scan.
- Analysis prompt rendered byte-stably; generation prompt rendered
  byte-stably; both `PROMPT_VERSION` bumps land cleanly.
- Live golden-replay against the existing 8-thread set shows
  **0 pass-flips** on the eval prompt (longevity is additive, not
  reformative).
- A seeded longevity-bearing lead in `tests/test_outreach_pipeline.py`
  produces `Thread.angle_type=longevity` with the longevity phrase
  visible in `Thread.angle`. The drafted message contains the verbatim
  phrase or a paraphrase of the years figure.
- `LeadDetail` shows the longevity line; angle-funnel renders the
  longevity chip; CLI filter works. All new tests pass; 661+ backend
  tests still pass.

## Effort & risk

- **Size:** S (~ 0.5 person-week).
- **Touched surfaces:**
  - `autosdr/enrichment_extract.py` — regex + helper.
  - `autosdr/enrichment.py` — bump `ENVELOPE_VERSION`.
  - `autosdr/prompts/{analysis,generation}.py` — angle vocab + worked example + version bump.
  - `autosdr/api/stats.py` — angle-funnel surfaces the new value (no code change typically; vocab is data).
  - `autosdr/api/schemas.py` — extend `LeadEnrichment.signals` typing.
  - `frontend/src/lib/types.ts`, `frontend/src/routes/{LeadDetail,Logs,CampaignDetail}.tsx`.
  - Tests.
- **Change class:** additive end-to-end. New regex; new envelope fields
  (with NULL fallback for cached envelopes); new closed-vocab token
  on `angle_type`.
- **Risks:**
  - **Regex over-match on dates that aren't longevity.** "Open since 9
    AM", "© 2024", "Australia Day 2024", "2003 Toyota Hilux for sale"
    on a wreckers' homepage. Mitigation: closed phrase set anchored on
    `since|established|est\.|founded|trading|operating` + adjacent year
    digit-pattern; current-year < `founded_year` cap; footer-skip rule
    for direct duration phrases.
  - **Two-digit year ambiguity.** "since '95" → 1995 or 2095? Same
    ambiguity Excel solved decades ago. Default lean: `<= current
    last-2 → 20XX, otherwise 19XX`. Tested.
  - **Drafted phrase mis-quotes the page.** The model could write "40
    years on the same corner" against an evidence_phrase of "30 years
    in business". Mitigation: the prompt gets the verbatim phrase, not
    the int — the model copies what's there. Test the round-trip.
  - **Prompt-version bumps need deploy-watch coverage.** Both
    `analysis-v3.6` and `generation-v9` (or v10) need to register on
    the deploy-watch dashboard from ticket 0016. Composes with that
    ticket's slice metrics.
  - **The httpx-vs-crawlee question.** Today's production scan worker
    is the *crawlee* fetcher, not the httpx script the operator says
    they use. Both call `extract_signals_from_soup` → both inherit the
    new fields. **But** the production worker's reach is gated on
    crawlee actually fetching the page. If the operator's running the
    httpx script *because crawlee is failing on AU SMB sites*, the
    real reach of this ticket is ~ 0 in production until the worker
    migrates to httpx. That migration is a separate ticket (see
    Dependencies). Flag explicitly.

## Open questions

1. **Production worker: crawlee or httpx?** The operator's working
   tool is `scripts/enrich_leads_httpx.py`. The production scan
   worker
   ([`autosdr/pipeline/scans.py:35`](../../autosdr/pipeline/scans.py))
   imports `enrich_lead` from `autosdr.enrichment` — the
   crawlee implementation. If crawlee is failing on the operator's
   real cohort and the operator runs the httpx script manually
   instead, the production worker is doing nothing useful. **This
   ticket should not migrate the worker** — that's a separate,
   bigger ticket — but should flag the gap. Council if the user
   wants the migration in the same scope.
2. **Threshold on `years_in_business` for the `longevity` angle.**
   The default lean is `>= 20` (a generation in business is a real
   trust signal). Lower (`>= 10`) widens reach but dilutes the
   "decades-old, clearly doing something right" effect. Higher
   (`>= 30`) is rarer. Council; default 20.
3. **Angle precedence vs. `signature_detail` and `review_theme`.**
   What if a lead has both 30 years in business *and* 200 reviews?
   `review_theme` is currently the strongest positive. Default
   precedence: `not_found` > `stale_info` > `signature_detail` >
   `review_theme` > `longevity` > `differentiator` > `weak_presence`
   > `brand_voice` > `fallback`. Longevity slots in **after**
   review-theme because review-theme is more recipient-specific
   ("the $5 Friday meals"); council if it should outrank.
4. **What's the actual incidence rate?** The hypothesis says
   ≥ 8%, but that's an estimate. Run a one-off on the QLD dump
   (`scripts/enrich_leads_httpx.py --limit 1000 --report` + a
   throwaway grep against the new signals dict) before
   implementation to size the actual reach. If < 4%, the angle is
   too rare to justify the prompt-version bump and we file as
   *Considered, not committed* instead.
5. **Should the longevity angle be a positive-pivot or its own
   archetype?** Today the prompt has six angle types + `fallback`.
   The new angle could be (a) `longevity` as a peer (recommended),
   or (b) re-frame `signature_detail` to absorb longevity as a
   sub-case. Default: (a). (b) makes the analysis prompt simpler
   but conflates two operator-meaningful signals.
6. **Two-digit year cutoff: 49 / 50?** A "since '49" → 1949 or
   2049? With current_year=2026, "since '99" → 1999 (clearly past),
   but "since '40" is ambiguous. Default lean: cutoff at *current
   last-2 digits + 5* — anything ≤ that (e.g. ≤ 31 in 2026) → 20XX,
   anything beyond → 19XX. Trade-off accepted: 5 years of
   ambiguity gets resolved in favour of "this is the current
   century" — wrong for businesses founded between 1932 and 1949,
   but those are rare. Council if it bites in real data.

## Principle check

- **Simplicity first:** ✓ — regex extraction + one new vocab token.
- **Quality over speed:** ✓ — gives the model a concrete positive
  to lean on instead of inferring from prose. Less salesy as a
  result.
- **Honest data contracts:** ✓ — promotes longevity from "the LLM
  is supposed to spot it in the snippet" to "structured signal in
  the envelope".
- **Extensible by design:** ✓ — additive on the closed `angle_type`
  vocab, additive on the envelope. Future longevity sources (JSON-LD,
  WHOIS) plug in without re-shaping.
- **Human always wins:** ✓ — operator can disable the angle by
  removing the example from the prompt; no auto-action.
- **Owner stays in control:** ✓ — every signal surfaces on
  `LeadDetail`; CLI filter and angle-funnel both surface the slice.

## Links

- Spec: `autosdr-doc1-product-overview.md § 3 (Principles)` —
  *"story-branded, not salesy"*.
- Architecture: `ARCHITECTURE.md § 3 (Components)` — enrichment
  + analysis + generation.
- Code:
  - `autosdr/enrichment_extract.py:91-96` — copyright + ABN +
    ACN extraction. Longevity slots alongside.
  - `autosdr/enrichment.py:67` — `ENVELOPE_VERSION` to bump.
  - `autosdr/prompts/analysis.py` — `angle_type` vocab.
  - `autosdr/prompts/generation.py:_REFERENCE_EXAMPLES` — worked
    examples.
  - `scripts/enrich_leads_httpx.py:64-79` — operator's httpx fetcher.
- Audit: [`docs/prompt-audit-2026-05-02.md`](../prompt-audit-2026-05-02.md)
  Phase 3 #9 — module-split refactor that makes ablation surgical.

## Dependencies

- **Blocks:** none.
- **Blocked by:** none directly. Soft-blocked by ticket 0016
  (deploy-watch dashboard) for the prompt-version slice metrics.
  Composes with ticket 0017 (occupation-aware tone register) — a
  longevity-bearing lead at a `professional` register reads very
  differently from a `tradie` lead with the same signal; both
  layers compose cleanly because they're orthogonal.
- **Related:** *Later* item "Pre-fetch enrichment at import time" —
  composes; longevity extraction is part of the enrichment envelope.
  *Later* item "A/B compare two personalisation angles per lead" —
  longevity is a natural A in an A/B against `fallback` for
  thin-signal leads.
