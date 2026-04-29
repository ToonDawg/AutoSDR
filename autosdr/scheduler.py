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
from datetime import datetime

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
    Message,
    MessageRole,
    Thread,
    Workspace,
)
from autosdr.pacing import (
    count_sends_in_today_window,
    resolve_window,
    window_allowance,
)
from autosdr.pipeline import process_incoming_message, run_outreach_for_campaign_lead
from autosdr.quota import (
    count_outreach_contacts_last_24h,
    count_outreach_contacts_per_category_24h,
)
from autosdr.workspace_settings import load_workspace_settings_or_empty

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class OutreachBatchSummary:
    requested: int
    attempted: int = 0
    sent: int = 0
    failed: int = 0
    capped_by_quota: bool = False
    # Distinct from ``capped_by_quota`` so the operator-facing "why
    # didn't this campaign send?" surfaces ("daily cap hit" vs "outside
    # business hours / paced for later in the day") can stay
    # honest. Both can be true on the same tick.
    capped_by_window: bool = False


# Sentinel used by the picker to mean "no previous category at all"
# (cold-start campaign). Crucially distinct from ``None``, which is a
# real category value (uncategorised leads); using ``None`` as the
# sentinel would make the very first cold-start pick wrongly
# deprioritise the uncategorised bucket against itself.
_NO_LAST_CATEGORY: object = object()


def _most_recent_contact_category(
    session: Session, campaign_id: str
) -> str | None | object:
    """Category of the lead whose most recent AI message landed in this campaign.

    Used by the picker to avoid back-to-back same-category sends across
    tick boundaries — without it, the very first pick of each tick has no
    "last category" memory and would happily start a 50-plumber streak.

    Returns ``_NO_LAST_CATEGORY`` if no AI messages exist yet (cold-start
    campaign) so the picker can express "no preference" without colliding
    with a real ``None`` (uncategorised) category. If the most recent
    contact's lead is itself uncategorised, returns ``None`` — the
    picker should still avoid stacking another uncategorised send next.
    """

    stmt = (
        select(Lead.category)
        .join(CampaignLead, CampaignLead.lead_id == Lead.id)
        .join(Thread, Thread.campaign_lead_id == CampaignLead.id)
        .join(Message, Message.thread_id == Thread.id)
        .where(
            CampaignLead.campaign_id == campaign_id,
            Message.role == MessageRole.AI,
        )
        .order_by(Message.created_at.desc())
        .limit(1)
    )
    row = session.execute(stmt).first()
    return row[0] if row is not None else _NO_LAST_CATEGORY


def _categories_ever_contacted(
    session: Session, campaign_id: str
) -> set[str | None]:
    """Distinct ``Lead.category`` values that have ever received an AI message.

    Powers the "untouched categories first" tier of the picker score: a
    category that has *never* been messaged in this campaign sorts ahead
    of any category that has, regardless of how recently it was hit.
    Includes ``None`` if any uncategorised lead has been contacted.
    """

    stmt = (
        select(Lead.category)
        .join(CampaignLead, CampaignLead.lead_id == Lead.id)
        .join(Thread, Thread.campaign_lead_id == CampaignLead.id)
        .join(Message, Message.thread_id == Thread.id)
        .where(
            CampaignLead.campaign_id == campaign_id,
            Message.role == MessageRole.AI,
        )
        .distinct()
    )
    return {row[0] for row in session.execute(stmt).all()}


def _next_queued_leads(
    session: Session, campaign_id: str, limit: int
) -> list[tuple[CampaignLead, Lead]]:
    """Pick the next N queued campaign-lead assignments with category rotation.

    The naive picker — ``ORDER BY queue_position ASC LIMIT N`` — burns a
    plumber-heavy import on plumbers for days. Instead, we fetch the
    queued candidates once and run a 4-key Python scoring loop per pick:

    1. Avoid back-to-back same category (compared against the previous
       pick in this batch, falling back to the most recent AI contact in
       the campaign so the rule survives tick boundaries).
    2. Prefer categories that have never been contacted in this
       campaign — first the untouched buckets, then the rest.
    3. Within "already contacted", prefer the *least* recently contacted
       (24h count + intra-batch picks).
    4. Tie-break on ``queue_position`` so import order still wins inside
       a category.

    The intent is light interleaving, not a strict cap: degenerates to
    pure FIFO when only one category is queued, and won't ever skip a
    lead that the old picker would have sent.

    All quota / outreach-window enforcement happens upstream in
    :func:`run_campaign_outreach_batch`; we only reorder within the
    ``limit`` it has already decided on.
    """

    if limit <= 0:
        return []

    candidate_stmt = (
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
    )
    all_candidates: list[tuple[CampaignLead, Lead]] = list(
        session.execute(candidate_stmt).all()
    )
    if not all_candidates:
        return []

    sent_24h = count_outreach_contacts_per_category_24h(session, campaign_id)
    ever_contacted = _categories_ever_contacted(session, campaign_id)
    last_sent_cat = _most_recent_contact_category(session, campaign_id)

    # Group candidates by category, keeping each bucket FIFO. Popping
    # from the head of each bucket is O(1) and makes the per-pick scan
    # O(num_categories) rather than O(num_queued).
    buckets: dict[str | None, list[tuple[CampaignLead, Lead]]] = {}
    for cl, lead in all_candidates:
        buckets.setdefault(lead.category, []).append((cl, lead))

    intra_batch: dict[str | None, int] = {}
    picked: list[tuple[CampaignLead, Lead]] = []

    while len(picked) < limit:
        non_empty_cats = [cat for cat, rows in buckets.items() if rows]
        if not non_empty_cats:
            break

        def score(cat: str | None) -> tuple[int, int, int, int]:
            head = buckets[cat][0]
            return (
                1 if cat == last_sent_cat else 0,
                1 if cat in ever_contacted else 0,
                sent_24h.get(cat, 0) + intra_batch.get(cat, 0),
                head[0].queue_position,
            )

        chosen_cat = min(non_empty_cats, key=score)
        cl, lead = buckets[chosen_cat].pop(0)
        picked.append((cl, lead))
        intra_batch[chosen_cat] = intra_batch.get(chosen_cat, 0) + 1
        last_sent_cat = chosen_cat

    return picked


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
    now_local: datetime | None = None,
) -> OutreachBatchSummary:
    """Send up to ``max_count`` queued leads for one campaign.

    The scheduler uses ``respect_quota=True``; manual operator kick-offs use
    ``False`` so they can intentionally spend beyond the rolling cap.
    ``respect_quota`` also gates the working-hours window — kickoff bypasses
    both because the operator pressed the button on purpose.

    ``now_local`` is injectable so tests can drive the window logic at a
    deterministic clock; production callers leave it ``None`` and we read
    the system clock with the OS timezone.
    """

    requested = max(0, int(max_count))
    summary = OutreachBatchSummary(requested=requested)
    if requested <= 0:
        return summary

    batch_limit = requested
    if respect_quota:
        sent_last_24h = count_outreach_contacts_last_24h(session, campaign.id)
        remaining_quota = max(0, campaign.outreach_per_day - sent_last_24h)
        if remaining_quota < batch_limit:
            summary.capped_by_quota = True
            batch_limit = remaining_quota
        if batch_limit == 0:
            logger.debug(
                "campaign %s at daily cap (%d sent in last 24h)",
                campaign.name,
                sent_last_24h,
            )
            return summary

        # Working-hours window pacing — stacks under the 24h cap. ``None``
        # local clock means "use the OS clock with its current tz"; tests
        # inject a fixed datetime to keep the gate deterministic.
        clock = now_local or datetime.now().astimezone()
        window = resolve_window(
            campaign_window=campaign.outreach_window,
            workspace_settings=workspace.settings,
        )
        sent_in_window = count_sends_in_today_window(
            session, campaign.id, window=window, now_local=clock
        )
        pacing_allowance = window_allowance(
            window=window,
            daily_quota=campaign.outreach_per_day,
            sent_in_window=sent_in_window,
            now_local=clock,
        )
        if pacing_allowance < batch_limit:
            summary.capped_by_window = True
            batch_limit = pacing_allowance
        if batch_limit == 0:
            logger.debug(
                "campaign %s outside or saturated by outreach window "
                "(window=%s sent_in_window=%d quota=%d now=%s)",
                campaign.name,
                window,
                sent_in_window,
                campaign.outreach_per_day,
                clock,
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


async def _run_campaign_tick(
    connector: BaseConnector,
) -> dict[str, int]:
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

    if await killswitch.await_shutdown_or_timeout(poll_s):
        logger.info("inbound poller exiting on shutdown")
        return

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
    """Main scheduler loop. Exits when the kill-switch shutdown event fires.

    Lead-website enrichment is owned by crawlee inside
    :mod:`autosdr.enrichment`; no per-loop client to thread through.
    """

    settings_blob = load_workspace_settings_or_empty()
    tick_s = int(settings_blob.get("scheduler_tick_s", 60))
    logger.info("scheduler started; tick=%ds connector=%s", tick_s, connector.connector_type)

    if await killswitch.await_shutdown_or_timeout(tick_s):
        logger.info("scheduler exiting on shutdown")
        return

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
