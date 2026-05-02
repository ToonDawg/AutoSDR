"""Tests for the follow-up reply suggestion flow.

Covers two contracts:

1. **Routing.** ``generate_reply_variants`` switches between the cold-
   outreach generate-then-evaluate loop and the follow-up single-call
   flow based on whether the thread already has a lead message in
   history. Anything with a lead reply → follow-up; first-touch only →
   outreach.

2. **Follow-up flow contents.** The follow-up flow uses the dedicated
   ``followup-reply-v1`` prompt (no mandatory credential line, no
   first-contact framing) and runs **no evaluator** — one
   ``complete_text`` call per variant, no ``complete_json`` for
   evaluation, suggestion dicts have ``overall=None`` / ``pass=None`` /
   ``scores=None``.

These tests are unit-scoped against ``generate_reply_variants`` directly
so they don't depend on the inbound webhook plumbing — that's covered
end-to-end in ``tests/test_reply_first_message_only.py``.
"""

from __future__ import annotations

from typing import Any

import pytest

from autosdr.models import (
    Campaign,
    CampaignLead,
    CampaignLeadStatus,
    CampaignStatus,
    Lead,
    LeadStatus,
    MessageRole,
    Thread,
    ThreadStatus,
    Workspace,
)
from autosdr.pipeline.suggestions import generate_reply_variants
from autosdr.prompts import followup_reply


@pytest.fixture
def thread_with_lead_reply(fresh_db, workspace_factory):
    """Active thread that already has one outbound + one inbound lead reply."""

    ws_id = workspace_factory()

    with fresh_db() as session:
        ws = session.get(Workspace, ws_id)
        campaign = Campaign(
            workspace_id=ws.id,
            name="C",
            goal="Get them to text back",
            outreach_per_day=5,
            connector_type="android_sms",
            status=CampaignStatus.ACTIVE,
        )
        session.add(campaign)
        session.flush()

        lead = Lead(
            workspace_id=ws.id,
            name="Sunny Daycare",
            contact_uri="+61400000003",
            contact_type="mobile",
            category="Childcare",
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
            angle="stale_info: their google listing still has the old name",
            tone_snapshot="direct, casual aussie",
        )
        session.add(thread)
        session.flush()

        history = [
            {"role": MessageRole.AI, "content": "hey, saw your listing still says the old name"},
            {"role": MessageRole.LEAD, "content": "no thanks, all sorted on our end"},
        ]

        return {
            "workspace": ws,
            "campaign": campaign,
            "lead": lead,
            "thread": thread,
            "history": history,
        }


def _patch_complete_text(monkeypatch, responses: list[str], capture: list[dict[str, Any]]):
    """Patch ``complete_text`` in the suggestions module.

    ``responses`` is consumed in order — one entry per variant. ``capture``
    is appended to with each call's kwargs so tests can assert on prompt
    content.
    """

    from autosdr.llm.client import CompletionResult

    queue = list(responses)

    async def _fake(
        *, system, user, model, prompt_version, temperature, context=None, **_kwargs
    ):
        capture.append(
            {
                "system": system,
                "user": user,
                "model": model,
                "prompt_version": prompt_version,
                "temperature": temperature,
            }
        )
        text = queue.pop(0) if queue else "(no canned response left)"
        return CompletionResult(
            text=text,
            model=model,
            prompt_version=prompt_version,
            tokens_in=5,
            tokens_out=5,
            attempts=1,
            latency_ms=1,
            llm_call_id=f"call-{len(capture)}",
        )

    monkeypatch.setattr("autosdr.pipeline.suggestions.complete_text", _fake)


def _no_complete_json(monkeypatch):
    """Fail the test if any ``complete_json`` runs in the follow-up flow.

    The follow-up flow must not call the evaluator. Patching to raise
    catches any regression that wires ``generate_reply_variants`` back
    into the audit loop on a thread with a lead reply.
    """

    async def _refuse(**_kwargs):
        raise AssertionError(
            "complete_json should not run in the follow-up suggestion flow "
            "(no evaluator allowed)"
        )

    monkeypatch.setattr("autosdr.pipeline._shared.complete_json", _refuse)
    monkeypatch.setattr("autosdr.pipeline.suggestions.generate_and_evaluate", _refuse)


# ---------------------------------------------------------------------------
# Routing
# ---------------------------------------------------------------------------


async def test_thread_with_lead_reply_uses_followup_flow(
    thread_with_lead_reply, monkeypatch
):
    """Any lead message in history routes through the follow-up generator."""

    capture: list[dict[str, Any]] = []
    _patch_complete_text(monkeypatch, ["yeah no worries", "all good", "fair enough"], capture)
    _no_complete_json(monkeypatch)

    suggestions = await generate_reply_variants(
        workspace=thread_with_lead_reply["workspace"],
        campaign=thread_with_lead_reply["campaign"],
        lead=thread_with_lead_reply["lead"],
        thread=thread_with_lead_reply["thread"],
        history=thread_with_lead_reply["history"],
        n=3,
    )

    assert len(suggestions) == 3
    # All three calls used the follow-up prompt version, not the
    # cold-outreach generation prompt.
    assert {c["prompt_version"] for c in capture} == {followup_reply.PROMPT_VERSION}
    # Drafts came back stripped and intact.
    assert [s["draft"] for s in suggestions] == [
        "yeah no worries",
        "all good",
        "fair enough",
    ]


async def test_followup_suggestions_carry_no_eval_metadata(
    thread_with_lead_reply, monkeypatch
):
    """No evaluator ran, so eval-shaped fields stay null in the suggestion."""

    capture: list[dict[str, Any]] = []
    _patch_complete_text(monkeypatch, ["yeah", "sure", "ok"], capture)
    _no_complete_json(monkeypatch)

    suggestions = await generate_reply_variants(
        workspace=thread_with_lead_reply["workspace"],
        campaign=thread_with_lead_reply["campaign"],
        lead=thread_with_lead_reply["lead"],
        thread=thread_with_lead_reply["thread"],
        history=thread_with_lead_reply["history"],
        n=3,
    )

    for s in suggestions:
        assert s["overall"] is None
        assert s["scores"] is None
        assert s["pass"] is None
        assert s["feedback"] is None
        assert s["eval_llm_call_id"] is None
        # Audit row id from the gen call still flows through so the UI
        # can deep-link to the LLM call log.
        assert s["gen_llm_call_id"]
        assert s["source"] == "followup"
        assert s["attempts"] == 1


async def test_followup_variants_use_distinct_temperatures(
    thread_with_lead_reply, monkeypatch
):
    """Three variants ask for three different temperatures.

    The temperature spread is what makes the variants meaningfully
    different rather than three near-identical drafts.
    """

    capture: list[dict[str, Any]] = []
    _patch_complete_text(monkeypatch, ["a", "b", "c"], capture)
    _no_complete_json(monkeypatch)

    await generate_reply_variants(
        workspace=thread_with_lead_reply["workspace"],
        campaign=thread_with_lead_reply["campaign"],
        lead=thread_with_lead_reply["lead"],
        thread=thread_with_lead_reply["thread"],
        history=thread_with_lead_reply["history"],
        n=3,
    )

    temps = [c["temperature"] for c in capture]
    assert len(set(temps)) == 3, f"expected 3 distinct temperatures, got {temps}"


async def test_followup_drops_failed_and_empty_variants(
    thread_with_lead_reply, monkeypatch
):
    """A variant that comes back empty is filtered out, not surfaced as a blank chip."""

    from autosdr.llm.client import CompletionResult

    queue = ["a real draft", "   ", "another good one"]

    async def _fake(
        *, system, user, model, prompt_version, temperature, context=None, **_kwargs
    ):
        text = queue.pop(0)
        return CompletionResult(
            text=text,
            model=model,
            prompt_version=prompt_version,
            tokens_in=1,
            tokens_out=1,
            attempts=1,
            latency_ms=1,
            llm_call_id="call",
        )

    monkeypatch.setattr("autosdr.pipeline.suggestions.complete_text", _fake)
    _no_complete_json(monkeypatch)

    suggestions = await generate_reply_variants(
        workspace=thread_with_lead_reply["workspace"],
        campaign=thread_with_lead_reply["campaign"],
        lead=thread_with_lead_reply["lead"],
        thread=thread_with_lead_reply["thread"],
        history=thread_with_lead_reply["history"],
        n=3,
    )

    assert len(suggestions) == 2
    assert [s["draft"] for s in suggestions] == ["a real draft", "another good one"]


async def test_thread_without_lead_reply_uses_outreach_flow(
    thread_with_lead_reply, monkeypatch
):
    """Empty / outbound-only history falls back to the cold-outreach audit loop.

    This is the operator-clicks-regenerate-on-a-fresh-thread case: the
    next message is structurally still a first touch, so the credential
    line, evaluator, and rest of the cold-outreach contract are still
    the right thing.
    """

    audit_calls: list[str] = []

    async def _fake_audit(**kwargs):
        audit_calls.append("ran")
        return {
            "status": "pass",
            "draft": "hey, saw your listing — i build websites for a living",
            "attempts": 1,
            "drafts": [
                {
                    "attempt": 1,
                    "draft": "hey, saw your listing — i build websites for a living",
                    "scores": {
                        "tone_match": 0.9,
                        "personalisation": 0.9,
                        "goal_alignment": 0.9,
                        "length_valid": 1.0,
                        "naturalness": 0.9,
                    },
                    "overall": 0.92,
                    "pass": True,
                    "feedback": "",
                    "gen_tokens_in": 5,
                    "gen_tokens_out": 5,
                    "gen_llm_call_id": "g1",
                    "eval_tokens_in": 5,
                    "eval_tokens_out": 5,
                    "eval_llm_call_id": "e1",
                }
            ],
            "overall": 0.92,
            "scores": {
                "tone_match": 0.9,
                "personalisation": 0.9,
                "goal_alignment": 0.9,
                "length_valid": 1.0,
                "naturalness": 0.9,
            },
            "last_feedback": "",
        }

    monkeypatch.setattr(
        "autosdr.pipeline.suggestions.generate_and_evaluate", _fake_audit
    )

    async def _refuse_followup(**_kwargs):
        raise AssertionError(
            "follow-up flow should not run when history has no lead messages"
        )

    monkeypatch.setattr(
        "autosdr.pipeline.suggestions.complete_text", _refuse_followup
    )

    history_outbound_only = [
        {"role": MessageRole.AI, "content": "hey, saw your listing"},
    ]

    suggestions = await generate_reply_variants(
        workspace=thread_with_lead_reply["workspace"],
        campaign=thread_with_lead_reply["campaign"],
        lead=thread_with_lead_reply["lead"],
        thread=thread_with_lead_reply["thread"],
        history=history_outbound_only,
        n=3,
    )

    assert audit_calls == ["ran", "ran", "ran"]
    assert all(s["source"] == "outreach" for s in suggestions)
    assert all(s["overall"] is not None for s in suggestions)


# ---------------------------------------------------------------------------
# Prompt contract
# ---------------------------------------------------------------------------


def test_followup_prompt_does_not_carry_first_contact_contract():
    """The follow-up system prompt is a different agent than cold outreach.

    Pin the contract: the new prompt must NOT replicate the cold-outreach
    rules that have been bleeding into reply drafts (mandatory credential
    line, "first message" framing, "never heard of them"). Doing so is
    the bug we're fixing — if any of these regress in the prompt body,
    this test fails loudly.
    """

    system = followup_reply.build_system_prompt(tone_snapshot="direct, casual")

    # The cold-outreach prompt's tell-tale framings. We're checking the
    # *mandatory* / *first-contact* framing leaks, not the specific
    # phrase "I build websites for a living" — that string appears in
    # the follow-up prompt's anti-pattern *explanation* by design ("do
    # NOT re-state your credential like 'I build websites for a
    # living'"), and explaining the anti-pattern to the model is what
    # keeps it from repeating it.
    forbidden = [
        "MANDATORY: Include exactly ONE short peer-framed line",
        "CREDENTIAL — MANDATORY",
        "has never heard of them",
        "writing a short outreach SMS",
    ]
    for phrase in forbidden:
        assert phrase not in system, (
            f"Follow-up system prompt regressed and now contains cold-outreach "
            f"contract phrase: {phrase!r}"
        )

    # Positive contract: the new role framing is present.
    lower = system.lower()
    assert "guide" in lower
    assert "do not re-introduce" in lower or "do not re-pitch" in lower
    # Explicitly tells the model not to restate the credential — this
    # is the bit that fixes the user-reported bug.
    assert "credential" in lower
    assert "first message only" in lower or "first-message only" in lower


def test_followup_user_prompt_includes_history_and_goal():
    """Inputs the prompt actually needs make it through verbatim.

    Anti-regression for an accidental refactor that drops the history
    block — without it the model has no context and falls back to a
    generic reply.
    """

    user = followup_reply.build_user_prompt(
        campaign_goal="Get a quick chat",
        lead_short_name="Sunny",
        lead_category="Childcare",
        message_history=[
            {"role": "ai", "content": "hey, saw your listing"},
            {"role": "lead", "content": "no thanks"},
        ],
    )

    assert "hey, saw your listing" in user
    assert "no thanks" in user
    assert "Sunny" in user
    assert "Childcare" in user
    assert "Get a quick chat" in user


def test_followup_user_prompt_handles_no_history_gracefully():
    """A degenerate empty-history path doesn't blow up.

    Defensive: the routing rule above should keep this path from being
    used, but the prompt builder still needs to render something
    coherent so a future caller doesn't crash on an edge case.
    """

    user = followup_reply.build_user_prompt(
        campaign_goal=None,
        lead_short_name=None,
        lead_category=None,
        message_history=[],
    )
    assert "(no prior messages)" in user
