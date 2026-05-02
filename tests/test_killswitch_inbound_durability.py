"""Killswitch must not silently drop inbound webhooks.

Pre-ticket-0009 the webhook handler dropped inbounds while the
killswitch was on. This file pins the ticket's three durability
guarantees:

1. With killswitch ON, an inbound webhook results in a
   :class:`~autosdr.models.PausedInbound` row (not a silent drop).
2. After ``POST /api/status/resume``, the row replays through
   :func:`process_incoming_message` and the thread reaches the
   expected post-classification state.
3. STOP / opt-out keywords arriving during pause are honoured on
   resume — ticket 0001's deterministic-shortcut promise must hold
   across a pause window.

These run end-to-end via FastAPI's :class:`TestClient` so they
exercise the actual webhook → background-task → drain → reply
pipeline path.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from fastapi.testclient import TestClient

from autosdr import killswitch
from autosdr.connectors.file_connector import FileConnector
from autosdr.llm.client import CompletionResult
from autosdr.models import (
    Campaign,
    CampaignLead,
    CampaignLeadStatus,
    CampaignStatus,
    Lead,
    LeadStatus,
    Message,
    MessageRole,
    PausedInbound,
    Thread,
    ThreadStatus,
    Workspace,
)
from autosdr.pipeline import replay as replay_module
from autosdr.webhook import create_app


def _client() -> TestClient:
    """Test client without the scheduler — webhooks + status endpoints only."""

    return TestClient(
        create_app(run_scheduler_task=False), raise_server_exceptions=False
    )


@pytest.fixture
def active_thread(fresh_db, workspace_factory):
    """Workspace + active thread + one prior AI outbound, ready to receive a reply."""

    ws_id = workspace_factory(settings_overrides={"auto_reply_enabled": True})

    with fresh_db() as session:
        ws = session.get(Workspace, ws_id)
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
            connector_type="file",
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
        }


def _patch_llm(monkeypatch: pytest.MonkeyPatch, responses: dict[str, Any]) -> None:
    async def _fake_complete_text(
        *, system, user, model, prompt_version, temperature, context=None, **_kwargs
    ):
        payload = responses.get(prompt_version)
        if isinstance(payload, list):
            payload = payload.pop(0)
        return CompletionResult(
            text=payload,
            model=model,
            prompt_version=prompt_version,
            tokens_in=5,
            tokens_out=5,
            attempts=1,
            latency_ms=1,
        )

    async def _fake_complete_json(
        *, system, user, model, prompt_version, temperature=0.0, context=None, **_kwargs
    ):
        payload = responses.get(prompt_version)
        if isinstance(payload, list):
            payload = payload.pop(0)
        return payload, CompletionResult(
            text=str(payload),
            model=model,
            prompt_version=prompt_version,
            tokens_in=5,
            tokens_out=5,
            attempts=1,
            latency_ms=1,
        )

    monkeypatch.setattr("autosdr.pipeline.reply.complete_json", _fake_complete_json)
    monkeypatch.setattr("autosdr.pipeline._shared.complete_text", _fake_complete_text)
    monkeypatch.setattr("autosdr.pipeline._shared.complete_json", _fake_complete_json)
    monkeypatch.setattr("autosdr.pipeline.suggestions.complete_text", _fake_complete_text)


# ---------------------------------------------------------------------------
# Case 1 — webhook persists rather than drops while paused.
# ---------------------------------------------------------------------------


def test_webhook_sim_queues_paused_inbound_when_killswitch_on(
    active_thread, fresh_db, monkeypatch
):
    """``POST /api/webhooks/sim`` while paused → ``paused_inbound`` row."""

    _patch_llm(
        monkeypatch,
        {"classification-v1.1": {"intent": "negative", "confidence": 0.9, "reason": "no"}},
    )

    killswitch.touch_flag()
    try:
        with _client() as client:
            resp = client.post(
                "/api/webhooks/sim",
                json={"contact_uri": "+61400000001", "content": "Not for me, thanks."},
            )
            assert resp.status_code == 202

        with fresh_db() as session:
            rows = session.query(PausedInbound).all()
            assert len(rows) == 1, "killswitch ON should queue, not drop"
            row = rows[0]
            assert row.contact_uri == "+61400000001"
            assert row.content == "Not for me, thanks."
            assert row.replayed_at is None

            messages = session.query(Message).all()
            inbound_messages = [m for m in messages if m.role == MessageRole.LEAD]
            assert inbound_messages == [], (
                "the inbound Message row is created by replay, not by the "
                "queue insert — pre-replay there should be no LEAD message"
            )
    finally:
        killswitch.remove_flag()


def test_webhook_sms_queues_paused_inbound_when_killswitch_on(
    active_thread, fresh_db, monkeypatch
):
    """``POST /api/webhooks/sms`` (real connector path) also queues, not drops."""

    _patch_llm(
        monkeypatch,
        {"classification-v1.1": {"intent": "negative", "confidence": 0.9, "reason": "no"}},
    )

    # The default test connector is FileConnector — its parse_webhook
    # accepts the same simple shape as the simulator. We're not
    # exercising the simulator path here; the goal is the SMS handler.
    killswitch.touch_flag()
    try:
        with _client() as client:
            resp = client.post(
                "/api/webhooks/sms",
                json={"contact_uri": "+61400000001", "content": "No thanks."},
            )
            assert resp.status_code == 202

        with fresh_db() as session:
            rows = session.query(PausedInbound).all()
            assert len(rows) == 1
            assert rows[0].contact_uri == "+61400000001"
    finally:
        killswitch.remove_flag()


# ---------------------------------------------------------------------------
# Case 2 — resume drains the queue and the thread reaches its post-reply state.
# ---------------------------------------------------------------------------


async def test_resume_drains_queue_into_reply_pipeline(
    active_thread, fresh_db, monkeypatch
):
    """Pause → queue an inbound → resume → drain runs → thread closes lost."""

    _patch_llm(
        monkeypatch,
        {
            "classification-v1.1": {
                "intent": "negative",
                "confidence": 0.95,
                "reason": "no thanks",
            }
        },
    )

    killswitch.touch_flag()
    try:
        with _client() as client:
            resp = client.post(
                "/api/webhooks/sim",
                json={"contact_uri": "+61400000001", "content": "Nah, not for me."},
            )
            assert resp.status_code == 202

        with fresh_db() as session:
            assert session.query(PausedInbound).count() == 1

        # The TestClient's lifespan ``finally`` block calls
        # ``killswitch.mark_shutting_down()`` which sets the
        # process-wide ``_hard_stop`` flag — that's the right
        # behaviour in production (uvicorn wants every coroutine to
        # abort during graceful shutdown), but it bleeds into this
        # test because the next phase (``drain_paused_inbounds``)
        # runs in the same Python process. Reset deliberately AND
        # remove the flag file before the drain so the replay path
        # sees the system as fully resumed.
        killswitch.reset_for_tests()
        killswitch.remove_flag()
        summary = await replay_module.drain_paused_inbounds()
        assert summary == {"replayed": 1, "skipped": 0, "failed": 0}

        with fresh_db() as session:
            row = session.query(PausedInbound).one()
            assert row.replayed_at is not None

            thread = session.get(Thread, active_thread["thread_id"])
            assert thread.status == ThreadStatus.LOST, (
                "negative intent should close the thread lost on replay"
            )

            cl = session.get(CampaignLead, active_thread["campaign_lead_id"])
            assert cl.status == CampaignLeadStatus.LOST

            inbound = (
                session.query(Message)
                .filter(Message.role == MessageRole.LEAD)
                .one()
            )
            assert inbound.content == "Nah, not for me."
    finally:
        killswitch.remove_flag()


# ---------------------------------------------------------------------------
# Case 3 — STOP during pause honours the deterministic opt-out shortcut on resume.
# ---------------------------------------------------------------------------


async def test_stop_during_pause_marks_do_not_contact_on_resume(
    active_thread, fresh_db, monkeypatch
):
    """STOP arriving during pause → lead.do_not_contact_at set on resume.

    This is ticket 0001's deterministic opt-out promise extended
    across a pause window. Pre-ticket-0009 the inbound was dropped,
    the lead remained opt-in, and the next outreach beat would send
    to a person who had unambiguously asked us to stop.
    """

    # The deterministic opt-out shortcut runs *before* the LLM
    # classifier, so the LLM stub here only matters as a guard against
    # accidentally falling through to a real provider call. Make it
    # raise loudly if reached — opt-out should never reach it.
    async def _explode(*args, **kwargs):
        raise AssertionError("LLM should not be called for STOP — deterministic shortcut")

    monkeypatch.setattr("autosdr.pipeline.reply.complete_json", _explode)

    killswitch.touch_flag()
    try:
        with _client() as client:
            resp = client.post(
                "/api/webhooks/sim",
                json={"contact_uri": "+61400000001", "content": "STOP"},
            )
            assert resp.status_code == 202

        with fresh_db() as session:
            assert session.query(PausedInbound).count() == 1
            lead = session.get(Lead, active_thread["lead_id"])
            assert lead.do_not_contact_at is None, (
                "while paused, the row is queued; the lead's opt-out "
                "flag flips on replay, not on insert"
            )

        # See the equivalent comment in
        # ``test_resume_drains_queue_into_reply_pipeline`` — the
        # TestClient's lifespan teardown sets ``_hard_stop`` which we
        # need to clear, and the flag file must be removed too.
        killswitch.reset_for_tests()
        killswitch.remove_flag()
        await replay_module.drain_paused_inbounds()

        with fresh_db() as session:
            lead = session.get(Lead, active_thread["lead_id"])
            assert lead.do_not_contact_at is not None, (
                "STOP arriving during pause must honour the "
                "deterministic opt-out shortcut on resume"
            )
            assert lead.do_not_contact_reason is not None
            assert "stop" in lead.do_not_contact_reason.lower()
    finally:
        killswitch.remove_flag()


# ---------------------------------------------------------------------------
# Case 4 — connector mismatch on resume → skip, don't replay.
# ---------------------------------------------------------------------------


async def test_drain_skips_connector_mismatch(
    active_thread, fresh_db, monkeypatch
):
    """If the active connector at resume differs from the queued one, skip."""

    _patch_llm(
        monkeypatch,
        {"classification-v1.1": {"intent": "negative", "confidence": 0.9, "reason": "no"}},
    )

    killswitch.touch_flag()
    try:
        with _client() as client:
            client.post(
                "/api/webhooks/sim",
                json={"contact_uri": "+61400000001", "content": "no"},
            )

        # Simulate the operator swapping connectors during the pause
        # window: the queued row was tagged "file" (the test default),
        # so we lie about the active connector type at drain time.
        class _FakeConnector(FileConnector):
            connector_type = "smsgate"

        from autosdr.pipeline import replay as replay_mod

        monkeypatch.setattr(
            replay_mod,
            "get_connector",
            lambda: _FakeConnector(outbox_path="/tmp/_replay_outbox.jsonl"),
        )

        killswitch.reset_for_tests()
        killswitch.remove_flag()
        summary = await replay_module.drain_paused_inbounds()
        assert summary["skipped"] == 1
        assert summary["replayed"] == 0

        with fresh_db() as session:
            row = session.query(PausedInbound).one()
            assert row.replayed_at is None, (
                "skipped rows must keep replayed_at NULL so the next "
                "resume retries them after the operator swaps back"
            )
    finally:
        killswitch.remove_flag()


# ---------------------------------------------------------------------------
# Case 5 — status endpoint exposes the queue depth.
# ---------------------------------------------------------------------------


def test_status_endpoint_surfaces_pending_count(active_thread, fresh_db, monkeypatch):
    """``GET /api/status`` must expose ``paused_inbound.pending_count``."""

    _patch_llm(
        monkeypatch,
        {"classification-v1.1": {"intent": "negative", "confidence": 0.9, "reason": "no"}},
    )

    killswitch.touch_flag()
    try:
        with _client() as client:
            for content in ("first", "second", "third"):
                resp = client.post(
                    "/api/webhooks/sim",
                    json={"contact_uri": "+61400000001", "content": content},
                )
                assert resp.status_code == 202

            status_body = client.get("/api/status").json()

        assert status_body["paused"] is True
        assert status_body["paused_inbound"]["pending_count"] == 3
        assert status_body["paused_inbound"]["oldest_pending_at"] is not None
    finally:
        killswitch.remove_flag()
