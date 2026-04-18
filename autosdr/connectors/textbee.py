"""TextBee connector — Android SMS gateway via REST (poll-based).

Why poll instead of webhook? TextBee offers both push (webhook to a public URL)
and pull (GET received-sms with just an API key). For the POC we use polling so
the owner does not have to stand up ngrok/cloudflared. API reference:

* Send:   POST /api/v1/gateway/devices/{device_id}/send-sms
* Receive GET  /api/v1/gateway/devices/{device_id}/get-received-sms

Both authenticate with the ``x-api-key`` header. Inbound dedup is keyed on
``smsId`` (TextBee's message id) so we can safely poll every 15-30 s.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

import httpx

from autosdr import killswitch
from autosdr.connectors.base import (
    BaseConnector,
    ConnectorError,
    IncomingMessage,
    OutgoingMessage,
    SendResult,
)

logger = logging.getLogger(__name__)


def _parse_ts(raw: Any) -> datetime:
    if isinstance(raw, str):
        try:
            # TextBee returns ISO-8601 with 'Z'; fromisoformat understands that
            # on Python 3.11+.
            return datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            return datetime.now(tz=timezone.utc)
    return datetime.now(tz=timezone.utc)


class TextBeeConnector(BaseConnector):
    connector_type = "textbee"

    def __init__(
        self,
        *,
        api_url: str,
        api_key: str,
        device_id: str,
        http_timeout_s: float = 20.0,
        poll_limit: int = 50,
    ) -> None:
        if not api_key:
            raise ConnectorError("TEXTBEE_API_KEY must be set")
        if not device_id:
            raise ConnectorError("TEXTBEE_DEVICE_ID must be set")

        self.api_url = api_url.rstrip("/")
        self.api_key = api_key
        self.device_id = device_id
        self.http_timeout_s = http_timeout_s
        self.poll_limit = max(1, min(100, poll_limit))
        self.consecutive_failures = 0

        # High-water mark of message ids already dispatched. Persisted across
        # polls for the life of the process; we do not persist across restarts
        # because the reply pipeline's ``UnmatchedWebhook`` and ``Message``
        # tables catch most duplicates via ``provider_message_id``.
        self._seen_ids: set[str] = set()
        self._last_poll_at: datetime | None = None

    def _headers(self) -> dict[str, str]:
        return {"x-api-key": self.api_key, "Accept": "application/json"}

    # ----- send ------------------------------------------------------------

    async def send(self, message: OutgoingMessage) -> SendResult:
        killswitch.raise_if_paused()

        url = f"{self.api_url}/api/v1/gateway/devices/{self.device_id}/send-sms"
        payload = {
            "recipients": [message.contact_uri],
            "message": message.content,
        }

        try:
            async with httpx.AsyncClient(timeout=self.http_timeout_s) as client:
                response = await client.post(
                    url, headers={**self._headers(), "Content-Type": "application/json"},
                    json=payload,
                )
        except httpx.HTTPError as exc:
            self.consecutive_failures += 1
            logger.warning("textbee network error: %s", exc)
            return SendResult(success=False, error=f"network_error: {exc}")

        if response.status_code >= 500:
            self.consecutive_failures += 1
            return SendResult(
                success=False,
                error=f"textbee {response.status_code}: {response.text[:200]}",
            )
        if response.status_code >= 400:
            # 4xx is typically a config issue (bad device id, disabled SMS
            # permission) — not recoverable without owner intervention.
            logger.error(
                "textbee %s while sending to %s: %s",
                response.status_code,
                message.contact_uri,
                response.text[:500],
            )
            return SendResult(
                success=False,
                error=f"textbee {response.status_code}: {response.text[:200]}",
            )

        self.consecutive_failures = 0
        try:
            body = response.json()
        except ValueError:
            body = {}
        provider_id = None
        if isinstance(body, dict):
            # Shape varies; dig for any id-ish field.
            data = body.get("data") or body
            if isinstance(data, dict):
                provider_id = (
                    data.get("smsId")
                    or data.get("id")
                    or data.get("messageId")
                )
            elif isinstance(data, list) and data:
                first = data[0] if isinstance(data[0], dict) else {}
                provider_id = first.get("smsId") or first.get("id")
        return SendResult(success=True, provider_message_id=provider_id)

    # ----- receive ---------------------------------------------------------

    def parse_webhook(self, payload: dict[str, Any]) -> IncomingMessage:
        """Accept TextBee webhook payloads if the owner later enables them.

        Webhook payload shape (per textbee.dev/quickstart):
            {"smsId": "...", "sender": "+61...", "message": "hi",
             "receivedAt": "2026-04-18T...", "deviceId": "...",
             "webhookEvent": "MESSAGE_RECEIVED"}
        """

        sender = str(payload.get("sender") or payload.get("from") or "").strip()
        content = str(
            payload.get("message") or payload.get("content") or payload.get("body") or ""
        ).strip()
        if not sender or not content:
            raise ValueError(
                "textbee webhook missing required fields (sender + message/body)"
            )
        return IncomingMessage(
            contact_uri=sender,
            content=content,
            received_at=_parse_ts(payload.get("receivedAt")),
            raw_payload=payload,
            provider_message_id=payload.get("smsId"),
        )

    async def poll_incoming(self) -> list[IncomingMessage]:
        """Fetch messages received since last poll.

        Polling is the POC default — we ask TextBee for the most recent
        received SMS each tick and skip any id we have already dispatched in
        this process. The LLM client + reply pipeline dedup at the message
        layer, so an occasional replay after restart is benign.
        """

        killswitch.raise_if_paused()

        url = (
            f"{self.api_url}/api/v1/gateway/devices/{self.device_id}/get-received-sms"
        )
        params = {"limit": self.poll_limit}

        try:
            async with httpx.AsyncClient(timeout=self.http_timeout_s) as client:
                response = await client.get(url, headers=self._headers(), params=params)
        except httpx.HTTPError as exc:
            self.consecutive_failures += 1
            logger.warning("textbee poll network error: %s", exc)
            return []

        if response.status_code >= 400:
            self.consecutive_failures += 1
            logger.warning(
                "textbee poll %s: %s", response.status_code, response.text[:200]
            )
            return []

        self.consecutive_failures = 0
        try:
            body = response.json()
        except ValueError:
            logger.warning("textbee poll returned non-JSON body")
            return []

        items: list[dict[str, Any]] = []
        if isinstance(body, dict):
            data = body.get("data")
            if isinstance(data, list):
                items = [x for x in data if isinstance(x, dict)]
            elif isinstance(data, dict) and isinstance(data.get("messages"), list):
                items = [x for x in data["messages"] if isinstance(x, dict)]
        elif isinstance(body, list):
            items = [x for x in body if isinstance(x, dict)]

        incoming: list[IncomingMessage] = []
        for item in items:
            msg_id = str(
                item.get("smsId") or item.get("_id") or item.get("id") or ""
            ).strip()
            if msg_id and msg_id in self._seen_ids:
                continue

            sender = str(item.get("sender") or item.get("from") or "").strip()
            content = str(
                item.get("message") or item.get("content") or item.get("body") or ""
            ).strip()
            if not sender or not content:
                continue

            incoming.append(
                IncomingMessage(
                    contact_uri=sender,
                    content=content,
                    received_at=_parse_ts(item.get("receivedAt") or item.get("createdAt")),
                    raw_payload=item,
                    provider_message_id=msg_id or None,
                )
            )
            if msg_id:
                self._seen_ids.add(msg_id)

        self._last_poll_at = datetime.now(tz=timezone.utc)
        if incoming:
            logger.info(
                "textbee polled %d new inbound (%d seen cumulative)",
                len(incoming),
                len(self._seen_ids),
            )
        return incoming

    # ----- validate --------------------------------------------------------

    async def validate_config(self) -> tuple[bool, str]:
        url = (
            f"{self.api_url}/api/v1/gateway/devices/{self.device_id}/get-received-sms"
        )
        try:
            async with httpx.AsyncClient(timeout=self.http_timeout_s) as client:
                response = await client.get(
                    url, headers=self._headers(), params={"limit": 1}
                )
        except httpx.HTTPError as exc:
            return False, f"textbee unreachable: {exc}"

        if response.status_code == 401 or response.status_code == 403:
            return False, f"textbee rejected API key ({response.status_code})"
        if response.status_code == 404:
            return False, f"textbee device {self.device_id} not found (404)"
        if response.status_code >= 400:
            return False, f"textbee returned {response.status_code}: {response.text[:200]}"
        return True, f"textbee reachable; device {self.device_id} accepted"
