# Research Playbook

How the PM agent runs different kinds of research without ratholing. Pick the right method for the question, timebox it, cite sources.

**Default research bias:** narrow + sourced + opinionated > broad + unsourced + neutral. A 1-page brief beats a 5-page literature review every time.

---

## When to use which method

| Question | Method | Section |
| --- | --- | --- |
| "How does $competitor do X?" | Competitor scan | [§ 1](#1-competitor-scan) |
| "Should we build X?" | Prior-art search | [§ 2](#2-prior-art-search) |
| "Is X technically feasible?" | Technical spike scoping | [§ 3](#3-technical-spike-scoping) |
| "What do operators actually want?" | User-signal synthesis | [§ 4](#4-user-signal-synthesis) |
| "How big is this opportunity?" | Lightweight market sizing | [§ 5](#5-lightweight-market-sizing) |
| "What does this product framework / pattern / library actually do?" | Concept dive | [§ 6](#6-concept-dive) |

If a request matches more than one, pick the one that produces the **decision** the user needs. If the user just wants raw notes, say so back to them and offer to skip the brief format.

---

## 1. Competitor scan

**Goal:** mine 3–5 comparable tools for valuable patterns, not feature-completeness.

**Timebox:** 30 minutes of agent work. If you can't say something useful by then, your sources are wrong.

**Steps:**

1. Pick the tools from [competitive-landscape.md](competitive-landscape.md). Default to "direct" + "open-source comparable" + at most one "AI-native".
2. For each tool, gather:
   - **Marketing claim** — what they say they do (one sentence).
   - **Actual workflow** — the 3–5 steps an operator takes (find a screenshot, demo video, or docs page).
   - **One "win"** — the single thing they get right that we should learn from.
   - **One "miss"** — the single thing they get wrong that AutoSDR avoids or could exploit.
3. Run the **principle filter** ([product-context.md § 3](product-context.md)) on each "win". Reject any that violate it. The survivors are your candidate features.
4. Output as a brief.

**Sources to use:**

- The tool's own pricing + features page (capture date).
- G2 / Capterra reviews — sort by "low rating" first; complaints reveal limits.
- YouTube demo videos by the vendor (skip influencer reviews — they're paid).
- Hacker News threads about the tool ("Show HN" or "Ask HN: alternative to $tool").
- GitHub repo + issue tracker if the tool is open-source.
- The tool's changelog or release notes (most recent 3–6 months).

**Anti-patterns:**

- Trying to be exhaustive. 5 tools deeply > 20 tools shallowly.
- Lifting features without the principle filter. Most enterprise SDR features will fail it.
- Quoting marketing copy as if it's evidence. Find the workflow.

---

## 2. Prior-art search

**Goal:** "has someone solved this already? what did they learn?" Do this before any meaningful build.

**Timebox:** 20 minutes.

**Steps:**

1. Phrase the *problem* as a query, not the solution. ("How do single-operator outbound tools handle compliance opt-outs?" not "STOP keyword filter.")
2. Search:
   - GitHub code search for related solutions (`/search?type=code`).
   - Hacker News (Algolia search) for postmortems and "I built X" threads.
   - The tool's docs in [competitive-landscape.md](competitive-landscape.md) — they often describe the trade-off they made.
   - Academic / RFC corpora when the problem is informational (e.g. E.164 normalization → libphonenumber).
3. Capture:
   - **Existing solutions** (≥ 2). Cite.
   - **Trade-offs each makes.** Be specific.
   - **Why none of them fit AutoSDR** (or where one does).

**Output:** brief. Recommendation must say either "adopt $solution / pattern", "adapt with modification (specify)", or "build greenfield because (specify)".

---

## 3. Technical spike scoping

**Goal:** size the cost of a feature *before* committing.

You are NOT writing the code. You are answering: "is this 1 day, 1 week, or 1 month? What are the unknowns?"

**Steps:**

1. Identify the **touched surfaces** in the AutoSDR architecture cheat-sheet ([product-context.md § 7](product-context.md)). List them.
2. For each surface, classify the change as **additive** (new module, no existing-behaviour risk), **invasive** (modifies a core abstraction or schema), or **breaking** (changes a public contract: API schema, settings shape, prompt version, connector ABC).
3. List **unknowns** — things you can't size without a spike. ("How does TextBee return delivery receipts?" / "Does Gemini's structured output mode handle nested arrays?")
4. Estimate effort in **t-shirt sizes** (S = 1–2 days, M = 3–5 days, L = 1–2 weeks, XL = > 2 weeks). XL items must be split.
5. Flag **risks** — things that could blow up the estimate. (Migration, prompt-version compat, killswitch coverage, evaluator behaviour drift.)

**Output:** a 5-line block to drop into a ticket's "Effort & risk" section. Don't pad.

---

## 4. User-signal synthesis

**Goal:** answer "what do operators actually want?" with sourced evidence, not inference.

**Sources, in priority order:**

1. **HITL escalation patterns.** What reasons appear most in `paused_for_hitl` threads? Run `autosdr logs llm --purpose classification --errors` and skim. This is gold — it's literally the AI failing.
2. **Open issues / discussions** on the AutoSDR repo. (When they exist.)
3. **Operator quotes** the user has shared (even informally). Treat 1 quote per persona type as an N=1 signal, not a trend, but cite it.
4. **Adjacent forums** — r/Entrepreneur, r/sales, r/coldemail, IndieHackers — for *frustrations* with existing tools. Strip the recommendations; keep the pain.
5. **Reviews** of competitors (low-rated first, see § 1).

**Output:** brief with a **quote bank** section. Each quote with source and date.

**Anti-patterns:**

- Treating one Reddit post as a trend.
- "Users want…" without a source. Either cite or label as inference.
- Only using positive signals. Frustrations are more useful for forecasting.

---

## 5. Lightweight market sizing

**Goal:** is this a "10 people care" feature or a "every user cares" feature?

This is for prioritisation, not VC pitch decks. Be order-of-magnitude. Don't pretend to be precise.

**Steps:**

1. Define the **addressable population** in concrete terms. ("Operators on a connector other than `file`" / "operators with > 100 leads imported" / "operators using non-Gemini LLMs".)
2. Estimate the **prevalence** of the trigger. Ranges are fine: < 10%, 10–50%, > 50%.
3. Multiply roughly. Round generously.
4. Compare to other features in the candidate pool. Sizing is comparative, not absolute.

**Output:** one paragraph. Lead with the comparison. ("This affects roughly 1/10 the users that the [other feature] affects — deprioritise unless impact is much higher.")

---

## 6. Concept dive

**Goal:** explain a concept (a library, a pattern, a methodology) well enough that the team can decide whether to use it.

**Steps:**

1. **Definition** — one paragraph in plain language. No jargon you don't define.
2. **What it solves** — the problem it addresses, with a counter-example showing what life looks like without it.
3. **What it costs** — runtime overhead, team familiarity, lock-in, learning curve.
4. **How it would land in AutoSDR** — file paths, surfaces touched, principle-filter check. Be concrete; if you can't be, say so.
5. **Recommendation** — adopt / adapt / pass, with a confidence note.

**Sources:**

- Original docs / RFCs (cite version).
- A maintainer post or talk if available.
- One opinionated critique (find the strongest argument *against* it; if you can't, your research is incomplete).

---

## Citation discipline

Every claim that isn't common knowledge needs a source. Format:

```
[claim] (source: {url or "internal: file:line" or "user, 2026-04-26"})
```

If you can't cite something, label it `[inference]`. The user can tell the difference between a thing you know and a thing you guessed; you should preserve that distinction.

For tools that change frequently (most SaaS), include the **observation date**. "Lemlist supports SMS (observed 2026-04-26)" is honest. "Lemlist supports SMS" is a hostage to fortune.

---

## Output format: research brief

Use this for every research deliverable.

```markdown
# Research: [topic]

**Question:** [single sentence]
**Date:** YYYY-MM-DD
**Method:** [competitor scan / prior-art / ...]

## TL;DR

- [opinionated bullet 1]
- [opinionated bullet 2]
- [opinionated bullet 3]

## Findings

### [Grouping 1]
- [claim] (source: ...)
- [claim] (source: ...)

### [Grouping 2]
- ...

## Implications for AutoSDR

- [what this changes about the roadmap, code, or principles]
- [what it doesn't change]

## Recommendation

[Adopt / Adapt / Pass / Investigate further].

Confidence: [low / medium / high] because [why].

## Open threads

- [things we don't yet know]
- [follow-up research that would tighten the answer]
```

Keep the whole brief to ~1 page when rendered. If it's longer, the question was too broad — split it.
