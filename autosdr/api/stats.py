"""Aggregate stats for the Dashboard sparkline etc."""

from __future__ import annotations

from collections import OrderedDict
from datetime import date, datetime, timedelta, timezone

from fastapi import APIRouter
from sqlalchemy import select

from autosdr.api.deps import db_session, require_workspace
from autosdr.api.schemas import Sends14dOut, SendsByDay
from autosdr.models import Message, MessageRole

router = APIRouter(prefix="/api/stats", tags=["stats"])


@router.get("/sends-14d", response_model=Sends14dOut)
def sends_14d() -> Sends14dOut:
    """Per-day AI send count for the last 14 days (oldest first)."""

    end_day = datetime.now(tz=timezone.utc).date()
    start_day = end_day - timedelta(days=13)
    start_dt = datetime.combine(start_day, datetime.min.time(), tzinfo=timezone.utc)

    buckets: "OrderedDict[str, int]" = OrderedDict()
    cursor = start_day
    while cursor <= end_day:
        buckets[cursor.isoformat()] = 0
        cursor += timedelta(days=1)

    with db_session() as session:
        require_workspace(session)
        rows = session.execute(
            select(Message.created_at).where(
                Message.role == MessageRole.AI,
                Message.created_at >= start_dt,
            )
        ).all()
        for (created_at,) in rows:
            if isinstance(created_at, datetime):
                day = created_at.astimezone(timezone.utc).date().isoformat()
            elif isinstance(created_at, date):
                day = created_at.isoformat()
            else:
                continue
            if day in buckets:
                buckets[day] += 1

    return Sends14dOut(days=[SendsByDay(date=d, count=c) for d, c in buckets.items()])


__all__ = ["router"]
