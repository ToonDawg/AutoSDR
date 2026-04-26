"""Inbound SMS webhook + dev simulator.

Two endpoints:

* ``POST /api/webhooks/sms`` — push-based connectors (SmsGate) POST real
  provider payloads here. The active connector's ``parse_webhook`` is
  invoked to turn that into an :class:`IncomingMessage`.
* ``POST /api/webhooks/sim`` — dev-only simulator that accepts a simple
  ``{"contact_uri", "content"}`` payload regardless of which connector is
  active. Mirrors ``autosdr sim inbound``.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, BackgroundTasks, HTTPException, Request, status
from fastapi.responses import JSONResponse

from autosdr import killswitch
from autosdr.api.deps import db_session
from autosdr.config import get_settings
from autosdr.connectors import get_connector
from autosdr.connectors.base import BaseConnector
from autosdr.connectors.file_connector import FileConnector
from autosdr.models import Workspace
from autosdr.pipeline import process_incoming_message

router = APIRouter(prefix="/api/webhooks", tags=["webhooks"])
logger = logging.getLogger(__name__)


def _resolve_workspace_id() -> str | None:
    with db_session() as session:
        workspace = session.query(Workspace).first()
        return workspace.id if workspace else None


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


@router.post("/sms")
async def webhook_sms(
    request: Request, background_tasks: BackgroundTasks
) -> JSONResponse:
    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail={"error": "invalid_json"})

    workspace_id = _resolve_workspace_id()
    if workspace_id is None:
        return JSONResponse(
            content={"accepted": False, "reason": "no_workspace"},
            status_code=status.HTTP_202_ACCEPTED,
        )

    connector = get_connector()
    try:
        incoming = connector.parse_webhook(payload)
    except ValueError as exc:
        logger.info("ignoring non-inbound webhook: %s", exc)
        return JSONResponse(
            content={"accepted": False, "reason": str(exc)},
            status_code=status.HTTP_202_ACCEPTED,
        )

    background_tasks.add_task(
        _process_in_background,
        connector=connector,
        workspace_id=workspace_id,
        incoming=incoming,
    )
    return JSONResponse(
        content={"accepted": True}, status_code=status.HTTP_202_ACCEPTED
    )


@router.post("/sim")
async def webhook_sim(
    request: Request, background_tasks: BackgroundTasks
) -> JSONResponse:
    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail={"error": "invalid_json"})

    # Simulator always parses via FileConnector so payload schema is
    # independent of the active real connector.
    parser = FileConnector(outbox_path=get_settings().outbox_path)
    try:
        incoming = parser.parse_webhook(payload)
    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail={"error": "invalid_payload", "message": str(exc)},
        )

    workspace_id = _resolve_workspace_id()
    if workspace_id is None:
        return JSONResponse(
            content={"accepted": False, "reason": "no_workspace"},
            status_code=status.HTTP_202_ACCEPTED,
        )

    background_tasks.add_task(
        _process_in_background,
        connector=get_connector(),
        workspace_id=workspace_id,
        incoming=incoming,
    )
    return JSONResponse(
        content={"accepted": True}, status_code=status.HTTP_202_ACCEPTED
    )


__all__ = ["router"]
