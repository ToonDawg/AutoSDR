"""POST /api/leads/enrich."""

from __future__ import annotations

from fastapi.testclient import TestClient

from autosdr.models import Lead, LeadStatus, Workspace
from autosdr.webhook import create_app


def _client() -> TestClient:
    return TestClient(create_app(run_scheduler_task=False))


def test_enrich_dry_run_lists_candidate(fresh_db, workspace_factory) -> None:
    ws_id = workspace_factory()
    with fresh_db() as session:
        ws = session.get(Workspace, ws_id)
        session.add(
            Lead(
                workspace_id=ws.id,
                name="Has site",
                contact_uri="+61401111233",
                contact_type="mobile",
                category=None,
                address=None,
                website="https://example.com",
                raw_data={},
                import_order=1,
                source_file="seed",
                status=LeadStatus.NEW,
            )
        )

    with _client() as client:
        r = client.post(
            "/api/leads/enrich",
            json={"since_days": 30, "limit": 50, "dry_run": True},
        )
        assert r.status_code == 200
        body = r.json()
        assert body["dry_run"] is True
        assert body["total"] == 1
        assert body["candidates"] is not None
        assert len(body["candidates"]) == 1
        assert body["candidates"][0]["website"] == "https://example.com"


def test_enrich_empty_workspace_returns_zero_candidates(fresh_db, workspace_factory) -> None:
    workspace_factory()
    with _client() as client:
        r = client.post(
            "/api/leads/enrich",
            json={"since_days": 30, "limit": 10, "dry_run": True},
        )
        assert r.status_code == 200
        body = r.json()
        assert body["total"] == 0
        assert body["candidates"] == []
