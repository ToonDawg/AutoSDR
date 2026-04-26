"""Reply pipeline — routing, status propagation, HITL escalation."""

from __future__ import annotations

from typing import Any

import pytest

from autosdr.connectors.base import IncomingMessage
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
    UnmatchedWebhook,
    Workspace,
)
from autosdr.pipeline import process_incoming_message


@pytest.fixture
def active_thread(fresh_db, workspace_factory, tmp_path):
    """Workspace + active thread with one prior AI outbound.

    The legacy reply tests in this file cover the auto-reply loop. The new
    "first-message-only" default flips auto_reply_enabled off globally, so
    the fixture pins it on here to keep those tests meaningful. The separate
    ``test_reply_first_message_only.py`` covers the default-off behaviour.
    """

    ws_id = workspace_factory(settings_overrides={"auto_reply_enabled": True})
    outbox = tmp_path / "outbox.jsonl"

    with fresh_db() as session:
        ws = session.get(Workspace, ws_id)
        campaign = Campaign(
            workspace_id=ws.id,
            name="C",
            goal="Book a call",
            outreach_per_day=5,
            connector_type="android_sms",
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
            connector_type="android_sms",
            status=ThreadStatus.ACTIVE,
            angle="existing angle",
            tone_snapshot="direct, casual",
        )
        session.add(thread)
        session.flush()

        session.add(
            Message(
                thread_id=thread.id,
                role=MessageRole.AI,
                content="Hey — interested in a 15 min chat?",
                metadata_={},
            )
        )
        session.flush()

        return {
            "workspace_id": ws.id,
            "thread_id": thread.id,
            "lead_id": lead.id,
            "campaign_lead_id": cl.id,
            "outbox_path": outbox,
        }


def _patch_llm(monkeypatch: pytest.MonkeyPatch, responses: dict[str, Any]) -> None:
    """Patch classify + generate + eval used by the reply pipeline."""

    from autosdr.llm.client import CompletionResult

    async def _fake_complete_text(
        *, system, user, model, prompt_version, temperature, context=None
    ):
        payload = responses.get(prompt_version)
        if isinstance(payload, list):
            payload = payload.pop(0)
        return CompletionResult(
            text=payload, model=model, prompt_version=prompt_version,
            tokens_in=5, tokens_out=5, attempts=1, latency_ms=1,
        )

    async def _fake_complete_json(
        *, system, user, model, prompt_version, temperature=0.0, context=None
    ):
        payload = responses.get(prompt_version)
        if isinstance(payload, list):
            payload = payload.pop(0)
        return payload, CompletionResult(
            text=str(payload), model=model, prompt_version=prompt_version,
            tokens_in=5, tokens_out=5, attempts=1, latency_ms=1,
        )

    # Classify uses pipeline.reply's complete_json binding.
    monkeypatch.setattr("autosdr.pipeline.reply.complete_json", _fake_complete_json)
    # Generate + evaluate now live in pipeline._shared — both the legacy
    # auto-reply loop and the new suggested-replies path pull from there.
    monkeypatch.setattr("autosdr.pipeline._shared.complete_text", _fake_complete_text)
    monkeypatch.setattr("autosdr.pipeline._shared.complete_json", _fake_complete_json)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


async def test_negative_intent_closes_lost_and_propagates(active_thread, fresh_db, monkeypatch):
    _patch_llm(
        monkeypatch,
        {
            "classification-v1": {
                "intent": "negative",
                "confidence": 0.95,
                "reason": "Lead said STOP.",
            }
        },
    )
    connector = FileConnector(outbox_path=active_thread["outbox_path"])
    # Use a non-keyword negative so the deterministic opt-out shortcut
    # doesn't preempt the LLM classifier path under test.
    incoming = IncomingMessage(contact_uri="+61400000001", content="Nah, not interested.")

    result = await process_incoming_message(
        connector=connector,
        workspace_id=active_thread["workspace_id"],
        incoming=incoming,
    )
    assert result.action == "closed_lost"

    with fresh_db() as session:
        t = session.get(Thread, active_thread["thread_id"])
        cl = session.get(CampaignLead, active_thread["campaign_lead_id"])
        lead = session.get(Lead, active_thread["lead_id"])
        assert t.status == ThreadStatus.LOST
        assert cl.status == CampaignLeadStatus.LOST
        assert lead.status == LeadStatus.LOST


async def test_goal_achieved_closes_won(active_thread, fresh_db, monkeypatch):
    _patch_llm(
        monkeypatch,
        {
            "classification-v1": {
                "intent": "goal_achieved",
                "confidence": 0.95,
                "reason": "Lead agreed to book.",
            }
        },
    )
    connector = FileConnector(outbox_path=active_thread["outbox_path"])
    incoming = IncomingMessage(contact_uri="+61400000001", content="Sure, book me in.")

    result = await process_incoming_message(
        connector=connector,
        workspace_id=active_thread["workspace_id"],
        incoming=incoming,
    )
    assert result.action == "closed_won"

    with fresh_db() as session:
        t = session.get(Thread, active_thread["thread_id"])
        cl = session.get(CampaignLead, active_thread["campaign_lead_id"])
        lead = session.get(Lead, active_thread["lead_id"])
        assert t.status == ThreadStatus.WON
        assert cl.status == CampaignLeadStatus.WON
        assert lead.status == LeadStatus.WON


async def test_bot_check_escalates(active_thread, fresh_db, monkeypatch):
    _patch_llm(
        monkeypatch,
        {
            "classification-v1": {
                "intent": "bot_check",
                "confidence": 0.9,
                "reason": "Lead asked if this is a robot.",
            }
        },
    )
    connector = FileConnector(outbox_path=active_thread["outbox_path"])
    incoming = IncomingMessage(contact_uri="+61400000001", content="are you a bot?")

    result = await process_incoming_message(
        connector=connector,
        workspace_id=active_thread["workspace_id"],
        incoming=incoming,
    )
    assert result.action == "escalated_hitl"

    with fresh_db() as session:
        t = session.get(Thread, active_thread["thread_id"])
        assert t.status == ThreadStatus.PAUSED_FOR_HITL
        assert t.hitl_reason == "bot_check"
        assert t.hitl_context["intent"] == "bot_check"
        assert "are you a bot" in t.hitl_context["incoming_message"].lower()


async def test_low_confidence_escalates(active_thread, fresh_db, monkeypatch):
    _patch_llm(
        monkeypatch,
        {
            "classification-v1": {
                "intent": "question",
                "confidence": 0.55,
                "reason": "Not sure what they mean.",
            }
        },
    )
    connector = FileConnector(outbox_path=active_thread["outbox_path"])
    incoming = IncomingMessage(contact_uri="+61400000001", content="hrmm")

    result = await process_incoming_message(
        connector=connector,
        workspace_id=active_thread["workspace_id"],
        incoming=incoming,
    )
    assert result.action == "escalated_hitl"

    with fresh_db() as session:
        t = session.get(Thread, active_thread["thread_id"])
        assert t.hitl_reason == "low_confidence"


async def test_positive_triggers_auto_reply(active_thread, fresh_db, monkeypatch):
    _patch_llm(
        monkeypatch,
        {
            "classification-v1": {
                "intent": "positive",
                "confidence": 0.92,
                "reason": "Lead wants to know more.",
            },
            "generation-v6": "Happy to share more. Does Tuesday or Wednesday suit for 15 mins?",
            "evaluation-v4.2": {
                "scores": {
                    "tone_match": 0.92,
                    "personalisation": 0.9,
                    "goal_alignment": 0.95,
                    "length_valid": 1.0,
                    "naturalness": 0.92,
                },
                "pass": True,
                "feedback": "",
            },
        },
    )
    connector = FileConnector(outbox_path=active_thread["outbox_path"])
    incoming = IncomingMessage(contact_uri="+61400000001", content="sure, tell me more")

    result = await process_incoming_message(
        connector=connector,
        workspace_id=active_thread["workspace_id"],
        incoming=incoming,
    )
    assert result.action == "sent"
    assert result.intent == "positive"

    with fresh_db() as session:
        t = session.get(Thread, active_thread["thread_id"])
        lead = session.get(Lead, active_thread["lead_id"])
        cl = session.get(CampaignLead, active_thread["campaign_lead_id"])
        assert t.status == ThreadStatus.ACTIVE
        assert t.auto_reply_count == 1
        assert lead.status == LeadStatus.REPLIED
        assert cl.status == CampaignLeadStatus.REPLIED

        messages = (
            session.query(Message)
            .filter(Message.thread_id == t.id)
            .order_by(Message.created_at.asc())
            .all()
        )
        # Original AI, lead reply, AI auto-reply.
        assert [m.role for m in messages] == [
            MessageRole.AI,
            MessageRole.LEAD,
            MessageRole.AI,
        ]
        assert "tuesday" in messages[-1].content.lower()


async def test_unparseable_sender_goes_to_unmatched(active_thread, fresh_db, monkeypatch):
    _patch_llm(monkeypatch, {})  # shouldn't even get to classify
    connector = FileConnector(outbox_path=active_thread["outbox_path"])
    incoming = IncomingMessage(contact_uri="not-a-phone", content="hi")

    result = await process_incoming_message(
        connector=connector,
        workspace_id=active_thread["workspace_id"],
        incoming=incoming,
    )
    assert result.action == "unmatched"

    with fresh_db() as session:
        assert session.query(UnmatchedWebhook).count() == 1


async def test_unknown_sender_goes_to_unmatched(active_thread, fresh_db, monkeypatch):
    _patch_llm(monkeypatch, {})
    connector = FileConnector(outbox_path=active_thread["outbox_path"])
    incoming = IncomingMessage(contact_uri="+61499999999", content="hi")

    result = await process_incoming_message(
        connector=connector,
        workspace_id=active_thread["workspace_id"],
        incoming=incoming,
    )
    assert result.action == "unmatched"


async def test_inbound_while_thread_paused_is_logged_but_ignored(
    active_thread, fresh_db, monkeypatch
):
    _patch_llm(monkeypatch, {})
    # Put the thread into HITL state.
    with fresh_db() as session:
        t = session.get(Thread, active_thread["thread_id"])
        t.status = ThreadStatus.PAUSED_FOR_HITL
        t.hitl_reason = "manual"

    connector = FileConnector(outbox_path=active_thread["outbox_path"])
    incoming = IncomingMessage(contact_uri="+61400000001", content="still interested?")

    result = await process_incoming_message(
        connector=connector,
        workspace_id=active_thread["workspace_id"],
        incoming=incoming,
    )
    assert result.action == "ignored"
    assert result.detail == "thread_paused_for_hitl"

    with fresh_db() as session:
        messages = (
            session.query(Message)
            .filter(Message.thread_id == active_thread["thread_id"])
            .all()
        )
        # Original outbound + the inbound we just logged, but no auto reply.
        roles = [m.role for m in messages]
        assert MessageRole.LEAD in roles
        assert roles.count(MessageRole.AI) == 1  # still only the original outbound


async def test_multi_campaign_routes_to_most_recent_outbound(
    fresh_db, workspace_factory, tmp_path, monkeypatch
):
    """Lead in two campaigns → reply lands on the thread with the latest outbound."""

    from datetime import datetime, timedelta, timezone

    ws_id = workspace_factory()
    outbox = tmp_path / "outbox.jsonl"

    with fresh_db() as session:
        ws = session.get(Workspace, ws_id)
        c1 = Campaign(
            workspace_id=ws.id, name="older", goal="g", outreach_per_day=5,
            connector_type="android_sms", status=CampaignStatus.ACTIVE,
        )
        c2 = Campaign(
            workspace_id=ws.id, name="newer", goal="g", outreach_per_day=5,
            connector_type="android_sms", status=CampaignStatus.ACTIVE,
        )
        session.add_all([c1, c2])
        session.flush()

        lead = Lead(
            workspace_id=ws.id, name="Multi", contact_uri="+61400000002",
            contact_type="mobile", category="x", address="x",
            raw_data={}, import_order=1, source_file="x", status=LeadStatus.CONTACTED,
        )
        session.add(lead)
        session.flush()

        cl1 = CampaignLead(campaign_id=c1.id, lead_id=lead.id, queue_position=1,
                           status=CampaignLeadStatus.CONTACTED)
        cl2 = CampaignLead(campaign_id=c2.id, lead_id=lead.id, queue_position=1,
                           status=CampaignLeadStatus.CONTACTED)
        session.add_all([cl1, cl2])
        session.flush()

        t1 = Thread(campaign_lead_id=cl1.id, connector_type="android_sms",
                    status=ThreadStatus.ACTIVE, angle="old", tone_snapshot="x")
        t2 = Thread(campaign_lead_id=cl2.id, connector_type="android_sms",
                    status=ThreadStatus.ACTIVE, angle="new", tone_snapshot="x")
        session.add_all([t1, t2])
        session.flush()

        older = datetime.now(tz=timezone.utc) - timedelta(days=2)
        newer = datetime.now(tz=timezone.utc) - timedelta(hours=1)
        m1 = Message(thread_id=t1.id, role=MessageRole.AI, content="old ping", metadata_={})
        m1.created_at = older
        m2 = Message(thread_id=t2.id, role=MessageRole.AI, content="new ping", metadata_={})
        m2.created_at = newer
        session.add_all([m1, m2])
        session.flush()
        newer_thread_id = t2.id

    _patch_llm(
        monkeypatch,
        {
            "classification-v1": {
                "intent": "negative",
                "confidence": 0.95,
                "reason": "Stop.",
            }
        },
    )
    connector = FileConnector(outbox_path=outbox)
    result = await process_incoming_message(
        connector=connector,
        workspace_id=ws_id,
        # Non-keyword negative — keeps this test on the classifier path
        # (see deterministic opt-out shortcut in autosdr/pipeline/reply.py).
        incoming=IncomingMessage(contact_uri="+61400000002", content="not interested"),
    )
    assert result.action == "closed_lost"
    assert result.thread_id == newer_thread_id
