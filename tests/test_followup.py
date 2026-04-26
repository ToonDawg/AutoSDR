"""Follow-up beat — delayed second-message scheduling + send.

``autosdr.pipeline.followup`` is deliberately standalone (no LLM calls,
no scheduler loop). Tests drive it directly via :func:`schedule_followup_send`
with a mocked connector and a tiny ``delay_s=0`` so the task fires
immediately. The integration test covers the "outreach pipeline
schedules it after first send" path via the happy-path fixture.
"""

from __future__ import annotations

from typing import Any

import pytest

from autosdr.connectors.base import BaseConnector, OutgoingMessage, SendResult
from autosdr.connectors.file_connector import FileConnector
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
from autosdr.pipeline.followup import DEFAULT_FOLLOWUP_TEMPLATE, schedule_followup_send
from autosdr.pipeline import run_outreach_for_campaign_lead


# ---------------------------------------------------------------------------
# Fixtures + helpers
# ---------------------------------------------------------------------------


class _RecordingConnector(BaseConnector):
    """Minimal connector that records every outgoing message.

    Keeps the test independent of the file-connector serialisation path;
    the follow-up pipeline only cares that ``send`` is an async callable
    returning a ``SendResult`` with ``success=True``.
    """

    connector_type = "test_recording"

    def __init__(self) -> None:
        self.sent: list[OutgoingMessage] = []
        self.fail_next = False

    async def send(self, message: OutgoingMessage) -> SendResult:  # type: ignore[override]
        if self.fail_next:
            self.fail_next = False
            return SendResult(
                success=False,
                provider_message_id=None,
                error="forced_failure",
            )
        self.sent.append(message)
        return SendResult(
            success=True,
            provider_message_id=f"test-{len(self.sent)}",
            error=None,
        )

    def parse_webhook(self, payload):  # pragma: no cover - unused
        raise NotImplementedError

    async def validate_config(self):  # pragma: no cover - unused
        return True, ""


@pytest.fixture
def thread_with_parent(fresh_db, workspace_factory):
    """Workspace + Campaign + Lead + active Thread + one AI message.

    Mirrors the state the outreach pipeline would leave behind *right
    after* a successful first send — the spot where ``schedule_followup_send``
    is called.
    """

    ws_id = workspace_factory()
    with fresh_db() as session:
        ws = session.get(Workspace, ws_id)
        campaign = Campaign(
            workspace_id=ws.id,
            name="Follow-up test",
            goal="Book a 15-minute call",
            outreach_per_day=5,
            connector_type="test_recording",
            status=CampaignStatus.ACTIVE,
            followup={
                "enabled": True,
                "template": "Cheers {name}! - Jaclyn",
                "delay_s": 0,
                "delay_jitter_s": 0,
            },
        )
        session.add(campaign)
        lead = Lead(
            workspace_id=ws.id,
            name="Paul",
            contact_uri="+61400000001",
            contact_type="mobile",
            category="Plumbing",
            address="Augustine Heights QLD",
            raw_data={"rating": 4.9, "reviews": 300},
            import_order=1,
            source_file="test.csv",
            status=LeadStatus.CONTACTED,
        )
        session.add(lead)
        session.flush()
        cl = CampaignLead(
            campaign_id=campaign.id,
            lead_id=lead.id,
            queue_position=1,
            status=CampaignLeadStatus.CONTACTED,
        )
        session.add(cl)
        session.flush()
        thread = Thread(
            campaign_lead_id=cl.id,
            connector_type="test_recording",
            status=ThreadStatus.ACTIVE,
            tone_snapshot="casual",
            angle="Rating of 4.9 — elite reputation",
        )
        session.add(thread)
        session.flush()
        parent = Message(
            thread_id=thread.id,
            role=MessageRole.AI,
            content="Hey Paul, saw your 4.9 rating…",
            metadata_={"source": "ai"},
        )
        session.add(parent)
        session.flush()
        return {
            "campaign_id": campaign.id,
            "thread_id": thread.id,
            "lead_id": lead.id,
            "parent_message_id": parent.id,
            "contact_uri": lead.contact_uri,
            "followup_cfg": dict(campaign.followup),
        }


# ---------------------------------------------------------------------------
# schedule_followup_send — guardrails
# ---------------------------------------------------------------------------


async def test_schedule_returns_none_when_config_is_none():
    task = schedule_followup_send(
        campaign_followup=None,
        thread_id="t",
        parent_message_id="p",
        contact_uri="+614",
    )
    assert task is None


async def test_schedule_returns_none_when_disabled():
    task = schedule_followup_send(
        campaign_followup={"enabled": False, "template": "hi", "delay_s": 1, "delay_jitter_s": 0},
        thread_id="t",
        parent_message_id="p",
        contact_uri="+614",
    )
    assert task is None


async def test_blank_template_uses_default_followup_copy(thread_with_parent):
    connector = _RecordingConnector()
    task = schedule_followup_send(
        campaign_followup={"enabled": True, "template": "   ", "delay_s": 1, "delay_jitter_s": 0},
        thread_id=thread_with_parent["thread_id"],
        parent_message_id=thread_with_parent["parent_message_id"],
        contact_uri=thread_with_parent["contact_uri"],
        connector=connector,
    )
    assert task is not None
    await task
    assert connector.sent[0].content == DEFAULT_FOLLOWUP_TEMPLATE


async def test_schedule_returns_none_when_contact_uri_empty():
    task = schedule_followup_send(
        campaign_followup={"enabled": True, "template": "hi", "delay_s": 1, "delay_jitter_s": 0},
        thread_id="t",
        parent_message_id="p",
        contact_uri="",
    )
    assert task is None


# ---------------------------------------------------------------------------
# _run_followup body — happy path + skip conditions
# ---------------------------------------------------------------------------


async def test_followup_sends_and_persists_message(thread_with_parent, fresh_db):
    connector = _RecordingConnector()
    task = schedule_followup_send(
        campaign_followup=thread_with_parent["followup_cfg"],
        thread_id=thread_with_parent["thread_id"],
        parent_message_id=thread_with_parent["parent_message_id"],
        contact_uri=thread_with_parent["contact_uri"],
        lead_name="Paul",
        lead_short_name="Paul",
        connector=connector,
    )
    assert task is not None
    await task

    assert len(connector.sent) == 1
    assert connector.sent[0].content == "Cheers Paul! - Jaclyn"
    assert connector.sent[0].contact_uri == thread_with_parent["contact_uri"]

    with fresh_db() as session:
        messages = (
            session.query(Message)
            .filter(Message.thread_id == thread_with_parent["thread_id"])
            .order_by(Message.created_at.asc())
            .all()
        )
        assert len(messages) == 2
        followup_msg = messages[1]
        assert followup_msg.role == MessageRole.AI
        assert followup_msg.content == "Cheers Paul! - Jaclyn"
        assert followup_msg.metadata_["source"] == "followup"
        assert (
            followup_msg.metadata_["parent_message_id"]
            == thread_with_parent["parent_message_id"]
        )


async def test_followup_skips_when_lead_replied_in_interim(thread_with_parent, fresh_db):
    """A LEAD message landing after the parent invalidates the context."""

    with fresh_db() as session:
        session.add(
            Message(
                thread_id=thread_with_parent["thread_id"],
                role=MessageRole.LEAD,
                content="who is this?",
                metadata_={},
            )
        )

    connector = _RecordingConnector()
    task = schedule_followup_send(
        campaign_followup=thread_with_parent["followup_cfg"],
        thread_id=thread_with_parent["thread_id"],
        parent_message_id=thread_with_parent["parent_message_id"],
        contact_uri=thread_with_parent["contact_uri"],
        lead_name="Paul",
        connector=connector,
    )
    await task

    assert connector.sent == []
    with fresh_db() as session:
        # Only the two pre-existing messages (parent AI + lead reply) —
        # the follow-up was correctly skipped.
        count = (
            session.query(Message)
            .filter(Message.thread_id == thread_with_parent["thread_id"])
            .count()
        )
        assert count == 2


async def test_followup_skips_when_ai_message_landed_after_parent(
    thread_with_parent, fresh_db
):
    """Any newer outbound invalidates the delayed afterthought."""

    with fresh_db() as session:
        session.add(
            Message(
                thread_id=thread_with_parent["thread_id"],
                role=MessageRole.AI,
                content="manual replacement",
                metadata_={"source": "manual"},
            )
        )

    connector = _RecordingConnector()
    task = schedule_followup_send(
        campaign_followup=thread_with_parent["followup_cfg"],
        thread_id=thread_with_parent["thread_id"],
        parent_message_id=thread_with_parent["parent_message_id"],
        contact_uri=thread_with_parent["contact_uri"],
        lead_name="Paul",
        connector=connector,
    )
    await task

    assert connector.sent == []
    with fresh_db() as session:
        assert (
            session.query(Message)
            .filter(Message.thread_id == thread_with_parent["thread_id"])
            .count()
            == 2
        )


async def test_followup_skips_when_lead_contact_uri_changed(
    thread_with_parent, fresh_db
):
    with fresh_db() as session:
        lead = session.get(Lead, thread_with_parent["lead_id"])
        lead.contact_uri = "+61400000999"

    connector = _RecordingConnector()
    task = schedule_followup_send(
        campaign_followup=thread_with_parent["followup_cfg"],
        thread_id=thread_with_parent["thread_id"],
        parent_message_id=thread_with_parent["parent_message_id"],
        contact_uri=thread_with_parent["contact_uri"],
        lead_name="Paul",
        connector=connector,
    )
    await task

    assert connector.sent == []
    with fresh_db() as session:
        assert (
            session.query(Message)
            .filter(Message.thread_id == thread_with_parent["thread_id"])
            .count()
            == 1
        )


async def test_followup_skips_when_thread_no_longer_active(
    thread_with_parent, fresh_db
):
    with fresh_db() as session:
        thread = session.get(Thread, thread_with_parent["thread_id"])
        thread.status = ThreadStatus.PAUSED_FOR_HITL

    connector = _RecordingConnector()
    task = schedule_followup_send(
        campaign_followup=thread_with_parent["followup_cfg"],
        thread_id=thread_with_parent["thread_id"],
        parent_message_id=thread_with_parent["parent_message_id"],
        contact_uri=thread_with_parent["contact_uri"],
        connector=connector,
    )
    await task
    assert connector.sent == []


async def test_followup_connector_failure_does_not_persist_message(
    thread_with_parent, fresh_db
):
    connector = _RecordingConnector()
    connector.fail_next = True

    task = schedule_followup_send(
        campaign_followup=thread_with_parent["followup_cfg"],
        thread_id=thread_with_parent["thread_id"],
        parent_message_id=thread_with_parent["parent_message_id"],
        contact_uri=thread_with_parent["contact_uri"],
        connector=connector,
    )
    await task

    with fresh_db() as session:
        # Parent only — no follow-up row because the send failed.
        count = (
            session.query(Message)
            .filter(Message.thread_id == thread_with_parent["thread_id"])
            .count()
        )
        assert count == 1


# ---------------------------------------------------------------------------
# Template rendering
# ---------------------------------------------------------------------------


async def test_template_renders_known_placeholders(thread_with_parent):
    connector = _RecordingConnector()
    cfg = dict(thread_with_parent["followup_cfg"])
    cfg["template"] = "Cheers {short_name}! — {owner_first_name}"
    task = schedule_followup_send(
        campaign_followup=cfg,
        thread_id=thread_with_parent["thread_id"],
        parent_message_id=thread_with_parent["parent_message_id"],
        contact_uri=thread_with_parent["contact_uri"],
        lead_short_name="Paul",
        owner_first_name="Jaclyn",
        connector=connector,
    )
    await task
    assert connector.sent[0].content == "Cheers Paul! — Jaclyn"


async def test_template_keeps_unknown_tokens_literal(thread_with_parent):
    """Operators who typo ``{nam}`` should see it in-message, not crash."""

    connector = _RecordingConnector()
    cfg = dict(thread_with_parent["followup_cfg"])
    cfg["template"] = "hey {nam}, one more thing"
    task = schedule_followup_send(
        campaign_followup=cfg,
        thread_id=thread_with_parent["thread_id"],
        parent_message_id=thread_with_parent["parent_message_id"],
        contact_uri=thread_with_parent["contact_uri"],
        connector=connector,
    )
    await task
    assert connector.sent[0].content == "hey {nam}, one more thing"


# ---------------------------------------------------------------------------
# Outreach pipeline integration
# ---------------------------------------------------------------------------


async def test_outreach_schedules_followup_on_success(
    fresh_db, workspace_factory, tmp_path, monkeypatch
):
    """A first-contact send on a campaign with followup enabled wires up the task.

    We patch ``schedule_followup_send`` where ``outreach`` imports it
    (``autosdr.pipeline.outreach.schedule_followup_send``) so we can
    capture the call without actually running a coroutine — the real
    body is covered by the tests above.
    """

    ws_id = workspace_factory()
    with fresh_db() as session:
        ws = session.get(Workspace, ws_id)
        campaign = Campaign(
            workspace_id=ws.id,
            name="Test",
            goal="Book a 15-minute call",
            outreach_per_day=5,
            connector_type="android_sms",
            status=CampaignStatus.ACTIVE,
            followup={
                "enabled": True,
                "template": "cheers",
                "delay_s": 10,
                "delay_jitter_s": 0,
            },
        )
        session.add(campaign)
        lead = Lead(
            workspace_id=ws.id,
            name="Paul",
            contact_uri="+61400000001",
            contact_type="mobile",
            category="Plumbing",
            address="Augustine Heights QLD",
            raw_data={"rating": 4.9},
            import_order=1,
            source_file="test.csv",
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
        ids = {
            "workspace_id": ws.id,
            "campaign_id": campaign.id,
            "campaign_lead_id": cl.id,
            "lead_id": lead.id,
        }

    # Lazy-import to keep this helper scoped to this test file.
    from autosdr.llm.client import CompletionResult

    async def fake_text(**kwargs):
        return CompletionResult(
            text="hey — saw your reviews, quick chat?",
            model=kwargs["model"],
            prompt_version=kwargs["prompt_version"],
            tokens_in=1,
            tokens_out=1,
            attempts=1,
            latency_ms=1,
        )

    async def fake_json(**kwargs):
        pv = kwargs["prompt_version"]
        if pv.startswith("analysis"):
            data = {"angle": "x", "signal": "y", "confidence": 0.7}
        elif pv.startswith("evaluation"):
            data = {
                "scores": {
                    "tone_match": 0.95,
                    "personalisation": 0.95,
                    "goal_alignment": 0.95,
                    "length_valid": 1.0,
                    "naturalness": 0.95,
                },
                "pass": True,
                "feedback": "",
            }
        else:
            raise AssertionError(pv)
        return data, CompletionResult(
            text=str(data),
            model=kwargs["model"],
            prompt_version=pv,
            tokens_in=1,
            tokens_out=1,
            attempts=1,
            latency_ms=1,
        )

    monkeypatch.setattr("autosdr.pipeline._shared.complete_text", fake_text)
    monkeypatch.setattr("autosdr.pipeline._shared.complete_json", fake_json)
    monkeypatch.setattr("autosdr.pipeline.outreach.complete_json", fake_json)

    captured_calls: list[dict[str, Any]] = []

    def fake_schedule(**kwargs):
        captured_calls.append(kwargs)
        return None

    monkeypatch.setattr(
        "autosdr.pipeline.outreach.schedule_followup_send", fake_schedule
    )

    connector = FileConnector(outbox_path=tmp_path / "outbox.jsonl")
    with fresh_db() as session:
        result = await run_outreach_for_campaign_lead(
            session=session,
            connector=connector,
            workspace=session.get(Workspace, ids["workspace_id"]),
            campaign=session.get(Campaign, ids["campaign_id"]),
            campaign_lead=session.get(CampaignLead, ids["campaign_lead_id"]),
            lead=session.get(Lead, ids["lead_id"]),
        )

    assert result.sent
    assert len(captured_calls) == 1
    call = captured_calls[0]
    assert call["campaign_followup"]["enabled"] is True
    assert call["campaign_followup"]["template"] == "cheers"
    assert call["thread_id"] == result.thread_id
    assert call["parent_message_id"] == result.message_id
    assert call["contact_uri"] == "+61400000001"


async def test_outreach_skips_followup_when_disabled(
    fresh_db, workspace_factory, tmp_path, monkeypatch
):
    """With ``followup=None`` on the campaign, schedule_followup_send is still
    called but returns None — nothing is actually scheduled. The import
    side is what we verify here; the return-value side is covered above.
    """

    ws_id = workspace_factory()
    with fresh_db() as session:
        ws = session.get(Workspace, ws_id)
        campaign = Campaign(
            workspace_id=ws.id,
            name="Test",
            goal="Book a call",
            outreach_per_day=5,
            connector_type="android_sms",
            status=CampaignStatus.ACTIVE,
            followup=None,
        )
        session.add(campaign)
        lead = Lead(
            workspace_id=ws.id,
            name="Quiet",
            contact_uri="+61400000002",
            contact_type="mobile",
            import_order=1,
            source_file="t",
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
        ids = {
            "workspace_id": ws.id,
            "campaign_id": campaign.id,
            "campaign_lead_id": cl.id,
            "lead_id": lead.id,
        }

    from autosdr.llm.client import CompletionResult

    async def fake_text(**kwargs):
        return CompletionResult(
            text="hey, quick one?",
            model=kwargs["model"],
            prompt_version=kwargs["prompt_version"],
            tokens_in=1,
            tokens_out=1,
            attempts=1,
            latency_ms=1,
        )

    async def fake_json(**kwargs):
        pv = kwargs["prompt_version"]
        if pv.startswith("analysis"):
            data = {"angle": "x", "signal": "y", "confidence": 0.6}
        else:
            data = {
                "scores": {
                    "tone_match": 0.9,
                    "personalisation": 0.9,
                    "goal_alignment": 0.9,
                    "length_valid": 1.0,
                    "naturalness": 0.9,
                },
                "pass": True,
                "feedback": "",
            }
        return data, CompletionResult(
            text=str(data),
            model=kwargs["model"],
            prompt_version=pv,
            tokens_in=1,
            tokens_out=1,
            attempts=1,
            latency_ms=1,
        )

    monkeypatch.setattr("autosdr.pipeline._shared.complete_text", fake_text)
    monkeypatch.setattr("autosdr.pipeline._shared.complete_json", fake_json)
    monkeypatch.setattr("autosdr.pipeline.outreach.complete_json", fake_json)

    captured_calls: list[dict[str, Any]] = []

    def fake_schedule(**kwargs):
        captured_calls.append(kwargs)
        return None

    monkeypatch.setattr(
        "autosdr.pipeline.outreach.schedule_followup_send", fake_schedule
    )

    connector = FileConnector(outbox_path=tmp_path / "outbox.jsonl")
    with fresh_db() as session:
        result = await run_outreach_for_campaign_lead(
            session=session,
            connector=connector,
            workspace=session.get(Workspace, ids["workspace_id"]),
            campaign=session.get(Campaign, ids["campaign_id"]),
            campaign_lead=session.get(CampaignLead, ids["campaign_lead_id"]),
            lead=session.get(Lead, ids["lead_id"]),
        )
    assert result.sent
    assert len(captured_calls) == 1
    assert captured_calls[0]["campaign_followup"] is None
