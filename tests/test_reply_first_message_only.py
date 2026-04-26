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
    LlmCall,
    LlmCallPurpose,
    Message,
    MessageRole,
    Thread,
    ThreadStatus,
    Workspace,
)
from autosdr.pipeline import process_incoming_message
from autosdr.pipeline.reply import HITL_AWAITING_HUMAN_REPLY, OPT_OUT_AUDIT_MODEL


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
    """Terminal intents skip the suggestion path and close the thread.

    Uses a non-keyword negative ("not interested") so the deterministic
    opt-out shortcut doesn't preempt the classifier path under test.
    """

    _patch_llm(
        monkeypatch,
        {
            "classification-v1": {
                "intent": "negative",
                "confidence": 0.95,
                "reason": "lead is not interested",
            }
        },
    )
    connector = FileConnector(outbox_path=first_message_only_thread["outbox_path"])
    incoming = IncomingMessage(contact_uri="+61400000002", content="not interested, thanks")

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


# ---------------------------------------------------------------------------
# Deterministic opt-out shortcut (Spam Act 2003 / TCPA compliance)
# ---------------------------------------------------------------------------


async def test_stop_keyword_triggers_deterministic_opt_out(
    first_message_only_thread, fresh_db, monkeypatch
):
    """A literal STOP closes the thread + flags DNC + skips the classifier."""

    classifier_calls: list[str] = []

    async def _spy_complete_json(*, prompt_version, **_kwargs):
        classifier_calls.append(prompt_version)
        raise AssertionError(
            f"classifier should not run on opt-out; called with {prompt_version}"
        )

    monkeypatch.setattr("autosdr.pipeline.reply.complete_json", _spy_complete_json)

    connector = FileConnector(outbox_path=first_message_only_thread["outbox_path"])
    incoming = IncomingMessage(contact_uri="+61400000002", content="STOP")

    result = await process_incoming_message(
        connector=connector,
        workspace_id=first_message_only_thread["workspace_id"],
        incoming=incoming,
    )

    assert result.action == "closed_opt_out"
    assert result.intent == "opt_out"
    assert result.detail == "STOP"
    assert classifier_calls == []

    with fresh_db() as session:
        t = session.get(Thread, first_message_only_thread["thread_id"])
        assert t.status == ThreadStatus.LOST

        lead = session.get(Lead, first_message_only_thread["lead_id"])
        assert lead.do_not_contact_at is not None
        assert lead.do_not_contact_reason == "opt_out:STOP"
        assert lead.status == LeadStatus.LOST

        # Zero classification rows; exactly one synthetic audit row.
        classification_rows = (
            session.query(LlmCall)
            .filter(LlmCall.purpose == LlmCallPurpose.CLASSIFICATION)
            .all()
        )
        assert classification_rows == []

        audit_rows = (
            session.query(LlmCall)
            .filter(LlmCall.model == OPT_OUT_AUDIT_MODEL)
            .all()
        )
        assert len(audit_rows) == 1
        audit = audit_rows[0]
        assert audit.purpose == LlmCallPurpose.OTHER
        assert audit.thread_id == t.id
        assert audit.response_text == "STOP"
        assert audit.tokens_in == 0
        assert audit.tokens_out == 0


async def test_unsubscribe_in_full_sentence_still_short_circuits(
    first_message_only_thread, fresh_db, monkeypatch
):
    """Word-boundary policy: ``please unsubscribe me`` matches the shortcut."""

    async def _refuse_classifier(*, prompt_version, **_kwargs):
        raise AssertionError("classifier should not run on opt-out")

    monkeypatch.setattr("autosdr.pipeline.reply.complete_json", _refuse_classifier)

    connector = FileConnector(outbox_path=first_message_only_thread["outbox_path"])
    incoming = IncomingMessage(
        contact_uri="+61400000002", content="please unsubscribe me, thanks"
    )

    result = await process_incoming_message(
        connector=connector,
        workspace_id=first_message_only_thread["workspace_id"],
        incoming=incoming,
    )
    assert result.action == "closed_opt_out"
    assert result.detail == "UNSUBSCRIBE"

    with fresh_db() as session:
        lead = session.get(Lead, first_message_only_thread["lead_id"])
        assert lead.do_not_contact_reason == "opt_out:UNSUBSCRIBE"


async def test_third_party_stop_does_not_trigger_shortcut(
    first_message_only_thread, fresh_db, monkeypatch
):
    """Denylist: ``stop texting them`` does NOT flag DNC."""

    _patch_llm(
        monkeypatch,
        {
            "classification-v1": {
                "intent": "unclear",
                "confidence": 0.5,
                "reason": "ambiguous",
            },
            "generation-v6": ["draft"],
            "evaluation-v4.2": {
                "scores": {
                    "tone_match": 0.8,
                    "personalisation": 0.8,
                    "goal_alignment": 0.8,
                    "length_valid": 1.0,
                    "naturalness": 0.8,
                },
                "pass": True,
                "feedback": "",
            },
        },
    )

    connector = FileConnector(outbox_path=first_message_only_thread["outbox_path"])
    incoming = IncomingMessage(
        contact_uri="+61400000002",
        content="STOP texting them, not me — they keep spamming",
    )

    result = await process_incoming_message(
        connector=connector,
        workspace_id=first_message_only_thread["workspace_id"],
        incoming=incoming,
    )
    # The classifier ran (this is NOT closed_opt_out).
    assert result.action != "closed_opt_out"

    with fresh_db() as session:
        lead = session.get(Lead, first_message_only_thread["lead_id"])
        assert lead.do_not_contact_at is None
