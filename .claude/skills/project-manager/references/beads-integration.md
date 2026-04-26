# Beads (`bd`) — Optional Graph Tracker

The canonical roadmap is `docs/ROADMAP.md`. **Beads is optional** — adopt it only when the user explicitly opts in or when the roadmap has clearly outgrown a flat markdown file (≥ ~ 30 active items, dependency graph that no longer fits in a table).

This file is the cheat-sheet for that opt-in. If beads is not in use, ignore the rest of this document.

**What beads is:** a git-backed, Dolt-powered issue tracker designed for AI agents. Hierarchical IDs (`bd-a3f8.1.1`), dependency graph (`blocks`, `parent-child`, `relates_to`), JSON output for tooling, atomic claim semantics. Source: <https://github.com/steveyegge/beads>.

---

## When to suggest opting in

Suggest beads (don't auto-install) when **any** of:

- The roadmap has > 25 active items across `Now`/`Next`/`Later`.
- There are ≥ 5 dependency relationships people are tracking by hand.
- The user mentions "graph", "dependency", "blocked by", or asks for a tool.
- Multiple agents are concurrently working on tickets and need atomic claim.

Phrase the suggestion plainly: "the roadmap is large enough that a graph tracker would help — beads (`bd`) drops in cleanly. Want me to set it up?"

When the user opts in, run **Bootstrap** below. Don't run it preemptively.

---

## Bootstrap (only when opted in)

Run interactively — confirm before each side-effecting step.

```bash
# 1. Verify install
which bd || (
  echo "Beads not installed. Recommend:" \
    && echo "  brew install beads     # macOS" \
    && echo "  npm install -g @beads/bd" \
    && echo "  curl -fsSL https://raw.githubusercontent.com/gastownhall/beads/main/scripts/install.sh | bash"
)

# 2. Initialise inside the repo
bd init

# 3. Tell agents to use it (project AGENTS.md / CLAUDE.md)
echo "Use 'bd' for task tracking. The canonical roadmap remains docs/ROADMAP.md; bd mirrors it for graph queries." >> AGENTS.md

# 4. Smoke-test
bd info
bd ready --json
```

**Important:** check `.gitignore` — beads writes a `.beads/` directory that *should* be committed (that's how it syncs across clones). The default `bd init` gets this right; don't add `.beads/` to `.gitignore` unless using `--stealth`.

If the user wants beads kept local (not committed), use:

```bash
bd init --stealth
```

---

## Mapping markdown roadmap → beads

When mirroring the roadmap into beads, use this convention:

| Roadmap concept | Beads | Notes |
| --- | --- | --- |
| Section ("Next", "Later") | Label (`section:next`) | Easy to filter |
| Big initiative | Epic — `bd create "..." -t epic` | Use hierarchical ID for child tasks |
| Ticket | Task — `bd create "..." -t feature` (or `-t bug`, `-t chore`) | Title matches roadmap row |
| Out-of-scope | Don't import | These aren't tracked work |
| Done | `bd close` with a closing note | Closed items still searchable |
| RICE score | Priority (`-p 0` highest, `-p 4` lowest) and a `rice:NN` label | Beads' priority is coarse; label keeps the number |
| Dependency | `bd dep add <a> blocks <b>` | This unlocks `bd ready` |
| Decisions log | Append to roadmap; reference `bd-id` if applicable | Keep the log human-readable in markdown |

**The roadmap stays canonical** — when in doubt, edit `docs/ROADMAP.md` first, then run `bd update` to mirror. Don't let beads diverge silently.

---

## Day-to-day commands

```bash
# What's unblocked and ready to start?
bd ready

# What's in flight?
bd list --status in_progress

# What's blocked, and on what?
bd blocked

# Inspect a single item
bd show bd-a1b2

# Create a task with a parent epic
bd create "Add TextBee push inbound" -t feature -p 1 \
  --parent bd-a3f8 \
  --description "$(cat <<'EOF'
Problem: ...
Hypothesis: ...
EOF
)"

# Add a blocking dep
bd dep add bd-a1b2 blocks bd-a3f8.2

# Atomically claim and start work
bd update bd-a1b2 --claim

# Close with a note (closing a ticket creates an audit row)
bd close bd-a1b2 "Shipped in #42 — TextBee polling cadence shortened from 15s to 8s."

# JSON for scripting / agent ingestion
bd ready --json
bd list --status open --json | jq '.[].id'
```

---

## PM-flavoured beads workflows

### Forecasting against the graph

Instead of (or alongside) RICE, sort by:

```bash
bd ready --json | jq 'sort_by(.priority) | .[] | {id, title, priority, labels}'
```

Then layer the RICE rubric ([forecasting.md](forecasting.md)) on top. Beads' priority field is coarse; use the `rice:NN` label for the score.

### Spotting orphan work

```bash
# Issues with no parent epic, no blockers, no dependents
bd list --json | jq '.[] | select(.parent == null and (.blocks // []) == [] and (.blocked_by // []) == [])'
```

These are usually scope creep — should they belong to an existing epic, or should they be deferred?

### Generating roadmap rows from beads

When you want to refresh the markdown roadmap from beads state:

```bash
bd list --status open --json | jq -r '
  .[] |
  "| \(.title) | \(.description // "—" | split("\n")[0]) | \(.labels[]?|select(startswith("rice:"))|sub("rice:";"")) | \(.status) | bd-\(.id) |"
'
```

Don't run this destructively against the roadmap — diff first, merge by hand. The markdown file has decisions and history beads doesn't capture.

---

## Known sharp edges

These are gotchas observed in the wild — keep them in mind so you don't burn time.

- **Daemon is gone (Feb 2026).** Don't expect a long-running `bd daemon`; `--local` flags or the embedded mode is the path.
- **Epic status with all children done** has occasionally shown as `BLOCKED` instead of complete. Check `bd show <epic-id>` if status looks weird; close manually if needed.
- **`bd init --from-jsonl` on a fresh clone** can fail with "database not found". Workaround: `bd backup restore` from `.beads/backup/`.
- **Dolt under the hood** means initial install pulls a chunky binary. Don't suggest beads for tiny throwaway projects.

---

## When NOT to use beads

- Solo operator with < 10 active items. Markdown is faster.
- Project where the roadmap audience includes non-technical stakeholders. Beads' UI/CLI surface is engineer-shaped.
- Hostile network environments (corporate proxies, locked-down CI). Embedded mode is fine but install ergonomics can fight you.
- When the user has explicitly said "no extra tools".

When in doubt, stay in markdown. Adding beads later is cheap; ripping it out is expensive.
