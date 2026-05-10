"""Tests for POST /api/threads/requeue (ticket 0018).

Re-queuing flips connector-failed threads back to ACTIVE/QUEUED so the
scheduler picks them up within the campaign's outreach_per_day limit.
No connector calls happen here — sends go through the normal pipeline.
"""

from __future__ import annotations

from autosdr.api.schemas import RequeueThreadsRequest
from autosdr.api.threads import requeue_threads
from autosdr.connectors import rebuild_connector
from autosdr.db import session_scope
from autosdr.models import (
    Campaign,
    CampaignLead,
    CampaignLeadStatus,
    CampaignStatus,
    Lead,
    LeadStatus,
    Thread,
    ThreadStatus,
    Workspace,
)


def _seed_connector_failed_thread(
    *,
    fresh_db,
    workspace_id: str,
    contact_uri: str,
) -> dict[str, str]:
    with fresh_db() as session:
        ws = session.get(Workspace, workspace_id)
        campaign = Campaign(
            workspace_id=ws.id,
            name="C",
            goal="Book a call",
            outreach_per_day=5,
            connector_type="file",
            status=CampaignStatus.ACTIVE,
        )
        session.add(campaign)
        session.flush()

        lead = Lead(
            workspace_id=ws.id,
            name="Tester",
            contact_uri=contact_uri,
            contact_type="mobile",
            category="Retail",
            address="Brisbane",
            raw_data={},
            import_order=1,
            source_file="x",
            status=LeadStatus.NEW,
        )
        session.add(lead)
        session.flush()

        cl = CampaignLead(
            campaign_id=campaign.id,
            lead_id=lead.id,
            queue_position=1,
            status=CampaignLeadStatus.PAUSED_FOR_HITL,
        )
        session.add(cl)
        session.flush()

        thread = Thread(
            campaign_lead_id=cl.id,
            connector_type="file",
            status=ThreadStatus.PAUSED_FOR_HITL,
            hitl_reason="connector_send_failed",
            hitl_context={
                "last_drafts": ["hey, quick chat?"],
                "connector_error": "network_error",
            },
        )
        session.add(thread)
        session.flush()
        return {
            "thread_id": thread.id,
            "campaign_lead_id": cl.id,
        }


def test_requeue_flips_state(fresh_db, workspace_factory):
    ws_id = workspace_factory()
    with fresh_db() as session:
        ws = session.get(Workspace, ws_id)
        rebuild_connector(dict(ws.settings))

    ids = [
        _seed_connector_failed_thread(
            fresh_db=fresh_db, workspace_id=ws_id, contact_uri=f"+6140000010{i}"
        )
        for i in range(3)
    ]
    thread_ids = [d["thread_id"] for d in ids]

    resp = requeue_threads(RequeueThreadsRequest(thread_ids=thread_ids))

    assert resp.requeued == 3
    assert resp.skipped == 0

    with session_scope() as session:
        for d in ids:
            thread = session.get(Thread, d["thread_id"])
            assert thread.status == ThreadStatus.ACTIVE
            assert thread.hitl_reason is None
            # last_drafts is kept so the scheduler can send without LLM calls.
            assert (thread.hitl_context or {}).get("last_drafts")
            assert "connector_error" not in (thread.hitl_context or {})
            cl = session.get(CampaignLead, d["campaign_lead_id"])
            assert cl.status == CampaignLeadStatus.QUEUED


def test_requeue_skips_already_active(fresh_db, workspace_factory):
    ws_id = workspace_factory()
    with fresh_db() as session:
        ws = session.get(Workspace, ws_id)
        rebuild_connector(dict(ws.settings))

    d = _seed_connector_failed_thread(
        fresh_db=fresh_db, workspace_id=ws_id, contact_uri="+61400000200"
    )
    with session_scope() as session:
        thread = session.get(Thread, d["thread_id"])
        thread.status = ThreadStatus.ACTIVE
        thread.hitl_reason = None

    resp = requeue_threads(RequeueThreadsRequest(thread_ids=[d["thread_id"]]))
    assert resp.requeued == 0
    assert resp.skipped == 1


def test_requeue_skips_unknown_thread(fresh_db, workspace_factory):
    ws_id = workspace_factory()
    with fresh_db() as session:
        ws = session.get(Workspace, ws_id)
        rebuild_connector(dict(ws.settings))

    resp = requeue_threads(
        RequeueThreadsRequest(thread_ids=["does-not-exist"])
    )
    assert resp.requeued == 0
    assert resp.skipped == 1


def test_requeue_dedupes_ids(fresh_db, workspace_factory):
    ws_id = workspace_factory()
    with fresh_db() as session:
        ws = session.get(Workspace, ws_id)
        rebuild_connector(dict(ws.settings))

    d = _seed_connector_failed_thread(
        fresh_db=fresh_db, workspace_id=ws_id, contact_uri="+61400000300"
    )
    resp = requeue_threads(
        RequeueThreadsRequest(
            thread_ids=[d["thread_id"], d["thread_id"], d["thread_id"]]
        )
    )
    assert resp.requeued == 1
    assert resp.skipped == 0


def test_requeue_empty_is_noop(fresh_db, workspace_factory):
    ws_id = workspace_factory()
    with fresh_db() as session:
        ws = session.get(Workspace, ws_id)
        rebuild_connector(dict(ws.settings))

    resp = requeue_threads(RequeueThreadsRequest(thread_ids=[]))
    assert resp.requeued == 0
    assert resp.skipped == 0
