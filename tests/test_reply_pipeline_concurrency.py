"""Reply pipeline — concurrency / event-loop responsiveness.

Ticket 0008's contract: the pipeline must not hold a SQLite write
transaction across an LLM ``await``. The AST lint test
(:mod:`tests.test_no_await_in_session`) catches the structural pattern;
the tests here exercise the runtime consequence: a concurrent writer
sharing the database completes promptly while the pipeline is mid-LLM-
call, instead of waiting the full ``PRAGMA busy_timeout`` for the
pipeline's outer transaction to commit.

Pre-fix (pipeline held the session): a competing writer waited up to
~120 000 ms for the lock. Post-fix: a competing writer waits at most
the duration of a Phase 1/3 commit (single INSERT/UPDATE,
microseconds). The bounds asserted here are loose enough to be
reliable on slow CI runners while still being orders of magnitude
below the pre-fix latency.
"""

from __future__ import annotations

import asyncio
import time

import pytest

from autosdr.connectors.base import IncomingMessage
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
    Thread,
    ThreadStatus,
    UnmatchedWebhook,
    Workspace,
)
from autosdr.pipeline import process_incoming_message


@pytest.fixture
def active_thread(fresh_db, workspace_factory, tmp_path):
    """Mirror of the active_thread fixture in ``test_reply_pipeline``.

    Re-declared here so the concurrency tests aren't coupled to import-
    order side-effects from the legacy module.
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


def _patch_llm_with_delay(monkeypatch: pytest.MonkeyPatch, delay_s: float) -> None:
    """Stub the LLM with an artificial ``await asyncio.sleep`` delay.

    The delay simulates a real LLM round-trip (typically 500-3000 ms in
    production). During this delay any code that holds a SQLite write
    transaction would block competing writers — the property under
    test.
    """

    async def _fake_complete_json(
        *,
        system,
        user,
        model,
        prompt_version,
        temperature=0.0,
        context=None,
        **_kwargs,
    ):
        await asyncio.sleep(delay_s)
        payload = {"intent": "negative", "confidence": 0.95, "reason": "no thanks"}
        return payload, CompletionResult(
            text=str(payload),
            model=model,
            prompt_version=prompt_version,
            tokens_in=5,
            tokens_out=5,
            attempts=1,
            latency_ms=int(delay_s * 1000),
        )

    async def _fake_complete_text(
        *,
        system,
        user,
        model,
        prompt_version,
        temperature,
        context=None,
        **_kwargs,
    ):
        await asyncio.sleep(delay_s)
        return CompletionResult(
            text="ok",
            model=model,
            prompt_version=prompt_version,
            tokens_in=5,
            tokens_out=5,
            attempts=1,
            latency_ms=int(delay_s * 1000),
        )

    monkeypatch.setattr("autosdr.pipeline.reply.complete_json", _fake_complete_json)
    monkeypatch.setattr("autosdr.pipeline._shared.complete_text", _fake_complete_text)
    monkeypatch.setattr("autosdr.pipeline._shared.complete_json", _fake_complete_json)
    # ``suggestions.py`` imports ``complete_text`` directly from the
    # public ``autosdr.llm`` package re-export — patching it inside
    # _shared/reply isn't enough.
    monkeypatch.setattr("autosdr.pipeline.suggestions.complete_text", _fake_complete_text)


async def test_competing_writer_not_blocked_during_pipeline_llm_call(
    active_thread, fresh_db, monkeypatch
):
    """A concurrent writer completes promptly during the LLM await.

    Pre-ticket-0008 the pipeline held a ``session_scope()`` for the
    full duration of ``classify → generate → evaluate`` (~30-60 s in
    prod, ~0.3 s here). A competing INSERT — for example the webhook
    handler stashing an :class:`UnmatchedWebhook` from a different
    contact — would queue on the writer lock and wait up to the full
    ``PRAGMA busy_timeout``. Post-fix the pipeline phases its work so
    no session is held across the LLM ``await``; competing writers
    serialise only on the small Phase 1/3 commits.
    """

    _patch_llm_with_delay(monkeypatch, delay_s=0.4)

    connector = FileConnector(outbox_path=active_thread["outbox_path"])
    incoming = IncomingMessage(
        contact_uri="+61400000001", content="No thanks, not interested."
    )

    pipeline_task = asyncio.create_task(
        process_incoming_message(
            connector=connector,
            workspace_id=active_thread["workspace_id"],
            incoming=incoming,
        )
    )

    # Yield long enough that the pipeline has reached at least one LLM
    # ``await`` (Phase 1 commit + classify dispatch < 50 ms, classify
    # itself sleeps 400 ms).
    await asyncio.sleep(0.1)

    write_durations: list[float] = []
    deadline = time.monotonic() + 0.25
    counter = 0
    while time.monotonic() < deadline:
        start = time.monotonic()
        with fresh_db() as session:
            session.add(
                UnmatchedWebhook(
                    workspace_id=active_thread["workspace_id"],
                    connector_type="android_sms",
                    sender_uri=f"+614000000{counter:02d}",
                    reason="probe",
                    raw_payload={"content": "probe"},
                )
            )
            session.flush()
        write_durations.append(time.monotonic() - start)
        counter += 1
        await asyncio.sleep(0.02)

    result = await pipeline_task
    assert result.action == "closed_lost", (
        f"pipeline failed mid-test: {result.action}"
    )

    assert write_durations, "probe loop never ran"
    slowest = max(write_durations)
    assert slowest < 0.5, (
        "competing writer waited too long during pipeline LLM call: "
        f"{slowest * 1000:.0f}ms (expected < 500ms; pre-fix was "
        f"≤ 120000ms = busy_timeout)"
    )
    median = sorted(write_durations)[len(write_durations) // 2]
    assert median < 0.05, (
        f"median competing-writer latency too high: {median * 1000:.0f}ms"
    )


async def test_concurrent_inbounds_complete_without_busy_timeout(
    active_thread, fresh_db, monkeypatch, workspace_factory, tmp_path
):
    """Multiple inbound webhooks land concurrently without timing out.

    Each ``process_incoming_message`` call has its own LLM round-trip;
    pre-ticket-0008 they would each hold a writer lock for the full
    pipeline duration and serialise behind the busy_timeout. Post-fix
    they share the writer lock only for the brief commit phases and
    overlap freely on the LLM ``await``.

    The wall-clock budget asserts that two concurrent calls finish in
    roughly the duration of one (~delay), not two (~2*delay).
    """

    _patch_llm_with_delay(monkeypatch, delay_s=0.2)

    # A second active thread on a different contact, so the two
    # process_incoming_message calls don't deadlock on the same row.
    with fresh_db() as session:
        ws = session.get(Workspace, active_thread["workspace_id"])
        campaign = (
            session.query(Campaign)
            .filter(Campaign.workspace_id == ws.id)
            .first()
        )
        lead2 = Lead(
            workspace_id=ws.id,
            name="Tester 2",
            contact_uri="+61400000002",
            contact_type="mobile",
            category="Retail",
            address="Sydney",
            raw_data={},
            import_order=2,
            source_file="x",
            status=LeadStatus.CONTACTED,
        )
        session.add(lead2)
        session.flush()
        cl2 = CampaignLead(
            campaign_id=campaign.id,
            lead_id=lead2.id,
            queue_position=2,
            status=CampaignLeadStatus.CONTACTED,
        )
        session.add(cl2)
        session.flush()
        thread2 = Thread(
            campaign_lead_id=cl2.id,
            connector_type="android_sms",
            status=ThreadStatus.ACTIVE,
            angle="existing angle",
            tone_snapshot="direct, casual",
        )
        session.add(thread2)
        session.flush()
        session.add(
            Message(
                thread_id=thread2.id,
                role=MessageRole.AI,
                content="Hey — interested in a 15 min chat?",
                metadata_={},
            )
        )
        session.flush()

    connector = FileConnector(outbox_path=active_thread["outbox_path"])
    inbound1 = IncomingMessage(
        contact_uri="+61400000001", content="Not for me, thanks."
    )
    inbound2 = IncomingMessage(
        contact_uri="+61400000002", content="No thanks, please don't message again."
    )

    start = time.monotonic()
    results = await asyncio.gather(
        process_incoming_message(
            connector=connector,
            workspace_id=active_thread["workspace_id"],
            incoming=inbound1,
        ),
        process_incoming_message(
            connector=connector,
            workspace_id=active_thread["workspace_id"],
            incoming=inbound2,
        ),
    )
    elapsed = time.monotonic() - start

    assert all(r.action == "closed_lost" for r in results), [r.action for r in results]
    # Both inbounds run a single LLM call (classify) at delay_s=0.2.
    # Concurrent: ~0.2s. Serial pre-fix (busy_timeout-bound): ≥ 5s.
    assert elapsed < 1.0, (
        "two concurrent inbounds took too long; pipeline likely "
        f"serialised on the writer lock: {elapsed:.2f}s"
    )
