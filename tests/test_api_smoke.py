"""Full FastAPI app smoke tests.

Most route tests drive handler functions directly. These checks keep the
``create_app(run_scheduler_task=False)`` wiring honest without starting the
scheduler or inbound poller.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from autosdr.webhook import create_app


def _client() -> TestClient:
    return TestClient(create_app(run_scheduler_task=False), raise_server_exceptions=False)


def test_app_reports_setup_required_without_workspace(fresh_db) -> None:
    with _client() as client:
        health = client.get("/healthz")
        assert health.status_code == 200
        assert health.json()["connector"] is None

        status = client.get("/api/status")
        assert status.status_code == 200
        assert status.json()["setup_required"] is True
        assert status.json()["scheduler"] == {"tick_s": 60, "poll_s": 20}

        protected = client.get("/api/leads")
        assert protected.status_code == 409
        assert protected.json() == {"setup_required": True}


def test_app_boots_with_workspace_and_file_connector(
    fresh_db, workspace_factory
) -> None:
    workspace_factory(settings_overrides={"scheduler_tick_s": 7, "inbound_poll_s": 3})

    with _client() as client:
        health = client.get("/healthz")
        assert health.status_code == 200
        assert health.json()["connector"] == "FileConnector"

        status = client.get("/api/status")
        assert status.status_code == 200
        body = status.json()
        assert body["setup_required"] is False
        assert body["active_connector"] == "file"
        assert body["scheduler"] == {"tick_s": 7, "poll_s": 3}
        # Cost is 0.0 on a fresh boot; the field exists and is a float.
        assert body["llm_usage"]["estimated_cost_today_usd"] == 0.0
        # Ticket 0009: paused-inbound queue is empty on a fresh boot,
        # but the field is still present so the frontend can render
        # the badge unconditionally.
        assert body["paused_inbound"] == {
            "pending_count": 0,
            "oldest_pending_at": None,
        }


def test_status_estimated_cost_reflects_in_memory_counter(
    fresh_db, workspace_factory
) -> None:
    """One real token-bearing call → status surfaces non-zero cost.

    Verifies the wiring from ``_record_usage`` → ``get_usage_snapshot()``
    → ``LlmUsage.estimated_cost_today_usd`` end-to-end without going
    through LiteLLM.
    """

    from autosdr.llm.client import _record_usage, reset_usage

    workspace_factory()
    reset_usage()
    try:
        _record_usage("gemini/gemini-2.5-flash-lite", 1_000_000, 1_000_000, 0.50)

        with _client() as client:
            body = client.get("/api/status").json()
            assert body["llm_usage"]["calls_today"] == 1
            assert body["llm_usage"]["tokens_in_today"] == 1_000_000
            assert body["llm_usage"]["tokens_out_today"] == 1_000_000
            assert body["llm_usage"]["estimated_cost_today_usd"] == 0.50
    finally:
        reset_usage()
