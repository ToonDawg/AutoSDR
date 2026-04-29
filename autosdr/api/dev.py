"""Development-only rehearsal endpoints (never for production SMS gateways)."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, status

from autosdr.api.deps import db_session, require_workspace
from autosdr.api.schemas import DevSimInboundIn, DevSimInboundOut
from autosdr.connectors import get_connector

router = APIRouter(prefix="/api/dev", tags=["dev"])


@router.post("/sim-inbound", response_model=DevSimInboundOut)
async def sim_inbound(body: DevSimInboundIn) -> DevSimInboundOut:
    """Drive the reply pipeline with a fake inbound message (file connector only)."""

    from autosdr.pipeline.reply import process_incoming_message

    with db_session() as session:
        workspace = require_workspace(session)
        connector_cfg = (workspace.settings or {}).get("connector") or {}
        if connector_cfg.get("type") != "file":
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail={
                    "error": "sim_inbound_file_only",
                    "message": (
                        "Simulated inbound is allowed only when connector.type is "
                        '"file". Point the Settings connector at the dev file '
                        "connector first."
                    ),
                },
            )

    try:
        connector = get_connector()
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"error": "connector_unavailable", "message": str(exc)},
        )

    incoming = connector.parse_webhook(
        {
            "contact_uri": body.contact_uri,
            "content": body.content,
            "from": body.contact_uri,
            "text": body.content,
        }
    )

    with db_session() as session:
        workspace = require_workspace(session)
        workspace_id = workspace.id

    result = await process_incoming_message(
        connector=connector,
        workspace_id=workspace_id,
        incoming=incoming,
    )
    return DevSimInboundOut(
        action=result.action,
        thread_id=result.thread_id,
        intent=result.intent,
        confidence=result.confidence,
        detail=result.detail,
    )


__all__ = ["router"]
