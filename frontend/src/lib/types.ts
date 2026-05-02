/**
 * Domain types mirror the Pydantic schemas in `autosdr/api/schemas.py`.
 *
 * Keep this file and `schemas.py` in sync — the API is the source of
 * truth. Status vocabularies are mirrored from `autosdr/models.py`.
 */

export type UUID = string;
export type ISODate = string;

// ---------- enums ----------

export const LeadStatus = {
  NEW: 'new',
  CONTACTED: 'contacted',
  REPLIED: 'replied',
  WON: 'won',
  LOST: 'lost',
  SKIPPED: 'skipped',
} as const;
export type LeadStatusT = (typeof LeadStatus)[keyof typeof LeadStatus];

export const ContactType = {
  MOBILE: 'mobile',
  LANDLINE: 'landline',
  TOLL_FREE: 'toll_free',
  UNKNOWN: 'unknown',
  EMAIL: 'email',
} as const;
export type ContactTypeT = (typeof ContactType)[keyof typeof ContactType];

export const CampaignStatus = {
  DRAFT: 'draft',
  ACTIVE: 'active',
  PAUSED: 'paused',
  COMPLETED: 'completed',
} as const;
export type CampaignStatusT = (typeof CampaignStatus)[keyof typeof CampaignStatus];

export const ThreadStatus = {
  ACTIVE: 'active',
  PAUSED: 'paused',
  PAUSED_FOR_HITL: 'paused_for_hitl',
  WON: 'won',
  LOST: 'lost',
  SKIPPED: 'skipped',
} as const;
export type ThreadStatusT = (typeof ThreadStatus)[keyof typeof ThreadStatus];

export const MessageRole = {
  AI: 'ai',
  HUMAN: 'human',
  LEAD: 'lead',
} as const;
export type MessageRoleT = (typeof MessageRole)[keyof typeof MessageRole];

export const LlmCallPurpose = {
  ANALYSIS: 'analysis',
  GENERATION: 'generation',
  EVALUATION: 'evaluation',
  CLASSIFICATION: 'classification',
  OTHER: 'other',
} as const;
export type LlmCallPurposeT = (typeof LlmCallPurpose)[keyof typeof LlmCallPurpose];

export const ReplyIntent = {
  POSITIVE: 'positive',
  OBJECTION: 'objection',
  QUESTION: 'question',
  NEGATIVE: 'negative',
  UNCLEAR: 'unclear',
  BOT_CHECK: 'bot_check',
  GOAL_ACHIEVED: 'goal_achieved',
  HUMAN_REQUESTED: 'human_requested',
} as const;
export type ReplyIntentT = (typeof ReplyIntent)[keyof typeof ReplyIntent];

/**
 * HITL reasons. Values mirror the raw strings emitted by the backend
 * pipelines — see:
 *   - ``autosdr/pipeline/reply.py`` (``HITL_AWAITING_HUMAN_REPLY`` +
 *     ``_hitl_reason_for`` + ``reply_eval_failed`` + ``connector_send_failed``)
 *   - ``autosdr/pipeline/outreach.py`` (``eval_failed_after_max_attempts`` +
 *     ``connector_send_failed``)
 *   - ``autosdr/api/threads.py`` (``taken_over_by_human``)
 *
 * With ``auto_reply_enabled=false`` (the default), every classified
 * non-terminal reply lands on ``awaiting_human_reply`` with a set of
 * AI-drafted suggestions stashed on the thread. The other values only
 * surface in legacy auto-reply mode or when outreach/connector blew up.
 */
export const HitlReason = {
  AWAITING_HUMAN_REPLY: 'awaiting_human_reply',
  EVAL_FAILED_AFTER_MAX_ATTEMPTS: 'eval_failed_after_max_attempts',
  REPLY_EVAL_FAILED: 'reply_eval_failed',
  CONNECTOR_SEND_FAILED: 'connector_send_failed',
  UNCLEAR: 'unclear',
  BOT_CHECK: 'bot_check',
  HUMAN_REQUESTED: 'human_requested',
  LOW_CONFIDENCE: 'low_confidence',
  MAX_AUTO_REPLIES_REACHED: 'max_auto_replies_reached',
  ESCALATED: 'escalated',
  TAKEN_OVER_BY_HUMAN: 'taken_over_by_human',
} as const;
export type HitlReasonT = (typeof HitlReason)[keyof typeof HitlReason];

// ---------- workspace settings ----------

export type ConnectorType = 'file' | 'textbee' | 'smsgate';

export interface WorkspaceSettings {
  auto_reply_enabled: boolean;
  suggestions_count: number;
  default_region?: string;

  // AI loop (stored flat for parity with the pipeline helpers).
  eval_threshold: number;
  eval_max_attempts: number;

  // Scheduler (flat too).
  scheduler_tick_s: number;
  min_inter_send_delay_s: number;
  max_batch_per_tick: number;
  inbound_poll_s: number;

  // Working-hours pacing window applied to scheduler outreach sends.
  // Replies, manual kickoff, and the follow-up beat are unaffected.
  // Per-campaign override on ``Campaign.outreach_window``.
  outreach_window: OutreachWindowConfig;

  // Lead-website enrichment. Run inline at outreach-time before the
  // analysis LLM call. Off-by-default at the schema level is wrong —
  // the polite path (enabled, robots-respecting) is the default. See
  // ``autosdr/api/schemas.py::EnrichmentConfig`` and ticket 0011.
  enrichment: EnrichmentConfig;

  // Send-order priority — when ``enabled`` is true (the default), the
  // scheduler picker drains a priority tier (today: leads whose
  // website returned 404 on scan) before the normal tier on every
  // tick. The category-mix interleave runs unchanged within each
  // tier. Toggling off restores the pre-0013 single-tier behaviour.
  // See ticket 0013.
  priority: PriorityConfig;

  llm: {
    provider_api_keys: {
      gemini?: string | null;
      openai?: string | null;
      anthropic?: string | null;
    };
    model_main: string;
    model_analysis: string;
    model_eval: string;
    model_classification: string;
    temperature_main?: number;
    temperature_eval?: number;
    reasoning_classification?: 'disable' | 'low' | 'medium' | 'high';
  };

  connector: {
    type: ConnectorType;
    textbee?: {
      api_url?: string;
      api_key?: string | null;
      device_id?: string | null;
      poll_limit?: number;
    };
    smsgate?: {
      api_url?: string | null;
      username?: string | null;
      password?: string | null;
    };
  };

  rehearsal: {
    override_to: string | null;
  };

  // Browser-vendor Web Push for HITL escalations. The
  // ``vapid_public`` is hot-loaded once via ``GET /api/push/vapid-public``;
  // the private half never crosses the API boundary, so the type only
  // declares the operator-visible knobs. See ticket 0005.
  push?: {
    hitl_escalations?: boolean;
    dashboard_origin?: string | null;
    vapid_subject?: string;
  };

  // Anything else the server knows about but the UI doesn't.
  [key: string]: unknown;
}

export interface PushVapidPublic {
  public_key: string | null;
  dashboard_origin: string | null;
}

export interface PushSubscriptionRow {
  id: string;
  endpoint_host: string;
  user_agent: string | null;
  last_seen_at: ISODate | null;
  last_error: string | null;
  created_at: ISODate;
}

export interface PushSubscriptionsResponse {
  subscriptions: PushSubscriptionRow[];
  hitl_escalations: boolean;
  dashboard_origin: string | null;
}

export interface PushTestResult {
  sent: number;
  gone: number;
  failed: number;
}

export interface NetworkingStatus {
  host: string;
  port: number;
  bound_for_remote_access: boolean;
  tailscale: {
    state: 'running' | 'not_running' | 'not_detected';
    detail: string | null;
  };
  warning: string | null;
  dashboard_origin_override: string | null;
  dashboard_origin_resolved: string | null;
  request_origin: string | null;
}

// ---------- records ----------

export interface Workspace {
  id: UUID;
  business_name: string;
  business_dump: string;
  tone_prompt: string | null;
  settings: WorkspaceSettings;
  created_at: ISODate;
  updated_at: ISODate;
}

export interface Lead {
  id: UUID;
  name: string | null;
  contact_uri: string | null;
  contact_type: ContactTypeT | null;
  category: string | null;
  address: string | null;
  website: string | null;
  raw_data: Record<string, unknown>;
  import_order: number;
  source_file: string | null;
  status: LeadStatusT;
  skip_reason: string | null;
  /** Set when the lead has opted out (Spam Act / TCPA shortcut). */
  do_not_contact_at: ISODate | null;
  /** Stable ``opt_out:<KEYWORD>`` string or operator-supplied free text. */
  do_not_contact_reason: string | null;
  created_at: ISODate;
  /**
   * ``true`` when the scheduler will send this lead before normal-tier
   * leads on the next pass. Fires on either
   * ``enrichment_status == "not_found"`` (ticket 0013) or
   * ``Lead.website`` being a social-profile URL (ticket 0014). See
   * ``autosdr/pipeline/priority.py::is_priority_lead``.
   */
  is_priority: boolean;
  /**
   * Literal token explaining the priority tier:
   * - ``"not_found"`` — server returned 404/410 on the website scan.
   * - ``"social_profile_website"`` — ``website`` is a Facebook / IG /
   *   LinkedIn / etc. URL.
   * ``null`` when ``is_priority`` is false. Single deterministic
   * winner: ``not_found`` outranks ``social_profile_website`` when
   * both fire on the same lead.
   */
  priority_reason: string | null;
  /**
   * Informational platform token (``"facebook"``, ``"instagram"``,
   * ``"linkedin"``, ``"twitter"``, ``"x"``, ``"tiktok"``,
   * ``"youtube"``) when ``website`` is itself a social-profile URL,
   * else ``null``. Set independently of priority — a 404'd Facebook
   * URL has ``priority_reason="not_found"`` (precedence) but still
   * exposes ``is_social_website="facebook"`` so the
   * ``SocialProfileTag`` chip renders. Ticket 0014.
   */
  is_social_website: string | null;
}

/**
 * Paginated response for ``GET /api/leads``. ``counts_by_status`` always
 * reflects the search-filtered set (status filter excluded) so filter
 * tabs can render accurate tallies without a round-trip per tab.
 */
export interface LeadList {
  leads: Lead[];
  total: number;
  limit: number;
  offset: number;
  counts_by_status: Record<string, number>;
}

/** ``POST /api/leads/enrich`` — warm-up enrichment cache. */
export interface LeadEnrichCandidate {
  lead_id: UUID;
  name: string | null;
  website: string | null;
  last_fetched: string | null;
}

export type LeadEnrichResult = {
  ok: number;
  failed: number;
  total: number;
  dry_run: boolean;
  candidates: LeadEnrichCandidate[] | null;
};

/** ``POST /api/dev/sim-inbound`` (file connector rehearsal). */
export interface DevSimInboundResult {
  action: string;
  thread_id: string | null;
  intent: string | null;
  confidence: number | null;
  detail: string | null;
}

/**
 * Per-campaign follow-up beat. When ``enabled``, a literal-template
 * second message is fired ``delay_s ± delay_jitter_s`` seconds after the
 * first outbound on any thread in the campaign. The backend skips the
 * send if the lead replies or the thread is closed in the interim.
 *
 * ``template`` supports a small set of placeholders — ``{name}``,
 * ``{short_name}``, ``{owner_first_name}`` — rendered from the same
 * analysis output as the first message. Unknown tokens render literally
 * so the operator can spot typos in their own drafts.
 *
 * Mirrors ``autosdr/api/schemas.py::FollowupConfig``.
 */
export interface FollowupConfig {
  enabled: boolean;
  template: string;
  delay_s: number;
  delay_jitter_s: number;
}

/**
 * Working-hours window the scheduler paces outreach across. ``start_hour``
 * is inclusive, ``end_hour`` is exclusive — ``8..17`` means
 * ``[08:00, 17:00)`` in server-local time. ``enabled=false`` is the
 * escape hatch (no time gating). Replies, manual kickoff, and the
 * follow-up beat are unaffected.
 *
 * Mirrors ``autosdr/api/schemas.py::OutreachWindowConfig``.
 */
export interface OutreachWindowConfig {
  enabled: boolean;
  start_hour: number;
  end_hour: number;
}

/**
 * Per-workspace knobs for the lead-website enrichment fetcher. See
 * ``autosdr/api/schemas.py::EnrichmentConfig`` and ticket 0011.
 *
 * - ``enabled``: master switch. Off → outreach skips the fetch and
 *   records ``enrichment_status: "disabled"`` on the analysis row.
 * - ``budget_s``: total wall-clock budget per lead (1–15 s). The
 *   fetcher caps each request at 1.5 s and stops after the budget,
 *   so a hung site can never block the scheduler tick.
 * - ``cache_ttl_days``: re-run outreach inside this window does NOT
 *   re-fetch — the cached envelope is reused. ``0`` disables caching.
 * - ``respect_robots``: polite-default true. Operator can flip it
 *   for an aggressive scrape but the default is the polite path.
 */
export interface EnrichmentConfig {
  enabled: boolean;
  budget_s: number;
  cache_ttl_days: number;
  respect_robots: boolean;
}

/**
 * Per-workspace knobs for the send-order priority tier (ticket 0013).
 *
 * - ``enabled``: master switch. When ``false`` the scheduler picker
 *   collapses to the pre-0013 single-tier behaviour — exactly what
 *   operators saw before priority shipped.
 *
 * The vocabulary widens to platform filters in ticket 0014; this
 * interface gains additional keys then.
 */
export interface PriorityConfig {
  enabled: boolean;
}

/**
 * Closed vocabulary for the per-lead enrichment outcome — mirrors
 * ``autosdr/enrichment.py::EnrichmentStatus`` plus the pipeline-only
 * ``"disabled"`` variant produced when the workspace toggle is off.
 *
 * The frontend uses this in two places:
 * 1. The Lead-detail enrichment card renders a status badge.
 * 2. The angle-funnel ``?enrichment=`` segmented control filters
 *    threads by whether their first AI message carried ``"ok"``.
 */
export type EnrichmentStatus =
  | 'ok'
  | 'no_url'
  | 'timeout'
  | 'blocked'
  | 'empty_shell'
  | 'not_found'
  | 'error'
  | 'killswitch_aborted'
  | 'disabled';

/**
 * Versioned envelope persisted under ``Lead.raw_data.enrichment``.
 * Kept loose-typed on ``signals`` because the parser may grow new
 * fields ahead of the frontend; the LeadDetail card reads only the
 * fields it knows about and falls back to "—" for the rest.
 *
 * ``connector`` / ``connector_version`` are present on envelopes
 * written by ``ENVELOPE_VERSION >= 2``. Older v1 blobs (none on
 * fresh installs) won't carry them; the worker treats those as stale
 * and re-scans automatically.
 */
export interface LeadEnrichment {
  _meta: {
    version: number;
    status: EnrichmentStatus;
    fetched_at: ISODate;
    final_url?: string;
    http_status?: number;
    latency_ms?: number;
    user_agent?: string;
    robots_respected?: boolean;
    connector?: string;
    connector_version?: string;
  };
  signals: {
    title?: string;
    meta_description?: string;
    h1?: string;
    cms?: string;
    cms_evidence?: string;
    viewport_present?: boolean;
    is_https?: boolean;
    og_image_present?: boolean;
    favicon_present?: boolean;
    sitemap_count?: number;
    sitemap_last_modified?: string;
    robots_present?: boolean;
    external_links_to_socials?: string[];
    [key: string]: unknown;
  };
}

export type ScanStatus = EnrichmentStatus | 'never_scanned' | 'disabled';

/**
 * One row of the ``/scans`` index page. Mirrors
 * ``autosdr.api.schemas.ScanRowOut``.
 *
 * Lean by design: the table only shows status/cms/sitemap/latency.
 * Audit-detail fields like ``http_status`` / ``final_url`` /
 * ``connector`` live on ``ScanDetail`` (the per-lead detail route),
 * which still exposes the full envelope.
 */
export interface ScanRow {
  lead_id: UUID;
  lead_name: string | null;
  website: string | null;
  status: ScanStatus;
  fetched_at: ISODate | null;
  latency_ms: number | null;
  cms: string | null;
  sitemap_count: number | null;
}

/**
 * Paginated response for ``GET /api/scans``.
 *
 * ``counts_by_status`` carries every ``ScanStatus`` bucket the worker
 * can produce plus the synthetic ``never_scanned`` bucket so the
 * filter chips have honest tallies on first paint.
 */
export interface ScanList {
  scans: ScanRow[];
  total: number;
  limit: number;
  offset: number;
  counts_by_status: Record<string, number>;
}

/** Header strip on the Scans page. Mirrors ``ScanSummaryOut``. */
export interface ScanSummary {
  total_leads: number;
  ok: number;
  blocked: number;
  timeout: number;
  error: number;
  not_found: number;
  empty_shell: number;
  no_url: number;
  never_scanned: number;
  last_run_at: ISODate | null;

  runner_running: boolean;
  runner_total: number;
  runner_done: number;
  runner_ok: number;
  runner_failed: number;
  runner_started_at: ISODate | null;
}

/** Body of ``POST /api/scans/run``. */
export interface ScanRunRequest {
  /** When set, scan that lead synchronously (detail \"Re-scan now\"). */
  lead_id?: UUID;
  /** Starts (true) or stops (false) the batch scan runner. */
  enabled?: boolean;
}

/** Result of ``POST /api/scans/run`` — mirrors summary plus optional one-off sync fields. */
export interface ScanRunResult extends ScanSummary {
  started?: boolean | null;
  lead_id?: UUID | null;
  status?: EnrichmentStatus | string | null;
}

/**
 * Full envelope + lead summary for ``GET /api/scans/{lead_id}``.
 * ``enrichment`` is the same versioned blob the LeadDetail card
 * reads, exposed verbatim so the operator can audit what we
 * captured (including the raw ``_meta`` block).
 */
export interface ScanDetail {
  lead_id: UUID;
  lead_name: string | null;
  website: string | null;
  status: ScanStatus;
  enrichment: LeadEnrichment | null;
}

/**
 * One field per ``CampaignLeadStatus`` bucket. Each is a precise count
 * — they don't roll up and don't double-count, so summing all eight
 * equals ``lead_count``. Frontend rollups (e.g. "leads we ever
 * messaged" = ``contacted_count + replied_count + won_count + lost_count``)
 * are computed at the call site, not on the server.
 *
 * Mirrors ``autosdr/api/schemas.py::CampaignOut`` — see ticket 0003 for
 * the rationale on replacing the previous rolled-up
 * ``contacted_count`` / ``replied_count`` semantics.
 */
export interface Campaign {
  id: UUID;
  name: string;
  goal: string;
  outreach_per_day: number;
  connector_type: string;
  status: CampaignStatusT;
  followup: FollowupConfig;
  /** Per-campaign override; ``null`` = inherit the workspace default. */
  outreach_window: OutreachWindowConfig | null;
  /** Resolved window the scheduler will actually use (override if set, otherwise workspace default). */
  effective_outreach_window: OutreachWindowConfig;
  quota_reset_at: ISODate | null;
  created_at: ISODate;
  lead_count: number;
  queued_count: number;
  /**
   * Subset of ``queued_count`` whose leads will be sent ahead of the
   * rest by the scheduler picker (ticket 0013). Always
   * ``<= queued_count``. Used by ``CampaignDetail`` to render
   * "X of Y queued are priority" next to the queued tile.
   */
  queued_priority_count: number;
  sending_count: number;
  paused_for_hitl_count: number;
  contacted_count: number;
  replied_count: number;
  won_count: number;
  lost_count: number;
  skipped_count: number;
  /**
   * Outreach contacts opened *today* (calendar day, server-local
   * midnight reset). One contact = one thread whose first AI message
   * landed at-or-after today's midnight; follow-ups and auto-replies
   * don't count. Resets again at server-local midnight.
   */
  sent_today: number;
}

/**
 * Result of ``POST /campaigns/{id}/assign-leads`` — a Campaign plus the IDs
 * of any leads the API refused to enqueue (today: do-not-contact-flagged).
 */
export interface CampaignAssignLeadsResult extends Campaign {
  skipped_lead_ids: UUID[];
  skipped_reason: string | null;
}

export interface CampaignKickoffResult {
  requested: number;
  attempted: number;
  sent: number;
  failed: number;
  remaining_queued: number;
  campaign: Campaign;
}

/**
 * One day of the per-campaign funnel — UTC ``YYYY-MM-DD`` and four
 * independent counters. ``replied`` is the number of threads whose
 * **first ever** lead reply landed on that day, so a chatty lead
 * replying twice on Tuesday is still one ``replied``. ``won`` /
 * ``lost`` use the terminal ``Thread.status`` and its ``updated_at``,
 * so a thread that closes the same day it replied is counted in both
 * ``replied`` and ``won`` / ``lost``.
 *
 * Mirrors ``autosdr/api/schemas.py::CampaignTimeseriesBucket``.
 */
export interface CampaignTimeseriesBucket {
  date: string;
  sent: number;
  replied: number;
  won: number;
  lost: number;
}

/**
 * Response for ``GET /api/campaigns/{id}/timeseries``. Always
 * ``days`` rows, oldest first, padded with zero rows for days with no
 * activity so the chart can render a stable window even on a fresh
 * campaign.
 */
export interface CampaignTimeseries {
  days: number;
  buckets: CampaignTimeseriesBucket[];
}

/**
 * Per-criterion scores the evaluator emits for a draft. All floats in
 * the [0, 1] range. Mirrors
 * ``autosdr/prompts/evaluation.py::evaluate_result`` — the LLM sees
 * exactly these keys.
 */
export interface EvalScores {
  tone_match?: number;
  personalisation?: number;
  goal_alignment?: number;
  length_valid?: number;
  naturalness?: number;
}

/** Full evaluator output: scores + overall + pass flag + optional feedback. */
export interface EvalResult {
  scores: EvalScores;
  overall: number;
  pass: boolean;
  feedback?: string | null;
}

/**
 * A single AI-generated reply option surfaced to the operator when a
 * thread is awaiting human reply. The operator picks one, edits, or
 * regenerates. Comes from `autosdr/pipeline/suggestions.py`.
 */
export interface Suggestion {
  draft: string;
  /** ``null`` for follow-up suggestions that didn't run through the eval loop. */
  overall: number | null;
  scores?: EvalScores | null;
  feedback?: string | null;
  pass?: boolean | null;
  attempts?: number;
  temperature?: number | null;
  gen_llm_call_id?: string | null;
  eval_llm_call_id?: string | null;
  /** "outreach" = first-touch audit flow, "followup" = thread-aware single-call flow. */
  source?: 'outreach' | 'followup';
}

export interface DraftAttempt {
  attempt: number;
  draft: string;
  scores: EvalScores;
  overall: number;
  pass: boolean;
  feedback: string | null;
  gen_llm_call_id?: string | null;
  eval_llm_call_id?: string | null;
}

export interface HitlContext {
  intent?: ReplyIntentT;
  confidence?: number;
  reason?: string;
  incoming_message?: string;
  classification_llm_call_id?: string | null;
  suggestions?: Suggestion[];
  attempts?: DraftAttempt[];
  last_drafts?: string[];
  last_scores?: { overall?: number; feedback?: string | null; breakdown?: EvalScores }[];
  last_feedback?: string | null;
  connector_error?: string | null;
  last_intent?: ReplyIntentT;
  last_confidence?: number;
  note?: string;
}

export interface Thread {
  id: UUID;
  campaign_id: UUID;
  campaign_name: string;
  lead_id: UUID;
  lead_name: string | null;
  lead_phone: string | null;
  lead_category: string | null;
  lead_address: string | null;
  connector_type: string;
  status: ThreadStatusT;
  auto_reply_count: number;
  angle: string | null;
  tone_snapshot: string | null;
  hitl_reason: HitlReasonT | string | null;
  hitl_context: HitlContext | null;
  hitl_dismissed_at: ISODate | null;
  last_message_at: ISODate | null;
  created_at: ISODate;
}

export interface Message {
  id: UUID;
  thread_id: UUID;
  role: MessageRoleT;
  content: string;
  metadata: {
    model?: string;
    prompt_version?: string;
    attempt_count?: number;
    // Single overall score the evaluator gave the draft, 0..1.
    eval_score?: number;
    eval_scores_breakdown?: EvalScores;
    provider_id?: string;
    provider_message_id?: string;
    intent?: ReplyIntentT;
    confidence?: number;
    // ``followup`` is the delayed second beat set by
    // ``autosdr/pipeline/followup.py`` — no LLM, static template, parent
    // points back at the first outbound so the transcript can group
    // the pair visually.
    source?: 'ai_suggested' | 'manual' | 'auto_reply' | 'followup';
    parent_message_id?: string;
    scheduled_delay_s?: number;
    sent_at?: string;
    human_sent_at?: string;
    [key: string]: unknown;
  };
  created_at: ISODate;
}

export interface LlmCall {
  id: UUID;
  created_at: ISODate;
  workspace_id: UUID | null;
  campaign_id: UUID | null;
  thread_id: UUID | null;
  lead_id: UUID | null;
  purpose: LlmCallPurposeT;
  model: string;
  prompt_version: string | null;
  temperature: number | null;
  attempt: number;
  response_format: 'text' | 'json';
  system_prompt: string | null;
  user_prompt: string | null;
  response_text: string | null;
  response_parsed: Record<string, unknown> | null;
  tokens_in: number;
  tokens_out: number;
  latency_ms: number;
  error: string | null;
  /**
   * Estimated USD cost computed from the row's
   * ``model``/``tokens_in``/``tokens_out`` against
   * ``autosdr/llm/pricing.py``. ``null`` for models we don't have a
   * rate card for — render as ``—`` rather than ``$0.00`` to avoid
   * lying. See ticket 0006.
   */
  cost_usd: number | null;
}

/**
 * All-time aggregate response for ``GET /api/llm-calls/summary``.
 *
 * ``total_cost_usd`` is summed server-side across *every* LlmCall row
 * (filtered by the same params the list endpoint accepts), so it
 * stays accurate past the list endpoint's 500-row cap. ``unpriced_calls``
 * counts rows we couldn't price — those contribute $0 to the total, so
 * the UI should show "≥ $X" with a tooltip when this is non-zero.
 */
export interface LlmCallsSummary {
  total_calls: number;
  total_tokens_in: number;
  total_tokens_out: number;
  total_cost_usd: number;
  unpriced_calls: number;
}

/**
 * One named blend the operator can apply with a single click in the
 * Settings → LLM card. Mirrors
 * ``autosdr/api/schemas.py::LlmPresetOut``.
 */
export interface LlmPreset {
  id: string;
  label: string;
  description: string;
  models: {
    model_main: string;
    model_analysis: string;
    model_eval: string;
    model_classification: string;
  };
}

/**
 * Response for ``GET /api/llm/presets``.
 *
 * ``pricing_verified_at`` is the snapshot date of the backend's
 * pricing table — surface alongside the buttons so operators know
 * how stale the cost numbers are.
 */
export interface LlmPresetCatalog {
  pricing_verified_at: string;
  presets: LlmPreset[];
}

export interface SystemStatus {
  paused: boolean;
  started_at: ISODate | null;
  active_connector: ConnectorType | string;
  override_to: string | null;
  auto_reply_enabled: boolean;
  setup_required: boolean;
  llm_usage: {
    calls_today: number;
    tokens_in_today: number;
    tokens_out_today: number;
    estimated_cost_today_usd: number;
  };
  campaigns: {
    id: UUID;
    name: string;
    sent_today: number;
    quota: number;
  }[];
  scheduler: {
    tick_s: number;
    poll_s: number;
  };
  /**
   * Depth + age of the killswitch's deferred-inbound queue (ticket
   * 0009). When the killswitch is on the webhook handler stashes
   * inbounds in ``paused_inbound`` instead of dropping them; the
   * resume endpoint drains the queue. Mirrors
   * ``autosdr.api.schemas.PausedInboundStatus``.
   *
   * - ``pending_count`` is the unreplayed row count.
   * - ``oldest_pending_at`` is the ``created_at`` of the oldest row,
   *   useful for flagging stale queues.
   *
   * Both are zero / null on a fresh boot.
   */
  paused_inbound: {
    pending_count: number;
    oldest_pending_at: ISODate | null;
  };
}

/**
 * Canonical core fields a source column can map onto. Mirrors
 * ``autosdr.api.schemas._CORE_FIELD_NAMES`` and the literal-tuple
 * ``autosdr.importer._CORE_FIELDS``. If a new core field lands on
 * the wire, this union must move with it.
 */
export type CoreFieldName = 'name' | 'category' | 'address' | 'website' | 'phone';

/**
 * Per-column suggestion-engine output surfaced in the preview. Used
 * by the LeadsImport mapping table so the operator can see what the
 * server *would* do and override before commit. Mirrors
 * ``autosdr.api.schemas.ImportPreviewColumn``.
 *
 * - ``suggested_target`` is one of the core field names, ``"raw_only"``
 *   (server saw the column but won't promote it), or ``null`` (no
 *   confident pick — the operator decides).
 * - ``suggestion_confidence`` is the tiered score from
 *   ``autosdr.importer._suggest_column_target`` — ``"high"`` is an
 *   exact / alias / strong-heuristic match, ``"medium"`` is a fuzzy
 *   or substring lean, ``"low"`` is a weak signal, ``"none"`` is no
 *   suggestion at all.
 */
export interface ImportPreviewColumn {
  name: string;
  sample_values: unknown[];
  suggested_target: CoreFieldName | 'raw_only' | null;
  suggestion_confidence: 'high' | 'medium' | 'low' | 'none';
  suggestion_reason: string;
}

export interface ImportPreview {
  filename: string;
  file_type: 'csv' | 'json' | 'ndjson' | string;
  total_rows: number;
  would_import: number;
  would_skip: {
    reason: string;
    count: number;
  }[];
  sample: {
    name: string | null;
    phone: string | null;
    normalised_phone: string | null;
    contact_type: ContactTypeT | string;
    skip_reason: string | null;
  }[];
  columns: ImportPreviewColumn[];
  /**
   * Per-platform tally of rows whose ``website`` is a social-profile
   * URL. Empty object when no social URLs were detected — frontend
   * renders nothing in that case. Sample shape:
   * ``{ "facebook": 12, "instagram": 3 }``. Ticket 0014.
   */
  social_website_hosts?: Record<string, number>;
}

/**
 * Operator-supplied override sent to ``/api/leads/import/preview`` and
 * ``/api/leads/import/commit`` as a JSON-encoded string in the
 * ``mapping_config`` multipart form field.
 *
 * Mirrors ``autosdr.api.schemas.MappingConfigIn`` — strict on the
 * server (``extra=forbid``), so typos like ``drop_form_raw`` will 422.
 *
 * - ``mapping`` is canonical → source column. Empty entries (drop the
 *   suggestion entirely) are omitted from this object before
 *   serialising.
 * - ``drop_from_raw`` is **commit-only**: it filters incoming
 *   ``raw_data`` on this import only. It does **not** retroactively
 *   prune existing rows.
 * - ``include_in_raw_only`` keeps a source column in ``raw_data``
 *   even when its name would alias-match a core field.
 */
export interface MappingConfig {
  mapping: Partial<Record<CoreFieldName, string>>;
  drop_from_raw: string[];
  include_in_raw_only: string[];
}

export interface ImportCommit {
  job_id: UUID;
  row_count: number;
  imported_count: number;
  skipped_count: number;
  error_count: number;
  errors: { row: number; reason: string }[];
}

export interface SetupStatus {
  setup_required: boolean;
  workspace_id: string | null;
}

export interface SetupPayload {
  business_name: string;
  business_dump: string;
  tone_prompt?: string;
  llm_provider: 'gemini' | 'openai' | 'anthropic';
  llm_api_key: string;
  model_main?: string;
  connector_type: ConnectorType;
  textbee?: {
    api_url?: string;
    api_key: string;
    device_id: string;
    poll_limit?: number;
  };
  smsgate?: {
    api_url: string;
    username: string;
    password: string;
  };
}

export interface SendsByDay {
  date: string;
  count: number;
}

/**
 * One row of the angle-funnel aggregation. Mirrors
 * ``autosdr/api/schemas.py::AngleFunnelRow``.
 *
 * ``angle`` is the discrete bucket from ``Thread.angle_type`` — one of
 * the seven values the analysis prompt emits, plus ``"unknown"`` for
 * legacy threads written before that column existed.
 *
 * ``replied`` counts threads with at least one ``role=lead`` message
 * (the more honest signal than ``CampaignLead.status``, which can lag).
 * ``won`` / ``lost`` reflect the terminal ``Thread.status`` and are
 * independent of ``replied`` (a thread can be both replied AND won).
 */
export interface AngleFunnelRow {
  angle: string;
  threads: number;
  replied: number;
  won: number;
  lost: number;
}

/**
 * Stratifier for ``GET /api/stats/angle-funnel?enrichment=``. ``"all"``
 * is the default and matches every thread. ``"enriched"`` keeps only
 * threads whose first AI message has ``metadata.analysis.enrichment_status
 * == "ok"``; ``"unenriched"`` is the strict complement (timeouts,
 * blocked, no_url, disabled, AND legacy threads written before the
 * column existed).
 */
export type EnrichmentFilter = 'all' | 'enriched' | 'unenriched';

export interface AngleFunnel {
  /** ISO 8601 string. ``null`` when scope is a campaign and no override
   *  was supplied — implies "campaign lifetime". */
  since: ISODate | null;
  /** Echoed back when the request was campaign-scoped, otherwise null. */
  campaign_id: UUID | null;
  /** Resolved value of the ``?enrichment=`` filter — defaults to ``"all"``. */
  enrichment: EnrichmentFilter;
  /** Per-bucket counts, server-sorted by ``threads`` descending. */
  rows: AngleFunnelRow[];
}

/**
 * Result of ``POST /api/workspace/connector/test``. The backend always
 * returns 200 and encodes failures as ``ok=false`` with a human-readable
 * ``detail`` (missing creds, network error, server 4xx, …) so the UI can
 * render the outcome inline instead of treating every failure as an
 * unhandled exception.
 */
export interface ConnectorTestResult {
  ok: boolean;
  detail: string;
  connector_type: string;
}

export interface ConnectorTestRequest {
  type?: ConnectorType;
  textbee?: {
    api_url?: string;
    api_key?: string | null;
    device_id?: string | null;
  };
  smsgate?: {
    api_url?: string | null;
    username?: string | null;
    password?: string | null;
  };
}
