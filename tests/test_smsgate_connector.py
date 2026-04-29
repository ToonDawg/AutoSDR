"""SmsGate connector — send + webhook parsing, with httpx mocked."""

from __future__ import annotations

import base64
import json
from datetime import datetime, timezone

import pytest
from httpx import AsyncClient, MockTransport, Request, Response

from autosdr.connectors.base import ConnectorError, OutgoingMessage
from autosdr.connectors.smsgate import SmsGateConnector, _normalize_api_url, _parse_ts


def _install_transport(monkeypatch: pytest.MonkeyPatch, handler):
    transport = MockTransport(handler)

    def _factory(*args, **kwargs):
        kwargs.pop("timeout", None)
        return AsyncClient(transport=transport)

    monkeypatch.setattr("autosdr.connectors.smsgate.httpx.AsyncClient", _factory)


def _make_connector() -> SmsGateConnector:
    return SmsGateConnector(
        api_url="http://localhost:3000/api/3rdparty/v1",
        username="ops",
        password="s3cret",
    )


def test_requires_api_url():
    with pytest.raises(ConnectorError):
        SmsGateConnector(api_url="", username="u", password="p")


class TestNormalizeApiUrl:
    """Covers ``_normalize_api_url`` — the forgiveness we layer on top of
    whatever string the operator typed into the settings form.

    The contract is deliberately small: we add an ``http://`` scheme if it
    is missing and strip trailing slashes / surrounding whitespace. We
    intentionally do **not** invent a path because the right path differs
    by deployment shape (root for the Android local server, ``/3rdparty/v1``
    for cloud, ``/api/3rdparty/v1`` for the docker private server) and
    silently picking one used to send every local-server message to a 404.
    """

    def test_bare_host_port_just_gets_scheme(self):
        # This is literally what the phone's Local Server panel shows.
        # The on-device server mounts the API at the root, so no path
        # is added — ``send`` will POST to ``/messages`` directly.
        assert (
            _normalize_api_url("192.168.0.13:8080")
            == "http://192.168.0.13:8080"
        )

    def test_scheme_only_is_left_alone(self):
        assert (
            _normalize_api_url("http://192.168.0.13:8080")
            == "http://192.168.0.13:8080"
        )

    def test_trailing_slash_is_stripped(self):
        assert (
            _normalize_api_url("http://192.168.0.13:8080/")
            == "http://192.168.0.13:8080"
        )

    def test_surrounding_whitespace_is_stripped(self):
        assert (
            _normalize_api_url("  192.168.0.13:8080  ")
            == "http://192.168.0.13:8080"
        )

    def test_explicit_docker_server_path_is_preserved(self):
        # Private-Server / docker mounts under "/api/3rdparty/v1".
        assert (
            _normalize_api_url("http://localhost:3000/api/3rdparty/v1")
            == "http://localhost:3000/api/3rdparty/v1"
        )

    def test_explicit_cloud_path_is_preserved(self):
        assert (
            _normalize_api_url("https://api.sms-gate.app/3rdparty/v1")
            == "https://api.sms-gate.app/3rdparty/v1"
        )

    def test_https_scheme_is_preserved(self):
        assert (
            _normalize_api_url("https://sms.example.com/3rdparty/v1")
            == "https://sms.example.com/3rdparty/v1"
        )

    def test_trailing_slash_on_real_path_is_stripped(self):
        assert (
            _normalize_api_url("http://localhost:3000/api/3rdparty/v1/")
            == "http://localhost:3000/api/3rdparty/v1"
        )

    def test_empty_returns_empty(self):
        # Preserves the "missing config" signal so __init__ can raise.
        assert _normalize_api_url("") == ""
        assert _normalize_api_url("   ") == ""


def test_connector_accepts_bare_host_port():
    """Constructor wires normalization in: paste-from-phone just works."""

    connector = SmsGateConnector(
        api_url="192.168.0.13:8080", username="ops", password="s3cret"
    )
    assert connector.api_url == "http://192.168.0.13:8080"


def test_requires_credentials():
    with pytest.raises(ConnectorError):
        SmsGateConnector(
            api_url="http://localhost:3000/api/3rdparty/v1",
            username="",
            password="p",
        )
    with pytest.raises(ConnectorError):
        SmsGateConnector(
            api_url="http://localhost:3000/api/3rdparty/v1",
            username="u",
            password="",
        )


async def test_send_hits_messages_endpoint_with_basic_auth(monkeypatch):
    seen: dict = {}

    def handler(request: Request) -> Response:
        seen["url"] = str(request.url)
        seen["method"] = request.method
        seen["headers"] = dict(request.headers)
        seen["body"] = request.read().decode("utf-8")
        return Response(201, json={"id": "msg-123", "state": "Pending"})

    _install_transport(monkeypatch, handler)

    connector = _make_connector()
    result = await connector.send(
        OutgoingMessage(contact_uri="+61400000001", content="hello there")
    )

    assert result.success
    assert result.provider_message_id == "msg-123"
    assert seen["method"] == "POST"
    assert seen["url"].endswith("/api/3rdparty/v1/messages")

    expected_token = base64.b64encode(b"ops:s3cret").decode("ascii")
    assert seen["headers"].get("authorization") == f"Basic {expected_token}"

    body = json.loads(seen["body"])
    assert body["phoneNumbers"] == ["+61400000001"]
    assert body["textMessage"]["text"] == "hello there"
    # The body shape matters for SMSGate — textMessage.text, not "message".
    assert '"textMessage"' in seen["body"]


async def test_send_returns_failure_on_401(monkeypatch):
    def handler(request: Request) -> Response:
        return Response(401, text="bad creds")

    _install_transport(monkeypatch, handler)

    result = await _make_connector().send(
        OutgoingMessage(contact_uri="+61400000001", content="hi")
    )
    assert not result.success
    assert "401" in (result.error or "")


async def test_send_handles_5xx(monkeypatch):
    def handler(request: Request) -> Response:
        return Response(502, text="bad gateway")

    _install_transport(monkeypatch, handler)

    connector = _make_connector()
    result = await connector.send(
        OutgoingMessage(contact_uri="+61400000001", content="hi")
    )
    assert not result.success
    assert "502" in (result.error or "")
    assert connector.consecutive_failures == 1


def test_parse_webhook_extracts_core_fields():
    connector = _make_connector()
    incoming = connector.parse_webhook(
        {
            "deviceId": "device-abc",
            "event": "sms:received",
            "id": "evt-1",
            "webhookId": "hook-1",
            "payload": {
                "messageId": "m-42",
                "message": "hey there",
                "sender": "+61400000001",
                "recipient": "+61400000009",
                "simNumber": 1,
                "receivedAt": "2024-06-22T15:46:11+07:00",
            },
        }
    )
    assert incoming.contact_uri == "+61400000001"
    assert incoming.content == "hey there"
    assert incoming.provider_message_id == "m-42"


def test_parse_webhook_rejects_non_received_events():
    connector = _make_connector()
    with pytest.raises(ValueError):
        connector.parse_webhook(
            {
                "event": "sms:delivered",
                "payload": {"message": "x", "sender": "+61400000001"},
            }
        )


def test_parse_webhook_rejects_missing_fields():
    connector = _make_connector()
    with pytest.raises(ValueError):
        connector.parse_webhook(
            {"event": "sms:received", "payload": {"sender": "+61400000001"}}
        )


class TestParseTs:
    """``_parse_ts`` is the on-ramp every inbound timestamp goes through.

    The downstream pipeline persists ``Message.created_at`` straight from
    this value and the frontend renders it via ``date-fns format()`` —
    if it's naive we'd serialise without a tz offset, the React side
    would treat it as UTC, and AEST devices would show inbound replies
    10 hours after the AI sent the opener (the bug that motivated this
    test). Every branch must therefore return a tz-aware UTC datetime.
    """

    def test_z_suffix_returns_utc(self):
        result = _parse_ts("2026-04-28T07:17:00Z")
        assert result.tzinfo is not None
        assert result.utcoffset() == timezone.utc.utcoffset(None)
        assert result == datetime(2026, 4, 28, 7, 17, 0, tzinfo=timezone.utc)

    def test_explicit_offset_is_normalised_to_utc(self):
        result = _parse_ts("2026-04-28T17:17:00+10:00")
        assert result.tzinfo is not None
        assert result == datetime(2026, 4, 28, 7, 17, 0, tzinfo=timezone.utc)

    def test_naive_string_is_treated_as_local_and_returns_utc(self, monkeypatch):
        # Pin the host's local timezone to AEST so the test runs the same
        # in CI and on a developer laptop. We patch ``astimezone`` on
        # naive datetimes by overriding the system tz via ``time.tzset``.
        import os
        import time

        monkeypatch.setenv("TZ", "Australia/Brisbane")
        time.tzset()

        try:
            result = _parse_ts("2026-04-28T07:17:00")
        finally:
            os.environ.pop("TZ", None)
            time.tzset()

        assert result.tzinfo is not None
        # 07:17 Brisbane (UTC+10) → 21:17 UTC the previous day.
        assert result == datetime(2026, 4, 27, 21, 17, 0, tzinfo=timezone.utc)

    def test_unparseable_string_falls_back_to_now_utc(self):
        result = _parse_ts("not a date")
        assert result.tzinfo is not None
        assert result.utcoffset() == timezone.utc.utcoffset(None)

    def test_non_string_input_falls_back_to_now_utc(self):
        result = _parse_ts(None)
        assert result.tzinfo is not None
        assert result.utcoffset() == timezone.utc.utcoffset(None)


async def test_validate_config_reports_unreachable(monkeypatch):
    import httpx

    def handler(request: Request) -> Response:
        raise httpx.ConnectError("refused")

    _install_transport(monkeypatch, handler)
    ok, detail = await _make_connector().validate_config()
    assert not ok
    assert "unreachable" in detail


async def test_validate_config_reports_bad_auth(monkeypatch):
    def handler(request: Request) -> Response:
        return Response(401, text="nope")

    _install_transport(monkeypatch, handler)
    ok, detail = await _make_connector().validate_config()
    assert not ok
    assert "rejected" in detail


async def test_validate_config_success(monkeypatch):
    def handler(request: Request) -> Response:
        return Response(200, json=[])

    _install_transport(monkeypatch, handler)
    ok, detail = await _make_connector().validate_config()
    assert ok
    assert "reachable" in detail


async def test_validate_config_hits_messages_endpoint_at_root(monkeypatch):
    """User pastes bare host:port → we probe ``{base}/messages`` directly.

    No ``/3rdparty/v1`` is invented for them: the on-device server mounts
    the API at the root so this is the URL that actually exists.
    """

    seen: dict = {}

    def handler(request: Request) -> Response:
        seen["url"] = str(request.url)
        return Response(200, json=[])

    _install_transport(monkeypatch, handler)

    connector = SmsGateConnector(
        api_url="192.168.0.13:8080", username="ops", password="s3cret"
    )
    ok, detail = await connector.validate_config()

    assert ok
    assert seen["url"] == "http://192.168.0.13:8080/messages"
    # Detail echoes the URL we hit so the operator can verify it from the
    # inline Test-connection popover.
    assert "http://192.168.0.13:8080/messages" in detail


async def test_validate_config_falls_back_to_singular(monkeypatch):
    """If ``/messages`` 404s, we try ``/message`` — older on-device builds
    only ship the singular endpoint and we mirror that fallback in send.
    """

    seen: list[str] = []

    def handler(request: Request) -> Response:
        seen.append(str(request.url))
        if request.url.path.endswith("/messages"):
            return Response(404, text="not found")
        return Response(200, json=[])

    _install_transport(monkeypatch, handler)

    connector = SmsGateConnector(
        api_url="http://192.168.0.13:8080",
        username="ops",
        password="s3cret",
    )
    ok, detail = await connector.validate_config()

    assert ok
    assert seen == [
        "http://192.168.0.13:8080/messages",
        "http://192.168.0.13:8080/message",
    ]
    assert "/message" in detail


async def test_validate_config_reports_404_on_both_paths(monkeypatch):
    """A base URL that exposes neither endpoint must surface as a clear
    "endpoint not found" — this is the regression that was masked when
    validate treated 404 as "reachable" and let bad URLs pass.
    """

    def handler(request: Request) -> Response:
        return Response(404, text="not found")

    _install_transport(monkeypatch, handler)

    connector = SmsGateConnector(
        api_url="http://192.168.0.13:8080/3rdparty/v1",
        username="ops",
        password="s3cret",
    )
    ok, detail = await connector.validate_config()

    assert not ok
    assert "endpoint not found" in detail
    assert "http://192.168.0.13:8080/3rdparty/v1" in detail


async def test_send_falls_back_to_singular_on_404(monkeypatch):
    """The send path mirrors validate: plural first, singular on 404."""

    posts: list[str] = []

    def handler(request: Request) -> Response:
        posts.append(str(request.url))
        if request.url.path.endswith("/messages"):
            return Response(404, text="not found")
        return Response(202, json={"id": "msg-fallback"})

    _install_transport(monkeypatch, handler)

    connector = SmsGateConnector(
        api_url="http://192.168.0.13:8080",
        username="ops",
        password="s3cret",
    )
    result = await connector.send(
        OutgoingMessage(contact_uri="+61400000001", content="hi")
    )

    assert result.success
    assert result.provider_message_id == "msg-fallback"
    assert posts == [
        "http://192.168.0.13:8080/messages",
        "http://192.168.0.13:8080/message",
    ]


# ---------------------------------------------------------------------------
# poll_incoming — GET /messages/inbox, the polling path the scheduler drives
# every ``inbound_poll_s`` so the operator does not have to register a webhook
# or expose the AutoSDR API to the phone's LAN.
# ---------------------------------------------------------------------------


async def test_poll_incoming_hits_inbox_endpoint_with_basic_auth(monkeypatch):
    """Hits ``{api_url}/messages/inbox`` with Basic auth + ``limit`` set.

    The on-device server, the docker private server, and the cloud server
    all mount the inbox API under the same path relative to the configured
    base — so we just append ``/messages/inbox`` and trust the URL the
    operator pasted the same way ``send`` does.
    """

    seen: dict = {}

    def handler(request: Request) -> Response:
        seen["url"] = str(request.url)
        seen["method"] = request.method
        seen["headers"] = dict(request.headers)
        return Response(
            200,
            json=[
                {
                    "id": "in-1",
                    "phoneNumber": "+61400000001",
                    "message": "hi back",
                    "receivedAt": "2026-04-27T01:00:00Z",
                    "type": "SMS",
                }
            ],
        )

    _install_transport(monkeypatch, handler)

    connector = _make_connector()
    incoming = await connector.poll_incoming()

    assert seen["method"] == "GET"
    assert seen["url"].startswith(
        "http://localhost:3000/api/3rdparty/v1/messages/inbox"
    )
    assert "limit=50" in seen["url"]

    expected_token = base64.b64encode(b"ops:s3cret").decode("ascii")
    assert seen["headers"].get("authorization") == f"Basic {expected_token}"

    assert len(incoming) == 1
    assert incoming[0].contact_uri == "+61400000001"
    assert incoming[0].content == "hi back"
    assert incoming[0].provider_message_id == "in-1"


async def test_poll_incoming_dedups_by_message_id(monkeypatch):
    """Polling the same id twice in a row yields it once.

    The reply pipeline already dedups via ``provider_message_id`` at the
    DB layer, but per-process ``_seen_ids`` keeps us from re-running the
    classification + LLM call burst for an id we already dispatched in
    this process. Mirrors TextBee's behaviour.
    """

    def handler(request: Request) -> Response:
        return Response(
            200,
            json=[
                {
                    "id": "in-42",
                    "phoneNumber": "+61400000007",
                    "message": "yep",
                    "receivedAt": "2026-04-27T01:05:00Z",
                }
            ],
        )

    _install_transport(monkeypatch, handler)

    connector = _make_connector()
    first = await connector.poll_incoming()
    second = await connector.poll_incoming()

    assert [m.provider_message_id for m in first] == ["in-42"]
    assert second == []


async def test_poll_incoming_handles_field_aliases(monkeypatch):
    """The on-device, docker, and cloud builds disagree on field names.

    PR #339 introduced ``IncomingMessageResponse`` with ``sender`` /
    ``contentPreview`` / ``createdAt``; earlier on-device builds and the
    webhook payload use ``phoneNumber`` / ``message`` / ``receivedAt``;
    some older builds expose ``messageId`` instead of ``id``. We accept
    any of these so a SMSGate point release can't silently zero-out
    inbound.
    """

    def handler(request: Request) -> Response:
        return Response(
            200,
            json=[
                {
                    "id": "in-modern",
                    "phoneNumber": "+61400000001",
                    "message": "modern shape",
                    "receivedAt": "2026-04-27T02:00:00Z",
                },
                {
                    "messageId": "in-pr339",
                    "sender": "+61400000002",
                    "contentPreview": "pr339 shape",
                    "createdAt": "2026-04-27T02:00:01Z",
                },
            ],
        )

    _install_transport(monkeypatch, handler)

    incoming = await _make_connector().poll_incoming()

    assert len(incoming) == 2
    by_id = {m.provider_message_id: m for m in incoming}
    assert by_id["in-modern"].contact_uri == "+61400000001"
    assert by_id["in-modern"].content == "modern shape"
    assert by_id["in-pr339"].contact_uri == "+61400000002"
    assert by_id["in-pr339"].content == "pr339 shape"


async def test_poll_incoming_returns_empty_on_4xx(monkeypatch):
    """A 4xx on both fallback paths must not crash the poller —
    return ``[]`` and bump the failure counter so an
    operator-visible degradation can be surfaced later.
    """

    def handler(request: Request) -> Response:
        return Response(404, text="not found")

    _install_transport(monkeypatch, handler)

    connector = _make_connector()
    result = await connector.poll_incoming()

    assert result == []
    assert connector.consecutive_failures == 1


async def test_poll_incoming_falls_back_to_singular_inbox_on_404(monkeypatch):
    """The Android local-server build mounts the inbox at ``/inbox`` —
    NOT ``/messages/inbox`` like the cloud / docker servers. We mirror
    the ``/messages`` → ``/message`` plural-first fallback so the same
    code path serves all three deployment shapes.
    """

    seen: list[str] = []

    def handler(request: Request) -> Response:
        seen.append(str(request.url))
        if "/messages/inbox" in request.url.path:
            return Response(404, text="not found")
        return Response(
            200,
            json=[
                {
                    "id": "in-local",
                    "sender": "+61400000003",
                    "contentPreview": "from /inbox",
                    "createdAt": "2026-04-27T03:00:00+10:00",
                }
            ],
        )

    _install_transport(monkeypatch, handler)

    connector = SmsGateConnector(
        api_url="http://192.168.0.99:8080",
        username="ops",
        password="s3cret",
    )
    incoming = await connector.poll_incoming()

    assert [u.split("?", 1)[0] for u in seen] == [
        "http://192.168.0.99:8080/messages/inbox",
        "http://192.168.0.99:8080/inbox",
    ]
    assert len(incoming) == 1
    assert incoming[0].provider_message_id == "in-local"
    assert incoming[0].contact_uri == "+61400000003"
    assert incoming[0].content == "from /inbox"
    assert connector.consecutive_failures == 0
