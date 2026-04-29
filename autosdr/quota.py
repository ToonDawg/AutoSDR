"""Rolling-24h outreach-quota accounting.

``outreach_per_day`` is enforced as a rolling window, not a calendar day, to
avoid midnight-burst behaviour and keep the limit predictable regardless of
when an owner activates the campaign. The scheduler, the status endpoint,
the campaigns endpoint, and the CLI all need the same count — previously
each had its own identical private helper, which made it too easy for them
to drift (different cutoff, different role filter, etc.).

Semantics: one **outreach contact** is one *new conversation started*, i.e.
the first AI message on a thread. Follow-up beats and auto-reply messages
are extra texts on a thread that's already been contacted, so they don't
consume a second quota slot. Pre-1.x revisions of this module counted every
outbound AI message — which silently halved the effective daily cap as soon
as the operator turned the follow-up beat on.

Keep this module stateless and import-light so it can be used from both
request handlers and the scheduler tick without pulling in heavy deps.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from autosdr.models import Campaign, CampaignLead, Lead, Message, MessageRole, Thread


def count_outreach_contacts_last_24h(session: Session, campaign_id: str) -> int:
    """Return how many *new outreach contacts* the campaign opened in the last 24h.

    A contact is one thread whose first AI message landed inside the
    rolling window — so a follow-up beat that fires 10s after the
    initial send still only counts as one contact, and an auto-reply
    sent days later doesn't re-charge the lead's quota slot.
    """

    counts = count_outreach_contacts_last_24h_bulk(session, [campaign_id])
    return counts.get(campaign_id, 0)


def count_outreach_contacts_last_24h_bulk(
    session: Session, campaign_ids: Iterable[str]
) -> dict[str, int]:
    """Batched variant of :func:`count_outreach_contacts_last_24h`.

    Returns ``{campaign_id: count}`` for every id asked for — including
    zero entries for campaigns that opened no new threads in the window.
    The status endpoint and campaign list previously ran one query per
    active campaign, which scales linearly with the workspace.

    Implementation: per thread, take the timestamp of the first AI
    message (``MIN(message.created_at) WHERE role='ai'``); the thread
    counts iff that timestamp is inside the window. Follow-up sends and
    auto-replies sit later in the thread by definition, so they're
    naturally excluded.
    """

    ids = list(dict.fromkeys(campaign_ids))
    if not ids:
        return {}

    cutoff = datetime.now(tz=timezone.utc) - timedelta(hours=24)

    # Per-thread first-AI timestamp, scoped to campaigns we care about.
    first_ai = (
        select(
            Thread.id.label("thread_id"),
            CampaignLead.campaign_id.label("campaign_id"),
            func.min(Message.created_at).label("first_ai_at"),
        )
        .join(Thread, Thread.id == Message.thread_id)
        .join(CampaignLead, CampaignLead.id == Thread.campaign_lead_id)
        .where(
            CampaignLead.campaign_id.in_(ids),
            Message.role == MessageRole.AI,
        )
        .group_by(Thread.id, CampaignLead.campaign_id)
    ).subquery()

    stmt = (
        select(first_ai.c.campaign_id, func.count(first_ai.c.thread_id))
        .join(Campaign, Campaign.id == first_ai.c.campaign_id)
        .where(
            first_ai.c.first_ai_at >= cutoff,
            or_(
                Campaign.quota_reset_at.is_(None),
                first_ai.c.first_ai_at >= Campaign.quota_reset_at,
            ),
        )
        .group_by(first_ai.c.campaign_id)
    )
    counts: dict[str, int] = {cid: 0 for cid in ids}
    for campaign_id, count in session.execute(stmt).all():
        counts[campaign_id] = int(count or 0)
    return counts


def count_outreach_contacts_per_category_24h(
    session: Session, campaign_id: str
) -> dict[str | None, int]:
    """Return ``{Lead.category: contacts_in_last_24h}`` for one campaign.

    Same definition of "contact" as :func:`count_outreach_contacts_last_24h`
    (one thread per first AI message, respecting ``quota_reset_at``) but
    bucketed by the lead's ``category``. Used by the scheduler's
    category-rotation picker to bias toward under-represented buckets so
    a plumber-heavy queue doesn't burn the whole day on plumbers.

    Categories with zero contacts are simply absent from the dict — the
    caller treats a missing key as ``0``. ``Lead.category`` may be
    ``None``; that is preserved as a distinct ``None`` bucket so
    uncategorised leads rotate among themselves rather than collapsing
    into another category's count.
    """

    cutoff = datetime.now(tz=timezone.utc) - timedelta(hours=24)

    first_ai = (
        select(
            Thread.id.label("thread_id"),
            CampaignLead.campaign_id.label("campaign_id"),
            Lead.category.label("category"),
            func.min(Message.created_at).label("first_ai_at"),
        )
        .join(Thread, Thread.id == Message.thread_id)
        .join(CampaignLead, CampaignLead.id == Thread.campaign_lead_id)
        .join(Lead, Lead.id == CampaignLead.lead_id)
        .where(
            CampaignLead.campaign_id == campaign_id,
            Message.role == MessageRole.AI,
        )
        .group_by(Thread.id, CampaignLead.campaign_id, Lead.category)
    ).subquery()

    stmt = (
        select(first_ai.c.category, func.count(first_ai.c.thread_id))
        .join(Campaign, Campaign.id == first_ai.c.campaign_id)
        .where(
            first_ai.c.first_ai_at >= cutoff,
            or_(
                Campaign.quota_reset_at.is_(None),
                first_ai.c.first_ai_at >= Campaign.quota_reset_at,
            ),
        )
        .group_by(first_ai.c.category)
    )

    counts: dict[str | None, int] = {}
    for category, count in session.execute(stmt).all():
        counts[category] = int(count or 0)
    return counts


# Back-compat aliases — the previous public names referenced "ai_messages"
# but the semantics changed to "outreach contacts" (one per thread, not one
# per AI send). Callers can migrate at their leisure; both names point at
# the same implementation.
count_ai_messages_last_24h = count_outreach_contacts_last_24h
count_ai_messages_last_24h_bulk = count_outreach_contacts_last_24h_bulk


__all__ = [
    "count_ai_messages_last_24h",
    "count_ai_messages_last_24h_bulk",
    "count_outreach_contacts_last_24h",
    "count_outreach_contacts_last_24h_bulk",
    "count_outreach_contacts_per_category_24h",
]
