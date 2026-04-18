"""File-backed connector for local dev and testing.

``send`` appends a JSON line to ``data/outbox.jsonl``. The ``/api/webhooks/sim``
endpoint accepts arbitrary JSON payloads shaped like ``IncomingMessage`` and
drives the reply pipeline end-to-end without needing a phone or a tunnel.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from autosdr import killswitch
from autosdr.connectors.base import (
    BaseConnector,
    IncomingMessage,
    OutgoingMessage,
    SendResult,
)

logger = logging.getLogger(__name__)


class FileConnector(BaseConnector):
    connector_type = "file"

    def __init__(self, outbox_path: Path):
        self.outbox_path = Path(outbox_path)
        self.outbox_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = asyncio.Lock()

    async def send(self, message: OutgoingMessage) -> SendResult:
        killswitch.raise_if_paused()
        record = {
            "id": str(uuid.uuid4()),
            "sent_at": datetime.now(tz=timezone.utc).isoformat(),
            "contact_uri": message.contact_uri,
            "content": message.content,
        }
        async with self._lock:
            with self.outbox_path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(record, ensure_ascii=False) + "\n")
        logger.info("outbox append -> %s (%d chars)", message.contact_uri, len(message.content))
        return SendResult(success=True, provider_message_id=record["id"])

    def parse_webhook(self, payload: dict[str, Any]) -> IncomingMessage:
        contact_uri = str(payload.get("contact_uri") or payload.get("from") or "").strip()
        content = str(payload.get("content") or payload.get("text") or "").strip()
        if not contact_uri or not content:
            raise ValueError("sim webhook requires 'contact_uri' and 'content'")

        received_raw = payload.get("received_at")
        if isinstance(received_raw, str):
            try:
                received_at = datetime.fromisoformat(received_raw)
            except ValueError:
                received_at = datetime.now(tz=timezone.utc)
        else:
            received_at = datetime.now(tz=timezone.utc)

        return IncomingMessage(
            contact_uri=contact_uri,
            content=content,
            received_at=received_at,
            raw_payload=payload,
        )

    async def validate_config(self) -> tuple[bool, str]:
        try:
            self.outbox_path.parent.mkdir(parents=True, exist_ok=True)
            return True, f"FileConnector ready; writing to {self.outbox_path}"
        except OSError as exc:
            return False, f"cannot create outbox directory: {exc}"
