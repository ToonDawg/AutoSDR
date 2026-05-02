# [feature/scheduler] Treat social-profile-as-website as a priority signal

<!-- TYPE: feature -->
<!-- AREA: scheduler -->

## Problem

Some imported leads have a social-media profile URL where the
operator expects a corporate website — `facebook.com/jims-mowing-qld`,
`instagram.com/some-spa`, etc. That's a **strong** "they don't have
a real website" signal: the pitch ("we'll get you a real website")
lands harder than for any other broken-site state. The operator
explicitly raised this in the planning round (2026-04-30):
*"prioritise things we are confident on (404's on the scan should be
pretty reliable I think, **or facebook profile as their website
link?**)"*.

Today the scheduler doesn't see this signal at all:

- The lead-website fetcher's [`normalise_website_url`](../../autosdr/enrichment.py)
  accepts `facebook.com/somepage` as a valid URL and goes on to
  scrape it. Facebook's anti-bot then either 200s or 403s; either
  way `enrichment_status` lands as `"ok"` or `"blocked"` — neither
  fires the priority predicate from ticket 0013.
- The signal extractor at
  [`autosdr/enrichment_extract.py:77-79`](../../autosdr/enrichment_extract.py)
  centralises the seven platforms we already track on the
  *external* side (`facebook`, `instagram`, `linkedin`, `twitter`,
  `x`, `tiktok`, `youtube`) — but it scans the *page body*, not
  `Lead.website` itself. A lead whose `Lead.website` IS a Facebook
  profile is invisible to the rest of the system.
- The scan worker [`autosdr/pipeline/scans.py`](../../autosdr/pipeline/scans.py)
  has no concept of "the URL itself was a social profile" — it
  scrapes whatever it gets.

Evidence:

- Operator request, 2026-04-30 (above quote).
- [`autosdr/enrichment.py:133-154`](../../autosdr/enrichment.py)
  — `normalise_website_url` is permissive on hostname.
- [`autosdr/enrichment_extract.py:77-79`](../../autosdr/enrichment_extract.py)
  — seven-platform vocabulary the new helper must mirror.
- [`docs/tickets/0013-broken-website-priority.md`](0013-broken-website-priority.md)
  — defers the social signal explicitly: *"social-profile-as-website
  detection — whole signal class — ticket 0014"*.

## Hypothesis

If the priority predicate also fires when `Lead.website` is itself
a profile URL on one of the seven tracked social platforms, then
the operator's batch will spend its first messages on leads with the
sharpest pitch (clearly broken websites + clearly missing websites)
without distinguishing between the two failure modes — both deserve
priority.

The combined predicate is `is_priority(lead)` =
`enrichment_status == "not_found"` OR
`is_social_website(lead.website) is not None`. Tier dimension and
category-mix interleave from 0013 stay untouched; this ticket only
widens the predicate.

## Scope

### Backend

- New helper `is_social_website(url) -> str | None` in
  [`autosdr/enrichment.py`](../../autosdr/enrichment.py)
  next to `normalise_website_url`. Returns the platform token
  (`"facebook"`, `"instagram"`, `"linkedin"`, `"twitter"`, `"x"`,
  `"tiktok"`, `"youtube"`) or `None`. Hostname-suffix match against
  the same vocabulary as `_SOCIAL_RE` in
  [`autosdr/enrichment_extract.py:77-79`](../../autosdr/enrichment_extract.py)
  — exposed as a single `_SOCIAL_HOSTS: frozenset[str]` constant
  imported by both modules so the vocab can't drift. URL parsing
  via `urlparse(url).hostname` (lower-cased, leading `www.`
  stripped). Quiet on garbage input — matches `normalise_website_url`'s
  permissive contract.
- Extend `is_priority_lead(lead)` and `priority_reason(lead)` in
  [`autosdr/pipeline/priority.py`](../../autosdr/pipeline/priority.py)
  to OR in the social check. New constant
  `PRIORITY_REASON_SOCIAL_PROFILE_WEBSITE = "social_profile_website"`.
  Precedence: `not_found` outranks `social_profile_website` when
  both fire (so a 404 from a Facebook profile reads as `not_found`
  in the badge — keeps a single deterministic winner).
- Bulk count `_campaign_queued_priority_bulk` in
  [`autosdr/api/campaigns.py`](../../autosdr/api/campaigns.py)
  gains an OR clause with prefix `LIKE` patterns over
  `Lead.website` for the seven `https://(www\.)?(host)/` shapes.
  Helper `_social_website_sql_predicate(...)` keeps the OR
  composable. Tested against an in-memory mix.
- New `LeadOut.is_social_website: str | None` in
  [`autosdr/api/schemas.py`](../../autosdr/api/schemas.py) —
  surfaced on every `LeadOut` response via the existing
  `_lead_to_out` helper. Pure informational; the priority decision
  is still on `is_priority` / `priority_reason`. The platform
  token (`"facebook"`, etc.) drives a future "Facebook profile" /
  "Instagram profile" tag distinct from the priority badge if the
  operator wants both visible.
- Import preview surface: extend
  [`autosdr/api/schemas.py::ImportPreviewOut`](../../autosdr/api/schemas.py)
  with `social_website_hosts: dict[str, int]` (`{"facebook": 12,
  "instagram": 3}`). Counts come from running the new helper over
  the parsed rows in `autosdr/importer.py::preview_import_file`.
  Empty dict if no social URLs in the batch — frontend renders
  nothing in that case.
- The fetcher itself stays untouched — `enrich_urls` still scrapes
  Facebook pages because some operators do legitimately have a
  Facebook page that returns useful signal (open hours, address);
  surfacing the social-as-website flag is **independent** of
  whether we scrape it. Operator gets both signals.

### Frontend

- `frontend/src/lib/types.ts`:
  - Mirror `is_social_website: string | null` on `Lead`.
  - Mirror `social_website_hosts?: Record<string, number>` on
    `ImportPreview`.
- `PriorityBadge` from 0013 grows the new reason in its label/tooltip
  table:
  - `social_profile_website` → label `"Social profile"`, tooltip
    `"This lead's website is a social profile (Facebook, Instagram, etc.) — they likely don't have a corporate site, so the scheduler sends them before normal-tier leads."`
- New tiny primitive `SocialProfileTag` in
  `frontend/src/components/domain/` — purely informational, e.g.
  `Facebook profile` chip rendered next to the contact details on
  `LeadDetail`. Visible regardless of whether the lead is priority
  (so an operator can see "we have a social URL here, the scan
  returned ok" without conflating with the priority badge).
- `Leads.tsx` + `LeadDetail.tsx`: render `SocialProfileTag` next to
  the website cell when `lead.is_social_website != null`.
- `LeadsImport.tsx` (the import preview screen): when
  `preview.social_website_hosts` is non-empty, render a small
  callout above the Skip-reasons table:
  `"12 leads have Facebook as their website. They'll be flagged as priority."`
  Plain text, one line per platform with a count > 0.
- No Settings → Behaviour change. The existing `priority.enabled`
  toggle covers both signals — operator's "turn off priority"
  expectation matches "stop elevating broken-site leads" regardless
  of which sub-signal fired.

### Migrations

- **No new columns.** Predicate is computed on read from
  `Lead.website` (operator already imports it). No backfill, no
  migration entry.

### Tests

- `tests/test_enrichment_social_website.py` (new):
  - Truth table for `is_social_website` across the seven platforms,
    with and without `www.`, with paths, with HTTPS / HTTP, with
    trailing slashes, with `None`, with garbage, with corporate
    URLs that mention a platform in the path (e.g.
    `https://acme.com/facebook-marketing` must read as `None`).
  - Verifies it shares the platform vocab with
    `_SOCIAL_RE` — assert `_SOCIAL_HOSTS` matches the regex's
    alternation set.
- `tests/test_priority_lead.py` extension (no new file): fold in
  parametrize rows for the social-as-website cases against
  `is_priority_lead` (which now reads `Lead.website`).
- `tests/test_scheduler_priority.py` extension: one test that
  mixes a 404 lead, a Facebook-as-website lead with
  `enrichment_status = "ok"`, and a normal lead — verifies the
  priority tier picks both 404 and Facebook leads before the
  normal lead, with the existing category-mix rotation untouched.
- `tests/test_campaign_api.py` extension: `queued_priority_count`
  counts queued leads where EITHER `enrichment_status == "not_found"`
  OR the website hostname is on the social list. New test pins the
  OR semantics; existing 0013 tests still pass unchanged.
- `tests/test_lead_priority_api.py` extension: a Facebook-as-
  website lead reads as `is_priority=true,
  priority_reason="social_profile_website",
  is_social_website="facebook"`. A 404 + Facebook lead reads as
  `priority_reason="not_found"` (precedence) but
  `is_social_website="facebook"` (informational still set).
- `tests/test_leads_import_api.py` extension: import preview of a
  CSV containing two Facebook URLs, one Instagram URL, three
  normal URLs returns
  `social_website_hosts == {"facebook": 2, "instagram": 1}`.

## Out of scope

- **Persisting `is_social_website` as a denormalised column on
  `Lead`.** Compute-on-read keeps the migration out of this ticket;
  the bulk SQL OR clause runs over the `idx_lead_enrichment_status`
  composite index for the not_found half and an in-query `LIKE`
  prefix match for the social half — fine at our row counts. If
  operator pain emerges (queries slow, queue counts jitter), file
  a follow-up to denormalise.
- **Per-platform toggles.** No operator asking; today's
  `priority.enabled` covers the whole signal class.
- **Distinguishing `linkedin.com/company/...` from
  `linkedin.com/in/...`.** Both are LinkedIn profiles for our
  purposes — the operator's pitch ("you don't have your own site")
  applies regardless.
- **Re-classifying the social URL itself as `Lead.website`.** The
  field stays as imported. A future ticket could surface "did you
  mean to put a real website here?" as an operator nudge in
  `LeadDetail`.
- **Excluding social URLs from the scan worker entirely.** No.
  Some operators legitimately want the page-body signal from a
  Facebook profile (hours, address). The flag is independent of
  whether we scrape.

## Success criteria

- `is_social_website` returns the correct platform token for the
  seven platforms with and without `www.` — verified by
  `tests/test_enrichment_social_website.py::test_truth_table_per_platform`.
- The vocabulary shared between the helper and `_SOCIAL_RE` cannot
  drift — verified by
  `tests/test_enrichment_social_website.py::test_vocab_matches_extract_regex`.
- A queue mixing a 404 lead, a Facebook-as-website lead, and a
  normal lead picks both priority leads first regardless of
  queue position — verified by
  `tests/test_scheduler_priority.py::test_social_website_lead_joins_priority_tier`.
- `LeadOut.is_priority=true, priority_reason="social_profile_website",
  is_social_website="facebook"` for a Facebook-as-website lead with
  `enrichment_status="ok"` — verified by
  `tests/test_lead_priority_api.py::test_lead_out_marks_facebook_as_priority`.
- `LeadOut.priority_reason="not_found"` (precedence) plus
  `is_social_website="facebook"` for a Facebook URL that returned
  404 — verified by
  `tests/test_lead_priority_api.py::test_priority_reason_precedence`.
- `CampaignOut.queued_priority_count` counts queued leads where
  EITHER signal fires — verified by
  `tests/test_campaign_api.py::test_queued_priority_count_includes_social_websites`.
- Import preview returns a non-empty `social_website_hosts` for a
  CSV containing social URLs — verified by
  `tests/test_leads_import_api.py::test_preview_counts_social_website_hosts`.
- Frontend `tsc -b --noEmit` clean.
- `PriorityBadge` renders the social label/tooltip when
  `priority_reason="social_profile_website"`; `SocialProfileTag`
  renders the platform name when `is_social_website` is set
  (independent of priority). Visual check on dev server.
- Import preview screen shows the platform-counts callout when the
  uploaded file has social URLs in the website column.

## Effort & risk

- **Size:** S–M (~0.6 person-weeks).
- **Touched surfaces:**
  - `autosdr/enrichment.py` (new helper + shared vocab constant)
  - `autosdr/enrichment_extract.py` (use the shared vocab constant
    for `_SOCIAL_RE`'s alternation)
  - `autosdr/pipeline/priority.py` (predicate + reason constant)
  - `autosdr/api/schemas.py` (`LeadOut.is_social_website`,
    `ImportPreviewOut.social_website_hosts`)
  - `autosdr/api/campaigns.py::_campaign_queued_priority_bulk`
    (OR clause + helper)
  - `autosdr/api/leads.py::_lead_to_out` (set
    `is_social_website`)
  - `autosdr/importer.py::preview_import_file` (count platform
    matches in the parsed rows)
  - `frontend/src/lib/types.ts`, `PriorityBadge.tsx`,
    `components/domain/SocialProfileTag.tsx` (new),
    `Leads.tsx`, `LeadDetail.tsx`, `LeadsImport.tsx`
- **Change class:** additive (no schema migration; predicate
  composes with 0013; opt-out via the existing toggle).
- **Risks:**
  - **False positives on path-only mentions.** A lead whose
    website is `https://acme.com/we-also-do-facebook-marketing`
    must NOT match. Mitigated by: hostname match only (not full
    URL substring).
  - **Bulk SQL prefix LIKE performance.** 7 hosts × 2 prefix
    variants = 14 LIKE clauses ORed in. SQLite + Postgres both
    seq-scan this on `Lead.website`; query is per-campaign and
    runs over queued rows only — small N. If we ever paginate the
    campaign list to thousands of campaigns the bulk count would
    need an index — accepted as a follow-up.
  - **Drift between the helper and `_SOCIAL_RE`.** Mitigated by
    extracting the platform list to a single
    `_SOCIAL_HOSTS: frozenset[str]` constant imported by both
    modules. Test pins the equivalence.
  - **Operator confusion: "the badge says priority but the scan
    is OK".** Mitigated by the tooltip text on the badge spelling
    out the reason ("Social profile in lieu of website").

## Open questions

1. **Does `is_social_website` override an `ok` enrichment status
   for priority?** Yes — they're independent signals on different
   surfaces. The predicate is OR; precedence is `not_found` >
   `social_profile_website` when both fire (single deterministic
   winner for the badge). No council needed — obvious once the
   predicate is two boolean clauses.
2. **Should we strip `LinkedIn company-page` URLs from priority
   while keeping `LinkedIn personal profile` URLs?** No. Both are
   "you don't have your own site" — same pitch surface. Keep the
   vocabulary in lockstep with `_SOCIAL_RE`.
3. **Per-platform UI toggles in Settings → Behaviour?** Defer.
   The plan called this out. One toggle for the whole signal
   class is enough until an operator asks for finer control.

## Resolved questions (2026-04-30)

### Resolved: signal-precedence

**Decision:** `not_found` outranks `social_profile_website` when both
fire (precedence in `priority_reason`). The predicate itself is OR,
so both leads land in the priority tier; the badge needs a single
winner and the more confident "their server returned 404" wins.
**Confidence:** high.
**Why this is acceptable:** The bulk count includes both; the badge
is just a label.

### Resolved: linkedin-company-vs-personal

**Decision:** Treat both as `linkedin`. No path-level distinction.
**Confidence:** high.
**Why this is acceptable:** Same underlying problem (no corporate
website); same pitch.

### Resolved: per-platform-toggles

User-preference call deferred. One toggle (`priority.enabled`) is
enough today.

## Principle check

- **Simplicity first:** ✓ — one new helper (~10 lines), one
  predicate widening (one more `or` clause), one OR in the bulk
  SQL. No schema change.
- **Quality over speed:** ✓ — leads with no real website get the
  pitch first.
- **Honest data contracts:** ✓ — `is_social_website` exposes the
  platform token; `priority_reason` distinguishes the two signals
  for the operator. `_SOCIAL_HOSTS` is a single closed vocabulary.
- **Extensible by design:** ✓ — adding an eighth platform is one
  line in `_SOCIAL_HOSTS`. Adding a third priority reason is one
  more `or` clause + one constant.
- **Human always wins:** ✓ — predicate is dynamic and read-only;
  operator can edit `Lead.website` and watch the flag flip.
  Killswitch / HITL untouched.
- **Owner stays in control:** ✓ — `priority.enabled` toggles the
  whole signal class; per-lead override available by editing
  `Lead.website`.

## Links

- Spec: `autosdr-doc1-product-overview.md § 5` — non-goal on AI
  lead scoring; this is deterministic again (hostname predicate).
- Code: [`autosdr/enrichment.py:133-154`](../../autosdr/enrichment.py)
  (`normalise_website_url` neighbour);
  [`autosdr/enrichment_extract.py:77-79`](../../autosdr/enrichment_extract.py)
  (vocab source);
  [`autosdr/pipeline/priority.py`](../../autosdr/pipeline/priority.py)
  (predicate);
  [`autosdr/api/campaigns.py::_campaign_queued_priority_bulk`](../../autosdr/api/campaigns.py)
  (bulk count).
- Plan: [`.cursor/plans/broken-website_lead_priority_27ffae7e.plan.md`](../../.cursor/plans/broken-website_lead_priority_27ffae7e.plan.md)

## Dependencies

- **Blocks:** none.
- **Blocked by:**
  - `0013-broken-website-priority.md` — provides the predicate +
    tier dimension this ticket extends.
- **Related:**
  - `0011-lead-enrichment.md` — produces the `enrichment_status`
    column the existing predicate reads.
  - `0004-import-field-mapping.md` — the import preview surface
    this ticket adds the social-host counts to.
  - `0015-scrape-confidence-promotion.md` (deferred) — re-opens
    `timeout` / `blocked` once a confidence score lands.

## Implementation log (2026-04-30)

**Status:** done

| # | Unit | Outcome | Evidence |
|---|------|---------|----------|
| 1 | Shared `SOCIAL_HOSTS` vocab + `is_social_website` helper + `_SOCIAL_RE` rewire | done | `tests/test_enrichment_social_website.py` (34 assertions); `autosdr/enrichment_vocab.py:21`, `autosdr/enrichment.py:159-218`, `autosdr/enrichment_extract.py:79-89` |
| 2 | Extend `priority_reason()` + `PRIORITY_REASON_SOCIAL_PROFILE_WEBSITE`; precedence `not_found` > `social` | done | `tests/test_priority_lead.py::test_priority_reason_social_website_branch`, `::test_priority_reason_precedence_not_found_outranks_social`; `autosdr/pipeline/priority.py:36-72` |
| 3 | `LeadOut.is_social_website` + `_lead_to_out` wiring | done | `tests/test_lead_priority_api.py::test_lead_out_marks_facebook_as_priority`, `::test_priority_reason_precedence_not_found_outranks_social`, `::test_is_social_website_is_none_for_real_corporate_website`; `autosdr/api/leads.py:53-79`; `autosdr/api/schemas.py:430-445` |
| 4 | Scheduler test extension (social lead joins priority tier) | done | `tests/test_scheduler_priority.py::test_social_website_lead_joins_priority_tier` |
| 5 | `_campaign_queued_priority_bulk` SQL OR for social-as-website | done | `tests/test_campaign_api.py::test_queued_priority_count_includes_social_websites`; `autosdr/api/campaigns.py:174-260` |
| 6 | Importer preview — count `social_website_hosts` + schema | done | `tests/test_leads_import_api.py::test_preview_counts_social_website_hosts`, `::test_preview_no_social_websites_returns_empty_dict`; `autosdr/importer.py:531-559`, `:802-867`; `autosdr/api/schemas.py:537-549` |
| 7 | Frontend — types, `PriorityBadge` social variant, `SocialProfileTag`, Leads/LeadDetail rendering, LeadsImport callout, CampaignDetail copy | done | `frontend/src/lib/types.ts:213-248`, `:867-880`; `frontend/src/components/domain/PriorityBadge.tsx`; `frontend/src/components/domain/SocialProfileTag.tsx` (new); `frontend/src/routes/Leads.tsx`, `LeadDetail.tsx`, `LeadsImport.tsx`, `CampaignDetail.tsx`; `tsc -b --noEmit` clean |

**Final state of success criteria:**

- SC1: ✓ — `is_social_website` truth table over the seven platforms with/without `www.` (`tests/test_enrichment_social_website.py::test_is_social_website_positives`).
- SC2: ✓ — vocab/regex equivalence pinned (`tests/test_enrichment_social_website.py::test_extract_regex_tracks_vocab`).
- SC3: ✓ — picker drains 404 + Facebook-as-website before normal P (`tests/test_scheduler_priority.py::test_social_website_lead_joins_priority_tier`).
- SC4: ✓ — `LeadOut.is_priority=true, priority_reason="social_profile_website", is_social_website="facebook"` (`tests/test_lead_priority_api.py::test_lead_out_marks_facebook_as_priority`).
- SC5: ✓ — `not_found` outranks `social_profile_website`; informational `is_social_website` stays set (`tests/test_lead_priority_api.py::test_priority_reason_precedence_not_found_outranks_social`).
- SC6: ✓ — `queued_priority_count` covers both signals; path-only mentions excluded (`tests/test_campaign_api.py::test_queued_priority_count_includes_social_websites`).
- SC7: ✓ — preview returns `{"facebook": 2, "instagram": 1, "linkedin": 1}` for the mixed sample; empty dict for clean uploads (`tests/test_leads_import_api.py::test_preview_counts_social_website_hosts`, `::test_preview_no_social_websites_returns_empty_dict`).
- SC8: ✓ — `tsc -b --noEmit` returns no errors.
- SC9: ⚠ — `PriorityBadge` social variant + `SocialProfileTag` ship code-complete and type-check; visual check on dev server still owed to operator (no Jest/RTL infra in `frontend/`, mirroring 0013's wrap-up).
- SC10: ⚠ — `LeadsImport` callout ships code-complete (`SocialWebsiteCallout` in `frontend/src/routes/LeadsImport.tsx`) and renders only when the upload has social URLs; visual check pending.

**Principle check after implementation:**

- Simplicity first: ✓ — one new shared module (`enrichment_vocab.py`, ~10 lines), one helper, one OR clause in SQL, one OR clause in Python predicate. Bulk SQL widened with prefix `LIKE`s (no schema change, no migration).
- Quality over speed: ✓ — leads with no real website land before normal-tier ones in the same picker pass that ship 404 leads first.
- Honest data contracts: ✓ — `is_social_website` is its own informational field (not folded into `priority_reason`), so a 404'd Facebook URL still tells the operator both signals fired.
- Extensible by design: ✓ — adding an eighth platform is one line in `SOCIAL_HOSTS`; the regex rebuilds from the constant on import.
- Human always wins: ✓ — predicate is dynamic; editing `Lead.website` flips the flag on the next read. Existing `priority.enabled` toggle covers the whole signal class.
- Owner stays in control: ✓ — workspace toggle untouched; bulk count helper is read-only.

**Follow-ups raised:**

- (none required to ship, but flagged for future) Bulk SQL `LIKE` clause is `O(2 prefixes × 7 platforms)` ORed against `Lead.website`. Acceptable today; if campaign list paginates to thousands of campaigns, denormalise `is_social_website` onto the `Lead` row + add an index. Same trade-off the ticket called out under **Out of scope**.

**Open questions still unresolved:** (none)

**Pattern-unifier diff scan (2026-04-30):** ✓ — no new drift introduced. Backend changes use SQLAlchemy 2.0 + Pydantic v2 (blessed); frontend changes use Tailwind tokens + lucide icons (blessed) and add no new dependencies.
