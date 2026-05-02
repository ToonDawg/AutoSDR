# Prompt audit — 2026-05-02

**Scope.** `autosdr/prompts/` (5 modules, 1685 LoC) plus the LiteLLM client
that drives them. Goal: determine whether the prompts are bloating input
tokens and how to optimise without breaking the generate / evaluate loop
that gates outbound sends.

**TL;DR.**

1. **There is a smoking-gun bug.** A Python implicit-string-concat
   foot-gun in `evaluation.py` makes every evaluation call ship ~63K
   input tokens instead of ~1.5K. ~50% of total LLM spend on this DB
   is wasted bug. One-line fix.
2. **The prompts ARE long, but length is mostly justified.** Each
   prompt encodes specific failure modes that were observed and patched
   over 7 generation versions, 4 evaluation versions, 5 analysis
   versions. Aggressive shrink without a regression harness will trade
   today's known failure modes for new silent ones.
3. **The local LLM (Gemma-4-31b on LM Studio at :1234) is loaded with
   only 4K context** — current production prompts can't fit there.
   Useful for ablation smoke tests, not for behavioural verification.
4. **Recommendation.** Ship the bug fix today as a standalone change
   (evaluation-v4.4). Add slice metrics by `prompt_version`. Then
   consider targeted shrink — starting with deduping evaluation against
   generation, not cutting worked examples.

---

## 1. Smoking-gun bug — `evaluation.py:335`

### What's there

```python
return (
    "BACKGROUND CONTEXT (do NOT score these; they are only here so you\n"
    "can judge whether the draft uses them well):\n\n"
    f"Tone guide:\n{tone}\n\n"
    f"Campaign goal: {campaign_goal}\n\n"
    f"Recipient category: {lead_category or 'unknown'}\n\n"
    f"Personalisation angle (background — the draft author saw this):\n{angle}\n\n"
    "=" * 60 + "\n\n"                                                   # ← bug
    f"THE DRAFT TO SCORE ({len(draft)} chars) — score THIS and nothing else.\n"
    "Any criticism in `feedback` MUST quote text that is literally present\n"
    "in the draft below. If you cannot copy-paste the phrase from here,\n"
    "do not mention it.\n\n"
    f"{draft}"
)
```

The author thought they were drawing a 60-char `=` separator. What
Python actually parses (AST-confirmed):

```
BinOp(
  left=BinOp(
    left=JoinedStr(  # all 7 strings before "*" become ONE f-string
      "BACKGROUND CONTEXT (do NOT score these; they are only here so you\n"
      "can judge whether the draft uses them well):\n\n"
      f"Tone guide:\n{tone}\n\n"
      f"Campaign goal: {campaign_goal}\n\n"
      f"Recipient category: {lead_category or 'unknown'}\n\n"
      f"Personalisation angle (background): {angle}\n\n"
      "="
    ),
    op=Mult(),
    right=Constant(value=60)),                    # the ENTIRE thing × 60
  op=Add(),
  right=JoinedStr(  # the rest of the strings get added once
    "\n\nTHE DRAFT TO SCORE (...) — score THIS and nothing else.\n"
    ...
    f"{draft}"))
```

Python concatenates **all adjacent string literals** (including
f-strings) into one big string at parse time, then `* 60` multiplies
that whole thing by 60.

### Reproduction

```python
>>> from autosdr.prompts import evaluation
>>> out = evaluation.build_user_prompt(
...     tone_snapshot="X" * 3276,           # production tone size
...     campaign_goal="Get website build or management leads.",
...     angle="...about 450 chars of angle...",
...     draft="hey mate, ...about 270 chars of SMS draft...",
...     lead_category="Plumber",
... )
>>> len(out)
235867   # should be ~5,000
```

### Cost in production

Confirmed via the DB across 757 evaluation calls (eval-v4.2 + v4.3):

| metric | value |
|---|---|
| avg `tokens_in` per eval call | **63,194** |
| avg `tokens_in` if bug were absent | ~1,500 |
| total spend on eval calls | **$8.60** |
| of which is bug-induced repetition | **~$8.00** |

The model has been receiving 60 stacked copies of `BACKGROUND
CONTEXT...tone...goal...category...angle...=` followed by a single
copy of `THE DRAFT TO SCORE...`. The eval has been silently calibrated
against this pathological prompt.

### One-line fix

```python
"=" * 60 + "\n\n"
```

becomes

```python
+ "=" * 60 + "\n\n"
```

(or, equivalently, hoist the separator to a local: `sep = "=" * 60`.)
Then bump `PROMPT_VERSION` to `evaluation-v4.4` so the change is
attributable in the `llm_call` table.

The same pattern does **not** appear in `analysis.py`, `generation.py`,
`classification.py`, or `followup_reply.py`. Grep confirms.

---

## 2. Prompt sizes (current)

Measured by running `len(build_system_prompt(...))` in-process:

| prompt | system chars | system tokens (≈) | with tone block |
|---|---:|---:|---|
| `classification` | 1,092 | 290 | n/a |
| `analysis` | 15,004 | 4,000 | n/a |
| `evaluation` | 16,481 | 4,400 | n/a |
| `generation` (no tone) | 23,431 | 6,250 | n/a |
| `generation` (3.3K tone block) | 26,519 | 7,070 | yes |
| `followup_reply` (no tone) | 3,842 | 1,025 | n/a |
| `followup_reply` (3.3K tone block) | 6,427 | 1,715 | yes |

For comparison, the local LLM (`google/gemma-4-31b` on LM Studio,
quantised Q4_K_M) is currently loaded with 4,096 tokens of context.
Today's production prompts can't fit:

| prompt | n_keep at attempt | n_ctx | result |
|---|---:|---:|---|
| analysis | 4,358 | 4,096 | HTTP 400 |
| evaluation (post-bugfix) | 4,677 | 4,096 | HTTP 400 |
| generation | 6,293 | 4,096 | HTTP 400 |

If the local model is going to be a usable fallback, either
`loaded_context_length` needs to be bumped at LM Studio side (the
model file itself supports 262K), or the prompts need to fit ~3K.

---

## 3. DB stats — what's actually been costing money

`select purpose, count(*), avg(tokens_in), avg(tokens_out), avg(latency_ms), sum(cost_usd) from llm_call group by purpose;`

| purpose | calls | avg tokens_in | avg tokens_out | avg latency | total cost |
|---|---:|---:|---:|---:|---:|
| `generation` | 837 | 6,345 | 2,829 | 13.5s | **$6.76** |
| `evaluation` | 757 | **63,194** | 117 | 2.8s | **$8.60** |
| `analysis` | 470 | 5,068 | 1,516 | 8.8s | **$2.41** |
| `classification` | 26 | 453 | 59 | 1.6s | $0.004 |
| `other` | 1 | 0 | 0 | 0 | 0 |

Notes on the numbers:

- **Evaluation is HALF of all spend, almost entirely from the bug.**
- **Generation `tokens_out` average is 2,829** even though stored
  responses are ~290 chars (≤ 320-char SMS cap). This is Gemini Flash
  reasoning tokens being billed as completion tokens — the same
  pattern shows up locally (Gemma-4 charges 90%+ of its output budget
  to internal reasoning before producing the final SMS). Real-world
  per-message visible output is small; reasoning tokens are not free.
- **Analysis avg tokens_out 1,516** is similarly reasoning-heavy.
- **Latency** is dominated by Gemini Flash at 8-13s; Flash-Lite eval
  is faster (2.8s) but currently bug-bloated.

---

## 4. What the prompts actually contain (and why)

Each system prompt is a mix of:

1. **Hard rules / mandatory contracts** — JSON schema, length cap,
   credential line ("I build websites for a living" must appear),
   forbidden CTA list. This is the executable spec.
2. **Anti-pattern enumerations** — explicit ban lists (formal openers,
   AI-speak phrasings, retired CTAs, hype words, em-dash punctuation
   tells). These were added one-by-one as failures were observed
   (analysis on v3.5, generation on v7, evaluation on v4.3).
3. **Conditional rules** — case-1-vs-case-2 short-name handling
   ("hey Lions Park," reads as greeting a park, must use third-person);
   stale-info strict signal rules; positive-signal-pivot rule (when the
   angle is positive, the offer must be additive not corrective);
   GBP-vs-website disambiguation.
4. **Worked examples** — generation has 6 worked examples (signature_detail,
   stale_info, weak_presence, GBP-only, positive-signal fallback,
   place-style listing); evaluation has 8 "good feedback" examples.
   These teach voice and calibrate scoring anchors.
5. **Reference data** — analysis has a 28-item franchise prefix list
   (RE/MAX, Bupa, Bunnings, ...) that suppresses owner-name false
   positives, plus 9 ownership keywords. There is also a code-level
   `validate_owner_first_name` that re-runs these checks
   deterministically, so the prompt is partially redundant with code.

The prompt's verbosity is institutional memory. Cutting it without
rebuilding the lesson elsewhere = silent regression in the corresponding
failure mode.

---

## 5. Ablation experiment

Ran each prompt + a hand-rolled "lean" variant (~70-80% reduction by
stripping worked examples + the franchise list + most case-1/case-2
elaboration) against the local Gemma-4-31b. Harness at
`/tmp/autosdr_prompt_lab.py`, full report at
`/tmp/autosdr_prompt_lab_report.json`.

**Headline numbers (lean only, since current prompts don't fit
4K context):**

| purpose | sys chars | usr chars | prompt tokens | latency | output |
|---|---:|---:|---:|---:|---|
| classification (current; already small) | 1,092 | 321 | 400 | 72s | clean JSON, intent=`question`, conf=0.98 ✓ |
| analysis (lean) | 4,193 | 1,480 | 1,658 | 128s | clean JSON, picked `signature_detail` ✓ |
| generation (lean) | 5,359 | 722 | 1,573 | 112s | empty content, all 1,200 output tokens spent on reasoning |
| evaluation (lean) | 2,965 | 1,477 | 1,227 | 154s | empty content, all 1,800 output tokens on reasoning |
| followup (current) | 3,842 | 641 | 1,160 | 105s | empty (all reasoning) |
| followup (lean) | 1,676 | 641 | 584 | 52s | "*yeah, no worries. depends on what you need exactly. usually a few hundred for a basic page. keen for a quick chat to figure it out?*" |

### Caveats from the ablation

- **Reasoning eats output budget on local Gemma-4.** With max_tokens=1200,
  most calls produce 1,197 reasoning tokens and 0 content. Need
  max_tokens >2,000 to reliably see any final answer. This is **NOT
  representative of Gemini Flash production behaviour**.
- **One observed quality regression on lean analysis:** the model
  accepted `owner_first_name: "Mick"` from the evidence
  `"Thanks for the kind words mate, glad we could help. - Mick"`.
  The current production prompt's franchise list + the worked
  ownership-evidence examples would have caught it; the lean version
  didn't. The code-level `validate_owner_first_name` *would* still
  reject it (no ownership word in the quote), so production never
  sees the bad name — but the analysis call burned tokens guessing
  wrong.
- **One CTA regression on lean followup:** the produced reply ended
  in *"keen for a quick chat to figure it out?"* — "keen to chat?" is
  on the retired CTA list. The current followup prompt explicitly
  bans this; my lean version dropped the ban.

Both regressions illustrate the council's central concern: shrinking
the prompt strips guard-rails for low-frequency but high-damage
failure modes.

---

## 6. Council verdict

Convened a four-voice council (Architect, Skeptic, Pragmatist, Critic).
Full positions in §6.1; synthesis in §6.2.

### 6.1 Positions

**Architect (in-context):** Two distinct moves, sequenced. P0: fix the
implicit-concat bug; one line, near-zero risk. P1: treat shrink as a
behaviour change with a regression suite, not a token-budget exercise.
Risk: the bug fix IS technically a behaviour change since the eval
has been calibrated under repeated input.

**Skeptic:** "Prompts are too big" is the wrong frame. After the bug
fix, the real problem is **protecting quality under a token budget**,
not maximal compression. Long prompts are partly executable spec +
regression suite in text. Surprise: flakiness and the 4K Gemma issue
are mainly solved by **routing, max_tokens, and structured output
constraints** — not shorter prose. Fixing the eval bug may do more
for stable latency and cost than any editorial pass on `generation.py`.

**Pragmatist:** Ship the one-line fix today. Half of all eval spend
is the bug. No intentional rule change, version bump + deploy, existing
`prompt_version` logging supports before/after forensics. Don't touch
classification / generation / followup on day one. Surprise: "prompts
are too big" conflates two distinct problems — interpreter foot-gun
(dominant) vs genuine density (real but secondary).

**Critic:** The bug fix is a live A/B swap on the send gate. Pass/fail
distribution was learned under absurd repetition; sane prompts change
what "acceptable" looks like. Trim risk is long-tail regression
(wrong entity, fabricated pain, place-as-person greetings,
staff-as-owner). No regression harness exists. **Surprise: the trap
is loop multiplication** — smaller prompts → worse drafts → eval
rejects more → 3× retries per send → total spend can RISE even with
smaller per-call tokens. Watch attempts-per-send, not just per-call
cost. Rollback isn't `git revert`; it's pinning `prompt_version` for
in-flight threads + slice metrics on pass-rate, attempts-per-send,
HITL rate, and `$/thread`.

### 6.2 Synthesis

**Consensus across all four voices:**

- Fix the bug FIRST, as a standalone change.
- Don't pair the bug fix with a "lean prompts" sweep.
- Aggressive shrink without metrics is a known way to regress.
- Long prompts encode failure-mode lessons; cutting them moves risk
  rather than removing it.

**Strongest dissent (preserved):**

- Critic vs everyone else on the "watch for a day" framing: the
  watching has to be specific (pass-rate, attempts-per-send, HITL
  rate, `$/thread`, by `prompt_version`) — not just "tokens dropped".
  This is a real point and the recommendation should reflect it.

**Premise check (Skeptic challenged the question):** Yes — "prompts
are too big" is the wrong primary frame. After the bug fix the real
problem is structural (eval replays the entire generation doctrine to
score a draft against a rubric — that's the highest-leverage shrink),
not editorial.

**Verdict.**

The user's instinct that prompts are too big is partially right but
secondary. The dominant lever is one bug. Fix the bug, set up the
metrics, then the prompt-shrink question becomes a different (better)
conversation.

---

## 7. Recommended plan

### Phase 1 — today (DONE, 2026-05-02)

1. ✅ **Fix the bug in `evaluation.py:335`.** Hoisted the separator to
   `separator = "=" * 60` and interpolated it as `f"{separator}\n\n"`,
   which moves the `*` out of the adjacent-literal chain. Added a
   block comment above explaining why so the trap can't sneak back.
2. ✅ **Bumped `PROMPT_VERSION` to `evaluation-v4.4`.** Test fixtures
   in `tests/test_outreach_pipeline.py`, `tests/test_reply_pipeline.py`,
   and `tests/test_reply_first_message_only.py` were keyed off the old
   string; bumped them in lockstep.
3. ✅ **Added 3 regression tests** in `tests/test_prompts.py`:
   - `test_evaluation_user_prompt_does_not_repeat_background_context`
     — locks the BACKGROUND CONTEXT block to one occurrence.
   - `test_evaluation_user_prompt_size_is_bounded` — asserts
     `len(build_user_prompt(...)) < 10_000` at realistic max-sized
     inputs (was 235,867 chars pre-fix).
   - `test_evaluation_user_prompt_includes_separator_once` — asserts
     the 60-`=` separator renders exactly once.
   Full suite: 530 passed, 0 failed.
4. ✅ **Live-replayed 8 representative threads against real Gemini
   Flash-Lite** with `scripts/replay_evaluator.py`. Numbers:

   | metric | v4.3 (historical) | v4.4 (live) | delta |
   |---|---:|---:|---:|
   | tokens_in (avg) | ~63,400 | ~5,394 | **-92%** |
   | latency (avg) | ~2.8s | ~1.1s | **-61%** |
   | $/eval call (avg) | ~$0.011 | ~$0.001 | **-91%** |
   | pass flips | — | **0 / 8** | none |
   | overall score Δ | — | -0.024 avg, [-0.135, +0.060] | tiny |

   Net: every thread that passed under v4.3 still passes under v4.4,
   at one-twelfth the input tokens and roughly half the latency.
   Several v4.4 evaluations actually produced **better** feedback —
   they caught a retired "keen" CTA and a comma splice that v4.3 had
   silently waved through (v4.3 was being beaten over the head with
   60 copies of the prompt and was effectively zoning out on the
   nuanced rules).

**Expected ongoing effect:** evaluation token spend should drop ~92%
on every new run. Watch `scripts/llm_call_metrics.py --since
<deploy-date>` over the next few days to confirm in production.

### Phase 2 — this week (PARTIAL)

4. ✅ **Slice-metrics CLI** at `scripts/llm_call_metrics.py`. Reports
   per-`prompt_version` cuts of:
   - calls / errors / token averages / cost
   - evaluator pass-rate + p10/p50/p90 of `overall`
   - attempts-per-send (per thread, with HITL paused-thread count)
   - $/sent-thread, by eval prompt_version
   Read-only. Supports `--since 2026-05-02`, `--purpose evaluation`,
   `--json`. This is the regression harness the project didn't have.
5. ✅ **Golden-replay harness** at `scripts/replay_evaluator.py`. Picks a
   diverse sample of historical threads (mixes pass/fail × angle_type),
   reconstructs the evaluator inputs from the DB, and re-runs the
   *current* `evaluation.PROMPT_VERSION` against the same draft on a
   live Gemini Flash-Lite call. Prints OLD vs NEW score+pass+feedback
   side-by-side with a `pass_flips` summary. Default dry-run; `--apply`
   persists the new `llm_call` rows. Already used to validate v4.4
   (see Phase 1 #4).
6. ⏳ **Watch Phase 1 deploy for 48h.** Specifically: did
   attempts-per-send go up? Did HITL rate go up? Did $/thread go down
   (it should)? If anything regresses, the rollback is reverting the
   `evaluation.py` diff and bumping back to v4.3. Use:
   ```bash
   .venv/bin/python scripts/llm_call_metrics.py --since 2026-05-02
   ```

### Phase 3 — when 1 + 2 are stable (next sprint)

The shrink work, in priority order. Each step is a separate prompt
version bump + a golden-set diff before merge.

7. **Dedup eval against generation** (Skeptic's point — highest
   leverage). Today's eval prompt re-explains the same anti-patterns
   and worked examples that generation already encodes. The eval
   should score against a SHORT rubric, not replay the doctrine. Cut
   the eval system prompt to ~5K chars by reducing it to:
   - the JSON schema + scoring anchors (keep)
   - the anti-pattern checklist (keep — it shapes scoring)
   - cut: 8 worked-feedback examples → keep 2-3
   - cut: extensive category calibration prose → 4 lines

8. **Tone block budget.** The tone snapshot is 3,276 chars and gets
   injected into BOTH `generation.build_system_prompt()` AND
   `evaluation.build_user_prompt()`. Cap it at ~1,500 chars and
   document the cap. The current text repeats itself across "Voice"
   and "Avoid" sections.

9. ✅ **Split rules / examples / data in prompt files
   (2026-05-02).** Each large prompt module now exposes the
   composition as named module-level constants instead of one
   monolithic triple-quoted string:

   - `autosdr/prompts/evaluation.py` →
     `_RULES + _WORKED_EXAMPLES + _OUTPUT_SCHEMA`
   - `autosdr/prompts/generation.py` →
     `_DEFAULT_TONE + _RULES + _REFERENCE_EXAMPLES + _OUTPUT_INSTRUCTION`
   - `autosdr/prompts/analysis.py` →
     `_RULES_INTRO + _RULES_ANGLE_VOICE + _RULES_TRUTHFULNESS +
     _RULES_ENRICHMENT + _RULES_STALE_INFO + _RULES_OWNER_FIRST_NAME
     + _RULES_SHORT_NAME + _OUTPUT_SCHEMA`

   Pure refactor — no `PROMPT_VERSION` bump and no behaviour change.
   `tests/test_prompts.py::test_rendered_prompts_are_byte_stable`
   pins the SHA-256 of every rendered system / user prompt, so any
   future drift fails loudly and forces a deliberate version bump.

   The DATA bucket is already extracted into code:
   `_OWNERSHIP_KEYWORDS` and `_FRANCHISE_BRAND_PREFIXES` in
   `analysis.py` are imported by `validate_owner_first_name` and
   referenced by name from the docstring of
   `_RULES_OWNER_FIRST_NAME` (per audit #10). The prompt teaches
   the SHAPE; the validator enforces the exact list.

   `classification.py` and `followup_reply.py` were left as-is —
   they're already small enough (~1K and ~3K chars) that a split
   would add ceremony without buying ablation safety.

   What this enables:
   - Ablation experiments can drop `_REFERENCE_EXAMPLES` (or
     `_WORKED_EXAMPLES`) without touching rules, then a single
     audit-harness run measures whether examples actually moved
     pass-rate or were ceremonial.
   - `validate_owner_first_name`'s docstring now points at
     `_RULES_OWNER_FIRST_NAME` so the prompt-vs-code contract is
     a one-jump symbol-find.

10. **Move what can move into code.** The franchise prefix list and
    ownership keywords are checked again in
    `validate_owner_first_name`. The prompt only needs to teach the
    SHAPE of the rule; the deterministic list belongs in code only.
    Saves ~600 chars from analysis system.

### Phase 4 — out of scope today, list for later

11. ✅ + 12. ✅ **JSON schema response_format for evaluation — done
    (2026-05-02).** Bumped `evaluation-v4.7`. Defined
    `EVALUATION_RESPONSE_SCHEMA` in `autosdr/prompts/evaluation.py`
    with strict `additionalProperties: false` on both the outer
    object and the inner `scores` object, threaded a new optional
    `json_schema=` kwarg through `complete_json`, and wired the
    eval call site in `autosdr/pipeline/_shared.py` to pass it.

    Capability detection lives in
    `_supports_json_schema_response_format(model)`, which delegates
    to `litellm.supports_response_schema(model=...)`. The wire
    format is picked at call time:

    1. `json_schema` (when supplied AND provider supports it) —
       Gemini 2+, OpenAI gpt-4o family, LM Studio, Anthropic,
       Bedrock, Groq, Databricks per LiteLLM's lookup.
    2. `json_object` — falls back here for older providers.
    3. Text + injected JSON-only instruction — final fallback for
       providers that reject both (kept for safety).

    The contract is "strongest available constraint, never worse
    than today" — callers that don't pass `json_schema` keep the
    pre-change `json_object` behaviour, so `analysis`,
    `classification`, and `followup_reply` see no behaviour change
    from this ticket.

    The prompt's existing JSON schema description block is kept as
    belt-and-braces: if json_schema is silently dropped (provider
    quirk, future LiteLLM regression) the model still has the
    prompt-level instruction. Cost is ~250 chars of system prompt,
    safety value is real.

    **Live audit (golden set, 8 threads,
    `data/audit/05-eval-v4.7-json-schema/`):**
    - `pass_flips`: **0** — every thread that passed under v4.6
      still passes under v4.7.
    - `Δoverall_avg`: -0.0225 (range -0.12 to +0.06) — within
      Flash-Lite's run-to-run noise at temp=0. The two -0.12
      regressions are nitpicks on the optional exclamation in
      "hi there!" / "hey tess!" openers; both still pass the
      0.85 threshold and the eval feedback is well-formed and
      surgical, not vague.
    - `eval_tokens_out_total`: 861 over 8 threads (~108 avg per
      call) — sharply down from the ~120-180 historical range
      under v4.6, because the schema constraint kills extra
      prose / hedging tokens. No self-heal retries fired.
    - `eval_latency_ms_avg`: 1233ms — comparable to v4.6.

    **Unit tests** (`tests/test_llm_call_log.py`) pin the routing
    matrix:
    - schema supplied + provider supports json_schema → wrapper
      sent verbatim and `response_format` logged as
      `"json_schema"`.
    - schema supplied + provider supports only json_object → falls
      back to `{"type": "json_object"}`.
    - schema supplied + provider supports neither → text mode +
      injected JSON instruction (LM Studio / minimal-provider
      escape hatch).
    - schema NOT supplied → preserves the pre-change
      `json_object` behaviour exactly (no-regression contract for
      analysis / classification / follow-up callers).
    - The schema's `scores` keys are pinned identical to
      `SCORING_WEIGHTS`, and the top-level keys to the canonical
      `evaluate_result` output.

13. ✅ **Reasoning-token budget — classification done with a
    twist (2026-05-02).** Added a `reasoning_classification` setting
    in `autosdr/config.py` and wired it through `_classify_reply` in
    `autosdr/pipeline/reply.py` via the new `reasoning_effort`
    kwarg on `complete_json`. LiteLLM accepts `"disable" | "low" |
    "medium" | "high"` and translates to the provider-specific
    budget (`thinking_budget` for Gemini, `reasoning` for OpenAI
    o-series; ignored by providers that don't support it,
    including LM Studio's Gemma today). Bumped
    `classification.PROMPT_VERSION` to `classification-v1.1`.

    **Plot twist from the live smoke
    (`scripts/replay_classifier_smoke.py`, 5 historical inbounds):**
    Flash-Lite was *already* skipping thinking by default for
    classification, so the audit's premise ("burning reasoning
    budget on a 60-token output") didn't hold up. The data:

    | reasoning_effort | tokens_out | latency  | intent flips |
    |---|---:|---:|---:|
    | (no override)    | ~60        | ~1.5s    | n/a (baseline) |
    | `"low"`          | ~180       | ~3s      | 1/5            |
    | `"disable"`      | ~55        | ~1.1s    | 0/5            |

    Setting `"low"` *enabled* thinking that wasn't happening before,
    inflating tokens 3x and latency 2x without improving accuracy
    (it actually flipped a thumbs-up reply from `objection` to
    `negative`). Default flipped to `"disable"` — a no-op against
    today's defaults but a guard against a future provider change
    silently turning thinking back on for Flash-Lite. Test
    `test_classification_forwards_reasoning_effort` (parametrised
    over `None | "low" | "disable" | "medium"`) pins the wiring
    end-to-end.

    Generation + analysis still default to provider reasoning. The
    audit's own steer ("for generation, keep but log separately")
    plus the fact that the angle is the personalisation seed for
    the draft means we don't want to cap those without more
    evidence. The real reasoning-spend lever is **analysis on Flash
    main** (1,516 avg tokens_out per the audit) — left for a
    follow-up once we have a smoke for it.

14. **Local LLM context.** Bump `loaded_context_length` in LM Studio
    from 4,096 to ≥16K so the local fallback can actually run today's
    prompts. Without this the local LLM is only useful for ablation
    of LEAN variants, not for production fallback.

---

## 8. What NOT to do

- **Don't ship a "lean prompts" sweep this afternoon.** Bug fix is
  the win. Pair the bug fix with a behaviour change and you'll never
  attribute the next regression cleanly.
- **Don't measure success as "tokens dropped".** Critic's point: the
  loop can multiply. Measure attempts-per-send and `$/thread`.
- **Don't trust the local Gemma ablation as a behaviour proxy.** It's
  4K-loaded, reasoning-heavy, and a different model family from
  production. Use it for "does the prompt parse and produce coherent
  output?" — not for "is this prompt good?"

---

## 9. Appendix — files & commands

- Ablation harness: `/tmp/autosdr_prompt_lab.py`
- Ablation report: `/tmp/autosdr_prompt_lab_report.json`
- AST proof of bug: `/tmp/parsecheck.py`
- DB queries used:
  ```sql
  SELECT purpose, COUNT(*), AVG(tokens_in), AVG(tokens_out),
         AVG(latency_ms), SUM(cost_usd)
  FROM llm_call GROUP BY purpose;

  SELECT length(system_prompt), length(user_prompt), tokens_in
  FROM llm_call WHERE purpose='evaluation'
  ORDER BY created_at DESC LIMIT 5;

  SELECT prompt_version, COUNT(*) FROM llm_call
  GROUP BY prompt_version ORDER BY 2 DESC;
  ```
- Local LLM smoke test (LM Studio at :1234, OpenAI-compat
  `/v1/chat/completions`):
  ```bash
  curl -s -m 20 http://localhost:1234/v1/chat/completions \
    -H "Content-Type: application/json" \
    -d '{"model":"google/gemma-4-31b","messages":[
        {"role":"system","content":"You answer only in rhymes."},
        {"role":"user","content":"What is your favourite color?"}],
      "temperature":0.7,"max_tokens":120}'
  ```
  Note: the URL form in the original brief (`/api/v1/chat`) is not
  what LM Studio exposes; OpenAI-compat is `/v1/chat/completions`.
  LM Studio rejects `response_format: {"type":"json_object"}` —
  use `"text"` or `"json_schema"`.
