"""FastAPI app: REST API + static frontend + scheduler lifecycle.

Single-process boot: the one ``uvicorn autosdr.webhook:app`` serves both
``/api/*`` JSON routes and the built React frontend at ``/``. No separate
Vite dev server is needed in production — the plan's goal 5.

Async tasks inside the app lifespan:

1. ``run_scheduler`` — outreach tick loop (sends queued leads).
2. ``run_inbound_poller`` — TextBee / file connector poll for inbound SMS.
3. ``watch_flag_file`` — notices the pause flag appearing / disappearing.

Startup order matters: we hot-apply the workspace's LLM provider keys into
``os.environ`` *before* the scheduler fires so the first outreach tick has
valid credentials without needing a restart.
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from sqlalchemy.orm.attributes import flag_modified

from autosdr import killswitch
from autosdr.api import ALL_ROUTERS
from autosdr.api.deps import SETUP_REQUIRED_STATUS
from autosdr.api.errors import install_exception_handlers
from autosdr.config import get_settings, merge_workspace_settings
from autosdr.connectors import get_connector
from autosdr.db import create_all, session_scope
from autosdr.llm import apply_llm_provider_keys, get_usage_snapshot
from autosdr.models import Workspace
from autosdr.scheduler import run_inbound_poller, run_scheduler

logger = logging.getLogger(__name__)


def _load_and_backfill_workspace_settings() -> dict | None:
    """Load the workspace settings blob, self-healing any legacy gaps.

    This is distinct from the plain readers in :mod:`autosdr.workspace_settings`
    because it also mutates: legacy workspaces created before the current
    settings schema (or by the old CLI init path) can be missing top-level
    keys like ``connector``, ``auto_reply_enabled``, ``rehearsal``, or
    ``llm.provider_api_keys``. The UI Settings page relies on those keys
    to render inputs, and the status endpoint/pipeline code otherwise has
    to paper over them with ``.get(..., default)`` everywhere. Merging the
    defaults back in once at boot keeps the DB, the UI, and the runtime
    in sync. We keep the read + write in one session so the caller gets a
    dict that already reflects what was just committed.

    Obsolete keys (settings we used to support but have since removed) are
    pruned here too — see :func:`_strip_obsolete_settings`. Without this
    sweep, deep-merge would keep them in the JSON forever; with it, the
    next boot quietly cleans up after the previous schema.
    """

    with session_scope() as session:
        workspace = session.query(Workspace).first()
        if workspace is None:
            return None

        existing = dict(workspace.settings or {})
        _strip_obsolete_settings(existing)
        merged = merge_workspace_settings(existing, {})
        if merged != (workspace.settings or {}):
            workspace.settings = merged
            flag_modified(workspace, "settings")
            logger.info(
                "workspace=%s settings backfilled / pruned to current schema",
                workspace.id,
            )
        return dict(merged)


def _strip_obsolete_settings(blob: dict) -> None:
    """Remove keys we no longer honour from a settings dict in place.

    Currently:

    * ``rehearsal.dry_run`` — replaced by ``connector.type == "file"``.
      The factory ignores the flag at runtime, but we want it gone from
      the persisted JSON so old workspaces stop carrying a misleading
      "dry-run is on!" hint that has no effect.
    """

    rehearsal = blob.get("rehearsal")
    if isinstance(rehearsal, dict):
        rehearsal.pop("dry_run", None)


def create_app(*, run_scheduler_task: bool = True) -> FastAPI:
    """Build the FastAPI app.

    ``run_scheduler_task=False`` is used by tests that drive the reply
    pipeline directly without the scheduler or poller running.
    """

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        scheduler_task: asyncio.Task | None = None
        poller_task: asyncio.Task | None = None
        flag_watcher: asyncio.Task | None = None

        # Create any missing tables + apply additive column migrations
        # *before* any session opens. This keeps ``uvicorn
        # autosdr.webhook:app`` boots in parity with the CLI entrypoints,
        # which call ``create_all()`` in their own startup paths. Without
        # this, pulling new code that adds a nullable column to an
        # existing table (e.g. ``campaign.followup``) produces an
        # ``OperationalError: no such column`` on the next read.
        try:
            create_all()
        except Exception:
            logger.exception("failed to initialise db schema on boot")
            raise

        # Apply LLM provider keys from settings into os.environ before any
        # scheduler tick runs — LiteLLM reads them at call time.
        settings_blob = _load_and_backfill_workspace_settings()
        if settings_blob is not None:
            try:
                apply_llm_provider_keys(settings_blob)
            except Exception:
                logger.exception("failed to apply llm provider keys on boot")

            # Build and cache the connector so the first tick doesn't stall
            # behind a cold start. If the workspace hasn't been set up yet,
            # we skip this — the scheduler + poller gracefully no-op until
            # setup completes.
            try:
                app.state.connector = get_connector()
            except Exception as exc:
                logger.warning("connector unavailable at boot: %s", exc)
                app.state.connector = None
        else:
            logger.info(
                "no workspace yet — waiting for setup wizard before starting scheduler"
            )
            app.state.connector = None

        if run_scheduler_task and app.state.connector is not None:
            scheduler_task = asyncio.create_task(
                run_scheduler(app.state.connector), name="autosdr.scheduler"
            )
            poller_task = asyncio.create_task(
                run_inbound_poller(app.state.connector), name="autosdr.inbound_poller"
            )
        flag_watcher = asyncio.create_task(
            killswitch.watch_flag_file(), name="autosdr.flag_watcher"
        )

        try:
            yield
        finally:
            killswitch.mark_shutting_down()
            killswitch.shutdown_event().set()
            for task in (scheduler_task, poller_task, flag_watcher):
                if task is None:
                    continue
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):  # pragma: no cover
                    pass

    app = FastAPI(title="AutoSDR", lifespan=lifespan)
    install_exception_handlers(app)

    @app.get("/healthz")
    async def healthz() -> dict[str, Any]:
        connector = getattr(app.state, "connector", None)
        return {
            "status": "ok",
            "paused": killswitch.is_flag_set(),
            "shutting_down": killswitch.is_shutting_down(),
            "connector": connector.__class__.__name__ if connector else None,
            "llm_usage": get_usage_snapshot(),
        }

    # ------------------------------------------------------------------
    # API routers
    # ------------------------------------------------------------------
    for router in ALL_ROUTERS:
        app.include_router(router)

    # ------------------------------------------------------------------
    # Static frontend (built Vite bundle)
    # ------------------------------------------------------------------
    _attach_frontend(app)

    return app


def _attach_frontend(app: FastAPI) -> None:
    """Mount the built React SPA at ``/`` with an index-html fallback.

    We don't want the existence of the frontend to be load-bearing — if
    ``frontend/dist`` hasn't been built yet we just log a warning and
    serve API-only. The SPA handles 404s client-side anyway.
    """

    dist_dir: Path = get_settings().frontend_dist_dir
    if not dist_dir.exists():
        logger.warning(
            "frontend dist directory %s not found — skipping static mount "
            "(run `cd frontend && npm run build`)",
            dist_dir,
        )
        return

    assets_dir = dist_dir / "assets"
    if assets_dir.exists():
        app.mount(
            "/assets",
            StaticFiles(directory=str(assets_dir)),
            name="assets",
        )

    index_html = dist_dir / "index.html"
    if not index_html.exists():
        logger.warning("frontend index.html missing at %s", index_html)
        return

    # Serve specific public-root files (favicon, robots, manifest) directly.
    for static_file in ("favicon.svg", "favicon.ico", "icon.svg", "icons.svg", "robots.txt", "manifest.json"):
        asset = dist_dir / static_file
        if asset.exists():
            _register_root_file(app, asset, "/" + static_file)

    @app.get("/", include_in_schema=False)
    async def serve_root() -> FileResponse:
        return FileResponse(index_html)

    @app.get("/{full_path:path}", include_in_schema=False)
    async def spa_fallback(full_path: str, request: Request) -> FileResponse:
        """Catch-all that returns index.html for SPA deep links.

        Keeps API 404s as real 404s: we only rewrite GETs that (a) are not
        under ``/api/`` and (b) accept HTML. Anything else (an asset the
        build didn't produce, an API caller pinging a missing endpoint)
        gets a genuine 404.
        """

        if full_path.startswith("api/"):
            raise HTTPException(status_code=404, detail={"error": "not_found"})

        accept = request.headers.get("accept", "")
        if "text/html" not in accept and "*/*" not in accept:
            raise HTTPException(status_code=404, detail={"error": "not_found"})

        return FileResponse(index_html)


def _register_root_file(app: FastAPI, asset: Path, url_path: str) -> None:
    """Register a single static file at ``url_path``."""

    @app.get(url_path, include_in_schema=False)
    async def _serve_root_file() -> FileResponse:  # noqa: D401
        return FileResponse(asset)


# ---------------------------------------------------------------------------
# Module-level app instance for ``uvicorn autosdr.webhook:app``.
# ---------------------------------------------------------------------------


app = create_app()


__all__ = ["app", "create_app", "SETUP_REQUIRED_STATUS"]
