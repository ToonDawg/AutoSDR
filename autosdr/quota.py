"""Rolling-24h send-quota accounting.

``outreach_per_day`` is enforced as a rolling window, not a calendar day, to
avoid midnight-burst behaviour and keep the limit predictable regardless of
when an owner activates the campaign. The scheduler, the status endpoint,
the campaigns endpoint, and the CLI all need the same count — previously
each had its own identical private helper, which made it too easy for them
to drift (different cutoff, different role filter, etc.).

Keep this module stateless and import-light so it can be used from both
request handlers and the scheduler tick without pulling in heavy deps.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from autosdr.models import CampaignLead, Message, MessageRole, Thread


def count_ai_messages_last_24h(session: Session, campaign_id: str) -> int:
    """Return how many AI messages the given campaign has sent in the last 24h.

    Only ``MessageRole.AI`` rows count — inbound replies are free and should
    not consume quota.
    """

    counts = count_ai_messages_last_24h_bulk(session, [campaign_id])
    return counts.get(campaign_id, 0)


def count_ai_messages_last_24h_bulk(
    session: Session, campaign_ids: Iterable[str]
) -> dict[str, int]:
    """Batched variant of ``count_ai_messages_last_24h``.

    Returns ``{campaign_id: count}`` for every id asked for — including
    zero entries for campaigns that have sent nothing in the window. The
    status endpoint and campaign list previously ran one query per
    active campaign, which scales linearly with the workspace.
    """

    ids = list(dict.fromkeys(campaign_ids))
    if not ids:
        return {}

    cutoff = datetime.now(tz=timezone.utc) - timedelta(hours=24)
    stmt = (
        select(CampaignLead.campaign_id, func.count(Message.id))
        .join(Thread, Thread.id == Message.thread_id)
        .join(CampaignLead, CampaignLead.id == Thread.campaign_lead_id)
        .where(
            CampaignLead.campaign_id.in_(ids),
            Message.role == MessageRole.AI,
            Message.created_at >= cutoff,
        )
        .group_by(CampaignLead.campaign_id)
    )
    counts: dict[str, int] = {cid: 0 for cid in ids}
    for campaign_id, count in session.execute(stmt).all():
        counts[campaign_id] = int(count or 0)
    return counts


__all__ = ["count_ai_messages_last_24h", "count_ai_messages_last_24h_bulk"]
