"""Transport-layer behaviour for ticket 0005 unit 4.

These tests mock :func:`pywebpush.webpush` so the suite never touches a
real push gateway. They pin the contract every consumer of the
transport assumes:

* HTTP 404 / 410 from the gateway is a *gone* signal — the row is
  hard-deleted by the fanout helper.
* Other gateway failures stamp ``last_error`` on the row but do not
  delete it.
* :func:`fanout_hitl_push` is a no-op when the killswitch is paused
  (no push attempts; rows untouched).
* :func:`fanout_hitl_push` is a no-op when ``hitl_escalations`` is
  off in workspace settings.
* :func:`fanout_hitl_push` is a no-op when no VAPID keys are
  configured (lifespan hasn't generated them yet).
"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import patch

import pytest
from pywebpush import WebPushException

from autosdr.db import session_scope
from autosdr.killswitch import remove_flag, touch_flag
from autosdr.models import PushSubscription, Workspace
from autosdr.push import (
    PushSendResult,
    build_hitl_payload,
    ensure_vapid_keys,
    fanout_hitl_push,
)


@pytest.fixture
def seeded_subscription(fresh_db, workspace_factory):
    """One workspace + one push subscription + a generated VAPID keypair."""

    workspace_id = workspace_factory()
    ensure_vapid_keys()
    with session_scope() as session:
        sub = PushSubscription(
            workspace_id=workspace_id,
            endpoint="https://push.example.test/abc",
            p256dh="p1",
            auth="a1",
            user_agent="iPhone Safari",
            dashboard_origin="http://autosdr.tail-scale.ts.net:8000",
        )
        session.add(sub)
        session.flush()
        return sub.id


def _fake_response(status_code: int = 201):
    class _R:
        pass

    response = _R()
    response.status_code = status_code
    return response


@pytest.mark.asyncio
async def test_fanout_sends_to_each_subscription(seeded_subscription):
    """A successful send refreshes ``last_seen_at`` and clears any old error."""

    with session_scope() as session:
        row = session.get(PushSubscription, seeded_subscription)
        row.last_error = "stale"

    with patch("pywebpush.webpush", return_value=_fake_response(201)):
        sent = await fanout_hitl_push(
            thread_id="t-1",
            lead_name="Sarah Chen",
            hitl_reason="objection",
            escalated_at=datetime.now(timezone.utc),
        )
    assert sent == 1
    with session_scope() as session:
        row = session.get(PushSubscription, seeded_subscription)
        assert row.last_error is None


@pytest.mark.asyncio
async def test_fanout_hard_deletes_gone_subscriptions(seeded_subscription):
    """HTTP 410 = "gone" → hard-delete the row."""

    response = _fake_response(410)
    exc = WebPushException("Gone", response=response)

    with patch("pywebpush.webpush", side_effect=exc):
        sent = await fanout_hitl_push(
            thread_id="t-1",
            lead_name="Sarah",
            hitl_reason="confused",
            escalated_at=datetime.now(timezone.utc),
        )
    assert sent == 0
    with session_scope() as session:
        assert session.get(PushSubscription, seeded_subscription) is None


@pytest.mark.asyncio
async def test_fanout_records_last_error_for_non_gone_failures(
    seeded_subscription,
):
    """5xx-style failures stamp ``last_error`` but keep the row."""

    response = _fake_response(500)
    exc = WebPushException("Bad Gateway", response=response)

    with patch("pywebpush.webpush", side_effect=exc):
        sent = await fanout_hitl_push(
            thread_id="t-1",
            lead_name="Sarah",
            hitl_reason="confused",
            escalated_at=datetime.now(timezone.utc),
        )
    assert sent == 0
    with session_scope() as session:
        row = session.get(PushSubscription, seeded_subscription)
        assert row is not None
        assert row.last_error and "Bad Gateway" in row.last_error


@pytest.mark.asyncio
async def test_fanout_is_noop_when_killswitch_paused(seeded_subscription):
    touch_flag()
    try:
        with patch("pywebpush.webpush") as mock:
            sent = await fanout_hitl_push(
                thread_id="t-1",
                lead_name="Sarah",
                hitl_reason="confused",
                escalated_at=datetime.now(timezone.utc),
            )
        assert sent == 0
        assert mock.call_count == 0
    finally:
        remove_flag()


@pytest.mark.asyncio
async def test_fanout_is_noop_when_hitl_escalations_off(
    seeded_subscription,
):
    with session_scope() as session:
        workspace = session.query(Workspace).first()
        settings = dict(workspace.settings or {})
        push = dict(settings.get("push") or {})
        push["hitl_escalations"] = False
        settings["push"] = push
        workspace.settings = settings

    with patch("pywebpush.webpush") as mock:
        sent = await fanout_hitl_push(
            thread_id="t-1",
            lead_name="Sarah",
            hitl_reason="confused",
            escalated_at=datetime.now(timezone.utc),
        )
    assert sent == 0
    assert mock.call_count == 0


@pytest.mark.asyncio
async def test_fanout_is_noop_without_vapid_keys(fresh_db, workspace_factory):
    """The lifespan hasn't generated keys yet — fanout silently no-ops."""

    workspace_factory()

    with patch("pywebpush.webpush") as mock:
        sent = await fanout_hitl_push(
            thread_id="t-1",
            lead_name="Sarah",
            hitl_reason="confused",
            escalated_at=datetime.now(timezone.utc),
        )
    assert sent == 0
    assert mock.call_count == 0


def test_build_payload_strips_to_first_name():
    """The privacy posture from § *Remote-access architecture* council."""

    payload = build_hitl_payload(
        thread_id="t-1",
        lead_name="Sarah Chen O'Brien",
        hitl_reason="objection",
        escalated_at=datetime(2026, 5, 2, 12, 0, tzinfo=timezone.utc),
        dashboard_origin="http://autosdr.tail-scale.ts.net:8000",
    )
    blob = payload.as_dict()
    assert blob["lead_first_name"] == "Sarah"
    assert blob["title"] == "AutoSDR: Sarah needs your eye"
    assert blob["url"] == "http://autosdr.tail-scale.ts.net:8000/inbox/t-1"
    assert "Chen" not in blob["title"]
    assert "Chen" not in blob["body"]
    assert "Chen" not in blob["url"]
    assert all("Chen" not in str(value) for value in blob.values())
    assert blob["escalated_at"] == "2026-05-02T12:00:00+00:00"


def test_build_payload_handles_missing_name():
    payload = build_hitl_payload(
        thread_id="t-1",
        lead_name=None,
        hitl_reason="confused",
        escalated_at=datetime(2026, 5, 2, 12, 0, tzinfo=timezone.utc),
        dashboard_origin=None,
    )
    blob = payload.as_dict()
    assert blob["lead_first_name"] == ""
    assert blob["title"] == "AutoSDR: thread needs your eye"
    assert blob["url"] == "/inbox/t-1"


def test_send_push_returns_gone_for_404():
    """The 404 case mirrors 410; covered separately because some gateways
    use 404 + ``Gone`` rather than the cleaner 410."""

    response = _fake_response(404)
    exc = WebPushException("Not Found", response=response)

    with patch("pywebpush.webpush", side_effect=exc):
        from autosdr.push import send_push

        result: PushSendResult = send_push(
            subscription_info={
                "endpoint": "https://push.example.test/abc",
                "keys": {"p256dh": "p", "auth": "a"},
            },
            payload={"title": "x"},
            vapid_private="ignored",
            vapid_subject="mailto:x@y.test",
        )

    assert result.gone is True
    assert result.ok is False
