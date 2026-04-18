"""Shared pytest fixtures.

Each test gets its own SQLite file + a pristine singleton cache so the
``get_settings`` / ``get_engine`` / killswitch modules don't leak state across
cases.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from autosdr import config as config_module
from autosdr import db as db_module
from autosdr import killswitch as killswitch_module
from autosdr.llm import client as llm_client


@pytest.fixture(autouse=True)
def _isolate_settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Point every path at a fresh tmp dir per test."""

    db_path = tmp_path / "autosdr.db"
    outbox_path = tmp_path / "outbox.jsonl"
    pause_flag = tmp_path / ".autosdr-pause"
    pid_path = tmp_path / "autosdr.pid"
    log_dir = tmp_path / "logs"

    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_path}")
    monkeypatch.setenv("OUTBOX_PATH", str(outbox_path))
    monkeypatch.setenv("PAUSE_FLAG_PATH", str(pause_flag))
    monkeypatch.setenv("PID_FILE_PATH", str(pid_path))
    monkeypatch.setenv("LOG_DIR", str(log_dir))
    monkeypatch.setenv("CONNECTOR", "file")
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")

    config_module.reset_settings_for_tests()
    db_module.reset_for_tests()
    killswitch_module.reset_for_tests()
    llm_client.reset_usage()
    yield
    db_module.reset_for_tests()
    config_module.reset_settings_for_tests()
    killswitch_module.reset_for_tests()
    llm_client.reset_usage()


@pytest.fixture
def fresh_db():
    """Create all tables for a test. Returns a session-scope helper."""

    db_module.create_all()
    return db_module.session_scope


@pytest.fixture
def workspace_factory(fresh_db):
    """Create a default workspace for tests that need one."""

    from autosdr.config import default_workspace_settings, get_settings
    from autosdr.models import Workspace

    def _make(
        business_dump: str = "We run a staffing platform for aged care.",
        tone: str = "Casual, direct, one idea per sentence.",
        default_region: str = "AU",
    ) -> str:
        ws_settings = default_workspace_settings(get_settings())
        ws_settings["default_region"] = default_region
        with fresh_db() as session:
            ws = Workspace(
                business_name="Test Biz",
                business_dump=business_dump,
                tone_prompt=tone,
                settings=ws_settings,
            )
            session.add(ws)
            session.flush()
            return ws.id

    return _make
