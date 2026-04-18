"""Connector wrapper that redirects outbound to a single override number.

Use when you want to exercise the real SMS path (TextBee, SmsGate, ...) but
route every message to a phone you own — the agent still generates unique
content per lead, but only your own device receives it. Handy as a POC
dress-rehearsal before pointing the agent at real leads.

Mechanics:

* ``send()`` rewrites :attr:`OutgoingMessage.contact_uri` to the override
  number. The original target is stashed so inbound remapping can find it.
* ``parse_webhook`` / ``poll_incoming`` look at the incoming sender; if it
  matches the override number, the sender is rewritten back to the most recent
  real target so the reply pipeline routes to the right thread. This is
  intentionally a single-slot mapping — override mode is a one-lead rehearsal,
  not a many-lead fan-out.
* ``validate_config`` passes through to the inner connector.

This is a pure wrapper — no state other than ``_last_original`` leaks out, so
it composes cleanly on top of any :class:`BaseConnector` implementation.
"""

from __future__ import annotations

import logging
from dataclasses import replace
from typing import Any

from autosdr.connectors.base import (
    BaseConnector,
    IncomingMessage,
    OutgoingMessage,
    SendResult,
)

logger = logging.getLogger(__name__)


class OverrideConnector(BaseConnector):
    """Wraps another connector and redirects all outbound to ``override_to``."""

    def __init__(self, inner: BaseConnector, override_to: str) -> None:
        override_to = (override_to or "").strip()
        if not override_to:
            raise ValueError("override_to must be a non-empty phone number")
        self._inner = inner
        self._override_to = override_to
        self.connector_type = f"{inner.connector_type}+override"
        self._last_original: str | None = None

    @property
    def inner(self) -> BaseConnector:
        return self._inner

    @property
    def override_to(self) -> str:
        return self._override_to

    # ----- send ------------------------------------------------------------

    async def send(self, message: OutgoingMessage) -> SendResult:
        original = message.contact_uri
        if original == self._override_to:
            # Already targeting the override number (e.g. the owner's own
            # self-test) — nothing to rewrite.
            return await self._inner.send(message)

        logger.warning(
            "override mode: redirecting outbound %s -> %s",
            original,
            self._override_to,
        )
        rewritten = replace(message, contact_uri=self._override_to)
        result = await self._inner.send(rewritten)
        if result.success:
            self._last_original = original
        return result

    # ----- receive ---------------------------------------------------------

    def parse_webhook(self, payload: dict[str, Any]) -> IncomingMessage:
        incoming = self._inner.parse_webhook(payload)
        return self._maybe_rewrite(incoming)

    async def poll_incoming(self) -> list[IncomingMessage]:
        incoming = await self._inner.poll_incoming()
        return [self._maybe_rewrite(m) for m in incoming]

    def _maybe_rewrite(self, message: IncomingMessage) -> IncomingMessage:
        if (
            self._last_original
            and message.contact_uri == self._override_to
        ):
            logger.warning(
                "override mode: rewriting inbound sender %s -> %s",
                message.contact_uri,
                self._last_original,
            )
            message.contact_uri = self._last_original
        return message

    # ----- validate --------------------------------------------------------

    async def validate_config(self) -> tuple[bool, str]:
        ok, detail = await self._inner.validate_config()
        suffix = f"(OVERRIDE ON -> {self._override_to})"
        return ok, f"{detail} {suffix}"
