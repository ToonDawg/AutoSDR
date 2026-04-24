"""SMSGate connector — open-source Android SMS gateway (capcom6/android-sms-gateway).

SMSGate exposes a REST API for sending/receiving SMS via the Android phone.
The two supported deployment shapes for AutoSDR are:

1. **Local docker server (recommended for POC):** The owner runs
   ``docker run -p 3000:3000 ghcr.io/android-sms-gateway/server:latest`` on the
   same machine as AutoSDR. The Android app registers with the docker server.
   AutoSDR calls ``http://localhost:3000/api/3rdparty/v1/messages``.
2. **Device local-server mode:** The SMSGate Android app exposes an HTTP API
   on port 8080 on the phone itself. AutoSDR calls
   ``http://<phone-lan-ip>:8080/3rdparty/v1/messages``.

Either way, ``SMSGATE_API_URL`` should be the full base URL including the
``/3rdparty/v1`` path segment.

Inbound replies are pushed to us as ``sms:received`` webhook POSTs (the Android
device delivers them directly, so your LAN IP is enough — no internet tunnel
is needed as long as the phone and host are on the same network). The POC
exposes ``POST /api/webhooks/sms`` for this purpose.

API reference: https://docs.sms-gate.app/integration/api
"""

from __future__ import annotations

import base64
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
            return datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            return datetime.now(tz=timezone.utc)
    return datetime.now(tz=timezone.utc)


class SmsGateConnector(BaseConnector):
    """Webhook-based Android SMS gateway (SMSGate / capcom6)."""

    connector_type = "smsgate"

    def __init__(
        self,
        *,
        api_url: str,
        username: str,
        password: str,
        http_timeout_s: float = 20.0,
    ) -> None:
        if not api_url:
            raise ConnectorError(
                "SMSGATE_API_URL must be set "
                "(e.g. http://localhost:3000/api/3rdparty/v1)"
            )
        if not username or not password:
            raise ConnectorError(
                "SMSGATE_USERNAME and SMSGATE_PASSWORD must both be set"
            )

        self.api_url = api_url.rstrip("/")
        self.username = username
        self.password = password
        self.http_timeout_s = http_timeout_s
        self.consecutive_failures = 0

    def _auth_header(self) -> dict[str, str]:
        token = base64.b64encode(
            f"{self.username}:{self.password}".encode("utf-8")
        ).decode("ascii")
        return {"Authorization": f"Basic {token}"}

    def _headers(self) -> dict[str, str]:
        return {**self._auth_header(), "Accept": "application/json"}

    # ----- send ------------------------------------------------------------

    async def send(self, message: OutgoingMessage) -> SendResult:
        killswitch.raise_if_paused()

        # Try plural first (Private Server default)
        url = f"{self.api_url}/messages"
        payload = {
            "phoneNumbers": [message.contact_uri],
            "textMessage": {"text": message.content},
        }

        try:
            async with httpx.AsyncClient(timeout=self.http_timeout_s) as client:
                response = await client.post(
                    url,
                    headers={**self._headers(), "Content-Type": "application/json"},
                    json=payload,
                )
                
                # If 404, retry with singular (Local Device mode)
                if response.status_code == 404:
                    url_singular = f"{self.api_url}/message"
                    response = await client.post(
                        url_singular,
                        headers={**self._headers(), "Content-Type": "application/json"},
                        json=payload,
                    )
        except httpx.HTTPError as exc:
            self.consecutive_failures += 1
            logger.warning("smsgate network error: %s", exc)
            return SendResult(success=False, error=f"network_error: {exc}")

        if response.status_code >= 500:
            self.consecutive_failures += 1
            return SendResult(
                success=False,
                error=f"smsgate {response.status_code}: {response.text[:200]}",
            )
        if response.status_code >= 400:
            # 4xx is typically a config issue (bad credentials, phone offline) —
            # not recoverable without owner intervention.
            logger.error(
                "smsgate %s while sending to %s: %s",
                response.status_code,
                message.contact_uri,
                response.text[:500],
            )
            return SendResult(
                success=False,
                error=f"smsgate {response.status_code}: {response.text[:200]}",
            )

        self.consecutive_failures = 0
        try:
            body = response.json()
        except ValueError:
            body = {}
        provider_id: str | None = None
        if isinstance(body, dict):
            raw_id = body.get("id") or body.get("messageId")
            if raw_id is not None:
                provider_id = str(raw_id)
        return SendResult(success=True, provider_message_id=provider_id)

    # ----- receive ---------------------------------------------------------

    def parse_webhook(self, payload: dict[str, Any]) -> IncomingMessage:
        """Parse an ``sms:received`` webhook POST into :class:`IncomingMessage`.

        Example payload (from the SMSGate docs)::

            {"deviceId": "...", "event": "sms:received",
             "id": "Ey6...", "webhookId": "...",
             "payload": {"messageId": "abc", "message": "hi",
                         "sender": "+61400000001", "recipient": "+61...",
                         "simNumber": 1, "receivedAt": "2024-06-22T15:46:11+07:00"}}

        Non-``sms:received`` events (``sms:delivered``, ``sms:failed``, ...)
        raise ``ValueError`` so the webhook handler can 400 them cleanly.
        """

        event = str(payload.get("event") or payload.get("webhookEvent") or "").strip()
        inner_raw = payload.get("payload")
        inner: dict[str, Any] = (
            inner_raw if isinstance(inner_raw, dict) else payload
        )

        if event and event != "sms:received":
            raise ValueError(
                f"smsgate webhook event {event!r} is not sms:received"
            )

        sender = str(inner.get("sender") or inner.get("from") or "").strip()
        content = str(
            inner.get("message") or inner.get("content") or inner.get("body") or ""
        ).strip()
        if not sender or not content:
            raise ValueError(
                "smsgate webhook missing required fields (sender + message)"
            )

        msg_id = inner.get("messageId") or inner.get("id")
        return IncomingMessage(
            contact_uri=sender,
            content=content,
            received_at=_parse_ts(inner.get("receivedAt")),
            raw_payload=payload,
            provider_message_id=str(msg_id) if msg_id else None,
        )

    # ----- validate --------------------------------------------------------

    async def validate_config(self) -> tuple[bool, str]:
        """Check we can reach the SMSGate server and the creds are accepted.

        We probe ``GET {api_url}/messages`` because every SMSGate deployment
        supports it (the server's list-messages endpoint). 4xx that is not
        401/403 still counts as "reachable + auth OK" — the actual send is the
        definitive test.
        """

        url = f"{self.api_url}/messages"
        try:
            async with httpx.AsyncClient(timeout=self.http_timeout_s) as client:
                response = await client.get(url, headers=self._headers())
        except httpx.HTTPError as exc:
            return False, f"smsgate unreachable at {self.api_url}: {exc}"

        if response.status_code in (401, 403):
            return False, f"smsgate rejected credentials ({response.status_code})"
        if response.status_code >= 500:
            return False, f"smsgate server error: {response.status_code}"
        return True, f"smsgate reachable at {self.api_url}"
