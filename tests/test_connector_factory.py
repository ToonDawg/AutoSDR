"""Connector factory behaviour.

The factory is driven by ``workspace.settings`` now — env vars are
infra-only — so these tests poke settings blobs at ``build_connector``
directly. ``get_connector()`` / ``rebuild_connector()`` still read the
workspace row, which the end-to-end tests cover.
"""

from __future__ import annotations

import pytest

from autosdr.config import default_workspace_settings
from autosdr.connectors import (
    build_connector,
    get_connector,
    rebuild_connector,
    reset_connector,
)
from autosdr.connectors.base import ConnectorError
from autosdr.connectors.file_connector import FileConnector
from autosdr.connectors.override import OverrideConnector
from autosdr.connectors.smsgate import SmsGateConnector
from autosdr.connectors.textbee import TextBeeConnector


def _settings(**connector_overrides) -> dict:
    """Return a fresh default settings blob with connector block merged."""

    blob = default_workspace_settings()
    if connector_overrides:
        blob["connector"].update(connector_overrides)
    return blob


def test_file_is_the_default():
    settings = default_workspace_settings()
    assert isinstance(build_connector(settings), FileConnector)


def test_textbee_requires_credentials():
    settings = _settings(type="textbee")
    # Default credentials in the blob are all None.
    with pytest.raises(Exception):
        build_connector(settings)


def test_textbee_with_credentials():
    settings = _settings(type="textbee")
    settings["connector"]["textbee"].update(
        {"api_key": "k", "device_id": "d"}
    )
    assert isinstance(build_connector(settings), TextBeeConnector)


def test_smsgate_requires_credentials():
    settings = _settings(type="smsgate")
    with pytest.raises(Exception):
        build_connector(settings)


def test_smsgate_with_credentials():
    settings = _settings(type="smsgate")
    settings["connector"]["smsgate"].update(
        {"api_url": "http://x", "username": "ops", "password": "s3cret"}
    )
    connector = build_connector(settings)
    assert isinstance(connector, SmsGateConnector)
    assert connector.username == "ops"


def test_legacy_dry_run_flag_is_ignored():
    """The old ``rehearsal.dry_run`` toggle is gone — extra keys must
    pass through ``build_connector`` without effect.

    Older workspaces still have ``"dry_run": true`` baked into their
    settings JSON; we want those to keep using whatever connector is
    configured (file / textbee / smsgate) until the operator picks
    again. The deep-merge defaults strip the key on read, but this
    test keeps us honest if someone re-introduces ``dry_run`` handling
    by accident.
    """

    settings = _settings(type="textbee")
    settings["connector"]["textbee"].update(
        {"api_key": "k", "device_id": "d"}
    )
    settings["rehearsal"]["dry_run"] = True
    connector = build_connector(settings)
    assert isinstance(connector, TextBeeConnector)


def test_override_wraps_file_connector():
    settings = default_workspace_settings()
    settings["rehearsal"]["override_to"] = "+61400000099"
    connector = build_connector(settings)
    assert isinstance(connector, OverrideConnector)
    assert isinstance(connector.inner, FileConnector)
    assert connector.override_to == "+61400000099"


def test_override_wraps_textbee_connector():
    settings = _settings(type="textbee")
    settings["connector"]["textbee"].update(
        {"api_key": "k", "device_id": "d"}
    )
    settings["rehearsal"]["override_to"] = "+61400000099"
    connector = build_connector(settings)
    assert isinstance(connector, OverrideConnector)
    assert isinstance(connector.inner, TextBeeConnector)


def test_get_connector_requires_workspace(fresh_db):
    """Without a workspace row the cached factory raises ConnectorError."""

    reset_connector()
    with pytest.raises(ConnectorError):
        get_connector()


def test_rebuild_connector_hot_swaps(workspace_factory):
    """PATCH /api/workspace/settings ultimately calls rebuild_connector."""

    workspace_factory()
    reset_connector()
    first = get_connector()
    assert isinstance(first, FileConnector)

    # Now swap to textbee via a fresh settings blob.
    new_settings = default_workspace_settings()
    new_settings["connector"]["type"] = "textbee"
    new_settings["connector"]["textbee"].update(
        {"api_key": "k", "device_id": "d"}
    )
    swapped = rebuild_connector(new_settings)
    assert isinstance(swapped, TextBeeConnector)
    # Subsequent get_connector() returns the swapped instance.
    assert get_connector() is swapped
