"""Web Push subscription + test-fire endpoints (ticket 0005).

Public surface:

* ``GET /api/push/vapid-public`` — the public half of the workspace
  VAPID keypair plus the deep-link ``dashboard_origin`` the SW should
  use. Both can be ``None`` before the lifespan has generated keys
  (the SW treats that as "push isn't enabled yet").
* ``POST /api/push/subscribe`` — upserts the device's subscription
  on its ``endpoint`` (the natural unique key Web Push assigns).
  The request's ``Host`` header is the dashboard-origin snapshot so
  the deep-link works even before the operator sets the override.
* ``DELETE /api/push/subscribe`` — hard-deletes by endpoint. Returns
  204 whether or not anything matched, so the SW's "remove me"
  request is idempotent.
* ``GET /api/push/subscriptions`` — Settings → Notifications list.
* ``POST /api/push/test`` — fires a test notification to one
  (``endpoint``-targeted) or all subscriptions; reports per-row
  outcome.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from urllib.parse import urlparse

from fastapi import APIRouter, HTTPException, Request, Response, status
from sqlalchemy import select

from autosdr.api.deps import db_session, require_workspace
from autosdr.api.schemas import (
    PushSubscribeRequest,
    PushSubscriptionOut,
    PushSubscriptionsOut,
    PushTestRequest,
    PushTestResult,
    PushVapidPublicOut,
)
from autosdr.models import PushSubscription
from autosdr.push import build_hitl_payload, send_push

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/push", tags=["push"])


def _origin_from_request(request: Request) -> str | None:
    """Best-effort origin reconstruction from the inbound headers.

    Honours the proxy-forwarded headers if present (so a Cloudflare
    Tunnel deployment also gets the right deep-link origin) and falls
    back to FastAPI's resolved scheme + Host.
    """

    forwarded_proto = request.headers.get("x-forwarded-proto")
    forwarded_host = request.headers.get("x-forwarded-host")
    host = forwarded_host or request.headers.get("host")
    if not host:
        return None
    scheme = forwarded_proto or request.url.scheme or "http"
    return f"{scheme}://{host}"


def _resolve_dashboard_origin(workspace_settings: dict, request: Request | None) -> str | None:
    """Push override > saved subscription origin > request Host. ``None`` last."""

    push = (workspace_settings or {}).get("push") or {}
    override = (push.get("dashboard_origin") or "").strip()
    if override:
        return override
    if request is not None:
        return _origin_from_request(request)
    return None


def _endpoint_host(endpoint: str) -> str:
    try:
        host = urlparse(endpoint).netloc
    except (ValueError, AttributeError):
        return ""
    return host or ""


def _to_out(row: PushSubscription) -> PushSubscriptionOut:
    return PushSubscriptionOut(
        id=row.id,
        user_agent=row.user_agent,
        created_at=row.created_at,
        last_seen_at=row.last_seen_at,
        last_error=row.last_error,
        endpoint_host=_endpoint_host(row.endpoint),
    )


@router.get("/vapid-public", response_model=PushVapidPublicOut)
def get_vapid_public(request: Request) -> PushVapidPublicOut:
    """Return the workspace's public VAPID key + dashboard origin."""

    with db_session() as session:
        workspace = require_workspace(session)
        push = (workspace.settings or {}).get("push") or {}
        return PushVapidPublicOut(
            public_key=push.get("vapid_public") or None,
            dashboard_origin=_resolve_dashboard_origin(workspace.settings, request),
        )


@router.post("/subscribe", response_model=PushSubscriptionOut)
def subscribe(body: PushSubscribeRequest, request: Request) -> PushSubscriptionOut:
    """Upsert a push subscription on its endpoint URL."""

    if body.keys is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="missing subscription keys",
        )

    snapshot_origin = _origin_from_request(request)

    with db_session() as session:
        workspace = require_workspace(session)
        existing = (
            session.execute(
                select(PushSubscription).where(PushSubscription.endpoint == body.endpoint)
            )
            .scalar_one_or_none()
        )
        if existing is None:
            row = PushSubscription(
                workspace_id=workspace.id,
                endpoint=body.endpoint,
                p256dh=body.keys.p256dh,
                auth=body.keys.auth,
                user_agent=body.user_agent,
                dashboard_origin=snapshot_origin,
            )
            session.add(row)
            session.flush()
        else:
            existing.workspace_id = workspace.id
            existing.p256dh = body.keys.p256dh
            existing.auth = body.keys.auth
            existing.user_agent = body.user_agent or existing.user_agent
            if snapshot_origin:
                existing.dashboard_origin = snapshot_origin
            existing.last_error = None
            existing.last_seen_at = datetime.now(timezone.utc)
            row = existing
        return _to_out(row)


@router.delete("/subscribe", status_code=status.HTTP_204_NO_CONTENT)
def unsubscribe(body: PushSubscribeRequest) -> Response:
    """Hard-delete by endpoint. Idempotent."""

    with db_session() as session:
        require_workspace(session)
        existing = (
            session.execute(
                select(PushSubscription).where(PushSubscription.endpoint == body.endpoint)
            )
            .scalar_one_or_none()
        )
        if existing is not None:
            session.delete(existing)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/subscriptions", response_model=PushSubscriptionsOut)
def list_subscriptions(request: Request) -> PushSubscriptionsOut:
    """Settings → Notifications view."""

    with db_session() as session:
        workspace = require_workspace(session)
        rows = (
            session.execute(
                select(PushSubscription)
                .where(PushSubscription.workspace_id == workspace.id)
                .order_by(PushSubscription.created_at.desc())
            )
            .scalars()
            .all()
        )
        push = (workspace.settings or {}).get("push") or {}
        return PushSubscriptionsOut(
            subscriptions=[_to_out(row) for row in rows],
            hitl_escalations=bool(push.get("hitl_escalations", True)),
            dashboard_origin=_resolve_dashboard_origin(workspace.settings, request),
        )


@router.post("/test", response_model=PushTestResult)
def fire_test(body: PushTestRequest, request: Request) -> PushTestResult:
    """Fire a test notification to one or all subscriptions.

    Synchronous + serial — this is an operator-initiated affordance
    that runs maybe twice in the lifetime of an install. Wrapping it
    in :func:`asyncio.to_thread` would buy nothing and cost a fanout
    primitive duplicated from :func:`autosdr.push.fanout_hitl_push`.
    """

    sent = gone = failed = 0
    rows_to_persist: list[tuple[str, str | None]] = []

    with db_session() as session:
        workspace = require_workspace(session)
        push = (workspace.settings or {}).get("push") or {}
        vapid_private = push.get("vapid_private")
        vapid_subject = push.get("vapid_subject") or "mailto:autosdr@localhost"
        if not vapid_private:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="VAPID keys not yet generated",
            )

        stmt = select(PushSubscription).where(
            PushSubscription.workspace_id == workspace.id
        )
        if body.endpoint:
            stmt = stmt.where(PushSubscription.endpoint == body.endpoint)
        rows = session.execute(stmt).scalars().all()
        if not rows:
            return PushTestResult(sent=0, gone=0, failed=0)

        origin = _resolve_dashboard_origin(workspace.settings, request)
        payload = build_hitl_payload(
            thread_id="test",
            lead_name="Test Notification",
            hitl_reason="test",
            escalated_at=datetime.now(timezone.utc),
            dashboard_origin=origin,
        ).as_dict()
        payload["title"] = "AutoSDR test notification"
        payload["body"] = "If you can read this, push is working."

        for row in rows:
            sub_info = {
                "endpoint": row.endpoint,
                "keys": {"p256dh": row.p256dh, "auth": row.auth},
            }
            result = send_push(
                subscription_info=sub_info,
                payload=payload,
                vapid_private=vapid_private,
                vapid_subject=vapid_subject,
            )
            if result.gone:
                gone += 1
                rows_to_persist.append((row.id, None))
            elif result.ok:
                sent += 1
                rows_to_persist.append((row.id, "OK"))
            else:
                failed += 1
                rows_to_persist.append((row.id, result.error or "send failed"))

        for row_id, error in rows_to_persist:
            row = session.get(PushSubscription, row_id)
            if row is None:
                continue
            if error is None:
                session.delete(row)
            elif error == "OK":
                row.last_error = None
                row.last_seen_at = datetime.now(timezone.utc)
            else:
                row.last_error = error[:512]

    logger.info(
        "push: test fanout sent=%d gone=%d failed=%d", sent, gone, failed
    )
    return PushTestResult(sent=sent, gone=gone, failed=failed)


__all__ = ["router"]
