"""Environment-backed configuration.

Settings are loaded once at process start via :class:`Settings`. Workspace-level
values (tone, LLM model slots, scheduler knobs) also live in the database so
they can be changed at runtime without a redeploy; env values act as defaults
that a fresh ``autosdr init`` writes into the workspace row.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Process-wide configuration read from environment and ``.env``."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # LLM
    gemini_api_key: str | None = None
    openai_api_key: str | None = None
    anthropic_api_key: str | None = None

    llm_model_main: str = "gemini/gemini-3-flash-preview"
    llm_model_analysis: str = "gemini/gemini-3-flash-preview"
    llm_model_eval: str = "gemini/gemini-3.1-flash-lite-preview"
    llm_model_classification: str = "gemini/gemini-3.1-flash-lite-preview"
    llm_temperature_main: float = 0.7
    llm_temperature_eval: float = 0.0

    # Connector
    connector: Literal["file", "textbee", "smsgate"] = "file"
    textbee_api_url: str = "https://api.textbee.dev"
    textbee_api_key: str | None = None
    textbee_device_id: str | None = None
    textbee_poll_limit: int = 50

    # SmsGate (capcom6/android-sms-gateway). API URL should include the full
    # ``/3rdparty/v1`` path — e.g. http://localhost:3000/api/3rdparty/v1 for a
    # local docker server, or http://<lan-ip>:8080/3rdparty/v1 for local mode.
    smsgate_api_url: str = "http://localhost:3000/api/3rdparty/v1"
    smsgate_username: str | None = None
    smsgate_password: str | None = None

    # Test modes (both composable, both honored by ``get_connector``)
    # DRY_RUN=true forces FileConnector regardless of CONNECTOR — no SMS is
    # ever sent. SMS_OVERRIDE_TO=+61... wraps whichever connector is active so
    # every outbound goes to that one number (real sends, one phone).
    dry_run: bool = False
    sms_override_to: str | None = None

    # Server
    host: str = "127.0.0.1"
    port: int = 8000

    # Database
    database_url: str = "sqlite:///data/autosdr.db"

    # Kill switch
    pause_flag_path: Path = Path("data/.autosdr-pause")
    pid_file_path: Path = Path("data/autosdr.pid")

    # Scheduler overrides (optional — workspace.settings is the source of truth;
    # when set here, these override the DB values at tick time).
    scheduler_tick_s: int | None = None
    min_inter_send_delay_s: int | None = None
    max_batch_per_tick: int | None = None
    inbound_poll_s: int | None = None

    # FileConnector outbox
    outbox_path: Path = Path("data/outbox.jsonl")

    # Logging / observability
    log_dir: Path = Path("data/logs")
    llm_log_enabled: bool = True
    llm_log_max_prompt_chars: int = 16000

    # ----- Derived helpers -------------------------------------------------

    @property
    def webhook_path_sim(self) -> str:
        return "/api/webhooks/sim"


_DEFAULT_WORKSPACE_SETTINGS: dict = {
    "max_auto_replies": 5,
    "eval_threshold": 0.85,
    "eval_max_attempts": 3,
    "raw_data_size_limit_kb": 50,
    "default_region": "AU",
    "scheduler_tick_s": 60,
    "min_inter_send_delay_s": 30,
    "max_batch_per_tick": 2,
    "inbound_poll_s": 20,
    "llm": {
        "model_main": "gemini/gemini-3-flash-preview",
        "model_analysis": "gemini/gemini-3-flash-preview",
        "model_eval": "gemini/gemini-3.1-flash-lite-preview",
        "model_classification": "gemini/gemini-3.1-flash-lite-preview",
        "temperature_main": 0.7,
        "temperature_eval": 0.0,
    },
}


def default_workspace_settings(env: Settings) -> dict:
    """Seed `workspace.settings` from env defaults at `autosdr init` time."""

    merged = {**_DEFAULT_WORKSPACE_SETTINGS}
    merged["llm"] = {
        "model_main": env.llm_model_main,
        "model_analysis": env.llm_model_analysis,
        "model_eval": env.llm_model_eval,
        "model_classification": env.llm_model_classification,
        "temperature_main": env.llm_temperature_main,
        "temperature_eval": env.llm_temperature_eval,
    }
    return merged


_settings: Settings | None = None


def get_settings() -> Settings:
    """Return a process-wide :class:`Settings` singleton."""

    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings


def reset_settings_for_tests() -> None:
    """Clear the cached singleton so tests can rebuild it with fresh env."""

    global _settings
    _settings = None
