"""HTTP coverage for inbound webhook routes."""

from __future__ import annotations

from types import SimpleNamespace

from fastapi.testclient import TestClient

from autosdr.webhook import create_app


def _client() -> TestClient:
    return TestClient(create_app(run_scheduler_task=False), raise_server_exceptions=False)


def test_sms_webhook_rejects_invalid_json(fresh_db) -> None:
    with _client() as client:
        response = client.post(
            "/api/webhooks/sms",
            content="{",
            headers={"content-type": "application/json"},
        )

    assert response.status_code == 400
    assert response.json() == {"error": "invalid_json"}


def test_sms_webhook_acknowledges_no_workspace(fresh_db) -> None:
    with _client() as client:
        response = client.post(
            "/api/webhooks/sms",
            json={"contact_uri": "+61400000001", "content": "hello"},
        )

    assert response.status_code == 202
    assert response.json() == {"accepted": False, "reason": "no_workspace"}


def test_sms_webhook_ignores_unparseable_payload(fresh_db, workspace_factory) -> None:
    workspace_factory()

    with _client() as client:
        response = client.post(
            "/api/webhooks/sms",
            json={"contact_uri": "+61400000001"},
        )

    assert response.status_code == 202
    body = response.json()
    assert body["accepted"] is False
    assert "content" in body["reason"]


def test_sms_webhook_accepts_and_processes_inbound(
    fresh_db, workspace_factory, monkeypatch
) -> None:
    workspace_id = workspace_factory()
    seen: dict[str, object] = {}

    async def fake_process_incoming_message(*, connector, workspace_id, incoming):
        seen["connector_type"] = connector.connector_type
        seen["workspace_id"] = workspace_id
        seen["contact_uri"] = incoming.contact_uri
        seen["content"] = incoming.content
        return SimpleNamespace(action="escalated_hitl", intent="question", thread_id="t1")

    monkeypatch.setattr(
        "autosdr.api.webhooks.process_incoming_message",
        fake_process_incoming_message,
    )

    with _client() as client:
        response = client.post(
            "/api/webhooks/sms",
            json={"contact_uri": "+61400000001", "content": "tell me more"},
        )

    assert response.status_code == 202
    assert response.json() == {"accepted": True}
    assert seen == {
        "connector_type": "file",
        "workspace_id": workspace_id,
        "contact_uri": "+61400000001",
        "content": "tell me more",
    }
