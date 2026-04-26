"""Pydantic response/request models for the REST API.

Every model here is the public contract the TypeScript types in
``frontend/src/lib/types.ts`` mirror. If you add a field here, add the
matching field there.

Design rules:
- ``extra='ignore'`` on requests — the frontend shouldn't care about
  server-internal fields leaking in.
- Every datetime is serialised as an ISO 8601 UTC string.
- ``workspace.settings`` is passed through as a generic ``dict`` — the
  setup wizard and Settings page both patch arbitrary subsets.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class _ApiModel(BaseModel):
    """Base for all response models — permissive enough for ORM objects."""

    model_config = ConfigDict(
        from_attributes=True,
        arbitrary_types_allowed=True,
        populate_by_name=True,
    )


# ---------------------------------------------------------------------------
# Setup wizard
# ---------------------------------------------------------------------------


class SetupStatus(_ApiModel):
    setup_required: bool
    workspace_id: str | None = None


class ConnectorTextBeeCreds(BaseModel):
    api_url: str = "https://api.textbee.dev"
    api_key: str
    device_id: str
    poll_limit: int = 50


class ConnectorSmsGateCreds(BaseModel):
    api_url: str
    username: str
    password: str


class SetupRequest(BaseModel):
    """POST /api/setup — seeds the one workspace row."""

    model_config = ConfigDict(extra="ignore")

    business_name: str
    business_dump: str
    tone_prompt: str | None = None

    llm_provider: Literal["gemini", "openai", "anthropic"] = "gemini"
    llm_api_key: str
    model_main: str | None = None  # falls back to default

    connector_type: Literal["file", "textbee", "smsgate"] = "file"
    textbee: ConnectorTextBeeCreds | None = None
    smsgate: ConnectorSmsGateCreds | None = None


# ---------------------------------------------------------------------------
# Workspace
# ---------------------------------------------------------------------------


class WorkspaceOut(_ApiModel):
    id: str
    business_name: str
    business_dump: str
    tone_prompt: str | None
    settings: dict[str, Any]
    created_at: datetime
    updated_at: datetime


class WorkspacePatch(BaseModel):
    model_config = ConfigDict(extra="ignore")

    business_name: str | None = None
    business_dump: str | None = None
    tone_prompt: str | None = None


class WorkspaceSettingsPatch(BaseModel):
    """Arbitrary subset of ``workspace.settings`` — deep-merged server-side."""

    model_config = ConfigDict(extra="allow")


# ---------------------------------------------------------------------------
# Connector test
# ---------------------------------------------------------------------------


class ConnectorTestRequest(BaseModel):
    """Optional override of the saved connector for ``POST /connector/test``.

    The Settings page lets the operator click "Test connection" before
    saving — the unsaved form state is posted here verbatim. When every
    field is omitted we validate the currently-cached connector instead.

    ``textbee`` / ``smsgate`` are plain dicts so the connector's own
    constructor + ``validate_config`` surface credential issues in the
    response body rather than 422'ing at the schema layer.
    """

    model_config = ConfigDict(extra="ignore")

    type: Literal["file", "textbee", "smsgate"] | None = None
    textbee: dict[str, Any] | None = None
    smsgate: dict[str, Any] | None = None


class ConnectorTestResult(BaseModel):
    """Outcome of a connector connectivity probe."""

    ok: bool
    detail: str
    connector_type: str


# ---------------------------------------------------------------------------
# Status
# ---------------------------------------------------------------------------


class LlmUsage(BaseModel):
    calls_today: int = 0
    tokens_in_today: int = 0
    tokens_out_today: int = 0
    estimated_cost_today_usd: float = 0.0


class CampaignQuota(BaseModel):
    id: str
    name: str
    sent_24h: int
    quota: int


class SchedulerInfo(BaseModel):
    tick_s: int
    poll_s: int


class SystemStatusOut(BaseModel):
    paused: bool
    started_at: datetime | None
    active_connector: str
    override_to: str | None
    auto_reply_enabled: bool
    setup_required: bool
    llm_usage: LlmUsage
    campaigns: list[CampaignQuota]
    scheduler: SchedulerInfo


# ---------------------------------------------------------------------------
# Campaign
# ---------------------------------------------------------------------------


class FollowupConfig(BaseModel):
    """Per-campaign follow-up beat config.

    Mirrors the shape stored on ``campaign.followup``. All fields are
    optional so a partial PATCH can toggle the feature without the
    frontend having to round-trip the full blob. The backend fills
    reasonable defaults before persisting.
    """

    model_config = ConfigDict(extra="ignore")

    enabled: bool = False
    template: str = ""
    delay_s: int = 10
    delay_jitter_s: int = 5


class CampaignOut(BaseModel):
    id: str
    name: str
    goal: str
    outreach_per_day: int
    connector_type: str
    status: str
    followup: FollowupConfig
    quota_reset_at: datetime | None = None
    created_at: datetime
    lead_count: int = 0
    contacted_count: int = 0
    replied_count: int = 0
    won_count: int = 0
    sent_24h: int = 0


class CampaignKickoffRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")

    count: int = Field(default=1, ge=1, le=100)


class CampaignKickoffResult(BaseModel):
    requested: int
    attempted: int
    sent: int
    failed: int
    remaining_queued: int
    campaign: CampaignOut


class CampaignCreate(BaseModel):
    model_config = ConfigDict(extra="ignore")

    name: str
    goal: str
    outreach_per_day: int = 50
    connector_type: str | None = None
    followup: FollowupConfig | None = None


class CampaignPatch(BaseModel):
    model_config = ConfigDict(extra="ignore")

    name: str | None = None
    goal: str | None = None
    outreach_per_day: int | None = None
    status: Literal["draft", "active", "paused", "completed"] | None = None
    followup: FollowupConfig | None = None


class CampaignAssignLeads(BaseModel):
    model_config = ConfigDict(extra="ignore")

    lead_ids: list[str] = Field(default_factory=list)
    # If omitted, all eligible leads in the workspace are assigned.
    all_eligible: bool = False


# ---------------------------------------------------------------------------
# Lead
# ---------------------------------------------------------------------------


class LeadOut(BaseModel):
    id: str
    name: str | None
    contact_uri: str | None
    contact_type: str | None
    category: str | None
    address: str | None
    website: str | None
    raw_data: dict[str, Any] = Field(default_factory=dict)
    import_order: int
    source_file: str | None
    status: str
    skip_reason: str | None
    created_at: datetime


class LeadListOut(BaseModel):
    """Paginated leads response.

    The Leads page can be fed tens of thousands of rows (e.g. a single
    regional scrape), so the server is the source of truth for both
    pagination and per-status counts. ``counts_by_status`` includes the
    ``all`` bucket so the filter tabs can render without an extra round-trip.
    """

    leads: list[LeadOut]
    total: int
    limit: int
    offset: int
    counts_by_status: dict[str, int] = Field(default_factory=dict)


class ImportPreviewRow(BaseModel):
    name: str | None
    phone: str | None
    normalised_phone: str | None
    contact_type: str
    skip_reason: str | None


class ImportPreviewSkipReason(BaseModel):
    reason: str
    count: int


class ImportPreviewOut(BaseModel):
    filename: str
    file_type: str
    total_rows: int
    would_import: int
    would_skip: list[ImportPreviewSkipReason]
    sample: list[ImportPreviewRow]


class ImportCommitOut(BaseModel):
    job_id: str
    row_count: int
    imported_count: int
    skipped_count: int
    error_count: int
    errors: list[dict[str, Any]] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Threads / messages
# ---------------------------------------------------------------------------


class SuggestionOut(BaseModel):
    draft: str
    overall: float
    scores: dict[str, Any] | None = None
    feedback: str | None = None
    pass_: bool = Field(alias="pass", default=False)
    attempts: int = 1
    temperature: float | None = None
    gen_llm_call_id: str | None = None
    eval_llm_call_id: str | None = None

    model_config = ConfigDict(populate_by_name=True)


class ThreadOut(BaseModel):
    id: str
    campaign_id: str
    campaign_name: str
    lead_id: str
    lead_name: str | None
    lead_phone: str | None
    lead_category: str | None
    lead_address: str | None
    connector_type: str
    status: str
    auto_reply_count: int
    angle: str | None
    tone_snapshot: str | None
    hitl_reason: str | None
    hitl_context: dict[str, Any] | None
    hitl_dismissed_at: datetime | None
    last_message_at: datetime | None
    created_at: datetime


class MessageOut(_ApiModel):
    """Outbound representation of a ``Message`` row.

    The ORM column is named ``metadata`` but the Python attribute is
    ``metadata_`` (SQLAlchemy reserves ``metadata`` on the declarative
    base). Surfacing that underscore to API consumers is noisy, so we
    keep the public JSON field name ``metadata`` and use a validation
    alias so ``model_validate(msg, from_attributes=True)`` can still
    pull the value off the ORM object.
    """

    id: str
    thread_id: str
    role: str
    content: str
    metadata: dict[str, Any] = Field(
        default_factory=dict,
        validation_alias="metadata_",
    )
    created_at: datetime


class SendDraftRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")

    draft: str
    source: Literal["ai_suggested", "manual"] = "manual"


class TakeOverRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")
    note: str | None = None


class CloseThreadRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")
    outcome: Literal["won", "lost"]


# ---------------------------------------------------------------------------
# LLM calls
# ---------------------------------------------------------------------------


class LlmCallOut(BaseModel):
    id: str
    created_at: datetime
    workspace_id: str | None
    campaign_id: str | None
    thread_id: str | None
    lead_id: str | None
    purpose: str
    model: str
    prompt_version: str | None
    temperature: float | None
    attempt: int
    response_format: str
    system_prompt: str | None
    user_prompt: str | None
    response_text: str | None
    response_parsed: dict[str, Any] | None
    tokens_in: int
    tokens_out: int
    latency_ms: int
    error: str | None


# ---------------------------------------------------------------------------
# Stats
# ---------------------------------------------------------------------------


class SendsByDay(BaseModel):
    date: str  # YYYY-MM-DD
    count: int


class Sends14dOut(BaseModel):
    days: list[SendsByDay]


__all__ = [
    "CampaignAssignLeads",
    "CampaignCreate",
    "CampaignOut",
    "CampaignPatch",
    "CampaignQuota",
    "CloseThreadRequest",
    "ConnectorSmsGateCreds",
    "ConnectorTestRequest",
    "ConnectorTestResult",
    "ConnectorTextBeeCreds",
    "FollowupConfig",
    "ImportCommitOut",
    "ImportPreviewOut",
    "ImportPreviewRow",
    "ImportPreviewSkipReason",
    "LeadListOut",
    "LeadOut",
    "LlmCallOut",
    "LlmUsage",
    "MessageOut",
    "SchedulerInfo",
    "SendDraftRequest",
    "Sends14dOut",
    "SendsByDay",
    "SetupRequest",
    "SetupStatus",
    "SuggestionOut",
    "SystemStatusOut",
    "TakeOverRequest",
    "ThreadOut",
    "WorkspaceOut",
    "WorkspacePatch",
    "WorkspaceSettingsPatch",
]
