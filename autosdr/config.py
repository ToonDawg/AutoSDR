"""Infrastructure-only settings.

AutoSDR's behaviour-affecting configuration — LLM keys, connector choice,
thresholds, override recipient, scheduler knobs — lives in
``workspace.settings`` (a JSON blob on the workspace row). That is the
single source of truth at runtime and is mutated via the REST API.

Only truly infrastructural values (database url, server host/port, paths for
the kill-switch flag/PID file, log directory, built-frontend location) are
read from the environment. These rarely change and don't belong in the UI.
"""

from __future__ import annotations

import copy
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Infra-only settings. Read once at process start from environment / .env."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # Server
    host: str = "127.0.0.1"
    port: int = 8000

    # Database
    database_url: str = "sqlite:///data/autosdr.db"

    # Kill switch
    pause_flag_path: Path = Path("data/.autosdr-pause")
    pid_file_path: Path = Path("data/autosdr.pid")

    # FileConnector outbox
    outbox_path: Path = Path("data/outbox.jsonl")

    # Logging
    log_dir: Path = Path("data/logs")
    llm_log_enabled: bool = True
    llm_log_max_prompt_chars: int = 16000

    # Built frontend (served from FastAPI at / when present).
    frontend_dist_dir: Path = Path("frontend/dist")


# ---------------------------------------------------------------------------
# workspace.settings — the DB-backed source of truth for everything else.
# These defaults are written the first time a workspace is created (setup
# wizard) and can be edited live via PATCH /api/workspace/settings.
# ---------------------------------------------------------------------------

DEFAULT_WORKSPACE_SETTINGS: dict = {
    # Behaviour
    "auto_reply_enabled": False,  # first-message-only mode by default
    "default_region": "AU",
    "suggestions_count": 3,  # number of reply drafts to offer the human

    # AI loop
    "eval_threshold": 0.85,
    "eval_max_attempts": 3,
    "raw_data_size_limit_kb": 50,

    # Scheduler
    "scheduler_tick_s": 60,
    "min_inter_send_delay_s": 30,
    "max_batch_per_tick": 2,
    "inbound_poll_s": 20,

    # LLM
    "llm": {
        "provider_api_keys": {
            "gemini": None,
            "openai": None,
            "anthropic": None,
        },
        "model_main": "gemini/gemini-3-flash-preview",
        "model_analysis": "gemini/gemini-3-flash-preview",
        "model_eval": "gemini/gemini-3.1-flash-lite-preview",
        "model_classification": "gemini/gemini-3.1-flash-lite-preview",
        "temperature_main": 1.0,
        "temperature_eval": 0.0,
    },

    # Connector
    "connector": {
        "type": "file",
        "textbee": {
            "api_url": "https://api.textbee.dev",
            "api_key": None,
            "device_id": None,
            "poll_limit": 50,
        },
        "smsgate": {
            "api_url": "http://localhost:3000/api/3rdparty/v1",
            "username": None,
            "password": None,
        },
    },

    # Rehearsal: redirect every real-connector send to a single phone you own.
    # Pair with connector.type=file when you just want to write to the outbox
    # without sending anything at all.
    "rehearsal": {
        "override_to": None,
    },
}


def default_workspace_settings() -> dict:
    """Return a fresh deep copy of the default workspace settings blob."""

    return copy.deepcopy(DEFAULT_WORKSPACE_SETTINGS)


def _deep_merge(base: dict, overrides: dict) -> dict:
    """Recursively merge ``overrides`` into ``base`` (mutating ``base``)."""

    for key, value in overrides.items():
        if (
            isinstance(value, dict)
            and isinstance(base.get(key), dict)
        ):
            _deep_merge(base[key], value)
        else:
            base[key] = value
    return base


def merge_workspace_settings(existing: dict | None, updates: dict) -> dict:
    """Return a deep-merged copy of ``existing`` with ``updates`` applied.

    Missing keys in ``existing`` are backfilled from the defaults — safe to
    call on legacy workspace rows without a full settings blob.
    """

    merged = default_workspace_settings()
    if existing:
        _deep_merge(merged, existing)
    _deep_merge(merged, updates)
    return merged


# ---------------------------------------------------------------------------
# Singleton
# ---------------------------------------------------------------------------


_settings: Settings | None = None


def get_settings() -> Settings:
    """Return the process-wide infra settings singleton."""

    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings


def reset_settings_for_tests() -> None:
    """Clear the cached singleton so tests can rebuild it with fresh env."""

    global _settings
    _settings = None
