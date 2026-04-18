"""Async scheduler.

Two concurrent concerns run inside the scheduler:

* **Outreach tick** — every ``scheduler_tick_s`` seconds, scan active campaigns,
  enforce the rolling 24h quota, and send the next batch of queued leads
  through the outreach pipeline (:func:`run_outreach_for_campaign_lead`).
* **Inbound poll** — every ``inbound_poll_s`` seconds, ask the connector for
  any new inbound messages (``connector.poll_incoming``) and push each through
  the reply pipeline. For TextBee this is how replies arrive — no public URL
  is required. The FileConnector and webhook-only providers return ``[]``, so
  the poll is a no-op for those.

Both run as cooperating asyncio tasks managed by the FastAPI lifespan in
``webhook.create_app``. All three respect the shared kill-switch event.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import and_, func, select
from sqlalchemy.orm import Session

from autosdr import killswitch
from autosdr.config import Settings, get_settings
from autosdr.connectors.base import BaseConnector
from autosdr.db import session_scope
from autosdr.models import (
    Campaign,
    CampaignLead,
    CampaignLeadStatus,
    CampaignStatus,
    Lead,
    Message,
    MessageRole,
    Thread,
    Workspace,
)
from autosdr.pipeline import process_incoming_message, run_outreach_for_campaign_lead

logger = logging.getLogger(__name__)


def _effective_setting(settings_blob: dict, key: str, env_override: int | None, fallback: int) -> int:
    if env_override is not None:
        return env_override
    return int(settings_blob.get(key, fallback))


def _count_ai_messages_last_24h(session: Session, campaign_id: str) -> int:
    cutoff = datetime.now(tz=timezone.utc) - timedelta(hours=24)
    stmt = (
        select(func.count(Message.id))
        .join(Thread, Thread.id == Message.thread_id)
        .join(CampaignLead, CampaignLead.id == Thread.campaign_lead_id)
        .where(
            CampaignLead.campaign_id == campaign_id,
            Message.role == MessageRole.AI,
            Message.created_at >= cutoff,
        )
    )
    return int(session.execute(stmt).scalar_one() or 0)


def _next_queued_leads(
    session: Session, campaign_id: str, limit: int
) -> list[tuple[CampaignLead, Lead]]:
    """Return the next N queued campaign-lead assignments, joined with the lead."""

    if limit <= 0:
        return []

    stmt = (
        select(CampaignLead, Lead)
        .join(Lead, Lead.id == CampaignLead.lead_id)
        .where(
            and_(
                CampaignLead.campaign_id == campaign_id,
                CampaignLead.status == CampaignLeadStatus.QUEUED,
                Lead.status.in_(["new", "contacted"]),
            )
        )
        .order_by(CampaignLead.queue_position.asc())
        .limit(limit)
    )
    return list(session.execute(stmt).all())


async def _run_campaign_tick(
    *,
    connector: BaseConnector,
    settings: Settings,
) -> dict[str, int]:
    """One pass across all active campaigns; returns a send summary."""

    summary = {"campaigns": 0, "sent": 0, "failed": 0, "idle": 0}

    with session_scope() as session:
        workspace = session.query(Workspace).first()
        if workspace is None:
            logger.warning("no workspace — run `autosdr init` first")
            summary["idle"] += 1
            return summary

        settings_blob = workspace.settings or {}
        min_delay_s = _effective_setting(
            settings_blob,
            "min_inter_send_delay_s",
            settings.min_inter_send_delay_s,
            30,
        )
        max_batch = _effective_setting(
            settings_blob,
            "max_batch_per_tick",
            settings.max_batch_per_tick,
            2,
        )

        campaigns = (
            session.query(Campaign)
            .filter(Campaign.status == CampaignStatus.ACTIVE)
            .all()
        )
        summary["campaigns"] = len(campaigns)

        sends_this_tick = 0

        for campaign in campaigns:
            if killswitch.is_paused():
                break

            sent_last_24h = _count_ai_messages_last_24h(session, campaign.id)
            remaining = max(0, campaign.outreach_per_day - sent_last_24h)
            batch_limit = min(remaining, max_batch)

            if batch_limit == 0:
                logger.debug(
                    "campaign %s at daily cap (%d sent in last 24h)",
                    campaign.name,
                    sent_last_24h,
                )
                continue

            candidates = _next_queued_leads(session, campaign.id, batch_limit)
            if not candidates:
                continue

            for campaign_lead, lead in candidates:
                if killswitch.is_paused():
                    break

                if sends_this_tick > 0:
                    fired = await killswitch.await_shutdown_or_timeout(min_delay_s)
                    if fired:
                        break

                try:
                    result = await run_outreach_for_campaign_lead(
                        session=session,
                        connector=connector,
                        workspace=workspace,
                        campaign=campaign,
                        campaign_lead=campaign_lead,
                        lead=lead,
                    )
                except killswitch.KillSwitchTripped:
                    logger.info("kill switch tripped mid-outreach; stopping tick")
                    break
                except Exception:
                    logger.exception(
                        "outreach pipeline crashed for campaign_lead=%s", campaign_lead.id
                    )
                    summary["failed"] += 1
                    continue

                if result.sent:
                    summary["sent"] += 1
                    sends_this_tick += 1
                else:
                    summary["failed"] += 1
                    logger.warning(
                        "outreach skipped: campaign_lead=%s reason=%s",
                        campaign_lead.id,
                        result.reason,
                    )

        if summary["sent"] == 0 and summary["failed"] == 0:
            summary["idle"] += 1

    return summary


async def _poll_inbound_once(connector: BaseConnector) -> int:
    """Fetch pending inbound messages from the connector and dispatch them.

    Returns the number of messages dispatched. Connectors that are push-only
    (or the file connector in dev) return an empty list, so this is a cheap
    no-op for them.
    """

    try:
        incoming_list = await connector.poll_incoming()
    except killswitch.KillSwitchTripped:
        return 0
    except Exception:
        logger.exception("inbound poll crashed for connector=%s", connector.connector_type)
        return 0

    if not incoming_list:
        return 0

    with session_scope() as session:
        workspace = session.query(Workspace).first()
        workspace_id = workspace.id if workspace else None

    if workspace_id is None:
        logger.warning("received %d inbound messages but no workspace — dropping", len(incoming_list))
        return 0

    dispatched = 0
    for incoming in incoming_list:
        if killswitch.is_paused():
            logger.info("kill switch tripped mid-poll; stopping")
            break
        try:
            await process_incoming_message(
                connector=connector,
                workspace_id=workspace_id,
                incoming=incoming,
            )
            dispatched += 1
        except Exception:
            logger.exception(
                "reply pipeline crashed for inbound from=%s", incoming.contact_uri
            )
    return dispatched


async def run_inbound_poller(
    connector: BaseConnector,
    settings: Settings | None = None,
) -> None:
    """Background task: poll the connector for inbound messages every N seconds."""

    settings = settings or get_settings()
    with session_scope() as session:
        workspace = session.query(Workspace).first()
        settings_blob = (workspace.settings if workspace else {}) or {}

    poll_s = _effective_setting(
        settings_blob, "inbound_poll_s", settings.inbound_poll_s, 20
    )
    logger.info(
        "inbound poller started; poll=%ds connector=%s", poll_s, connector.connector_type
    )

    while True:
        if killswitch.is_shutting_down():
            logger.info("inbound poller exiting on shutdown")
            return

        if not killswitch.is_flag_set():
            dispatched = await _poll_inbound_once(connector)
            if dispatched:
                logger.info(
                    "inbound poll dispatched %d message(s) via %s",
                    dispatched,
                    connector.connector_type,
                )

        fired = await killswitch.await_shutdown_or_timeout(poll_s)
        if fired and killswitch.is_shutting_down():
            logger.info("inbound poller exiting on shutdown")
            return


async def run_scheduler(
    connector: BaseConnector,
    settings: Settings | None = None,
) -> None:
    """Main scheduler loop. Exits when the kill-switch shutdown event fires."""

    settings = settings or get_settings()

    with session_scope() as session:
        workspace = session.query(Workspace).first()
        settings_blob = (workspace.settings if workspace else {}) or {}

    tick_s = _effective_setting(
        settings_blob, "scheduler_tick_s", settings.scheduler_tick_s, 60
    )
    logger.info("scheduler started; tick=%ds connector=%s", tick_s, connector.connector_type)

    while True:
        if killswitch.is_shutting_down():
            logger.info("scheduler exiting on shutdown")
            return

        if killswitch.is_flag_set():
            logger.debug("pause flag set — skipping tick")
        else:
            try:
                summary = await _run_campaign_tick(
                    connector=connector, settings=settings
                )
                if summary["sent"] or summary["failed"]:
                    logger.info("scheduler tick summary: %s", summary)
            except Exception:
                logger.exception("scheduler tick crashed; continuing")

        fired = await killswitch.await_shutdown_or_timeout(tick_s)
        if fired and killswitch.is_shutting_down():
            logger.info("scheduler exiting on shutdown")
            return
