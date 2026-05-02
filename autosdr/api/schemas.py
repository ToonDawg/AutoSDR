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
    sent_today: int
    quota: int


class SchedulerInfo(BaseModel):
    tick_s: int
    poll_s: int


class PausedInboundStatus(BaseModel):
    """Depth + age of the killswitch's deferred-inbound queue.

    Surfaced on ``GET /api/status`` so the killswitch banner can render
    a "12 inbound waiting for resume" badge — the operator's only
    visible signal that the queue exists. ``oldest_pending_at`` lets
    the UI flag stale queues (e.g. paused for hours).

    Both fields are zero / None on a fresh install. The queue itself
    lives in ``paused_inbound`` (see :class:`autosdr.models.PausedInbound`).
    """

    pending_count: int = 0
    oldest_pending_at: datetime | None = None


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
    paused_inbound: PausedInboundStatus = Field(default_factory=PausedInboundStatus)


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


class EnrichmentConfig(BaseModel):
    """Per-workspace knobs for the lead-website enrichment fetcher.

    Mirrors the shape stored on ``workspace.settings.enrichment``. The
    enrichment fetcher (:mod:`autosdr.enrichment`) is consulted at
    outreach-time to fold a small structural-signal blob into
    ``Lead.raw_data['enrichment']`` before the analysis LLM call. See
    ticket 0011.

    All fields are deliberately conservative defaults: enabled, polite
    (respect robots.txt), 4-second wall-clock budget, 30-day cache. The
    operator can flip ``respect_robots`` for an aggressive scrape but
    the default is the polite path.

    Bounds:
    * ``budget_s`` is clamped to ``[1.0, 15.0]`` so a stuck mock can't
      hold a scheduler tick hostage and a 0-second budget can't stall
      the pipeline forever waiting on a fetch the function will skip.
    * ``cache_ttl_days`` is clamped to ``[0, 365]``. ``0`` means "never
      cache, always re-enrich"; ``365`` is the upper bound so an
      operator can't accidentally pin a stale blob forever.
    """

    model_config = ConfigDict(extra="ignore")

    enabled: bool = True
    budget_s: float = Field(default=4.0, ge=1.0, le=15.0)
    cache_ttl_days: int = Field(default=30, ge=0, le=365)
    respect_robots: bool = True


class OutreachWindowConfig(BaseModel):
    """Working-hours window the scheduler paces outreach across.

    Mirrors the shape stored on ``workspace.settings.outreach_window``
    (the workspace default) and ``campaign.outreach_window`` (per-campaign
    override; ``None`` means inherit). Both ``start_hour`` and
    ``end_hour`` are server-local 24h integers; the window is inclusive
    on the start and exclusive on the end, so ``8..17`` means
    ``[08:00, 17:00)`` in local time.

    See :mod:`autosdr.pacing` for how this is consumed; the gate only
    affects the outreach scheduler tick — replies, follow-ups, manual
    kickoff and the inbound poller are unaffected.
    """

    model_config = ConfigDict(extra="ignore")

    enabled: bool = True
    start_hour: int = Field(default=8, ge=0, le=23)
    end_hour: int = Field(default=17, ge=1, le=24)


class CampaignOut(BaseModel):
    """Public shape of a campaign on the REST API.

    The eight ``*_count`` fields are **bucket-precise** mirrors of
    :class:`autosdr.models.CampaignLeadStatus`. Each one is the count of
    leads currently parked in exactly that bucket — they do not roll
    over and do not double-count, so:

        lead_count
          == queued_count
          + sending_count
          + paused_for_hitl_count
          + contacted_count
          + replied_count
          + won_count
          + lost_count
          + skipped_count

    Frontend rollups ("how many leads did we actually message?") sum
    these on demand. This deliberately replaces the pre-0003 rolled-up
    ``contacted_count`` / ``replied_count`` semantics — the names lied,
    so the names changed meaning. See ticket 0003.
    """

    id: str
    name: str
    goal: str
    outreach_per_day: int
    connector_type: str
    status: str
    followup: FollowupConfig
    # Per-campaign override of the workspace's outreach window. ``None``
    # means "inherit the workspace default" — the resolved window the
    # scheduler will actually use is exposed on
    # ``effective_outreach_window``.
    outreach_window: OutreachWindowConfig | None = None
    # The window the scheduler will actually use after resolving the
    # campaign override against the workspace default. Always populated
    # so frontend consumers don't have to merge themselves.
    effective_outreach_window: OutreachWindowConfig
    quota_reset_at: datetime | None = None
    created_at: datetime
    lead_count: int = 0
    queued_count: int = 0
    # Subset of ``queued_count`` whose ``Lead.enrichment_status``
    # makes them priority — today, leads that returned 404 on the
    # scan worker. Surfaces the "X of Y queued are priority" hint
    # next to the queued count on the Campaign Detail page. Always
    # ``<= queued_count``. See ticket 0013.
    queued_priority_count: int = 0
    sending_count: int = 0
    paused_for_hitl_count: int = 0
    contacted_count: int = 0
    replied_count: int = 0
    won_count: int = 0
    lost_count: int = 0
    skipped_count: int = 0
    # Outreach contacts opened *today* (calendar day, server-local
    # midnight reset). One contact = one thread whose first AI message
    # landed at-or-after today's midnight; follow-ups and auto-replies
    # don't count. Resets again at server-local midnight.
    sent_today: int = 0


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


class CampaignTimeseriesBucket(BaseModel):
    """One day of campaign activity in UTC.

    Counts are independent slices of the funnel, not stages. ``replied``
    counts the number of threads whose **first ever** lead-message
    landed on this day — so a chatty lead that replies twice on Tuesday
    is still one ``replied``, and a thread that first replied on Monday
    and again on Tuesday is only counted on Monday. ``won`` / ``lost``
    use the terminal :class:`Thread.status` and ``Thread.updated_at``;
    a thread that closes after replying on the same day is counted in
    both ``replied`` and ``won`` / ``lost``.
    """

    date: str  # YYYY-MM-DD UTC
    sent: int = 0
    replied: int = 0
    won: int = 0
    lost: int = 0


class CampaignTimeseriesOut(BaseModel):
    """Response for ``GET /api/campaigns/{id}/timeseries``.

    ``buckets`` always has ``days`` entries, oldest first, padded with
    zero rows for days with no activity so the chart can render a stable
    14-day window even on a fresh campaign.
    """

    days: int
    buckets: list[CampaignTimeseriesBucket]


class CampaignCreate(BaseModel):
    model_config = ConfigDict(extra="ignore")

    name: str
    goal: str
    outreach_per_day: int = 50
    connector_type: str | None = None
    followup: FollowupConfig | None = None
    outreach_window: OutreachWindowConfig | None = None


class CampaignPatch(BaseModel):
    model_config = ConfigDict(extra="ignore")

    name: str | None = None
    goal: str | None = None
    outreach_per_day: int | None = None
    status: Literal["draft", "active", "paused", "completed"] | None = None
    followup: FollowupConfig | None = None
    # Patch semantics (relies on FastAPI's ``exclude_unset`` handling
    # downstream): omit the field for "no change", send ``null`` to clear
    # the per-campaign override and fall back to the workspace default,
    # send a populated object to set the override.
    outreach_window: OutreachWindowConfig | None = None


class CampaignAssignLeads(BaseModel):
    model_config = ConfigDict(extra="ignore")

    lead_ids: list[str] = Field(default_factory=list)
    # If omitted, all eligible leads in the workspace are assigned.
    all_eligible: bool = False


class CampaignAssignLeadsOut(CampaignOut):
    """Result of POST ``/campaigns/{id}/assign-leads``.

    Extends :class:`CampaignOut` with the lead IDs the API refused to assign
    so the operator can see at a glance which ones got skipped (today: leads
    flagged ``do_not_contact`` for compliance reasons).
    """

    skipped_lead_ids: list[str] = Field(default_factory=list)
    skipped_reason: str | None = None


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
    do_not_contact_at: datetime | None = None
    do_not_contact_reason: str | None = None
    created_at: datetime
    # Send-order priority surfacing for tickets 0013 + 0014.
    # ``is_priority`` is the boolean used by
    # ``frontend/src/components/domain/PriorityBadge`` to decide
    # whether to render; ``priority_reason`` is the literal token
    # (``"not_found"`` or ``"social_profile_website"``) used as a
    # tooltip / a11y label. Both are computed server-side in
    # :func:`autosdr.api.leads._lead_to_out` from
    # :func:`autosdr.pipeline.priority.priority_reason`. Plain
    # ``str`` is intentional — see the ticket's resolved
    # ``priority-reason-type`` open question.
    is_priority: bool = False
    priority_reason: str | None = None
    # Informational sibling of ``priority_reason``: the platform
    # token (``"facebook"``, ``"instagram"``, ``"linkedin"``,
    # ``"twitter"``, ``"x"``, ``"tiktok"``, ``"youtube"``) when
    # ``Lead.website`` is itself a social-profile URL, else
    # ``None``. Set independently of priority so a 404'd Facebook
    # URL reads as ``priority_reason="not_found"`` (precedence) but
    # still exposes ``is_social_website="facebook"`` to drive the
    # ``SocialProfileTag`` chip.
    is_social_website: str | None = None


class LeadOptOutIn(BaseModel):
    """Body for marking a lead as do-not-contact (manual opt-off SMS channel)."""

    reason: str = "manual"


class LeadEnrichIn(BaseModel):
    """Warm up website enrichment for leads with stale or missing cache."""

    since_days: int = Field(default=30, ge=1, le=365)
    limit: int = Field(default=50, ge=1, le=200)
    dry_run: bool = False


class LeadEnrichCandidateOut(BaseModel):
    lead_id: str
    name: str | None = None
    website: str | None = None
    # ISO8601 from enrichment ``_meta.fetched_at``, or null if never enriched.
    last_fetched: str | None = None


class LeadEnrichOut(BaseModel):
    ok: int
    failed: int
    total: int
    dry_run: bool
    candidates: list[LeadEnrichCandidateOut] | None = None


class DevSimInboundIn(BaseModel):
    contact_uri: str
    content: str


class DevSimInboundOut(BaseModel):
    """Result from POST /api/dev/sim-inbound (file connector rehearsal)."""

    action: str
    thread_id: str | None = None
    intent: str | None = None
    confidence: float | None = None
    detail: str | None = None


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


class ImportPreviewColumn(BaseModel):
    """One distinct source column observed in the preview sample, plus the
    suggestion-engine's recommendation. Shape mirrored on the frontend in
    ``ImportPreview['columns']`` (``frontend/src/lib/types.ts``)."""

    name: str
    sample_values: list[Any]
    suggested_target: str | None
    suggestion_confidence: Literal["high", "medium", "low", "none"]
    suggestion_reason: str


class ImportPreviewOut(BaseModel):
    filename: str
    file_type: str
    total_rows: int
    would_import: int
    would_skip: list[ImportPreviewSkipReason]
    sample: list[ImportPreviewRow]
    columns: list[ImportPreviewColumn] = Field(default_factory=list)
    # Per-platform tally of rows whose mapped ``website`` is a
    # social-profile URL (ticket 0014). Empty dict when no social
    # URLs are detected in the upload — frontend renders nothing in
    # that case. Sample shape: ``{"facebook": 12, "instagram": 3}``.
    # Defaulted so the field is always present in the JSON response.
    social_website_hosts: dict[str, int] = Field(default_factory=dict)


# The four canonical core fields the operator can map a source column to.
# Mirrors ``autosdr.importer._CORE_FIELDS`` — kept in sync deliberately so
# the shape contract on the wire is explicit; if a new core field lands,
# both lists must move together.
_CORE_FIELD_NAMES = Literal["name", "category", "address", "website", "phone"]


class MappingConfigIn(BaseModel):
    """Operator-supplied import override (ticket 0004).

    Posted as a JSON-encoded string in the multipart form field
    ``mapping_config`` on both ``/api/leads/import/preview`` and
    ``/api/leads/import/commit``. Validation is strict — unknown keys at the
    top level are rejected so we don't silently swallow a typo
    (``drop_form_raw``).
    """

    model_config = ConfigDict(extra="forbid")

    mapping: dict[_CORE_FIELD_NAMES, str] = Field(default_factory=dict)
    drop_from_raw: list[str] = Field(default_factory=list)
    include_in_raw_only: list[str] = Field(default_factory=list)


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
    # Estimated USD cost at current pricing (autosdr/llm/pricing.py).
    # ``null`` for models we don't have a rate card for so the UI shows
    # ``—`` rather than a misleading $0.00. See ticket 0006.
    cost_usd: float | None = None


class LlmCallsSummaryOut(BaseModel):
    """All-time aggregate of every LLM call in the workspace.

    The list endpoint caps at 500 rows for UI virtualisation, so summing
    the visible rows on the client would silently underreport spend on
    any non-trivial workspace. This response is the source of truth for
    the "total spend" stat above the Logs table.

    ``unpriced_calls`` is the count of legacy rows with ``cost_usd``
    missing (pre write-time-cost migration). Those rows contribute zero
    to ``total_cost_usd`` so the UI can show "≥ $X" with a tooltip when
    this number is non-zero rather than implying a precise figure.
    """

    total_calls: int = 0
    total_tokens_in: int = 0
    total_tokens_out: int = 0
    total_cost_usd: float = 0.0
    unpriced_calls: int = 0


# ---------------------------------------------------------------------------
# Stats
# ---------------------------------------------------------------------------


class SendsByDay(BaseModel):
    date: str  # YYYY-MM-DD
    count: int


class Sends14dOut(BaseModel):
    days: list[SendsByDay]


class AngleFunnelRow(BaseModel):
    """Per-angle funnel counts for one bucket of `Thread.angle_type`.

    Buckets are the seven values the analysis prompt emits, plus
    ``"unknown"`` for legacy threads that pre-date the column. The four
    counters always satisfy ``replied <= threads`` and
    ``won + lost <= threads`` (a thread can be both replied and
    won/lost; these are independent slices of the funnel, not stages).
    """

    angle: str
    threads: int
    replied: int
    won: int
    lost: int


EnrichmentFilter = Literal["all", "enriched", "unenriched"]


class AngleFunnelOut(BaseModel):
    """Response for ``GET /api/stats/angle-funnel``.

    ``since`` is echoed back as the effective time window the server
    applied (server-resolved, never trusted from the query string for
    rendering). ``campaign_id`` is echoed when scoped to a campaign;
    when scoped to a workspace and a campaign filter wasn't supplied,
    it is ``None``. ``rows`` is the per-bucket aggregation, ordered by
    ``threads`` descending so the dominant angle renders first.

    ``enrichment`` is the resolved value of the ``?enrichment=`` filter
    (default ``"all"``); the API echoes it back so the frontend can
    render the segmented control without re-parsing the query string.
    """

    since: datetime | None
    campaign_id: str | None
    enrichment: EnrichmentFilter = "all"
    rows: list[AngleFunnelRow]


# ---------------------------------------------------------------------------
# LLM presets (Gemini-only for now — see ticket 0006)
# ---------------------------------------------------------------------------


class LlmPresetModels(BaseModel):
    """Four-role model blend for an :class:`LlmPresetOut`.

    Keys mirror ``WorkspaceSettings.llm.model_*`` so the frontend can
    spread this object straight into a settings PATCH.
    """

    model_main: str
    model_analysis: str
    model_eval: str
    model_classification: str


class LlmPresetOut(BaseModel):
    """One named blend the operator can apply with one click.

    Surfaces alongside enough pricing information that the UI can show
    "MAX is ~Nx more than CHEAP" without re-deriving it on the
    frontend.
    """

    id: str
    label: str
    description: str
    models: LlmPresetModels


class LlmPresetsOut(BaseModel):
    """Response for ``GET /api/llm/presets``.

    ``pricing_verified_at`` is the snapshot date of
    :data:`autosdr.llm.pricing.PRICING_VERIFIED_AT` — surfaced so the
    UI can render "Pricing as of YYYY-MM-DD" alongside the buttons.
    """

    pricing_verified_at: str  # ISO date YYYY-MM-DD
    presets: list[LlmPresetOut]


# ---------------------------------------------------------------------------
# Scans (lead-website enrichment)
# ---------------------------------------------------------------------------


# Pseudo-status when ``Lead.enrichment_status IS NULL`` — no scan attempt yet.
SCAN_STATUS_NEVER = "never_scanned"


class ScanRowOut(_ApiModel):
    """One row of the ``/scans`` index page.

    Deliberately lean: the list view only renders the columns below.
    Audit-detail fields like ``http_status`` / ``final_url`` /
    ``connector`` live on :class:`ScanDetailOut` (which exposes the
    full envelope) so the list payload stays small and the row query
    can avoid hydrating ``raw_data``.
    """

    lead_id: str
    lead_name: str | None
    website: str | None
    status: str
    fetched_at: datetime | None = None
    latency_ms: int | None = None
    cms: str | None = None
    sitemap_count: int | None = None


class ScanListOut(_ApiModel):
    """Paginated response for ``GET /api/scans``.

    ``counts_by_status`` includes both the real enrichment statuses and
    the synthetic ``never_scanned`` bucket so the filter tabs render
    without an extra round-trip.
    """

    scans: list[ScanRowOut]
    total: int
    limit: int
    offset: int
    counts_by_status: dict[str, int] = Field(default_factory=dict)


class ScanDetailOut(_ApiModel):
    """Full envelope + lead summary for ``GET /api/scans/{lead_id}``."""

    lead_id: str
    lead_name: str | None
    website: str | None
    status: str
    enrichment: dict[str, Any] | None = None


class ScanSummaryOut(_ApiModel):
    """Header strip on the Scans page — breakdown + optional batch runner."""

    total_leads: int
    ok: int
    blocked: int
    timeout: int
    error: int
    not_found: int
    empty_shell: int
    no_url: int
    never_scanned: int
    last_run_at: datetime | None = None

    runner_running: bool = False
    runner_total: int = 0
    runner_done: int = 0
    runner_ok: int = 0
    runner_failed: int = 0
    runner_started_at: datetime | None = None


class ScanRunRequest(BaseModel):
    """Body of ``POST /api/scans/run``.

    ``enabled`` starts (``True``) or stops (``False``) the in-process
    scan fan-out. ``lead_id`` triggers one synchronous re-scan — ``enabled``
    is ignored in that branch.
    """

    model_config = ConfigDict(extra="ignore")

    lead_id: str | None = None
    enabled: bool | None = None


class ScanRunResult(ScanSummaryOut):
    """Response of ``POST /api/scans/run``.

    Mirrors :class:`ScanSummaryOut` plus optional fields describing a
    one-off synchronous re-scan.
    """

    started: bool | None = None
    lead_id: str | None = None
    status: str | None = None


# ---------------------------------------------------------------------------
# Push subscriptions (ticket 0005)
# ---------------------------------------------------------------------------


class PushVapidPublicOut(_ApiModel):
    """Response of ``GET /api/push/vapid-public``.

    The SW reads ``public_key`` to call :js:func:`PushManager.subscribe`
    and ``dashboard_origin`` to deep-link the notification. Both can be
    ``None`` on a fresh boot before the lifespan has generated keys —
    the SW treats that as "push not yet enabled".
    """

    public_key: str | None
    dashboard_origin: str | None


class PushSubscribeKeys(BaseModel):
    """Mirror of the JSON the browser hands back from
    :js:func:`subscription.toJSON`."""

    model_config = ConfigDict(extra="ignore")

    p256dh: str
    auth: str


class PushSubscribeRequest(BaseModel):
    """Body of ``POST /api/push/subscribe`` / ``DELETE /api/push/subscribe``."""

    model_config = ConfigDict(extra="ignore")

    endpoint: str
    keys: PushSubscribeKeys | None = None
    user_agent: str | None = None


class PushSubscriptionOut(_ApiModel):
    """One push-subscription row, surfaced on Settings → Notifications."""

    id: str
    user_agent: str | None
    created_at: datetime
    last_seen_at: datetime
    last_error: str | None
    endpoint_host: str


class PushSubscriptionsOut(_ApiModel):
    subscriptions: list[PushSubscriptionOut]
    hitl_escalations: bool
    dashboard_origin: str | None


class PushTestRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")

    endpoint: str | None = None


class PushTestResult(_ApiModel):
    sent: int
    gone: int
    failed: int


class TailscaleProbeOut(_ApiModel):
    """Outcome of ``tailscale status`` at request time."""

    state: str
    detail: str | None = None


class NetworkingStatusOut(_ApiModel):
    """Snapshot of remote-access readiness (ticket 0005 unit 8).

    Surfaces the configured ``HOST``/``port``, the Tailscale probe,
    and the resolved push deep-link origin so the operator can spot
    the *PC-bind-interface footgun* without leaving Settings.
    """

    host: str
    port: int
    bound_for_remote_access: bool
    tailscale: TailscaleProbeOut
    warning: str | None = None
    dashboard_origin_override: str | None = None
    dashboard_origin_resolved: str | None = None
    request_origin: str | None = None


__all__ = [
    "AngleFunnelOut",
    "AngleFunnelRow",
    "EnrichmentFilter",
    "CampaignAssignLeads",
    "CampaignCreate",
    "CampaignOut",
    "CampaignPatch",
    "CampaignQuota",
    "CampaignTimeseriesBucket",
    "CampaignTimeseriesOut",
    "CloseThreadRequest",
    "ConnectorSmsGateCreds",
    "ConnectorTestRequest",
    "ConnectorTestResult",
    "ConnectorTextBeeCreds",
    "EnrichmentConfig",
    "FollowupConfig",
    "ImportCommitOut",
    "ImportPreviewColumn",
    "ImportPreviewOut",
    "ImportPreviewRow",
    "ImportPreviewSkipReason",
    "MappingConfigIn",
    "LeadEnrichCandidateOut",
    "LeadEnrichIn",
    "LeadEnrichOut",
    "LeadListOut",
    "LeadOptOutIn",
    "LeadOut",
    "DevSimInboundIn",
    "DevSimInboundOut",
    "LlmCallOut",
    "LlmCallsSummaryOut",
    "LlmPresetModels",
    "LlmPresetOut",
    "LlmPresetsOut",
    "LlmUsage",
    "MessageOut",
    "NetworkingStatusOut",
    "PushSubscribeKeys",
    "PushSubscribeRequest",
    "PushSubscriptionOut",
    "PushSubscriptionsOut",
    "PushTestRequest",
    "PushTestResult",
    "PushVapidPublicOut",
    "SchedulerInfo",
    "TailscaleProbeOut",
    "SendDraftRequest",
    "SCAN_STATUS_NEVER",
    "ScanDetailOut",
    "ScanListOut",
    "ScanRowOut",
    "ScanRunRequest",
    "ScanRunResult",
    "ScanSummaryOut",
    "Sends14dOut",
    "SendsByDay",
    "SuggestionOut",
    "SystemStatusOut",
    "TakeOverRequest",
    "ThreadOut",
    "WorkspaceOut",
    "WorkspacePatch",
    "WorkspaceSettingsPatch",
]
