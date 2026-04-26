"""SMSGate connector — open-source Android SMS gateway (capcom6/android-sms-gateway).

SMSGate exposes a REST API for sending/receiving SMS. The three deployment
shapes AutoSDR supports look like this:

1. **Device local-server mode** (the most common POC setup): The SMSGate
   Android app exposes its API on port 8080 of the phone *at the root* —
   ``POST http://<phone-lan-ip>:8080/messages``. There is no
   ``/3rdparty/v1`` prefix on this server. The operator pastes whatever
   the Android app's Local Server panel shows (e.g. ``192.168.0.13:8080``).
2. **Local docker private server:** The owner runs
   ``docker run -p 3000:3000 ghcr.io/android-sms-gateway/server:latest``.
   The endpoints live under ``/api/3rdparty/v1`` so the operator pastes
   ``http://localhost:3000/api/3rdparty/v1``.
3. **Cloud server (api.sms-gate.app):** Fully managed. Operator pastes
   ``https://api.sms-gate.app/3rdparty/v1``.

In all three cases the configured URL is the *base* the API is mounted at,
and we POST to ``{base}/messages`` (with a fall-back to ``{base}/message``
because the on-device server has historically supported both).

``_normalize_api_url`` only papers over the obvious typing pitfalls: missing
scheme, trailing slash, surrounding whitespace. It does **not** invent a
path — if the deployment needs ``/3rdparty/v1``, the operator must include
it. We learned this the hard way: silently appending ``/3rdparty/v1`` for
local-server users sent every POST to a non-existent path while the GET-based
test-connection probe falsely reported "reachable" because it didn't
distinguish 404 from 200.

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
from urllib.parse import urlsplit, urlunsplit

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

def _normalize_api_url(raw: str) -> str:
    """Make the SMSGate API URL forgiving of what the operator pastes.

    We only fix the two things people actually get wrong:

    1. Missing scheme — we prepend ``http://`` so ``192.168.0.13:8080``
       (literally what the Android app's Local Server panel shows) is a
       URL ``httpx`` can POST to.
    2. Trailing slashes / surrounding whitespace.

    Crucially we do **not** invent a path. The on-device API is mounted at
    the root (``POST /messages``) but the docker private server and the
    cloud server live under ``/api/3rdparty/v1`` and ``/3rdparty/v1``
    respectively. There is no safe default that works for all three —
    silently appending one path used to send every local-server send to a
    non-existent route while the GET-based probe lied "reachable".
    Operators paste whatever full base URL their deployment exposes and
    we trust it.
    """

    candidate = (raw or "").strip().rstrip("/")
    if not candidate:
        return ""

    if "://" not in candidate:
        candidate = f"http://{candidate}"

    parts = urlsplit(candidate)
    if not parts.netloc:
        # "http:///messages" or similar garbage — leave it for the
        # constructor's validation to surface, rather than inventing a
        # hostname.
        return candidate

    return urlunsplit((parts.scheme, parts.netloc, parts.path, parts.query, parts.fragment))


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
        normalized = _normalize_api_url(api_url)
        if not normalized:
            raise ConnectorError(
                "SMSGATE_API_URL must be set "
                "(e.g. 192.168.0.13:8080 for on-device, "
                "or http://localhost:3000/api/3rdparty/v1 for the docker server)"
            )
        if not username or not password:
            raise ConnectorError(
                "SMSGATE_USERNAME and SMSGATE_PASSWORD must both be set"
            )

        self.api_url = normalized
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

        # POST {base}/messages first; fall back to {base}/message on 404.
        # Both shapes have shipped on the SMSGate Android server at various
        # times and the docker / cloud variants standardise on plural —
        # trying plural first means modern deployments hit the right URL on
        # the first request, while older on-device builds still get a
        # successful retry.
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

        Probes ``GET {api_url}/messages`` first, falling back to
        ``GET {api_url}/message`` on 404 — the on-device server has
        historically supported both. We mirror the same fallback in
        :meth:`send` so the probe and the real call agree on which
        endpoints are valid.

        Failure modes we map to ``(False, ...)``:

        * Network error (connection refused, DNS, timeout).
        * 401 / 403 — credentials rejected.
        * 404 on **both** ``/messages`` and ``/message`` — the configured
          base URL doesn't expose the messages API. This is the case that
          used to silently report "reachable" and let bad config slip
          through.
        * 5xx — server-side fault.

        Anything <400 is treated as success and the detail message echoes
        the URL we hit, so the operator can verify it in the inline test
        result.
        """

        try:
            async with httpx.AsyncClient(timeout=self.http_timeout_s) as client:
                plural_url = f"{self.api_url}/messages"
                response = await client.get(plural_url, headers=self._headers())
                hit_url = plural_url

                if response.status_code == 404:
                    singular_url = f"{self.api_url}/message"
                    response = await client.get(singular_url, headers=self._headers())
                    hit_url = singular_url
        except httpx.HTTPError as exc:
            return False, f"smsgate unreachable at {self.api_url}: {exc}"

        if response.status_code in (401, 403):
            return False, f"smsgate rejected credentials ({response.status_code})"
        if response.status_code == 404:
            return (
                False,
                f"smsgate endpoint not found at {self.api_url} — neither "
                "/messages nor /message exists. Double-check the base URL "
                "(local server: paste the host:port the Android app shows; "
                "docker private server: include /api/3rdparty/v1; "
                "cloud server: include /3rdparty/v1).",
            )
        if response.status_code >= 500:
            return False, f"smsgate server error: {response.status_code}"
        if response.status_code >= 400:
            return False, f"smsgate {response.status_code} at {hit_url}"
        return True, f"smsgate reachable at {hit_url}"
