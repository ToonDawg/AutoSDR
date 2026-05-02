"""``dashboard_origin`` resolution (ticket 0005, success criterion).

Covers the OQ-Net3 verdict from the ticket: the operator override wins
over the per-row snapshot, which itself wins over the
request ``Host`` header. Same chain regardless of whether the caller
is the SW (``GET /api/push/vapid-public``), the Settings →
Notifications card (``GET /api/push/subscriptions``), or the HITL
fanout (``fanout_hitl_push``).
"""

from __future__ import annotations

from contextlib import contextmanager

from fastapi.testclient import TestClient

from autosdr.api import ALL_ROUTERS
from autosdr.db import session_scope
from autosdr.models import PushSubscription, Workspace
from autosdr.push import _resolve_dashboard_origin as fanout_resolve


def _build_app():
    from fastapi import FastAPI

    from autosdr.api.errors import install_exception_handlers
    from autosdr.db import create_all

    create_all()
    app = FastAPI()
    install_exception_handlers(app)
    for router in ALL_ROUTERS:
        app.include_router(router)
    return app


@contextmanager
def _client():
    app = _build_app()
    with TestClient(app) as client:
        yield client


def _set_override(workspace_id: str, origin: str | None) -> None:
    with session_scope() as session:
        ws = session.get(Workspace, workspace_id)
        assert ws is not None
        settings = dict(ws.settings or {})
        push = dict(settings.get("push") or {})
        if origin is None:
            push.pop("dashboard_origin", None)
        else:
            push["dashboard_origin"] = origin
        settings["push"] = push
        ws.settings = settings


# ---------------------------------------------------------------------------
# /api/push/vapid-public — SW reads this on every subscribe attempt
# ---------------------------------------------------------------------------


def test_vapid_public_returns_request_host_when_no_override(workspace_factory):
    workspace_factory()
    with _client() as client:
        response = client.get(
            "/api/push/vapid-public",
            headers={"host": "autosdr-pc.tail-scale.ts.net:8000"},
        )
    assert response.status_code == 200
    assert (
        response.json()["dashboard_origin"]
        == "http://autosdr-pc.tail-scale.ts.net:8000"
    )


def test_vapid_public_honours_override(workspace_factory):
    workspace_id = workspace_factory()
    _set_override(workspace_id, "http://my-override.test")
    with _client() as client:
        response = client.get(
            "/api/push/vapid-public",
            headers={"host": "autosdr-pc.tail-scale.ts.net:8000"},
        )
    assert response.status_code == 200
    assert response.json()["dashboard_origin"] == "http://my-override.test"


def test_vapid_public_respects_x_forwarded_proto(workspace_factory):
    """A future Cloudflare-Tunnel deployment surfaces an HTTPS origin."""

    workspace_factory()
    with _client() as client:
        response = client.get(
            "/api/push/vapid-public",
            headers={
                "host": "autosdr.example.com",
                "x-forwarded-proto": "https",
                "x-forwarded-host": "autosdr.example.com",
            },
        )
    assert response.json()["dashboard_origin"] == "https://autosdr.example.com"


# ---------------------------------------------------------------------------
# /api/push/subscriptions — what the Settings card reads
# ---------------------------------------------------------------------------


def test_subscriptions_origin_uses_override_when_set(workspace_factory):
    workspace_id = workspace_factory()
    _set_override(workspace_id, "http://override.example.test")
    with _client() as client:
        response = client.get(
            "/api/push/subscriptions",
            headers={"host": "autosdr-pc.tail-scale.ts.net:8000"},
        )
    assert response.status_code == 200
    assert response.json()["dashboard_origin"] == "http://override.example.test"


def test_subscriptions_origin_falls_back_to_request(workspace_factory):
    workspace_factory()
    with _client() as client:
        response = client.get(
            "/api/push/subscriptions",
            headers={"host": "autosdr-pc.tail-scale.ts.net:8000"},
        )
    assert (
        response.json()["dashboard_origin"]
        == "http://autosdr-pc.tail-scale.ts.net:8000"
    )


# ---------------------------------------------------------------------------
# Server-side fanout — uses the row snapshot, not the request
# ---------------------------------------------------------------------------


def test_fanout_resolution_prefers_override_over_row_snapshot(workspace_factory):
    workspace_id = workspace_factory()
    _set_override(workspace_id, "http://override.example.test")
    with session_scope() as session:
        row = PushSubscription(
            workspace_id=workspace_id,
            endpoint="https://push.example.test/abc",
            p256dh="p",
            auth="a",
            dashboard_origin="http://snapshot.example.test:8000",
        )
        session.add(row)
        session.flush()
        ws = session.get(Workspace, workspace_id)
        assert ws is not None
        origin = fanout_resolve(row=row, settings_blob=ws.settings or {})
    assert origin == "http://override.example.test"


def test_fanout_resolution_falls_back_to_row_snapshot(workspace_factory):
    workspace_id = workspace_factory()
    with session_scope() as session:
        row = PushSubscription(
            workspace_id=workspace_id,
            endpoint="https://push.example.test/xyz",
            p256dh="p",
            auth="a",
            dashboard_origin="http://snapshot.example.test:8000",
        )
        session.add(row)
        session.flush()
        ws = session.get(Workspace, workspace_id)
        assert ws is not None
        origin = fanout_resolve(row=row, settings_blob=ws.settings or {})
    assert origin == "http://snapshot.example.test:8000"


def test_fanout_resolution_returns_none_when_nothing_set(workspace_factory):
    workspace_id = workspace_factory()
    with session_scope() as session:
        row = PushSubscription(
            workspace_id=workspace_id,
            endpoint="https://push.example.test/no-origin",
            p256dh="p",
            auth="a",
            dashboard_origin=None,
        )
        session.add(row)
        session.flush()
        ws = session.get(Workspace, workspace_id)
        assert ws is not None
        origin = fanout_resolve(row=row, settings_blob=ws.settings or {})
    assert origin is None
