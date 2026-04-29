"""Manual lead opt-out / clear endpoints."""

from __future__ import annotations

from fastapi.testclient import TestClient

from autosdr.models import Lead, LeadStatus, Workspace
from autosdr.webhook import create_app


def _client() -> TestClient:
    return TestClient(create_app(run_scheduler_task=False))


def test_opt_out_then_idempotent_then_clear(fresh_db, workspace_factory) -> None:
    ws_id = workspace_factory()
    with fresh_db() as session:
        ws = session.get(Workspace, ws_id)
        lead = Lead(
            workspace_id=ws.id,
            name="Jane",
            contact_uri="+61401111222",
            contact_type="mobile",
            category="Retail",
            address="Brisbane",
            raw_data={},
            import_order=1,
            source_file="seed",
            status=LeadStatus.NEW,
        )
        session.add(lead)
        session.flush()
        lead_id = lead.id

    with _client() as client:
        r1 = client.post(
            f"/api/leads/{lead_id}/opt-out",
            json={"reason": "manual: pytest"},
        )
        assert r1.status_code == 200
        j1 = r1.json()
        assert j1["do_not_contact_reason"] == "manual: pytest"
        assert j1["do_not_contact_at"] is not None

        r2 = client.post(f"/api/leads/{lead_id}/opt-out", json={"reason": "other"})
        assert r2.status_code == 200
        j2 = r2.json()
        assert j2["do_not_contact_reason"] == "manual: pytest"

        r3 = client.delete(f"/api/leads/{lead_id}/opt-out")
        assert r3.status_code == 200
        assert r3.json()["do_not_contact_at"] is None

        r4 = client.delete(f"/api/leads/{lead_id}/opt-out")
        assert r4.status_code == 200
        assert r4.json()["do_not_contact_at"] is None


def test_opt_out_returns_404_for_unknown_lead(fresh_db, workspace_factory) -> None:
    workspace_factory()
    with _client() as client:
        resp = client.post("/api/leads/does-not-exist/opt-out", json={"reason": "x"})
        assert resp.status_code == 404
