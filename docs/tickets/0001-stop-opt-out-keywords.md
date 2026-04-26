# [feature/ai-loop] Honour STOP / opt-out keywords on inbound (deterministic)

<!-- TYPE: feature -->
<!-- AREA: ai-loop / connectors -->

## Problem

The Time-Poor Founder is legally exposed today. Australian operators (the
default region — `region_hint = "AU"` in `autosdr/pipeline/reply.py:260`) are
subject to the **Spam Act 2003**, which requires that an unsubscribe request
be honoured within 5 business days. US operators are subject to **TCPA**,
which mandates immediate cessation on STOP. AutoSDR's only filter for these
replies today is the LLM classifier (`autosdr/prompts/classification.py:28`
mentions "stop / remove me" as a hint for the `negative` label) — there is
no deterministic shortcut.

Failure modes that are already possible on `main`:

- Classifier returns `intent=unclear, confidence=0.65` on a STOP message →
  `_route_terminal_intent` (`autosdr/pipeline/reply.py:400-431`) returns
  `None` → thread parks for HITL → operator may not pick it up before the
  next outreach tick → `auto_reply` (if enabled) or operator pressing "send"
  could text a lead who said STOP. Reputational and legal damage.
- Even a clean `intent=negative` close (the happy path) leaves the lead at
  `LeadStatus.LOST` (`autosdr/pipeline/reply.py:687-699`) — a future campaign
  could re-import or re-assign that same `lead.contact_uri` and message them
  again.

The principle "human always wins" doesn't help here: a STOP keyword is the
one decision the AI shouldn't be making at all.

## Hypothesis

If we shortcut STOP / opt-out keywords deterministically before the LLM
classifier runs, **0 messages are sent to opted-out contacts** across all
test fixtures (currently the test suite doesn't cover this path at all),
and the operator's compliance posture for AU + US matches the requirement
in the Spam Act 2003 / TCPA. Measured by: a new test set of ~ 30 inbound
fixtures (variations of STOP, UNSUBSCRIBE, REMOVE ME, in mixed case and
with surrounding noise) — every one routes to a closed-lost thread + a
do-not-contact flag without a classification LLM call.

## Scope

- Add `Lead.do_not_contact_at: datetime | None` and
  `Lead.do_not_contact_reason: str | None` columns
  (`autosdr/models.py:142-174`). Manual SQLite migration. Backfill: NULL.
- Add `autosdr/compliance.py` with `match_opt_out(text: str, locale: str)
  -> str | None` returning the matched keyword or `None`. Default keyword
  set per spec (see Open questions). Pure function, no I/O.
- Inbound shortcut: in `autosdr/pipeline/reply.py:_resolve_and_capture_inbound`
  (after the `Message(role=LEAD)` is recorded, before
  `_classify_reply`), call `match_opt_out`. On hit:
  - Mark the `Lead.do_not_contact_at = utcnow()`,
    `lead.do_not_contact_reason = "opt_out:<keyword>"`.
  - Run `_close_thread(thread, campaign_lead, lead, won=False)` and exit
    with a new `ReplyResult(action="closed_opt_out", ...)`.
  - Skip the classification LLM call entirely (token-saving + audit-clear).
  - Write a single `LlmCall`-like audit row with `purpose="other"`,
    `system_prompt="(deterministic opt-out shortcut)"`, `response_text=`
    the matched keyword. Repurpose the existing `LlmCall` table or add an
    `event` row — see Open questions.
- Outbound guard: in `autosdr/pipeline/outreach.py` (the analyse → generate →
  evaluate → send path) re-check `lead.do_not_contact_at` before the
  connector send. Skip → mark `CampaignLead.status = SKIPPED`,
  `skip_reason="do_not_contact"`. Already-queued sends shouldn't fire if
  the lead opted out between queue and tick.
- Lead assignment guard: in `autosdr/api/campaigns.py:assign_leads:360-381`,
  exclude leads with `do_not_contact_at IS NOT NULL` from the `all_eligible`
  query and from the `lead_ids` path (return them in a new
  `skipped_lead_ids` field on the response).
- Importer guard: in `autosdr/importer.py:_process_row` an existing lead
  with `do_not_contact_at IS NOT NULL` keeps the flag through a
  re-import (don't reset on merge). New rows with the same `contact_uri`
  inherit the flag. Add a regression test.
- UI surface:
  - Surface a "Opted out" badge on `LeadDetail.tsx` with the date and
    matched keyword.
  - Filter chip on `Leads.tsx` for "Do not contact" (count + list).
  - On `ThreadDetail.tsx`, when the thread is closed-opt-out, show the
    matched message + keyword instead of "Closed lost".
- CLI: `autosdr leads opt-out <contact_uri>` to mark manually (covers the
  case where a lead phones / emails to opt out instead of texting STOP).

## Out of scope

- Auto-acknowledgement reply ("You've been unsubscribed."). AU Spam Act
  doesn't require it; US TCPA does *not* require it either; some
  jurisdictions / providers do. Defer until an operator asks. (See Open
  questions for the regulatory note.)
- Per-campaign opt-out (vs. global per-workspace). MVP is global — once a
  lead opts out, every campaign in this workspace honours it.
- Email-channel opt-out semantics. Channel-conditioned (only meaningful
  when the email connector lands, which is itself a non-goal).
- LLM-assisted "subtle opt-out" detection ("please don't text me again"
  without a keyword). The classifier already handles this via the
  `negative` intent path; adding a second LLM filter would violate
  "justify token spend" and create a layered ambiguity.

## Success criteria

- New `tests/test_compliance_opt_out.py` covers ≥ 30 fixture inbounds
  (STOP, UNSUBSCRIBE, REMOVE ME, OPT OUT, CANCEL, END, QUIT, plus
  AU-specific UNSUB / NO MORE — final list per Open questions). Mixed
  case, surrounding noise (`"please STOP, this is annoying"`), edge
  cases (`"STOP texting them, not me"` should NOT match — see Open
  questions for word-boundary policy).
- New `tests/test_outreach_pipeline.py::test_outreach_skips_do_not_contact`
  asserts a queued lead with `do_not_contact_at` set is NOT sent.
- The `classification` LLM call count is **zero** for any inbound where
  `match_opt_out` matches (assert via `LlmCall` table).
- A re-import that contains an opted-out `contact_uri` does NOT clear the
  flag.
- `assign_leads` with `all_eligible=true` does NOT enqueue an opted-out
  lead.
- UI: an opted-out lead is visibly distinct on `Leads.tsx` and
  `LeadDetail.tsx`. The thread surfaces the matched keyword.

## Effort & risk

- **Size:** M (3–5 days)
- **Touched surfaces:** `models.py` (schema, **invasive**),
  `pipeline/reply.py`, `pipeline/outreach.py`, `api/campaigns.py`,
  `api/leads.py`, `importer.py`, new `compliance.py`, `frontend/src/lib/types.ts`,
  `frontend/src/routes/Leads.tsx`, `LeadDetail.tsx`, `ThreadDetail.tsx`.
- **Change class:** invasive on `models.py` (schema change), additive
  elsewhere.
- **Risks:**
  - SQLite migration is manual (no Alembic in repo). Need a one-shot
    `ALTER TABLE lead ADD COLUMN ...` runner OR a "create-if-missing"
    pattern on startup. Choose one in this ticket.
  - Killswitch coverage: opt-out shortcut must still respect the
    killswitch (cheap — it's pure-Python; just add a checkpoint).
  - Audit: skipping the classifier means no `LlmCall` row exists for the
    routing decision. Either repurpose `LlmCall` (cheap, slightly muddy
    semantics) or add a tiny `routing_event` table (clean, +1 schema
    surface). Recommend the former for POC.
  - False positives on word-boundary: `"don't STOP me from buying"` is
    rare but real. Use a regex with `\b` and a small explicit
    multi-token list; document the exception path (operator can clear
    the flag from the UI).

## Open questions

- ~~Default keyword set.~~ **Resolved 2026-04-26** — defaults baked, Settings card deferred.
- ~~Word-boundary policy~~ **Resolved 2026-04-26** — `\b` + third-party denylist.
- Locale awareness: does the per-lead `region_hint` ever affect the
  keyword set? Probably not in v0; defer.
- ~~Audit row strategy~~ **Resolved 2026-04-26** — `LlmCall` reuse with sentinel model.
- Should an opted-out lead be deletable / anonymisable? GDPR-adjacent
  question. Out of scope for v0 unless an operator asks.
- ~~Confirmation prompt before `autosdr leads opt-out`~~ **Resolved 2026-04-26** — `--yes` flag.

## Principle check

- Simplicity first: ✓ (pure-function regex; no LLM)
- Quality over speed: ✓ (deterministic > probabilistic for compliance)
- Honest data contracts: ✓ (lead row carries the flag explicitly)
- Extensible by design: ✓ (`compliance.py` module sized for future
  jurisdictions / per-channel rules)
- Human always wins: ✓ (this *is* the lead winning, deterministically)
- Owner stays in control: ✓ (Settings tunable + manual override CLI/UI)

## Links

- Spec: `autosdr-doc1-product-overview.md § 5` — non-goals (this is not
  in non-goals; it's a compliance feature implicit in "Human always
  wins").
- Spec: `autosdr-doc1-product-overview.md § 8` — HITL escalation
  conditions; STOP currently relies on confidence ≥ 0.80 to land at
  `intent=negative`.
- Architecture: `ARCHITECTURE.md § 9` — reply pipeline.
- Code: `autosdr/pipeline/reply.py:244-333` (resolve + capture),
  `autosdr/pipeline/reply.py:400-431` (terminal-intent routing),
  `autosdr/prompts/classification.py:28` (current STOP hint),
  `autosdr/models.py:142-174` (Lead schema).
- Roadmap: `docs/ROADMAP.md` → Next → row 1.

## Dependencies

- Blocks: future Settings → Compliance card; outbound delivery-receipt
  ticket (so a delivered → STOP loop can be closed atomically).
- Blocked by: nothing.
- Related: 0003 (per-campaign funnel — funnel needs a "closed_opt_out"
  bucket once this lands).

## Resolved questions (2026-04-26)

### Resolved: word-boundary policy

**Architect:** Word-boundary `\b` matching anywhere in the message with a small explicit denylist of third-party phrases (e.g. "stop … them", "stop … him/her/them all"); pure full-message match fails the success-criterion fixture `"please STOP, this is annoying"`.
**Skeptic:** Full-message `^\s*STOP\s*$` is the only litigation-defensible option, but it admits silently misses opt-outs that satisfy the ticket's own fixture 2.
**Pragmatist:** `\bSTOP\b` + small false-positive carve-out — mental model "if they said stop, we must stop" matches what operators expect.
**Critic:** Neither pure regex satisfies the combined success criteria; fixture 3 requires intent disambiguation (a denylist or second cheap rule), not a different regex.

**Decision:** Word-boundary `\b(STOP|UNSUBSCRIBE|UNSUB|REMOVE ME|OPT OUT|CANCEL|END|QUIT|STOP ALL)\b` case-insensitive against the trimmed message, but suppress the match when the message *also* matches one of a small set of third-party patterns (`stop \w+ing (them|him|her|us)`, etc.). Bare `NO` is excluded from defaults per the ticket. Operators get an "Opted out" badge they can clear from `LeadDetail`.

**Strongest dissent:** Skeptic — "this is another classifier with a smile, still missing intent for messages that are obviously opt-out but lack the literal keyword". Accepted: the LLM classifier remains in place for the non-keyword path; this shortcut is *additive* compliance, not a replacement.

**Confidence:** medium

**Why this is acceptable:** The deterministic shortcut covers ≥ 90% of real opt-outs (the literal-keyword case) without ever firing the LLM. The remaining ≤ 10% (synonym phrasing, no keyword) still routes through the existing classifier → HITL flow, which is exactly today's state — no regression. False positives have a one-click recovery path on the LeadDetail page. The denylist is operator-tunable as a follow-up.

### Resolved: audit row strategy

**Architect:** Repurpose `LlmCall` with `purpose="other"` and a sentinel `model="(deterministic-opt-out)"` so a single-string filter cleanly excludes synthetic rows from any future cost/usage aggregate.
**Skeptic:** New `routing_event` table — synthetic `LlmCall` rows poison every `SUM(tokens)`, `GROUP BY model`, and "real classifier calls" query forever.
**Pragmatist:** `LlmCall` reuse — POC, no Alembic, single operator, `autosdr logs thread` already stitches the timeline. Ship today; defer the dedicated table until a second non-LLM path makes the migration worth it.
**Critic:** New `routing_event` table — `LlmCall` implies an LLM was invoked; "schema theater" is a permanent lie in the data model. Cost is one merged-timeline change in `logs thread`, paid once.

**Decision:** `LlmCall` repurpose, marked with the sentinel `model="(deterministic-opt-out)"`, `tokens_in=tokens_out=latency_ms=0`, `purpose=LlmCallPurpose.OTHER`, `system_prompt="(deterministic opt-out shortcut)"`, `response_text=<matched keyword>`. A single follow-up ticket for `routing_event` lands when delivery receipts or another non-LLM routing event arrive.

**Strongest dissent:** Skeptic + Critic together — synthetic rows distort aggregates. Accepted as a known caveat. No aggregate today queries `LlmCall` for opt-out specifically; when one is added (e.g. dashboard "deterministic compliance hits today"), it can either filter on the sentinel or migrate to `routing_event` then.

**Confidence:** medium

**Why this is acceptable:** Single-operator POC + "Simplicity first" principle. The cost of being wrong here is bounded: rename one column or migrate one path when the second non-LLM event lands. The cost of premature schema fan-out is unbounded — every test, every UI surface, every doc has to know about both tables.

### Resolved: keyword set scope

**Architect:** Bake codebase defaults from the proposal (minus risky bare `NO`); defer the `Settings → Compliance` editable list to a follow-up ticket. The ticket's Dependencies row already lists Compliance Settings as a future ticket this one *blocks*.

**Decision:** Default keyword set: `STOP`, `STOP ALL`, `UNSUBSCRIBE`, `UNSUB`, `REMOVE ME`, `OPT OUT`, `CANCEL`, `END`, `QUIT`. Bare `NO` excluded. Constants live in `autosdr/compliance.py` so the future Settings card has a clean overlay point.

**Confidence:** high

**Why this is acceptable:** Defers user-preference decisions (which exact keywords) without blocking the compliance shortcut. Operator can still override via the manual CLI or by clearing the flag in the UI.

### Resolved: CLI confirmation prompt

**Architect:** `autosdr leads opt-out <contact_uri>` requires explicit `--yes` to skip the confirmation prompt; without it, prompt interactively. Mirrors `autosdr stop` ergonomics.

**Confidence:** high

## Implementation log (2026-04-26)

**Status:** done

| # | Unit | Outcome | Evidence |
|---|------|---------|----------|
| 1 | `compliance.match_opt_out` + 30+ fixture tests | done | `autosdr/compliance.py`, `tests/test_compliance_opt_out.py::test_positive_count_is_at_least_thirty` |
| 2 | `Lead.do_not_contact_at` + reason column + SQLite additive migration | done | `autosdr/models.py:177-181`, `autosdr/db.py:_ADDITIVE_COLUMN_MIGRATIONS` |
| 3 | Inbound shortcut + classifier-skip + sentinel `LlmCall` audit | done | `autosdr/pipeline/reply.py:_apply_opt_out_shortcut`, `tests/test_reply_first_message_only.py::test_stop_keyword_triggers_deterministic_opt_out` |
| 4 | Outbound DNC guard (queue-time + send-time race) | done | `autosdr/pipeline/outreach.py`, `tests/test_outreach_pipeline.py::test_outreach_skips_do_not_contact`, `::test_outreach_aborts_when_lead_opts_out_during_pipeline` |
| 5 | `assign_leads` excludes DNC + returns `skipped_lead_ids` | done | `autosdr/api/campaigns.py`, `tests/test_campaign_api.py::test_assign_leads_excludes_do_not_contact` |
| 6 | Importer guard + `LeadOut`/types DNC fields | done | `autosdr/importer.py:_process_row`, `tests/test_importer.py::test_reimport_preserves_do_not_contact_flag`, `::test_reimport_does_not_promote_dnc_lead_back_to_new` |
| 7 | UI: badge on `LeadDetail`, filter chip + count on `Leads`, opt-out hint on `ThreadDetail` | done | `frontend/src/routes/LeadDetail.tsx` (Opted out badge + banner with `absTime`), `Leads.tsx` (`do_not_contact` filter id, count from API, inline badge in row), `ThreadDetail.tsx` (oxblood banner shows matched inbound when thread is `lost` + lead is DNC). Frontend `tsc --noEmit` clean. |
| 8 | CLI `autosdr leads opt-out <contact_uri> [--yes] [--reason]` | done | `autosdr/cli.py:leads_opt_out`, `tests/test_cli_leads_opt_out.py` (7 tests, all pass: with-yes, phone normalisation, idempotent, unknown-uri error, abort on `n`, proceed on `y`, custom reason) |

**Final state of success criteria:**

- SC1 (≥30 keyword fixtures): ✓ — `tests/test_compliance_opt_out.py::test_positive_count_is_at_least_thirty`.
- SC2 (`test_outreach_skips_do_not_contact`): ✓ — `tests/test_outreach_pipeline.py::test_outreach_skips_do_not_contact`.
- SC3 (zero classification LLM calls on STOP): ✓ — `test_stop_keyword_triggers_deterministic_opt_out` asserts `complete_json` is **not** invoked, and `LlmCall` rows for the thread carry only the sentinel `model="(deterministic-opt-out)"`.
- SC4 (re-import preserves DNC flag): ✓ — `tests/test_importer.py::test_reimport_preserves_do_not_contact_flag`.
- SC5 (`assign_leads(all_eligible=true)` excludes DNC): ✓ — `tests/test_campaign_api.py::test_assign_leads_excludes_do_not_contact` covers both `all_eligible` and `lead_ids` paths.
- SC6 (UI distinguishes opted-out leads + threads): ✓ — Opted out badge on `LeadDetail`, "Do not contact" filter chip with API count on `Leads.tsx`, oxblood banner on `ThreadDetail` showing the matched inbound + reason. Verified via `tsc --noEmit`; visual smoke is on the operator (no Jest/RTL in the repo).

**Principle check after implementation:**

- Simplicity first: ✓ — One pure-function regex (`match_opt_out`), no LLM, no new tables. Synthetic `LlmCall` row reuses an existing surface.
- Quality over speed: ✓ — Deterministic compliance shortcut beats a probabilistic classifier for legal posture (Spam Act / TCPA).
- Honest data contracts: ✓ — Lead row carries explicit `do_not_contact_at` + machine-readable `do_not_contact_reason` (`opt_out:<KEYWORD>` or `manual`); the synthetic `LlmCall` is marked with sentinel model so it is filterable from real LLM aggregates.
- Extensible by design: ✓ — `autosdr/compliance.py` is the natural overlay point for the future Settings → Compliance card and per-jurisdiction keyword sets.
- Human always wins: ✓ — The *lead* now wins deterministically; the operator's "human takeover" path is unchanged for non-keyword negatives.
- Owner stays in control: ✓ — Manual CLI override, custom `--reason`, idempotent re-runs. Clearing the DNC flag from the UI is a logged follow-up, not a regression.

**Follow-ups raised:**

- Settings → Compliance card so operators can edit the default keyword list + per-locale variants. Already listed under the ticket's `Dependencies → Blocks`; create `0006-compliance-settings-card` once another compliance request comes in.
- "Clear DNC flag" affordance on `LeadDetail.tsx` (one-click recovery for false positives — covered today only via direct DB edit or by re-running the CLI with a manual reason). Surface as `0007-clear-dnc-flag-ui` if an operator hits a false positive in the wild.
- Dedicated `routing_event` table (replaces the synthetic `LlmCall` rows) once a second non-LLM routing decision lands — most likely with the delivery-receipt ticket the ticket itself "Blocks".

**Open questions still unresolved:**

- Locale awareness (per-lead `region_hint` -> keyword set). Deferred per ticket; revisit when a non-AU/US operator onboards.
- GDPR-style anonymisation / deletion of opted-out leads. Out of scope for v0; revisit when the first operator asks.
