"""Replay paused inbounds through the reply pipeline on resume.

Pre-ticket-0009 the webhook handler dropped inbounds while the
killswitch was on. Post-fix, ``autosdr.api.webhooks._process_in_background``
persists each one to :class:`~autosdr.models.PausedInbound`, and the
``POST /api/status/resume`` endpoint fires :func:`drain_paused_inbounds`
on a fresh ``asyncio.create_task`` so the resume request returns
immediately while the queue drains in the background.

Design rules
------------
- **Serial**, not parallel. ``process_incoming_message`` already
  manages its own per-thread row-lock, but draining serially means
  the LLM API doesn't get burst-fanned-out on resume after a long
  pause. One failed inbound also can't poison the rest of the queue.
- **Phased**, per ticket 0008: this module never holds a
  ``session_scope()`` across the ``await`` of
  ``process_incoming_message``. The reads (queue snapshot) and
  writes (``replayed_at`` stamp) each run in their own short
  ``session_scope``.
- **Connector-mismatch is a skip, not an error.** If the operator
  swapped the active connector while paused (e.g. SMSGate → TextBee),
  a queued row tagged with the old ``connector_type`` is logged and
  left for next time. Clearing requires either swapping back or
  deleting the row by hand.
- **Failures don't stamp ``replayed_at``.** Next resume retries.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from sqlalchemy import select

from autosdr.connectors import get_connector
from autosdr.connectors.base import IncomingMessage
from autosdr.db import session_scope
from autosdr.models import PausedInbound
from autosdr.pipeline.reply import process_incoming_message

logger = logging.getLogger(__name__)


def _snapshot_pending() -> list[dict]:
    """Snapshot every unreplayed row, oldest first.

    Materialises plain dicts (not ORM instances) so the snapshot is
    safe to use after the session closes.
    """

    out: list[dict] = []
    with session_scope() as session:
        rows = session.execute(
            select(PausedInbound)
            .where(PausedInbound.replayed_at.is_(None))
            .order_by(PausedInbound.created_at.asc())
        ).scalars()
        for row in rows:
            out.append(
                {
                    "id": row.id,
                    "workspace_id": row.workspace_id,
                    "connector_type": row.connector_type,
                    "contact_uri": row.contact_uri,
                    "content": row.content,
                    "provider_message_id": row.provider_message_id,
                    "raw_payload": dict(row.raw_payload) if row.raw_payload else {},
                    "created_at": row.created_at,
                }
            )
    return out


def _stamp_replayed(paused_inbound_id: str) -> None:
    """Mark one row as replayed in its own short transaction."""

    with session_scope() as session:
        row = session.get(PausedInbound, paused_inbound_id)
        if row is not None:
            row.replayed_at = datetime.now(tz=timezone.utc)


async def drain_paused_inbounds() -> dict[str, int]:
    """Replay every unreplayed :class:`PausedInbound` row through the reply pipeline.

    Returns a small summary dict (``{"replayed": n, "skipped": n,
    "failed": n}``) for logging / future telemetry. The summary is
    not surfaced to the operator — they watch the count drop on
    ``GET /api/status``.
    """

    pending = _snapshot_pending()
    if not pending:
        return {"replayed": 0, "skipped": 0, "failed": 0}

    try:
        connector = get_connector()
    except Exception:  # pragma: no cover - defensive
        logger.exception("paused-inbound drain: failed to resolve active connector")
        return {"replayed": 0, "skipped": 0, "failed": len(pending)}

    active_connector_type = getattr(connector, "connector_type", None)

    replayed = 0
    skipped = 0
    failed = 0
    for row in pending:
        if (
            active_connector_type is not None
            and row["connector_type"] != active_connector_type
        ):
            logger.warning(
                "paused-inbound %s: connector mismatch "
                "(queued=%s, active=%s) — skipping; will retry on next resume",
                row["id"],
                row["connector_type"],
                active_connector_type,
            )
            skipped += 1
            continue

        incoming = IncomingMessage(
            contact_uri=row["contact_uri"],
            content=row["content"],
            received_at=row["created_at"],
            raw_payload=row["raw_payload"],
            provider_message_id=row["provider_message_id"],
        )
        try:
            await process_incoming_message(
                connector=connector,
                workspace_id=row["workspace_id"],
                incoming=incoming,
            )
        except Exception:
            logger.exception(
                "paused-inbound %s: replay failed; leaving for next resume",
                row["id"],
            )
            failed += 1
            continue

        try:
            _stamp_replayed(row["id"])
        except Exception:
            logger.exception(
                "paused-inbound %s: replay succeeded but stamp failed; "
                "row will be replayed again on next resume",
                row["id"],
            )
            failed += 1
            continue

        replayed += 1

    if replayed or skipped or failed:
        logger.info(
            "paused-inbound drain complete: replayed=%d skipped=%d failed=%d",
            replayed,
            skipped,
            failed,
        )
    return {"replayed": replayed, "skipped": skipped, "failed": failed}


__all__ = ["drain_paused_inbounds"]
