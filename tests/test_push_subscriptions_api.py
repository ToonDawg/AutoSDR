"""HTTP-level coverage for ``/api/push/*`` (ticket 0005 unit 3).

Pins:

* ``GET /api/push/vapid-public`` reports either the live keys + the
  resolved deep-link origin, or ``None`` before keys exist.
* ``POST /api/push/subscribe`` upserts on ``endpoint``; re-subscribing
  the same endpoint returns the same row id and refreshes ``last_seen_at``
  + ``last_error``.
* The first subscribe captures the request ``Host`` header on the
  row's ``dashboard_origin`` snapshot.
* The settings ``push.dashboard_origin`` override beats the snapshot.
* ``DELETE /api/push/subscribe`` returns 204 whether the row existed
  or not.
* Subscribing without a workspace yields the standard 409
  setup-required envelope so the frontend can redirect.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from autosdr.db import session_scope
from autosdr.models import PushSubscription, Workspace
from autosdr.push import ensure_vapid_keys
from autosdr.webhook import create_app


def _client() -> TestClient:
    return TestClient(create_app(run_scheduler_task=False), raise_server_exceptions=False)


def _set_dashboard_origin(value: str | None) -> None:
    with session_scope() as session:
        workspace = session.query(Workspace).first()
        assert workspace is not None
        settings = dict(workspace.settings or {})
        push = dict(settings.get("push") or {})
        push["dashboard_origin"] = value
        settings["push"] = push
        workspace.settings = settings


def test_vapid_public_requires_workspace(fresh_db) -> None:
    with _client() as client:
        response = client.get("/api/push/vapid-public")
        assert response.status_code == 409
        assert response.json() == {"setup_required": True}


def test_vapid_public_returns_keys_after_lifespan(
    fresh_db, workspace_factory
) -> None:
    workspace_factory()
    ensure_vapid_keys()

    with _client() as client:
        response = client.get(
            "/api/push/vapid-public",
            headers={"host": "autosdr-pc.tail-scale.ts.net:8000"},
        )
    assert response.status_code == 200
    body = response.json()
    assert body["public_key"]
    assert body["dashboard_origin"] == "http://autosdr-pc.tail-scale.ts.net:8000"


def test_subscribe_upserts_on_endpoint(fresh_db, workspace_factory) -> None:
    workspace_factory()
    ensure_vapid_keys()

    payload = {
        "endpoint": "https://push.example.test/abc",
        "keys": {"p256dh": "p1", "auth": "a1"},
        "user_agent": "iPhone Safari",
    }

    with _client() as client:
        response = client.post(
            "/api/push/subscribe",
            json=payload,
            headers={"host": "autosdr-pc.tail-scale.ts.net:8000"},
        )
    assert response.status_code == 200
    first = response.json()
    assert first["user_agent"] == "iPhone Safari"
    assert first["endpoint_host"] == "push.example.test"

    payload["keys"] = {"p256dh": "p2", "auth": "a2"}
    payload["user_agent"] = "Pixel Chrome"

    with _client() as client:
        response = client.post(
            "/api/push/subscribe",
            json=payload,
            headers={"host": "autosdr-pc.tail-scale.ts.net:8000"},
        )
    assert response.status_code == 200
    second = response.json()
    assert second["id"] == first["id"], "re-subscribing same endpoint must upsert"

    with session_scope() as session:
        row = session.query(PushSubscription).one()
        assert row.p256dh == "p2"
        assert row.auth == "a2"
        assert row.user_agent == "Pixel Chrome"
        assert row.dashboard_origin == "http://autosdr-pc.tail-scale.ts.net:8000"


def test_subscribe_origin_override_wins_over_snapshot(
    fresh_db, workspace_factory
) -> None:
    """The settings override beats the per-row Host snapshot.

    This is the OQ-Net3 contract: an operator who's just set up
    Tailscale and pasted a tailnet hostname into Settings →
    Networking gets that origin in the deep-link from then on, even
    on subscriptions captured against a stale ``Host`` header.
    """

    workspace_factory()
    ensure_vapid_keys()
    _set_dashboard_origin("https://autosdr-pc.tail-scale.ts.net")

    payload = {
        "endpoint": "https://push.example.test/abc",
        "keys": {"p256dh": "p1", "auth": "a1"},
        "user_agent": "iPhone Safari",
    }
    with _client() as client:
        response = client.post(
            "/api/push/subscribe",
            json=payload,
            headers={"host": "127.0.0.1:8000"},
        )
        assert response.status_code == 200

        listing = client.get(
            "/api/push/subscriptions",
            headers={"host": "127.0.0.1:8000"},
        )
    body = listing.json()
    assert body["dashboard_origin"] == "https://autosdr-pc.tail-scale.ts.net"


def test_unsubscribe_is_idempotent(fresh_db, workspace_factory) -> None:
    workspace_factory()
    ensure_vapid_keys()

    payload = {
        "endpoint": "https://push.example.test/abc",
        "keys": {"p256dh": "p1", "auth": "a1"},
        "user_agent": "iPhone Safari",
    }
    with _client() as client:
        client.post("/api/push/subscribe", json=payload)
        first_delete = client.request(
            "DELETE", "/api/push/subscribe", json={"endpoint": payload["endpoint"]}
        )
        second_delete = client.request(
            "DELETE", "/api/push/subscribe", json={"endpoint": payload["endpoint"]}
        )

    assert first_delete.status_code == 204
    assert second_delete.status_code == 204
    with session_scope() as session:
        assert session.query(PushSubscription).count() == 0


def test_subscriptions_list_reports_hitl_toggle(
    fresh_db, workspace_factory
) -> None:
    workspace_factory(
        settings_overrides={
            "push": {
                "vapid_public": None,
                "vapid_private": None,
                "vapid_subject": "mailto:autosdr@localhost",
                "hitl_escalations": False,
                "dashboard_origin": None,
            }
        }
    )
    ensure_vapid_keys()

    with _client() as client:
        response = client.get(
            "/api/push/subscriptions",
            headers={"host": "127.0.0.1:8000"},
        )
    body = response.json()
    assert body["subscriptions"] == []
    assert body["hitl_escalations"] is False


def test_test_endpoint_409_when_vapid_missing(fresh_db, workspace_factory) -> None:
    """If the workspace's VAPID keys are missing (e.g. operator wiped
    them by hand from the DB), the test-fire endpoint fails fast with a
    409 instead of silently sending no-op pushes — the operator needs
    to know push isn't configured yet.

    The lifespan re-creates keys on next boot, so we have to wipe them
    *after* ``create_app`` has run.
    """

    workspace_factory()

    with _client() as client:
        with session_scope() as session:
            ws = session.query(Workspace).first()
            assert ws is not None
            settings = dict(ws.settings or {})
            push = dict(settings.get("push") or {})
            push["vapid_public"] = None
            push["vapid_private"] = None
            settings["push"] = push
            ws.settings = settings

        response = client.post("/api/push/test", json={})
    assert response.status_code == 409


def test_test_endpoint_returns_zeroes_with_no_subscriptions(
    fresh_db, workspace_factory
) -> None:
    """No subscriptions = nothing to do, no error."""

    workspace_factory()
    ensure_vapid_keys()
    with _client() as client:
        response = client.post("/api/push/test", json={})
    assert response.status_code == 200
    assert response.json() == {"sent": 0, "gone": 0, "failed": 0}


def test_test_endpoint_fans_out_to_each_subscription(
    fresh_db, workspace_factory, monkeypatch
) -> None:
    """Wires the API ⇄ pywebpush plumbing end-to-end. ``send_push`` is
    stubbed so the suite never touches the real push gateway."""

    workspace_factory()
    ensure_vapid_keys()

    payload_a = {
        "endpoint": "https://push.example.test/aaa",
        "keys": {"p256dh": "pa", "auth": "aa"},
        "user_agent": "iPhone Safari",
    }
    payload_b = {
        "endpoint": "https://push.example.test/bbb",
        "keys": {"p256dh": "pb", "auth": "ab"},
        "user_agent": "Pixel Chrome",
    }
    with _client() as client:
        client.post("/api/push/subscribe", json=payload_a)
        client.post("/api/push/subscribe", json=payload_b)

    from autosdr.push import PushSendResult

    def fake_send(*, subscription_info, payload, vapid_private, vapid_subject):
        return PushSendResult(ok=True, status_code=201)

    monkeypatch.setattr("autosdr.api.push.send_push", fake_send)

    with _client() as client:
        response = client.post("/api/push/test", json={})
    assert response.status_code == 200
    body = response.json()
    assert body == {"sent": 2, "gone": 0, "failed": 0}


def test_test_endpoint_can_target_one_subscription(
    fresh_db, workspace_factory, monkeypatch
) -> None:
    workspace_factory()
    ensure_vapid_keys()

    payload_a = {
        "endpoint": "https://push.example.test/only-this",
        "keys": {"p256dh": "pa", "auth": "aa"},
    }
    payload_b = {
        "endpoint": "https://push.example.test/not-this",
        "keys": {"p256dh": "pb", "auth": "ab"},
    }
    with _client() as client:
        client.post("/api/push/subscribe", json=payload_a)
        client.post("/api/push/subscribe", json=payload_b)

    from autosdr.push import PushSendResult

    seen: list[str] = []

    def fake_send(*, subscription_info, payload, vapid_private, vapid_subject):
        seen.append(subscription_info["endpoint"])
        return PushSendResult(ok=True, status_code=201)

    monkeypatch.setattr("autosdr.api.push.send_push", fake_send)

    with _client() as client:
        response = client.post(
            "/api/push/test",
            json={"endpoint": payload_a["endpoint"]},
        )
    assert response.status_code == 200
    assert response.json() == {"sent": 1, "gone": 0, "failed": 0}
    assert seen == [payload_a["endpoint"]]
