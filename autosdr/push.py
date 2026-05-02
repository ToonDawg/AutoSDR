"""Web Push transport + VAPID key lifecycle for HITL escalations.

Ticket 0005. The shape is:

* :func:`ensure_vapid_keys` runs at app startup (once) and writes a
  VAPID keypair to ``workspace.settings.push.{vapid_public,
  vapid_private}`` if the operator hasn't got one yet. The private
  half never leaves the server.
* :func:`build_hitl_payload` produces the privacy-strict notification
  body the SW is allowed to render. The Critic-mandated rule from the
  ticket's *Remote-access architecture* council round: thread id +
  lead first name + hitl reason + escalated_at, and *nothing else*.
  No message content, no last name, no business name. The shape is
  pinned by ``tests/test_push_payload_privacy.py`` so a future change
  to ``pause_thread_for_hitl`` can't quietly leak more.
* :func:`send_push` wraps :func:`pywebpush.webpush` for one
  subscription and translates HTTP 404/410 into a "delete this row"
  signal so the caller can hard-delete dead subs.
* :func:`fanout_hitl_push` is the call the HITL hot-path makes — it
  honours the killswitch (no pushes during a paused workspace, see
  :mod:`autosdr.killswitch`), runs each per-subscription ``webpush``
  in :func:`asyncio.to_thread` so a slow gateway never blocks the
  reply pipeline, and hard-deletes any subscriptions that returned
  *gone*.

The transport never raises. The HITL hot-path treats missed push
attempts as best-effort by design — operator-visible state is the
HITL queue itself, not the OS notification.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from sqlalchemy import select
from sqlalchemy.orm import Session

from autosdr.config import merge_workspace_settings
from autosdr.db import session_scope
from autosdr.killswitch import is_paused
from autosdr.models import PushSubscription, Workspace

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# VAPID keypair helpers (RFC 8292 voluntary application identification).
# ---------------------------------------------------------------------------


def _b64url_no_pad(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _generate_vapid_pair() -> tuple[str, str]:
    """Generate a fresh P-256 keypair encoded for Web Push.

    Returns ``(public_b64url, private_b64url)``. The public encoding is
    the uncompressed X9.62 octet string (65 bytes, prefix ``0x04``) the
    Push API mandates; the private is the 32-byte scalar.
    """

    private_key = ec.generate_private_key(ec.SECP256R1())
    public_bytes = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.X962,
        format=serialization.PublicFormat.UncompressedPoint,
    )
    private_int = private_key.private_numbers().private_value
    private_bytes = private_int.to_bytes(32, "big")
    return _b64url_no_pad(public_bytes), _b64url_no_pad(private_bytes)


def ensure_vapid_keys() -> dict[str, str | None]:
    """Load — or generate-and-persist — the workspace VAPID keypair.

    Idempotent: subsequent calls return the existing keypair untouched.
    Safe to call before the setup wizard has run (returns an
    all-``None`` dict in that case; the caller handles "no workspace
    yet" by falling back to "no push" without crashing).

    Caller is the FastAPI lifespan in :mod:`autosdr.webhook`.
    """

    with session_scope() as session:
        workspace = session.query(Workspace).first()
        if workspace is None:
            return {"public": None, "private": None, "subject": None}

        existing = dict(workspace.settings or {})
        push = existing.get("push") or {}
        public = push.get("vapid_public")
        private = push.get("vapid_private")
        subject = push.get("vapid_subject") or "mailto:autosdr@localhost"

        if public and private:
            return {"public": public, "private": private, "subject": subject}

        public, private = _generate_vapid_pair()
        merged = merge_workspace_settings(
            existing,
            {
                "push": {
                    "vapid_public": public,
                    "vapid_private": private,
                    "vapid_subject": subject,
                }
            },
        )
        workspace.settings = merged
        logger.info("push: generated workspace VAPID keypair (first boot)")
        return {"public": public, "private": private, "subject": subject}


# ---------------------------------------------------------------------------
# Privacy-strict notification payload.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class HitlPushPayload:
    """The fixed shape every HITL push notification carries.

    Only fields listed here ever leave the server. The dataclass is
    ``frozen=True`` so a programming error elsewhere can't mutate the
    payload between build and send. ``__init__`` is the only entry
    point.
    """

    title: str
    body: str
    thread_id: str
    lead_first_name: str
    hitl_reason: str
    escalated_at: str
    url: str

    def as_dict(self) -> dict[str, str]:
        return {
            "title": self.title,
            "body": self.body,
            "thread_id": self.thread_id,
            "lead_first_name": self.lead_first_name,
            "hitl_reason": self.hitl_reason,
            "escalated_at": self.escalated_at,
            "url": self.url,
        }


def _first_name_only(name: str | None) -> str:
    """Return the first whitespace-delimited token of ``name``, or ``""``.

    The privacy posture forbids leaking the lead's last name or business
    name; collapsing to the first token loses zero information the
    operator can use ("Sarah needs your eye") while preserving
    de-anonymisation if the lock-screen is glanced at by someone else.
    """

    if not name:
        return ""
    return name.strip().split()[0] if name.strip() else ""


def build_hitl_payload(
    *,
    thread_id: str,
    lead_name: str | None,
    hitl_reason: str,
    escalated_at: datetime,
    dashboard_origin: str | None,
) -> HitlPushPayload:
    """Compose a HITL push payload for one thread.

    ``dashboard_origin`` is what the SW reads to assemble the
    notification's ``click`` URL. The path component is fixed —
    ``/inbox/<thread_id>`` — to align with the mobile master-detail
    routing collapse from ticket 0015.
    """

    first = _first_name_only(lead_name)
    title = f"AutoSDR: {first} needs your eye" if first else "AutoSDR: thread needs your eye"
    body = "Tap to triage."
    origin = (dashboard_origin or "").rstrip("/")
    url = f"{origin}/inbox/{thread_id}" if origin else f"/inbox/{thread_id}"
    iso_ts = escalated_at.astimezone(timezone.utc).isoformat()
    return HitlPushPayload(
        title=title,
        body=body,
        thread_id=thread_id,
        lead_first_name=first,
        hitl_reason=hitl_reason,
        escalated_at=iso_ts,
        url=url,
    )


# ---------------------------------------------------------------------------
# Transport — one subscription at a time.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PushSendResult:
    """What :func:`send_push` reports back to the caller.

    ``gone`` means the gateway said this subscription is dead (HTTP
    404/410); the caller hard-deletes the row. ``error`` is any other
    transport failure — kept on the row's ``last_error`` so Settings →
    Notifications can surface it; the row stays.
    """

    ok: bool
    gone: bool = False
    status_code: int | None = None
    error: str | None = None


def send_push(
    *,
    subscription_info: dict[str, Any],
    payload: dict[str, Any],
    vapid_private: str,
    vapid_subject: str,
) -> PushSendResult:
    """Send one push notification. Synchronous; intended for ``asyncio.to_thread``.

    ``pywebpush`` is requests-based, so wrapping it in
    :func:`asyncio.to_thread` at every call site is the
    project-blessed pattern (see :doc:`docs/PATTERNS.md` — outbound HTTP
    is httpx for AutoSDR-controlled traffic, pywebpush gets a thread
    pool exemption because it owns the encryption). Never raises;
    every failure returns a :class:`PushSendResult`.
    """

    try:
        import pywebpush  # local import keeps test runs cheap when push is unused
    except ImportError as exc:  # pragma: no cover — install-time error
        return PushSendResult(ok=False, error=f"pywebpush import failed: {exc}")

    try:
        response = pywebpush.webpush(
            subscription_info=subscription_info,
            data=json.dumps(payload),
            vapid_private_key=vapid_private,
            vapid_claims={"sub": vapid_subject},
            timeout=10,
        )
    except pywebpush.WebPushException as exc:  # type: ignore[attr-defined]
        status = getattr(getattr(exc, "response", None), "status_code", None)
        if status in (404, 410):
            return PushSendResult(ok=False, gone=True, status_code=status, error=str(exc))
        return PushSendResult(ok=False, status_code=status, error=str(exc))
    except Exception as exc:  # noqa: BLE001 — final safety net for the hot path
        return PushSendResult(ok=False, error=f"{type(exc).__name__}: {exc}")

    if isinstance(response, str):
        return PushSendResult(ok=True)
    return PushSendResult(ok=True, status_code=getattr(response, "status_code", None))


# ---------------------------------------------------------------------------
# Fan-out — used by the HITL hook + the test-fire endpoint.
# ---------------------------------------------------------------------------


def _subscription_info(row: PushSubscription) -> dict[str, Any]:
    return {
        "endpoint": row.endpoint,
        "keys": {"p256dh": row.p256dh, "auth": row.auth},
    }


def _resolve_dashboard_origin(
    *, row: PushSubscription, settings_blob: dict[str, Any]
) -> str | None:
    """Settings override wins over the row's snapshot; both fall back to ``None``.

    Resolution order matches the OQ-Net3 verdict in the ticket:
    operator override → ``Host`` header captured at subscribe-time →
    ``None`` (the SW falls back to same-origin).
    """

    push = (settings_blob or {}).get("push") or {}
    override = (push.get("dashboard_origin") or "").strip()
    if override:
        return override
    return (row.dashboard_origin or "").strip() or None


def _load_workspace_push_config(session: Session) -> dict[str, Any] | None:
    workspace = session.query(Workspace).first()
    if workspace is None:
        return None
    settings = dict(workspace.settings or {})
    push = settings.get("push") or {}
    if not push.get("vapid_public") or not push.get("vapid_private"):
        return None
    return {
        "settings": settings,
        "vapid_private": push["vapid_private"],
        "vapid_subject": push.get("vapid_subject") or "mailto:autosdr@localhost",
        "hitl_escalations": bool(push.get("hitl_escalations", True)),
    }


async def _send_to_row(
    *,
    row_id: str,
    subscription_info: dict[str, Any],
    payload: dict[str, Any],
    vapid_private: str,
    vapid_subject: str,
) -> tuple[str, PushSendResult]:
    result = await asyncio.to_thread(
        send_push,
        subscription_info=subscription_info,
        payload=payload,
        vapid_private=vapid_private,
        vapid_subject=vapid_subject,
    )
    return row_id, result


def _apply_results(
    *, session: Session, results: list[tuple[str, PushSendResult]]
) -> None:
    """Hard-delete *gone* rows; stamp ``last_error`` / ``last_seen_at`` on the rest."""

    for row_id, result in results:
        row = session.get(PushSubscription, row_id)
        if row is None:
            continue
        if result.gone:
            session.delete(row)
            logger.warning(
                "push: hard-deleting subscription %s (gone: %s)", row_id, result.error
            )
            continue
        if result.ok:
            row.last_error = None
            row.last_seen_at = datetime.now(timezone.utc)
        else:
            row.last_error = (result.error or "send failed")[:512]
            logger.warning("push: subscription %s send failed: %s", row_id, result.error)


async def fanout_hitl_push(
    *,
    thread_id: str,
    lead_name: str | None,
    hitl_reason: str,
    escalated_at: datetime,
) -> int:
    """Fire one push to every active subscription. Best-effort, never raises.

    Returns the number of *successful* sends — useful in logs but
    deliberately not surfaced in the HITL response so a temporary push-
    gateway outage can't turn into a HITL pipeline error.
    """

    if is_paused():
        logger.info("push: skipping HITL fanout — workspace paused")
        return 0

    snapshot: list[tuple[str, dict[str, Any], str | None]] = []
    config: dict[str, Any] | None = None

    with session_scope() as session:
        config = _load_workspace_push_config(session)
        if config is None:
            logger.info("push: no VAPID keys configured — fanout no-op")
            return 0
        if not config["hitl_escalations"]:
            logger.info("push: hitl_escalations disabled — fanout no-op")
            return 0
        rows = session.execute(select(PushSubscription)).scalars().all()
        for row in rows:
            snapshot.append(
                (
                    row.id,
                    _subscription_info(row),
                    _resolve_dashboard_origin(row=row, settings_blob=config["settings"]),
                )
            )

    if not snapshot:
        return 0

    coros = []
    for row_id, sub_info, origin in snapshot:
        payload = build_hitl_payload(
            thread_id=thread_id,
            lead_name=lead_name,
            hitl_reason=hitl_reason,
            escalated_at=escalated_at,
            dashboard_origin=origin,
        ).as_dict()
        coros.append(
            _send_to_row(
                row_id=row_id,
                subscription_info=sub_info,
                payload=payload,
                vapid_private=config["vapid_private"],
                vapid_subject=config["vapid_subject"],
            )
        )

    results = await asyncio.gather(*coros, return_exceptions=False)

    successes = sum(1 for _, result in results if result.ok)
    with session_scope() as session:
        _apply_results(session=session, results=list(results))

    logger.info(
        "push: HITL fanout fired %d/%d to thread=%s reason=%s",
        successes,
        len(results),
        thread_id,
        hitl_reason,
    )
    return successes


__all__ = [
    "HitlPushPayload",
    "PushSendResult",
    "build_hitl_payload",
    "ensure_vapid_keys",
    "fanout_hitl_push",
    "send_push",
]
