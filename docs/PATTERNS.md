# AutoSDR Patterns

This is the **single contract** for which library, framework, or convention is canonical for each concern in AutoSDR. There is one obvious answer per row. If you need a second answer, change the row before you change the code — not after.

Bootstrapped 2026-04-26 from a fresh repo scan. Maintained by the [`pattern-unifier`](../.claude/skills/pattern-unifier/SKILL.md) skill; consulted by the [`ticket-implementer`](../.claude/skills/ticket-implementer/SKILL.md) skill on every ticket.

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
4. If the change forbids something currently in the tree, include a migration plan (which files, in what order). The migration becomes a ticket via the `project-manager` skill.

---

## Frontend patterns

| Concern               | Blessed                                          | Why                                                | Boundary                                  | Avoid                                                                    | Notes / migration |
| --------------------- | ------------------------------------------------ | -------------------------------------------------- | ----------------------------------------- | ------------------------------------------------------------------------ | ----------------- |
| Build tool            | Vite 8                                           | Already wired; SPA boot via `frontend/index.html`. | —                                         | Webpack, Parcel, Turbopack                                               | —                 |
| Framework             | React 19 + TypeScript                            | Existing baseline.                                 | —                                         | Preact, Solid, vanilla                                                   | —                 |
| Server-state fetching | TanStack Query (`@tanstack/react-query`)         | Cache + retries + stale-while-revalidate for free. | Anything reading `/api/...`               | SWR, redux-toolkit-query, raw `fetch` in components, `useEffect` for data fetching | Raw `fetch` only inside `frontend/src/lib/api.ts`. |
| Client-side routing   | React Router 7 (`react-router-dom`)              | One router per repo.                               | All page-level navigation                 | wouter, Next router, hash-based routing                                  | —                 |
| Styling               | Tailwind CSS v4 + `@theme` tokens (`src/index.css`) | One styling system; tokens enable dark-mode swap. | All components                            | CSS modules, styled-components, emotion, ad-hoc hex colours              | Inline `style={{...}}` allowed only for runtime-dynamic values (e.g. computed widths). |
| Icons                 | lucide-react                                     | Tree-shakeable, consistent stroke.                 | All UI                                    | heroicons, react-icons, emoji as icons                                   | —                 |
| Date formatting       | date-fns                                         | Pure functions, ESM-native, smaller than moment.   | All date display                          | moment, dayjs, ad-hoc `toLocaleString` calls                             | —                 |
| API access            | `frontend/src/lib/api.ts` (only)                 | One audited surface; centralises 409 setup-redirect. | Anywhere needing `/api/...`               | Raw `fetch` / `axios` from components or hooks                           | New endpoint → add a method on `api.ts` and a type in `lib/types.ts`. |
| Type definitions      | Mirror `autosdr/api/schemas.py` in `src/lib/types.ts` | Backend schema is the source of truth.        | All shapes consumed from the API          | Hand-rolling types per component, `any`, untyped `unknown` for API shapes | —                |
| Component layout      | `ui/` (primitives) · `domain/` (AutoSDR concepts) · `layout/` (shell) | Clear primitive vs. concept split.        | All components                            | Mixing domain logic into `ui/`                                           | —                 |
| Local state           | React state (`useState`, `useReducer`)           | Most pages don't need a store.                     | UI-only, single-component state           | Redux, Zustand, Jotai, Recoil, Valtio, MobX (no global store)            | If a global store is genuinely needed, council it before adding. |
| Form state            | `lib/usePatchForm.ts` hook                       | De-facto standard across all 5 Settings sub-cards. | All forms that PATCH a settings-shaped resource | react-hook-form, formik, @tanstack/react-form                            | One-off forms (e.g. Setup wizard) may use plain `useState` per field; promote to `usePatchForm` if shape grows. |
| Switch UI primitive   | `@radix-ui/react-switch`                         | Accessible, headless; styled via Tailwind in `components/ui/`. | All toggles                               | Other Radix primitives (only add when a real need shows up)              | This is the **only** Radix package installed; do not generalise to "we use Radix" without a council. |
| Theme                 | CSS variables on `:root` + `html.light` / `html.dark` swap (`src/index.css`) | Token swap, no JS theme provider.        | All theming                               | Theme providers, theme libs                                              | Preference stored in `localStorage` via `lib/useTheme.ts`. |

## Backend patterns

| Concern              | Blessed                                          | Why                                                | Boundary                                  | Avoid                                                                    | Notes / migration |
| -------------------- | ------------------------------------------------ | -------------------------------------------------- | ----------------------------------------- | ------------------------------------------------------------------------ | ----------------- |
| Web framework        | FastAPI                                          | Existing baseline; pydantic-native.                | —                                         | Flask, Starlette-direct, Django                                          | —                 |
| ORM                  | SQLAlchemy 2.0                                   | One ORM per repo.                                  | All DB access in `autosdr/`               | SQLModel, Tortoise, Peewee, raw psycopg/sqlite3 in app code              | Raw `sqlite3` allowed in `autosdr/db.py` engine setup if needed. |
| Validation           | Pydantic v2                                      | FastAPI-native; settings-aware via pydantic-settings. | All API request/response shapes; settings | dataclasses-as-schemas, attrs, marshmallow, msgspec                      | —                 |
| Outbound HTTP        | httpx                                            | Async-first, FastAPI-aligned.                      | `autosdr/connectors/`                     | requests, urllib in app code                                             | LLM HTTP is owned by LiteLLM; do not call providers via httpx directly. SMS connectors (`textbee`, `smsgate`) own their own short-lived clients per call. |
| Lead-website fetch   | crawlee (`BeautifulSoupCrawler`)                 | Browser-like UA + session pool + retry; identifies 403/429 distinctly from `error`. | `autosdr/enrichment.py` and `scripts/enrich_leads.py` | httpx for homepage scraping in app code; Playwright/Chromium             | Replaced the httpx fetcher 2026-04-29 (supersedes ticket 0012). One fetcher across the standalone script and the scan worker. Signal extraction lives in `autosdr/enrichment_extract.py` so both call sites emit identical envelopes. |
| LLM access           | LiteLLM via `autosdr/llm/client.py`              | Single audited LLM surface; logs every call to DB + JSONL. | All prompted calls                       | Direct `openai` / `google-generativeai` / `anthropic` SDK calls          | Provider/model selection is a config switch in `autosdr/config.py`. |
| Prompts              | Versioned files under `autosdr/prompts/`         | Promptable diff surface; quoted by `autosdr-doc3`. | All prompts                               | Inline f-string prompts in pipeline code                                 | New prompt → new file under `autosdr/prompts/`, bump the version in the filename. |
| SMS connector        | Implements `autosdr/connectors/base.py`          | One contract for swappable connectors.             | `autosdr/connectors/`                     | Hard-coding TextBee / SMSGate calls outside `connectors/`                | New connector → new file in `connectors/`, registered in the connector factory. |
| Settings             | `pydantic-settings` via `autosdr/config.py`      | Env- and DB-backed in one place.                   | All config reads                          | `os.environ.get(...)` outside `config.py`                                | Drift: files outside `config.py` read `os.environ` directly (`webhook.py`, `api/workspace.py`). Migrate when next touched. |
| Logging              | Python `logging` (module-level `logger = logging.getLogger(__name__)`) | Already used across the codebase consistently.  | All app code                              | `print(...)` for non-debug output                       | —                 |
| Background work      | Single async scheduler in `autosdr/scheduler.py` | One process; explicit loop ownership.              | All recurring jobs                        | Celery, RQ, APScheduler, threading.Timer ad hoc                          | —                 |
| Killswitch           | Three-layer pause via `autosdr/killswitch.py`    | Shared by every hot path.                          | All loops, all sends                      | Per-feature pause flags                                                  | —                 |
| Testing              | pytest + pytest-asyncio + respx (HTTP mocking)   | Existing baseline.                                 | All `tests/`                              | unittest, nose, vcrpy                                                    | —                 |
| Database engine      | SQLite today, Postgres-ready via SQLAlchemy URL  | POC simplicity; path to scale documented.          | —                                         | Tying schema to a SQLite-only feature without a fallback                 | See `ARCHITECTURE.md § 1`. |

## Forbidden / migrating

| Item                                | Status            | Reason                                                | Migration plan                                              |
| ----------------------------------- | ----------------- | ----------------------------------------------------- | ----------------------------------------------------------- |
| `requests` (Python)                 | forbidden         | httpx is blessed for outbound HTTP.                   | Not present today; reject on PR if introduced.              |
| `axios`                             | forbidden         | `lib/api.ts` is the only HTTP surface on the frontend. | Not present today; reject on PR if introduced.              |
| `moment`, `dayjs`                   | forbidden         | date-fns is blessed.                                  | Not present today.                                          |
| `wouter`, alternative routers       | forbidden         | React Router 7 is blessed.                            | Not present today.                                          |
| Global state libs (Redux, Zustand, Jotai, …) | forbidden | React state covers current pages.                     | Not present today; if a real need arises, council before adding. |
| Direct LLM SDKs (`openai`, `google-generativeai`) | forbidden | LiteLLM is the single LLM surface.            | Not present today.                                          |
| `os.environ` reads outside `config.py` | migrating-out  | Should funnel through `config.py`.                    | Drift in `webhook.py`, `api/workspace.py`. Migrate opportunistically when those files are next touched; full ticket only if the count grows. |

---

## Decisions log

| Date       | Decision                                              | Notes                                                     |
| ---------- | ----------------------------------------------------- | --------------------------------------------------------- |
| 2026-04-26 | Initial bootstrap from repo scan.                     | All blessed choices reflect actual current usage. No surprise drift found beyond `os.environ` minor case noted above. |
| 2026-04-26 | `usePatchForm` blessed for settings-shaped forms.     | Used in all 5 Settings sub-cards; one-off forms can stay on `useState`. |
| 2026-04-26 | LLM HTTP belongs to LiteLLM, not httpx.               | Tightens the "outbound HTTP" boundary to `connectors/` only. |
| 2026-04-28 | Outbound HTTP boundary widened to include `autosdr/enrichment.py`. | Lead-website enrichment (homepage + robots + sitemap) is the same blessed concern as a connector — same library (`httpx`), different bounded module. See ticket 0011. |
| 2026-04-28 | Typer CLI (`autosdr` entry point) removed — operator console is the single surface. | `uvicorn autosdr.webhook:app`; ad-hoc scripting uses the REST API instead. |
