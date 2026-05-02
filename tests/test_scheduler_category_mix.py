"""Scheduler — category-aware lead rotation in ``_next_queued_leads``.

The naive ``ORDER BY queue_position`` picker burned a plumber-heavy import
on plumbers for days; the picker now interleaves business categories so a
single tick (and a single day) doesn't stack same-category sends. These
tests pin the four scoring keys (anti-consecutive, untouched-categories,
least-recently-sent, FIFO tiebreak) plus the degenerate single-category
fallback.
"""

from __future__ import annotations

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
)
from autosdr.scheduler import _next_queued_leads


def _make_campaign(session, ws_id: str) -> str:
    campaign = Campaign(
        workspace_id=ws_id,
        name="C",
        goal="g",
        outreach_per_day=50,
        connector_type="android_sms",
        status=CampaignStatus.ACTIVE,
    )
    session.add(campaign)
    session.flush()
    return campaign.id


def _add_lead(
    session,
    ws_id: str,
    campaign_id: str,
    *,
    category: str | None,
    queue_position: int,
    cl_status: str = CampaignLeadStatus.QUEUED,
    lead_status: str = LeadStatus.NEW,
    name_suffix: str | None = None,
) -> CampaignLead:
    """Create a lead + queued CampaignLead. Phone numbers stay unique per row."""

    suffix = name_suffix or str(queue_position)
    lead = Lead(
        workspace_id=ws_id,
        name=f"Lead {suffix}",
        contact_uri=f"+6140000{queue_position:04d}",
        contact_type="mobile",
        category=category,
        address="x",
        raw_data={},
        import_order=queue_position,
        source_file="x",
        status=lead_status,
    )
    session.add(lead)
    session.flush()
    cl = CampaignLead(
        campaign_id=campaign_id,
        lead_id=lead.id,
        queue_position=queue_position,
        status=cl_status,
    )
    session.add(cl)
    session.flush()
    return cl


def _categories_of(picks: list[tuple[CampaignLead, Lead]]) -> list[str | None]:
    return [lead.category for _cl, lead in picks]


def _seed_contact(session, campaign_lead: CampaignLead) -> Thread:
    """Attach a thread + a single AI message so the lead reads as 'contacted'.

    The picker's "ever contacted" / "sent_today" signals are derived from
    the first AI message per thread; one message is enough to flip both.
    """

    thread = Thread(
        campaign_lead_id=campaign_lead.id,
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
            content="hi",
            metadata_={},
        )
    )
    session.flush()
    return thread


def test_round_robin_from_cold_start(fresh_db, workspace_factory):
    """Cold-start campaign with multiple categories -> distinct first picks.

    Queue: [P, P, P, E, E, O]. Asking for 3 should not return three
    plumbers (the old picker would). All three categories must appear.
    """

    ws_id = workspace_factory()
    with fresh_db() as session:
        cid = _make_campaign(session, ws_id)
        for pos, cat in enumerate(["P", "P", "P", "E", "E", "O"], start=1):
            _add_lead(session, ws_id, cid, category=cat, queue_position=pos)

        picks = _next_queued_leads(session, cid, limit=3)

    assert len(picks) == 3
    assert set(_categories_of(picks)) == {"P", "E", "O"}


def test_avoids_consecutive_same_category(fresh_db, workspace_factory):
    """Queue [P, P, E], limit=2 -> [P, E], not [P, P]."""

    ws_id = workspace_factory()
    with fresh_db() as session:
        cid = _make_campaign(session, ws_id)
        _add_lead(session, ws_id, cid, category="P", queue_position=1)
        _add_lead(session, ws_id, cid, category="P", queue_position=2)
        _add_lead(session, ws_id, cid, category="E", queue_position=3)

        picks = _next_queued_leads(session, cid, limit=2)

    assert _categories_of(picks) == ["P", "E"]


def test_biases_away_from_already_contacted_categories(fresh_db, workspace_factory):
    """If P has today's history and E has none, E goes first regardless of FIFO.

    Queue is [P (pos1), P (pos2), E (pos3)] but a previous tick already
    contacted a Plumber on this campaign. The "untouched-categories
    first" tier should pull E ahead of both queued plumbers even though
    E sits last in import order.
    """

    ws_id = workspace_factory()
    with fresh_db() as session:
        cid = _make_campaign(session, ws_id)
        contacted_p = _add_lead(
            session,
            ws_id,
            cid,
            category="P",
            queue_position=0,
            cl_status=CampaignLeadStatus.CONTACTED,
            lead_status=LeadStatus.CONTACTED,
            name_suffix="contacted",
        )
        _seed_contact(session, contacted_p)

        _add_lead(session, ws_id, cid, category="P", queue_position=1)
        _add_lead(session, ws_id, cid, category="P", queue_position=2)
        _add_lead(session, ws_id, cid, category="E", queue_position=3)

        picks = _next_queued_leads(session, cid, limit=2)

    assert _categories_of(picks) == ["E", "P"]


def test_none_category_is_a_valid_bucket(fresh_db, workspace_factory):
    """Uncategorised leads rotate as their own bucket, not as 'last category'.

    Queue [None, None, P], cold start, limit=2: the picker must treat
    ``Lead.category IS NULL`` as a real bucket and rotate against the
    Plumber bucket. The cold-start sentinel must not collapse onto
    ``None``, or the very first pick would deprioritise the
    uncategorised bucket against itself.
    """

    ws_id = workspace_factory()
    with fresh_db() as session:
        cid = _make_campaign(session, ws_id)
        _add_lead(session, ws_id, cid, category=None, queue_position=1)
        _add_lead(session, ws_id, cid, category=None, queue_position=2)
        _add_lead(session, ws_id, cid, category="P", queue_position=3)

        picks = _next_queued_leads(session, cid, limit=2)

    cats = _categories_of(picks)
    assert cats[0] is None
    assert cats[1] == "P"


def test_single_category_degenerates_to_fifo(fresh_db, workspace_factory):
    """Only one category in the queue -> picker behaves like the old FIFO.

    We promised callers nothing about this case other than "still works
    and still respects ``queue_position``". A future regression that
    skipped or reordered same-category leads would break operators who
    only run single-vertical campaigns.
    """

    ws_id = workspace_factory()
    with fresh_db() as session:
        cid = _make_campaign(session, ws_id)
        for pos in (1, 2, 3):
            _add_lead(session, ws_id, cid, category="P", queue_position=pos)

        picks = _next_queued_leads(session, cid, limit=3)

    positions = [cl.queue_position for cl, _lead in picks]
    assert positions == [1, 2, 3]


def test_excludes_non_queued_and_non_eligible_leads(fresh_db, workspace_factory):
    """Filters from the old picker still apply: status + cl-status guards.

    A SENDING campaign-lead and a REPLIED lead must be skipped. This
    duplicates one assertion from ``test_scheduler_quota.py`` to make
    sure the rewrite didn't accidentally widen the candidate set.
    """

    ws_id = workspace_factory()
    with fresh_db() as session:
        cid = _make_campaign(session, ws_id)
        _add_lead(
            session,
            ws_id,
            cid,
            category="P",
            queue_position=1,
            cl_status=CampaignLeadStatus.SENDING,
        )
        _add_lead(
            session,
            ws_id,
            cid,
            category="E",
            queue_position=2,
            lead_status=LeadStatus.REPLIED,
        )
        _add_lead(session, ws_id, cid, category="O", queue_position=3)
        _add_lead(session, ws_id, cid, category="O", queue_position=4)

        picks = _next_queued_leads(session, cid, limit=4)

    assert [cl.queue_position for cl, _lead in picks] == [3, 4]
    assert _categories_of(picks) == ["O", "O"]
