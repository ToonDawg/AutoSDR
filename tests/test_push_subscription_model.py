"""Schema-level coverage for the ``push_subscription`` table introduced in
ticket 0005.

These tests intentionally pin the *table* shape — column types, the
unique-on-endpoint constraint, the workspace-scoped index — without
exercising any of the API/transport layers (those land in their own
suites in later units of the ticket plan).
"""

from __future__ import annotations

import pytest
from sqlalchemy import inspect
from sqlalchemy.exc import IntegrityError

from autosdr.db import get_engine
from autosdr.models import PushSubscription


def test_push_subscription_table_columns(fresh_db, workspace_factory):
    """Every column on the model is reachable via the inspector."""

    workspace_factory()
    inspector = inspect(get_engine())
    columns = {col["name"]: col for col in inspector.get_columns("push_subscription")}

    expected = {
        "id",
        "workspace_id",
        "endpoint",
        "p256dh",
        "auth",
        "user_agent",
        "dashboard_origin",
        "created_at",
        "last_seen_at",
        "last_error",
    }
    assert expected.issubset(columns.keys())


def test_push_subscription_endpoint_is_unique(fresh_db, workspace_factory):
    """Two rows with the same endpoint cannot coexist (upsert key)."""

    workspace_id = workspace_factory()

    with fresh_db() as session:
        session.add(
            PushSubscription(
                workspace_id=workspace_id,
                endpoint="https://push.example.test/abc",
                p256dh="p1",
                auth="a1",
                user_agent="iPhone Safari",
            )
        )

    with pytest.raises(IntegrityError):
        with fresh_db() as session:
            session.add(
                PushSubscription(
                    workspace_id=workspace_id,
                    endpoint="https://push.example.test/abc",
                    p256dh="p2",
                    auth="a2",
                    user_agent="Pixel Chrome",
                )
            )


def test_push_subscription_workspace_index_exists(fresh_db, workspace_factory):
    """Workspace+created_at index is present so listing-per-workspace is
    a single B-tree scan rather than a full table sweep."""

    workspace_factory()
    inspector = inspect(get_engine())
    indexes = {idx["name"] for idx in inspector.get_indexes("push_subscription")}
    assert "idx_push_subscription_workspace" in indexes
