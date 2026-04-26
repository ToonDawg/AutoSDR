"""Outreach pipeline — analyse / generate / evaluate / send, with LLM mocked."""

from __future__ import annotations

import json
from typing import Any

import pytest

from autosdr.connectors.base import SendResult
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
from autosdr.pipeline import run_outreach_for_campaign_lead


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def prepared_campaign(fresh_db, workspace_factory, tmp_path):
    """Workspace + campaign + queued CampaignLead for a mobile lead."""

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
        )
        session.add(campaign)
        session.flush()

        lead = Lead(
            workspace_id=ws.id,
            name="Mobile Lead",
            contact_uri="+61400000001",
            contact_type="mobile",
            category="Retail",
            address="Caboolture QLD",
            raw_data={"rating": 3, "reviews": 10},
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

        return {
            "workspace_id": ws.id,
            "campaign_id": campaign.id,
            "lead_id": lead.id,
            "campaign_lead_id": cl.id,
            "outbox_path": tmp_path / "outbox.jsonl",
        }


def _install_mock_llm(monkeypatch: pytest.MonkeyPatch, *, responses: dict[str, Any]) -> list[dict]:
    """Patch the LLM helpers to return deterministic responses.

    ``responses`` keyed by prompt_version. The values are:
      analysis-v1:      dict (the parsed JSON)
      generation-v1:    str or list[str] (the drafts, one per attempt)
      evaluation-v1:    dict or list[dict] (the eval JSONs)
      classification-v1: dict
    """

    calls: list[dict] = []

    async def _fake_complete_text(
        *, system, user, model, prompt_version, temperature, context=None
    ):
        from autosdr.llm.client import CompletionResult

        calls.append(
            {
                "kind": "text",
                "prompt_version": prompt_version,
                "model": model,
                "context": context,
            }
        )
        payload = responses.get(prompt_version)
        if payload is None:
            raise AssertionError(f"no mock configured for {prompt_version}")
        if isinstance(payload, list):
            idx = sum(
                1
                for c in calls
                if c["kind"] == "text" and c["prompt_version"] == prompt_version
            ) - 1
            text = payload[min(idx, len(payload) - 1)]
        else:
            text = payload
        return CompletionResult(
            text=text,
            model=model,
            prompt_version=prompt_version,
            tokens_in=10,
            tokens_out=10,
            attempts=1,
            latency_ms=1,
        )

    async def _fake_complete_json(
        *, system, user, model, prompt_version, temperature=0.0, context=None
    ):
        from autosdr.llm.client import CompletionResult

        calls.append(
            {
                "kind": "json",
                "prompt_version": prompt_version,
                "model": model,
                "context": context,
            }
        )
        payload = responses.get(prompt_version)
        if payload is None:
            raise AssertionError(f"no mock configured for {prompt_version}")
        if isinstance(payload, list):
            idx = sum(
                1
                for c in calls
                if c["kind"] == "json" and c["prompt_version"] == prompt_version
            ) - 1
            data = payload[min(idx, len(payload) - 1)]
        else:
            data = payload
        return data, CompletionResult(
            text=str(data),
            model=model,
            prompt_version=prompt_version,
            tokens_in=10,
            tokens_out=10,
            attempts=1,
            latency_ms=1,
        )

    monkeypatch.setattr("autosdr.pipeline._shared.complete_text", _fake_complete_text)
    monkeypatch.setattr("autosdr.pipeline._shared.complete_json", _fake_complete_json)
    monkeypatch.setattr("autosdr.pipeline.outreach.complete_json", _fake_complete_json)
    return calls


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


async def test_outreach_happy_path(prepared_campaign, fresh_db, monkeypatch):
    _install_mock_llm(
        monkeypatch,
        responses={
            "analysis-v3.3": {
                "angle": "Rating of 3 from 10 reviews suggests room to improve service perception.",
                "signal": "rating=3, reviews=10",
                "confidence": 0.7,
            },
            "generation-v6": "Hey — saw your rating is sitting around 3. Open to a quick chat on lifting it?",
            "evaluation-v4.2": {
                "scores": {
                    "tone_match": 0.9,
                    "personalisation": 0.9,
                    "goal_alignment": 0.9,
                    "length_valid": 1.0,
                    "naturalness": 0.9,
                },
                "pass": True,
                "feedback": "",
            },
        },
    )
    connector = FileConnector(outbox_path=prepared_campaign["outbox_path"])

    with fresh_db() as session:
        workspace = session.get(Workspace, prepared_campaign["workspace_id"])
        campaign = session.get(Campaign, prepared_campaign["campaign_id"])
        lead = session.get(Lead, prepared_campaign["lead_id"])
        cl = session.get(CampaignLead, prepared_campaign["campaign_lead_id"])

        result = await run_outreach_for_campaign_lead(
            session=session,
            connector=connector,
            workspace=workspace,
            campaign=campaign,
            campaign_lead=cl,
            lead=lead,
        )

    assert result.sent
    assert result.attempts == 1
    assert result.overall_score >= 0.85

    with fresh_db() as session:
        lead = session.get(Lead, prepared_campaign["lead_id"])
        cl = session.get(CampaignLead, prepared_campaign["campaign_lead_id"])
        thread = (
            session.query(Thread)
            .filter(Thread.campaign_lead_id == cl.id)
            .one()
        )
        message = (
            session.query(Message)
            .filter(Message.thread_id == thread.id)
            .one()
        )

        assert lead.status == LeadStatus.CONTACTED
        assert cl.status == CampaignLeadStatus.CONTACTED
        assert thread.status == ThreadStatus.ACTIVE
        assert thread.angle  # analysis wrote it
        assert thread.tone_snapshot  # snapshot at creation
        assert message.role == MessageRole.AI
        assert "saw your rating" in message.content.lower()
        assert message.metadata_["eval_score"] >= 0.85
        assert message.metadata_["angle_used"] == thread.angle

    records = [
        json.loads(line)
        for line in prepared_campaign["outbox_path"].read_text().splitlines()
        if line.strip()
    ]
    assert [record["contact_uri"] for record in records] == ["+61400000001"]


async def test_outreach_retries_then_passes(prepared_campaign, fresh_db, monkeypatch):
    """First draft fails eval; second passes. Ensure we don't send the first."""

    _install_mock_llm(
        monkeypatch,
        responses={
            "analysis-v3.3": {
                "angle": "Rating 3 — opportunity to stand out",
                "signal": "rating",
                "confidence": 0.6,
            },
            "generation-v6": [
                "Hello valued customer! Let's discuss synergies.",  # bad
                "Hey — noticed your ratings slipped recently. Quick chat about it?",  # good
            ],
            "evaluation-v4.2": [
                {
                    "scores": {
                        "tone_match": 0.3,
                        "personalisation": 0.2,
                        "goal_alignment": 0.5,
                        "length_valid": 1.0,
                        "naturalness": 0.3,
                    },
                    "pass": False,
                    "feedback": "Sounds templated; drop the corporate speak.",
                },
                {
                    "scores": {
                        "tone_match": 0.92,
                        "personalisation": 0.9,
                        "goal_alignment": 0.9,
                        "length_valid": 1.0,
                        "naturalness": 0.9,
                    },
                    "pass": True,
                    "feedback": "",
                },
            ],
        },
    )
    connector = FileConnector(outbox_path=prepared_campaign["outbox_path"])

    with fresh_db() as session:
        result = await run_outreach_for_campaign_lead(
            session=session,
            connector=connector,
            workspace=session.get(Workspace, prepared_campaign["workspace_id"]),
            campaign=session.get(Campaign, prepared_campaign["campaign_id"]),
            campaign_lead=session.get(CampaignLead, prepared_campaign["campaign_lead_id"]),
            lead=session.get(Lead, prepared_campaign["lead_id"]),
        )

    assert result.sent
    assert result.attempts == 2

    with fresh_db() as session:
        messages = session.query(Message).all()
        # Only the second (passing) draft is sent.
        assert len(messages) == 1
        assert "noticed your ratings" in messages[0].content.lower()


async def test_outreach_escalates_after_max_attempts(
    prepared_campaign, fresh_db, monkeypatch
):
    _install_mock_llm(
        monkeypatch,
        responses={
            "analysis-v3.3": {"angle": "x", "signal": "y", "confidence": 0.5},
            "generation-v6": "Hi hi hi hi hi hi hi hi.",
            "evaluation-v4.2": {
                "scores": {
                    "tone_match": 0.3,
                    "personalisation": 0.3,
                    "goal_alignment": 0.3,
                    "length_valid": 1.0,
                    "naturalness": 0.3,
                },
                "pass": False,
                "feedback": "Too generic.",
            },
        },
    )
    connector = FileConnector(outbox_path=prepared_campaign["outbox_path"])

    with fresh_db() as session:
        result = await run_outreach_for_campaign_lead(
            session=session,
            connector=connector,
            workspace=session.get(Workspace, prepared_campaign["workspace_id"]),
            campaign=session.get(Campaign, prepared_campaign["campaign_id"]),
            campaign_lead=session.get(CampaignLead, prepared_campaign["campaign_lead_id"]),
            lead=session.get(Lead, prepared_campaign["lead_id"]),
        )

    assert not result.sent
    assert result.reason == "eval_failed"
    assert result.attempts == 3

    with fresh_db() as session:
        thread = session.query(Thread).one()
        cl = session.get(CampaignLead, prepared_campaign["campaign_lead_id"])
        assert cl.status == CampaignLeadStatus.PAUSED_FOR_HITL
        assert thread.status == ThreadStatus.PAUSED_FOR_HITL
        assert thread.hitl_reason == "eval_failed_after_max_attempts"
        assert len(thread.hitl_context["last_drafts"]) == 3
        assert len(thread.hitl_context["last_scores"]) == 3
        # No message row written for rejected drafts.
        assert session.query(Message).count() == 0


async def test_outreach_rejects_message_over_max_length(
    prepared_campaign, fresh_db, monkeypatch
):
    """length_valid is recomputed from the draft itself, not trusted from the LLM."""

    from autosdr.prompts.evaluation import MAX_SMS_LENGTH

    long_draft = "x" * (MAX_SMS_LENGTH + 10)
    _install_mock_llm(
        monkeypatch,
        responses={
            "analysis-v3.3": {"angle": "y", "signal": "z", "confidence": 0.6},
            "generation-v6": long_draft,
            "evaluation-v4.2": {
                "scores": {
                    "tone_match": 1.0,
                    "personalisation": 1.0,
                    "goal_alignment": 1.0,
                    "length_valid": 1.0,  # model lying
                    "naturalness": 1.0,
                },
                "pass": True,
                "feedback": "",
            },
        },
    )
    connector = FileConnector(outbox_path=prepared_campaign["outbox_path"])

    with fresh_db() as session:
        result = await run_outreach_for_campaign_lead(
            session=session,
            connector=connector,
            workspace=session.get(Workspace, prepared_campaign["workspace_id"]),
            campaign=session.get(Campaign, prepared_campaign["campaign_id"]),
            campaign_lead=session.get(CampaignLead, prepared_campaign["campaign_lead_id"]),
            lead=session.get(Lead, prepared_campaign["lead_id"]),
        )

    assert not result.sent
    assert result.reason == "eval_failed"


async def test_outreach_skips_when_campaign_lead_already_claimed(
    prepared_campaign, fresh_db
):
    with fresh_db() as session:
        cl = session.get(CampaignLead, prepared_campaign["campaign_lead_id"])
        cl.status = CampaignLeadStatus.SENDING
        session.flush()

    connector = FileConnector(outbox_path=prepared_campaign["outbox_path"])
    with fresh_db() as session:
        result = await run_outreach_for_campaign_lead(
            session=session,
            connector=connector,
            workspace=session.get(Workspace, prepared_campaign["workspace_id"]),
            campaign=session.get(Campaign, prepared_campaign["campaign_id"]),
            campaign_lead=session.get(
                CampaignLead, prepared_campaign["campaign_lead_id"]
            ),
            lead=session.get(Lead, prepared_campaign["lead_id"]),
        )

    assert not result.sent
    assert result.reason == "campaign_lead_not_queued:sending"
    assert not prepared_campaign["outbox_path"].exists()


async def test_outreach_marks_existing_outbound_contacted_without_resending(
    prepared_campaign, fresh_db
):
    with fresh_db() as session:
        cl = session.get(CampaignLead, prepared_campaign["campaign_lead_id"])
        thread = Thread(
            campaign_lead_id=cl.id,
            connector_type="android_sms",
            status=ThreadStatus.ACTIVE,
            angle="x",
            tone_snapshot="x",
        )
        session.add(thread)
        session.flush()
        session.add(
            Message(
                thread_id=thread.id,
                role=MessageRole.AI,
                content="previous outbound",
                metadata_={},
            )
        )
        session.flush()

    connector = FileConnector(outbox_path=prepared_campaign["outbox_path"])
    with fresh_db() as session:
        result = await run_outreach_for_campaign_lead(
            session=session,
            connector=connector,
            workspace=session.get(Workspace, prepared_campaign["workspace_id"]),
            campaign=session.get(Campaign, prepared_campaign["campaign_id"]),
            campaign_lead=session.get(
                CampaignLead, prepared_campaign["campaign_lead_id"]
            ),
            lead=session.get(Lead, prepared_campaign["lead_id"]),
        )

    assert not result.sent
    assert result.reason == "existing_outbound_message"
    assert not prepared_campaign["outbox_path"].exists()
    with fresh_db() as session:
        cl = session.get(CampaignLead, prepared_campaign["campaign_lead_id"])
        lead = session.get(Lead, prepared_campaign["lead_id"])
        assert cl.status == CampaignLeadStatus.CONTACTED
        assert lead.status == LeadStatus.CONTACTED
        assert session.query(Message).count() == 1


async def test_outreach_does_not_claim_inactive_thread(prepared_campaign, fresh_db):
    with fresh_db() as session:
        cl = session.get(CampaignLead, prepared_campaign["campaign_lead_id"])
        thread = Thread(
            campaign_lead_id=cl.id,
            connector_type="android_sms",
            status=ThreadStatus.PAUSED_FOR_HITL,
            angle="x",
            tone_snapshot="x",
        )
        session.add(thread)
        session.flush()

    connector = FileConnector(outbox_path=prepared_campaign["outbox_path"])
    with fresh_db() as session:
        result = await run_outreach_for_campaign_lead(
            session=session,
            connector=connector,
            workspace=session.get(Workspace, prepared_campaign["workspace_id"]),
            campaign=session.get(Campaign, prepared_campaign["campaign_id"]),
            campaign_lead=session.get(
                CampaignLead, prepared_campaign["campaign_lead_id"]
            ),
            lead=session.get(Lead, prepared_campaign["lead_id"]),
        )

    assert not result.sent
    assert result.reason == "thread_not_active:paused_for_hitl"
    with fresh_db() as session:
        cl = session.get(CampaignLead, prepared_campaign["campaign_lead_id"])
        assert cl.status == CampaignLeadStatus.PAUSED_FOR_HITL


async def test_outreach_rejects_mismatched_campaign_lead_and_lead(
    prepared_campaign, fresh_db
):
    with fresh_db() as session:
        other = Lead(
            workspace_id=prepared_campaign["workspace_id"],
            name="Wrong Lead",
            contact_uri="+61400000002",
            contact_type="mobile",
            category="Retail",
            address="Brisbane",
            raw_data={},
            import_order=2,
            source_file="test.csv",
            status=LeadStatus.NEW,
        )
        session.add(other)
        session.flush()
        other_id = other.id

    connector = FileConnector(outbox_path=prepared_campaign["outbox_path"])
    with fresh_db() as session:
        result = await run_outreach_for_campaign_lead(
            session=session,
            connector=connector,
            workspace=session.get(Workspace, prepared_campaign["workspace_id"]),
            campaign=session.get(Campaign, prepared_campaign["campaign_id"]),
            campaign_lead=session.get(
                CampaignLead, prepared_campaign["campaign_lead_id"]
            ),
            lead=session.get(Lead, other_id),
        )

    assert not result.sent
    assert result.reason == "lead_mismatch"
    assert not prepared_campaign["outbox_path"].exists()


async def test_outreach_requeues_claim_when_analysis_crashes_before_send(
    prepared_campaign, fresh_db, monkeypatch
):
    async def fake_run_analysis(**kwargs):
        raise RuntimeError("analysis exploded")

    monkeypatch.setattr("autosdr.pipeline.outreach._run_analysis", fake_run_analysis)

    connector = FileConnector(outbox_path=prepared_campaign["outbox_path"])
    with pytest.raises(RuntimeError, match="analysis exploded"):
        with fresh_db() as session:
            await run_outreach_for_campaign_lead(
                session=session,
                connector=connector,
                workspace=session.get(Workspace, prepared_campaign["workspace_id"]),
                campaign=session.get(Campaign, prepared_campaign["campaign_id"]),
                campaign_lead=session.get(
                    CampaignLead, prepared_campaign["campaign_lead_id"]
                ),
                lead=session.get(Lead, prepared_campaign["lead_id"]),
            )

    assert not prepared_campaign["outbox_path"].exists()
    with fresh_db() as session:
        cl = session.get(CampaignLead, prepared_campaign["campaign_lead_id"])
        assert cl.status == CampaignLeadStatus.QUEUED
        assert session.query(Message).count() == 0


async def test_outreach_pauses_campaign_lead_when_connector_send_fails(
    prepared_campaign, fresh_db, monkeypatch
):
    _install_mock_llm(
        monkeypatch,
        responses={
            "analysis-v3.3": {"angle": "x", "signal": "y", "confidence": 0.7},
            "generation-v6": "hey, quick chat?",
            "evaluation-v4.2": {
                "scores": {
                    "tone_match": 0.9,
                    "personalisation": 0.9,
                    "goal_alignment": 0.9,
                    "length_valid": 1.0,
                    "naturalness": 0.9,
                },
                "pass": True,
                "feedback": "",
            },
        },
    )

    connector = FileConnector(outbox_path=prepared_campaign["outbox_path"])

    async def fake_send(message):
        return SendResult(success=False, error="forced_failure")

    monkeypatch.setattr(connector, "send", fake_send)

    with fresh_db() as session:
        result = await run_outreach_for_campaign_lead(
            session=session,
            connector=connector,
            workspace=session.get(Workspace, prepared_campaign["workspace_id"]),
            campaign=session.get(Campaign, prepared_campaign["campaign_id"]),
            campaign_lead=session.get(
                CampaignLead, prepared_campaign["campaign_lead_id"]
            ),
            lead=session.get(Lead, prepared_campaign["lead_id"]),
        )

    assert not result.sent
    assert result.reason == "connector_failed:forced_failure"
    with fresh_db() as session:
        cl = session.get(CampaignLead, prepared_campaign["campaign_lead_id"])
        thread = session.query(Thread).one()
        assert cl.status == CampaignLeadStatus.PAUSED_FOR_HITL
        assert thread.status == ThreadStatus.PAUSED_FOR_HITL
        assert session.query(Message).count() == 0


async def test_outreach_skips_if_contact_uri_changes_before_send(
    prepared_campaign, fresh_db, monkeypatch
):
    _install_mock_llm(
        monkeypatch,
        responses={
            "analysis-v3.3": {"angle": "x", "signal": "y", "confidence": 0.7},
            "generation-v6": "unused",
            "evaluation-v4.2": {
                "scores": {
                    "tone_match": 0.9,
                    "personalisation": 0.9,
                    "goal_alignment": 0.9,
                    "length_valid": 1.0,
                    "naturalness": 0.9,
                },
                "pass": True,
                "feedback": "",
            },
        },
    )

    async def fake_generate_and_evaluate(**kwargs):
        with fresh_db() as session:
            lead = session.get(Lead, prepared_campaign["lead_id"])
            lead.contact_uri = "+61400009999"
            session.flush()
        return {
            "status": "pass",
            "draft": "hey, quick one?",
            "attempts": 1,
            "overall": 0.9,
            "scores": {},
            "feedback": "",
        }

    monkeypatch.setattr(
        "autosdr.pipeline.outreach.generate_and_evaluate",
        fake_generate_and_evaluate,
    )

    connector = FileConnector(outbox_path=prepared_campaign["outbox_path"])
    with fresh_db() as session:
        result = await run_outreach_for_campaign_lead(
            session=session,
            connector=connector,
            workspace=session.get(Workspace, prepared_campaign["workspace_id"]),
            campaign=session.get(Campaign, prepared_campaign["campaign_id"]),
            campaign_lead=session.get(
                CampaignLead, prepared_campaign["campaign_lead_id"]
            ),
            lead=session.get(Lead, prepared_campaign["lead_id"]),
        )

    assert not result.sent
    assert result.reason == "lead_contact_uri_changed_before_send"
    assert not prepared_campaign["outbox_path"].exists()
    with fresh_db() as session:
        cl = session.get(CampaignLead, prepared_campaign["campaign_lead_id"])
        assert cl.status == CampaignLeadStatus.QUEUED


# ---------------------------------------------------------------------------
# Compliance — do_not_contact guard
# ---------------------------------------------------------------------------


async def test_outreach_skips_do_not_contact(prepared_campaign, fresh_db, monkeypatch):
    """A queued CampaignLead whose Lead is flagged DNC must not trigger an LLM call or a send."""

    from datetime import datetime, timezone

    with fresh_db() as session:
        lead = session.get(Lead, prepared_campaign["lead_id"])
        lead.do_not_contact_at = datetime.now(timezone.utc)
        lead.do_not_contact_reason = "opt_out:STOP"
        session.flush()

    async def _refuse(**_kwargs):
        raise AssertionError("LLM must not be invoked for a do-not-contact lead")

    monkeypatch.setattr("autosdr.pipeline.outreach.complete_json", _refuse)
    monkeypatch.setattr("autosdr.pipeline._shared.complete_json", _refuse)
    monkeypatch.setattr("autosdr.pipeline._shared.complete_text", _refuse)

    connector = FileConnector(outbox_path=prepared_campaign["outbox_path"])
    with fresh_db() as session:
        result = await run_outreach_for_campaign_lead(
            session=session,
            connector=connector,
            workspace=session.get(Workspace, prepared_campaign["workspace_id"]),
            campaign=session.get(Campaign, prepared_campaign["campaign_id"]),
            campaign_lead=session.get(
                CampaignLead, prepared_campaign["campaign_lead_id"]
            ),
            lead=session.get(Lead, prepared_campaign["lead_id"]),
        )

    assert not result.sent
    assert result.reason == "do_not_contact"
    assert not prepared_campaign["outbox_path"].exists()

    with fresh_db() as session:
        cl = session.get(CampaignLead, prepared_campaign["campaign_lead_id"])
        lead = session.get(Lead, prepared_campaign["lead_id"])
        assert cl.status == CampaignLeadStatus.SKIPPED
        assert lead.skip_reason == "do_not_contact"
        # No thread, no AI message — we never burned an analysis call.
        assert session.query(Thread).count() == 0
        assert session.query(Message).count() == 0


async def test_outreach_aborts_when_lead_opts_out_during_pipeline(
    prepared_campaign, fresh_db, monkeypatch
):
    """Race window: lead opts out *between* claim and send. Send must be aborted."""

    from datetime import datetime, timezone

    _install_mock_llm(
        monkeypatch,
        responses={
            "analysis-v3.3": {
                "angle": "Rating 3 — opportunity",
                "signal": "rating",
                "confidence": 0.7,
            },
            "generation-v6": "hey, quick one?",
            "evaluation-v4.2": {
                "scores": {
                    "tone_match": 0.9,
                    "personalisation": 0.9,
                    "goal_alignment": 0.9,
                    "length_valid": 1.0,
                    "naturalness": 0.9,
                },
                "pass": True,
                "feedback": "",
            },
        },
    )

    async def fake_generate_and_evaluate(**_kwargs):
        # Simulate inbound STOP firing while we're mid-loop: the inbound
        # handler stamps DNC on the Lead row in another session.
        with fresh_db() as session:
            lead = session.get(Lead, prepared_campaign["lead_id"])
            lead.do_not_contact_at = datetime.now(timezone.utc)
            lead.do_not_contact_reason = "opt_out:STOP"
            session.flush()
        return {
            "status": "pass",
            "draft": "hey, quick one?",
            "attempts": 1,
            "overall": 0.9,
            "scores": {},
            "feedback": "",
        }

    monkeypatch.setattr(
        "autosdr.pipeline.outreach.generate_and_evaluate", fake_generate_and_evaluate
    )

    connector = FileConnector(outbox_path=prepared_campaign["outbox_path"])
    with fresh_db() as session:
        result = await run_outreach_for_campaign_lead(
            session=session,
            connector=connector,
            workspace=session.get(Workspace, prepared_campaign["workspace_id"]),
            campaign=session.get(Campaign, prepared_campaign["campaign_id"]),
            campaign_lead=session.get(
                CampaignLead, prepared_campaign["campaign_lead_id"]
            ),
            lead=session.get(Lead, prepared_campaign["lead_id"]),
        )

    assert not result.sent
    assert result.reason == "do_not_contact"
    assert not prepared_campaign["outbox_path"].exists()
    with fresh_db() as session:
        cl = session.get(CampaignLead, prepared_campaign["campaign_lead_id"])
        assert cl.status == CampaignLeadStatus.SKIPPED
        # Thread was created during the analysis stage (before the race) — that's fine.
        assert session.query(Message).count() == 0
