# [feature/ai-loop] Tone register adapts to lead occupation/category

<!-- TYPE: feature -->
<!-- AREA: ai-loop -->

## Problem

The Time-Poor Founder is selling a single web-studio service to every kind of
Australian small business — tradies, hospitality, retail, salons, allied
health, legal, financial, education, aged care. Today's `generation` prompt
treats them as **two registers**, both glued into the same monolithic system
prompt:

- A **tradie register** — opener `"hey mate,"`, lowercase first word, dropped
  full stops, sentence fragments. The default for all leads.
- A **professional register** — same friendly opener style, but punctuation
  cleaner and proper nouns capitalised. Triggered by a hard-coded list inside
  the prompt itself
  ([`autosdr/prompts/generation.py:391-402`](../../autosdr/prompts/generation.py)):
  *"Healthcare, aged care, allied health, legal, financial, education → stay
  casual but keep punctuation clean and capitalise proper nouns."*

Two failure modes are already happening:

1. **The category is read by the LLM, not by code.** The `Lead.category` field
   ([`autosdr/models.py:165`](../../autosdr/models.py)) makes it into the user
   prompt as `"Category: nail salon"`
   ([`autosdr/prompts/generation.py:534`](../../autosdr/prompts/generation.py))
   and then the model has to (a) parse it, (b) recognise that it's allied
   health / personal services, (c) decide which calibration applies, (d)
   produce the right register. Three points of LLM judgement on a free-text
   string. The model's bias under thin signal is to default to the tradie
   register — the prompt itself says *"When the category is ambiguous, default
   to the tradie register."* That bias mis-fires on legitimately professional
   categories where Google Maps returns terse labels (`"Nail salon"`,
   `"Solicitor"`, `"Dental clinic"`). The operator's words: **"if its a
   tradie, 'hey mate...' would work well, but if we are sending to a nail
   salon, lawyer, or clinic, we might have a nicer introduction and tone in
   general."**

2. **The professional category list is opaque and ungrowable.** Adding
   "veterinary clinic" to the professional register today means editing the
   prompt, bumping `PROMPT_VERSION = "generation-v9"`, and breaking the
   byte-stable SHA test in
   [`tests/test_prompts.py`](../../tests/test_prompts.py). Six categories are
   pinned in prose; everything else falls through to the tradie default.

Plus a third failure mode the audit surface doesn't see today: **we have no
data on whether per-category register actually moves reply rate.** The
angle-funnel groups by `angle_type` (`stale_info`, `weak_presence`, …), not
category. We can't measure register-fit until categories are first-class.

The principle that bites here is **"Honest data contracts"** — `Lead.category`
is a structured field but we treat it as freeform LLM context. **"Quality over
speed"** also bites — Aussies smell salesy energy, and a `"hey mate,"` to a
solicitor reads as the wrong kind of energy entirely.

Evidence:

- Operator request, this session: *"if we are sending to a nail salon, lawyer,
  or clinic, we might have a nicer introduction and tone in general. Still
  shouldn't be salesy as we are selling to Aussies and they can smell it."*
- [`autosdr/prompts/generation.py:391-402`](../../autosdr/prompts/generation.py)
  — current `CATEGORY CALIBRATION` block lives in prompt prose.
- [`autosdr/prompts/generation.py:534`](../../autosdr/prompts/generation.py)
  — `Category: {lead_category or 'unknown'}` is the only category surface
  reaching the model.
- [`autosdr/prompts/_tone.py`](../../autosdr/prompts/_tone.py) — workspace
  `tone_snapshot` is global, applied to every lead identically.
- [`docs/prompt-audit-2026-05-02.md`](../prompt-audit-2026-05-02.md) Phase 3
  #8 — tone block already flagged as too long; per-category tone overlays
  must compose *under* the 1500-char cap, not on top of it.

## Hypothesis

If we map every `Lead.category` to one of a small set of named **registers**
deterministically in code, and inject the resolved register's voice rules into
the generation prompt as a structured `<register>` block (instead of relying
on the model to infer category from a freeform string), then:

- Every lead's draft uses the right register for that category, observable as
  a per-register opener-shape audit (`"hey mate,"` rate ≥ 90% for `tradie`
  register, ≤ 10% for `professional` register).
- Operators can grow the category → register map (Settings UI) without
  bumping `PROMPT_VERSION`.
- The angle-funnel gains a `register` dimension so we can measure reply rate
  per (angle, register) and answer "are we annoying salons by being too
  casual?" with data, not vibes.

Expected magnitude — register *mis-fit* is the most-hedged failure mode in
the prompt. Replacing prose-driven register selection with code-driven
selection is a textbook **Honest Data Contracts** win: the LLM stops doing
work it's bad at (mapping fuzzy category strings to a register) and the code
does work it's good at (table lookup with operator-tunable rules).

## Scope

### Backend — register vocabulary + mapper

- New module `autosdr/tone_register.py`. Pure-Python, dependency-free, no
  LLM:
  - `class ToneRegister: TRADIE | PROFESSIONAL | HOSPITALITY | RETAIL |
    PERSONAL_SERVICES | AGED_CARE | UNKNOWN` — string constants. The vocab
    is closed (Critic's "denormalisation drift" risk applies here too).
  - `register_for_category(category: str | None) -> str` — returns the
    register token. Uses a curated **prefix/keyword map** with longest-match
    semantics (e.g. `"nail salon"` → `personal_services`; `"family
    lawyer"` → `professional`; `"plumber"` → `tradie`; `"hairdresser"` →
    `personal_services`). Unknown categories → `UNKNOWN` (not `tradie`,
    so we don't paper over the gap).
  - `CATEGORY_REGISTER_MAP: dict[str, str]` — the seed table. Keys are
    lower-cased exact matches *and* keyword fragments (e.g.
    `"plumb"` → `tradie`). Order-stable so the `register_for_category`
    rule is deterministic for the operator.
  - `RegisterCalibration` dataclass per register with the four fields the
    prompt actually consumes:
    - `opener_examples: tuple[str, ...]` — preferred greetings
      (`("hey,", "hey mate,", "g'day,")` for tradie;
      `("hi there,", "hello,")` for professional, etc.)
    - `punctuation_directive: str` — single-line directive
      (e.g. `"Punctuation can be loose; lowercase openers are fine."`
      vs `"Punctuation must be clean. Capitalise proper nouns."`)
    - `formality_note: str` — one-line "sounds-like-a-mate-texting" vs
      "sounds-like-a-warm-but-considered-introduction" hint.
    - `register_label: str` — operator-facing label (`"Tradie"`,
      `"Professional"`, …).
  - The seed map covers the categories actually present in the QLD Apify
    fixture (`tests/fixtures/apify_qld_excerpt.ndjson`) plus the
    nail-salon / lawyer / clinic shapes the operator named. Aim for
    ~40-60 keywords across 6 registers — enough to cover the long tail
    of the operator's QLD dump without trying to be a global taxonomy.

### Backend — wire into generation

- `autosdr/prompts/generation.py`:
  - New `_REGISTER_INSTRUCTIONS: dict[str, str]` keyed on register token.
    Each value is a ≤ 600-char block: opener guidance + punctuation
    directive + 1-2 worked openers in that register. Compact enough to
    fit under the 1500-char tone-cap budget without amputation.
  - `build_system_prompt(...)` gains a new `register: str | None`
    keyword argument. When provided, splices the matching
    `_REGISTER_INSTRUCTIONS[register]` block in **after** the workspace
    tone block and **before** `_RULES`. Position is deliberate: workspace
    tone wins on global voice, register layers in category fit, then the
    rules constrain both.
  - Bump `PROMPT_VERSION = "generation-v9"`. Update the byte-stable SHA
    test in `tests/test_prompts.py` deliberately (one new SHA per
    register + one for the no-register case).
  - **Remove** the `CATEGORY CALIBRATION` paragraph
    (`autosdr/prompts/generation.py:391-402`) — it's superseded by the
    register block, and leaving it duplicates rules.

- Caller change in
  [`autosdr/pipeline/_shared.py::generate_and_evaluate`](../../autosdr/pipeline/_shared.py):
  - Resolve `register = register_for_category(lead.category)` once per
    invocation.
  - Pass it to `generation.build_system_prompt(register=register, ...)`.
  - Persist it on `Thread.tone_register` (new column, see Schema).
  - Stash it in the LLM call audit row's `metadata` for slicing.

- The `evaluation` prompt does **not** receive the register block — the
  evaluator scores against the rules, not the register. (Open Question 2
  challenges this; council the call.)

### Backend — schema + serialisation

- `Thread.tone_register: str | None` column (additive, nullable). Set on
  first generate-and-evaluate, never edited after. Pre-existing rows stay
  NULL → reported as `unknown` everywhere.
- `ThreadOut.tone_register` mirrors the column.
- Manual SQLite migration via the existing `_ADDITIVE_COLUMN_MIGRATIONS`
  list in `autosdr/models.py` (the same path 0002 used for
  `Thread.angle_type`).

### Backend — settings + override

- New `tone_registers` block on `workspace.settings`:
  ```json
  {
    "tone_registers": {
      "category_overrides": { "Yoga studio": "personal_services", "Vet": "professional" },
      "disabled": false
    }
  }
  ```
  - `category_overrides` — operator-tunable. Looked up before
    `register_for_category` so the operator can correct a mis-mapping
    without a code change.
  - `disabled: true` — kill switch back to the pre-0017 behaviour
    (no register block in the prompt). For regression triage if a
    register cohort under-performs.

### Backend — angle-funnel breakdown

- Extend
  [`autosdr/api/stats.py::angle_funnel`](../../autosdr/api/stats.py)
  to optionally group by `register` as a second dimension:
  - New query param `dimension=register` returns
    `{angle, register, threads, replied}` rows.
  - Existing single-dim shape preserved (default `dimension=angle`).
- Wire into the existing `/Logs` "By angle" panel as a tab toggle:
  `Angle | Register | Angle × Register`. The third tab is a small heatmap
  using the same CSS-bar primitive (no chart-lib pull-in).

### Frontend

- `frontend/src/lib/types.ts` — `tone_register?: string | null` on
  `Thread`; `tone_registers` block on `WorkspaceSettings`.
- New `RegisterChip` primitive in `frontend/src/components/domain/`:
  small mustard-soft chip with the `register_label` (`"Tradie"`,
  `"Professional"`, …). Rendered next to the angle on
  `ThreadDetail`'s right rail and on `LeadDetail`'s thread list.
- Settings → Behaviour: new "Tone register" card. Lists the resolved
  register for each unique `Lead.category` currently in the workspace,
  with an inline override (dropdown of the six registers). Saves to
  `workspace.settings.tone_registers.category_overrides`. Bulk action:
  "Reset to defaults". Kill switch: a single toggle for `disabled`.
- `LeadsImport.tsx` preview: when the parsed categories resolve to the
  six registers, show a small per-register count
  (`23 tradie / 4 professional / 7 personal services / 3 unknown`) so
  the operator can sanity-check before commit.
- `Logs.tsx` "By angle" panel: new `Register` and `Angle × Register`
  view modes (URL-param-driven so the tab survives page reloads, mirrors
  ticket 0014's pattern).

### CLI

- `autosdr leads list` (already exists from ticket 0011) gains an
  optional `--register <token>` filter. Useful for "show me the
  professional-register leads I've contacted this week" when triaging
  reply patterns.
- `autosdr logs angles --by register` mirrors the new dimension on the
  existing CLI surface.

### Tests

- `tests/test_tone_register.py` — pure unit suite on
  `register_for_category`. ~ 30 cases spanning the QLD fixture
  categories + lawyer/nail-salon/clinic + edge cases (empty, None,
  "Plumber", "Family Lawyer", "Vegan Restaurant").
- `tests/test_prompts.py` — register block makes it into
  `build_system_prompt(register=...)`; byte-stable SHA per register;
  `register=None` gracefully omits the block.
- `tests/test_outreach_pipeline.py` — `Thread.tone_register` is set on
  first generation and persisted across the session boundary; legacy
  threads remain NULL after a re-analyse.
- `tests/test_stats_angle_funnel.py` — `dimension=register` and
  `dimension=angle_register` return the expected aggregates against the
  in-memory factory.
- `tests/test_workspace_settings.py` — `category_overrides` round-trips
  through the JSON column; `disabled=true` short-circuits register
  resolution.
- One golden-replay smoke against
  `scripts/replay_outreach_loop.py` on a 10-lead mix (tradie + salon +
  legal + clinic + cafe) confirming the right register block landed in
  each system prompt.

## Out of scope

- **Per-register tone snapshots** — workspace `tone_prompt` stays global.
  An operator who wants a totally different voice per register is
  better served by having two campaigns, one for each persona slice.
- **LLM-assisted register suggestion** — the seed map is curated, not
  inferred. We're explicitly closing the loop where the LLM has to
  guess; reopening it via "let an LLM categorise" defeats the point.
- **Auto-A/B between registers** — a register is a deterministic call
  per lead, not a variant. The angle A/B work tracked in *Later* is the
  right surface for that.
- **Backfill of `Thread.tone_register` for legacy threads.** They stay
  NULL; the funnel buckets NULL as `unknown` (mirrors 0002's handling
  of legacy `angle_type`). Re-running analysis on a legacy thread will
  populate it on the next generate.
- **Removing the existing tradie default for unknown categories.** A
  lead with `category=NULL` keeps reading as `unknown` register; the
  prompt for `unknown` is a soft-tradie variant so we don't regress the
  base case.
- **Multi-language register support.** AU-English only.

## Success criteria

- `register_for_category("Plumber")` → `"tradie"`,
  `register_for_category("Family Lawyer")` → `"professional"`,
  `register_for_category("Nail Salon")` → `"personal_services"`,
  `register_for_category(None)` → `"unknown"`.
- The system prompt for a "Family Lawyer" lead contains the
  `_REGISTER_INSTRUCTIONS["professional"]` block; the system prompt for a
  "Plumber" lead does not. Pinned by a snapshot test.
- A 30-lead golden-replay smoke shows opener-shape audit:
  - `register=tradie` cohort: ≥ 90% drafts open with one of
    `("hey,", "hey mate,", "g'day,")` (case-insensitive, leading
    whitespace-stripped).
  - `register=professional` cohort: ≥ 90% drafts open with one of
    `("hi there,", "hello,", "hi,")` and **no** `"hey mate,"`.
  - `register=personal_services` cohort: openers either tradie-style
    `"hey,"` or professional-style `"hi there,"` — never `"hey mate,"`.
- `Thread.tone_register` is populated on every new outreach send;
  `ThreadOut.tone_register` round-trips. New tests pass.
- Settings → Behaviour shows the resolved register for each unique
  `Lead.category` in the workspace; an override saved there is honoured
  on the next generation (no restart, hot-reload via the existing
  `workspace.settings` reader).
- `/api/stats/angle-funnel?dimension=register` returns the new shape;
  `Logs` page renders the new tab.
- `disabled=true` on the kill switch reverts to legacy behaviour
  byte-for-byte (the prompt does not contain a `<register>` block, the
  byte-stable SHA matches `generation-v8`).
- 661+ backend tests pass; `tsc -b --noEmit` clean.

## Effort & risk

- **Size:** M (~ 1 person-week).
- **Touched surfaces:**
  - `autosdr/tone_register.py` (new module)
  - `autosdr/prompts/generation.py` (new register block + signature change)
  - `autosdr/pipeline/_shared.py` (caller passes register)
  - `autosdr/models.py` (additive column, additive migration list)
  - `autosdr/api/{schemas,stats,workspace}.py`
  - `frontend/src/lib/types.ts`
  - `frontend/src/routes/{Settings,LeadsImport,Logs,LeadDetail,ThreadDetail}.tsx`
  - `frontend/src/components/domain/RegisterChip.tsx` (new)
  - `tests/test_tone_register.py` (new), plus extensions to existing
    test files.
- **Change class:** additive end-to-end (column nullable, prompt block
  optional via the kill switch, all backend calls keyword-only).
- **Risks:**
  - **Prompt-version bump (`generation-v8` → `generation-v9`).** Audit
    log compatibility is fine — the column is freeform, but every
    deploy-watch dashboard slice needs to register the new version.
    Ties into ticket 0016's deploy-watch surface.
  - **Register mis-classification.** A lead categorised "Wellness
    Studio" landing in `personal_services` when the operator meant
    `professional` is a soft failure (still readable) but a real
    register mis-fit. Mitigated by `category_overrides` and surfaced in
    Settings.
  - **Tone budget cap.** `_REGISTER_INSTRUCTIONS` blocks add ~ 600 chars
    on top of the workspace tone. Cap test in `_tone.py` already
    enforces budgets; verify the register block stays under cap when
    composed with a worst-case workspace tone.
  - **Evaluator drift.** Eval prompt does NOT see the register block
    (decision per Open Question 2). Risk: evaluator marks a clean
    professional-register draft down for being "too formal". Smoke
    against `scripts/replay_evaluator.py` on the existing 8-thread
    golden set; if pass-flips > 2/8, escalate to passing register into
    eval.
  - **Backward-compat on the analysis prompt.** This ticket touches
    generation only; analysis still picks the angle from category +
    enrichment. No change. Confirm via the existing test in
    `tests/test_outreach_pipeline.py`.

## Open questions

1. ~~**Where does the seed `CATEGORY_REGISTER_MAP` live?**~~ — resolved
   2026-05-10 → seed map in `autosdr/tone_register.py`; per-workspace
   overrides only via `workspace.settings.tone_registers.category_overrides`.
   ~~**Superseded 2026-05-10 (re-council)** → no seed map. The analysis
   LLM picks `tone_register` as a structured enum field on its JSON
   output (same shape as `angle_type`); persistence guard at
   `outreach.py` enforces the closed vocab. See "Re-council
   (2026-05-10)" below for the full rationale.~~
2. ~~**Does the evaluator see the register?**~~ — resolved 2026-05-10 →
   no. Eval stays on `evaluation-v4.7`; register-stratified
   pass-rate is the regression bar.
3. ~~**What's the "unknown" register prompt?**~~ — resolved 2026-05-10 →
   skip-the-block when register is unknown. Match the kill-switch
   shape per-lead.
4. **Should the register block be injected after `cap_tone_snapshot`?**
   Today's audit-pinned tone budget is 1500 chars + the rules + the
   examples. Adding ~ 600 register chars pushes the gen prompt up by
   ~ 5%. Cheap, but verify against the 0016 deploy-watch token-in
   panel post-ship. **(Verified at compose-time: `tests/test_tone_register.py::test_register_block_fits_under_compose_budget`.)**
5. **Per-register example openers in `_REFERENCE_EXAMPLES`?** Currently
   six worked examples are tradie-leaning. Should we add two
   professional-register examples? Risk: the model picks the closest
   example regardless of register, drifting professional drafts toward
   the tradie example anyway. Sub-ticket if the cohort smoke shows
   register-mis-fit > 10%. **(Deferred — measure after 1-2 weeks of
   stratified angle-funnel data.)**

## Resolved questions (2026-05-10)

### Resolved: seed-map-location

**Architect:** Seed map in `autosdr/tone_register.py`; only per-workspace overrides land in `workspace.settings.tone_registers.category_overrides`. Same pattern as `SOCIAL_HOSTS` + `_OWNERSHIP_KEYWORDS` — closed-vocab routing policy lives in code, with one narrow operator escape hatch.
**Skeptic:** Code-only seed; overrides for fixes only. Boot-seeding into JSON invents two truths and tanks reproducibility ("which workspace JSON is canonical?").
**Pragmatist:** Code seed + override field. Closed-vocab keyword lists already live in code (precedent: `SOCIAL_HOSTS`, `_OWNERSHIP_KEYWORDS`); operators only need an exception path, not the whole map.
**Critic:** Code seed. Routing rules + register prose share the same versioning story; two sources of truth (prose in repo, routing in DB) drift across upgrades.

**Decision:** Hardcoded `CATEGORY_REGISTER_MAP` in `autosdr/tone_register.py`. Operator-tunable `workspace.settings.tone_registers.category_overrides` is the only DB-side knob. No first-boot seeding into settings.
**Strongest dissent:** Operators may grow `category_overrides` into a shadow full map (no code review, per-workspace drift). Mitigation: surface the override count + cap the field at 50 entries in the Settings UI; if it ever fills, file a ticket to merge keys back into the code seed.
**Confidence:** high
**Why this is acceptable:** The override field is intentionally narrow; the Settings UI shows the resolved register per category alongside the override, which makes drift visible.

### Resolved: evaluator-sees-register

**Architect:** No — eval stays on `evaluation-v4.7` and `_RULES`. Register is a generation constraint, not an eval rubric. The 8-thread golden replay is the existing regression bar; angle-funnel grouped by register is the empirical layer.
**Skeptic:** No. Coupling eval to register doubles the regression surface and redefines "naturalness" per register, which makes scores incomparable across history.
**Pragmatist:** No. Cost is certain (replay + bump + parallel prose), benefit uncertain. Stratified funnel is enough to detect "did we hurt legal?" without entangling prompts.
**Critic:** No. Register-aware eval risks splitting global invariants (safety, disclosure, anti-spam) and laundering resolver bugs into "graded-as-correct".

**Decision:** Generation prompt gets the `<register>` block; evaluator stays register-blind on `evaluation-v4.7`.
**Strongest dissent:** Professional-register drafts may chronically under-score on `naturalness` because the worked examples are tradie-leaning. Mitigation: stratified angle-funnel by register surfaces per-register pass-rate; if reject-rate diverges by > 15% across registers, escalate to a follow-up that either (i) adds professional-register worked examples to gen, or (ii) adjusts the eval threshold per register.
**Confidence:** high
**Why this is acceptable:** The dissent is observable post-ship via the funnel, not silent. The kill switch reverts byte-for-byte if the regression bar trips.

### Resolved: unknown-register-prompt

**Architect:** Skip the block when `register == "unknown"`. The model sees `_RULES` + workspace tone + worked examples — exactly the kill-switch shape, applied per-lead. Match epistemic state: "we don't know" → "no register assertion".
**Skeptic:** Skip. Re-injecting a soft-tradie default just re-centralises the removed CATEGORY CALIBRATION sentence in 600 chars; fights the compose-under-cap goal.
**Pragmatist:** Skip. Honesty matters: `unknown` means "no reliable category", not "probably tradie". Saving 600 chars × ~5% NULL rows compounds.
**Critic:** Skip. Per-lead kill-switch shape mirrors the global kill switch and keeps tests + mental models aligned ("block iff classified register").

**Decision:** When `register_for_category()` returns `"unknown"`, the system prompt does **not** contain a `<register>` block. Caller passes `register=None` to `build_system_prompt`, which is the same code path the global `tone_registers.disabled = true` kill switch uses.
**Strongest dissent:** Voice variance on NULL-category leads may skew slightly more "professional/cooler" than the explicitly-tagged tradie cohort. Mitigation: angle-funnel stratification surfaces this; if NULL-category reply-rate diverges by > 20%, file a follow-up to either fall back to soft-tradie or run a category-classifier pass first.
**Confidence:** high
**Why this is acceptable:** The risk is observable, bounded (~5% of leads), and the kill-switch + override surfaces give the operator a recovery path without a redeploy.

## Mini plan (2026-05-10)

| # | Unit | Files | Change class | Tests | Depends on | Risk |
|---|------|-------|--------------|-------|------------|------|
| 1 | New `tone_register.py` module: `ToneRegisterT` literal, `_REGISTER_INSTRUCTIONS`, `CATEGORY_REGISTER_MAP`, `register_for_category()`, `RegisterCalibration` dataclass | `autosdr/tone_register.py` (new) | additive | `tests/test_tone_register.py` (new, ≥ 6 assertions) | — | high (vocab + map shape locks the contract) |
| 2 | Wire `register` kwarg into `generation.build_system_prompt`, drop CATEGORY CALIBRATION sub-block, bump `PROMPT_VERSION` to `generation-v9`, refresh SHA snapshots, add compose-cap test | `autosdr/prompts/generation.py`, `tests/test_prompts.py` | invasive | `test_rendered_prompts_are_byte_stable` updated + new `test_register_block_fits_under_compose_budget` + `test_register_block_omitted_when_none` | unit 1 | high (prompt-version bump invalidates byte-stable SHA harness) |
| 3 | Persist `Thread.tone_register` column (additive migration in `_ADDITIVE_COLUMN_MIGRATIONS`) | `autosdr/models.py`, `autosdr/db.py` | additive | new `test_thread_tone_register_column_migrates` smoke | — | low (additive nullable column, established pattern) |
| 4 | Workspace settings: `tone_registers` block (`category_overrides: dict`, `disabled: bool`); load via existing `workspace_settings` helpers | `autosdr/workspace_settings.py` (read-only — settings consumed in `_shared.py`) | additive | covered by units 5/6 integration tests | — | low |
| 5 | Resolve register in `pipeline/_shared.py::generate_and_evaluate`, persist `thread.tone_register`, pass register to `build_system_prompt` | `autosdr/pipeline/_shared.py` | invasive | `tests/test_outreach_pipeline.py::test_outreach_persists_tone_register` (new) + existing pipeline tests | units 1, 2, 3, 4 | med (caller change, killswitch path) |
| 6 | Expose `ThreadOut.tone_register` round-trip on `GET /api/threads`, `GET /api/threads/{id}` | `autosdr/api/schemas.py`, `autosdr/api/threads.py` | additive | `tests/test_api_threads.py::test_thread_out_includes_tone_register` (new) | unit 3 | low |
| 7 | Extend `/api/stats/angle-funnel` with `dimension=register` query param | `autosdr/api/stats.py`, `autosdr/api/schemas.py` | additive | `tests/test_api_stats.py::test_angle_funnel_by_register` (new) | unit 3 | low |
| 8 | Frontend types: `Thread.tone_register`, `WorkspaceSettings.tone_registers`, `AngleFunnelRow.register?`, `AngleFunnel.dimension?` | `frontend/src/lib/types.ts` | additive | `tsc -b --noEmit` clean | units 6, 7 | low |
| 9 | Frontend: `RegisterChip` component + `LeadDetail` / `ThreadDetail` chip render; HITL row chip in Inbox | `frontend/src/components/domain/RegisterChip.tsx` (new), `frontend/src/routes/{LeadDetail,ThreadDetail,Inbox}.tsx` | additive | visual; logic unit-tested in chip component | unit 8 | low |
| 10 | Frontend: Settings → Behaviour gains "Tone register overrides" card with `disabled` kill switch + override editor | `frontend/src/routes/settings/BehaviourCard.tsx` | additive | covered by `tsc` + manual smoke | unit 8 | low |
| 11 | Frontend: AngleFunnelPanel adds dimension toggle (`by angle` vs `by register`) | `frontend/src/components/domain/AngleFunnelPanel.tsx` | additive | visual | unit 8 | low |

**Sequencing rationale:** unit 1 (tone-register vocab + map) is the single biggest contract decision — if any keyword shape is wrong everything downstream rebuilds. Unit 2 is the next-riskiest because the byte-stable SHA harness will scream the moment the prompt drifts. Schema (unit 3) lands before any consumer reads/writes the column.

**Map back to Scope:**
- New `autosdr/tone_register.py` → unit 1
- `generation.py` rewire + version bump → unit 2
- `Thread.tone_register` column + migration → unit 3
- `workspace.settings.tone_registers` block → unit 4
- Pipeline persistence → unit 5
- API round-trip (`ThreadOut.tone_register`) → unit 6
- `/api/stats/angle-funnel?dimension=register` → unit 7
- Frontend types → unit 8
- `RegisterChip` + Lead/Thread detail render → unit 9
- Settings card → unit 10
- AngleFunnelPanel dimension toggle → unit 11
- ~~CLI~~ → out (PATTERNS.md: Typer CLI removed 2026-04-28)

**Map back to Success criteria:**
- `register_for_category("Plumber")=="tradie"` → unit 1, observable via `tests/test_tone_register.py`
- Family-Lawyer prompt has the professional block, Plumber doesn't → unit 2, observable via `test_register_block_present_for_known_register` + `test_register_block_omitted_when_none`
- Cohort opener-shape audit (≥ 90% per register) → defer to deploy-watch when 0016 lands; this ticket lands the structural plumbing + a smoke that demonstrates per-register prompts wire correctly
- `Thread.tone_register` populated on every new outreach send → unit 5, observable via `test_outreach_persists_tone_register`
- Settings shows resolved register per `Lead.category` + override honoured on next gen → units 4 + 5 + 10
- `/api/stats/angle-funnel?dimension=register` returns the new shape → unit 7
- `disabled=true` reverts byte-for-byte to `generation-v8` SHA → unit 2, observable via `test_register_block_omitted_when_none` (kill-switch path uses `register=None`)
- 661+ backend tests pass + `tsc -b --noEmit` clean → final verification

**Blessed-pattern check:**
- Unit 1: Python 3.11+ stdlib-only module, `Literal` + `dict[str, …]` (PATTERNS.md typing/closed-vocab pattern). Same shape as `enrichment_vocab.py::SOCIAL_HOSTS`.
- Unit 2: Existing `autosdr.prompts` byte-stable SHA harness (PATTERNS.md prompts row). PROMPT_VERSION bumped per audit doc convention.
- Unit 3: Existing additive-column migration helper (PATTERNS.md ORM/migrations row).
- Unit 4: Existing `workspace.settings` JSON blob + `load_workspace_settings_or_empty` helpers (PATTERNS.md workspace-settings row).
- Unit 5: Existing `generate_and_evaluate` flow; no new concurrency primitives.
- Units 6/7: FastAPI routers + Pydantic v2 (PATTERNS.md HTTP row).
- Units 8-11: TanStack Query + Tailwind + lucide-react (PATTERNS.md frontend rows).

## Principle check

- **Simplicity first:** ⚠ — adds a new module + column + Settings card.
  Justified because the alternative (more prompt prose) keeps growing
  unboundedly and the LLM is bad at the lookup.
- **Quality over speed:** ✓ — addresses the "can't sound like 'hey mate,'
  to a solicitor" problem head-on.
- **Honest data contracts:** ✓ — promotes `Lead.category` from "freeform
  prose for the LLM" to "structured input that drives a register".
- **Extensible by design:** ✓ — `CATEGORY_REGISTER_MAP` and the six
  registers form a closed vocab the operator can grow.
- **Human always wins:** ✓ — the kill switch (`disabled: true`) reverts
  to legacy behaviour without a redeploy.
- **Owner stays in control:** ✓ — Settings UI exposes the mapping;
  per-category override is the operator's lever.

## Links

- Spec: `autosdr-doc1-product-overview.md § 3 (Principles)` —
  AU-Aussie tone is part of "story-branded, not salesy".
- Architecture: `ARCHITECTURE.md § 3 (Components)` — generation +
  evaluation prompts.
- Code:
  - `autosdr/prompts/generation.py:391-402` (current calibration paragraph)
  - `autosdr/prompts/generation.py:534` (where category reaches the model)
  - `autosdr/prompts/_tone.py` (cap budget the register block has to live under)
  - `autosdr/pipeline/_shared.py:244-280` (generate-and-evaluate caller)
  - `autosdr/models.py:295-336` (Thread schema for the new column)
  - `tests/test_prompts.py` (byte-stable SHA harness)
- Audit: [`docs/prompt-audit-2026-05-02.md`](../prompt-audit-2026-05-02.md)
  Phase 3 #8 (tone block budget — relevant for cap composition).

## Dependencies

- **Blocks:** none (additive).
- **Blocked by:** ticket 0016 (deploy-watch dashboard) — the
  register-stratified pass-rate panel needs the surface ticket 0016
  delivers. Can ship this ticket without 0016, but the regression bar
  for Open Question 2 is weaker.
- **Related:** ticket 0002 (`Thread.angle_type`) — same additive-column
  pattern; ticket 0014 (`SOCIAL_HOSTS` vocabulary) — same closed-vocab
  pattern; *Later* item "A/B compare two personalisation angles per
  lead" — composes once both registers and angles are first-class.

## Re-council (2026-05-10)

Mid-implementation pushback from the operator: *"the tone registry is
over the top. We have an angle LLM right? We can in that agent choose a
tone right?"* The original code-driven seed-map design treated this as
"the LLM is bad at picking register from prose," which was the right
call **for the generation model picking implicitly while writing**.
But the analysis model already does enum-typed JSON output (it picks
`angle_type` from a closed 7-token vocabulary every call). Adding
`tone_register` as an 8th enum field is qualitatively the same problem
the analysis model already solves well, with the bonus that it sees
website + reviews + signals — not just the freeform `Lead.category`
string the substring map would have been keyed on.

### Resolved (revised): seed-map-location

**Architect:** Drop the seed map. Analysis LLM picks `tone_register` as
a structured enum field on its JSON output (same shape as `angle_type`
today). Closed-vocab guard at the persistence boundary (`outreach.py`)
collapses anything outside the seven tokens to NULL. Generation prompt
reads register off `Thread.tone_register` and injects the matching
prose block. The seed map, the workspace overrides, the Settings UI
card, the kill switch, and `autosdr/tone_register.py` itself all go.

**Decision:** LLM-picks-register. ~700 LOC deleted; ~150 LOC added
across the analysis prompt + persistence guard + frontend chip.
**Strongest dissent:** Lose deterministic "Plumber → tradie" audit
trail; same lead can flip register across re-analyse runs. Mitigation:
coarse 7-value enum keeps flip-rate low, register stratification on
`/api/stats/angle-funnel?dimension=register` makes any drift visible,
operator HITL takes over if a draft mis-fits.
**Confidence:** high.
**Why this is acceptable:** revert path is bumping `analysis-v3.7` →
`v3.6` (drop the new schema field), which is identical in cost to any
prompt regression revert. The original kill-switch was solving a
problem one layer too high.

## Mini plan (revised 2026-05-10)

| # | Unit | Files | Change class | Tests | Risk |
|---|------|-------|--------------|-------|------|
| R1 | Delete `autosdr/tone_register.py` (504 LOC), `tests/test_tone_register.py` (250 LOC), `tone_registers` block in `autosdr/config.py`, `resolve_register` plumbing in `_shared.py` | (deletes) | deletion | n/a | low |
| S1 | Inline `_REGISTER_INSTRUCTIONS` + `ToneRegisterT` literal into `autosdr/prompts/generation.py`; fix three "DO use" → "Do NOT use" prose typos in `professional`, `personal_services`, `aged_care` blocks | `autosdr/prompts/generation.py` | refactor | byte-stable SHA pin updated | low |
| S2 | Add `_RULES_TONE_REGISTER` block + `tone_register` enum field to `_OUTPUT_SCHEMA` in `autosdr/prompts/analysis.py`; bump `analysis-v3.6` → `analysis-v3.7` | `autosdr/prompts/analysis.py` | invasive | `test_rendered_prompts_are_byte_stable` SHA bump + `test_analysis_prompt_advertises_tone_register_field` (new) | high (prompt-version bump) |
| S3 | `outreach.py` persists `analysis_result["tone_register"]` onto `thread.tone_register` with closed-vocab guard; `_shared.py::generate_and_evaluate` reads off the column and passes to `build_system_prompt(register=…)` | `autosdr/pipeline/outreach.py`, `autosdr/pipeline/_shared.py` | invasive | `test_outreach_persists_tone_register_from_analysis_output` + `test_outreach_collapses_unknown_register_to_null` + `test_outreach_collapses_invalid_register_token_to_null` (new) | med |
| S4 | Bump `analysis-v3.6` → `analysis-v3.7` mock keys across `tests/test_outreach_pipeline.py` + `tests/test_llm_call_log.py`; refresh SHAs; replace deleted kill-switch test with focused persistence tests | (test files) | additive | full backend suite green | med |
| S5 | Frontend `ToneRegister` type + `Thread.tone_register` field + `RegisterChip` component + chip render on `ThreadDetail` (right rail) and `LeadDetail` (per-thread row) | `frontend/src/lib/types.ts`, `frontend/src/components/domain/RegisterChip.tsx` (new), `frontend/src/routes/{ThreadDetail,LeadDetail}.tsx` | additive | `tsc -b --noEmit` clean | low |

**Sequencing rationale:** R1 (clean slate) before any code that
references the dropped module. S1 (locality move) before S2 (prompt
bump) so the new prose ships in one prompt-version change instead of
two. S3 only after S1 + S2 are green so the column write has a real
register to persist.

**Map back to (original) Scope:**
- New `autosdr/tone_register.py` → **dropped** (LLM-picks); functionality consolidated into `analysis.py` + `generation.py`
- `generation.py` rewire + version bump → S1 + S2
- `Thread.tone_register` column + migration → already shipped pre-revision (kept)
- `workspace.settings.tone_registers` block → **dropped** (no overrides, no kill switch)
- Pipeline persistence → S3
- API round-trip (`ThreadOut.tone_register`) → already shipped pre-revision (kept)
- `/api/stats/angle-funnel?dimension=register` → already shipped pre-revision (kept)
- Frontend types → S5
- `RegisterChip` + Lead/Thread detail render → S5
- Settings card → **dropped** (no overrides to edit)
- AngleFunnelPanel dimension toggle → **deferred** (backend ready, frontend can add when there is data to read)

## Implementation log (2026-05-10)

**Status:** done

| # | Unit | Outcome | Evidence |
|---|------|---------|----------|
| R1 | Delete over-built modules + settings block | done | `git status` shows `autosdr/tone_register.py` + `tests/test_tone_register.py` deleted; `autosdr/config.py` no longer carries `tone_registers` block |
| S1 | Inline register prose into generation.py + fix typos | done | `generation.py:48-185` carries `ToneRegisterT` + `_REGISTER_INSTRUCTIONS` + `render_register_block`; "Do NOT use" replaces "DO use" in `professional`, `personal_services`, `aged_care` |
| S2 | Add `tone_register` to analysis prompt + bump v3.6 → v3.7 | done | `analysis.py:34` PROMPT_VERSION; `_RULES_TONE_REGISTER` block at `analysis.py:294`; `test_analysis_prompt_advertises_tone_register_field` |
| S3 | Pipeline reads register from analysis output | done | `outreach.py:_VALID_TONE_REGISTERS` + persistence at `outreach.py:412`; `_shared.py::generate_and_evaluate` reads `thread.tone_register or None`; 3 new pipeline tests |
| S4 | Update SHAs + mock keys + replace kill-switch test | done | `test_prompts.py:91` SHA `999d35f631d0386a` for `analysis-v3.7`; mock keys bumped via repo-wide replace; `test_outreach_collapses_*` tests cover defensive paths |
| S5 | Frontend chip + types | done | `RegisterChip.tsx` component; `types.ts:ToneRegister` literal + `Thread.tone_register`; chip rendered on `ThreadDetail.tsx:407` and `LeadDetail.tsx:262`; `tsc -b --noEmit` clean |

**Final state of success criteria** (mapped to the SIMPLER design):

- Per-register voice swap-in works: ✓ — `test_generation_includes_register_block_for_known_register` proves the `professional` register renders between `_RULES` and `_REFERENCE_EXAMPLES`; `test_generation_register_blocks_differ_per_register` proves four registers produce four distinct prompts.
- Family-Lawyer prompt has the professional block, Plumber doesn't: ✓ — same tests above. The picker is now the analysis LLM, not a substring map; the SC's intent (right block lands per recipient) is preserved with one extra layer of trust on the analysis enum.
- Cohort opener-shape audit (≥ 90% per register): **deferred** — needs ticket 0016 deploy-watch (still pending). The structural plumbing this ticket lands lets that audit be a 1-shot SQL stratification when the surface exists.
- `Thread.tone_register` populated on every new outreach send when the analysis LLM picks a concrete register: ✓ — `test_outreach_persists_tone_register_from_analysis_output`. `"unknown"` collapses to NULL: ✓ — `test_outreach_collapses_unknown_register_to_null`. Invalid tokens collapse to NULL: ✓ — `test_outreach_collapses_invalid_register_token_to_null`.
- `ThreadOut.tone_register` round-trips: ✓ — `test_thread_out_round_trips_tone_register` (parametrized 3 ways).
- Settings shows resolved register per `Lead.category` + override honoured: ✗ — **deliberately dropped per re-council**. No seed map, no override surface, no Settings UI card. Operator's recourse for a wrong-register draft is the existing HITL take-over flow.
- `/api/stats/angle-funnel?dimension=register` returns the new shape: ✓ — endpoint extended in `autosdr/api/stats.py`; `dimension=register` and `dimension=angle_register` group correctly. Frontend toggle deferred (backend contract ready).
- `disabled=true` reverts byte-for-byte to `generation-v8` SHA: ✗ — **deliberately dropped per re-council**. Revert path is `analysis-v3.7` → `v3.6` (drop the new schema field), same cost as any prompt regression revert.
- 661+ backend tests pass: ✓ — **675 passed, 6 skipped** (`tests/` full suite).
- `tsc -b --noEmit` clean: ✓.

**Principle check after implementation:**
- **Simplicity first:** ✓ — re-council *improved* this score. No new module, no new workspace settings, no Settings UI surface. Net change in repo: ~700 LOC deleted, ~150 LOC added.
- **Quality over speed:** ✓ — addresses the "can't sound like 'hey mate,' to a solicitor" problem; analysis-LLM picker uses richer signal than substring map would have.
- **Honest data contracts:** ✓ — `Thread.tone_register` is a structured enum column; closed-vocab guard at persistence prevents arbitrary strings drifting in.
- **Extensible by design:** ✓ — adding a 7th register is one literal token in `ToneRegisterT` + one `_REGISTER_INSTRUCTIONS` entry + one paragraph in `_RULES_TONE_REGISTER`.
- **Human always wins:** ✓ — operator HITL take-over is the recourse for any register mis-fit (existing flow, no new lever needed).
- **Owner stays in control:** ⚠ — *no per-workspace operator-tunable register override*. Trade-off taken deliberately in re-council; recoverable later if a per-workspace skew emerges.

**Follow-ups raised:**

- (none ticketed). If register stratification on the funnel shows a > 15% reply-rate spread across registers in the first 1-2 weeks of post-ship data, file a ticket to either (a) add per-register worked examples to the generation prompt, or (b) adjust the eval threshold per register. Funnel surface (`?dimension=register`) is already in place to detect this.
- AngleFunnelPanel frontend dimension toggle (`by angle` / `by register` / `by angle×register`) — backend contract is ready, frontend can add when there is data to read.

**Open questions still unresolved:**

- (none). Original Open Questions 4 (compose-budget) and 5 (per-register worked examples) noted as deferred / verified in the ticket body.

