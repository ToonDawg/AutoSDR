"""Send-order priority tier on ``_next_queued_leads``.

The picker drains a priority tier before the normal tier, while
preserving today's category-mix rotation within each tier and
across the tier boundary. Two predicates feed the tier today:

* ``Lead.enrichment_status == "not_found"`` (ticket 0013).
* ``Lead.website`` is itself a social-profile URL (ticket 0014).

Toggling ``priority_enabled=False`` collapses to the pre-0013
single-pass behaviour — pinned here as a regression bar so a future
refactor of the picker can't silently change the order operators
have been getting since 0010.
"""

from __future__ import annotations

from autosdr.models import (
    Campaign,
    CampaignLead,
    CampaignLeadStatus,
    CampaignStatus,
    Lead,
    LeadStatus,
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
    enrichment_status: str | None = None,
    website: str | None = None,
    cl_status: str = CampaignLeadStatus.QUEUED,
    lead_status: str = LeadStatus.NEW,
    name_suffix: str | None = None,
) -> CampaignLead:
    """Create a lead + queued ``CampaignLead`` with optional enrichment_status.

    Mirrors the helper in ``test_scheduler_category_mix.py`` but adds
    the enrichment_status + website knobs (the latter for ticket
    0014's social-as-website branch); phone numbers stay unique per
    row to keep the workspace UNIQUE constraint happy.
    """

    suffix = name_suffix or str(queue_position)
    lead = Lead(
        workspace_id=ws_id,
        name=f"Lead {suffix}",
        contact_uri=f"+6140001{queue_position:04d}",
        contact_type="mobile",
        category=category,
        address="x",
        website=website,
        raw_data={},
        import_order=queue_position,
        source_file="x",
        status=lead_status,
    )
    if enrichment_status is not None:
        lead.enrichment_status = enrichment_status
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


def _enrichments_of(picks: list[tuple[CampaignLead, Lead]]) -> list[str | None]:
    return [lead.enrichment_status for _cl, lead in picks]


def _categories_of(picks: list[tuple[CampaignLead, Lead]]) -> list[str | None]:
    return [lead.category for _cl, lead in picks]


def _positions_of(picks: list[tuple[CampaignLead, Lead]]) -> list[int]:
    return [cl.queue_position for cl, _lead in picks]


def test_priority_before_normal_within_a_category(fresh_db, workspace_factory):
    """Priority tier drains first regardless of queue_position.

    Queue positions 1, 2, 3 with categories ["P", "P", "P"]. The
    middle row is ``not_found``; the others are ``ok``. Cold-start
    pull of 3: priority lead pops first, then the remaining two in
    queue order. Pins the core promise — tier dominates queue
    position even within a single category.
    """

    ws_id = workspace_factory()
    with fresh_db() as session:
        cid = _make_campaign(session, ws_id)
        _add_lead(
            session, ws_id, cid,
            category="P", queue_position=1, enrichment_status="ok",
        )
        _add_lead(
            session, ws_id, cid,
            category="P", queue_position=2, enrichment_status="not_found",
        )
        _add_lead(
            session, ws_id, cid,
            category="P", queue_position=3, enrichment_status="ok",
        )

        picks = _next_queued_leads(session, cid, limit=3)

    assert _enrichments_of(picks) == ["not_found", "ok", "ok"]
    assert _positions_of(picks) == [2, 1, 3]


def test_priority_tier_still_rotates_categories(fresh_db, workspace_factory):
    """Inside the priority tier, the existing category-mix scoring runs.

    Three priority leads — two plumbers and one electrician. Cold
    start, limit 2 → categories ``["P", "E"]``: the picker still
    avoids two priority plumbers in a row. The third plumber would
    only land on the next tick (or with limit=3).
    """

    ws_id = workspace_factory()
    with fresh_db() as session:
        cid = _make_campaign(session, ws_id)
        _add_lead(
            session, ws_id, cid,
            category="P", queue_position=1, enrichment_status="not_found",
        )
        _add_lead(
            session, ws_id, cid,
            category="P", queue_position=2, enrichment_status="not_found",
        )
        _add_lead(
            session, ws_id, cid,
            category="E", queue_position=3, enrichment_status="not_found",
        )

        picks = _next_queued_leads(session, cid, limit=2)

    assert _categories_of(picks) == ["P", "E"]
    assert all(s == "not_found" for s in _enrichments_of(picks))


def test_normal_tier_runs_after_priority_drains(fresh_db, workspace_factory):
    """Mixed tiers, limit covers both. Priority first, then normal.

    Queue: priority [P-not_found, E-not_found], normal [P-ok, P-ok].
    Limit 4 — order is [P-priority, E-priority, P-normal, P-normal].
    Within the normal tier the rotation continues from
    ``last_sent_cat = E`` so the first normal pick is a P (which
    was 2 picks ago, so allowed under the anti-consecutive rule).
    """

    ws_id = workspace_factory()
    with fresh_db() as session:
        cid = _make_campaign(session, ws_id)
        _add_lead(
            session, ws_id, cid,
            category="P", queue_position=1, enrichment_status="not_found",
        )
        _add_lead(
            session, ws_id, cid,
            category="E", queue_position=2, enrichment_status="not_found",
        )
        _add_lead(
            session, ws_id, cid,
            category="P", queue_position=3, enrichment_status="ok",
        )
        _add_lead(
            session, ws_id, cid,
            category="P", queue_position=4, enrichment_status="ok",
        )

        picks = _next_queued_leads(session, cid, limit=4)

    assert _enrichments_of(picks) == ["not_found", "not_found", "ok", "ok"]
    assert _categories_of(picks) == ["P", "E", "P", "P"]


def test_priority_toggle_off_is_byte_identical(fresh_db, workspace_factory):
    """``priority_enabled=False`` collapses to today's single-tier picker.

    Queue: same setup as ``test_priority_before_normal_within_a_category``
    (one ``not_found`` flanked by two ``ok``). With the toggle off,
    the picker must return queue_positions [1, 2, 3] in order — i.e.
    the priority lead does NOT jump the queue. This is the regression
    bar for operators who flip the toggle off.
    """

    ws_id = workspace_factory()
    with fresh_db() as session:
        cid = _make_campaign(session, ws_id)
        _add_lead(
            session, ws_id, cid,
            category="P", queue_position=1, enrichment_status="ok",
        )
        _add_lead(
            session, ws_id, cid,
            category="P", queue_position=2, enrichment_status="not_found",
        )
        _add_lead(
            session, ws_id, cid,
            category="P", queue_position=3, enrichment_status="ok",
        )

        picks = _next_queued_leads(
            session, cid, limit=3, priority_enabled=False
        )

    assert _positions_of(picks) == [1, 2, 3]


def test_empty_priority_tier_is_byte_identical(fresh_db, workspace_factory):
    """No priority leads in queue → picker output matches pre-0013 exactly.

    Same fixture as ``test_round_robin_from_cold_start`` in
    ``test_scheduler_category_mix.py`` — no enrichment_status set on
    any lead, so no lead is priority. Categories appear in the same
    set as the existing test (``{P, E, O}``); picking 3 must hit all
    three categories. Pins "we don't accidentally affect the
    no-priority path".
    """

    ws_id = workspace_factory()
    with fresh_db() as session:
        cid = _make_campaign(session, ws_id)
        for pos, cat in enumerate(["P", "P", "P", "E", "E", "O"], start=1):
            _add_lead(session, ws_id, cid, category=cat, queue_position=pos)

        picks = _next_queued_leads(session, cid, limit=3)

    assert len(picks) == 3
    assert set(_categories_of(picks)) == {"P", "E", "O"}


def test_drained_priority_tier_does_not_crash_on_empty_buckets(
    fresh_db, workspace_factory,
):
    """Tick 2 with no priority leads left must still serve normal tier.

    Cross-tick continuity: tick 1 used the only priority lead; tick 2
    starts with an empty priority bucket map. The picker must skip
    the empty tier without raising and pick from the normal tier.
    """

    ws_id = workspace_factory()
    with fresh_db() as session:
        cid = _make_campaign(session, ws_id)
        priority_cl = _add_lead(
            session, ws_id, cid,
            category="P", queue_position=1, enrichment_status="not_found",
        )
        _add_lead(
            session, ws_id, cid,
            category="E", queue_position=2, enrichment_status="ok",
        )
        _add_lead(
            session, ws_id, cid,
            category="O", queue_position=3, enrichment_status="ok",
        )

        # Simulate tick 1 having picked the priority lead by marking it
        # CONTACTED — the candidate SQL filters CL.status=QUEUED, so on
        # tick 2 the picker sees only the two normal-tier leads.
        priority_cl.status = CampaignLeadStatus.CONTACTED
        session.flush()

        picks = _next_queued_leads(session, cid, limit=2)

    assert _enrichments_of(picks) == ["ok", "ok"]
    assert _categories_of(picks) == ["E", "O"]


def test_social_website_lead_joins_priority_tier(fresh_db, workspace_factory):
    """A lead whose website is a Facebook URL joins the priority tier.

    Queue: a 404 lead (P), a Facebook-as-website lead with
    ``enrichment_status="ok"`` (E), a normal lead (P). Picker runs
    with the default ``priority_enabled=True``. Both priority leads
    must drain before the normal lead, regardless of queue position;
    the existing category-mix rotation within the tier still
    interleaves P→E. Pins the ticket-0014 contract that the social
    branch widens the predicate without disturbing the tier shape.
    """

    ws_id = workspace_factory()
    with fresh_db() as session:
        cid = _make_campaign(session, ws_id)
        _add_lead(
            session, ws_id, cid,
            category="P", queue_position=1, enrichment_status="not_found",
        )
        _add_lead(
            session, ws_id, cid,
            category="E", queue_position=2, enrichment_status="ok",
            website="https://facebook.com/Acme",
        )
        _add_lead(
            session, ws_id, cid,
            category="P", queue_position=3, enrichment_status="ok",
        )

        picks = _next_queued_leads(session, cid, limit=3)

    # First two picks are the priority tier (P-not_found, E-social);
    # the normal-tier P trails. Category mix interleaves P→E within
    # the priority tier as the existing logic dictates.
    assert _categories_of(picks) == ["P", "E", "P"]
    assert _positions_of(picks) == [1, 2, 3]
    # Sanity check: two of the three are priority (one not_found,
    # one social), one is plain ok.
    statuses = _enrichments_of(picks)
    assert statuses[0] == "not_found"
    assert statuses[1] == "ok"  # social lead — scan returned ok
    assert statuses[2] == "ok"  # normal lead
