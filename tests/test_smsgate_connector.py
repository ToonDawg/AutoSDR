"""SmsGate connector — send + webhook parsing, with httpx mocked."""

from __future__ import annotations

import base64

import pytest
from httpx import AsyncClient, MockTransport, Request, Response

from autosdr.connectors.base import ConnectorError, OutgoingMessage
from autosdr.connectors.smsgate import SmsGateConnector


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

    assert "+61400000001" in seen["body"]
    assert "hello there" in seen["body"]
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
