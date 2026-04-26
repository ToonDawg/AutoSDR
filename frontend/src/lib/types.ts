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

  // Anything else the server knows about but the UI doesn't.
  [key: string]: unknown;
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
  created_at: ISODate;
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

export interface Campaign {
  id: UUID;
  name: string;
  goal: string;
  outreach_per_day: number;
  connector_type: string;
  status: CampaignStatusT;
  followup: FollowupConfig;
  quota_reset_at: ISODate | null;
  created_at: ISODate;
  lead_count: number;
  contacted_count: number;
  replied_count: number;
  won_count: number;
  sent_24h: number;
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
  overall: number;
  scores?: EvalScores | null;
  feedback?: string | null;
  pass?: boolean;
  attempts?: number;
  temperature?: number | null;
  gen_llm_call_id?: string | null;
  eval_llm_call_id?: string | null;
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
  last_scores?: EvalScores;
  last_feedback?: string | null;
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
    sent_24h: number;
    quota: number;
  }[];
  scheduler: {
    tick_s: number;
    poll_s: number;
  };
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
