"""Read the single workspace's settings blob.

The POC is single-tenant: exactly one row in ``workspace``. Several call
sites need that row's ``settings`` blob with different expectations about
whether the row exists yet:

* Scheduler + startup tasks — "give me whatever, ``{}`` if nothing." Ticks
  must be safe during first-boot before the setup wizard has run.
* Webhook lifespan — "tell me if the workspace exists." Returns ``None``
  so the lifespan can skip scheduler/connector construction until setup
  completes. (The lifespan additionally backfills missing default keys;
  that write-path lives in ``webhook.py`` because it also mutates.)
* Connector construction — "fail loudly if no workspace." Callers can't
  function without one, so a clear exception is better than an empty dict.

Keeping the read in one place prevents the three call sites from drifting
on null semantics (they already had: ``{}`` vs ``None`` vs ``raise``).
"""

from __future__ import annotations

from typing import Any

from autosdr.db import session_scope
from autosdr.models import Workspace


def load_workspace_settings_optional() -> dict[str, Any] | None:
    """Return the workspace settings blob, or ``None`` if no workspace exists."""

    with session_scope() as session:
        workspace = session.query(Workspace).first()
        if workspace is None:
            return None
        return dict(workspace.settings or {})


def load_workspace_settings_or_empty() -> dict[str, Any]:
    """Return the workspace settings blob, or ``{}`` if no workspace exists."""

    return load_workspace_settings_optional() or {}


__all__ = [
    "load_workspace_settings_optional",
    "load_workspace_settings_or_empty",
]
