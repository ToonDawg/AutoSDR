"""Calendar-day outreach-quota accounting.

``outreach_per_day`` is enforced as a **calendar day** that resets at
server-local midnight. The previous shape was a rolling 24-hour window;
operators reported it as confusing because the counter never visibly
"resets" at the start of a working day — a campaign that hit cap at
4pm yesterday was still capped at 8am today. The midnight reset makes
the daily budget match the operator's mental model: each scheduled day
in the working-hours window starts fresh.

Trade-off vs. the old rolling-24h shape: a campaign with the
working-hours pacer disabled could now stack a full day's quota
shortly after midnight and another full day's quota during the
following day. With the default pacer enabled (8am–5pm window) the
quota gate effectively resets when the window opens, so the practical
behaviour matches what the operator sees on the dashboard.

Semantics: one **outreach contact** is one *new conversation started*,
i.e. the first AI message on a thread. Follow-up beats and auto-reply
messages are extra texts on a thread that's already been contacted, so
they don't consume a second quota slot. Pre-1.x revisions of this
module counted every outbound AI message — which silently halved the
effective daily cap as soon as the operator turned the follow-up beat
on.

Keep this module stateless and import-light so it can be used from
both request handlers and the scheduler tick without pulling in heavy
deps.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime, timezone

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from autosdr.models import Campaign, CampaignLead, Lead, Message, MessageRole, Thread


def today_start_utc(now_local: datetime | None = None) -> datetime:
    """Return today's server-local midnight, expressed in UTC.

    The cutoff is anchored to the OS's local timezone — the same
    convention :mod:`autosdr.pacing` uses for the working-hours
    window. AutoSDR is single-tenant on the operator's laptop so
    "local" is unambiguous; the day a workspace IANA timezone setting
    becomes a thing, this helper is the only place that needs to
    consult it.

    ``now_local`` is injectable so tests can drive the rollover at a
    fixed clock; production callers leave it ``None`` and the OS
    clock is used.
    """

    clock = now_local or datetime.now().astimezone()
    if clock.tzinfo is None:
        clock = clock.astimezone()
    midnight_local = clock.replace(hour=0, minute=0, second=0, microsecond=0)
    return midnight_local.astimezone(timezone.utc)


def count_outreach_contacts_today(
    session: Session,
    campaign_id: str,
    *,
    now_local: datetime | None = None,
) -> int:
    """Return how many *new outreach contacts* the campaign opened today.

    A contact is one thread whose first AI message landed at-or-after
    today's server-local midnight — so a follow-up beat that fires 10s
    after the initial send still only counts as one contact, and an
    auto-reply sent days later doesn't re-charge the lead's quota
    slot.
    """

    counts = count_outreach_contacts_today_bulk(
        session, [campaign_id], now_local=now_local
    )
    return counts.get(campaign_id, 0)


def count_outreach_contacts_today_bulk(
    session: Session,
    campaign_ids: Iterable[str],
    *,
    now_local: datetime | None = None,
) -> dict[str, int]:
    """Batched variant of :func:`count_outreach_contacts_today`.

    Returns ``{campaign_id: count}`` for every id asked for — including
    zero entries for campaigns that opened no new threads today. The
    status endpoint and campaign list previously ran one query per
    active campaign, which scales linearly with the workspace.

    Implementation: per thread, take the timestamp of the first AI
    message (``MIN(message.created_at) WHERE role='ai'``); the thread
    counts iff that timestamp is at-or-after today's local midnight.
    Follow-up sends and auto-replies sit later in the thread by
    definition, so they're naturally excluded.
    """

    ids = list(dict.fromkeys(campaign_ids))
    if not ids:
        return {}

    cutoff = today_start_utc(now_local)

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


def count_outreach_contacts_per_category_today(
    session: Session,
    campaign_id: str,
    *,
    now_local: datetime | None = None,
) -> dict[str | None, int]:
    """Return ``{Lead.category: contacts_today}`` for one campaign.

    Same definition of "contact" as :func:`count_outreach_contacts_today`
    (one thread per first AI message, respecting today's local midnight
    cutoff and ``quota_reset_at``) but bucketed by the lead's
    ``category``. Used by the scheduler's category-rotation picker to
    bias toward under-represented buckets so a plumber-heavy queue
    doesn't burn the whole day on plumbers.

    Categories with zero contacts are simply absent from the dict —
    the caller treats a missing key as ``0``. ``Lead.category`` may be
    ``None``; that is preserved as a distinct ``None`` bucket so
    uncategorised leads rotate among themselves rather than collapsing
    into another category's count.
    """

    cutoff = today_start_utc(now_local)

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


__all__ = [
    "count_outreach_contacts_today",
    "count_outreach_contacts_today_bulk",
    "count_outreach_contacts_per_category_today",
    "today_start_utc",
]
