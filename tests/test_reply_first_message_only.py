"""Reply pipeline — first-message-only mode (auto_reply_enabled=False).

The default AutoSDR config is first-message-only: send the outreach, then
route every reply to HITL with suggested drafts stashed on the thread.
Tests here cover that default. The legacy auto-reply behaviour is covered
in ``tests/test_reply_pipeline.py``.
"""

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
    Workspace,
)
from autosdr.pipeline import process_incoming_message
from autosdr.pipeline.reply import HITL_AWAITING_HUMAN_REPLY


@pytest.fixture
def first_message_only_thread(fresh_db, workspace_factory, tmp_path):
    """Active thread on a workspace with the default ``auto_reply_enabled=False``."""

    ws_id = workspace_factory()  # default: auto_reply_enabled = False

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
            contact_uri="+61400000002",
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
            "outbox_path": tmp_path / "outbox.jsonl",
        }


def _patch_llm(monkeypatch: pytest.MonkeyPatch, responses: dict[str, Any]) -> None:
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

    monkeypatch.setattr("autosdr.pipeline.reply.complete_json", _fake_complete_json)
    monkeypatch.setattr("autosdr.pipeline._shared.complete_text", _fake_complete_text)
    monkeypatch.setattr("autosdr.pipeline._shared.complete_json", _fake_complete_json)


async def test_positive_reply_goes_to_hitl_with_suggestions(
    first_message_only_thread, fresh_db, monkeypatch
):
    """A positive reply stashes 3 AI drafts on the thread instead of auto-sending."""

    drafts = [
        "Happy to share more. Tuesday 2pm or Wednesday 10am work?",
        "Sure — either Tuesday or Wednesday suits, what's better?",
        "Glad you're interested. How's Tuesday 2pm for a quick chat?",
    ]
    eval_payload = {
        "scores": {
            "tone_match": 0.92,
            "personalisation": 0.9,
            "goal_alignment": 0.95,
            "length_valid": 1.0,
            "naturalness": 0.92,
        },
        "pass": True,
        "feedback": "",
    }
    _patch_llm(
        monkeypatch,
        {
            "classification-v1": {
                "intent": "positive",
                "confidence": 0.92,
                "reason": "Lead wants to know more.",
            },
            "generation-v6": list(drafts),
            "evaluation-v4.2": [eval_payload, eval_payload, eval_payload],
        },
    )

    connector = FileConnector(outbox_path=first_message_only_thread["outbox_path"])
    incoming = IncomingMessage(
        contact_uri="+61400000002", content="sure, tell me more"
    )

    result = await process_incoming_message(
        connector=connector,
        workspace_id=first_message_only_thread["workspace_id"],
        incoming=incoming,
    )
    assert result.action == "escalated_hitl"
    assert result.intent == "positive"

    with fresh_db() as session:
        t = session.get(Thread, first_message_only_thread["thread_id"])
        assert t.status == ThreadStatus.PAUSED_FOR_HITL
        assert t.hitl_reason == HITL_AWAITING_HUMAN_REPLY
        suggestions = t.hitl_context.get("suggestions") or []
        # Three variants were requested.
        assert len(suggestions) == 3
        assert all("draft" in s for s in suggestions)
        assert all("overall" in s for s in suggestions)
        # No auto-send happened — only the original AI + lead reply exist.
        messages = (
            session.query(Message).filter(Message.thread_id == t.id).all()
        )
        assert [m.role for m in messages].count(MessageRole.AI) == 1
        assert [m.role for m in messages].count(MessageRole.LEAD) == 1


async def test_negative_still_closes_lost(first_message_only_thread, fresh_db, monkeypatch):
    """Terminal intents skip the suggestion path and close the thread."""

    _patch_llm(
        monkeypatch,
        {
            "classification-v1": {
                "intent": "negative",
                "confidence": 0.95,
                "reason": "STOP",
            }
        },
    )
    connector = FileConnector(outbox_path=first_message_only_thread["outbox_path"])
    incoming = IncomingMessage(contact_uri="+61400000002", content="stop")

    result = await process_incoming_message(
        connector=connector,
        workspace_id=first_message_only_thread["workspace_id"],
        incoming=incoming,
    )
    assert result.action == "closed_lost"

    with fresh_db() as session:
        t = session.get(Thread, first_message_only_thread["thread_id"])
        assert t.status == ThreadStatus.LOST


async def test_goal_achieved_still_closes_won(
    first_message_only_thread, fresh_db, monkeypatch
):
    _patch_llm(
        monkeypatch,
        {
            "classification-v1": {
                "intent": "goal_achieved",
                "confidence": 0.95,
                "reason": "booked",
            }
        },
    )
    connector = FileConnector(outbox_path=first_message_only_thread["outbox_path"])
    incoming = IncomingMessage(contact_uri="+61400000002", content="sure, book me in")

    result = await process_incoming_message(
        connector=connector,
        workspace_id=first_message_only_thread["workspace_id"],
        incoming=incoming,
    )
    assert result.action == "closed_won"

    with fresh_db() as session:
        t = session.get(Thread, first_message_only_thread["thread_id"])
        assert t.status == ThreadStatus.WON
