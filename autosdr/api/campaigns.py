"""Campaign CRUD + lead assignment.

Everything the former ``autosdr campaign`` CLI subcommands did now lives
here, so the operator can build a campaign end-to-end without shelling in.
"""

from __future__ import annotations

from collections import OrderedDict
from collections.abc import Iterable
from datetime import date, datetime, timedelta, timezone

from fastapi import APIRouter, HTTPException, Query, Response, status
from sqlalchemy import ColumnElement, delete, func, or_, select
from sqlalchemy.orm import Session

from autosdr import killswitch
from autosdr.api.deps import db_session, require_workspace
from autosdr.api.schemas import (
    CampaignAssignLeads,
    CampaignAssignLeadsOut,
    CampaignCreate,
    CampaignKickoffRequest,
    CampaignKickoffResult,
    CampaignOut,
    CampaignPatch,
    CampaignTimeseriesBucket,
    CampaignTimeseriesOut,
    FollowupConfig,
    OutreachWindowConfig,
)
from autosdr.connectors import ConnectorError, get_connector
from autosdr.enrichment_vocab import SOCIAL_HOSTS
from autosdr.models import (
    Campaign,
    CampaignLead,
    CampaignLeadStatus,
    CampaignStatus,
    Lead,
    LeadStatus,
    LlmCall,
    Message,
    MessageRole,
    Thread,
    ThreadStatus,
    Workspace,
)
from autosdr.pacing import resolve_window
from autosdr.pipeline.followup import DEFAULT_FOLLOWUP_TEMPLATE
from autosdr.pipeline.priority import PRIORITY_REASON_NOT_FOUND
from autosdr.quota import count_outreach_contacts_today_bulk
from autosdr.scheduler import _count_queued_leads, run_campaign_outreach_batch

router = APIRouter(prefix="/api/campaigns", tags=["campaigns"])

_CAMPAIGN_LEAD_STATUSES = (
    "queued",
    "sending",
    "paused_for_hitl",
    "contacted",
    "replied",
    "won",
    "lost",
    "skipped",
)


def _followup_for_out(raw: dict | None) -> FollowupConfig:
    """Coerce a raw ``campaign.followup`` blob to the response schema.

    ``None`` is treated as "feature off, defaults elsewhere" — same
    defaults the Pydantic model would apply. Enabled rows with blank
    templates inherit the backend default so the UI reflects what will send.
    """

    if not raw:
        return FollowupConfig()
    cfg = FollowupConfig.model_validate(raw)
    if cfg.enabled and not cfg.template.strip():
        cfg.template = DEFAULT_FOLLOWUP_TEMPLATE
    return cfg


def _followup_to_storage(value: FollowupConfig | None) -> dict | None:
    """Normalise an inbound ``FollowupConfig`` for persistence.

    ``None`` (operator didn't touch the field) is stored as ``None`` —
    equivalent to "disabled, use defaults if we ever flip it on". A
    provided object always persists as a full dict so downstream readers
    don't have to cope with half-filled rows.
    """

    if value is None:
        return None
    data = value.model_dump()
    if data.get("enabled") and not str(data.get("template") or "").strip():
        data["template"] = DEFAULT_FOLLOWUP_TEMPLATE
    return data


def _outreach_window_for_out(raw: dict | None) -> OutreachWindowConfig | None:
    """Public-facing override blob — ``None`` (= inherit) preserved as None."""

    if raw is None:
        return None
    return OutreachWindowConfig.model_validate(raw)


def _outreach_window_to_storage(
    value: OutreachWindowConfig | None,
) -> dict | None:
    """Normalise an inbound override for persistence.

    ``None`` is stored as ``None`` (= inherit the workspace default). A
    provided object always persists as a full dict so the resolver
    doesn't have to cope with half-filled rows.
    """

    if value is None:
        return None
    return value.model_dump()


def _effective_outreach_window(
    campaign: Campaign, workspace_settings: dict | None
) -> OutreachWindowConfig:
    """Resolved window for the API response — what the scheduler will use.

    Computed via :func:`autosdr.pacing.resolve_window` so the API and
    the scheduler can't drift on the inheritance / clamping rules.
    """

    resolved = resolve_window(
        campaign_window=campaign.outreach_window,
        workspace_settings=workspace_settings,
    )
    return OutreachWindowConfig(
        enabled=resolved.enabled,
        start_hour=resolved.start_hour,
        end_hour=resolved.end_hour,
    )


def _campaign_totals_bulk(
    session: Session, campaign_ids: Iterable[str]
) -> dict[str, dict[str, int]]:
    """Return per-campaign ``{status: count}`` for every id in one query.

    The list endpoint previously ran a GROUP BY per campaign, which is
    ``O(N)`` round-trips. Batching to a single grouped select keeps the
    whole list page to a constant number of queries.
    """

    ids = list(dict.fromkeys(campaign_ids))
    totals: dict[str, dict[str, int]] = {
        cid: {s: 0 for s in _CAMPAIGN_LEAD_STATUSES} for cid in ids
    }
    if not ids:
        return totals
    rows = session.execute(
        select(CampaignLead.campaign_id, CampaignLead.status, func.count(CampaignLead.id))
        .where(CampaignLead.campaign_id.in_(ids))
        .group_by(CampaignLead.campaign_id, CampaignLead.status)
    ).all()
    for campaign_id, status_name, count in rows:
        bucket = totals.setdefault(
            campaign_id, {s: 0 for s in _CAMPAIGN_LEAD_STATUSES}
        )
        bucket[status_name] = int(count)
    return totals


def _social_website_sql_predicate() -> ColumnElement[bool]:
    """Boolean SQL clause: ``Lead.website`` host is a tracked social platform.

    Mirrors the Python predicate
    :func:`autosdr.enrichment.is_social_website` in SQL space so the
    campaign-bulk count and the picker stay in agreement on which
    leads count as priority. Implementation: case-insensitive
    ``LIKE`` against the two prefix shapes the importer is
    guaranteed to leave on ``Lead.website`` after
    :func:`autosdr.enrichment.normalise_website_url`:

    * ``http(s)://platform.com/...``
    * ``http(s)://www.platform.com/...``

    Other host variants (``m.facebook.com``, ``mobile.x.com``) are
    deliberately NOT matched here — leads land on
    ``Lead.website`` from operator imports, which overwhelmingly
    use the bare host. The Python predicate is the source of
    truth for tier membership; this SQL is just an estimator for
    the dashboard count. Drift between estimator and truth is
    bounded to "we under-count" and surfaces as a missing
    callout, not a wrong send order.

    ``func.lower`` keeps the comparison portable between SQLite
    (case-sensitive default) and Postgres. Sorting the platform
    list keeps generated SQL deterministic in tests.
    """

    clauses: list[ColumnElement[bool]] = []
    for platform in sorted(SOCIAL_HOSTS):
        suffix = f"{platform}.com/%"
        # Two prefix shapes: bare host and `www.`. Both are reachable
        # after `normalise_website_url` runs; the importer doesn't
        # deduplicate them.
        clauses.append(
            func.lower(Lead.website).like(f"http://{suffix}")
        )
        clauses.append(
            func.lower(Lead.website).like(f"https://{suffix}")
        )
        clauses.append(
            func.lower(Lead.website).like(f"http://www.{suffix}")
        )
        clauses.append(
            func.lower(Lead.website).like(f"https://www.{suffix}")
        )
    return or_(*clauses)


def _campaign_queued_priority_bulk(
    session: Session, campaign_ids: Iterable[str]
) -> dict[str, int]:
    """Per-campaign count of queued leads whose enrichment is priority.

    A queued lead counts as priority when EITHER:

    * ``Lead.enrichment_status == "not_found"`` (ticket 0013, the
      strongest broken-website signal — uses the existing composite
      index).
    * ``Lead.website`` is a tracked social-profile URL (ticket 0014;
      see :func:`_social_website_sql_predicate`).

    Single grouped query (no per-campaign round trip) so the
    campaign list endpoint keeps its constant query budget. The
    ``OR`` widens the row scan, but it's bounded to queued
    ``CampaignLead`` rows only, which is small per-campaign.
    """

    ids = list(dict.fromkeys(campaign_ids))
    counts: dict[str, int] = {cid: 0 for cid in ids}
    if not ids:
        return counts
    rows = session.execute(
        select(CampaignLead.campaign_id, func.count(CampaignLead.id))
        .join(Lead, Lead.id == CampaignLead.lead_id)
        .where(
            CampaignLead.campaign_id.in_(ids),
            CampaignLead.status == CampaignLeadStatus.QUEUED,
            or_(
                Lead.enrichment_status == PRIORITY_REASON_NOT_FOUND,
                _social_website_sql_predicate(),
            ),
        )
        .group_by(CampaignLead.campaign_id)
    ).all()
    for campaign_id, count in rows:
        counts[campaign_id] = int(count)
    return counts


def _build_out(
    campaign: Campaign,
    totals: dict[str, int],
    sent_today: int,
    workspace_settings: dict | None,
    queued_priority: int = 0,
) -> CampaignOut:
    """Map per-status counts to the public schema.

    Every ``CampaignLeadStatus`` bucket is exposed as its own field
    using its literal name. Rollups (e.g. "anyone we ever messaged")
    are intentionally not pre-computed server-side — frontend consumers
    sum on demand. See :class:`CampaignOut`.

    ``workspace_settings`` is threaded through so the response can
    expose ``effective_outreach_window`` (the window the scheduler will
    actually use after merging the per-campaign override with the
    workspace default).

    ``queued_priority`` is the subset of ``totals["queued"]`` whose
    leads are priority-tier — broken websites (ticket 0013) plus
    social-profile-as-website (ticket 0014). Defaulted to zero so
    legacy callers that haven't been updated still produce a valid
    response.
    """

    lead_count = sum(totals.values())
    return CampaignOut(
        id=campaign.id,
        name=campaign.name,
        goal=campaign.goal,
        outreach_per_day=campaign.outreach_per_day,
        connector_type=campaign.connector_type,
        status=campaign.status,
        followup=_followup_for_out(campaign.followup),
        outreach_window=_outreach_window_for_out(campaign.outreach_window),
        effective_outreach_window=_effective_outreach_window(
            campaign, workspace_settings
        ),
        quota_reset_at=campaign.quota_reset_at,
        created_at=campaign.created_at,
        lead_count=lead_count,
        queued_count=totals["queued"],
        queued_priority_count=queued_priority,
        sending_count=totals["sending"],
        paused_for_hitl_count=totals["paused_for_hitl"],
        contacted_count=totals["contacted"],
        replied_count=totals["replied"],
        won_count=totals["won"],
        lost_count=totals["lost"],
        skipped_count=totals["skipped"],
        sent_today=sent_today,
    )


def _to_out(session: Session, campaign: Campaign) -> CampaignOut:
    totals = _campaign_totals_bulk(session, [campaign.id])[campaign.id]
    sent_today = count_outreach_contacts_today_bulk(
        session, [campaign.id]
    ).get(campaign.id, 0)
    queued_priority = _campaign_queued_priority_bulk(
        session, [campaign.id]
    ).get(campaign.id, 0)
    workspace = session.query(Workspace).first()
    workspace_settings = workspace.settings if workspace else None
    return _build_out(
        campaign, totals, sent_today, workspace_settings, queued_priority
    )


@router.get("", response_model=list[CampaignOut])
def list_campaigns() -> list[CampaignOut]:
    with db_session() as session:
        workspace = require_workspace(session)
        rows = list(
            session.execute(
                select(Campaign).order_by(Campaign.created_at.desc())
            ).scalars()
        )
        if not rows:
            return []
        ids = [c.id for c in rows]
        totals_by_campaign = _campaign_totals_bulk(session, ids)
        sent_today_by_campaign = count_outreach_contacts_today_bulk(session, ids)
        queued_priority_by_campaign = _campaign_queued_priority_bulk(session, ids)
        ws_settings = workspace.settings
        return [
            _build_out(
                c,
                totals_by_campaign[c.id],
                sent_today_by_campaign.get(c.id, 0),
                ws_settings,
                queued_priority_by_campaign.get(c.id, 0),
            )
            for c in rows
        ]


@router.post("", response_model=CampaignOut, status_code=status.HTTP_201_CREATED)
def create_campaign(payload: CampaignCreate) -> CampaignOut:
    with db_session() as session:
        workspace = require_workspace(session)
        connector_type = (
            payload.connector_type
            or (workspace.settings or {}).get("connector", {}).get("type")
            or "file"
        )
        campaign = Campaign(
            workspace_id=workspace.id,
            name=payload.name.strip(),
            goal=payload.goal.strip(),
            outreach_per_day=max(1, int(payload.outreach_per_day)),
            connector_type=connector_type,
            status=CampaignStatus.DRAFT,
            followup=_followup_to_storage(payload.followup),
            outreach_window=_outreach_window_to_storage(payload.outreach_window),
        )
        session.add(campaign)
        session.flush()
        session.refresh(campaign)
        return _to_out(session, campaign)


@router.get("/{campaign_id}", response_model=CampaignOut)
def get_campaign(campaign_id: str) -> CampaignOut:
    with db_session() as session:
        require_workspace(session)
        campaign = session.get(Campaign, campaign_id)
        if campaign is None:
            raise HTTPException(status_code=404, detail={"error": "campaign_not_found"})
        return _to_out(session, campaign)


def _utc_iso_date(value: object) -> str | None:
    """Coerce a datetime/date to a UTC ``YYYY-MM-DD`` string, or ``None``.

    SQLite stores tz-aware datetimes as ISO strings; SQLAlchemy returns
    them as ``datetime`` objects with the original tzinfo preserved. We
    always normalise to UTC before bucketing so a message at 23:59 local
    on a different timezone lands on the right UTC day.
    """

    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc).date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    return None


@router.get("/{campaign_id}/timeseries", response_model=CampaignTimeseriesOut)
def campaign_timeseries(
    campaign_id: str,
    days: int = Query(default=14, ge=1, le=90),
) -> CampaignTimeseriesOut:
    """Per-day campaign funnel for the last ``days`` UTC days, oldest first.

    Buckets are pre-filled with zeroes so the response always has
    ``days`` rows, even on a brand-new campaign — the chart on
    ``CampaignDetail.tsx`` relies on a stable window length.

    Counters (one query each, all single-pass aggregates):

    * ``sent`` — every ``Message.role = 'ai'`` row in the window
      (follow-ups count separately; this matches the existing
      ``/api/stats/sends-14d`` semantics).
    * ``replied`` — number of threads whose **first ever** lead-message
      lands on that day. A chatty lead replying twice on the same day is
      one ``replied``; a thread that first replied last week and again
      today is counted *last week*, not today.
    * ``won`` / ``lost`` — terminal-status threads bucketed by
      ``Thread.updated_at``. Acknowledged limitation: ``updated_at``
      shifts on any post-close edit (rare). v0 trade-off; documented in
      the ticket.
    """

    end_day = datetime.now(tz=timezone.utc).date()
    start_day = end_day - timedelta(days=days - 1)
    start_dt = datetime.combine(
        start_day, datetime.min.time(), tzinfo=timezone.utc
    )

    blank_bucket = {"sent": 0, "replied": 0, "won": 0, "lost": 0}
    buckets: "OrderedDict[str, dict[str, int]]" = OrderedDict()
    cursor = start_day
    while cursor <= end_day:
        buckets[cursor.isoformat()] = dict(blank_bucket)
        cursor += timedelta(days=1)

    with db_session() as session:
        require_workspace(session)
        campaign = session.get(Campaign, campaign_id)
        if campaign is None:
            raise HTTPException(
                status_code=404, detail={"error": "campaign_not_found"}
            )

        sent_rows = session.execute(
            select(Message.created_at)
            .join(Thread, Thread.id == Message.thread_id)
            .join(CampaignLead, CampaignLead.id == Thread.campaign_lead_id)
            .where(
                CampaignLead.campaign_id == campaign_id,
                Message.role == MessageRole.AI,
                Message.created_at >= start_dt,
            )
        ).all()
        for (created_at,) in sent_rows:
            day = _utc_iso_date(created_at)
            if day is not None and day in buckets:
                buckets[day]["sent"] += 1

        # Per-thread first-reply timestamp. Computed unfiltered (across
        # the full thread history) and then filtered to the window so a
        # thread that first replied months ago doesn't get re-counted on
        # a later same-thread reply that happens to fall inside the
        # window.
        first_replies = (
            select(
                Message.thread_id.label("tid"),
                func.min(Message.created_at).label("first_ts"),
            )
            .join(Thread, Thread.id == Message.thread_id)
            .join(CampaignLead, CampaignLead.id == Thread.campaign_lead_id)
            .where(
                CampaignLead.campaign_id == campaign_id,
                Message.role == MessageRole.LEAD,
            )
            .group_by(Message.thread_id)
        ).subquery()
        reply_rows = session.execute(
            select(first_replies.c.first_ts).where(
                first_replies.c.first_ts >= start_dt
            )
        ).all()
        for (first_ts,) in reply_rows:
            day = _utc_iso_date(first_ts)
            if day is not None and day in buckets:
                buckets[day]["replied"] += 1

        terminal_rows = session.execute(
            select(Thread.status, Thread.updated_at)
            .join(CampaignLead, CampaignLead.id == Thread.campaign_lead_id)
            .where(
                CampaignLead.campaign_id == campaign_id,
                Thread.status.in_([ThreadStatus.WON, ThreadStatus.LOST]),
                Thread.updated_at >= start_dt,
            )
        ).all()
        for thread_status, updated_at in terminal_rows:
            day = _utc_iso_date(updated_at)
            if day is None or day not in buckets:
                continue
            if thread_status == ThreadStatus.WON:
                buckets[day]["won"] += 1
            else:
                buckets[day]["lost"] += 1

    return CampaignTimeseriesOut(
        days=days,
        buckets=[
            CampaignTimeseriesBucket(
                date=day,
                sent=counts["sent"],
                replied=counts["replied"],
                won=counts["won"],
                lost=counts["lost"],
            )
            for day, counts in buckets.items()
        ],
    )


@router.patch("/{campaign_id}", response_model=CampaignOut)
def patch_campaign(campaign_id: str, payload: CampaignPatch) -> CampaignOut:
    # ``exclude_unset`` so only operator-touched fields overwrite the DB —
    # otherwise a PATCH of just ``{name: ...}`` would clobber followup
    # back to defaults.
    updates = payload.model_dump(exclude_unset=True)
    with db_session() as session:
        require_workspace(session)
        campaign = session.get(Campaign, campaign_id)
        if campaign is None:
            raise HTTPException(status_code=404, detail={"error": "campaign_not_found"})
        if "followup" in updates:
            campaign.followup = _followup_to_storage(payload.followup)
            updates.pop("followup")
        if "outreach_window" in updates:
            # Special-case: explicit ``null`` means "clear the
            # per-campaign override and inherit the workspace default";
            # an object means "set the override". Either way the loop
            # below would clobber JSON columns with raw dicts in a way
            # the ORM type adapter doesn't expect, so we handle it here.
            campaign.outreach_window = _outreach_window_to_storage(
                payload.outreach_window
            )
            updates.pop("outreach_window")
        if "outreach_per_day" in updates and updates["outreach_per_day"] is not None:
            updates["outreach_per_day"] = max(1, int(updates["outreach_per_day"]))
        if "name" in updates and updates["name"] is not None:
            updates["name"] = updates["name"].strip()
        if "goal" in updates and updates["goal"] is not None:
            updates["goal"] = updates["goal"].strip()
        for field_name, value in updates.items():
            if value is None:
                continue
            setattr(campaign, field_name, value)
        session.flush()
        session.refresh(campaign)
        return _to_out(session, campaign)


@router.delete("/{campaign_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_campaign(campaign_id: str) -> Response:
    """Delete a campaign and every conversation hanging off it.

    Cascade order (children first, parent last) so foreign-key constraints
    on SQLite + Postgres stay happy:

    1. ``message`` rows for every thread in this campaign
    2. ``thread`` rows for every campaign_lead in this campaign
    3. ``llm_call`` audit rows tagged with this ``campaign_id`` (no FK,
       but we own the data and want it gone with the campaign)
    4. ``campaign_lead`` rows
    5. the ``campaign`` row itself

    Leads are workspace-scoped and may belong to other campaigns, so we
    intentionally do **not** delete or mutate them — the next assignment
    re-uses them as-is. Returns ``204 No Content`` on success and ``404``
    if the campaign doesn't exist (or belongs to a different workspace).
    """

    with db_session() as session:
        require_workspace(session)
        campaign = session.get(Campaign, campaign_id)
        if campaign is None:
            raise HTTPException(
                status_code=404, detail={"error": "campaign_not_found"}
            )

        campaign_lead_ids = list(
            session.execute(
                select(CampaignLead.id).where(
                    CampaignLead.campaign_id == campaign.id
                )
            ).scalars()
        )
        thread_ids = (
            list(
                session.execute(
                    select(Thread.id).where(
                        Thread.campaign_lead_id.in_(campaign_lead_ids)
                    )
                ).scalars()
            )
            if campaign_lead_ids
            else []
        )

        if thread_ids:
            session.execute(
                delete(Message)
                .where(Message.thread_id.in_(thread_ids))
                .execution_options(synchronize_session=False)
            )
            session.execute(
                delete(Thread)
                .where(Thread.id.in_(thread_ids))
                .execution_options(synchronize_session=False)
            )

        session.execute(
            delete(LlmCall)
            .where(LlmCall.campaign_id == campaign.id)
            .execution_options(synchronize_session=False)
        )

        if campaign_lead_ids:
            session.execute(
                delete(CampaignLead)
                .where(CampaignLead.id.in_(campaign_lead_ids))
                .execution_options(synchronize_session=False)
            )

        session.delete(campaign)
        session.flush()

    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/{campaign_id}/reset-send-count", response_model=CampaignOut)
def reset_send_count(campaign_id: str) -> CampaignOut:
    """Start a fresh quota window for this campaign without altering history."""

    with db_session() as session:
        require_workspace(session)
        campaign = session.get(Campaign, campaign_id)
        if campaign is None:
            raise HTTPException(status_code=404, detail={"error": "campaign_not_found"})
        campaign.quota_reset_at = datetime.now(tz=timezone.utc)
        session.flush()
        session.refresh(campaign)
        return _to_out(session, campaign)


@router.post("/{campaign_id}/kickoff", response_model=CampaignKickoffResult)
async def kickoff_campaign(
    campaign_id: str, payload: CampaignKickoffRequest
) -> CampaignKickoffResult:
    """Manually send the next N queued leads for a campaign.

    This is an explicit operator action, so it bypasses the pause flag and the
    campaign's rolling quota. Shutdown still aborts the send path.
    """

    with db_session() as session:
        workspace = require_workspace(session)
        campaign = session.get(Campaign, campaign_id)
        if campaign is None:
            raise HTTPException(status_code=404, detail={"error": "campaign_not_found"})
        if campaign.status == CampaignStatus.COMPLETED:
            raise HTTPException(status_code=409, detail={"error": "campaign_completed"})

        try:
            connector = get_connector()
        except ConnectorError as exc:
            raise HTTPException(
                status_code=409,
                detail={"error": "connector_unavailable", "detail": str(exc)},
            ) from exc

        try:
            with killswitch.allow_manual_send():
                batch = await run_campaign_outreach_batch(
                    session=session,
                    connector=connector,
                    workspace=workspace,
                    campaign=campaign,
                    max_count=payload.count,
                    respect_quota=False,
                )
        except killswitch.KillSwitchTripped as exc:
            raise HTTPException(
                status_code=409, detail={"error": "system_shutting_down"}
            ) from exc

        session.refresh(campaign)
        return CampaignKickoffResult(
            requested=payload.count,
            attempted=batch.attempted,
            sent=batch.sent,
            failed=batch.failed,
            remaining_queued=_count_queued_leads(session, campaign.id),
            campaign=_to_out(session, campaign),
        )


@router.post("/{campaign_id}/activate", response_model=CampaignOut)
def activate_campaign(campaign_id: str) -> CampaignOut:
    return _set_status(campaign_id, CampaignStatus.ACTIVE)


@router.post("/{campaign_id}/pause", response_model=CampaignOut)
def pause_campaign(campaign_id: str) -> CampaignOut:
    return _set_status(campaign_id, CampaignStatus.PAUSED)


@router.post("/{campaign_id}/complete", response_model=CampaignOut)
def complete_campaign(campaign_id: str) -> CampaignOut:
    return _set_status(campaign_id, CampaignStatus.COMPLETED)


def _set_status(campaign_id: str, new_status: str) -> CampaignOut:
    with db_session() as session:
        require_workspace(session)
        campaign = session.get(Campaign, campaign_id)
        if campaign is None:
            raise HTTPException(status_code=404, detail={"error": "campaign_not_found"})
        campaign.status = new_status
        session.flush()
        session.refresh(campaign)
        return _to_out(session, campaign)


@router.post("/{campaign_id}/assign-leads", response_model=CampaignAssignLeadsOut)
def assign_leads(
    campaign_id: str, payload: CampaignAssignLeads
) -> CampaignAssignLeadsOut:
    """Push leads into the campaign queue.

    Two modes:

    * ``all_eligible=true`` assigns every ``status='new'`` lead not already
      in this campaign. This is the one-click flow after import.
    * ``lead_ids=[...]`` assigns a specific set — used by per-lead selection
      in the Leads page.

    Compliance: leads with ``do_not_contact_at IS NOT NULL`` are silently
    excluded in both modes and reported back via ``skipped_lead_ids`` so the
    UI can surface the count.
    """

    with db_session() as session:
        require_workspace(session)
        campaign = session.get(Campaign, campaign_id)
        if campaign is None:
            raise HTTPException(status_code=404, detail={"error": "campaign_not_found"})

        existing_lead_ids = set(
            session.execute(
                select(CampaignLead.lead_id).where(
                    CampaignLead.campaign_id == campaign.id
                )
            ).scalars()
        )
        base_position = int(
            session.execute(
                select(func.coalesce(func.max(CampaignLead.queue_position), 0)).where(
                    CampaignLead.campaign_id == campaign.id
                )
            ).scalar_one()
        )

        if payload.all_eligible:
            candidates = list(
                session.execute(
                    select(Lead)
                    .where(
                        Lead.workspace_id == campaign.workspace_id,
                        Lead.status == LeadStatus.NEW,
                        ~Lead.id.in_(existing_lead_ids) if existing_lead_ids else True,
                    )
                    .order_by(Lead.import_order.asc())
                ).scalars()
            )
        else:
            candidates = list(
                session.execute(
                    select(Lead).where(
                        Lead.workspace_id == campaign.workspace_id,
                        Lead.id.in_(payload.lead_ids or []),
                        ~Lead.id.in_(existing_lead_ids) if existing_lead_ids else True,
                    )
                ).scalars()
            )

        leads: list[Lead] = []
        skipped_lead_ids: list[str] = []
        for lead in candidates:
            if lead.do_not_contact_at is not None:
                skipped_lead_ids.append(lead.id)
                continue
            leads.append(lead)

        for idx, lead in enumerate(leads, start=1):
            session.add(
                CampaignLead(
                    campaign_id=campaign.id,
                    lead_id=lead.id,
                    queue_position=base_position + idx,
                    status=CampaignLeadStatus.QUEUED,
                )
            )
        session.flush()
        session.refresh(campaign)
        out = _to_out(session, campaign)
        return CampaignAssignLeadsOut(
            **out.model_dump(),
            skipped_lead_ids=skipped_lead_ids,
            skipped_reason="do_not_contact" if skipped_lead_ids else None,
        )


__all__ = ["router"]
