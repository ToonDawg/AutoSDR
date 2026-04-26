"""Follow-up beat: a second, casual SMS fired shortly after the first contact.

The idea is human texture. A single long pitch message reads as a polished
pitch. Two short messages with a ~10s gap ("hey, saw X, here's the fix" …
then "or more generally, if you need anything, I can help — Cheers") reads
as a real person who remembered one more thing after hitting send.

This module owns three concerns:

1. **Scheduling.** :func:`schedule_followup_send` fires a background
   ``asyncio.Task`` that sleeps ``delay_s ± jitter_s`` seconds and then
   drives the send. The sleep is cancellable via the shared kill-switch
   event so SIGINT / ``autosdr stop`` doesn't leave orphan sends pending.
2. **Guarding.** Before sending we re-open a DB session and re-check the
   world: the thread must still be ACTIVE, the last message on the
   thread must still be the parent message we just sent, and the lead
   must still have a contact_uri. Any of those being false means the
   context that made the follow-up a good idea is gone (lead replied in
   the interim, operator closed the thread, etc.) and we skip.
3. **Persistence.** A successful send is recorded as a ``Message`` with
   ``role=AI`` and ``metadata.source="followup"`` plus a pointer back to
   the parent message id, so the transcript shows both beats together.

The template is a literal string. We deliberately do NOT run it through
the LLM: the whole point is cheap, predictable, "I build websites, shoot
me a text" style texture that the operator configures once per campaign
and then forgets. If you want recipient-aware variants later, add them
here — the scheduling / guarding / persistence halves don't need to
change.

Config lives on ``campaign.followup`` as a JSON dict with the shape::

    {
        "enabled":        bool,   # feature on/off
        "template":       str,    # literal text, may include {name} etc.
        "delay_s":        int,    # target mid-point of the delay window
        "delay_jitter_s": int,    # +/- this many seconds around delay_s
    }

``None`` on the campaign row is treated identically to ``enabled=False``.
"""

from __future__ import annotations

import asyncio
import logging
import random
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select

from autosdr import killswitch
from autosdr.connectors import get_connector
from autosdr.connectors.base import BaseConnector, OutgoingMessage
from autosdr.db import session_scope
from autosdr.models import CampaignLead, Lead, Message, MessageRole, Thread, ThreadStatus

logger = logging.getLogger(__name__)


# Delay ceiling — clamps operator typos. If someone puts 99999 in the
# delay field we don't want a task hanging around for days.
_MAX_DELAY_S = 600.0
DEFAULT_FOLLOWUP_TEMPLATE = (
    "or more generally, if you have any issues with your website or need any help, "
    "I can solve that for you. Cheers, Jaclyn"
)


def _normalise_followup_config(raw: dict[str, Any] | None) -> dict[str, Any] | None:
    """Validate a campaign's ``followup`` blob.

    Returns ``None`` when the feature is off or unusable. A blank template
    on an enabled config means "use the default copy".
    """

    if not raw:
        return None
    if not raw.get("enabled"):
        return None
    template = str(raw.get("template") or "").strip() or DEFAULT_FOLLOWUP_TEMPLATE
    try:
        delay_s = max(0, int(raw.get("delay_s", 10)))
    except (TypeError, ValueError):
        delay_s = 10
    try:
        jitter_s = max(0, int(raw.get("delay_jitter_s", 5)))
    except (TypeError, ValueError):
        jitter_s = 5
    return {
        "template": template,
        "delay_s": delay_s,
        "delay_jitter_s": jitter_s,
    }


class _SafeDict(dict):
    """``dict`` subclass that keeps unknown ``{tokens}`` literal in ``format_map``."""

    def __missing__(self, key: str) -> str:  # pragma: no cover - trivial
        return "{" + key + "}"


def _render_template(
    template: str,
    *,
    lead_name: str | None,
    lead_short_name: str | None,
    owner_first_name: str | None,
) -> str:
    """Render the follow-up template.

    Supports a tiny set of optional placeholders — ``{name}``,
    ``{short_name}``, ``{owner_first_name}`` — each of which falls back
    to a polite empty/default when the data isn't available. Unknown
    placeholders render as the literal token so the operator sees their
    typo in-message rather than crashing the send.
    """

    short = (lead_short_name or lead_name or "").strip()
    values = {
        "name": short or "there",
        "short_name": short,
        "owner_first_name": (owner_first_name or "").strip(),
    }

    try:
        return template.format_map(_SafeDict(values))
    except Exception:
        logger.exception("followup template render failed — sending raw template")
        return template


async def _wait_with_kill_switch(delay_s: float) -> bool:
    """Sleep up to ``delay_s`` seconds; return True if shutdown fired first."""

    if delay_s <= 0:
        return killswitch.is_shutting_down()
    return await killswitch.await_shutdown_or_timeout(delay_s)


def _is_followup_still_appropriate(
    *, thread: Thread, parent_message_id: str
) -> tuple[bool, str]:
    """Re-check the thread state after the delay.

    We want to send only when the thread is still a clean first-contact
    context. If anything changed — lead replied, human closed the thread,
    connector parked for HITL — the original reason for the follow-up
    ("I just said the first thing, here's the afterthought") is gone.
    """

    if thread.status != ThreadStatus.ACTIVE:
        return False, f"thread_status:{thread.status}"
    return True, ""


async def _run_followup(
    *,
    thread_id: str,
    parent_message_id: str,
    contact_uri: str,
    template: str,
    delay_s: float,
    lead_name: str | None,
    lead_short_name: str | None,
    owner_first_name: str | None,
    connector: BaseConnector | None,
) -> None:
    """Background task body. See :func:`schedule_followup_send`."""

    shutdown_fired = await _wait_with_kill_switch(delay_s)
    if shutdown_fired:
        logger.info(
            "followup skipped thread=%s reason=shutdown_during_delay", thread_id
        )
        return
    if killswitch.is_paused():
        logger.info(
            "followup skipped thread=%s reason=paused_during_delay", thread_id
        )
        return

    # Re-open a fresh session — the outreach session that scheduled us
    # has long since committed and closed. Do the state check, render,
    # send, and persist as close together as possible.
    try:
        with session_scope() as session:
            thread = session.get(Thread, thread_id)
            if thread is None:
                logger.warning("followup skipped — thread=%s vanished", thread_id)
                return

            last_message = session.execute(
                select(Message)
                .where(Message.thread_id == thread_id)
                .order_by(Message.created_at.desc())
                .limit(1)
            ).scalar_one_or_none()
            if last_message is None or last_message.id != parent_message_id:
                logger.info(
                    "followup skipped thread=%s reason=new_message_landed last=%s parent=%s",
                    thread_id,
                    last_message.id if last_message else None,
                    parent_message_id,
                )
                return

            ok, reason = _is_followup_still_appropriate(
                thread=thread, parent_message_id=parent_message_id
            )
            if not ok:
                logger.info("followup skipped thread=%s reason=%s", thread_id, reason)
                return

            campaign_lead = session.get(CampaignLead, thread.campaign_lead_id)
            lead = session.get(Lead, campaign_lead.lead_id) if campaign_lead else None
            current_contact_uri = (lead.contact_uri or "").strip() if lead else ""
            scheduled_contact_uri = (contact_uri or "").strip()
            if not current_contact_uri:
                logger.info(
                    "followup skipped thread=%s reason=lead_missing_contact_uri",
                    thread_id,
                )
                return
            if current_contact_uri != scheduled_contact_uri:
                logger.info(
                    "followup skipped thread=%s reason=lead_contact_uri_changed scheduled=%s current=%s",
                    thread_id,
                    scheduled_contact_uri,
                    current_contact_uri,
                )
                return

            rendered = _render_template(
                template,
                lead_name=lead_name,
                lead_short_name=lead_short_name,
                owner_first_name=owner_first_name,
            )
            if not rendered.strip():
                logger.info(
                    "followup skipped thread=%s reason=rendered_empty", thread_id
                )
                return

            # Prefer the connector the caller already had; fall back to
            # the cached singleton. ``get_connector()`` raises if the
            # workspace hasn't been set up, which can't actually happen
            # here (we got scheduled because a send just succeeded) but
            # we'd rather log than crash the task.
            active_connector = connector
            if active_connector is None:
                try:
                    active_connector = get_connector()
                except Exception:
                    logger.exception(
                        "followup skipped thread=%s reason=connector_unavailable",
                        thread_id,
                    )
                    return

            logger.info(
                "followup sending thread=%s chars=%d delay=%.2fs connector=%s",
                thread_id,
                len(rendered),
                delay_s,
                active_connector.connector_type,
            )
            send_result = await active_connector.send(
                OutgoingMessage(contact_uri=current_contact_uri, content=rendered)
            )
            if not send_result.success:
                logger.error(
                    "followup send failed thread=%s error=%s",
                    thread_id,
                    send_result.error,
                )
                return

            session.add(
                Message(
                    thread_id=thread_id,
                    role=MessageRole.AI,
                    content=rendered,
                    metadata_={
                        "source": "followup",
                        "parent_message_id": parent_message_id,
                        "scheduled_delay_s": delay_s,
                        "sent_at": datetime.now(tz=timezone.utc).isoformat(),
                        "provider_message_id": send_result.provider_message_id,
                    },
                )
            )
            session.flush()
            logger.info(
                "followup sent thread=%s parent=%s chars=%d provider_id=%s",
                thread_id,
                parent_message_id,
                len(rendered),
                send_result.provider_message_id,
            )
    except Exception:
        # Never let a follow-up failure take down the scheduler or the
        # FastAPI task group — log loudly and move on.
        logger.exception("followup task crashed thread=%s", thread_id)


def schedule_followup_send(
    *,
    campaign_followup: dict[str, Any] | None,
    thread_id: str,
    parent_message_id: str,
    contact_uri: str,
    lead_name: str | None = None,
    lead_short_name: str | None = None,
    owner_first_name: str | None = None,
    connector: BaseConnector | None = None,
) -> asyncio.Task | None:
    """Queue a follow-up send ``delay_s ± jitter_s`` seconds from now.

    ``campaign_followup`` is the raw ``campaign.followup`` JSON blob
    (shape documented at module top). Pass ``None`` to no-op explicitly;
    the function also no-ops when the blob has ``enabled=False``. An
    enabled config with a blank template uses ``DEFAULT_FOLLOWUP_TEMPLATE``.

    Returns the created ``asyncio.Task``, or ``None`` if the feature is
    disabled / there's no running event loop.
    Caller keeps no reference — the task drives itself; a strong
    reference is held for the lifetime of the task via the running
    event loop.

    Scheduling happens on the running loop. This function MUST be called
    from inside an async context (the outreach pipeline runs as an async
    task, and so does the API endpoint).
    """

    cfg = _normalise_followup_config(campaign_followup)
    if cfg is None:
        return None
    if not contact_uri:
        logger.warning(
            "followup not scheduled thread=%s reason=missing_contact_uri",
            thread_id,
        )
        return None

    base_delay = cfg["delay_s"]
    jitter = cfg["delay_jitter_s"]
    # +/-jitter spread around the base delay. We clamp at 0 so a 10s delay
    # with 20s jitter doesn't go negative; we also clamp at a sane ceiling
    # to avoid operator typos turning the feature into a time bomb.
    if jitter > 0:
        offset = random.uniform(-jitter, jitter)
    else:
        offset = 0.0
    delay_s = max(0.0, min(base_delay + offset, _MAX_DELAY_S))

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        logger.warning(
            "followup not scheduled thread=%s reason=no_running_loop", thread_id
        )
        return None

    task = loop.create_task(
        _run_followup(
            thread_id=thread_id,
            parent_message_id=parent_message_id,
            contact_uri=contact_uri,
            template=cfg["template"],
            delay_s=delay_s,
            lead_name=lead_name,
            lead_short_name=lead_short_name,
            owner_first_name=owner_first_name,
            connector=connector,
        ),
        name=f"autosdr.followup.{thread_id}",
    )
    logger.info(
        "followup scheduled thread=%s parent=%s delay=%.2fs",
        thread_id,
        parent_message_id,
        delay_s,
    )
    return task


__all__ = ["DEFAULT_FOLLOWUP_TEMPLATE", "schedule_followup_send"]
