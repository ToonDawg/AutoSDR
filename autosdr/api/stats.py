"""Aggregate stats for the Dashboard sparkline + angle-funnel insights."""

from __future__ import annotations

from collections import OrderedDict
from datetime import date, datetime, timedelta, timezone
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query
from sqlalchemy import case, exists, func, select

from autosdr.api.deps import db_session, require_workspace
from autosdr.api.schemas import (
    AngleFunnelOut,
    AngleFunnelRow,
    EnrichmentFilter,
    Sends14dOut,
    SendsByDay,
)
from autosdr.models import (
    Campaign,
    CampaignLead,
    Message,
    MessageRole,
    Thread,
    ThreadStatus,
)

router = APIRouter(prefix="/api/stats", tags=["stats"])


# Default workspace-scoped time window. When the caller scopes to a
# campaign, the default switches to "no time filter" (campaign-lifetime),
# because campaigns rarely run for more than a few weeks and trimming
# them to 30 days would silently hide early threads.
_DEFAULT_WORKSPACE_WINDOW_DAYS = 30
_UNKNOWN_BUCKET = "unknown"


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


@router.get("/angle-funnel", response_model=AngleFunnelOut)
def angle_funnel(
    campaign_id: str | None = Query(default=None),
    since_days: int | None = Query(
        default=None,
        ge=1,
        le=365,
        description=(
            "Override the time window in days. Omit for the defaults: "
            f"{_DEFAULT_WORKSPACE_WINDOW_DAYS} days workspace-scoped, "
            "campaign-lifetime when scoped to a campaign."
        ),
    ),
    enrichment: Annotated[
        EnrichmentFilter,
        Query(
            description=(
                "Stratify the funnel by lead-website enrichment outcome. "
                "``enriched`` keeps only threads whose first AI message has "
                "``metadata.analysis.enrichment_status == 'ok'``; "
                "``unenriched`` is the complement; ``all`` is the default."
            ),
        ),
    ] = "all",
) -> AngleFunnelOut:
    """Per-angle funnel: ``threads`` / ``replied`` / ``won`` / ``lost``.

    Replies are detected by ``Message.role = 'lead'`` existence on the
    thread — the more honest signal than ``CampaignLead.status``, which
    can lag behind the actual inbound. Won / lost reflect the
    terminal :class:`Thread.status` set by the operator (or auto-reply
    pipeline) when a thread closes.

    NULL ``angle_type`` (legacy threads written before this column
    existed) is bucketed as ``"unknown"`` so the operator sees them
    rather than silently dropping them.

    The aggregation is a single query — no N+1 — and uses portable
    SQL: ``SUM(CASE WHEN …)`` instead of dialect-specific
    ``COUNT(DISTINCT … WHERE …)``.
    """

    with db_session() as session:
        require_workspace(session)

        if campaign_id is not None:
            campaign = session.get(Campaign, campaign_id)
            if campaign is None:
                raise HTTPException(status_code=404, detail="campaign not found")

        if since_days is not None:
            since_dt: datetime | None = datetime.now(tz=timezone.utc) - timedelta(
                days=since_days
            )
        elif campaign_id is not None:
            since_dt = None
        else:
            since_dt = datetime.now(tz=timezone.utc) - timedelta(
                days=_DEFAULT_WORKSPACE_WINDOW_DAYS
            )

        # Replied: at least one lead-role message exists on the thread.
        # Correlated EXISTS subquery — keeps the GROUP BY single-pass and
        # avoids double-counting threads that received multiple replies.
        replied_exists = (
            select(Message.id)
            .where(
                Message.thread_id == Thread.id,
                Message.role == MessageRole.LEAD,
            )
            .exists()
            .correlate(Thread)
        )

        bucket = func.coalesce(Thread.angle_type, _UNKNOWN_BUCKET).label("bucket")
        threads_count = func.count(Thread.id).label("threads_count")
        replied_count = func.sum(
            case((replied_exists, 1), else_=0)
        ).label("replied_count")
        won_count = func.sum(
            case((Thread.status == ThreadStatus.WON, 1), else_=0)
        ).label("won_count")
        lost_count = func.sum(
            case((Thread.status == ThreadStatus.LOST, 1), else_=0)
        ).label("lost_count")

        stmt = select(
            bucket,
            threads_count,
            replied_count,
            won_count,
            lost_count,
        ).group_by(bucket)

        if campaign_id is not None:
            # Thread → CampaignLead → Campaign. We only need
            # CampaignLead for the campaign filter; no JOIN to Campaign
            # itself is required because ``campaign_lead.campaign_id``
            # already carries the value.
            stmt = stmt.join(
                CampaignLead, CampaignLead.id == Thread.campaign_lead_id
            ).where(CampaignLead.campaign_id == campaign_id)

        if since_dt is not None:
            stmt = stmt.where(Thread.created_at >= since_dt)

        # Enrichment stratifier — correlated EXISTS over AI messages
        # on this thread that carry the analysis enrichment_status in
        # their metadata. Uses SQLAlchemy's portable JSON indexed access
        # so the same query renders ``json_extract`` on SQLite and
        # ``->>`` on Postgres without us hand-coding either dialect.
        if enrichment != "all":
            enrichment_status_expr = (
                Message.metadata_["analysis"]["enrichment_status"].as_string()
            )
            ai_message_with_status_ok = (
                select(Message.id)
                .where(
                    Message.thread_id == Thread.id,
                    Message.role == MessageRole.AI,
                    enrichment_status_expr == "ok",
                )
                .exists()
                .correlate(Thread)
            )
            if enrichment == "enriched":
                stmt = stmt.where(ai_message_with_status_ok)
            else:  # "unenriched"
                stmt = stmt.where(~ai_message_with_status_ok)

        rows = session.execute(stmt).all()

    rows_sorted = sorted(rows, key=lambda r: r.threads_count, reverse=True)
    return AngleFunnelOut(
        since=since_dt,
        campaign_id=campaign_id,
        enrichment=enrichment,
        rows=[
            AngleFunnelRow(
                angle=row.bucket,
                threads=int(row.threads_count or 0),
                replied=int(row.replied_count or 0),
                won=int(row.won_count or 0),
                lost=int(row.lost_count or 0),
            )
            for row in rows_sorted
        ],
    )


__all__ = ["router"]
