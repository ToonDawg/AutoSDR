"""Operational status: paused / connector / quotas / LLM usage.

This is the one endpoint the top bar refetches every few seconds, so it
stays deliberately cheap: a single SELECT per active campaign and whatever
the LLM usage counter already has cached in memory.
"""

from __future__ import annotations

from fastapi import APIRouter

from autosdr import killswitch
from autosdr.api.deps import db_session
from autosdr.api.schemas import (
    CampaignQuota,
    LlmUsage,
    SchedulerInfo,
    SystemStatusOut,
)
from autosdr.connectors import get_connector
from autosdr.llm import get_usage_snapshot
from autosdr.models import (
    Campaign,
    CampaignStatus,
    Workspace,
)
from autosdr.quota import count_ai_messages_last_24h_bulk

router = APIRouter(prefix="/api/status", tags=["status"])


@router.get("", response_model=SystemStatusOut)
def get_status() -> SystemStatusOut:
    paused = killswitch.is_flag_set()

    with db_session() as session:
        workspace = session.query(Workspace).first()
        setup_required = workspace is None
        settings_blob = (workspace.settings if workspace else {}) or {}
        campaigns_out: list[CampaignQuota] = []

        active_connector = "file"
        dry_run = False
        override_to = None
        auto_reply_enabled = False

        if workspace is not None:
            rehearsal = settings_blob.get("rehearsal") or {}
            connector_cfg = settings_blob.get("connector") or {}
            active_connector = str(connector_cfg.get("type") or "file")
            dry_run = bool(rehearsal.get("dry_run", False))
            override_to = rehearsal.get("override_to") or None
            auto_reply_enabled = bool(settings_blob.get("auto_reply_enabled", False))

            # If we can cheaply surface the *actually-running* connector type
            # (which may differ from the configured one while dry_run is on),
            # prefer that for the badge.
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
            sent_24h_by_campaign = count_ai_messages_last_24h_bulk(
                session, [c.id for c in active_campaigns]
            )
            for campaign in active_campaigns:
                campaigns_out.append(
                    CampaignQuota(
                        id=campaign.id,
                        name=campaign.name,
                        sent_24h=sent_24h_by_campaign.get(campaign.id, 0),
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
            estimated_cost_today_usd=0.0,
        )

    return SystemStatusOut(
        paused=paused,
        started_at=None,
        active_connector=active_connector,
        dry_run=dry_run,
        override_to=override_to,
        auto_reply_enabled=auto_reply_enabled,
        setup_required=setup_required,
        llm_usage=llm_usage,
        campaigns=campaigns_out,
        scheduler=scheduler,
    )


@router.post("/pause", response_model=SystemStatusOut)
def pause_system() -> SystemStatusOut:
    killswitch.touch_flag()
    return get_status()


@router.post("/resume", response_model=SystemStatusOut)
def resume_system() -> SystemStatusOut:
    killswitch.remove_flag()
    return get_status()


__all__ = ["router"]
