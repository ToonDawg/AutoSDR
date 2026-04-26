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

Every knob here is read from ``workspace.settings`` — the only config source
of truth — so toggling a value in the UI takes effect on the next tick.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from sqlalchemy import and_, func, select
from sqlalchemy.orm import Session

from autosdr import killswitch
from autosdr.connectors.base import BaseConnector
from autosdr.db import session_scope
from autosdr.models import (
    Campaign,
    CampaignLead,
    CampaignLeadStatus,
    CampaignStatus,
    Lead,
    Workspace,
)
from autosdr.pipeline import process_incoming_message, run_outreach_for_campaign_lead
from autosdr.quota import count_ai_messages_last_24h
from autosdr.workspace_settings import load_workspace_settings_or_empty

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class OutreachBatchSummary:
    requested: int
    attempted: int = 0
    sent: int = 0
    failed: int = 0
    capped_by_quota: bool = False


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


def _count_queued_leads(session: Session, campaign_id: str) -> int:
    """Return queued assignments that are still eligible to receive outreach."""

    return int(
        session.execute(
            select(func.count(CampaignLead.id))
            .join(Lead, Lead.id == CampaignLead.lead_id)
            .where(
                and_(
                    CampaignLead.campaign_id == campaign_id,
                    CampaignLead.status == CampaignLeadStatus.QUEUED,
                    Lead.status.in_(["new", "contacted"]),
                )
            )
        ).scalar_one()
    )


async def run_campaign_outreach_batch(
    *,
    session: Session,
    connector: BaseConnector,
    workspace: Workspace,
    campaign: Campaign,
    max_count: int,
    respect_quota: bool,
    min_delay_s: int = 0,
    prior_success_count: int = 0,
) -> OutreachBatchSummary:
    """Send up to ``max_count`` queued leads for one campaign.

    The scheduler uses ``respect_quota=True``; manual operator kick-offs use
    ``False`` so they can intentionally spend beyond the rolling cap.
    """

    requested = max(0, int(max_count))
    summary = OutreachBatchSummary(requested=requested)
    if requested <= 0:
        return summary

    batch_limit = requested
    if respect_quota:
        sent_last_24h = count_ai_messages_last_24h(session, campaign.id)
        remaining = max(0, campaign.outreach_per_day - sent_last_24h)
        batch_limit = min(batch_limit, remaining)
        summary.capped_by_quota = batch_limit < requested
        if batch_limit == 0:
            logger.debug(
                "campaign %s at daily cap (%d sent in last 24h)",
                campaign.name,
                sent_last_24h,
            )
            return summary

    candidates = _next_queued_leads(session, campaign.id, batch_limit)
    for campaign_lead, lead in candidates:
        if killswitch.is_paused():
            raise killswitch.KillSwitchTripped()

        if min_delay_s > 0 and prior_success_count + summary.sent > 0:
            fired = await killswitch.await_shutdown_or_timeout(min_delay_s)
            if fired:
                raise killswitch.KillSwitchTripped()

        summary.attempted += 1
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
            raise
        except Exception:
            logger.exception(
                "outreach pipeline crashed for campaign_lead=%s", campaign_lead.id
            )
            summary.failed += 1
            continue

        if result.sent:
            summary.sent += 1
        else:
            summary.failed += 1
            logger.warning(
                "outreach skipped: campaign_lead=%s reason=%s",
                campaign_lead.id,
                result.reason,
            )

    return summary


async def _run_campaign_tick(connector: BaseConnector) -> dict[str, int]:
    """One pass across all active campaigns; returns a send summary."""

    summary = {"campaigns": 0, "sent": 0, "failed": 0, "idle": 0}

    with session_scope() as session:
        workspace = session.query(Workspace).first()
        if workspace is None:
            logger.debug("no workspace yet — waiting for setup wizard")
            summary["idle"] += 1
            return summary

        settings_blob = workspace.settings or {}
        min_delay_s = int(settings_blob.get("min_inter_send_delay_s", 30))
        max_batch = int(settings_blob.get("max_batch_per_tick", 2))

        campaigns = (
            session.query(Campaign)
            .filter(Campaign.status == CampaignStatus.ACTIVE)
            .all()
        )
        summary["campaigns"] = len(campaigns)

        for campaign in campaigns:
            if killswitch.is_paused():
                break

            try:
                batch = await run_campaign_outreach_batch(
                    session=session,
                    connector=connector,
                    workspace=workspace,
                    campaign=campaign,
                    max_count=max_batch,
                    respect_quota=True,
                    min_delay_s=min_delay_s,
                    prior_success_count=summary["sent"],
                )
            except killswitch.KillSwitchTripped:
                logger.info("kill switch tripped mid-outreach; stopping tick")
                break

            summary["sent"] += batch.sent
            summary["failed"] += batch.failed

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


async def run_inbound_poller(connector: BaseConnector) -> None:
    """Background task: poll the connector for inbound messages every N seconds."""

    settings_blob = load_workspace_settings_or_empty()
    poll_s = int(settings_blob.get("inbound_poll_s", 20))
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


async def run_scheduler(connector: BaseConnector) -> None:
    """Main scheduler loop. Exits when the kill-switch shutdown event fires."""

    settings_blob = load_workspace_settings_or_empty()
    tick_s = int(settings_blob.get("scheduler_tick_s", 60))
    logger.info("scheduler started; tick=%ds connector=%s", tick_s, connector.connector_type)

    while True:
        if killswitch.is_shutting_down():
            logger.info("scheduler exiting on shutdown")
            return

        if killswitch.is_flag_set():
            logger.debug("pause flag set — skipping tick")
        else:
            try:
                summary = await _run_campaign_tick(connector=connector)
                if summary["sent"] or summary["failed"]:
                    logger.info("scheduler tick summary: %s", summary)
            except Exception:
                logger.exception("scheduler tick crashed; continuing")

        fired = await killswitch.await_shutdown_or_timeout(tick_s)
        if fired and killswitch.is_shutting_down():
            logger.info("scheduler exiting on shutdown")
            return
