"""Connector ABC.

Intentionally minimal — per Doc 3 §6.1. A second connector needs only to
implement these three methods; the generation pipeline and thread model do
not change.

Connectors expose two inbound paths:

* ``parse_webhook`` — normalise a provider webhook POST into ``IncomingMessage``.
  Used when the provider pushes events to a public endpoint.
* ``poll_incoming`` — pull any unread inbound messages from the provider's
  REST API. Used by polling connectors (TextBee's default POC path) so the
  scheduler can drain new replies each tick without needing a public URL.

Providers that only support one style override just that method; the base class
implements ``poll_incoming`` as a no-op so webhook-only connectors remain valid.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any


class ConnectorError(RuntimeError):
    """Unrecoverable connector failure (bad config, persistent 5xx, etc.)."""


@dataclass
class OutgoingMessage:
    contact_uri: str
    content: str


@dataclass
class IncomingMessage:
    contact_uri: str
    content: str
    received_at: datetime = field(default_factory=lambda: datetime.now(tz=timezone.utc))
    raw_payload: dict[str, Any] = field(default_factory=dict)
    provider_message_id: str | None = None


@dataclass
class SendResult:
    success: bool
    provider_message_id: str | None = None
    error: str | None = None


class BaseConnector(ABC):
    """The abstraction separating AutoSDR's messaging logic from the channel."""

    connector_type: str = "base"

    @abstractmethod
    async def send(self, message: OutgoingMessage) -> SendResult:
        """Deliver ``message``. Raises :class:`ConnectorError` on unrecoverable failure."""

    @abstractmethod
    def parse_webhook(self, payload: dict[str, Any]) -> IncomingMessage:
        """Normalise a raw provider payload into an :class:`IncomingMessage`."""

    @abstractmethod
    async def validate_config(self) -> tuple[bool, str]:
        """Check the connector is usable. Returns ``(ok, detail)``."""

    async def poll_incoming(self) -> list[IncomingMessage]:
        """Pull unread inbound messages from the provider.

        Default is a no-op — webhook-only providers do not override. Polling
        connectors (e.g. TextBee) override to fetch new messages since the
        last poll; they are responsible for their own deduplication so the
        scheduler can call this freely every tick.
        """

        return []
