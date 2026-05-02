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

from fastapi import APIRouter, BackgroundTasks, HTTPException, Request, status
from fastapi.responses import JSONResponse

from autosdr import killswitch
from autosdr.api.deps import db_session
from autosdr.config import get_settings
from autosdr.connectors import get_connector
from autosdr.connectors.base import BaseConnector, IncomingMessage
from autosdr.connectors.file_connector import FileConnector
from autosdr.models import PausedInbound, Workspace
from autosdr.pipeline import process_incoming_message

router = APIRouter(prefix="/api/webhooks", tags=["webhooks"])
logger = logging.getLogger(__name__)


def _resolve_workspace_id() -> str | None:
    with db_session() as session:
        workspace = session.query(Workspace).first()
        return workspace.id if workspace else None


def _persist_paused_inbound(
    *,
    workspace_id: str,
    connector_type: str,
    incoming: IncomingMessage,
) -> None:
    """Stash one inbound in the durable replay queue.

    Runs in its own short ``db_session()`` so it never holds the
    writer lock across an ``await`` (ticket 0008's contract). The
    INSERT is small (single row, no joins) and competes only with the
    audit-log writer — sub-millisecond in practice.
    """

    with db_session() as session:
        session.add(
            PausedInbound(
                workspace_id=workspace_id,
                connector_type=connector_type,
                contact_uri=incoming.contact_uri,
                content=incoming.content,
                provider_message_id=incoming.provider_message_id,
                raw_payload=dict(incoming.raw_payload) if incoming.raw_payload else None,
            )
        )


async def _process_in_background(
    *, connector: BaseConnector, workspace_id: str, incoming: IncomingMessage
) -> None:
    """Background task wrapper — swallows exceptions so the task runner stays alive."""

    if killswitch.is_paused():
        # Pre-ticket-0009 we silently dropped here; the operator's "I
        # always see every reply" contract was violated and STOP/UNSUB
        # keywords arriving during pause were lost (compliance bug).
        # Now we persist to ``paused_inbound`` and let the resume path
        # replay through ``process_incoming_message``.
        try:
            _persist_paused_inbound(
                workspace_id=workspace_id,
                connector_type=connector.connector_type,
                incoming=incoming,
            )
        except Exception:
            logger.exception(
                "failed to persist paused inbound for %s", incoming.contact_uri
            )
            return
        logger.info(
            "killswitch on; queued inbound for replay: %s", incoming.contact_uri
        )
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
