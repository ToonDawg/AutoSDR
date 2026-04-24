"""Connector abstraction + registry.

The active connector is assembled from ``workspace.settings`` — the DB-backed
source of truth — at boot time and cached in a module-level singleton. PATCHes
to ``/api/workspace/settings`` call :func:`rebuild_connector` so that toggling
CONNECTOR, flipping dry-run, or pasting a new API key takes effect without a
server restart.
"""

from __future__ import annotations

import logging
import threading
from typing import Any

from autosdr.config import get_settings
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
from autosdr.workspace_settings import load_workspace_settings_optional

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
    "build_connector",
    "get_connector",
    "rebuild_connector",
    "reset_connector",
]


# ---------------------------------------------------------------------------
# Cached active connector
# ---------------------------------------------------------------------------

_connector: BaseConnector | None = None
_lock = threading.Lock()


def build_connector(workspace_settings: dict[str, Any]) -> BaseConnector:
    """Assemble a connector from the workspace settings blob.

    Composition, in order:

    1. Base connector selected by ``connector.type`` (file | textbee | smsgate).
    2. ``rehearsal.dry_run=true`` forces :class:`FileConnector` — nothing hits
       the wire, every outbound is appended to ``settings.outbox_path``. The
       LLM still runs normally so eval / logs remain useful.
    3. ``rehearsal.override_to`` wraps the result in :class:`OverrideConnector`
       so every outbound is redirected to that number. Composable with dry-run.
    """

    infra = get_settings()
    connector_cfg = (workspace_settings or {}).get("connector") or {}
    rehearsal = (workspace_settings or {}).get("rehearsal") or {}
    ctype = str(connector_cfg.get("type") or "file").lower()

    if rehearsal.get("dry_run"):
        logger.warning(
            "dry-run mode active: forcing FileConnector (configured=%s)",
            ctype,
        )
        inner: BaseConnector = FileConnector(outbox_path=infra.outbox_path)
    elif ctype == "file":
        inner = FileConnector(outbox_path=infra.outbox_path)
    elif ctype == "textbee":
        tb = connector_cfg.get("textbee") or {}
        inner = TextBeeConnector(
            api_url=str(tb.get("api_url") or "https://api.textbee.dev"),
            api_key=str(tb.get("api_key") or ""),
            device_id=str(tb.get("device_id") or ""),
            poll_limit=int(tb.get("poll_limit") or 50),
        )
    elif ctype == "smsgate":
        sg = connector_cfg.get("smsgate") or {}
        inner = SmsGateConnector(
            api_url=str(sg.get("api_url") or ""),
            username=str(sg.get("username") or ""),
            password=str(sg.get("password") or ""),
        )
    else:
        raise ConnectorError(f"unknown connector type {ctype!r}")

    override_to = rehearsal.get("override_to")
    if override_to:
        override_to = str(override_to).strip()
    if override_to:
        logger.warning(
            "override mode active: redirecting every outbound to %s",
            override_to,
        )
        return OverrideConnector(inner, override_to)

    return inner


def _load_workspace_settings_or_error() -> dict[str, Any]:
    """Read the single workspace's settings blob; raise if no workspace yet."""

    settings = load_workspace_settings_optional()
    if settings is None:
        raise ConnectorError(
            "workspace has not been set up yet — complete the setup wizard first"
        )
    return settings


def get_connector() -> BaseConnector:
    """Return the cached connector singleton (building it on first access)."""

    global _connector
    if _connector is not None:
        return _connector
    with _lock:
        if _connector is None:
            _connector = build_connector(_load_workspace_settings_or_error())
    return _connector


def rebuild_connector(workspace_settings: dict[str, Any] | None = None) -> BaseConnector:
    """Replace the cached connector with a fresh one.

    Called by PATCH /api/workspace/settings so connector changes (swapping
    TextBee for SmsGate, flipping dry-run, rotating credentials) take effect
    immediately without restarting the process.
    """

    global _connector
    settings = (
        workspace_settings
        if workspace_settings is not None
        else _load_workspace_settings_or_error()
    )
    with _lock:
        _connector = build_connector(settings)
    return _connector


def reset_connector() -> None:
    """Clear the cached connector (tests, teardown)."""

    global _connector
    with _lock:
        _connector = None
