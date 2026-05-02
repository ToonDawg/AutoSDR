"""``LeadOut.is_priority`` / ``priority_reason`` end-to-end.

The serialiser in :mod:`autosdr.api.leads` folds the priority
predicate output onto every ``LeadOut`` response so the React
``PriorityBadge`` can render the chip without re-deriving the
predicate client-side. These tests pin the contract for the
shapes the predicate cares about today: ``not_found`` (ticket
0013), ``social_profile_website`` (ticket 0014), the precedence
between them, and the informational ``is_social_website`` field
that fires regardless of priority.
"""

from __future__ import annotations

import pytest

from autosdr.api.leads import get_lead, list_leads
from autosdr.models import Lead, LeadStatus, Workspace


def _add_lead(
    session,
    ws_id: str,
    *,
    enrichment_status: str | None = None,
    contact_uri: str,
    name: str = "Lead",
    website: str | None = None,
    import_order: int = 1,
) -> Lead:
    lead = Lead(
        workspace_id=ws_id,
        name=name,
        contact_uri=contact_uri,
        contact_type="mobile",
        category="Retail",
        address="x",
        website=website,
        raw_data={},
        import_order=import_order,
        source_file="seed",
        status=LeadStatus.NEW,
    )
    if enrichment_status is not None:
        lead.enrichment_status = enrichment_status
    session.add(lead)
    session.flush()
    return lead


def test_lead_out_marks_not_found_as_priority(fresh_db, workspace_factory):
    """``not_found`` → ``is_priority=True, priority_reason="not_found"``."""

    ws_id = workspace_factory()
    with fresh_db() as session:
        lead = _add_lead(
            session, ws_id,
            enrichment_status="not_found",
            contact_uri="+61400000001",
        )
        lead_id = lead.id

    out = get_lead(lead_id)
    assert out.is_priority is True
    assert out.priority_reason == "not_found"


@pytest.mark.parametrize(
    "enrichment_status",
    ["ok", "timeout", "blocked", "error", "empty_shell", "no_url", None],
)
def test_lead_out_non_priority_states(
    fresh_db, workspace_factory, enrichment_status,
):
    """Every non-``not_found`` enrichment_status → priority fields off.

    Pins the closed vocabulary against accidental promotion: the
    ticket's "high-confidence only" framing means none of these
    states should fire the priority badge today. Ticket 0015 will
    re-open ``timeout`` / ``blocked`` once ``scrape_confidence``
    lands; this test will need updating then.
    """

    ws_id = workspace_factory()
    with fresh_db() as session:
        # Use a unique contact_uri per parametrize value so the
        # workspace UNIQUE constraint stays happy across the matrix.
        unique_suffix = enrichment_status or "null"
        lead = _add_lead(
            session, ws_id,
            enrichment_status=enrichment_status,
            contact_uri=f"+614000{abs(hash(unique_suffix)) % 100000:05d}",
        )
        lead_id = lead.id

    out = get_lead(lead_id)
    assert out.is_priority is False
    assert out.priority_reason is None


def test_lead_list_returns_priority_fields_per_row(
    fresh_db, workspace_factory,
):
    """``GET /api/leads`` carries the priority fields on every row.

    Mixed cohort: one ``not_found``, one ``ok``. Both leads must
    appear; only the ``not_found`` row reads as priority. Pins the
    list-page contract used by ``Leads.tsx`` to render the badge in
    the table.
    """

    ws_id = workspace_factory()
    with fresh_db() as session:
        _add_lead(
            session, ws_id,
            enrichment_status="not_found",
            contact_uri="+61400000010",
            name="Priority lead",
            import_order=1,
        )
        _add_lead(
            session, ws_id,
            enrichment_status="ok",
            contact_uri="+61400000011",
            name="Normal lead",
            import_order=2,
        )

    # ``list_leads`` is a FastAPI route handler — the default kwargs
    # are ``Query(...)`` sentinels that only resolve to ints under the
    # request lifecycle. Pass the runtime limits explicitly so the
    # direct call doesn't trip on the SQLAlchemy ``LIMIT`` coercion.
    page = list_leads(limit=100, offset=0)
    by_name = {row.name: row for row in page.leads}
    assert by_name["Priority lead"].is_priority is True
    assert by_name["Priority lead"].priority_reason == "not_found"
    assert by_name["Normal lead"].is_priority is False
    assert by_name["Normal lead"].priority_reason is None


def test_lead_out_marks_facebook_as_priority(fresh_db, workspace_factory):
    """Social-as-website with ``ok`` scan → social-priority + platform tag.

    Pins the ticket-0014 shape end-to-end:

    * ``is_priority=True`` — the picker tier sees this lead first.
    * ``priority_reason="social_profile_website"`` — the badge label
      distinguishes from the broken-website variant.
    * ``is_social_website="facebook"`` — the platform token drives
      the informational ``SocialProfileTag`` chip.
    """

    ws_id = workspace_factory()
    with fresh_db() as session:
        lead = _add_lead(
            session, ws_id,
            enrichment_status="ok",
            contact_uri="+61400000020",
            website="https://facebook.com/Acme",
        )
        lead_id = lead.id

    out = get_lead(lead_id)
    assert out.is_priority is True
    assert out.priority_reason == "social_profile_website"
    assert out.is_social_website == "facebook"


def test_priority_reason_precedence_not_found_outranks_social(
    fresh_db, workspace_factory,
):
    """A 404'd Facebook URL reads as ``not_found`` (single deterministic winner).

    Both signals fire on the lead but the badge needs one winner —
    server-confirmed 404 is more confident than the import-time
    hostname pattern, so ``not_found`` outranks
    ``social_profile_website`` in ``priority_reason``. The
    informational ``is_social_website`` field stays set so the
    operator still sees "this is a Facebook URL".
    """

    ws_id = workspace_factory()
    with fresh_db() as session:
        lead = _add_lead(
            session, ws_id,
            enrichment_status="not_found",
            contact_uri="+61400000021",
            website="https://facebook.com/Acme",
        )
        lead_id = lead.id

    out = get_lead(lead_id)
    assert out.is_priority is True
    assert out.priority_reason == "not_found"
    assert out.is_social_website == "facebook"


def test_is_social_website_is_none_for_real_corporate_website(
    fresh_db, workspace_factory,
):
    """A real corporate URL → no social tag, no social-priority.

    The tag must NOT fire on path-only mentions of a platform. Pins
    the negative half of the predicate end-to-end so a regression in
    ``is_social_website`` doesn't quietly start tagging every lead.
    """

    ws_id = workspace_factory()
    with fresh_db() as session:
        lead = _add_lead(
            session, ws_id,
            enrichment_status="ok",
            contact_uri="+61400000022",
            website="https://acme.com/about/our-facebook-page",
        )
        lead_id = lead.id

    out = get_lead(lead_id)
    assert out.is_priority is False
    assert out.priority_reason is None
    assert out.is_social_website is None
