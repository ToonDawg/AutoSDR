"""``send-draft`` — the HITL approve-and-send path.

The kill-switch flag is meant to stop the *autopilot*: the scheduler,
auto-reply loop, and follow-up beats. An explicit human click on "Send
this" must still go out — otherwise pausing the system strands the
operator mid-conversation. These tests pin that contract.

Shutdown (SIGTERM / lifespan finalisation) still aborts; the handler
reports that as a clean 409 so the UI can show a sensible message
instead of a generic 500.
"""

from __future__ import annotations

import json

import pytest
from fastapi import HTTPException

from autosdr import killswitch
from autosdr.api.schemas import SendDraftRequest
from autosdr.api.threads import send_draft
from autosdr.connectors import rebuild_connector
from autosdr.db import session_scope
from autosdr.models import (
    Campaign,
    CampaignLead,
    CampaignLeadStatus,
    CampaignStatus,
    Lead,
    LeadStatus,
    Message,
    MessageRole,
    Thread,
    ThreadStatus,
    Workspace,
)


@pytest.fixture
def paused_thread(fresh_db, workspace_factory):
    """Workspace + HITL-paused thread ready to be approved.

    Drops the FileConnector into the connector cache so ``send_draft``
    writes to the test's ``outbox_path`` rather than whatever happens
    to be cached from a previous test.
    """

    ws_id = workspace_factory()
    with fresh_db() as session:
        ws = session.get(Workspace, ws_id)
        rebuild_connector(dict(ws.settings))

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
            contact_uri="+61400000001",
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
            status=CampaignLeadStatus.QUEUED,
        )
        session.add(cl)
        session.flush()

        thread = Thread(
            campaign_lead_id=cl.id,
            connector_type="file",
            status=ThreadStatus.PAUSED_FOR_HITL,
            hitl_reason="awaiting_human_reply",
        )
        session.add(thread)
        session.flush()
        return {"thread_id": thread.id, "campaign_lead_id": cl.id, "lead_id": lead.id}


async def test_send_draft_succeeds_when_paused(paused_thread):
    """A pause flag must not block an explicit human-approved send."""

    killswitch.touch_flag()
    assert killswitch.is_paused()

    result = await send_draft(
        paused_thread["thread_id"],
        SendDraftRequest(draft="Hey, quick hello!", source="manual"),
    )

    # Bypass restored after the handler returns.
    assert killswitch.is_paused()

    assert result.content == "Hey, quick hello!"

    with session_scope() as session:
        messages = (
            session.query(Message)
            .filter(Message.thread_id == paused_thread["thread_id"])
            .all()
        )
        assert len(messages) == 1
        assert messages[0].content == "Hey, quick hello!"

        thread = session.get(Thread, paused_thread["thread_id"])
        # Thread returns to ACTIVE so the next inbound re-enters the pipeline.
        assert thread.status == ThreadStatus.ACTIVE
        assert thread.hitl_reason is None

    # And the FileConnector actually wrote the outbox record — i.e. the
    # message really left the building, it wasn't just persisted.
    from autosdr.config import get_settings

    outbox = get_settings().outbox_path
    assert outbox.exists()
    records = [json.loads(line) for line in outbox.read_text().splitlines() if line.strip()]
    assert any(r["content"] == "Hey, quick hello!" for r in records)


async def test_send_draft_first_outbound_schedules_followup_from_hitl_state(
    paused_thread, monkeypatch
):
    captured_calls: list[dict] = []

    def fake_schedule(**kwargs):
        captured_calls.append(kwargs)
        return None

    monkeypatch.setattr("autosdr.api.threads.schedule_followup_send", fake_schedule)

    with session_scope() as session:
        cl = session.get(CampaignLead, paused_thread["campaign_lead_id"])
        cl.status = CampaignLeadStatus.PAUSED_FOR_HITL
        campaign = session.get(Campaign, cl.campaign_id)
        campaign.followup = {
            "enabled": True,
            "template": "one more thing",
            "delay_s": 10,
            "delay_jitter_s": 0,
        }

    result = await send_draft(
        paused_thread["thread_id"],
        SendDraftRequest(draft="Approved first outbound", source="manual"),
    )

    assert result.content == "Approved first outbound"
    assert len(captured_calls) == 1
    call = captured_calls[0]
    assert call["thread_id"] == paused_thread["thread_id"]
    assert call["parent_message_id"] == result.id
    assert call["contact_uri"] == "+61400000001"
    assert call["campaign_followup"]["enabled"] is True

    with session_scope() as session:
        cl = session.get(CampaignLead, paused_thread["campaign_lead_id"])
        assert cl.status == CampaignLeadStatus.CONTACTED


async def test_send_draft_rejects_first_outbound_already_in_progress(paused_thread):
    with session_scope() as session:
        cl = session.get(CampaignLead, paused_thread["campaign_lead_id"])
        cl.status = CampaignLeadStatus.SENDING

    with pytest.raises(HTTPException) as excinfo:
        await send_draft(
            paused_thread["thread_id"],
            SendDraftRequest(draft="Double click", source="manual"),
        )

    assert excinfo.value.status_code == 409
    assert excinfo.value.detail == {"error": "send_in_progress"}

    with session_scope() as session:
        assert session.query(Message).count() == 0
        cl = session.get(CampaignLead, paused_thread["campaign_lead_id"])
        assert cl.status == CampaignLeadStatus.SENDING


async def test_send_draft_returns_409_when_shutting_down(paused_thread):
    """Shutdown still wins — the 500 error becomes a clean 409 for the UI."""

    killswitch.mark_shutting_down()
    assert killswitch.is_shutting_down()

    with pytest.raises(HTTPException) as excinfo:
        await send_draft(
            paused_thread["thread_id"],
            SendDraftRequest(draft="Hi", source="manual"),
        )
    assert excinfo.value.status_code == 409
    assert excinfo.value.detail == {"error": "system_shutting_down"}

    with session_scope() as session:
        messages = (
            session.query(Message)
            .filter(Message.thread_id == paused_thread["thread_id"])
            .all()
        )
        assert messages == []
        thread = session.get(Thread, paused_thread["thread_id"])
        # Thread was not flipped to ACTIVE — the write was rolled back.
        assert thread.status == ThreadStatus.PAUSED_FOR_HITL


async def test_send_draft_does_not_schedule_followup_after_prior_message(
    paused_thread, monkeypatch
):
    """Later human sends are replies, not a new cold-open follow-up sequence."""

    calls: list[dict] = []

    def fake_schedule_followup_send(**kwargs):
        calls.append(kwargs)

    monkeypatch.setattr(
        "autosdr.api.threads.schedule_followup_send",
        fake_schedule_followup_send,
    )
    with session_scope() as session:
        session.add(
            Message(
                thread_id=paused_thread["thread_id"],
                role=MessageRole.LEAD,
                content="Can you send details?",
                metadata_={},
            )
        )

    await send_draft(
        paused_thread["thread_id"],
        SendDraftRequest(draft="Sure, here are the details.", source="manual"),
    )

    assert calls == []
