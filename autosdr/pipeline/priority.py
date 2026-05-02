"""Lead send-order priority predicate.

A "priority" lead is one whose enrichment signal makes the operator's
pitch land harder than average. Two reasons fire today:

* ``not_found`` — `Lead.enrichment_status == "not_found"` (404/410
  on the website scan, ticket 0013).
* ``social_profile_website`` — `Lead.website` itself is a social-
  profile URL (Facebook page, Instagram, etc., ticket 0014). The
  pitch ("we'll get you a real website") lands harder than for any
  other broken-site state because the operator can see the symptom
  in the imported data alone.

The predicate is consulted by:

* :func:`autosdr.scheduler._next_queued_leads` — to bucket queued
  candidates into a priority tier that is drained before the normal
  tier (see :doc:`docs/tickets/0013-broken-website-priority`).
* :func:`autosdr.api.leads._lead_to_out` (and the list serialiser)
  — to surface the ``is_priority`` / ``priority_reason`` fields on
  :class:`autosdr.api.schemas.LeadOut`.

Module placement: a focused, dependency-light module so the picker
loop and the API serialiser can import it without dragging in the
LLM/prompt stack that lives in :mod:`autosdr.pipeline._shared`. The
predicate is pure: no I/O, no DB, no LLM calls.
"""

from __future__ import annotations

from typing import Final

from autosdr.enrichment import is_social_website
from autosdr.models import Lead

# Closed vocabulary for the reason a lead earned the priority tier.
# Kept as plain ``str`` constants (not a ``Literal`` type) so the
# bulk SQL count in :mod:`autosdr.api.campaigns` and the badge
# tooltip on the frontend can reference the same literal value
# without a Pydantic dance.
PRIORITY_REASON_NOT_FOUND: Final[str] = "not_found"
PRIORITY_REASON_SOCIAL_PROFILE_WEBSITE: Final[str] = "social_profile_website"


def priority_reason(lead: Lead) -> str | None:
    """Return the literal token explaining why ``lead`` is priority, or ``None``.

    Precedence (first matching condition wins):

    1. ``"not_found"`` — the server returned 404/410. The strongest
       confidence we have that the website is broken.
    2. ``"social_profile_website"`` — ``Lead.website`` is on a tracked
       social platform. Strong "no real website" signal even if the
       social profile itself returned 200/blocked.

    A lead that satisfies both conditions reads as ``"not_found"``
    in the badge — single deterministic winner — but is still
    counted in :func:`is_priority_lead` and the bulk priority count.
    The informational ``LeadOut.is_social_website`` field stays set
    independently so the operator can see both signals.
    """

    if lead.enrichment_status == PRIORITY_REASON_NOT_FOUND:
        return PRIORITY_REASON_NOT_FOUND
    if is_social_website(lead.website) is not None:
        return PRIORITY_REASON_SOCIAL_PROFILE_WEBSITE
    return None


def is_priority_lead(lead: Lead) -> bool:
    """``True`` if ``lead`` should be sent before normal-tier leads.

    Purely a function of fields already on the :class:`Lead` row;
    the scan worker is responsible for keeping
    ``lead.enrichment_status`` fresh, and the importer for keeping
    ``lead.website`` honest. The predicate is dynamic — a lead that
    was ``"not_found"`` last week but now resolves loses priority on
    the next picker tick after the scan worker reaches it; an
    operator who edits a Facebook URL out of ``Lead.website`` flips
    the flag immediately on the next read.
    """

    return priority_reason(lead) is not None


__all__ = [
    "PRIORITY_REASON_NOT_FOUND",
    "PRIORITY_REASON_SOCIAL_PROFILE_WEBSITE",
    "is_priority_lead",
    "priority_reason",
]
