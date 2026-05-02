"""Infrastructure-only settings.

AutoSDR's behaviour-affecting configuration — LLM keys, connector choice,
thresholds, override recipient, scheduler knobs — lives in
``workspace.settings`` (a JSON blob on the workspace row). That is the
single source of truth at runtime and is mutated via the REST API.

Only truly infrastructural values (database url, server host/port, path for
the kill-switch flag, log directory, built-frontend location) are read from
the environment. These rarely change and don't belong in the UI.
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

    # FileConnector outbox
    outbox_path: Path = Path("data/outbox.jsonl")

    # Logging
    log_dir: Path = Path("data/logs")
    llm_log_enabled: bool = True
    llm_log_max_prompt_chars: int = 16000

    # Built frontend (served from FastAPI at / when present).
    frontend_dist_dir: Path = Path("frontend/dist")

    # Lead-website scan fan-out — max concurrent ``enrich_lead`` tasks.
    # 20 is the polite-by-default rate for cross-host crawling on a
    # single Python process: enough parallelism to keep the network
    # busy without saturating DNS, the OS-level TCP stack, or the
    # shared ``httpx.AsyncClient`` pool. Override with
    # ``SCAN_CONCURRENCY=...`` in the environment; values north of
    # ~100 typically need OS ulimit + httpx pool tuning to actually
    # pay off (the previous 200 default ignored both, which was the
    # whole reason every lead was returning ``status=timeout``).
    # See :mod:`autosdr.pipeline.scans`.
    scan_concurrency: int = 20


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

    # Outreach window — pace each campaign's daily quota evenly across a
    # working window in server-local time. Outside the window, the
    # outreach tick is a no-op for sends (the inbound poller, reply
    # pipeline, follow-up beat and manual kickoff are unaffected). A
    # per-campaign override on ``campaign.outreach_window`` takes
    # precedence; ``None`` there means "inherit this default".
    "outreach_window": {
        "enabled": True,
        "start_hour": 8,   # inclusive, 0-23
        "end_hour": 17,    # exclusive, 1-24
    },

    # Lead-website enrichment — per-lead public-website fetch run
    # immediately before the analysis LLM call so the prompt has
    # structural signals (title, H1, CMS, sitemap count) the Apify
    # ``webResults: null`` slot otherwise leaves blank. See ticket 0011.
    # Caps: 3 HTTP requests max per lead, ≤1.5 s/request, ≤budget_s
    # total wall time, polite robots policy default.
    "enrichment": {
        "enabled": True,
        "budget_s": 4.0,
        "cache_ttl_days": 30,
        "respect_robots": True,
    },

    # Send-order priority — when ``enabled`` is true (default), the
    # scheduler's ``_next_queued_leads`` drains a priority tier
    # (today: leads whose website returned 404) before the normal
    # tier on every tick, while preserving the existing category-mix
    # rotation within each tier. See ticket 0013. The toggle is the
    # operator's "back to FIFO + category mix" escape hatch.
    "priority": {
        "enabled": True,
    },

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
        # ``reasoning_classification``: thinking-budget cap for the
        # classification call. Accepts ``"disable" | "low" | "medium"
        # | "high"``. Classification has a 60-token output (one of 8
        # intent labels + a one-line reason); reasoning is wasted
        # spend / latency.
        #
        # Default is ``"disable"`` based on a 5-thread live replay
        # (see ``scripts/replay_classifier_smoke.py``):
        #   - no override: ~60 tokens_out, ~1.5s latency
        #   - "low":      ~180 tokens_out, ~3s latency, 1/5 intent
        #                  flip (OLD objection → NEW negative on a
        #                  thumbs-up to a closing message)
        #   - "disable":   ~55 tokens_out, ~1.1s latency, 0/5 flips
        # i.e. setting ``"low"`` *enables* thinking that Flash-Lite
        # was skipping by default, costing ~3x tokens and inflating
        # latency without improving accuracy. ``"disable"`` is
        # effectively a no-op against today's defaults but pins the
        # behaviour so a future provider change (Flash-Lite quietly
        # turning thinking on) doesn't silently inflate cost.
        # See ``docs/prompt-audit-2026-05-02.md`` Phase 4 #13.
        "reasoning_classification": "disable",
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

    # Web Push notifications for HITL escalations (ticket 0005).
    #
    # ``vapid_public`` / ``vapid_private`` are generated on first boot
    # (see :mod:`autosdr.push`); never expose ``vapid_private`` via the
    # API. ``vapid_subject`` is the ``mailto:``-or-URL contact handed to
    # the push gateway as RFC 8292 voluntary application identification.
    #
    # ``hitl_escalations`` is the per-event v0 toggle — additional event
    # types (send-failed, quota-exhausted, deploy-watch alerts) are filed
    # as follow-up tickets and would each get their own boolean here.
    #
    # ``dashboard_origin`` overrides the deep-link origin baked into push
    # payloads. ``None`` means "use the request Host the API saw at
    # subscribe-time" (the same-origin default).
    "push": {
        "vapid_public": None,
        "vapid_private": None,
        "vapid_subject": "mailto:autosdr@localhost",
        "hitl_escalations": True,
        "dashboard_origin": None,
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
