"""OverrideConnector — redirect outbound, remap inbound, compose with FileConnector."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from autosdr.connectors.base import (
    BaseConnector,
    IncomingMessage,
    OutgoingMessage,
    SendResult,
)
from autosdr.connectors.file_connector import FileConnector
from autosdr.connectors.override import OverrideConnector


class _Recorder(BaseConnector):
    """Minimal BaseConnector that remembers sends + yields queued inbound."""

    connector_type = "recorder"

    def __init__(self) -> None:
        self.sent: list[OutgoingMessage] = []
        self.queued_inbound: list[IncomingMessage] = []
        self.validate_result: tuple[bool, str] = (True, "ok")

    async def send(self, message: OutgoingMessage) -> SendResult:
        self.sent.append(message)
        return SendResult(success=True, provider_message_id=f"id-{len(self.sent)}")

    def parse_webhook(self, payload):
        sender = payload.get("sender") or payload.get("from", "")
        content = payload.get("message") or payload.get("text", "")
        if not sender or not content:
            raise ValueError("bad payload")
        return IncomingMessage(contact_uri=str(sender), content=str(content))

    async def validate_config(self):
        return self.validate_result

    async def poll_incoming(self):
        out = list(self.queued_inbound)
        self.queued_inbound = []
        return out


def test_empty_override_rejected():
    with pytest.raises(ValueError):
        OverrideConnector(_Recorder(), "")


async def test_outbound_redirected_to_override_number():
    inner = _Recorder()
    override = OverrideConnector(inner, "+61400000099")

    result = await override.send(
        OutgoingMessage(contact_uri="+61400000001", content="hi")
    )
    assert result.success
    assert len(inner.sent) == 1
    assert inner.sent[0].contact_uri == "+61400000099"
    assert inner.sent[0].content == "hi"


async def test_outbound_already_at_override_is_not_rewritten():
    inner = _Recorder()
    override = OverrideConnector(inner, "+61400000099")

    await override.send(
        OutgoingMessage(contact_uri="+61400000099", content="self-test")
    )
    assert inner.sent[0].contact_uri == "+61400000099"


async def test_inbound_from_override_is_remapped_to_last_real_target():
    inner = _Recorder()
    override = OverrideConnector(inner, "+61400000099")

    # Seed the mapping by sending to a real lead — the override stores that
    # original target so inbound from +61400000099 can be remapped.
    await override.send(
        OutgoingMessage(contact_uri="+61400000001", content="outreach")
    )

    inner.queued_inbound = [
        IncomingMessage(contact_uri="+61400000099", content="reply from me")
    ]
    incoming = await override.poll_incoming()

    assert len(incoming) == 1
    assert incoming[0].contact_uri == "+61400000001"
    assert incoming[0].content == "reply from me"


async def test_inbound_from_other_number_is_untouched():
    inner = _Recorder()
    override = OverrideConnector(inner, "+61400000099")
    await override.send(
        OutgoingMessage(contact_uri="+61400000001", content="hi")
    )

    inner.queued_inbound = [
        IncomingMessage(contact_uri="+61400000002", content="unrelated")
    ]
    incoming = await override.poll_incoming()
    assert incoming[0].contact_uri == "+61400000002"


def test_parse_webhook_remaps_after_send():
    import asyncio

    inner = _Recorder()
    override = OverrideConnector(inner, "+61400000099")

    asyncio.run(
        override.send(OutgoingMessage(contact_uri="+61400000001", content="x"))
    )
    incoming = override.parse_webhook(
        {"sender": "+61400000099", "message": "pushed reply"}
    )
    assert incoming.contact_uri == "+61400000001"


def test_parse_webhook_before_any_send_is_passthrough():
    inner = _Recorder()
    override = OverrideConnector(inner, "+61400000099")
    incoming = override.parse_webhook(
        {"sender": "+61400000099", "message": "unsolicited"}
    )
    # No last_original yet, so no rewrite.
    assert incoming.contact_uri == "+61400000099"


async def test_validate_config_annotates_override():
    inner = _Recorder()
    inner.validate_result = (True, "recorder ready")
    override = OverrideConnector(inner, "+61400000099")
    ok, detail = await override.validate_config()
    assert ok
    assert "+61400000099" in detail
    assert "OVERRIDE" in detail


async def test_connector_type_annotates_wrapper():
    override = OverrideConnector(_Recorder(), "+61400000099")
    assert override.connector_type == "recorder+override"


async def test_compose_with_file_connector_writes_override_in_outbox(tmp_path: Path):
    outbox = tmp_path / "outbox.jsonl"
    wrapped = OverrideConnector(FileConnector(outbox_path=outbox), "+61400000099")

    result = await wrapped.send(
        OutgoingMessage(contact_uri="+61400000001", content="rehearsal")
    )
    assert result.success

    record = json.loads(outbox.read_text().strip().splitlines()[-1])
    assert record["contact_uri"] == "+61400000099"
    assert record["content"] == "rehearsal"
