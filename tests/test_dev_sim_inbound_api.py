"""POST /api/dev/sim-inbound."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from autosdr.webhook import create_app


def _client() -> TestClient:
    return TestClient(create_app(run_scheduler_task=False))


def test_sim_inbound_requires_file_connector(fresh_db, workspace_factory) -> None:
    workspace_factory(
        settings_overrides={
            "connector": {
                "type": "textbee",
                "textbee": {
                    "api_url": "https://api.textbee.dev",
                    "api_key": "x",
                    "device_id": "d",
                    "poll_limit": 50,
                },
                "smsgate": {
                    "api_url": "http://localhost:3000/api/3rdparty/v1",
                    "username": None,
                    "password": None,
                },
            }
        },
    )

    with _client() as client:
        r = client.post(
            "/api/dev/sim-inbound",
            json={"contact_uri": "+61401111222", "content": "hey"},
        )
        assert r.status_code == 403
        body = r.json()
        assert body["error"] == "sim_inbound_file_only"


def test_sim_inbound_calls_reply_pipeline_when_file_connector(
    fresh_db,
    workspace_factory,
) -> None:
    workspace_factory()

    fake = SimpleNamespace(
        action="ignored",
        thread_id="thread-xyz",
        intent="positive",
        confidence=0.9,
        detail="unittest",
    )

    with (
        patch(
            "autosdr.pipeline.reply.process_incoming_message",
            new_callable=AsyncMock,
            return_value=fake,
        ) as m,
        _client() as client,
    ):
        r = client.post(
            "/api/dev/sim-inbound",
            json={"contact_uri": "+61400000001", "content": "tell me more"},
        )
        assert r.status_code == 200
        body = r.json()
        assert body["action"] == "ignored"
        assert body["thread_id"] == "thread-xyz"
        assert body["intent"] == "positive"
        m.assert_awaited_once()
