"""Scheduler — inbound polling path.

Verifies `_poll_inbound_once` pulls messages from the connector and dispatches
each one through the reply pipeline. Uses a stub connector so we can script
the poll responses without talking to a real gateway.
"""

from __future__ import annotations

import pytest

from autosdr.connectors.base import (
    BaseConnector,
    IncomingMessage,
    OutgoingMessage,
    SendResult,
)
from autosdr.models import Workspace
from autosdr.scheduler import _poll_inbound_once


class _StubConnector(BaseConnector):
    connector_type = "stub"

    def __init__(self, pending: list[IncomingMessage]):
        self.pending = list(pending)
        self.poll_calls = 0

    async def send(self, message: OutgoingMessage) -> SendResult:
        raise AssertionError("send should not be called during inbound poll")

    def parse_webhook(self, payload):
        raise NotImplementedError

    async def validate_config(self) -> tuple[bool, str]:
        return True, "stub ok"

    async def poll_incoming(self) -> list[IncomingMessage]:
        self.poll_calls += 1
        out = list(self.pending)
        self.pending.clear()
        return out


async def test_poll_inbound_noop_when_workspace_missing(fresh_db, monkeypatch):
    fresh_db()
    connector = _StubConnector(
        [IncomingMessage(contact_uri="+61400000001", content="hi")]
    )
    dispatched = await _poll_inbound_once(connector)
    # Stub reports one pending but scheduler drops it because no workspace exists.
    assert dispatched == 0


async def test_poll_inbound_dispatches_through_reply_pipeline(
    fresh_db, workspace_factory, monkeypatch
):
    fresh_db()
    ws_id = workspace_factory()

    # Verify the workspace was created so the scheduler finds one.
    with fresh_db() as session:
        assert session.query(Workspace).count() == 1

    seen: list[dict] = []

    async def _fake_process(*, connector, workspace_id, incoming):
        seen.append(
            {
                "workspace_id": workspace_id,
                "contact_uri": incoming.contact_uri,
                "content": incoming.content,
                "provider_message_id": incoming.provider_message_id,
            }
        )

        class _Result:
            action = "ignored"
            detail = "test"

        return _Result()

    monkeypatch.setattr("autosdr.scheduler.process_incoming_message", _fake_process)

    connector = _StubConnector(
        [
            IncomingMessage(
                contact_uri="+61400000001",
                content="hi",
                provider_message_id="msg-1",
            ),
            IncomingMessage(
                contact_uri="+61400000002",
                content="yo",
                provider_message_id="msg-2",
            ),
        ]
    )
    dispatched = await _poll_inbound_once(connector)

    assert dispatched == 2
    assert connector.poll_calls == 1
    assert [row["provider_message_id"] for row in seen] == ["msg-1", "msg-2"]
    assert all(row["workspace_id"] == ws_id for row in seen)


async def test_poll_inbound_swallows_pipeline_errors(
    fresh_db, workspace_factory, monkeypatch
):
    fresh_db()
    workspace_factory()

    async def _boom(*, connector, workspace_id, incoming):
        raise RuntimeError("pipeline exploded")

    monkeypatch.setattr("autosdr.scheduler.process_incoming_message", _boom)

    connector = _StubConnector(
        [IncomingMessage(contact_uri="+61400000001", content="hi")]
    )
    # Should not raise; crashes are logged and polling stays alive.
    dispatched = await _poll_inbound_once(connector)
    assert dispatched == 0


async def test_poll_inbound_returns_zero_when_connector_raises(
    fresh_db, workspace_factory
):
    fresh_db()
    workspace_factory()

    class _CrashConnector(_StubConnector):
        async def poll_incoming(self):
            raise RuntimeError("network boom")

    connector = _CrashConnector([])
    assert await _poll_inbound_once(connector) == 0
