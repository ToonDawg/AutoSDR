"""`get_connector` factory — dry-run override + SMS_OVERRIDE_TO wrapping + smsgate wire-up."""

from __future__ import annotations

import pytest

from autosdr import config as config_module
from autosdr.connectors import get_connector
from autosdr.connectors.base import ConnectorError
from autosdr.connectors.file_connector import FileConnector
from autosdr.connectors.override import OverrideConnector
from autosdr.connectors.smsgate import SmsGateConnector
from autosdr.connectors.textbee import TextBeeConnector


@pytest.fixture(autouse=True)
def _scrub_connector_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Clear anything the host shell may have in the env that would leak in.

    ``conftest`` pins ``CONNECTOR=file`` and ``GEMINI_API_KEY`` but not the
    provider-specific keys — if the dev shell happens to have
    ``SMSGATE_USERNAME`` set (e.g. from a smoke test), the "unconfigured"
    tests below would spuriously pass construction.
    """

    for key in (
        "TEXTBEE_API_KEY",
        "TEXTBEE_DEVICE_ID",
        "SMSGATE_USERNAME",
        "SMSGATE_PASSWORD",
        "SMS_OVERRIDE_TO",
        "DRY_RUN",
    ):
        monkeypatch.delenv(key, raising=False)


def test_file_is_the_default(monkeypatch: pytest.MonkeyPatch):
    # conftest sets CONNECTOR=file already, just rebuild.
    config_module.reset_settings_for_tests()
    assert isinstance(get_connector(), FileConnector)


def test_textbee_requires_credentials(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("CONNECTOR", "textbee")
    monkeypatch.delenv("TEXTBEE_API_KEY", raising=False)
    monkeypatch.delenv("TEXTBEE_DEVICE_ID", raising=False)
    config_module.reset_settings_for_tests()
    with pytest.raises(ConnectorError):
        get_connector()


def test_textbee_with_credentials(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("CONNECTOR", "textbee")
    monkeypatch.setenv("TEXTBEE_API_KEY", "k")
    monkeypatch.setenv("TEXTBEE_DEVICE_ID", "d")
    config_module.reset_settings_for_tests()
    assert isinstance(get_connector(), TextBeeConnector)


def test_smsgate_requires_credentials(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("CONNECTOR", "smsgate")
    monkeypatch.delenv("SMSGATE_USERNAME", raising=False)
    monkeypatch.delenv("SMSGATE_PASSWORD", raising=False)
    config_module.reset_settings_for_tests()
    with pytest.raises(ConnectorError):
        get_connector()


def test_smsgate_with_credentials(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("CONNECTOR", "smsgate")
    monkeypatch.setenv("SMSGATE_USERNAME", "ops")
    monkeypatch.setenv("SMSGATE_PASSWORD", "s3cret")
    config_module.reset_settings_for_tests()
    connector = get_connector()
    assert isinstance(connector, SmsGateConnector)
    assert connector.username == "ops"


def test_dry_run_overrides_connector_choice(monkeypatch: pytest.MonkeyPatch):
    # Even with textbee fully configured, DRY_RUN short-circuits to file.
    monkeypatch.setenv("CONNECTOR", "textbee")
    monkeypatch.setenv("TEXTBEE_API_KEY", "k")
    monkeypatch.setenv("TEXTBEE_DEVICE_ID", "d")
    monkeypatch.setenv("DRY_RUN", "true")
    config_module.reset_settings_for_tests()

    connector = get_connector()
    assert isinstance(connector, FileConnector)


def test_dry_run_bypasses_missing_smsgate_creds(monkeypatch: pytest.MonkeyPatch):
    # Unconfigured smsgate would raise, but DRY_RUN skips straight to file.
    monkeypatch.setenv("CONNECTOR", "smsgate")
    monkeypatch.setenv("DRY_RUN", "true")
    config_module.reset_settings_for_tests()

    connector = get_connector()
    assert isinstance(connector, FileConnector)


def test_override_wraps_file_connector(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("SMS_OVERRIDE_TO", "+61400000099")
    config_module.reset_settings_for_tests()

    connector = get_connector()
    assert isinstance(connector, OverrideConnector)
    assert isinstance(connector.inner, FileConnector)
    assert connector.override_to == "+61400000099"


def test_override_wraps_textbee_connector(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("CONNECTOR", "textbee")
    monkeypatch.setenv("TEXTBEE_API_KEY", "k")
    monkeypatch.setenv("TEXTBEE_DEVICE_ID", "d")
    monkeypatch.setenv("SMS_OVERRIDE_TO", "+61400000099")
    config_module.reset_settings_for_tests()

    connector = get_connector()
    assert isinstance(connector, OverrideConnector)
    assert isinstance(connector.inner, TextBeeConnector)


def test_dry_run_and_override_compose(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("CONNECTOR", "textbee")
    monkeypatch.setenv("DRY_RUN", "true")
    monkeypatch.setenv("SMS_OVERRIDE_TO", "+61400000099")
    config_module.reset_settings_for_tests()

    connector = get_connector()
    assert isinstance(connector, OverrideConnector)
    # Inner is FileConnector because dry-run wins over CONNECTOR.
    assert isinstance(connector.inner, FileConnector)
