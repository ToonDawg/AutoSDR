"""Tests for ``GET /api/stats/angle-funnel``.

Covers the success criteria from ticket 0002:

* Empty workspace → empty ``rows``.
* Mixed angles + a NULL ``angle_type`` → NULL bucketed as ``"unknown"``.
* Replies counted via ``MessageRole.LEAD`` existence on the thread, not
  via ``CampaignLead.status`` alone.
* Campaign-scoped query excludes other campaigns.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from autosdr.api.stats import angle_funnel
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


def _seed_thread(
    session,
    *,
    workspace: Workspace,
    campaign: Campaign,
    contact: str,
    angle_type: str | None,
    has_lead_reply: bool = False,
    lead_status: str = LeadStatus.NEW,
    cl_status: str = CampaignLeadStatus.CONTACTED,
    thread_status: str = ThreadStatus.ACTIVE,
    queue_position: int = 1,
) -> Thread:
    """Helper to seed (Lead → CampaignLead → Thread → Messages) for one row.

    ``has_lead_reply`` controls whether a ``MessageRole.LEAD`` row exists
    on the thread. ``cl_status`` lets us prove the funnel uses
    ``MessageRole.LEAD`` existence and not ``CampaignLead.status``.
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
        status=lead_status,
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
        angle="freeform 2-3 sentences",
        angle_type=angle_type,
    )
    session.add(thread)
    session.flush()

    session.add(
        Message(
            thread_id=thread.id,
            role=MessageRole.AI,
            content="hey there",
            metadata_={},
        )
    )
    if has_lead_reply:
        session.add(
            Message(
                thread_id=thread.id,
                role=MessageRole.LEAD,
                content="yes please",
                metadata_={},
            )
        )
    session.flush()
    return thread


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


def test_empty_workspace_returns_no_rows(fresh_db, workspace_factory):
    workspace_factory()

    result = angle_funnel(campaign_id=None, since_days=None)

    assert result.rows == []
    assert result.campaign_id is None
    assert result.since is not None  # 30-day default applied


def test_mixed_angles_and_null_bucket_under_unknown(fresh_db, workspace_factory):
    """Mixed buckets + a thread written before the column existed (NULL)
    must all surface; NULL is reported as ``"unknown"``."""

    ws_id = workspace_factory()

    with fresh_db() as session:
        ws = session.get(Workspace, ws_id)
        campaign = _make_campaign(session, ws)

        _seed_thread(session, workspace=ws, campaign=campaign,
                     contact="+61400000001", angle_type="stale_info",
                     has_lead_reply=True, queue_position=1)
        _seed_thread(session, workspace=ws, campaign=campaign,
                     contact="+61400000002", angle_type="stale_info",
                     queue_position=2)
        _seed_thread(session, workspace=ws, campaign=campaign,
                     contact="+61400000003", angle_type="signature_detail",
                     queue_position=3)
        _seed_thread(session, workspace=ws, campaign=campaign,
                     contact="+61400000004", angle_type=None,
                     queue_position=4)

    result = angle_funnel(campaign_id=None, since_days=None)

    by_angle = {row.angle: row for row in result.rows}
    assert set(by_angle) == {"stale_info", "signature_detail", "unknown"}
    assert by_angle["stale_info"].threads == 2
    assert by_angle["stale_info"].replied == 1
    assert by_angle["signature_detail"].threads == 1
    assert by_angle["signature_detail"].replied == 0
    assert by_angle["unknown"].threads == 1

    # rows are ordered threads-desc so the dominant angle leads.
    assert result.rows[0].angle == "stale_info"


def test_replies_counted_via_lead_message_not_campaign_lead_status(
    fresh_db, workspace_factory
):
    """Source of truth for "replied" is a ``MessageRole.LEAD`` row on the
    thread. ``CampaignLead.status`` can lag (e.g. operator dismissed
    HITL but never closed; pipeline updates ``cl.status`` async) — the
    funnel must not over- or under-count by trusting it."""

    ws_id = workspace_factory()

    with fresh_db() as session:
        ws = session.get(Workspace, ws_id)
        campaign = _make_campaign(session, ws)

        # Lead-message present, but cl.status still CONTACTED (status
        # lag) — must count as replied.
        _seed_thread(
            session, workspace=ws, campaign=campaign,
            contact="+61400000010", angle_type="review_theme",
            has_lead_reply=True,
            cl_status=CampaignLeadStatus.CONTACTED,
            queue_position=1,
        )
        # cl.status flipped to REPLIED but no lead message on the thread
        # (e.g. status got nudged by an out-of-band edit) — must NOT
        # count as replied.
        _seed_thread(
            session, workspace=ws, campaign=campaign,
            contact="+61400000011", angle_type="review_theme",
            has_lead_reply=False,
            cl_status=CampaignLeadStatus.REPLIED,
            queue_position=2,
        )

    result = angle_funnel(campaign_id=None, since_days=None)
    by_angle = {row.angle: row for row in result.rows}

    assert by_angle["review_theme"].threads == 2
    assert by_angle["review_theme"].replied == 1


def test_campaign_scope_excludes_other_campaigns(fresh_db, workspace_factory):
    """``campaign_id`` filter must restrict to that campaign's threads only."""

    ws_id = workspace_factory()

    with fresh_db() as session:
        ws = session.get(Workspace, ws_id)
        campaign_a = _make_campaign(session, ws, name="A")
        campaign_b = _make_campaign(session, ws, name="B")

        _seed_thread(session, workspace=ws, campaign=campaign_a,
                     contact="+61400000020", angle_type="stale_info",
                     has_lead_reply=True, queue_position=1)
        _seed_thread(session, workspace=ws, campaign=campaign_b,
                     contact="+61400000021", angle_type="weak_presence",
                     has_lead_reply=True, queue_position=1)
        _seed_thread(session, workspace=ws, campaign=campaign_b,
                     contact="+61400000022", angle_type="weak_presence",
                     queue_position=2)

        campaign_a_id = campaign_a.id
        campaign_b_id = campaign_b.id

    result_a = angle_funnel(campaign_id=campaign_a_id, since_days=None)
    angles_a = {r.angle for r in result_a.rows}
    assert angles_a == {"stale_info"}
    assert result_a.rows[0].threads == 1
    assert result_a.rows[0].replied == 1
    assert result_a.campaign_id == campaign_a_id
    # Campaign-scoped default → no time filter (campaign-lifetime).
    assert result_a.since is None

    result_b = angle_funnel(campaign_id=campaign_b_id, since_days=None)
    angles_b = {r.angle for r in result_b.rows}
    assert angles_b == {"weak_presence"}
    assert result_b.rows[0].threads == 2
    assert result_b.rows[0].replied == 1


def test_won_and_lost_counters(fresh_db, workspace_factory):
    """Won / lost track ``Thread.status`` — independent of replied."""

    ws_id = workspace_factory()

    with fresh_db() as session:
        ws = session.get(Workspace, ws_id)
        campaign = _make_campaign(session, ws)

        # Replied + won.
        _seed_thread(session, workspace=ws, campaign=campaign,
                     contact="+61400000030", angle_type="differentiator",
                     has_lead_reply=True,
                     thread_status=ThreadStatus.WON, queue_position=1)
        # Replied + lost.
        _seed_thread(session, workspace=ws, campaign=campaign,
                     contact="+61400000031", angle_type="differentiator",
                     has_lead_reply=True,
                     thread_status=ThreadStatus.LOST, queue_position=2)
        # No reply, still active.
        _seed_thread(session, workspace=ws, campaign=campaign,
                     contact="+61400000032", angle_type="differentiator",
                     queue_position=3)

    result = angle_funnel(campaign_id=None, since_days=None)
    row = result.rows[0]
    assert row.angle == "differentiator"
    assert row.threads == 3
    assert row.replied == 2
    assert row.won == 1
    assert row.lost == 1


def test_since_days_override_filters_old_threads(fresh_db, workspace_factory):
    ws_id = workspace_factory()

    with fresh_db() as session:
        ws = session.get(Workspace, ws_id)
        campaign = _make_campaign(session, ws)

        # Recent thread.
        _seed_thread(session, workspace=ws, campaign=campaign,
                     contact="+61400000040", angle_type="brand_voice",
                     queue_position=1)
        # Backdated thread, 60 days old.
        old = _seed_thread(session, workspace=ws, campaign=campaign,
                           contact="+61400000041", angle_type="brand_voice",
                           queue_position=2)
        old.created_at = datetime.now(timezone.utc) - timedelta(days=60)
        session.flush()

    result_30d = angle_funnel(campaign_id=None, since_days=30)
    assert result_30d.rows[0].threads == 1

    result_lifetime = angle_funnel(campaign_id=None, since_days=365)
    assert result_lifetime.rows[0].threads == 2


def test_unknown_campaign_returns_404(fresh_db, workspace_factory):
    workspace_factory()

    import pytest
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as excinfo:
        angle_funnel(campaign_id="nope-not-real", since_days=None)
    assert excinfo.value.status_code == 404


# ---------------------------------------------------------------------------
# Enrichment stratifier (ticket 0011)
# ---------------------------------------------------------------------------


def _set_first_ai_enrichment_status(session, thread: Thread, status: str | None) -> None:
    """Stamp the first AI message's metadata.analysis.enrichment_status.

    Helper for the enrichment-stratifier tests below — mirrors what the
    outreach pipeline writes via ``analysis_meta`` so the funnel filter
    has something to read. ``status=None`` leaves the analysis block
    without an ``enrichment_status`` key (the pre-ticket-0011 shape) so
    the test can prove the filter treats "missing" as "not enriched".
    """

    from sqlalchemy.orm.attributes import flag_modified

    ai_message = (
        session.query(Message)
        .filter(Message.thread_id == thread.id, Message.role == MessageRole.AI)
        .order_by(Message.created_at.asc())
        .first()
    )
    assert ai_message is not None, "thread must have an AI message"
    meta = dict(ai_message.metadata_ or {})
    analysis = dict(meta.get("analysis") or {})
    if status is not None:
        analysis["enrichment_status"] = status
    elif "enrichment_status" in analysis:
        analysis.pop("enrichment_status")
    meta["analysis"] = analysis
    ai_message.metadata_ = meta
    flag_modified(ai_message, "metadata_")
    session.flush()


def test_enrichment_filter_enriched_returns_only_ok(fresh_db, workspace_factory):
    """``?enrichment=enriched`` keeps only threads whose first AI
    message carries ``metadata.analysis.enrichment_status == "ok"``."""

    ws_id = workspace_factory()

    with fresh_db() as session:
        ws = session.get(Workspace, ws_id)
        campaign = _make_campaign(session, ws)

        ok_thread = _seed_thread(
            session, workspace=ws, campaign=campaign,
            contact="+61400000050", angle_type="signature_detail",
            queue_position=1,
        )
        timeout_thread = _seed_thread(
            session, workspace=ws, campaign=campaign,
            contact="+61400000051", angle_type="fallback",
            queue_position=2,
        )
        legacy_thread = _seed_thread(
            session, workspace=ws, campaign=campaign,
            contact="+61400000052", angle_type="weak_presence",
            queue_position=3,
        )

        _set_first_ai_enrichment_status(session, ok_thread, "ok")
        _set_first_ai_enrichment_status(session, timeout_thread, "timeout")
        _set_first_ai_enrichment_status(session, legacy_thread, None)

    enriched = angle_funnel(
        campaign_id=None, since_days=None, enrichment="enriched"
    )
    assert {row.angle for row in enriched.rows} == {"signature_detail"}
    assert enriched.enrichment == "enriched"
    assert enriched.rows[0].threads == 1


def test_enrichment_filter_unenriched_excludes_ok(fresh_db, workspace_factory):
    """``?enrichment=unenriched`` is the strict complement: it includes
    "timeout", "blocked", "no_url", "disabled" AND legacy threads that
    pre-date the field. The bar is "first AI message did NOT carry
    status=ok"."""

    ws_id = workspace_factory()

    with fresh_db() as session:
        ws = session.get(Workspace, ws_id)
        campaign = _make_campaign(session, ws)

        ok_thread = _seed_thread(
            session, workspace=ws, campaign=campaign,
            contact="+61400000060", angle_type="signature_detail",
            queue_position=1,
        )
        timeout_thread = _seed_thread(
            session, workspace=ws, campaign=campaign,
            contact="+61400000061", angle_type="fallback",
            queue_position=2,
        )
        legacy_thread = _seed_thread(
            session, workspace=ws, campaign=campaign,
            contact="+61400000062", angle_type="weak_presence",
            queue_position=3,
        )

        _set_first_ai_enrichment_status(session, ok_thread, "ok")
        _set_first_ai_enrichment_status(session, timeout_thread, "timeout")
        _set_first_ai_enrichment_status(session, legacy_thread, None)

    unenriched = angle_funnel(
        campaign_id=None, since_days=None, enrichment="unenriched"
    )
    angles = {row.angle for row in unenriched.rows}
    assert angles == {"fallback", "weak_presence"}
    assert unenriched.enrichment == "unenriched"


def test_enrichment_filter_default_all_includes_everything(
    fresh_db, workspace_factory
):
    """No filter (or ``?enrichment=all``) returns the union — pre-0011
    threads MUST keep showing up in the dashboard's headline numbers."""

    ws_id = workspace_factory()

    with fresh_db() as session:
        ws = session.get(Workspace, ws_id)
        campaign = _make_campaign(session, ws)

        ok_thread = _seed_thread(
            session, workspace=ws, campaign=campaign,
            contact="+61400000070", angle_type="signature_detail",
            queue_position=1,
        )
        legacy_thread = _seed_thread(
            session, workspace=ws, campaign=campaign,
            contact="+61400000071", angle_type="weak_presence",
            queue_position=2,
        )

        _set_first_ai_enrichment_status(session, ok_thread, "ok")
        _set_first_ai_enrichment_status(session, legacy_thread, None)

    result = angle_funnel(campaign_id=None, since_days=None, enrichment="all")
    angles = {row.angle for row in result.rows}
    assert angles == {"signature_detail", "weak_presence"}
    assert result.enrichment == "all"
