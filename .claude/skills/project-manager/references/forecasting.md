# Forecasting & Prioritisation

Methods the PM agent uses to score, rank, and sequence work. **Bias toward concrete numbers over hand-wavy adjectives.** A wrong RICE score you can argue with beats "high priority" you can't.

---

## When to score, when not to

Score **everything that lands in `Next` or higher** in `docs/ROADMAP.md`.

Don't score:

- Trivial bugs (just fix them).
- Items in `Considered, not committed` — score lazily when promoting.
- Roadmap entries < 24 hours old where evidence is still being gathered.

If you don't have enough evidence to score, the right answer is usually "do more research" not "guess".

---

## RICE — primary tool

**Score = (Reach × Impact × Confidence) ÷ Effort.**

Use it for any "should we do A or B first?" question with > 1 candidate.

### Reach

How many operators are affected per quarter, given AutoSDR's persona (single-operator self-hosted)?

| Bucket | Score | Heuristic |
| --- | --- | --- |
| Every operator, every campaign | 10 | Touches the core loop on every send/reply |
| Every operator, sometimes | 5 | Triggered by a common condition (HITL escalation, reply received) |
| Most operators, rarely | 2 | Triggered occasionally (setup, configuration, edge cases) |
| Some operators, sometimes | 1 | Specific persona slice (e.g. high-volume only) |
| Rare path | 0.25 | Edge cases, debugging tooling |

These are **relative**, not absolute. Calibrate within the candidate pool.

### Impact

How much does the affected operator's experience change?

| Score | Meaning | Example |
| --- | --- | --- |
| 3 | Massive | "Doubles reply rate" / "removes a recurring failure mode entirely" |
| 2 | High | "Cuts setup time in half" / "replaces a workaround with a built-in" |
| 1 | Medium | "Visibly nicer for an operator who hits this" |
| 0.5 | Low | Polish, minor friction |
| 0.25 | Minimal | Nice-to-have |

If your default impact is "2 high", you're not being honest with yourself. Most things are 0.5–1.

### Confidence

How sure are you that the Reach × Impact estimate is right?

| Score | Meaning |
| --- | --- |
| 100% | Direct evidence (HITL log, operator quote, code-level certainty) |
| 80% | Strong inference from architecture / docs / well-cited research |
| 50% | Educated guess; we'd want a spike or research first |
| 20% | We're spitballing |

Confidence < 50% should usually mean "research first" rather than "ship". If you find yourself at 20% on a high-impact item, the next ticket is the research, not the build.

### Effort

Person-weeks. Be honest, including:

- Migration / backfill (especially for `models.py` schema changes).
- Prompt-version bumps and audit-log compatibility.
- Test coverage (LLM/connector mocking).
- Spec doc updates.
- Killswitch coverage on any new long-running path.

T-shirt → person-week mapping: S = 0.4, M = 1, L = 2.5, XL = 5.

XL items must be split into ≥ 2 deliverable sub-items before scoring.

### Worked example

Imagine `[AI] Add reply-rate per personalization angle to Logs`:

- Reach: every campaign with replies = 5
- Impact: helps operators iterate prompts = 1
- Confidence: 80% (we know operators look at Logs)
- Effort: M = 1 week

RICE = (5 × 1 × 0.8) / 1 = **4.0**

Compare to `[Connectors] TextBee push inbound`:

- Reach: TextBee operators only ≈ 2
- Impact: removes polling latency, frees a knob = 1
- Confidence: 50% (TextBee API surface unknown)
- Effort: M = 1 week

RICE = (2 × 1 × 0.5) / 1 = **1.0**

The first wins; the second wants research first to lift confidence.

---

## MoSCoW — for release-scoping conversations

When the user is planning a *release* and needs a binary "in or out" call, use MoSCoW instead of RICE.

- **Must** — the release fails its goal if this isn't in.
- **Should** — important; cut last.
- **Could** — nice if there's room.
- **Won't (this release)** — explicitly deferred.

Each release has a **goal sentence**. Items are Must/Should/Could *relative to that goal*. Same item can be Must in one release and Could in another.

A release with > 3 Musts is at risk. > 5 is not a release, it's a wishlist.

---

## Sequencing rules

After scoring, check sequencing. Ranks and sequences are different problems:

1. **Risk-first.** If two items have similar RICE, do the riskier one first — its uncertainty has more value to resolve.
2. **Foundation before features.** If item A's effort estimate hides item B as a "while we're here", split and do B first as its own ticket.
3. **One invasive change at a time.** Don't ship two breaking changes to the same surface (prompts, schema, ABC) in the same release.
4. **Shipped > shippable.** A 70%-impact thing that lands beats a 100%-impact thing that doesn't. Prefer smaller scopes.
5. **De-risk by spike.** When confidence < 50% on a top-3 item, schedule a spike *before* the build, even if the spike's RICE is low. Confidence is the lever.

---

## When to defer

Move an item to `Considered, not committed` (or out of the roadmap entirely) when **any** of:

- Reach < 1 *and* Impact < 1 (it serves a tiny slice with marginal value).
- Confidence < 50% with no clear research path.
- It violates a principle ([product-context.md § 3](product-context.md)) and the trade-off can't be justified.
- It depends on a non-goal that hasn't been re-opened by user decision.
- It would force a strategy shift (multi-user, enterprise, etc.) without that decision having been made.

Defer with a **dated note** explaining why. The decision matters more than the deferral.

---

## When to upsize

Some signals say "this is bigger than it looks; rescope":

- The same item keeps appearing in operator pain (3+ HITL patterns, multiple operator quotes).
- A spike doubles your Effort estimate.
- Two existing items would collapse into a cleaner abstraction if a third (foundation) item came first.

When upsizing, write the sequencing decision into the **Decisions log** in `docs/ROADMAP.md` so future-you remembers why.

---

## Anti-patterns

- **Score-stacking.** Putting RICE on a list and calling it prioritisation. The point is the *argument* the score makes.
- **Recency bias.** Scoring whatever was discussed last 8/10. Always rank against the existing top 3, not in isolation.
- **HiPPO.** "The user said this is important" without RICE is a signal, not a decision. Score it like everything else; if it loses, push back with the math.
- **Effort ≈ 0 features.** "It's a one-line change" → still has tests, docs, prompt-version bumps, killswitch coverage. Round up.
- **Confidence inflation.** If you wrote "high confidence", show the evidence. If you can't, demote it.
