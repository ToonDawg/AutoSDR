"""TextBee connector — send + poll + webhook parsing, with httpx mocked."""

from __future__ import annotations

import pytest
from httpx import AsyncClient, MockTransport, Request, Response

from autosdr.connectors.base import ConnectorError, OutgoingMessage
from autosdr.connectors.textbee import TextBeeConnector


def _install_transport(monkeypatch: pytest.MonkeyPatch, handler):
    """Redirect every ``httpx.AsyncClient(...)`` to one backed by ``MockTransport``."""

    transport = MockTransport(handler)

    def _factory(*args, **kwargs):
        kwargs.pop("timeout", None)
        return AsyncClient(transport=transport)

    monkeypatch.setattr("autosdr.connectors.textbee.httpx.AsyncClient", _factory)


def _make_connector() -> TextBeeConnector:
    return TextBeeConnector(
        api_url="https://api.textbee.dev",
        api_key="test-key",
        device_id="device-abc",
        poll_limit=10,
    )


def test_requires_api_key():
    with pytest.raises(ConnectorError):
        TextBeeConnector(api_url="https://api.textbee.dev", api_key="", device_id="x")


def test_requires_device_id():
    with pytest.raises(ConnectorError):
        TextBeeConnector(api_url="https://api.textbee.dev", api_key="k", device_id="")


async def test_send_hits_correct_endpoint_with_auth(monkeypatch):
    seen: dict = {}

    def handler(request: Request) -> Response:
        seen["url"] = str(request.url)
        seen["headers"] = dict(request.headers)
        seen["body"] = request.read().decode("utf-8")
        return Response(200, json={"data": {"smsId": "abc123"}})

    _install_transport(monkeypatch, handler)

    connector = _make_connector()
    result = await connector.send(
        OutgoingMessage(contact_uri="+61400000001", content="hello")
    )

    assert result.success
    assert result.provider_message_id == "abc123"
    assert seen["url"].endswith("/api/v1/gateway/devices/device-abc/send-sms")
    assert seen["headers"].get("x-api-key") == "test-key"
    assert "+61400000001" in seen["body"]
    assert "hello" in seen["body"]


async def test_send_returns_failure_on_4xx(monkeypatch):
    def handler(request: Request) -> Response:
        return Response(401, text="invalid api key")

    _install_transport(monkeypatch, handler)

    result = await _make_connector().send(
        OutgoingMessage(contact_uri="+61400000001", content="hi")
    )
    assert not result.success
    assert "401" in (result.error or "")


async def test_poll_incoming_deduplicates_seen_ids(monkeypatch):
    payload = {
        "data": [
            {
                "smsId": "msg-1",
                "sender": "+61400000001",
                "message": "hi",
                "receivedAt": "2026-04-18T00:00:00Z",
            },
            {
                "smsId": "msg-2",
                "sender": "+61400000002",
                "message": "what's up",
                "receivedAt": "2026-04-18T00:01:00Z",
            },
        ]
    }

    def handler(request: Request) -> Response:
        assert request.url.path.endswith(
            "/api/v1/gateway/devices/device-abc/get-received-sms"
        )
        return Response(200, json=payload)

    _install_transport(monkeypatch, handler)

    connector = _make_connector()
    first = await connector.poll_incoming()
    assert [m.provider_message_id for m in first] == ["msg-1", "msg-2"]

    # Second poll with the same response returns nothing — both ids dedupe.
    second = await connector.poll_incoming()
    assert second == []


async def test_poll_incoming_skips_messages_missing_sender_or_body(monkeypatch):
    payload = {
        "data": [
            {"smsId": "bad-1", "sender": "", "message": "no sender"},
            {"smsId": "bad-2", "sender": "+61400000001", "message": ""},
            {"smsId": "good", "sender": "+61400000001", "message": "ok"},
        ]
    }

    def handler(request: Request) -> Response:
        return Response(200, json=payload)

    _install_transport(monkeypatch, handler)

    msgs = await _make_connector().poll_incoming()
    assert [m.provider_message_id for m in msgs] == ["good"]


async def test_poll_incoming_swallows_4xx_and_returns_empty(monkeypatch):
    def handler(request: Request) -> Response:
        return Response(503, text="upstream down")

    _install_transport(monkeypatch, handler)

    connector = _make_connector()
    assert await connector.poll_incoming() == []
    assert connector.consecutive_failures == 1


def test_parse_webhook_extracts_core_fields():
    connector = _make_connector()
    incoming = connector.parse_webhook(
        {
            "smsId": "w-1",
            "sender": "+61400000001",
            "message": "webhook hi",
            "receivedAt": "2026-04-18T12:00:00Z",
            "webhookEvent": "MESSAGE_RECEIVED",
        }
    )
    assert incoming.contact_uri == "+61400000001"
    assert incoming.content == "webhook hi"
    assert incoming.provider_message_id == "w-1"


def test_parse_webhook_rejects_missing_fields():
    connector = _make_connector()
    with pytest.raises(ValueError):
        connector.parse_webhook({"sender": "+61400000001"})


async def test_validate_config_reports_401(monkeypatch):
    def handler(request: Request) -> Response:
        return Response(401, text="nope")

    _install_transport(monkeypatch, handler)
    ok, detail = await _make_connector().validate_config()
    assert not ok
    assert "401" in detail


async def test_validate_config_reports_device_not_found(monkeypatch):
    def handler(request: Request) -> Response:
        return Response(404, text="device not found")

    _install_transport(monkeypatch, handler)
    ok, detail = await _make_connector().validate_config()
    assert not ok
    assert "not found" in detail.lower()


async def test_validate_config_success(monkeypatch):
    def handler(request: Request) -> Response:
        return Response(200, json={"data": []})

    _install_transport(monkeypatch, handler)
    ok, detail = await _make_connector().validate_config()
    assert ok
    assert "device-abc" in detail
