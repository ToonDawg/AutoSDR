"""Operational status: paused / connector / quotas / LLM usage.

This is the one endpoint the top bar refetches every few seconds, so it
stays deliberately cheap: a single SELECT per active campaign and whatever
the LLM usage counter already has cached in memory.
"""

from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, Request
from sqlalchemy import func, select

from autosdr import killswitch
from autosdr.api.deps import db_session
from autosdr.api.schemas import (
    CampaignQuota,
    LlmUsage,
    NetworkingStatusOut,
    PausedInboundStatus,
    SchedulerInfo,
    SystemStatusOut,
    TailscaleProbeOut,
)
from autosdr.connectors import get_connector
from autosdr.llm import get_usage_snapshot
from autosdr.models import (
    Campaign,
    CampaignStatus,
    PausedInbound,
    Workspace,
)
from autosdr.networking import networking_status
from autosdr.pipeline.replay import drain_paused_inbounds
from autosdr.quota import count_outreach_contacts_today_bulk

router = APIRouter(prefix="/api/status", tags=["status"])
logger = logging.getLogger(__name__)


def _paused_inbound_status(session) -> PausedInboundStatus:
    """Cheap aggregate: pending count + oldest pending timestamp.

    Both columns are covered by ``idx_paused_inbound_pending`` so this
    is a single B-tree probe per call. Safe to compute on every
    ``/api/status`` hit (the top bar refetches every few seconds).
    """

    pending_count = (
        session.execute(
            select(func.count(PausedInbound.id)).where(
                PausedInbound.replayed_at.is_(None)
            )
        ).scalar_one()
    )
    oldest = (
        session.execute(
            select(func.min(PausedInbound.created_at)).where(
                PausedInbound.replayed_at.is_(None)
            )
        ).scalar()
    )
    return PausedInboundStatus(
        pending_count=int(pending_count or 0),
        oldest_pending_at=oldest,
    )


@router.get("", response_model=SystemStatusOut)
def get_status() -> SystemStatusOut:
    paused = killswitch.is_flag_set()

    with db_session() as session:
        workspace = session.query(Workspace).first()
        setup_required = workspace is None
        settings_blob = (workspace.settings if workspace else {}) or {}
        campaigns_out: list[CampaignQuota] = []

        active_connector = "file"
        override_to = None
        auto_reply_enabled = False

        if workspace is not None:
            rehearsal = settings_blob.get("rehearsal") or {}
            connector_cfg = settings_blob.get("connector") or {}
            active_connector = str(connector_cfg.get("type") or "file")
            override_to = rehearsal.get("override_to") or None
            auto_reply_enabled = bool(settings_blob.get("auto_reply_enabled", False))

            # If we can cheaply surface the *actually-running* connector type
            # (override-wrapped connectors advertise themselves as e.g.
            # "smsgate+override"), prefer that for the badge.
            try:
                connector = get_connector()
                active_connector = getattr(connector, "connector_type", active_connector)
            except Exception:
                pass

            active_campaigns = list(
                session.query(Campaign)
                .filter(Campaign.status == CampaignStatus.ACTIVE)
                .all()
            )
            sent_today_by_campaign = count_outreach_contacts_today_bulk(
                session, [c.id for c in active_campaigns]
            )
            for campaign in active_campaigns:
                campaigns_out.append(
                    CampaignQuota(
                        id=campaign.id,
                        name=campaign.name,
                        sent_today=sent_today_by_campaign.get(campaign.id, 0),
                        quota=campaign.outreach_per_day,
                    )
                )

        scheduler = SchedulerInfo(
            tick_s=int(settings_blob.get("scheduler_tick_s", 60)),
            poll_s=int(settings_blob.get("inbound_poll_s", 20)),
        )

        usage = get_usage_snapshot()
        llm_usage = LlmUsage(
            calls_today=int(usage.get("total_calls", 0)),
            tokens_in_today=int(usage.get("total_tokens_in", 0)),
            tokens_out_today=int(usage.get("total_tokens_out", 0)),
            estimated_cost_today_usd=float(usage.get("total_cost_usd", 0.0)),
        )

        paused_inbound = _paused_inbound_status(session)

    return SystemStatusOut(
        paused=paused,
        started_at=None,
        active_connector=active_connector,
        override_to=override_to,
        auto_reply_enabled=auto_reply_enabled,
        setup_required=setup_required,
        llm_usage=llm_usage,
        campaigns=campaigns_out,
        scheduler=scheduler,
        paused_inbound=paused_inbound,
    )


@router.post("/pause", response_model=SystemStatusOut)
def pause_system() -> SystemStatusOut:
    killswitch.touch_flag()
    return get_status()


@router.get("/networking", response_model=NetworkingStatusOut)
def get_networking_status(request: Request) -> NetworkingStatusOut:
    """Operator-facing networking diagnostics (ticket 0005 unit 8).

    Reports the configured ``HOST``/``port``, whether AutoSDR is
    bound for remote access, the Tailscale probe state, and the
    resolved ``dashboard_origin`` (override → request Host).
    """

    state = networking_status()
    push_block: dict = {}
    with db_session() as session:
        workspace = session.query(Workspace).first()
        if workspace is not None:
            push_block = (workspace.settings or {}).get("push") or {}
    override = (push_block.get("dashboard_origin") or "").strip() or None
    request_origin = None
    forwarded_host = request.headers.get("x-forwarded-host")
    request_host = forwarded_host or request.headers.get("host")
    if request_host:
        scheme = (
            request.headers.get("x-forwarded-proto")
            or request.url.scheme
            or "http"
        )
        request_origin = f"{scheme}://{request_host}"
    return NetworkingStatusOut(
        host=state.host,
        port=state.port,
        bound_for_remote_access=state.bound_for_remote_access,
        tailscale=TailscaleProbeOut(
            state=state.tailscale.state,
            detail=state.tailscale.detail,
        ),
        warning=state.warning,
        dashboard_origin_override=override,
        dashboard_origin_resolved=override or request_origin,
        request_origin=request_origin,
    )


@router.post("/resume", response_model=SystemStatusOut)
async def resume_system() -> SystemStatusOut:
    """Resume processing and kick off a background drain of paused inbounds.

    The drain is fire-and-forget (``asyncio.create_task``) so the
    operator's resume click returns immediately. The status endpoint
    surfaces ``paused_inbound.pending_count`` so they can watch the
    queue empty in the top bar.

    Per ticket 0009 OQ2 (resolved): blocking the resume request would
    tie it up for as long as the queue takes to walk — at one
    classify-plus-suggestion call per inbound, a queue of 30 messages
    could be a minute or more.
    """

    killswitch.remove_flag()

    async def _drain_and_log() -> None:
        try:
            await drain_paused_inbounds()
        except Exception:  # pragma: no cover - defensive
            logger.exception("paused-inbound drain task crashed")

    asyncio.create_task(_drain_and_log())

    return get_status()


__all__ = ["router"]
