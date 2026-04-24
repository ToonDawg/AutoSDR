# AutoSDR · Operator Console

The frontend for AutoSDR. A single-operator console for watching an
AI-driven SMS outreach loop and stepping in whenever a lead replies.

In production the built `dist/` is served by the FastAPI backend itself, so
there's just one process and one port. In dev you can run Vite separately for
HMR and it proxies `/api` to the backend.

## Stack

| Concern     | Choice                   |
| ----------- | ------------------------ |
| Build       | Vite 8                   |
| Framework   | React 19 + TypeScript    |
| Styling     | Tailwind CSS v4 (`@theme` tokens) |
| Data        | TanStack Query           |
| Routing     | React Router 7           |
| Icons       | lucide-react             |
| Dates       | date-fns                 |

Two font families (Instrument Sans + JetBrains Mono). The palette is a warm
paper / ink pair with a handful of accent tones (rust, forest, mustard,
oxblood, teal) mapped to semantic meaning — they live as CSS variables on
`:root` so dark mode is a token swap.

## Running it

### Shared with the backend (recommended)

```bash
cd frontend && npm install && npm run build && cd ..
uv run autosdr run
```

Open <http://localhost:8000>. The backend serves `/api/...` and, for
everything else that accepts HTML, serves `dist/index.html` — the SPA handles
routing client-side.

On a fresh server the API returns `409 { setup_required: true }`. The frontend
catches that and redirects to `/setup`, a three-step wizard that writes the
initial `workspace` row, seeds default settings, and hot-wires the connector.
All other configuration after that happens on the Settings page.

### With HMR (one command)

From the repo root:

```bash
./scripts/dev.sh
```

Starts uvicorn (with `--reload`) and Vite together, prefixes the output
with `[api]` / `[ui]`, and shuts both down on Ctrl+C. Then open
<http://localhost:5173>. Override `BACKEND_PORT` / `FRONTEND_PORT` in the
environment if the defaults clash.

### With HMR (two processes, by hand)

```bash
# terminal A — the API + scheduler
uv run uvicorn autosdr.webhook:app --reload --port 8000

# terminal B — Vite dev server on 5173, proxies /api -> 8000
cd frontend && npm install && npm run dev
```

The proxy is defined in `vite.config.ts`; change the target there if your
backend lives elsewhere.

## Structure

```
src/
  main.tsx              # QueryClientProvider + BrowserRouter boot
  App.tsx               # Route table + SetupGate (redirects to /setup if needed)
  index.css             # @theme tokens, base typography, utility classes
  lib/
    types.ts            # TypeScript mirrors of autosdr/api/schemas.py
    api.ts              # fetch wrapper + one method per REST endpoint
    format.ts           # phone / date / enum-label formatters
    utils.ts            # cn(), small helpers
  components/
    ui/                 # Button, Badge, Input, FilterTabs, SearchInput, PageHeader, BackLink
    domain/             # ThreadStatusBadge, AngleTag, MessageBubble, QuotaMeter, ConnectorPicker
    layout/             # AppShell, TopBar, Sidebar, KillSwitch
  routes/
    Setup.tsx           # /setup — first-run wizard
    Dashboard.tsx       # / — status + HITL queue + sparkline + campaign quotas
    Inbox.tsx           # /inbox — threads waiting for a human reply
    Threads.tsx         # /threads — all threads index
    ThreadDetail.tsx    # /threads/:id — messages + Suggested Replies card
    Campaigns.tsx       # /campaigns — list + inline create
    CampaignDetail.tsx  # /campaigns/:id — stats + per-thread table
    Leads.tsx           # /leads — searchable table
    LeadsImport.tsx     # /leads/import — preview + commit
    Logs.tsx            # /logs — LLM call audit, filterable
    Settings.tsx        # /settings — workspace / LLM / connector / AI behaviour / rehearsal
    NotFound.tsx        # 404
```

## Suggested Replies (core UX)

First-message-only is the default mode. That means on any inbound reply the
pipeline:

1. Classifies the intent. Negatives and "goal achieved" auto-close.
2. Generates two or three candidate replies in parallel with varied
   temperatures, evaluates each, and stashes them on
   `thread.hitl_context.suggestions`.
3. Parks the thread in `paused_for_hitl`.

On `/threads/:id` the operator sees a **Suggested replies** card above the
composer. Each card shows the draft, an eval score, and:

- **Send this** → `POST /api/threads/:id/send-draft` with `source: "ai_suggested"`
- **Edit** → copies the draft into the composer
- **Regenerate** → `POST /api/threads/:id/regenerate-suggestions`

The manual composer at the bottom posts to the same `send-draft` endpoint with
`source: "manual"`. Either path records the message, sends via the connector,
and puts the thread back into `ACTIVE` — still no auto-reply until the next
inbound.

## Kill switch & theme

- Top-right: **Pause** / **Resume** toggles the scheduler. Hits `POST
  /api/status/{pause,resume}`; state survives refresh because it's stored in
  the workspace row.
- Theme toggle flips between `html.light` / `html.dark`; preference lives in
  `localStorage`.

## Extending

- New entity → add the Pydantic schema server-side, mirror in `src/lib/types.ts`,
  add the method on `api.ts`, consume via `useQuery`.
- New page → add a route component in `src/routes/`, register in `App.tsx`,
  add a `Sidebar` entry.
- New accent tone → use an existing CSS variable in `src/index.css`. Don't
  introduce ad-hoc colours.
