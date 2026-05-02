"""Reply pipeline — first-message-only mode (auto_reply_enabled=False).

The default AutoSDR config is first-message-only: send the outreach, then
route every reply to HITL with suggested drafts stashed on the thread.
Tests here cover that default. The legacy auto-reply behaviour is covered
in ``tests/test_reply_pipeline.py``.
"""

from __future__ import annotations

from datetime import datetime, timezone
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

    # The inbound suggestion flow now uses the follow-up prompt
    # (``followup-reply-v1``); tests written against the legacy
    # generate-then-evaluate flow register responses under
    # ``generation-v7``. Alias one to the other so existing test
    # fixtures keep working without per-test edits.
    _PROMPT_ALIASES = {
        "followup-reply-v1": "generation-v8",
    }

    async def _fake_complete_text(
        *, system, user, model, prompt_version, temperature, context=None, **_kwargs
    ):
        key = prompt_version if prompt_version in responses else _PROMPT_ALIASES.get(
            prompt_version, prompt_version
        )
        payload = responses.get(key)
        if isinstance(payload, list):
            payload = payload.pop(0)
        return CompletionResult(
            text=payload, model=model, prompt_version=prompt_version,
            tokens_in=5, tokens_out=5, attempts=1, latency_ms=1,
        )

    async def _fake_complete_json(
        *, system, user, model, prompt_version, temperature=0.0, context=None, **_kwargs
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
    # Inbound replies route through the follow-up suggestion flow (single
    # ``complete_text`` per variant, no evaluator). It imports
    # ``complete_text`` directly into ``autosdr.pipeline.suggestions`` so
    # the patch on ``_shared.complete_text`` doesn't reach it.
    monkeypatch.setattr(
        "autosdr.pipeline.suggestions.complete_text", _fake_complete_text
    )


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
            "classification-v1.1": {
                "intent": "positive",
                "confidence": 0.92,
                "reason": "Lead wants to know more.",
            },
            "generation-v8": list(drafts),
            "evaluation-v4.7": [eval_payload, eval_payload, eval_payload],
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
        # The CampaignLead has to advance to REPLIED on the inbound, even
        # though we never sent an auto-reply — otherwise the campaign list
        # / detail "Replied" stat (which is bucketed off CampaignLeadStatus)
        # silently stays at zero in first-message-only mode.
        cl = session.get(
            CampaignLead, first_message_only_thread["campaign_lead_id"]
        )
        assert cl.status == CampaignLeadStatus.REPLIED
        lead = session.get(Lead, first_message_only_thread["lead_id"])
        assert lead.status == LeadStatus.REPLIED


async def test_negative_intent_still_parks_for_hitl(
    first_message_only_thread, fresh_db, monkeypatch
):
    """Terminal-intent shortcut is auto-reply-only.

    In first-message-only mode the operator must see every reply, even
    obvious "not interested" ones — silently closing a thread the human
    hasn't yet read would lose context for follow-ups, opt-out tracking,
    and the funnel close decision. The classifier verdict still flows
    through to ``hitl_context`` so the inbox surfaces "LLM thinks this
    is negative" alongside the drafts.

    Uses a non-keyword negative ("not interested") so the deterministic
    opt-out shortcut doesn't preempt the classifier path under test.
    """

    drafts = [
        "no worries, appreciate you replying — take care.",
        "all good, cheers for the reply.",
        "fair enough — shoot me a text if anything changes.",
    ]
    eval_payload = {
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
    _patch_llm(
        monkeypatch,
        {
            "classification-v1.1": {
                "intent": "negative",
                "confidence": 0.95,
                "reason": "lead is not interested",
            },
            "generation-v8": list(drafts),
            "evaluation-v4.7": [eval_payload, eval_payload, eval_payload],
        },
    )
    connector = FileConnector(outbox_path=first_message_only_thread["outbox_path"])
    incoming = IncomingMessage(contact_uri="+61400000002", content="not interested, thanks")

    result = await process_incoming_message(
        connector=connector,
        workspace_id=first_message_only_thread["workspace_id"],
        incoming=incoming,
    )
    assert result.action == "escalated_hitl"
    assert result.intent == "negative"

    with fresh_db() as session:
        t = session.get(Thread, first_message_only_thread["thread_id"])
        assert t.status == ThreadStatus.PAUSED_FOR_HITL
        assert t.hitl_reason == HITL_AWAITING_HUMAN_REPLY
        ctx = t.hitl_context or {}
        # Classifier verdict captured for the inbox sidebar.
        assert ctx.get("intent") == "negative"
        # Drafts stashed even on a "negative" reply so the operator has
        # a one-click "appreciate the reply, take care" close-out.
        suggestions = ctx.get("suggestions") or []
        assert len(suggestions) == 3


async def test_goal_achieved_intent_still_parks_for_hitl(
    first_message_only_thread, fresh_db, monkeypatch
):
    """Goal-achieved no longer auto-closes won — operator confirms the close."""

    drafts = [
        "great — I'll send a calendar link in a sec.",
        "sweet, I'll get something across to you today.",
        "love it, what's the best email to send a quick brief to?",
    ]
    eval_payload = {
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
    _patch_llm(
        monkeypatch,
        {
            "classification-v1.1": {
                "intent": "goal_achieved",
                "confidence": 0.95,
                "reason": "booked",
            },
            "generation-v8": list(drafts),
            "evaluation-v4.7": [eval_payload, eval_payload, eval_payload],
        },
    )
    connector = FileConnector(outbox_path=first_message_only_thread["outbox_path"])
    incoming = IncomingMessage(contact_uri="+61400000002", content="sure, book me in")

    result = await process_incoming_message(
        connector=connector,
        workspace_id=first_message_only_thread["workspace_id"],
        incoming=incoming,
    )
    assert result.action == "escalated_hitl"
    assert result.intent == "goal_achieved"

    with fresh_db() as session:
        t = session.get(Thread, first_message_only_thread["thread_id"])
        assert t.status == ThreadStatus.PAUSED_FOR_HITL
        assert t.hitl_reason == HITL_AWAITING_HUMAN_REPLY
        ctx = t.hitl_context or {}
        assert ctx.get("intent") == "goal_achieved"
        suggestions = ctx.get("suggestions") or []
        assert len(suggestions) == 3


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
            "classification-v1.1": {
                "intent": "unclear",
                "confidence": 0.5,
                "reason": "ambiguous",
            },
            "generation-v8": ["draft"],
            "evaluation-v4.7": {
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


# ---------------------------------------------------------------------------
# Phase-split rollback-safety regression tests
# ---------------------------------------------------------------------------
#
# Pre-refactor, ``process_incoming_message`` opened a single
# ``session_scope`` that spanned every LLM call. On SQLite, the inbound
# Message ``flush()`` acquired the write lock; the classifier's parallel
# suggestion calls then deadlocked against the outer txn (each tried to
# insert its own LlmCall audit row, blocked on the lock, hit the 2-min
# busy_timeout, raised, and the *whole pipeline* rolled back — losing the
# inbound Message that had already been "captured"). The phased design
# commits the inbound write before any LLM runs, so even if classify or
# suggestions explode the transcript still shows what came in.


async def test_inbound_message_persists_when_classifier_raises(
    first_message_only_thread, fresh_db, monkeypatch
):
    """Phase 1's commit must land even when Phase 2 classify blows up.

    Regression for the SQLite deadlock that rolled back inbound captures
    when the classifier (or any later LLM call) raised. The lead's reply
    is the audit-of-record — it must survive any downstream failure.
    """

    async def _classifier_explodes(**_kwargs):
        raise RuntimeError("simulated classifier crash")

    monkeypatch.setattr(
        "autosdr.pipeline.reply.complete_json", _classifier_explodes
    )

    connector = FileConnector(outbox_path=first_message_only_thread["outbox_path"])
    incoming = IncomingMessage(
        contact_uri="+61400000002",
        content="Yes please send the deck",
        provider_message_id="prov-rollback-1",
    )

    with pytest.raises(RuntimeError):
        await process_incoming_message(
            connector=connector,
            workspace_id=first_message_only_thread["workspace_id"],
            incoming=incoming,
        )

    with fresh_db() as session:
        thread = session.get(Thread, first_message_only_thread["thread_id"])
        # Thread state untouched by the failed classify — still ACTIVE,
        # so the next inbound (or a manual replay) will go down the
        # normal classify path. We didn't half-park it.
        assert thread.status == ThreadStatus.ACTIVE
        assert thread.hitl_reason is None

        # The inbound message itself MUST be on record. Without the
        # phased commit, this row would have rolled back with the rest
        # of the transaction and the operator's transcript would lie.
        lead_msgs = (
            session.query(Message)
            .filter(
                Message.thread_id == thread.id,
                Message.role == MessageRole.LEAD,
            )
            .all()
        )
        assert len(lead_msgs) == 1
        assert lead_msgs[0].content == "Yes please send the deck"
        assert (
            lead_msgs[0].metadata_.get("provider_message_id") == "prov-rollback-1"
        )


async def test_two_inbounds_same_thread_no_double_park(
    first_message_only_thread, fresh_db, monkeypatch
):
    """Phase 3's PAUSED_FOR_HITL guard handles back-to-back inbounds.

    The poller dispatches messages sequentially, but in real-world traffic
    we still see "same lead replies twice in five seconds" (e.g. carrier
    splits a multi-part SMS). The first reply parks the thread; the
    second reply finds it already paused and is captured-only — no
    second classify, no second suggestion fan-out, no overwritten
    suggestions list.
    """

    drafts = [
        "happy to share more — what's a good time?",
        "sure, want me to send the deck?",
        "great — Tuesday 2pm work?",
    ]
    eval_payload = {
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
    _patch_llm(
        monkeypatch,
        {
            "classification-v1.1": {
                "intent": "positive",
                "confidence": 0.9,
                "reason": "interested",
            },
            "generation-v8": list(drafts),
            "evaluation-v4.7": [eval_payload, eval_payload, eval_payload],
        },
    )

    connector = FileConnector(outbox_path=first_message_only_thread["outbox_path"])

    first_result = await process_incoming_message(
        connector=connector,
        workspace_id=first_message_only_thread["workspace_id"],
        incoming=IncomingMessage(
            contact_uri="+61400000002",
            content="sure, tell me more",
            provider_message_id="prov-1",
        ),
    )
    assert first_result.action == "escalated_hitl"

    # Second inbound on the now-paused thread. The classifier should NOT
    # be called again (the resolver short-circuits to the paused-thread
    # capture branch in Phase 1) — we sub in a sentinel that fails the
    # test if invoked.
    async def _classifier_must_not_run(**_kwargs):
        raise AssertionError("classifier should not run on a paused thread")

    monkeypatch.setattr(
        "autosdr.pipeline.reply.complete_json", _classifier_must_not_run
    )

    second_result = await process_incoming_message(
        connector=connector,
        workspace_id=first_message_only_thread["workspace_id"],
        incoming=IncomingMessage(
            contact_uri="+61400000002",
            content="any updates?",
            provider_message_id="prov-2",
        ),
    )
    assert second_result.action == "ignored"
    assert second_result.detail == "thread_paused_for_hitl"

    with fresh_db() as session:
        thread = session.get(Thread, first_message_only_thread["thread_id"])
        assert thread.status == ThreadStatus.PAUSED_FOR_HITL
        # Both inbounds are on the transcript — the second one didn't
        # silently disappear just because the thread was already paused.
        lead_msgs = (
            session.query(Message)
            .filter(
                Message.thread_id == thread.id,
                Message.role == MessageRole.LEAD,
            )
            .order_by(Message.created_at)
            .all()
        )
        assert [m.content for m in lead_msgs] == [
            "sure, tell me more",
            "any updates?",
        ]
        # The original suggestions list is preserved — the second
        # inbound's capture-only path didn't blow it away.
        suggestions = thread.hitl_context.get("suggestions") or []
        assert len(suggestions) == 3


# ---------------------------------------------------------------------------
# Inbound idempotency + timestamp fidelity
# ---------------------------------------------------------------------------
#
# The SMSGate ``/messages/inbox`` endpoint returns every undeleted SMS in
# the phone's inbox on every poll, and the connector's per-process
# ``_seen_ids`` cache resets whenever the API process restarts (auto-reload
# during dev, container restart, deploy). Without DB-layer dedup the same
# inbound used to land in the same thread twice — once on first arrival,
# again the next time the cache cleared. These tests pin the contract.


async def test_inbound_dedupes_on_provider_message_id(
    first_message_only_thread, fresh_db, monkeypatch
):
    """Re-polling the same SMS doesn't append a second ``Message`` row.

    Simulates the connector's in-memory ``_seen_ids`` cache being empty —
    e.g. after a process restart or two pollers running briefly side by
    side. The reply pipeline must recognise that a message with the same
    ``provider_message_id`` is already on the thread and skip everything:
    no duplicate Message row, no second classifier call, no second
    suggestion fan-out.
    """

    classifier_calls: list[str] = []

    drafts = [
        "happy to share more",
        "sure thing",
        "absolutely",
    ]
    eval_payload = {
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

    from autosdr.llm.client import CompletionResult

    async def _tracking_complete_json(
        *, system, user, model, prompt_version, temperature=0.0, context=None, **_kwargs
    ):
        classifier_calls.append(prompt_version)
        if prompt_version == "classification-v1.1":
            payload = {
                "intent": "positive",
                "confidence": 0.92,
                "reason": "interested",
            }
        elif prompt_version == "evaluation-v4.7":
            payload = eval_payload
        else:
            payload = {}
        return payload, CompletionResult(
            text=str(payload),
            model=model,
            prompt_version=prompt_version,
            tokens_in=5,
            tokens_out=5,
            attempts=1,
            latency_ms=1,
        )

    async def _tracking_complete_text(
        *, system, user, model, prompt_version, temperature, context=None, **_kwargs
    ):
        classifier_calls.append(prompt_version)
        return CompletionResult(
            text=drafts.pop(0) if drafts else "fallback",
            model=model,
            prompt_version=prompt_version,
            tokens_in=5,
            tokens_out=5,
            attempts=1,
            latency_ms=1,
        )

    monkeypatch.setattr(
        "autosdr.pipeline.reply.complete_json", _tracking_complete_json
    )
    monkeypatch.setattr(
        "autosdr.pipeline._shared.complete_json", _tracking_complete_json
    )
    monkeypatch.setattr(
        "autosdr.pipeline._shared.complete_text", _tracking_complete_text
    )
    monkeypatch.setattr(
        "autosdr.pipeline.suggestions.complete_text", _tracking_complete_text
    )

    connector = FileConnector(outbox_path=first_message_only_thread["outbox_path"])
    incoming = IncomingMessage(
        contact_uri="+61400000002",
        content="sure, tell me more",
        provider_message_id="sms-restart-1",
    )

    first = await process_incoming_message(
        connector=connector,
        workspace_id=first_message_only_thread["workspace_id"],
        incoming=incoming,
    )
    assert first.action == "escalated_hitl"

    calls_after_first = list(classifier_calls)

    # Second dispatch of the *same* message — happens whenever the
    # connector's per-process cache forgets the id. The pipeline must
    # short-circuit before re-running the classifier.
    second = await process_incoming_message(
        connector=connector,
        workspace_id=first_message_only_thread["workspace_id"],
        incoming=IncomingMessage(
            contact_uri="+61400000002",
            content="sure, tell me more",
            provider_message_id="sms-restart-1",
        ),
    )

    assert second.action == "ignored"
    assert second.detail == "duplicate_inbound"
    # No additional LLM calls fired on the duplicate dispatch.
    assert classifier_calls == calls_after_first

    with fresh_db() as session:
        lead_msgs = (
            session.query(Message)
            .filter(
                Message.thread_id == first_message_only_thread["thread_id"],
                Message.role == MessageRole.LEAD,
            )
            .all()
        )
        # The duplicate poll did NOT add a second Message row to the
        # thread — this is the user-visible bug the test guards against.
        assert len(lead_msgs) == 1
        assert lead_msgs[0].provider_message_id == "sms-restart-1"


async def test_inbound_message_uses_received_at_for_created_at(
    first_message_only_thread, fresh_db, monkeypatch
):
    """``Message.created_at`` reflects when the SMS *arrived*, not when we polled.

    Without this, a poller that scans the phone's inbox 20 s after a
    backlog of messages built up would stamp every backlog row with the
    same poll-tick timestamp and order them by scan order rather than
    by send time. The transcript would lie about message timing and any
    "time to reply" metric would be skewed by poll cadence.
    """

    eval_payload = {
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
    _patch_llm(
        monkeypatch,
        {
            "classification-v1.1": {
                "intent": "negative",
                "confidence": 0.95,
                "reason": "not interested",
            },
            "generation-v8": ["d1", "d2", "d3"],
            "evaluation-v4.7": [eval_payload, eval_payload, eval_payload],
        },
    )

    received_at = datetime(2026, 4, 27, 1, 23, 45, tzinfo=timezone.utc)
    incoming = IncomingMessage(
        contact_uri="+61400000002",
        content="not interested, thanks",
        received_at=received_at,
        provider_message_id="sms-ts-1",
    )

    connector = FileConnector(outbox_path=first_message_only_thread["outbox_path"])
    result = await process_incoming_message(
        connector=connector,
        workspace_id=first_message_only_thread["workspace_id"],
        incoming=incoming,
    )
    # First-message-only mode: even "negative" reply parks for HITL —
    # the timestamp-fidelity contract still holds on the captured row.
    assert result.action == "escalated_hitl"

    with fresh_db() as session:
        lead_msg = (
            session.query(Message)
            .filter(
                Message.thread_id == first_message_only_thread["thread_id"],
                Message.role == MessageRole.LEAD,
            )
            .one()
        )
        # ``received_at`` flowed through to the persisted row's
        # ``created_at`` — the transcript displays send time, not poll
        # time.
        stored_ts = lead_msg.created_at
        if stored_ts.tzinfo is None:
            stored_ts = stored_ts.replace(tzinfo=timezone.utc)
        assert stored_ts == received_at
        assert lead_msg.provider_message_id == "sms-ts-1"


async def test_inbound_on_paused_thread_dedupes_too(
    first_message_only_thread, fresh_db
):
    """The paused-for-HITL capture branch is also idempotent.

    A thread that's already parked for human review still has the lead
    Message inserted into the transcript on each subsequent inbound — but
    if the same inbound is replayed (cache miss after restart), we must
    not stamp a second copy onto that thread either.
    """

    with fresh_db() as session:
        thread = session.get(Thread, first_message_only_thread["thread_id"])
        thread.status = ThreadStatus.PAUSED_FOR_HITL
        thread.hitl_reason = "operator_intervention"
        session.flush()

    connector = FileConnector(outbox_path=first_message_only_thread["outbox_path"])
    incoming = IncomingMessage(
        contact_uri="+61400000002",
        content="ping",
        provider_message_id="sms-paused-1",
    )

    first = await process_incoming_message(
        connector=connector,
        workspace_id=first_message_only_thread["workspace_id"],
        incoming=incoming,
    )
    assert first.action == "ignored"
    assert first.detail == "thread_paused_for_hitl"

    second = await process_incoming_message(
        connector=connector,
        workspace_id=first_message_only_thread["workspace_id"],
        incoming=IncomingMessage(
            contact_uri="+61400000002",
            content="ping",
            provider_message_id="sms-paused-1",
        ),
    )
    assert second.action == "ignored"
    # The *duplicate* short-circuit beats the paused-thread branch — they
    # both end up "ignored" but the dedup detail tells us the row was
    # rejected before insertion, not after.
    assert second.detail == "duplicate_inbound"

    with fresh_db() as session:
        lead_msgs = (
            session.query(Message)
            .filter(
                Message.thread_id == first_message_only_thread["thread_id"],
                Message.role == MessageRole.LEAD,
            )
            .all()
        )
        assert len(lead_msgs) == 1
