"""FastAPI app: webhook ingress + health.

The POC runs three concurrent async tasks inside the FastAPI lifespan:

1. ``run_scheduler`` — outreach tick loop (sends queued leads).
2. ``run_inbound_poller`` — pulls new inbound messages from the connector
   (TextBee polling) and drives the reply pipeline.
3. ``watch_flag_file`` — notices the pause flag appearing/disappearing.

Two HTTP entry points for inbound messages:

* ``POST /api/webhooks/sms`` — push-based: the active connector's
  ``parse_webhook`` is invoked. Used by SmsGate (the Android app POSTs to us
  on the LAN).
* ``POST /api/webhooks/sim`` — dev-only simulator, accepts a simple
  ``{"contact_uri": ..., "content": ...}`` payload regardless of which
  connector is active.

Poll-based connectors (TextBee) just ignore ``/api/webhooks/sms`` — the
endpoint is still live, it's just inert because nothing POSTs to it.
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from typing import Any

from fastapi import BackgroundTasks, FastAPI, HTTPException, Request, status
from fastapi.responses import JSONResponse

from autosdr import killswitch
from autosdr.config import get_settings
from autosdr.connectors import get_connector
from autosdr.connectors.base import BaseConnector
from autosdr.connectors.file_connector import FileConnector
from autosdr.db import session_scope
from autosdr.llm import get_usage_snapshot
from autosdr.models import Workspace
from autosdr.pipeline import process_incoming_message
from autosdr.scheduler import run_inbound_poller, run_scheduler

logger = logging.getLogger(__name__)


def _resolve_workspace_id() -> str | None:
    """POC: there is exactly one workspace. Return its ID."""

    with session_scope() as session:
        workspace = session.query(Workspace).first()
        return workspace.id if workspace else None


def create_app(*, run_scheduler_task: bool = True) -> FastAPI:
    """Build the FastAPI app.

    ``run_scheduler_task=False`` is used by tests that drive the reply
    pipeline directly without needing the scheduler or poller to run.
    """

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # NB: we intentionally do NOT call killswitch.install_signal_handlers()
        # here — uvicorn installs its own SIGINT/SIGTERM handlers, and
        # overriding them would prevent the server from shutting down. Instead
        # we rely on uvicorn driving the lifespan shutdown path below, which
        # flips _hard_stop and wakes the scheduler.
        app.state.connector = get_connector()

        scheduler_task: asyncio.Task | None = None
        poller_task: asyncio.Task | None = None
        flag_watcher: asyncio.Task | None = None
        if run_scheduler_task:
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

    @app.get("/healthz")
    async def healthz() -> dict[str, Any]:
        return {
            "status": "ok",
            "paused": killswitch.is_flag_set(),
            "shutting_down": killswitch.is_shutting_down(),
            "connector": getattr(app.state, "connector", None).__class__.__name__
            if hasattr(app.state, "connector")
            else None,
            "llm_usage": get_usage_snapshot(),
        }

    @app.post("/api/webhooks/sms")
    async def webhook_sms(
        request: Request, background_tasks: BackgroundTasks
    ) -> JSONResponse:
        """Inbound SMS webhook — delegates parsing to the active connector.

        Used by push-based connectors such as SmsGate. TextBee polls instead,
        so this endpoint is inert under ``CONNECTOR=textbee`` (nothing will
        POST to it).
        """

        try:
            payload = await request.json()
        except Exception:
            raise HTTPException(status_code=400, detail="invalid JSON")

        try:
            incoming = app.state.connector.parse_webhook(payload)
        except ValueError as exc:
            # Non-inbound events (sms:delivered, sms:failed, ...) also arrive
            # here — they raise ValueError and we 202 them so the sender stops
            # retrying without polluting logs as hard errors.
            logger.info("ignoring non-inbound webhook: %s", exc)
            return JSONResponse(
                content={"accepted": False, "reason": str(exc)},
                status_code=status.HTTP_202_ACCEPTED,
            )

        workspace_id = _resolve_workspace_id()
        if workspace_id is None:
            return JSONResponse(
                content={"accepted": False, "reason": "no_workspace"},
                status_code=status.HTTP_202_ACCEPTED,
            )

        background_tasks.add_task(
            _process_in_background,
            connector=app.state.connector,
            workspace_id=workspace_id,
            incoming=incoming,
        )
        return JSONResponse(
            content={"accepted": True},
            status_code=status.HTTP_202_ACCEPTED,
        )

    @app.post("/api/webhooks/sim")
    async def webhook_sim(
        request: Request, background_tasks: BackgroundTasks
    ) -> JSONResponse:
        """Dev/testing webhook — accepts ``{"contact_uri": ..., "content": ...}``.

        Always parses via :class:`FileConnector` (simple ``contact_uri``/
        ``content`` shape) regardless of the active connector, so you can
        inject synthetic inbound without matching the real provider's
        payload schema.
        """

        try:
            payload = await request.json()
        except Exception:
            raise HTTPException(status_code=400, detail="invalid JSON")

        parser = FileConnector(outbox_path=get_settings().outbox_path)
        try:
            incoming = parser.parse_webhook(payload)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))

        workspace_id = _resolve_workspace_id()
        if workspace_id is None:
            return JSONResponse(
                content={"accepted": False, "reason": "no_workspace"},
                status_code=status.HTTP_202_ACCEPTED,
            )

        background_tasks.add_task(
            _process_in_background,
            connector=app.state.connector,
            workspace_id=workspace_id,
            incoming=incoming,
        )
        return JSONResponse(
            content={"accepted": True},
            status_code=status.HTTP_202_ACCEPTED,
        )

    return app


async def _process_in_background(
    *, connector: BaseConnector, workspace_id: str, incoming: Any
) -> None:
    """Background task wrapper — swallows exceptions so the task runner stays alive."""

    if killswitch.is_paused():
        logger.info("dropping inbound while paused: %s", incoming.contact_uri)
        return
    try:
        result = await process_incoming_message(
            connector=connector,
            workspace_id=workspace_id,
            incoming=incoming,
        )
        logger.info(
            "inbound processed: action=%s intent=%s thread=%s",
            result.action,
            result.intent,
            result.thread_id,
        )
    except Exception:
        logger.exception("inbound processing failed for %s", incoming.contact_uri)
