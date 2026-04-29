"""Tests for ``GET /api/campaigns/{id}/timeseries``.

Covers the success criteria from ticket 0003:

* Empty campaign → ``days`` zero-rows (window is stable so the chart can
  render against a fresh campaign without doing math UI-side).
* Replies and wins on the same day → both counted (they are independent
  slices of the funnel, not stages).
* Date boundaries: a message at 23:59 UTC vs 00:01 UTC the next day
  lands in the correct bucket.
* Per-thread reply de-dup: a chatty lead with two same-day replies is
  one ``replied``; a thread that first replied earlier and replied
  again inside the window is **not** re-counted.
* The ``days`` parameter is honoured and clamps the window length.
* Other campaigns' activity does not bleed into the response.
* Unknown campaign id → 404.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from fastapi import HTTPException

from autosdr.api.campaigns import campaign_timeseries
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


def _make_campaign(session, workspace: Workspace, name: str = "C") -> Campaign:
    campaign = Campaign(
        workspace_id=workspace.id,
        name=name,
        goal="Book a call",
        outreach_per_day=5,
        connector_type="file",
        status=CampaignStatus.ACTIVE,
    )
    session.add(campaign)
    session.flush()
    return campaign


def _seed_thread(
    session,
    *,
    workspace: Workspace,
    campaign: Campaign,
    contact: str,
    queue_position: int = 1,
    thread_status: str = ThreadStatus.ACTIVE,
    cl_status: str = CampaignLeadStatus.CONTACTED,
) -> Thread:
    """Seed a (Lead → CampaignLead → Thread) chain for one row.

    Messages are added separately so each test can control timestamps
    precisely — date-boundary cases need to set ``Message.created_at``
    after creation.
    """

    lead = Lead(
        workspace_id=workspace.id,
        name=f"Lead {contact}",
        contact_uri=contact,
        contact_type="mobile",
        category="Retail",
        address="Brisbane",
        raw_data={},
        import_order=queue_position,
        source_file="seed",
        status=LeadStatus.NEW,
    )
    session.add(lead)
    session.flush()

    cl = CampaignLead(
        campaign_id=campaign.id,
        lead_id=lead.id,
        queue_position=queue_position,
        status=cl_status,
    )
    session.add(cl)
    session.flush()

    thread = Thread(
        campaign_lead_id=cl.id,
        connector_type="file",
        status=thread_status,
    )
    session.add(thread)
    session.flush()
    return thread


def _add_message(
    session,
    *,
    thread: Thread,
    role: str,
    when: datetime,
    content: str = "x",
) -> Message:
    msg = Message(
        thread_id=thread.id,
        role=role,
        content=content,
        metadata_={},
    )
    session.add(msg)
    session.flush()
    msg.created_at = when
    session.flush()
    return msg


def _bucket_by_date(result) -> dict[str, dict]:
    return {b.date: b for b in result.buckets}


def test_empty_campaign_returns_days_zero_rows(fresh_db, workspace_factory):
    """Brand-new campaign → 14 zero rows; no math UI-side."""

    ws_id = workspace_factory()
    with fresh_db() as session:
        ws = session.get(Workspace, ws_id)
        campaign = _make_campaign(session, ws)
        campaign_id = campaign.id

    result = campaign_timeseries(campaign_id, days=14)

    assert result.days == 14
    assert len(result.buckets) == 14
    assert all(
        b.sent == 0 and b.replied == 0 and b.won == 0 and b.lost == 0
        for b in result.buckets
    )
    # Oldest first, contiguous days, no gaps.
    dates = [b.date for b in result.buckets]
    assert dates == sorted(dates)
    parsed = [datetime.fromisoformat(d).date() for d in dates]
    diffs = {(parsed[i + 1] - parsed[i]).days for i in range(len(parsed) - 1)}
    assert diffs == {1}


def test_sent_counts_every_ai_message_in_window(fresh_db, workspace_factory):
    """``sent`` is per-message — follow-ups count separately."""

    ws_id = workspace_factory()
    today_noon = datetime.now(tz=timezone.utc).replace(
        hour=12, minute=0, second=0, microsecond=0
    )

    with fresh_db() as session:
        ws = session.get(Workspace, ws_id)
        campaign = _make_campaign(session, ws)
        thread = _seed_thread(
            session, workspace=ws, campaign=campaign, contact="+61400000001"
        )
        # First-contact + follow-up beat 10s later → 2 sends, same day.
        _add_message(session, thread=thread, role=MessageRole.AI, when=today_noon)
        _add_message(
            session,
            thread=thread,
            role=MessageRole.AI,
            when=today_noon + timedelta(seconds=10),
        )
        campaign_id = campaign.id

    result = campaign_timeseries(campaign_id, days=14)
    by_date = _bucket_by_date(result)
    assert by_date[today_noon.date().isoformat()].sent == 2


def test_replies_and_wins_on_same_day_both_counted(fresh_db, workspace_factory):
    """Replied + won on the same day → both counters tick. They are
    independent slices, not stages."""

    ws_id = workspace_factory()
    today_noon = datetime.now(tz=timezone.utc).replace(
        hour=12, minute=0, second=0, microsecond=0
    )

    with fresh_db() as session:
        ws = session.get(Workspace, ws_id)
        campaign = _make_campaign(session, ws)
        thread = _seed_thread(
            session,
            workspace=ws,
            campaign=campaign,
            contact="+61400000010",
            thread_status=ThreadStatus.WON,
        )
        _add_message(session, thread=thread, role=MessageRole.AI, when=today_noon)
        _add_message(
            session,
            thread=thread,
            role=MessageRole.LEAD,
            when=today_noon + timedelta(minutes=5),
        )
        thread.updated_at = today_noon + timedelta(minutes=10)
        session.flush()
        campaign_id = campaign.id

    result = campaign_timeseries(campaign_id, days=14)
    by_date = _bucket_by_date(result)
    today_iso = today_noon.date().isoformat()
    assert by_date[today_iso].sent == 1
    assert by_date[today_iso].replied == 1
    assert by_date[today_iso].won == 1
    assert by_date[today_iso].lost == 0


def test_date_boundary_2359_vs_0001_lands_on_correct_day(
    fresh_db, workspace_factory
):
    """A message at 23:59 UTC on day N and another at 00:01 UTC on day
    N+1 must land on different buckets."""

    ws_id = workspace_factory()
    today = datetime.now(tz=timezone.utc).date()
    yesterday = today - timedelta(days=1)
    yesterday_late = datetime.combine(
        yesterday, datetime.min.time(), tzinfo=timezone.utc
    ) + timedelta(hours=23, minutes=59)
    today_early = datetime.combine(
        today, datetime.min.time(), tzinfo=timezone.utc
    ) + timedelta(minutes=1)

    with fresh_db() as session:
        ws = session.get(Workspace, ws_id)
        campaign = _make_campaign(session, ws)

        thread_late = _seed_thread(
            session, workspace=ws, campaign=campaign, contact="+61400000020"
        )
        _add_message(
            session, thread=thread_late, role=MessageRole.AI, when=yesterday_late
        )

        thread_early = _seed_thread(
            session,
            workspace=ws,
            campaign=campaign,
            contact="+61400000021",
            queue_position=2,
        )
        _add_message(
            session, thread=thread_early, role=MessageRole.AI, when=today_early
        )
        campaign_id = campaign.id

    result = campaign_timeseries(campaign_id, days=14)
    by_date = _bucket_by_date(result)
    assert by_date[yesterday.isoformat()].sent == 1
    assert by_date[today.isoformat()].sent == 1


def test_replied_dedups_per_thread_first_reply_only(
    fresh_db, workspace_factory
):
    """A chatty lead that replies twice on Tuesday is one ``replied``.

    A thread that first replied last week and replies again inside the
    window is NOT re-counted — its first-reply day was last week.
    """

    ws_id = workspace_factory()
    today_noon = datetime.now(tz=timezone.utc).replace(
        hour=12, minute=0, second=0, microsecond=0
    )

    with fresh_db() as session:
        ws = session.get(Workspace, ws_id)
        campaign = _make_campaign(session, ws)

        chatty = _seed_thread(
            session, workspace=ws, campaign=campaign, contact="+61400000030"
        )
        _add_message(
            session, thread=chatty, role=MessageRole.LEAD, when=today_noon
        )
        _add_message(
            session,
            thread=chatty,
            role=MessageRole.LEAD,
            when=today_noon + timedelta(hours=2),
        )

        # Thread that first replied 10 days ago (still inside the
        # 14-day window for the *test*, but its first-reply day was
        # ten days ago, not today).
        long_runner = _seed_thread(
            session,
            workspace=ws,
            campaign=campaign,
            contact="+61400000031",
            queue_position=2,
        )
        ten_days_ago = today_noon - timedelta(days=10)
        _add_message(
            session,
            thread=long_runner,
            role=MessageRole.LEAD,
            when=ten_days_ago,
        )
        _add_message(
            session, thread=long_runner, role=MessageRole.LEAD, when=today_noon
        )
        campaign_id = campaign.id

    result = campaign_timeseries(campaign_id, days=14)
    by_date = _bucket_by_date(result)
    today_iso = today_noon.date().isoformat()
    ten_days_ago_iso = ten_days_ago.date().isoformat()
    assert by_date[today_iso].replied == 1  # only the chatty thread
    assert by_date[ten_days_ago_iso].replied == 1  # the long-runner's first


def test_other_campaigns_do_not_bleed_in(fresh_db, workspace_factory):
    """The campaign filter must restrict to that campaign's threads only."""

    ws_id = workspace_factory()
    today_noon = datetime.now(tz=timezone.utc).replace(
        hour=12, minute=0, second=0, microsecond=0
    )

    with fresh_db() as session:
        ws = session.get(Workspace, ws_id)
        campaign_a = _make_campaign(session, ws, name="A")
        campaign_b = _make_campaign(session, ws, name="B")

        thread_a = _seed_thread(
            session, workspace=ws, campaign=campaign_a, contact="+61400000040"
        )
        _add_message(
            session, thread=thread_a, role=MessageRole.AI, when=today_noon
        )

        thread_b = _seed_thread(
            session,
            workspace=ws,
            campaign=campaign_b,
            contact="+61400000041",
            queue_position=2,
        )
        _add_message(
            session, thread=thread_b, role=MessageRole.AI, when=today_noon
        )
        _add_message(
            session,
            thread=thread_b,
            role=MessageRole.LEAD,
            when=today_noon + timedelta(minutes=5),
        )

        campaign_a_id = campaign_a.id

    result = campaign_timeseries(campaign_a_id, days=14)
    today_iso = today_noon.date().isoformat()
    by_date = _bucket_by_date(result)
    assert by_date[today_iso].sent == 1
    assert by_date[today_iso].replied == 0


def test_days_parameter_clamps_window(fresh_db, workspace_factory):
    """``days=7`` returns 7 buckets; activity older than 7 days drops out."""

    ws_id = workspace_factory()
    today_noon = datetime.now(tz=timezone.utc).replace(
        hour=12, minute=0, second=0, microsecond=0
    )

    with fresh_db() as session:
        ws = session.get(Workspace, ws_id)
        campaign = _make_campaign(session, ws)
        thread = _seed_thread(
            session, workspace=ws, campaign=campaign, contact="+61400000050"
        )
        # 10-day-old send: outside a 7-day window, inside a 14-day window.
        _add_message(
            session,
            thread=thread,
            role=MessageRole.AI,
            when=today_noon - timedelta(days=10),
        )
        _add_message(session, thread=thread, role=MessageRole.AI, when=today_noon)
        campaign_id = campaign.id

    result_7 = campaign_timeseries(campaign_id, days=7)
    assert result_7.days == 7
    assert len(result_7.buckets) == 7
    assert sum(b.sent for b in result_7.buckets) == 1  # only today

    result_14 = campaign_timeseries(campaign_id, days=14)
    assert result_14.days == 14
    assert len(result_14.buckets) == 14
    assert sum(b.sent for b in result_14.buckets) == 2


def test_unknown_campaign_returns_404(fresh_db, workspace_factory):
    workspace_factory()

    with pytest.raises(HTTPException) as excinfo:
        campaign_timeseries("nope-not-real", days=14)
    assert excinfo.value.status_code == 404
