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

Inbound replies arrive via two paths, either of which can be used in
isolation:

* **Polling** (``poll_incoming``) — the scheduler asks SMSGate for the
  current inbox every ``inbound_poll_s`` seconds via
  ``GET {api_url}/messages/inbox``. This is the default for the POC because
  it works with zero network plumbing on the host: no public URL, no
  ``--host 0.0.0.0`` binding, no firewall hole, no manual webhook
  registration. The ``/messages/inbox`` endpoint requires SMSGate
  **v1.60.0+** (capcom6/android-sms-gateway PR #339); older builds will
  404 and the poller will log it each tick.
* **Webhook** (``parse_webhook``) — the Android device POSTs
  ``sms:received`` events to ``POST /api/webhooks/sms``. Lower latency
  than polling but requires the operator to register the webhook with the
  device themselves (``POST {api_url}/webhooks``) and to ensure the
  AutoSDR API is reachable from the phone's LAN IP. AutoSDR does not
  currently auto-register the webhook.

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
    """Parse SMSGate's ``receivedAt`` to a timezone-aware UTC datetime.

    Modern builds return ISO-8601 with an explicit offset (``...Z`` or
    ``...+10:00``); older / mis-configured on-device builds occasionally
    drop the offset, leaving a naive ISO string. Treating that naive
    string as UTC would silently shift the lead's reply by the device's
    UTC offset — on an AEST phone that's a 10-hour drift, which is what
    surfaced as "lead replied 10 hours after the AI sent the opener" in
    the inbox. We normalise naive timestamps as host-local (the SMSGate
    server runs on the same LAN as AutoSDR in practice) and convert to
    UTC so downstream storage and the frontend see a single canonical
    representation.
    """

    if isinstance(raw, str):
        try:
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            return datetime.now(tz=timezone.utc)
        if parsed.tzinfo is None:
            # ``astimezone(utc)`` on a naive datetime treats it as the
            # process's local timezone (Python ≥3.6) — exactly what we
            # want for the on-device SMSGate case.
            return parsed.astimezone(timezone.utc)
        return parsed.astimezone(timezone.utc)
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
        poll_limit: int = 50,
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
        self.poll_limit = max(1, min(100, poll_limit))
        self.consecutive_failures = 0

        # Per-process high-water mark of inbox message ids already
        # dispatched. We do not persist this across restarts because the
        # reply pipeline now does its own idempotency check against the
        # ``message.provider_message_id`` column — see
        # ``_resolve_and_capture_inbound`` in :mod:`autosdr.pipeline.reply`.
        # This in-memory set therefore exists purely to skip the network
        # call to ``process_incoming_message`` (and the DB SELECT it
        # performs) when we already know we've ingested the id in the
        # current process. The DB is the source of truth; this is the
        # cache.
        self._seen_ids: set[str] = set()

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

    async def poll_incoming(self) -> list[IncomingMessage]:
        """Fetch the device inbox and return any messages we haven't seen.

        Tries ``GET {api_url}/messages/inbox`` first and falls back to
        ``GET {api_url}/inbox`` on 404. The two paths cover the three
        deployment shapes we support:

        * Cloud server + docker private server expose the inbox at
          ``/messages/inbox`` (matches the published OpenAPI spec).
        * The on-device Android local server (capcom6 v1.60.0+) mounts
          it at ``/inbox`` instead — same data, shorter path, mirrors
          the way it mounts ``/messages`` at root with no
          ``/3rdparty/v1`` prefix. This is the path operators hit when
          they paste their phone's host:port into Settings.

        Both paths predate nothing earlier than v1.60.0
        (capcom6/android-sms-gateway PR #339); on older builds both
        404 and we log + return ``[]`` so the poller stays alive
        without crashing the scheduler.

        Field aliasing mirrors :meth:`parse_webhook`. The on-device
        local-server build, the docker private server, and the cloud
        server have all shipped slightly different field names for the
        inbox DTO at various points (``sender``/``phoneNumber``,
        ``message``/``contentPreview``, ``receivedAt``/``createdAt``);
        we accept any of them so an SMSGate point-release does not
        silently break inbound. Dedup uses :attr:`_seen_ids` so the
        scheduler can poll on every ``inbound_poll_s`` tick without
        re-dispatching the same SMS.
        """

        killswitch.raise_if_paused()

        params = {"limit": self.poll_limit}
        plural_url = f"{self.api_url}/messages/inbox"
        singular_url = f"{self.api_url}/inbox"

        try:
            async with httpx.AsyncClient(timeout=self.http_timeout_s) as client:
                response = await client.get(
                    plural_url, headers=self._headers(), params=params
                )
                hit_url = plural_url
                if response.status_code == 404:
                    response = await client.get(
                        singular_url, headers=self._headers(), params=params
                    )
                    hit_url = singular_url
        except httpx.HTTPError as exc:
            self.consecutive_failures += 1
            logger.warning("smsgate poll network error: %s", exc)
            return []

        if response.status_code >= 400:
            self.consecutive_failures += 1
            logger.warning(
                "smsgate poll %s at %s: %s",
                response.status_code,
                hit_url,
                response.text[:200],
            )
            return []

        self.consecutive_failures = 0
        try:
            body = response.json()
        except ValueError:
            logger.warning("smsgate poll returned non-JSON body")
            return []

        items: list[dict[str, Any]] = []
        if isinstance(body, list):
            items = [x for x in body if isinstance(x, dict)]
        elif isinstance(body, dict):
            data = body.get("data") or body.get("messages")
            if isinstance(data, list):
                items = [x for x in data if isinstance(x, dict)]

        incoming: list[IncomingMessage] = []
        for item in items:
            msg_id = str(
                item.get("id")
                or item.get("messageId")
                or item.get("smsId")
                or ""
            ).strip()
            if msg_id and msg_id in self._seen_ids:
                continue

            sender = str(
                item.get("phoneNumber")
                or item.get("sender")
                or item.get("from")
                or ""
            ).strip()
            content = str(
                item.get("message")
                or item.get("content")
                or item.get("contentPreview")
                or item.get("body")
                or ""
            ).strip()
            if not sender or not content:
                continue

            incoming.append(
                IncomingMessage(
                    contact_uri=sender,
                    content=content,
                    received_at=_parse_ts(
                        item.get("receivedAt") or item.get("createdAt")
                    ),
                    raw_payload=item,
                    provider_message_id=msg_id or None,
                )
            )
            if msg_id:
                self._seen_ids.add(msg_id)

        if incoming:
            logger.info(
                "smsgate polled %d new inbound (%d seen cumulative)",
                len(incoming),
                len(self._seen_ids),
            )
        return incoming

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
