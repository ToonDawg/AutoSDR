"""Reply pipeline: inbound webhook -> classify -> route.

Routing strategy (first-message-only mode, the default — ``auto_reply_enabled=False``):

- *every* inbound → pause for human, stash N suggested replies.

The classifier still runs and its verdict is captured in the HITL
context (so the operator sees "LLM thinks this is negative /
goal_achieved / unclear / …" alongside the suggestions), but it never
auto-closes the thread. Close-won and close-lost are decisions the
operator drives from the inbox — the system would otherwise silently
swallow a reply the human has not yet seen.

If ``workspace.settings.auto_reply_enabled`` is flipped to ``true`` the
old classify → generate → evaluate → send loop is preserved for
backwards compatibility, including a terminal-intent shortcut that
auto-closes on ``negative`` / ``goal_achieved`` so the bot doesn't
waste an LLM round-trip drafting a reply to "no thanks".

Multi-campaign resolution:

- Resolve lead by (workspace_id, contact_uri) after E.164 normalisation.
- If the lead has multiple active threads, route to the thread whose
  most-recent outbound message is the most recent. Ties broken by thread.id.
- If no active thread exists, write to ``unmatched_webhook``.

Concurrency:

- The reply processor acquires a per-thread lock before classifying. SQLite
  gets this via ``BEGIN IMMEDIATE`` (serialises writers); Postgres via
  ``SELECT ... FOR UPDATE``. The second processor re-reads history after
  acquiring the lock so it sees the first inbound before classifying.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from autosdr import killswitch
from autosdr.compliance import OptOutMatch, match_opt_out
from autosdr.connectors.base import BaseConnector, IncomingMessage, OutgoingMessage
from autosdr.db import session_scope
from autosdr.importer import normalise_phone
from autosdr.llm import LlmCallContext, complete_json
from autosdr.models import (
    Campaign,
    CampaignLead,
    CampaignLeadStatus,
    Lead,
    LeadStatus,
    LlmCall,
    LlmCallPurpose,
    Message,
    MessageRole,
    Thread,
    ThreadStatus,
    UnmatchedWebhook,
    Workspace,
)
from autosdr.pipeline._shared import (
    build_send_metadata,
    generate_and_evaluate,
    hitl_context_from_loop_failure,
    hitl_context_from_send_failure,
    pause_thread_for_hitl,
    read_loop_settings,
    schedule_hitl_push,
    thread_history,
)
from autosdr.pipeline.suggestions import generate_reply_variants
from autosdr.prompts import classification

logger = logging.getLogger(__name__)


# HITL reason when we pause a thread because auto-reply is off and we're
# waiting for the operator to pick one of the suggested drafts.
HITL_AWAITING_HUMAN_REPLY = "awaiting_human_reply"

# Sentinel ``LlmCall.model`` value for synthetic audit rows written by the
# deterministic opt-out shortcut. Filtering on this string excludes synthetic
# rows from any future cost / token aggregate. See
# ``docs/tickets/0001-stop-opt-out-keywords.md`` § Resolved questions for the
# rationale (we repurpose ``LlmCall`` instead of adding a new ``routing_event``
# table; the trade-off is documented).
OPT_OUT_AUDIT_MODEL = "(deterministic-opt-out)"


@dataclass
class ReplyResult:
    # closed_opt_out fires for the deterministic STOP / opt-out shortcut.
    action: str  # sent | escalated_hitl | closed_won | closed_lost | closed_opt_out | unmatched | ignored
    thread_id: str | None = None
    intent: str | None = None
    confidence: float | None = None
    detail: str | None = None


def _resolve_contact(contact_uri: str, region_hint: str) -> str | None:
    normalised, _type = normalise_phone(contact_uri, region_hint=region_hint)
    return normalised


def _resolve_thread(session: Session, workspace_id: str, contact_uri: str) -> Thread | None:
    """Find the target thread per the multi-campaign routing rule."""

    lead = session.execute(
        select(Lead).where(
            Lead.workspace_id == workspace_id, Lead.contact_uri == contact_uri
        )
    ).scalar_one_or_none()
    if lead is None:
        return None

    active_threads = (
        session.query(Thread)
        .join(CampaignLead, Thread.campaign_lead_id == CampaignLead.id)
        .filter(
            CampaignLead.lead_id == lead.id,
            Thread.status.notin_(list(ThreadStatus.CLOSED)),
        )
        .all()
    )
    if not active_threads:
        return None
    if len(active_threads) == 1:
        return active_threads[0]

    def _latest_outbound_ts(t: Thread):
        last = (
            session.query(Message.created_at)
            .filter(Message.thread_id == t.id, Message.role == MessageRole.AI)
            .order_by(Message.created_at.desc())
            .first()
        )
        return (last[0] if last else t.created_at, t.id)

    active_threads.sort(key=_latest_outbound_ts, reverse=True)
    return active_threads[0]


def _lock_thread(session: Session, thread_id: str) -> Thread | None:
    """Acquire a row-level lock on a thread for reply processing."""

    dialect = session.bind.dialect.name if session.bind else "sqlite"
    if dialect in {"postgresql", "postgres"}:
        stmt = select(Thread).where(Thread.id == thread_id).with_for_update()
        return session.execute(stmt).scalar_one_or_none()
    return session.get(Thread, thread_id)


@dataclass
class _Classification:
    """Normalised classifier output, threaded through the reply pipeline."""

    intent: str
    confidence: float
    reason: str | None
    requires_human: bool
    llm_call_id: str | None


async def process_incoming_message(
    *,
    connector: BaseConnector,
    workspace_id: str,
    incoming: IncomingMessage,
) -> ReplyResult:
    """Main entry point — called from the webhook background task or poller.

    Three-phase architecture:

    1. **Persist inbound + early exits** (with DB transaction). Resolves
       the thread, records the inbound :class:`Message`, captures the
       message history snapshot, and runs the deterministic opt-out
       shortcut. The transaction commits before returning, releasing the
       SQLite write lock.
    2. **Classify + generate** (no DB transaction). Runs LLM calls —
       classifier, then either suggestion variants (HITL parking mode) or
       generate-and-evaluate + connector send (auto-reply mode). Each LLM
       call records its own ``LlmCall`` audit row in a short-lived inner
       session that no longer contends with this outer flow.
    3. **Persist results** (with DB transaction). Refetches the thread
       state in a fresh session, applies status changes, writes the
       parking record / outbound :class:`Message`, and commits.

    Why this matters: the pre-refactor code held a single
    ``session_scope`` open across all LLM calls. On SQLite, the inbound
    Message ``flush()`` acquired the write lock, then 1–4 parallel LLM
    calls each tried to insert their ``LlmCall`` audit row — every one
    waited up to ``PRAGMA busy_timeout`` (2 min) for the outer txn that
    couldn't commit until *they* returned. The deadlock blew through the
    timeout and rolled back the entire pipeline, dropping the inbound on
    the floor. The phased design keeps the outer txn write-window in the
    millisecond range and decouples LLM I/O from DB locking.

    Trade-off: per-thread ``SELECT FOR UPDATE`` locking now only protects
    Phase 1; two concurrent inbounds for the same thread could both
    classify + park in Phase 2/3. With the current single-worker poller
    that's impossible in practice, and the Phase 3 refetch reads the
    latest committed state so the second processor sees the first's
    parking and falls into the ``PAUSED_FOR_HITL`` capture-only branch.
    Multi-worker deployments will need a redis-backed mutex; the
    Postgres v1 cutover is the right point to revisit.
    """

    logger.info(
        "inbound received from=%s provider_id=%s chars=%d",
        incoming.contact_uri,
        incoming.provider_message_id,
        len(incoming.content),
    )

    # ===== Phase 1: persist inbound + capture context =====
    with session_scope() as session:
        workspace = session.get(Workspace, workspace_id)
        if workspace is None:
            logger.error("workspace %s not found while processing inbound", workspace_id)
            return ReplyResult(action="ignored", detail="workspace_missing")

        settings_blob = dict(workspace.settings or {})

        resolved = _resolve_and_capture_inbound(
            session=session,
            connector=connector,
            workspace=workspace,
            incoming=incoming,
            settings_blob=settings_blob,
        )
        if isinstance(resolved, ReplyResult):
            return resolved
        thread, campaign_lead, campaign, lead = resolved

        # Deterministic compliance shortcut — Spam Act 2003 (AU) / TCPA (US).
        # Runs *before* any LLM call so a literal STOP / UNSUBSCRIBE keyword
        # never depends on classifier confidence. Cheap pure-Python regex; if
        # it doesn't match, we fall through to the normal classify-and-route
        # path below.
        opt_out = _apply_opt_out_shortcut(
            session=session,
            workspace=workspace,
            campaign=campaign,
            campaign_lead=campaign_lead,
            lead=lead,
            thread=thread,
            incoming=incoming,
        )
        if opt_out is not None:
            return opt_out

        # Snapshot history + IDs before the session closes. Helpers in
        # Phase 2 read scalar attributes on the detached ORM instances
        # below — ``sessionmaker(expire_on_commit=False)`` keeps those
        # values cached after commit.
        history = thread_history(session, thread)
        settings_llm = settings_blob.get("llm") or {}
        thread_id_snapshot = thread.id
        campaign_lead_id_snapshot = campaign_lead.id
        lead_id_snapshot = lead.id
        session.expunge_all()
    # session committed here — SQLite write lock released

    # ===== Phase 2: classify (no DB transaction held) =====
    cls = await _classify_reply(
        workspace=workspace,
        campaign=campaign,
        lead=lead,
        thread=thread,
        history=history,
        incoming=incoming,
        settings_llm=settings_llm,
    )

    # First-message-only mode (default): every inbound parks for HITL with
    # suggestions, regardless of classifier intent. The classifier verdict
    # still flows through into ``hitl_context`` so the operator can see what
    # the LLM thought, but close-won and close-lost are operator decisions —
    # the pipeline must never silently swallow a reply the human hasn't seen.
    if not bool(settings_blob.get("auto_reply_enabled", False)):
        # Phase 2b + 3: generate suggestions then persist parking
        return await _park_with_suggestions_v2(
            workspace=workspace,
            campaign=campaign,
            lead=lead,
            thread=thread,
            history=history,
            classification=cls,
            incoming=incoming,
            settings_blob=settings_blob,
            thread_id=thread_id_snapshot,
        )

    # Auto-reply mode only: terminal intents short-circuit the
    # generate/evaluate loop. No suggestions get drafted because the bot
    # is the one replying — there's no human in the loop to read them.
    if cls.intent in ("negative", "goal_achieved"):
        return _persist_terminal_close(
            thread_id=thread_id_snapshot,
            campaign_lead_id=campaign_lead_id_snapshot,
            lead_id=lead_id_snapshot,
            classification=cls,
        )

    # Auto-reply path — same phased design (LLM outside session, then commit)
    return await _run_auto_reply_v2(
        connector=connector,
        workspace=workspace,
        campaign=campaign,
        lead=lead,
        thread=thread,
        history=history,
        classification=cls,
        incoming=incoming,
        settings_blob=settings_blob,
        settings_llm=settings_llm,
        thread_id=thread_id_snapshot,
        campaign_lead_id=campaign_lead_id_snapshot,
        lead_id=lead_id_snapshot,
    )


# ---------------------------------------------------------------------------
# Stage 1: resolve the inbound to a thread and record the message
# ---------------------------------------------------------------------------


def _resolve_and_capture_inbound(
    *,
    session: Session,
    connector: BaseConnector,
    workspace: Workspace,
    incoming: IncomingMessage,
    settings_blob: dict[str, Any],
) -> tuple[Thread, CampaignLead, Campaign, Lead] | ReplyResult:
    """Find the target thread and record the inbound ``Message``.

    Returns either the resolved ``(thread, campaign_lead, campaign, lead)``
    tuple (caller continues) or a short-circuit ``ReplyResult`` for the
    unparseable / unmatched / paused-thread cases. Callers should treat
    a ``ReplyResult`` as terminal.
    """

    region_hint = settings_blob.get("default_region", "AU")
    normalised = _resolve_contact(incoming.contact_uri, region_hint)
    if normalised is None:
        logger.warning(
            "inbound unparseable sender=%r — dropping to unmatched_webhook",
            incoming.contact_uri,
        )
        session.add(
            UnmatchedWebhook(
                workspace_id=workspace.id,
                connector_type=connector.connector_type,
                sender_uri=incoming.contact_uri,
                reason="unparseable_sender",
                raw_payload=incoming.raw_payload or {},
            )
        )
        return ReplyResult(action="unmatched", detail="unparseable_sender")

    thread = _resolve_thread(session, workspace.id, normalised)
    if thread is None:
        logger.info("inbound unmatched normalised=%s — no active thread", normalised)
        session.add(
            UnmatchedWebhook(
                workspace_id=workspace.id,
                connector_type=connector.connector_type,
                sender_uri=normalised,
                reason="no_matching_thread",
                raw_payload=incoming.raw_payload or {},
            )
        )
        return ReplyResult(action="unmatched", detail="no_matching_thread")

    thread = _lock_thread(session, thread.id) or thread

    # Idempotency: a poller restart, a transient second worker, or even
    # the SMSGate device server simply re-listing every undeleted SMS in
    # the phone's inbox each tick will re-deliver the same
    # ``IncomingMessage`` to this function. Without this check we'd
    # append a duplicate ``Message`` row to the thread (and re-run the
    # full classify + suggestion fan-out) every time the connector
    # forgot it had seen the id — for example, after every API restart,
    # because :attr:`SmsGateConnector._seen_ids` is per-process and
    # empty on boot. The DB is the single source of truth for "have we
    # ingested this before"; the in-memory set in the connector is now
    # purely a fast-path that skips the network/DB roundtrip.
    if incoming.provider_message_id:
        already_ingested = session.execute(
            select(Message.id).where(
                Message.thread_id == thread.id,
                Message.provider_message_id == incoming.provider_message_id,
            )
        ).scalar_one_or_none()
        if already_ingested is not None:
            logger.info(
                "inbound duplicate skipped thread=%s provider_id=%s",
                thread.id,
                incoming.provider_message_id,
            )
            return ReplyResult(
                action="ignored",
                thread_id=thread.id,
                detail="duplicate_inbound",
            )

    campaign_lead = session.get(CampaignLead, thread.campaign_lead_id)
    campaign = session.get(Campaign, campaign_lead.campaign_id)
    lead = session.get(Lead, campaign_lead.lead_id)

    if thread.status == ThreadStatus.PAUSED_FOR_HITL:
        # Someone already parked this thread. Capture the message so it
        # shows up in the transcript but don't run any AI on it — the
        # human will see the new inbound and decide.
        logger.info(
            "inbound captured but paused thread=%s hitl_reason=%s",
            thread.id,
            thread.hitl_reason,
        )
        session.add(
            Message(
                thread_id=thread.id,
                role=MessageRole.LEAD,
                content=incoming.content,
                provider_message_id=incoming.provider_message_id,
                created_at=incoming.received_at,
                metadata_={
                    "source": connector.connector_type,
                    "provider_message_id": incoming.provider_message_id,
                    "paused_for_hitl": True,
                },
            )
        )
        _mark_replied(campaign_lead=campaign_lead, lead=lead)
        return ReplyResult(
            action="ignored",
            thread_id=thread.id,
            detail="thread_paused_for_hitl",
        )

    session.add(
        Message(
            thread_id=thread.id,
            role=MessageRole.LEAD,
            content=incoming.content,
            provider_message_id=incoming.provider_message_id,
            # Use the connector-provided ``received_at`` as the canonical
            # message timestamp so the transcript reflects when the SMS
            # actually arrived on the device, not when the poller happened
            # to scan the inbox. The DB default of ``utcnow()`` would
            # otherwise compress every backlog message into a single
            # post-restart minute and order them by poll-scan rather than
            # by send time.
            created_at=incoming.received_at,
            metadata_={
                "source": connector.connector_type,
                "provider_message_id": incoming.provider_message_id,
            },
        )
    )
    # Mark the lead as having replied as soon as we capture the inbound,
    # not when we eventually decide to send a draft. The first-message-only
    # default never reaches the auto-reply send path, so without this bump
    # the campaign list / detail "Replied" stat stayed at zero forever even
    # for chatty threads. Downstream paths (opt-out closes lost, terminal
    # auto-reply closes won) will overwrite to the correct terminal status
    # before commit, so this is a floor, not a ceiling.
    _mark_replied(campaign_lead=campaign_lead, lead=lead)
    session.flush()

    return thread, campaign_lead, campaign, lead


# Status transitions from non-terminal pre-reply buckets onto REPLIED. We
# never *demote* (e.g. WON / LOST stay terminal) and we don't promote SKIPPED
# (the lead was deliberately bypassed). PAUSED_FOR_HITL and CONTACTED both
# advance — a thread can be parked for a connector failure pre-reply, and
# the lead's eventual reply is exactly the signal we want to surface.
_REPLY_PROMOTABLE_CL_STATUSES: frozenset[str] = frozenset(
    {
        CampaignLeadStatus.QUEUED,
        CampaignLeadStatus.SENDING,
        CampaignLeadStatus.PAUSED_FOR_HITL,
        CampaignLeadStatus.CONTACTED,
    }
)
_REPLY_PROMOTABLE_LEAD_STATUSES: frozenset[str] = frozenset(
    {LeadStatus.NEW, LeadStatus.CONTACTED}
)


def _mark_replied(*, campaign_lead: CampaignLead, lead: Lead) -> None:
    """Advance ``campaign_lead`` and ``lead`` to the REPLIED bucket.

    Idempotent and demote-safe — see ``_REPLY_PROMOTABLE_*`` for the
    transition table. Caller is responsible for flushing.
    """

    if campaign_lead.status in _REPLY_PROMOTABLE_CL_STATUSES:
        campaign_lead.status = CampaignLeadStatus.REPLIED
    if lead.status in _REPLY_PROMOTABLE_LEAD_STATUSES:
        lead.status = LeadStatus.REPLIED


# ---------------------------------------------------------------------------
# Stage 1b: deterministic opt-out shortcut (compliance)
# ---------------------------------------------------------------------------


def _apply_opt_out_shortcut(
    *,
    session: Session,
    workspace: Workspace,
    campaign: Campaign,
    campaign_lead: CampaignLead,
    lead: Lead,
    thread: Thread,
    incoming: IncomingMessage,
) -> ReplyResult | None:
    """Close-lost the thread + flag the lead do-not-contact when the inbound is a STOP keyword.

    Returns a terminal :class:`ReplyResult` on hit, ``None`` otherwise so the
    caller falls through to the normal LLM classifier path. Writes a sentinel
    :class:`LlmCall` audit row so ``autosdr logs thread`` shows the routing
    decision in the same timeline as real LLM events.
    """

    match: OptOutMatch | None = match_opt_out(incoming.content)
    if match is None:
        return None

    # The opt-out shortcut bypasses the LLM, so the LLM client's killswitch
    # check no longer covers this branch. The webhook will 5xx if paused; the
    # connector will retry the inbound, or the operator can replay it from
    # ``unmatched_webhook`` once the killswitch clears.
    killswitch.raise_if_paused()

    now = datetime.now(timezone.utc)
    if lead.do_not_contact_at is None:
        lead.do_not_contact_at = now
    if not lead.do_not_contact_reason:
        lead.do_not_contact_reason = match.reason

    _close_thread(thread, campaign_lead, lead, won=False)

    session.add(
        LlmCall(
            workspace_id=workspace.id,
            campaign_id=campaign.id,
            thread_id=thread.id,
            lead_id=lead.id,
            purpose=LlmCallPurpose.OTHER,
            model=OPT_OUT_AUDIT_MODEL,
            prompt_version=None,
            temperature=None,
            attempt=1,
            response_format="text",
            system_prompt="(deterministic opt-out shortcut)",
            user_prompt=incoming.content,
            response_text=match.keyword,
            response_parsed={"keyword": match.keyword, "reason": match.reason},
            tokens_in=0,
            tokens_out=0,
            latency_ms=0,
        )
    )
    session.flush()

    logger.info(
        "reply route thread=%s action=closed_opt_out keyword=%r",
        thread.id,
        match.keyword,
    )

    return ReplyResult(
        action="closed_opt_out",
        thread_id=thread.id,
        intent="opt_out",
        confidence=1.0,
        detail=match.keyword,
    )


# ---------------------------------------------------------------------------
# Stage 2: classify
# ---------------------------------------------------------------------------


async def _classify_reply(
    *,
    workspace: Workspace,
    campaign: Campaign,
    lead: Lead,
    thread: Thread,
    history: list[dict[str, Any]],
    incoming: IncomingMessage,
    settings_llm: dict[str, Any],
) -> _Classification:
    """Run the classification LLM and return a normalised verdict.

    History is truncated to exclude the just-captured inbound because the
    classifier prompt expects the message under review to be supplied
    separately via ``build_user_prompt(incoming_message=...)``.
    """

    cls_raw, cls_result = await complete_json(
        system=classification.build_system_prompt(),
        user=classification.build_user_prompt(
            campaign_goal=campaign.goal,
            history=history[:-1],
            incoming_message=incoming.content,
        ),
        model=settings_llm.get("model_classification", settings_llm["model_main"]),
        prompt_version=classification.PROMPT_VERSION,
        temperature=float(settings_llm.get("temperature_eval", 0.0)),
        # Cap thinking budget — see ``reasoning_classification`` in
        # ``autosdr/config.py`` for the long form. Default
        # ``"disable"`` matches Flash-Lite's current behaviour
        # exactly (verified via ``scripts/replay_classifier_smoke.py``)
        # and pins it so a future provider change can't silently
        # inflate per-classification cost.
        reasoning_effort=settings_llm.get("reasoning_classification", "disable"),
        context=LlmCallContext(
            purpose=LlmCallPurpose.CLASSIFICATION,
            workspace_id=workspace.id,
            campaign_id=campaign.id,
            thread_id=thread.id,
            lead_id=lead.id,
        ),
    )
    cls = classification.normalise_classification(cls_raw)

    logger.info(
        "classification thread=%s intent=%s confidence=%.2f requires_human=%s reason=%r",
        thread.id,
        cls["intent"],
        cls["confidence"],
        cls["requires_human"],
        (cls["reason"] or "")[:160],
    )
    return _Classification(
        intent=cls["intent"],
        confidence=cls["confidence"],
        reason=cls["reason"],
        requires_human=bool(cls["requires_human"]),
        llm_call_id=cls_result.llm_call_id,
    )


# ---------------------------------------------------------------------------
# Stage 3a: terminal intents (close won/lost) — Phase 3 DB write
# ---------------------------------------------------------------------------


def _persist_terminal_close(
    *,
    thread_id: str,
    campaign_lead_id: str,
    lead_id: str,
    classification: _Classification,
) -> ReplyResult:
    """Apply the close-won / close-lost status propagation in a fresh txn.

    Refetches the ORM rows so the writes happen against current state.
    Idempotent against the rare race where another caller already closed
    the thread: re-applying the same terminal status is a no-op.
    """

    won = classification.intent == "goal_achieved"
    with session_scope() as session:
        thread = session.get(Thread, thread_id)
        campaign_lead = session.get(CampaignLead, campaign_lead_id)
        lead = session.get(Lead, lead_id)
        if thread is None or campaign_lead is None or lead is None:
            # Race: thread was deleted between Phase 1 and Phase 3. Caller
            # can't recover, just emit a safe terminal result.
            return ReplyResult(
                action="ignored",
                thread_id=thread_id,
                detail="thread_disappeared",
            )

        _close_thread(thread, campaign_lead, lead, won=won)
        session.flush()

    action = "closed_won" if won else "closed_lost"
    logger.info(
        "reply route thread=%s action=%s intent=%s",
        thread_id,
        action,
        classification.intent,
    )
    return ReplyResult(
        action=action,
        thread_id=thread_id,
        intent=classification.intent,
        confidence=classification.confidence,
        detail=classification.reason,
    )


# ---------------------------------------------------------------------------
# Stage 3b: first-message-only mode (default)
# ---------------------------------------------------------------------------


async def _park_with_suggestions_v2(
    *,
    workspace: Workspace,
    campaign: Campaign,
    lead: Lead,
    thread: Thread,
    history: list[dict[str, Any]],
    classification: _Classification,
    incoming: IncomingMessage,
    settings_blob: dict[str, Any],
    thread_id: str,
) -> ReplyResult:
    """Pause the thread for HITL review, stashing N drafted reply variants.

    Two-phase: generate suggestions outside any DB transaction, then open a
    fresh short-lived txn to write the parking state. The detached
    ``thread`` / ``workspace`` / etc. instances we receive are read-only
    inputs to the LLM helpers; the actual mutation happens inside the
    Phase 3 ``session_scope`` after the LLM calls return.
    """

    suggestions_n = int(settings_blob.get("suggestions_count", 3))
    try:
        suggestions = await generate_reply_variants(
            workspace=workspace,
            campaign=campaign,
            lead=lead,
            thread=thread,
            history=history,
            n=suggestions_n,
        )
    except Exception:
        # Never let a suggestion failure drop the inbound on the floor —
        # the human still needs to see the message and be able to reply
        # manually.
        logger.exception(
            "reply suggestions failed thread=%s — parking without drafts",
            thread_id,
        )
        suggestions = []

    with session_scope() as session:
        live_thread = session.get(Thread, thread_id)
        if live_thread is None:
            return ReplyResult(
                action="ignored",
                thread_id=thread_id,
                detail="thread_disappeared",
            )

        # Race-safety: if another concurrent processor already parked
        # this thread, leave its parking state alone and just record that
        # we observed the inbound. Phase 1 already wrote the message.
        if live_thread.status == ThreadStatus.PAUSED_FOR_HITL:
            logger.info(
                "reply skip-park thread=%s already paused (race) reason=%s",
                thread_id,
                live_thread.hitl_reason,
            )
            return ReplyResult(
                action="ignored",
                thread_id=thread_id,
                intent=classification.intent,
                confidence=classification.confidence,
                detail="thread_paused_for_hitl",
            )

        pause_thread_for_hitl(
            live_thread,
            reason=HITL_AWAITING_HUMAN_REPLY,
            context={
                "intent": classification.intent,
                "confidence": classification.confidence,
                "reason": classification.reason,
                "incoming_message": incoming.content,
                "classification_llm_call_id": classification.llm_call_id,
                "suggestions": suggestions,
            },
        )
        session.flush()

    schedule_hitl_push(
        thread_id=thread_id,
        lead_name=lead.name,
        hitl_reason=HITL_AWAITING_HUMAN_REPLY,
    )
    logger.info(
        "reply parked thread=%s reason=%s intent=%s suggestions=%d",
        thread_id,
        HITL_AWAITING_HUMAN_REPLY,
        classification.intent,
        len(suggestions),
    )
    return ReplyResult(
        action="escalated_hitl",
        thread_id=thread_id,
        intent=classification.intent,
        confidence=classification.confidence,
        detail=HITL_AWAITING_HUMAN_REPLY,
    )


# ---------------------------------------------------------------------------
# Stage 3c: legacy auto-reply mode (opt-in)
# ---------------------------------------------------------------------------


async def _run_auto_reply_v2(
    *,
    connector: BaseConnector,
    workspace: Workspace,
    campaign: Campaign,
    lead: Lead,
    thread: Thread,
    history: list[dict[str, Any]],
    classification: _Classification,
    incoming: IncomingMessage,
    settings_blob: dict[str, Any],
    settings_llm: dict[str, Any],
    thread_id: str,
    campaign_lead_id: str,
    lead_id: str,
) -> ReplyResult:
    """Run the generate → evaluate → send loop with phased session use.

    Phase 2 (this body up through ``connector.send``) does no DB writes
    — all LLM calls and the SMS dispatch happen with no transaction
    held. Phase 3 (the ``session_scope`` blocks below) refetches the
    ORM rows and writes the outbound :class:`Message` and the thread /
    campaign_lead / lead status updates.

    Only used when ``auto_reply_enabled`` is true. Short-circuits to
    HITL on the first of: classifier wanted a human, auto-reply ceiling
    hit, evaluator never passed, or the connector send failed.
    """

    max_auto_replies = int(settings_blob.get("max_auto_replies", 5))

    # ---- Phase 3 helper: pause for HITL with arbitrary context ----
    def _persist_pause(reason: str, context: dict[str, Any]) -> ReplyResult:
        with session_scope() as session:
            live_thread = session.get(Thread, thread_id)
            if live_thread is None:
                return ReplyResult(
                    action="ignored",
                    thread_id=thread_id,
                    detail="thread_disappeared",
                )
            pause_thread_for_hitl(live_thread, reason=reason, context=context)
            session.flush()
        schedule_hitl_push(
            thread_id=thread_id,
            lead_name=lead.name,
            hitl_reason=reason,
        )
        return ReplyResult(
            action="escalated_hitl",
            thread_id=thread_id,
            intent=classification.intent,
            confidence=classification.confidence,
            detail=reason,
        )

    if classification.requires_human or thread.auto_reply_count >= max_auto_replies:
        hitl_reason = _hitl_reason_for(
            intent=classification.intent,
            confidence=classification.confidence,
            auto_reply_count=thread.auto_reply_count,
            max_auto_replies=max_auto_replies,
        )
        logger.warning(
            "reply escalated thread=%s reason=%s intent=%s confidence=%.2f",
            thread_id,
            hitl_reason,
            classification.intent,
            classification.confidence,
        )
        return _persist_pause(
            hitl_reason,
            {
                "intent": classification.intent,
                "confidence": classification.confidence,
                "reason": classification.reason,
                "incoming_message": incoming.content,
            },
        )

    _, eval_threshold, eval_max_attempts = read_loop_settings(workspace)

    loop_result = await generate_and_evaluate(
        settings_llm=settings_llm,
        eval_threshold=eval_threshold,
        eval_max_attempts=eval_max_attempts,
        workspace=workspace,
        campaign=campaign,
        lead=lead,
        thread=thread,
        angle=thread.angle or "",
        message_history=history,
    )

    if loop_result["status"] != "pass":
        logger.warning(
            "reply escalated thread=%s reason=reply_eval_failed attempts=%d",
            thread_id,
            loop_result["attempts"],
        )
        return _persist_pause(
            "reply_eval_failed",
            hitl_context_from_loop_failure(
                loop_result,
                intent=classification.intent,
                confidence=classification.confidence,
                incoming_message=incoming.content,
            ),
        )

    draft = loop_result["draft"]

    send_result = await connector.send(
        OutgoingMessage(contact_uri=lead.contact_uri, content=draft)
    )
    if not send_result.success:
        logger.error(
            "reply send failed thread=%s error=%s", thread_id, send_result.error
        )
        return _persist_pause(
            "connector_send_failed",
            hitl_context_from_send_failure(
                draft=draft,
                send_result=send_result,
                loop_result=loop_result,
                intent=classification.intent,
                confidence=classification.confidence,
                incoming_message=incoming.content,
            ),
        )

    # ---- Phase 3: persist the successful outbound + status flips ----
    with session_scope() as session:
        live_thread = session.get(Thread, thread_id)
        live_cl = session.get(CampaignLead, campaign_lead_id)
        live_lead = session.get(Lead, lead_id)
        if live_thread is None or live_cl is None or live_lead is None:
            return ReplyResult(
                action="ignored",
                thread_id=thread_id,
                detail="thread_disappeared",
            )

        session.add(
            Message(
                thread_id=thread_id,
                role=MessageRole.AI,
                content=draft,
                metadata_=build_send_metadata(
                    loop_result=loop_result,
                    settings_llm=settings_llm,
                    send_result=send_result,
                    intent=classification.intent,
                    confidence=classification.confidence,
                    classification_model=settings_llm.get(
                        "model_classification", settings_llm["model_main"]
                    ),
                    classification_llm_call_id=classification.llm_call_id,
                ),
            )
        )
        live_thread.auto_reply_count += 1
        live_thread.status = ThreadStatus.ACTIVE
        live_cl.status = CampaignLeadStatus.REPLIED
        live_lead.status = LeadStatus.REPLIED
        session.flush()

    logger.info(
        "reply sent thread=%s intent=%s confidence=%.2f score=%.3f chars=%d",
        thread_id,
        classification.intent,
        classification.confidence,
        loop_result["overall"],
        len(draft),
    )
    return ReplyResult(
        action="sent",
        thread_id=thread_id,
        intent=classification.intent,
        confidence=classification.confidence,
    )


def _hitl_reason_for(
    *, intent: str, confidence: float, auto_reply_count: int, max_auto_replies: int
) -> str:
    if intent == "bot_check":
        return "bot_check"
    if intent == "human_requested":
        return "human_requested"
    if intent == "unclear":
        return "unclear"
    if confidence < 0.80:
        return "low_confidence"
    if auto_reply_count >= max_auto_replies:
        return "max_auto_replies_reached"
    return "escalated"


def _close_thread(
    thread: Thread, campaign_lead: CampaignLead, lead: Lead, *, won: bool
) -> None:
    """Apply status propagation for terminal intents."""

    if won:
        thread.status = ThreadStatus.WON
        campaign_lead.status = CampaignLeadStatus.WON
        lead.status = LeadStatus.WON
    else:
        thread.status = ThreadStatus.LOST
        campaign_lead.status = CampaignLeadStatus.LOST
        lead.status = LeadStatus.LOST


__all__ = [
    "HITL_AWAITING_HUMAN_REPLY",
    "ReplyResult",
    "process_incoming_message",
]
