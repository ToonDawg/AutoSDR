# Ticket & Roadmap Templates

The two formats the PM agent must use when writing things up. Tight, scannable, evidence-backed.

---

## Ticket template

Copy this block when fleshing out a ticket. **Every section is mandatory** — if it's empty, label it `(none)` or `(open)` so the gap is visible.

````markdown
# [TYPE/AREA] Short, action-shaped title

<!-- TYPE: feature | bug | chore | refactor | spike | docs -->
<!-- AREA: ai-loop | connectors | scheduler | api | ui | cli | data | ops -->

## Problem

One paragraph. Who hits this? When? What do they do today as a workaround?
Cite evidence — a HITL pattern, a code path, an operator quote, a competitor
behaviour, a doc section. Don't say "users" — name the persona.

## Hypothesis

If we ship this, [observable change] will happen, measured by [metric or
behaviour]. State the expected magnitude where you can.

## Scope

- Concrete, verb-shaped bullets.
- Each bullet should be testable in isolation.
- Reference files / surfaces from the architecture cheat-sheet
  ([product-context.md § 7](../references/product-context.md)).

## Out of scope

- Things people will assume are included that aren't. Be explicit.
- Tied features deferred to a follow-up ticket.

## Success criteria

- Observable in code, logs, the UI, or a metric. Not "feels better".
- Each criterion has an obvious pass/fail check.

## Effort & risk

- **Size:** S / M / L / XL (per [research-playbook.md § 3](research-playbook.md#3-technical-spike-scoping))
- **Touched surfaces:** [list from architecture cheat-sheet]
- **Change class:** additive / invasive / breaking
- **Risks:** [migration, prompt-version, killswitch, evaluator drift, ...]

## Open questions

- Specific decisions blocked on user input.
- Don't paper over these — list every one.

## Principle check

Each item in [product-context.md § 3](../references/product-context.md):

- Simplicity first: ✓ / ⚠ / ✗ (note)
- Quality over speed: ✓ / ⚠ / ✗
- Honest data contracts: ✓ / ⚠ / ✗
- Extensible by design: ✓ / ⚠ / ✗
- Human always wins: ✓ / ⚠ / ✗
- Owner stays in control: ✓ / ⚠ / ✗

Any ⚠ or ✗ must be justified or the ticket isn't ready.

## Links

- Spec: [autosdr-doc{1..4}-*.md § X](#)
- Architecture: [ARCHITECTURE.md § Y](#)
- Code: `path/to/file.py:LINE`
- Research brief: `path/to/brief.md` (if any)
- Competitor reference: [competitive-landscape.md](../references/competitive-landscape.md)

## Dependencies

- Blocks: [list of tickets this unblocks]
- Blocked by: [list of tickets needed first]
- Related: [adjacent work]
````

### Title rules

- Action-shaped verb: "Add", "Refactor", "Track", "Replace", "Surface".
- ~ 60 characters max.
- Include the surface in brackets when it's the operator-visible name: `[Inbox] …`, `[Settings] …`, `[CLI] …`.

### Style notes

- **Prefer tables and bullets** over prose. PM output should be scannable.
- **Quote prompt versions** when touching prompts (`generation@v7`, etc.) so audit-log compatibility is part of the discussion.
- **Don't write the implementation.** A sentence pointing at `pipeline/outreach.py` is right; pseudocode usually isn't.
- **No emojis** in tickets. Status checks (`✓ ⚠ ✗`) are an exception.

---

## Roadmap template

Use this when creating `docs/ROADMAP.md` from scratch, or when refactoring an existing one. Once it exists, **edit in place** — don't regenerate.

````markdown
# AutoSDR Roadmap

**Last updated:** YYYY-MM-DD
**Maintainer:** project-manager skill (see `.claude/skills/project-manager/SKILL.md`)

This is the canonical roadmap. Tickets get fleshed out below or in dedicated
files under `docs/tickets/`. Beads (`bd`) is supported as an optional graph
tracker — see `.claude/skills/project-manager/references/beads-integration.md` —
but this document is the source of truth.

> **How to read this:** items are grouped by horizon. Within each group they
> are ranked by RICE score (highest first). Each item: title • problem in one
> line • RICE (or "—") • status • link.

---

## Now — in progress (≤ 4 items)

| Title | Problem | RICE | Owner | Link |
| --- | --- | --- | --- | --- |
| [Title] | [one-line problem] | XX | [name] | [link] |

---

## Next — committed for next quarter

| Title | Problem | RICE | Status | Link |
| --- | --- | --- | --- | --- |
| [Title] | [one-line problem] | XX | ready / scoping / blocked | [link] |

---

## Later — high-confidence, not yet committed

Same table format. Cap at ~ 8 items; deeper backlog goes into "Considered".

---

## Considered, not committed

<details>
<summary>Click to expand (low-priority backlog)</summary>

| Title | Problem | RICE | Why deferred |
| --- | --- | --- | --- |
| [Title] | [one-line problem] | XX | [explicit reason] |

</details>

---

## Done — last 90 days

Most-recent first. Each row: title • completion date • PR / commit ref •
release-note one-liner.

| Title | Date | Ref | Note |
| --- | --- | --- | --- |
| [Title] | YYYY-MM-DD | #PR or sha | [one line a user would care about] |

---

## Decisions log

Append-only. One bullet per material call (a deferral, a pivot, a principle
clarification). Never edit past entries.

- **YYYY-MM-DD** — [decision]. Rationale: [why]. (Source: [conversation, doc, or PR])

---

## Out of scope (current POC)

Mirror of `autosdr-doc1-product-overview.md § 5`. Update when the source doc
updates. These are pre-approved future-work candidates; **moving an item from
here into Now/Next requires explicit user sign-off** because it's a strategy
shift, not a normal prioritization call.

- Unstructured-text lead imports
- Website scraping / lead enrichment agents
- Multi-tenancy / SaaS / billing
- iOS SMS integration
- Email connector
- CRM integrations
- AI lead scoring / prioritization
- Conversational config UI
- LLM fine-tuning
````

### Maintenance rules

- **One thing per row.** If you're tempted to combine, split into two rows.
- **No items without a problem statement.** "Improve UX" is not a roadmap item.
- **Cap each section.** If `Now` is > 4, you're committed to too much. If `Next` is > 8, you're not really prioritising.
- **The Done section is the user's release-notes feed.** Write notes a non-engineer can understand.
- **Update the `Last updated` date every time you touch the file.**

### When to write a separate ticket file vs. inline

- **Inline (in the roadmap row):** items with a clear one-liner problem and obvious scope.
- **Separate file (`docs/tickets/[ticket-id]-[slug].md`):** anything the engineering team needs to read before starting — i.e. anything that uses the full ticket template.

If you spawn a separate file, link to it from the roadmap row. Don't duplicate.

---

## Review checklist (apply before delivering)

Before showing a ticket or roadmap update to the user:

- [ ] Persona named (not "users")
- [ ] Hypothesis stated, with metric or observable
- [ ] Scope and out-of-scope both populated
- [ ] Success criteria are observable, not vibes
- [ ] At least one source / link cited
- [ ] Principle filter applied — any ⚠/✗ justified
- [ ] Open questions enumerated rather than papered over
- [ ] Effort sized; risks named
- [ ] No emojis, no jargon, no marketing voice
