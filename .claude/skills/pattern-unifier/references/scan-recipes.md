# Scan recipes

Concrete `rg` patterns and file lists per concern. Read this every invocation; don't reinvent the scans. All `rg` examples are run from the repo root unless noted. All paths are relative.

---

## 1. Manifest scan (always run first)

### Frontend

Read `frontend/package.json`. List every entry in `dependencies` and `devDependencies`. For each, ask:

- Is it listed under any concern in `docs/PATTERNS.md`? If not → `◇ Unknown`.
- Does it overlap a "Blessed" row's concern? (e.g. `axios` overlaps "API access".) → `✗ Violation`.
- Is it listed under "Avoid" or "Forbidden"? → `✗ Violation`.

Then check imports: `rg "from ['\"]<pkg>['\"]|require\(['\"]<pkg>['\"]" frontend/src/`. No imports found → **dead dep**.

### Backend

Read `pyproject.toml` `[project] dependencies` and `[project.optional-dependencies] dev`. Same three questions.

Then check imports: `rg "^(from|import) <pkg>" autosdr/ tests/`. No imports → **dead dep**.

---

## 2. Source scan — frontend (`frontend/src/`)

Run each `rg` from the repo root.

### Server-state fetching / API access

```bash
# Direct fetch outside the api wrapper — should be empty except inside lib/api.ts
rg -n "fetch\(" frontend/src/ --glob '!frontend/src/lib/api.ts'

# axios anywhere — forbidden if blessed = TanStack + api.ts
rg -n "from ['\"]axios['\"]" frontend/src/

# useEffect that calls a fetch / api method — almost always wrong; should be useQuery
rg -n -B1 -A4 "useEffect\(" frontend/src/ | rg -B2 -A3 "fetch\(|api\."
```

### Routing

```bash
# Anything other than react-router-dom for routing
rg -n "from ['\"](wouter|react-router|@tanstack/router|next/router)['\"]" frontend/src/ \
  | rg -v "react-router-dom"
```

### Styling

```bash
# CSS modules / styled-components / emotion / inline style with non-dynamic values
rg -n "\.module\.(css|scss)" frontend/src/
rg -n "from ['\"](styled-components|@emotion/.+)['\"]" frontend/src/

# Hard-coded hex colours outside src/index.css
rg -n "#[0-9a-fA-F]{3,8}\b" frontend/src/ --glob '!frontend/src/index.css'

# Inline style with literal colour / px values (heuristic)
rg -n "style=\{\{[^}]*color:" frontend/src/
```

### Icons

```bash
# Anything other than lucide-react
rg -n "from ['\"](@heroicons|react-icons|@radix-ui/react-icons)" frontend/src/
```

### Date handling

```bash
# moment / dayjs anywhere
rg -n "from ['\"](moment|dayjs)['\"]" frontend/src/

# Raw new Date / toLocaleString in render paths (heuristic — manual review needed)
rg -n "toLocaleString\(|toLocaleDateString\(" frontend/src/
```

### State management

```bash
# Any global store
rg -n "from ['\"](redux|@reduxjs/toolkit|zustand|jotai|recoil|valtio|mobx)" frontend/src/
```

### Forms

```bash
# Form libraries (decide once if introduced)
rg -n "from ['\"](react-hook-form|formik|@tanstack/react-form|@hookform/.+)['\"]" frontend/src/
```

### Component layout

```bash
# Domain symbols leaking into ui/ (heuristic — review hits manually)
rg -n "(Thread|Lead|Campaign|Workspace)" frontend/src/components/ui/
```

---

## 3. Source scan — backend (`autosdr/`)

### Outbound HTTP

```bash
# httpx outside its boundary (allowed: connectors/ and llm/)
rg -n "import httpx|from httpx" autosdr/ \
  --glob '!autosdr/connectors/**' \
  --glob '!autosdr/llm/**'

# requests anywhere — forbidden
rg -n "^(import requests|from requests)" autosdr/
```

### LLM access

```bash
# Direct provider SDKs — should go via litellm + autosdr/llm/client.py
rg -n "^(import openai|from openai|from google\.generativeai|import anthropic)" autosdr/
```

### ORM / DB

```bash
# Second ORM
rg -n "from (sqlmodel|tortoise|peewee)" autosdr/

# Raw psycopg / sqlite3 outside db.py
rg -n "^(import sqlite3|from sqlite3|import psycopg|from psycopg)" autosdr/ \
  --glob '!autosdr/db.py'
```

### Validation

```bash
# Schema libs other than pydantic
rg -n "from (marshmallow|attrs|cattrs|msgspec)" autosdr/
```

### Settings

```bash
# os.environ outside config.py
rg -n "os\.environ" autosdr/ --glob '!autosdr/config.py'
```

### CLI

```bash
# argparse / click outside cli.py
rg -n "^(import argparse|from argparse|import click|from click)" autosdr/ \
  --glob '!autosdr/cli.py'
```

### Logging

```bash
# Mixed logging styles (print vs. logging vs. rich.print)
rg -n "^(\s*)print\(" autosdr/
rg -n "^(\s*)logging\." autosdr/
rg -n "from rich import print|rich\.print" autosdr/
```

### Connector boundary

```bash
# TextBee / SMSGate API names showing up outside connectors/
rg -ni "(textbee|smsgate)" autosdr/ --glob '!autosdr/connectors/**'
```

### Background work

```bash
# Alternate schedulers / queues
rg -n "from (celery|rq|apscheduler|huey)" autosdr/

# Threading timers (heuristic — review)
rg -n "threading\.Timer\(" autosdr/
```

---

## 4. Cross-cutting / dead-deps

After the manifest + source scan, cross-reference:

```bash
# For each package P in package.json:
rg -l "from ['\"]P['\"]|require\(['\"]P['\"]\)" frontend/src/ || echo "DEAD: P"

# For each package P in pyproject.toml:
rg -l "^(from|import) P" autosdr/ tests/ || echo "DEAD: P"
```

A package with zero hits in source is a dead dep. Recommend dropping unless it's a build-time / runtime peer (e.g. a vite plugin, a tailwind plugin, a TypeScript type-only import that uses a different name — note the exception in the report).

---

## 5. New-pattern scan

For anything imported in source that is **not** listed under any concern in `docs/PATTERNS.md`:

- If it's clearly a one-off helper (single file, internal-only) → ignore.
- If two or more files import it → propose a new row in `docs/PATTERNS.md` (`◇ Unknown`).
- If it overlaps an existing concern's blessed choice → flag as a violation.

A quick way to enumerate frontend imports:

```bash
rg -oN "from ['\"]([^'\".]+)['\"]" frontend/src/ -r '$1' | sort -u
```

For backend:

```bash
rg -oN "^(from|import) ([a-zA-Z0-9_]+)" autosdr/ -r '$2' | sort -u
```

Diff the resulting list against `docs/PATTERNS.md` rows. Anything in source but not in the doc is a candidate for a new row.

---

## 6. Diff-only scan (for ticket-implementer verify-done)

When the `ticket-implementer` skill calls in at verify-done, restrict scans to changed files:

```bash
# Files changed since the ticket started (assume the user passes the base ref)
git diff --name-only <base>...HEAD -- 'frontend/src/**' 'autosdr/**' 'frontend/package.json' 'pyproject.toml'
```

Run the relevant patterns from §2 / §3 against just those paths. The bar: **no new ⚠ or ✗** introduced by this ticket. Existing drift is fine — that's a separate clean-up.

---

## Output cadence

Run §1, §2, §3 every invocation unless the user is in pre-pick mode (decision support). Skip §6 unless explicitly invoked by the ticket-implementer skill.

If a section has zero findings, still surface it as `✓` in the report — silence makes it look like you skipped the check.
