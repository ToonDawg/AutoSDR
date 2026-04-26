"""POST /api/workspace/connector/test — "Test connection" button.

The endpoint must:

1. Test the currently-cached connector when the body is empty.
2. Build an ephemeral connector from the request body (unsaved form
   state) without touching the saved workspace settings.
3. Never raise — every failure is encoded as ``{ok: false, detail: ...}``
   so the UI can render it inline. Missing creds, network errors, and
   validate-raised exceptions all flow through the same shape.
4. Ignore the saved ``rehearsal.override_to`` — we want to probe the
   real target the operator just typed, not the rehearsal phone they
   set last week.
"""

from __future__ import annotations

import pytest
from httpx import AsyncClient, MockTransport, Request, Response

from autosdr.api.schemas import ConnectorTestRequest
from autosdr.api.workspace import test_connector


async def test_saved_connector_probe_uses_cached_singleton(fresh_db, workspace_factory):
    """Empty body → exercise ``get_connector()`` (the live FileConnector)."""

    workspace_factory()

    result = await test_connector(None)
    assert result.ok is True
    assert result.connector_type == "file"
    assert "fileconnector" in result.detail.lower()


async def test_unsaved_smsgate_probe_builds_ephemeral_connector(
    fresh_db, workspace_factory, monkeypatch
):
    """Body supplies unsaved SMSGate creds — we build a one-shot connector."""

    workspace_factory()

    seen: dict = {}

    def handler(request: Request) -> Response:
        seen["url"] = str(request.url)
        seen["auth"] = request.headers.get("authorization")
        return Response(200, json=[])

    transport = MockTransport(handler)

    def _factory(*args, **kwargs):
        kwargs.pop("timeout", None)
        return AsyncClient(transport=transport)

    monkeypatch.setattr("autosdr.connectors.smsgate.httpx.AsyncClient", _factory)

    payload = ConnectorTestRequest(
        type="smsgate",
        smsgate={
            "api_url": "http://phone.local:8080/3rdparty/v1",
            "username": "ops",
            "password": "s3cret",
        },
    )
    result = await test_connector(payload)

    assert result.ok is True
    assert result.connector_type == "smsgate"
    # Hit the expected endpoint with basic auth header derived from our creds.
    assert seen["url"].endswith("/messages")
    assert seen["auth"] is not None and seen["auth"].startswith("Basic ")


async def test_unsaved_smsgate_probe_reports_bad_credentials_inline(
    fresh_db, workspace_factory, monkeypatch
):
    """401 from the server must come back as ok=false, not an HTTP exception."""

    workspace_factory()

    def handler(request: Request) -> Response:
        return Response(401, json={"error": "bad creds"})

    transport = MockTransport(handler)

    def _factory(*args, **kwargs):
        kwargs.pop("timeout", None)
        return AsyncClient(transport=transport)

    monkeypatch.setattr("autosdr.connectors.smsgate.httpx.AsyncClient", _factory)

    result = await test_connector(
        ConnectorTestRequest(
            type="smsgate",
            smsgate={
                "api_url": "http://phone.local:8080/3rdparty/v1",
                "username": "ops",
                "password": "wrong",
            },
        )
    )

    assert result.ok is False
    assert result.connector_type == "smsgate"
    assert "401" in result.detail


async def test_unsaved_probe_surfaces_connector_error_when_creds_missing(
    fresh_db, workspace_factory
):
    """Missing required fields → friendly ok=false from the constructor raise."""

    workspace_factory()

    result = await test_connector(
        ConnectorTestRequest(
            type="smsgate",
            smsgate={"api_url": ""},
        )
    )
    # SmsGateConnector refuses to build without api_url — this should surface
    # as an inline error, not a 500.
    assert result.ok is False
    assert "SMSGATE_API_URL" in result.detail or "must be set" in result.detail


async def test_unsaved_probe_ignores_saved_rehearsal_override(
    fresh_db, workspace_factory, monkeypatch
):
    """A saved ``override_to`` must NOT wrap the probe.

    The operator presses Test to verify the creds they just typed reach
    the real gateway. If we let the saved override slip through, every
    probe would be redirected to the rehearsal phone and we'd be
    validating against a phantom target.
    """

    workspace_factory(settings_overrides={"rehearsal": {"override_to": "+61400000099"}})

    call_count = {"n": 0}

    def handler(request: Request) -> Response:
        call_count["n"] += 1
        return Response(200, json=[])

    transport = MockTransport(handler)

    def _factory(*args, **kwargs):
        kwargs.pop("timeout", None)
        return AsyncClient(transport=transport)

    monkeypatch.setattr("autosdr.connectors.smsgate.httpx.AsyncClient", _factory)

    result = await test_connector(
        ConnectorTestRequest(
            type="smsgate",
            smsgate={
                "api_url": "http://phone.local:8080/3rdparty/v1",
                "username": "ops",
                "password": "s3cret",
            },
        )
    )

    assert result.ok is True
    # not "smsgate+override" — the override layer must be stripped for the probe
    assert result.connector_type == "smsgate"
    assert call_count["n"] == 1


async def test_saved_connector_probe_runs_even_when_paused(
    fresh_db, workspace_factory
):
    """Operator tests are manual actions — they bypass the pause flag."""

    from autosdr import killswitch

    workspace_factory()
    killswitch.touch_flag()
    try:
        result = await test_connector(None)
    finally:
        killswitch.remove_flag()

    assert result.ok is True
    assert result.connector_type == "file"
