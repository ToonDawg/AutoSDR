# Template for `docs/PATTERNS.md`

This is the schema for the living patterns contract. Use it verbatim when bootstrapping the file or restructuring it. Keep it short — if a section grows past one screen, split it.

The file has four parts:

1. **Preamble** — what the doc is, how to change it, who reads it.
2. **Frontend patterns** — table.
3. **Backend patterns** — table.
4. **Forbidden / migrating** — table of explicit "do not introduce" entries with a reason.

---

## Template

```markdown
# AutoSDR Patterns

This is the **single contract** for which library, framework, or convention is canonical for each concern in AutoSDR. There is one obvious answer per row. If you need a second answer, you change the row before you change the code — not after.

## How to read this

- **Concern** — the category of work.
- **Blessed** — the one canonical choice. Use this without thinking.
- **Why** — one line on why this won. Picked over what.
- **Boundary** — where the choice is allowed to be used. Empty = anywhere.
- **Avoid** — alternatives that solve the same concern. Don't introduce these without changing the row.
- **Notes / migration** — open migration paths or known exceptions.

## How to change this

1. Run the `pattern-unifier` skill so the change is grounded in a current scan.
2. Propose the diff inline (one row at a time).
3. Land the doc change **before** the code change — the contract leads the diff.
4. If the change forbids something currently in the tree, include a migration plan (which files, in what order). The migration becomes a ticket.

---

## Frontend patterns

| Concern              | Blessed                                       | Why                                                | Boundary                                | Avoid                                | Notes / migration |
| -------------------- | --------------------------------------------- | -------------------------------------------------- | --------------------------------------- | ------------------------------------ | ----------------- |
| Build tool           | Vite 8                                        | Already wired; SPA boot.                           | —                                       | Webpack, Parcel, Turbopack           | —                 |
| Framework            | React 19 + TypeScript                         | Existing baseline.                                 | —                                       | Preact, Solid, vanilla               | —                 |
| Server-state fetching| TanStack Query                                | Cache + retries + stale-while-revalidate for free. | Anything reading `/api/...`             | SWR, redux-toolkit-query, raw `fetch` in components | Raw `fetch` only inside `lib/api.ts`. |
| Client-side routing  | React Router 7                                | One router per repo.                               | All page-level navigation               | wouter, Next router, hash-based      | —                 |
| Styling              | Tailwind v4 + `@theme` tokens (`src/index.css`) | One styling system. Tokens for theming.          | All components                          | CSS modules, styled-components, emotion, inline `style={{...}}` for non-dynamic values | Ad-hoc colour hex codes forbidden — use a token. |
| Icons                | lucide-react                                  | Tree-shakeable, consistent stroke.                 | All UI                                  | heroicons, react-icons, emoji        | —                 |
| Date formatting      | date-fns                                      | Pure functions, smaller than moment, ESM-native.   | All date display                        | moment, dayjs, `Intl.DateTimeFormat` ad hoc | —          |
| API access           | `frontend/src/lib/api.ts` (only)              | One audited surface to the backend; centralises 409 setup-redirect. | Anywhere needing `/api/...`            | Raw `fetch` / `axios` from components | New endpoint → add a method on `api.ts`. |
| Type definitions     | Mirror `autosdr/api/schemas.py` in `lib/types.ts` | Backend schema is the source of truth.         | All shapes consumed from the API        | Hand-rolling types per component, `any`, `unknown` for API shapes | —          |
| Component layout     | `ui/` (primitives) · `domain/` (AutoSDR concepts) · `layout/` (shell) | Clear primitive vs. concept split.               | All components                          | Mixing domain logic into `ui/`       | —                 |
| Local state          | React state (`useState`, `useReducer`)        | Most pages don't need a store.                     | UI-only state                           | Redux, Zustand, Jotai (no global store) | If a global store is needed, council it before adding. |
| Forms                | TBD (track in scan; promote after pattern stabilises) | Decide once, not per page.                | All forms                               | Multiple form libs                   | Currently `useState` per field + `usePatchForm` hook on Settings; pick one. |

## Backend patterns

| Concern              | Blessed                                       | Why                                                | Boundary                                | Avoid                                | Notes / migration |
| -------------------- | --------------------------------------------- | -------------------------------------------------- | --------------------------------------- | ------------------------------------ | ----------------- |
| Web framework        | FastAPI                                       | Existing baseline; pydantic-native.                | —                                       | Flask, Starlette-direct, Django      | —                 |
| ORM                  | SQLAlchemy 2.0                                | One ORM per repo.                                  | All DB access in `autosdr/`             | SQLModel, Tortoise, raw psycopg in app code | —          |
| Validation           | Pydantic v2                                   | FastAPI-native.                                    | All API request/response shapes; settings | dataclasses-as-schemas, attrs, marshmallow | —    |
| Outbound HTTP        | httpx                                         | Async-first, FastAPI-aligned.                      | `autosdr/connectors/` and `autosdr/llm/` only | requests, urllib in app code, raw `fetch` polyfills | —          |
| LLM access           | LiteLLM via `autosdr/llm/client.py`           | Single audited LLM surface; logs every call.       | All prompted calls                      | Direct `openai` / `google-generativeai` calls | —      |
| CLI                  | — (removed — use React UI + REST)              | —                                                  | —                                          | Typer, argparse, click, custom `if __name__ == "__main__"` parsers        | Historical `autosdr` Typer entry point was removed 2026-04; `uvicorn autosdr.webhook:app` only. |
| SMS connector        | Implements `autosdr/connectors/base.py`       | One contract for swappable connectors.             | `autosdr/connectors/`                   | Hard-coding TextBee / SMSGate calls outside `connectors/` | New connector → new file in `connectors/`. |
| Settings             | `pydantic-settings` via `autosdr/config.py`   | Env- and DB-backed in one place.                   | All config reads                        | `os.environ` directly outside `config.py` | —      |
| Logging              | (TBD — track in scan)                         | Pick once.                                         | All app code                            | Mixing `print`, `logging`, `rich.print` ad hoc | —    |
| Background work      | Single async scheduler in `autosdr/scheduler.py` | One process; explicit loop ownership.            | All recurring jobs                      | Celery, RQ, threading.Timer ad hoc   | —                 |
| Testing              | pytest + pytest-asyncio + respx               | Existing baseline.                                 | All `tests/`                            | unittest, nose                       | —                 |

## Forbidden / migrating

| Item                 | Status            | Reason                                            | Migration plan |
| -------------------- | ----------------- | ------------------------------------------------- | -------------- |
| (example) `wouter`   | forbidden         | Second router; React Router 7 is blessed.         | Replace 2 callsites in `Logs.tsx`, drop dep. |
| (example) `requests` | migrating-out     | httpx is blessed for outbound HTTP.               | Audit `autosdr/`; port any usage to httpx; drop dep. |
| (example) `axios`    | dead-dep          | Declared in `package.json`, never imported.       | Drop the dep. |

---

## Decisions log

| Date       | Decision                                | Notes |
| ---------- | --------------------------------------- | ----- |
| YYYY-MM-DD | (initial bootstrap from repo scan)      | See `pattern-unifier` SKILL.md. |

> Keep this log short. Only entries that *change* a row above need to land here.
```

---

## Bootstrap notes

When you create `docs/PATTERNS.md` for the first time:

- Fill the rows from what's actually in `frontend/package.json`, `pyproject.toml`, and the source tree right now. Don't aspire — describe.
- For any concern where the source scan shows two patterns in active use, leave the row's "Blessed" cell as **TBD** and add a row to the **Forbidden / migrating** section with status `decision-pending`. Then surface that decision to the user.
- For concerns the repo doesn't touch yet (e.g. forms, charts, animation), don't pre-bless — leave them out. Add the row when the first feature needs it.

The doc starts thin and grows on demand. Empty rows are worse than missing rows.
