"""``/api/scans`` router — list, summary, detail, manual run."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi.testclient import TestClient

from autosdr.enrichment import (
    CONNECTOR_NAME,
    CONNECTOR_VERSION,
    ENVELOPE_VERSION,
)
from autosdr.models import (
    Campaign,
    CampaignLead,
    CampaignLeadStatus,
    CampaignStatus,
    Lead,
    LeadStatus,
    Workspace,
)
from autosdr.webhook import create_app


# The Scans API defaults to "leads in at least one campaign" so the
# operator's view stays focused on what we'd actually outreach. Tests
# that don't care about campaign membership pass this flag through to
# include the unassigned leads they create.
ALL = "?include_unassigned=true"


def _client() -> TestClient:
    return TestClient(
        create_app(run_scheduler_task=False), raise_server_exceptions=False
    )


_lead_counter = 0
_campaign_counter = 0


def _resolved_status_for(envelope: dict[str, Any]) -> str:
    meta = envelope.get("_meta") or {}
    return meta.get("status") or "ok"


def _resolved_fetched_at(envelope: dict[str, Any]) -> datetime | None:
    raw = (envelope.get("_meta") or {}).get("fetched_at")
    if isinstance(raw, str) and raw:
        try:
            ts = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            return None
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        return ts
    return None


def _make_lead(
    session,
    *,
    workspace_id: str,
    name: str = "Test",
    website: str | None = "https://example.com.au",
    enrichment: dict[str, Any] | None = None,
    do_not_contact_at: datetime | None = None,
    contact_uri: str | None = None,
) -> Lead:
    """Build a lead, mirroring envelope ``_meta.status`` /
    ``_meta.fetched_at`` to the denormalised columns the API queries
    against."""

    global _lead_counter
    _lead_counter += 1
    if contact_uri is None:
        contact_uri = f"+614{_lead_counter:09d}"

    raw: dict[str, Any] = {}
    if enrichment is not None:
        raw["enrichment"] = enrichment

    lead = Lead(
        workspace_id=workspace_id,
        name=name,
        contact_uri=contact_uri,
        contact_type="mobile",
        category="Plumber",
        website=website,
        raw_data=raw,
        import_order=_lead_counter,
        source_file="test.csv",
        status=LeadStatus.NEW,
        do_not_contact_at=do_not_contact_at,
        enrichment_status=_resolved_status_for(enrichment) if enrichment else None,
        enrichment_fetched_at=(
            _resolved_fetched_at(enrichment) if enrichment else None
        ),
    )
    session.add(lead)
    session.flush()
    return lead


def _assign_to_campaign(session, *, workspace_id: str, lead: Lead) -> CampaignLead:
    """Create a campaign + assignment so the lead surfaces in the Scans
    page's default (campaign-only) scope."""

    global _campaign_counter
    _campaign_counter += 1
    campaign = Campaign(
        workspace_id=workspace_id,
        name=f"Test campaign {_campaign_counter}",
        goal="Make money.",
        outreach_per_day=10,
        status=CampaignStatus.ACTIVE,
    )
    session.add(campaign)
    session.flush()
    assignment = CampaignLead(
        campaign_id=campaign.id,
        lead_id=lead.id,
        queue_position=_campaign_counter,
        status=CampaignLeadStatus.QUEUED,
    )
    session.add(assignment)
    session.flush()
    return assignment


def _envelope(
    *,
    status: str = "ok",
    fetched_at: datetime | None = None,
    cms: str = "wordpress",
    sitemap_count: int = 12,
    latency_ms: int = 410,
    http_status: int = 200,
) -> dict[str, Any]:
    if fetched_at is None:
        fetched_at = datetime.now(tz=timezone.utc) - timedelta(hours=1)
    return {
        "_meta": {
            "version": ENVELOPE_VERSION,
            "connector": CONNECTOR_NAME,
            "connector_version": CONNECTOR_VERSION,
            "status": status,
            "fetched_at": fetched_at.isoformat(),
            "user_agent": "AutoSDR/test",
            "robots_respected": True,
            "latency_ms": latency_ms,
            "http_status": http_status,
            "final_url": "https://example.com.au/",
        },
        "signals": {
            "title": "Demo Plumbing",
            "h1": "Brisbane plumbers",
            "cms": cms,
            "sitemap_count": sitemap_count,
        },
    }


# ---------------------------------------------------------------------------
# GET /api/scans
# ---------------------------------------------------------------------------


def test_list_scans_returns_rows_and_counts(fresh_db, workspace_factory):
    """Every lead surfaces — those without an envelope show up as
    ``never_scanned``, the rest carry their stamped status."""

    ws_id = workspace_factory()
    with fresh_db() as session:
        _make_lead(
            session,
            workspace_id=ws_id,
            name="Fresh OK",
            enrichment=_envelope(status="ok"),
        )
        _make_lead(
            session,
            workspace_id=ws_id,
            name="Timed out",
            website="https://t.example",
            enrichment=_envelope(status="timeout", cms=""),
        )
        _make_lead(
            session,
            workspace_id=ws_id,
            name="Never scanned",
            website="https://n.example",
        )

    with _client() as client:
        response = client.get(f"/api/scans{ALL}")
        assert response.status_code == 200
        body = response.json()

    assert body["total"] == 3
    assert {s["status"] for s in body["scans"]} == {"ok", "timeout", "never_scanned"}
    counts = body["counts_by_status"]
    assert counts["ok"] == 1
    assert counts["timeout"] == 1
    assert counts["never_scanned"] == 1
    assert counts["all"] == 3


def test_list_scans_filters_by_status(fresh_db, workspace_factory):
    ws_id = workspace_factory()
    with fresh_db() as session:
        _make_lead(
            session,
            workspace_id=ws_id,
            name="Good",
            enrichment=_envelope(status="ok"),
        )
        _make_lead(
            session,
            workspace_id=ws_id,
            name="Blocked",
            website="https://b.example",
            enrichment=_envelope(status="blocked"),
        )

    with _client() as client:
        body = client.get(f"/api/scans{ALL}&status_filter=blocked").json()

    assert body["total"] == 1
    assert body["scans"][0]["status"] == "blocked"
    assert body["scans"][0]["lead_name"] == "Blocked"


def test_list_scans_filters_by_query(fresh_db, workspace_factory):
    ws_id = workspace_factory()
    with fresh_db() as session:
        _make_lead(session, workspace_id=ws_id, name="Acme Plumbing")
        _make_lead(
            session,
            workspace_id=ws_id,
            name="Beta Electrical",
            website="https://beta.example",
        )

    with _client() as client:
        body = client.get(f"/api/scans{ALL}&q=acme").json()

    assert body["total"] == 1
    assert body["scans"][0]["lead_name"] == "Acme Plumbing"


def test_list_scans_paginates(fresh_db, workspace_factory):
    ws_id = workspace_factory()
    with fresh_db() as session:
        for i in range(5):
            _make_lead(
                session,
                workspace_id=ws_id,
                name=f"Lead {i}",
                website=f"https://x{i}.example",
            )

    with _client() as client:
        body = client.get(f"/api/scans{ALL}&limit=2&offset=0").json()
        assert len(body["scans"]) == 2
        assert body["total"] == 5
        body2 = client.get(f"/api/scans{ALL}&limit=2&offset=2").json()
        assert len(body2["scans"]) == 2

    seen = {s["lead_id"] for s in body["scans"]} | {s["lead_id"] for s in body2["scans"]}
    assert len(seen) == 4


def test_list_scans_row_shape_is_lean(fresh_db, workspace_factory):
    """The list payload exposes only the columns the table renders.

    Audit-detail fields (``http_status`` / ``final_url`` /
    ``connector`` / ``connector_version``) live on the per-lead
    ``ScanDetail`` route, not on every list row, so the response
    body stays small and the row query never has to round-trip the
    full ``raw_data`` blob.
    """

    ws_id = workspace_factory()
    with fresh_db() as session:
        _make_lead(
            session,
            workspace_id=ws_id,
            name="Lean row",
            enrichment=_envelope(status="ok", cms="wordpress", sitemap_count=42),
        )

    with _client() as client:
        body = client.get(f"/api/scans{ALL}").json()

    assert body["total"] == 1
    row = body["scans"][0]
    expected_keys = {
        "lead_id",
        "lead_name",
        "website",
        "status",
        "fetched_at",
        "latency_ms",
        "cms",
        "sitemap_count",
    }
    assert set(row.keys()) == expected_keys
    assert row["cms"] == "wordpress"
    assert row["sitemap_count"] == 42
    assert "raw_data" not in row
    assert "enrichment" not in row


def test_list_scans_default_scope_excludes_unassigned_leads(
    fresh_db, workspace_factory
):
    """Without ``include_unassigned`` only campaign-assigned leads
    surface — that's the value of the new toggle."""

    ws_id = workspace_factory()
    with fresh_db() as session:
        assigned = _make_lead(
            session,
            workspace_id=ws_id,
            name="Assigned",
            enrichment=_envelope(status="ok"),
        )
        _make_lead(
            session,
            workspace_id=ws_id,
            name="Unassigned",
            website="https://u.example",
            enrichment=_envelope(status="ok"),
        )
        _assign_to_campaign(session, workspace_id=ws_id, lead=assigned)

    with _client() as client:
        default_body = client.get("/api/scans").json()
        wide_body = client.get(f"/api/scans{ALL}").json()

    assert {s["lead_name"] for s in default_body["scans"]} == {"Assigned"}
    assert {s["lead_name"] for s in wide_body["scans"]} == {"Assigned", "Unassigned"}


# ---------------------------------------------------------------------------
# GET /api/scans/summary
# ---------------------------------------------------------------------------


def test_summary_counts_each_bucket(fresh_db, workspace_factory):
    ws_id = workspace_factory()
    with fresh_db() as session:
        _make_lead(session, workspace_id=ws_id, name="OK", enrichment=_envelope(status="ok"))
        _make_lead(
            session,
            workspace_id=ws_id,
            name="Timed",
            website="https://t.example",
            enrichment=_envelope(status="timeout"),
        )
        _make_lead(session, workspace_id=ws_id, name="Never", website="https://n.example")

    with _client() as client:
        body = client.get(f"/api/scans/summary{ALL}").json()

    assert body["total_leads"] == 3
    assert body["ok"] == 1
    assert body["timeout"] == 1
    assert body["never_scanned"] == 1
    assert body["runner_running"] is False
    assert body["runner_done"] == 0
    assert isinstance(body["last_run_at"], str) or body["last_run_at"] is None


def test_summary_buckets_killswitch_aborted_under_error(fresh_db, workspace_factory):
    ws_id = workspace_factory()
    with fresh_db() as session:
        _make_lead(
            session,
            workspace_id=ws_id,
            name="Aborted",
            enrichment=_envelope(status="killswitch_aborted"),
        )

    with _client() as client:
        body = client.get(f"/api/scans/summary{ALL}").json()

    assert body["error"] == 1
    assert body["ok"] == 0


# ---------------------------------------------------------------------------
# GET /api/scans/{lead_id}
# ---------------------------------------------------------------------------


def test_get_scan_returns_full_envelope(fresh_db, workspace_factory):
    ws_id = workspace_factory()
    with fresh_db() as session:
        lead = _make_lead(
            session,
            workspace_id=ws_id,
            name="Detail",
            enrichment=_envelope(status="ok"),
        )
        lead_id = lead.id

    with _client() as client:
        body = client.get(f"/api/scans/{lead_id}").json()

    assert body["lead_id"] == lead_id
    assert body["status"] == "ok"
    assert body["enrichment"]["_meta"]["connector"] == CONNECTOR_NAME
    assert body["enrichment"]["signals"]["cms"] == "wordpress"


def test_get_scan_404_for_unknown_lead(fresh_db, workspace_factory):
    workspace_factory()
    with _client() as client:
        response = client.get("/api/scans/does-not-exist")
    assert response.status_code == 404


def test_get_scan_returns_never_scanned_when_no_blob(fresh_db, workspace_factory):
    ws_id = workspace_factory()
    with fresh_db() as session:
        lead = _make_lead(session, workspace_id=ws_id, name="Pristine")
        lead_id = lead.id

    with _client() as client:
        body = client.get(f"/api/scans/{lead_id}").json()

    assert body["status"] == "never_scanned"
    assert body["enrichment"] is None


# ---------------------------------------------------------------------------
# POST /api/scans/run
# ---------------------------------------------------------------------------


def _install_enrichment_handler(monkeypatch):
    """Stub :func:`enrich_lead` so the handler doesn't make real HTTP."""

    async def fake_enrich_lead(*, website_url, http_client, budget_s, respect_robots):
        from autosdr.enrichment import EnrichmentResult

        return EnrichmentResult(
            status="ok",
            signals={"title": "Stub", "cms": "wordpress"},
            meta={
                "fetched_at": datetime.now(tz=timezone.utc).isoformat(),
                "user_agent": "AutoSDR/test",
                "robots_respected": True,
                "latency_ms": 123,
                "http_status": 200,
                "final_url": website_url,
            },
        )

    import autosdr.pipeline.scans as pipeline_scans

    monkeypatch.setattr(pipeline_scans, "enrich_lead", fake_enrich_lead)


def test_run_scans_requires_enabled_or_lead_id(fresh_db, workspace_factory):
    """Empty body is rejected."""
    workspace_factory()
    with _client() as client:
        response = client.post("/api/scans/run", json={})

    assert response.status_code == 422


def test_run_scans_toggle_starts_fanout_placeholder(fresh_db, workspace_factory):
    """No scannable rows → runner accepts start but exits immediately."""

    workspace_factory()
    with _client() as client:
        response = client.post("/api/scans/run", json={"enabled": True})
        assert response.status_code == 200, response.text
        body = response.json()

    assert body["runner_total"] == 0
    assert body["runner_running"] is False


def test_run_scans_stop_when_idle(fresh_db, workspace_factory):
    workspace_factory()
    with _client() as client:
        response = client.post("/api/scans/run", json={"enabled": False})
        assert response.status_code == 200
        body = response.json()
    assert body["runner_running"] is False


def test_run_scans_with_lead_id_runs_synchronously(
    fresh_db, workspace_factory, monkeypatch
):
    """A ``lead_id`` payload runs the fetch inside the request — the
    operator's "Re-scan now" button gets an immediate result."""

    ws_id = workspace_factory()
    with fresh_db() as session:
        lead = _make_lead(session, workspace_id=ws_id, name="Sync", website="https://x.example")
        lead_id = lead.id

    _install_enrichment_handler(monkeypatch)

    with _client() as client:
        response = client.post("/api/scans/run", json={"lead_id": lead_id})
        assert response.status_code == 200, response.text
        body = response.json()

    assert body["started"] is True
    assert body["lead_id"] == lead_id
    assert body["status"] == "ok"
    # The envelope was persisted verbatim.
    with fresh_db() as session:
        lead = session.get(Lead, lead_id)
        assert lead.raw_data["enrichment"]["_meta"]["status"] == "ok"


def test_run_scans_returns_404_for_unknown_lead(fresh_db, workspace_factory):
    workspace_factory()
    with _client() as client:
        response = client.post("/api/scans/run", json={"lead_id": "no-such"})
    assert response.status_code == 404
    assert response.json()["error"] == "lead_not_found"
