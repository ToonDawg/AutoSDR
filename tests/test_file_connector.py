"""FileConnector smoke tests."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from autosdr import killswitch
from autosdr.connectors.base import OutgoingMessage
from autosdr.connectors.file_connector import FileConnector


async def test_send_appends_to_outbox(tmp_path: Path):
    outbox = tmp_path / "outbox.jsonl"
    connector = FileConnector(outbox_path=outbox)
    result = await connector.send(
        OutgoingMessage(contact_uri="+61400000000", content="hello")
    )
    assert result.success
    assert result.provider_message_id

    lines = outbox.read_text().strip().splitlines()
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert record["contact_uri"] == "+61400000000"
    assert record["content"] == "hello"
    assert record["id"] == result.provider_message_id


async def test_send_respects_kill_switch(tmp_path: Path):
    outbox = tmp_path / "outbox.jsonl"
    connector = FileConnector(outbox_path=outbox)
    killswitch.touch_flag()
    with pytest.raises(killswitch.KillSwitchTripped):
        await connector.send(
            OutgoingMessage(contact_uri="+61400000000", content="hello")
        )
    assert not outbox.exists() or outbox.read_text() == ""


def test_parse_webhook_happy(tmp_path: Path):
    connector = FileConnector(outbox_path=tmp_path / "outbox.jsonl")
    incoming = connector.parse_webhook({"contact_uri": "+61400000000", "content": "hi"})
    assert incoming.contact_uri == "+61400000000"
    assert incoming.content == "hi"


def test_parse_webhook_accepts_alt_keys(tmp_path: Path):
    connector = FileConnector(outbox_path=tmp_path / "outbox.jsonl")
    incoming = connector.parse_webhook({"from": "+61400000000", "text": "hi"})
    assert incoming.contact_uri == "+61400000000"
    assert incoming.content == "hi"


def test_parse_webhook_requires_both_fields(tmp_path: Path):
    connector = FileConnector(outbox_path=tmp_path / "outbox.jsonl")
    with pytest.raises(ValueError):
        connector.parse_webhook({"contact_uri": "+61400000000"})
