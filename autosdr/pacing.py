"""Outreach-window pacing maths.

The scheduler used to send the entire daily quota in a tight burst as
soon as a campaign activated. That reads as a spam cluster to carriers
and to recipients. This module turns the daily quota into a smooth
trickle across a configured working window in server-local time.

Two responsibilities:

* **Resolve the window.** A per-campaign override
  (``campaign.outreach_window``) wins over the workspace default
  (``workspace.settings.outreach_window``). ``None`` on the campaign
  means "inherit". Both shapes are
  ``{enabled, start_hour, end_hour}``.
* **Compute the pacing allowance.** Given a window, today's daily
  quota, the count of sends already made in this window, and the
  current local datetime, return how many sends pacing allows on this
  scheduler tick.

The maths is deliberately simple: target sends so far in the window
``= ceil(daily_quota * elapsed_fraction)``. If actual sends are
already at or above the target, return zero. Otherwise return
``target - actual`` — capped at ``daily_quota`` (defensive). The
scheduler stacks this with ``max_batch_per_tick`` and the daily
calendar-day quota (resets at server-local midnight), so a late
activation can't burst.

Server-local time is the reference (``datetime.now().astimezone()``)
because the POC is single-tenant on the operator's laptop. If AutoSDR
ever runs on a server in a different region from the operator we'll
add a workspace IANA timezone setting; for now keeping the surface
small wins.

Reply turns, manual kickoff, and the follow-up beat are unaffected —
only the scheduler tick consults this module.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from autosdr.models import (
    Campaign,
    CampaignLead,
    Message,
    MessageRole,
    Thread,
)

_DEFAULT_START_HOUR = 8
_DEFAULT_END_HOUR = 17


@dataclass(frozen=True, slots=True)
class OutreachWindow:
    """Resolved working-hours window for a single campaign.

    Constructed via :func:`resolve_window`. Not meant to be built by
    hand — going through the resolver guarantees the same default /
    clamping behaviour every caller gets.
    """

    enabled: bool
    start_hour: int  # inclusive, 0-23
    end_hour: int  # exclusive, 1-24

    @property
    def total_seconds(self) -> int:
        return (self.end_hour - self.start_hour) * 3600


def _coerce_hour(value: Any, default: int, *, lo: int, hi: int) -> int:
    """Clamp an int-ish value into ``[lo, hi]`` with a fallback default.

    Tolerates the JSON blob coming back as a string (which has happened
    in practice — the frontend posts numeric inputs as strings) without
    blowing up the scheduler tick.
    """

    try:
        n = int(value)
    except (TypeError, ValueError):
        return default
    if n < lo:
        return lo
    if n > hi:
        return hi
    return n


def _normalise_window_blob(raw: dict | None) -> OutreachWindow | None:
    """Interpret one ``{enabled, start_hour, end_hour}`` blob.

    Returns ``None`` when the blob doesn't carry enough information to
    decide (so a missing-or-empty workspace default falls through to
    the hardcoded default), and a fully-populated :class:`OutreachWindow`
    otherwise. The caller chooses what to do on ``None``.
    """

    if not isinstance(raw, dict) or not raw:
        return None

    enabled = bool(raw.get("enabled", True))
    start = _coerce_hour(
        raw.get("start_hour"), _DEFAULT_START_HOUR, lo=0, hi=23
    )
    end = _coerce_hour(raw.get("end_hour"), _DEFAULT_END_HOUR, lo=1, hi=24)
    if end <= start:
        # Pathological input: clamp end so the window has a positive width
        # rather than crashing or sending zero forever.
        end = min(24, start + 1)
    return OutreachWindow(enabled=enabled, start_hour=start, end_hour=end)


def resolve_window(
    *,
    campaign_window: dict | None,
    workspace_settings: dict | None,
) -> OutreachWindow:
    """Resolve the effective window for a campaign.

    Precedence: per-campaign override → workspace default → hardcoded
    default (``enabled=True, 08:00-17:00``). The hardcoded default
    matches :data:`autosdr.config.DEFAULT_WORKSPACE_SETTINGS`; we
    duplicate the literal here so this module doesn't import the config
    blob just for the fallback.
    """

    resolved = _normalise_window_blob(campaign_window)
    if resolved is not None:
        return resolved

    ws_blob = (workspace_settings or {}).get("outreach_window")
    resolved = _normalise_window_blob(ws_blob)
    if resolved is not None:
        return resolved

    return OutreachWindow(
        enabled=True,
        start_hour=_DEFAULT_START_HOUR,
        end_hour=_DEFAULT_END_HOUR,
    )


def today_window_bounds(
    window: OutreachWindow, now_local: datetime
) -> tuple[datetime, datetime]:
    """Return ``(start, end)`` of *today's* window in the same tz as ``now_local``.

    "Today" is the local-calendar day of ``now_local``. Callers that
    are outside the window (early morning or evening) should detect
    that themselves via :func:`is_in_window`.
    """

    start = now_local.replace(
        hour=window.start_hour, minute=0, second=0, microsecond=0
    )
    end = now_local.replace(
        hour=0, minute=0, second=0, microsecond=0
    ) + timedelta(hours=window.end_hour)
    return start, end


def is_in_window(window: OutreachWindow, now_local: datetime) -> bool:
    """``True`` iff ``now_local`` falls inside ``[start, end)``."""

    start, end = today_window_bounds(window, now_local)
    return start <= now_local < end


def window_allowance(
    *,
    window: OutreachWindow,
    daily_quota: int,
    sent_in_window: int,
    now_local: datetime,
) -> int:
    """Return the maximum number of sends pacing allows on this tick.

    ``window.enabled=False`` short-circuits to ``daily_quota`` (no
    gate). Outside the window returns ``0``. Inside, returns
    ``max(0, target_sent - sent_in_window)`` where
    ``target_sent = ceil(daily_quota * elapsed_fraction)``.

    The scheduler stacks this with ``max_batch_per_tick`` and the
    calendar-day quota (resets at server-local midnight); all gates
    apply, so a campaign that activated late in the day can't burst
    its full quota in the last hour.
    """

    if daily_quota <= 0:
        return 0
    if not window.enabled:
        return max(0, daily_quota)

    if not is_in_window(window, now_local):
        return 0

    start, end = today_window_bounds(window, now_local)
    total = (end - start).total_seconds()
    if total <= 0:
        # Defensive — _normalise_window_blob should have made this impossible,
        # but if it ever lands here we'd rather permit nothing than divide by 0.
        return 0
    elapsed = (now_local - start).total_seconds()
    elapsed_fraction = elapsed / total
    target_sent = math.ceil(daily_quota * elapsed_fraction)
    target_sent = min(daily_quota, target_sent)
    return max(0, target_sent - sent_in_window)


def count_outreach_contacts_since(
    session: Session,
    campaign_id: str,
    *,
    since_dt_utc: datetime,
) -> int:
    """Count *new outreach contacts* this campaign opened since ``since_dt_utc``.

    Mirrors :func:`autosdr.quota.count_outreach_contacts_today`
    semantics: one contact = one thread whose first AI message landed
    after ``since_dt_utc``. Follow-up beats and auto-replies are extra
    messages on a thread that's already been contacted, so they don't
    count — keeps pacing aligned with the daily-quota meaning of
    ``outreach_per_day``.

    ``Campaign.quota_reset_at`` is honoured the same way as the daily
    quota helper: a reset starts a fresh window, so threads contacted
    before it don't consume pacing budget either.
    """

    first_ai_subq = (
        select(
            Thread.id.label("thread_id"),
            CampaignLead.campaign_id.label("campaign_id"),
            func.min(Message.created_at).label("first_ai_at"),
        )
        .join(Thread, Thread.id == Message.thread_id)
        .join(CampaignLead, CampaignLead.id == Thread.campaign_lead_id)
        .where(
            CampaignLead.campaign_id == campaign_id,
            Message.role == MessageRole.AI,
        )
        .group_by(Thread.id, CampaignLead.campaign_id)
    ).subquery()

    stmt = (
        select(func.count(first_ai_subq.c.thread_id))
        .join(Campaign, Campaign.id == first_ai_subq.c.campaign_id)
        .where(
            first_ai_subq.c.first_ai_at >= since_dt_utc,
            or_(
                Campaign.quota_reset_at.is_(None),
                first_ai_subq.c.first_ai_at >= Campaign.quota_reset_at,
            ),
        )
    )
    return int(session.execute(stmt).scalar_one() or 0)


def count_sends_in_today_window(
    session: Session,
    campaign_id: str,
    *,
    window: OutreachWindow,
    now_local: datetime,
) -> int:
    """Count outreach contacts this campaign opened since today's window start.

    Returns ``0`` for a disabled window — the caller doesn't need a
    pacing reading in that case. The local→UTC conversion uses
    ``now_local``'s tzinfo so a daylight-savings rollover during the
    window doesn't double-count messages on the cusp.
    """

    if not window.enabled:
        return 0
    start_local, _ = today_window_bounds(window, now_local)
    if now_local < start_local:
        # Pre-window: yesterday's window has already closed and today's
        # hasn't opened. The pacing allowance returns 0 anyway, so we
        # short-circuit to avoid a useless query.
        return 0
    if start_local.tzinfo is None:
        start_utc = start_local.replace(tzinfo=timezone.utc)
    else:
        start_utc = start_local.astimezone(timezone.utc)
    return count_outreach_contacts_since(session, campaign_id, since_dt_utc=start_utc)


__all__ = [
    "OutreachWindow",
    "resolve_window",
    "today_window_bounds",
    "is_in_window",
    "window_allowance",
    "count_outreach_contacts_since",
    "count_sends_in_today_window",
]
