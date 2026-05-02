"""Truth-table tests for the priority-lead predicate.

The picker (``_next_queued_leads``) and the API serialiser
(``LeadOut.is_priority`` / ``priority_reason``) both depend on
:func:`autosdr.pipeline.priority.is_priority_lead` returning
``True`` exactly when (a) ``Lead.enrichment_status == "not_found"``
(ticket 0013) OR (b) ``Lead.website`` is a social-profile URL
(ticket 0014). A typo or vocabulary-drift here silently demotes
priority leads back into the normal tier, which is the failure
mode the tickets exist to prevent. These tests pin every value in
the closed ``EnrichmentStatus`` vocab plus the social-as-website
branch and its precedence vs. ``not_found``.
"""

from __future__ import annotations

import pytest

from autosdr.models import Lead
from autosdr.pipeline.priority import (
    PRIORITY_REASON_NOT_FOUND,
    PRIORITY_REASON_SOCIAL_PROFILE_WEBSITE,
    is_priority_lead,
    priority_reason,
)


def _lead(
    *,
    enrichment_status: str | None = None,
    website: str | None = None,
) -> Lead:
    """Construct a bare Lead with given fields, no DB."""

    lead = Lead(
        workspace_id="ws",
        name="x",
        contact_uri="+61400000000",
        contact_type="mobile",
        category=None,
        address=None,
        website=website,
        raw_data={},
        import_order=1,
        source_file=None,
        status="new",
    )
    lead.enrichment_status = enrichment_status
    return lead


@pytest.mark.parametrize(
    "status,expected_priority,expected_reason",
    [
        ("not_found", True, PRIORITY_REASON_NOT_FOUND),
        ("ok", False, None),
        ("timeout", False, None),
        ("blocked", False, None),
        ("error", False, None),
        ("empty_shell", False, None),
        ("no_url", False, None),
        ("killswitch_aborted", False, None),
        (None, False, None),
    ],
)
def test_is_priority_lead_truth_table(
    status: str | None,
    expected_priority: bool,
    expected_reason: str | None,
) -> None:
    lead = _lead(enrichment_status=status, website=None)
    assert is_priority_lead(lead) is expected_priority
    assert priority_reason(lead) == expected_reason


def test_priority_reason_constant_value() -> None:
    """The exposed constants must equal the literal tokens persisted on Lead.

    Pinning the values keeps the public API contract honest — the
    frontend looks for the literal strings ``"not_found"`` and
    ``"social_profile_website"`` to render the priority badge
    tooltip variants.
    """

    assert PRIORITY_REASON_NOT_FOUND == "not_found"
    assert PRIORITY_REASON_SOCIAL_PROFILE_WEBSITE == "social_profile_website"


@pytest.mark.parametrize(
    "website,expected_priority,expected_reason",
    [
        # Social-as-website with an OK enrichment_status → priority.
        (
            "https://facebook.com/Acme",
            True,
            PRIORITY_REASON_SOCIAL_PROFILE_WEBSITE,
        ),
        (
            "https://www.linkedin.com/company/acme",
            True,
            PRIORITY_REASON_SOCIAL_PROFILE_WEBSITE,
        ),
        ("https://instagram.com/acme", True, PRIORITY_REASON_SOCIAL_PROFILE_WEBSITE),
        ("https://x.com/acme", True, PRIORITY_REASON_SOCIAL_PROFILE_WEBSITE),
        # Real corporate websites must NOT trigger.
        ("https://acme.com", False, None),
        ("https://acme.com/about/our-facebook-page", False, None),
        # Empty / missing.
        (None, False, None),
        ("", False, None),
    ],
)
def test_priority_reason_social_website_branch(
    website: str | None,
    expected_priority: bool,
    expected_reason: str | None,
) -> None:
    """``Lead.website`` on a social platform fires priority even when scan was ``ok``."""

    lead = _lead(enrichment_status="ok", website=website)
    assert is_priority_lead(lead) is expected_priority
    assert priority_reason(lead) == expected_reason


def test_priority_reason_precedence_not_found_outranks_social() -> None:
    """A 404'd Facebook URL reads as ``not_found`` — single deterministic winner.

    Both signals fire (``enrichment_status == "not_found"`` AND the
    website is social), but the badge needs a single label. The
    server-confirmed 404 is more confident than the import-time
    hostname pattern, so ``not_found`` wins precedence. The lead is
    still in the priority tier.
    """

    lead = _lead(
        enrichment_status="not_found",
        website="https://facebook.com/Acme",
    )
    assert is_priority_lead(lead) is True
    assert priority_reason(lead) == PRIORITY_REASON_NOT_FOUND
