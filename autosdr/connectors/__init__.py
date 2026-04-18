"""Connector abstraction + registry."""

from __future__ import annotations

import logging

from autosdr.config import Settings, get_settings
from autosdr.connectors.base import (
    BaseConnector,
    ConnectorError,
    IncomingMessage,
    OutgoingMessage,
)
from autosdr.connectors.file_connector import FileConnector
from autosdr.connectors.override import OverrideConnector
from autosdr.connectors.smsgate import SmsGateConnector
from autosdr.connectors.textbee import TextBeeConnector

logger = logging.getLogger(__name__)

__all__ = [
    "BaseConnector",
    "ConnectorError",
    "FileConnector",
    "IncomingMessage",
    "OutgoingMessage",
    "OverrideConnector",
    "SmsGateConnector",
    "TextBeeConnector",
    "get_connector",
]


def get_connector(settings: Settings | None = None) -> BaseConnector:
    """Return the configured connector instance.

    Composition, in order:

    1. Base connector is selected by ``CONNECTOR`` (file | textbee | smsgate).
    2. ``DRY_RUN=true`` short-circuits to :class:`FileConnector` regardless of
       ``CONNECTOR`` — nothing hits the wire, but the LLM still runs.
    3. ``SMS_OVERRIDE_TO`` wraps the result in :class:`OverrideConnector` so
       every outbound is rerouted to that number. Composable with dry-run:
       the outbox then records the override number instead of real leads.

    The scheduler + webhook handler both call this once at process start and
    share the result — important for the TextBee connector's in-memory
    "seen ids" dedup set and the override wrapper's inbound remapping.
    """

    settings = settings or get_settings()

    if settings.dry_run:
        logger.warning(
            "DRY RUN mode active: using FileConnector (CONNECTOR=%s ignored). "
            "Outbound will be appended to %s — no real SMS will be sent.",
            settings.connector,
            settings.outbox_path,
        )
        inner: BaseConnector = FileConnector(outbox_path=settings.outbox_path)
    elif settings.connector == "file":
        inner = FileConnector(outbox_path=settings.outbox_path)
    elif settings.connector == "textbee":
        inner = TextBeeConnector(
            api_url=settings.textbee_api_url,
            api_key=settings.textbee_api_key or "",
            device_id=settings.textbee_device_id or "",
            poll_limit=settings.textbee_poll_limit,
        )
    elif settings.connector == "smsgate":
        inner = SmsGateConnector(
            api_url=settings.smsgate_api_url,
            username=settings.smsgate_username or "",
            password=settings.smsgate_password or "",
        )
    else:
        raise ConnectorError(f"unknown connector {settings.connector!r}")

    if settings.sms_override_to:
        logger.warning(
            "OVERRIDE mode active: every outbound SMS will be redirected to %s",
            settings.sms_override_to,
        )
        return OverrideConnector(inner, settings.sms_override_to)

    return inner
