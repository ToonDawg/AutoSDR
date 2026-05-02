"""VAPID keypair lifecycle for ticket 0005 unit 2.

The contract:

* :func:`ensure_vapid_keys` is a no-op when the workspace already has
  a keypair (idempotent across reboots).
* On a fresh workspace it generates a P-256 keypair, writes both
  halves to ``workspace.settings.push``, and returns them.
* Returns all-``None`` when no workspace exists yet (lifespan can call
  it before the setup wizard has run without crashing).
* Generated public/private values round-trip the b64url-no-pad
  encoding the Web Push spec requires (65-byte uncompressed point;
  32-byte private scalar).
"""

from __future__ import annotations

import base64

from autosdr.db import session_scope
from autosdr.models import Workspace
from autosdr.push import ensure_vapid_keys


def _b64url_decode(value: str) -> bytes:
    pad = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode((value + pad).encode("ascii"))


def test_ensure_vapid_keys_returns_none_without_workspace(fresh_db):
    keys = ensure_vapid_keys()
    assert keys == {"public": None, "private": None, "subject": None}


def test_ensure_vapid_keys_generates_on_first_call(fresh_db, workspace_factory):
    workspace_factory()
    keys = ensure_vapid_keys()

    assert keys["public"] and keys["private"]
    public = _b64url_decode(keys["public"])
    private = _b64url_decode(keys["private"])
    assert len(public) == 65, "public key must be 65 raw bytes (uncompressed P-256 point)"
    assert public[0] == 0x04, "public key must start with the X9.62 uncompressed prefix"
    assert len(private) == 32, "private scalar must be 32 raw bytes"

    with session_scope() as session:
        workspace = session.query(Workspace).first()
        push = workspace.settings["push"]
        assert push["vapid_public"] == keys["public"]
        assert push["vapid_private"] == keys["private"]
        assert push["vapid_subject"].startswith("mailto:")


def test_ensure_vapid_keys_is_idempotent(fresh_db, workspace_factory):
    workspace_factory()
    first = ensure_vapid_keys()
    second = ensure_vapid_keys()
    assert first == second


def test_ensure_vapid_keys_preserves_other_push_settings(
    fresh_db, workspace_factory
):
    """Operator-set push settings (e.g. dashboard_origin) survive keygen."""

    workspace_factory(
        settings_overrides={
            "push": {
                "vapid_public": None,
                "vapid_private": None,
                "vapid_subject": "mailto:operator@example.test",
                "hitl_escalations": True,
                "dashboard_origin": "https://autosdr.tail-scale.ts.net",
            }
        }
    )
    keys = ensure_vapid_keys()
    assert keys["subject"] == "mailto:operator@example.test"

    with session_scope() as session:
        workspace = session.query(Workspace).first()
        push = workspace.settings["push"]
        assert push["dashboard_origin"] == "https://autosdr.tail-scale.ts.net"
        assert push["hitl_escalations"] is True
