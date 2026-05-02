/**
 * Thin fetch wrapper around the AutoSDR REST API.
 *
 * Conventions:
 * - Every path here is relative to `/api/...`; Vite proxies that prefix
 *   to the FastAPI process in dev and the backend serves the UI itself
 *   in prod (single-process boot).
 * - The backend returns `409 { setup_required: true }` when the workspace
 *   row is missing. `req()` intercepts that and redirects to `/settings`,
 *   where the setup form is embedded without blocking the rest of the app.
 * - The one exception is `getSetupStatus`, which hits the setup endpoint
 *   directly and never triggers the redirect.
 */

import type {
  AngleFunnel,
  Campaign,
  CampaignAssignLeadsResult,
  CampaignKickoffResult,
  CampaignTimeseries,
  ConnectorTestRequest,
  ConnectorTestResult,
  EnrichmentFilter,
  FollowupConfig,
  ImportCommit,
  ImportPreview,
  MappingConfig,
  Lead,
  DevSimInboundResult,
  LeadEnrichResult,
  LeadList,
  LlmCall,
  LlmCallsSummary,
  LlmPresetCatalog,
  Message,
  NetworkingStatus,
  OutreachWindowConfig,
  PushSubscriptionsResponse,
  PushTestResult,
  PushVapidPublic,
  ScanDetail,
  ScanList,
  ScanRunRequest,
  ScanRunResult,
  ScanSummary,
  SendsByDay,
  SetupPayload,
  SetupStatus,
  Suggestion,
  SystemStatus,
  Thread,
  Workspace,
  WorkspaceSettings,
} from './types';

export class ApiError extends Error {
  readonly status: number;
  readonly payload: unknown;
  constructor(status: number, message: string, payload: unknown = null) {
    super(message);
    this.status = status;
    this.payload = payload;
    this.name = 'ApiError';
  }
}

type ReqInit = Omit<RequestInit, 'body'> & {
  body?: unknown;
  query?: Record<string, string | number | boolean | null | undefined>;
  // Bypass the 409 → /setup redirect. Only the setup page itself needs
  // this, to actually get the 409 body and handle it locally.
  skipSetupRedirect?: boolean;
};

function buildUrl(path: string, query?: ReqInit['query']): string {
  const base = `/api${path.startsWith('/') ? path : `/${path}`}`;
  if (!query) return base;
  const params = new URLSearchParams();
  for (const [key, value] of Object.entries(query)) {
    if (value === null || value === undefined || value === '') continue;
    params.set(key, String(value));
  }
  const qs = params.toString();
  return qs ? `${base}?${qs}` : base;
}

async function req<T>(path: string, init: ReqInit = {}): Promise<T> {
  const { body, query, headers, skipSetupRedirect, ...rest } = init;
  const hasBody = body !== undefined;
  const isFormData = typeof FormData !== 'undefined' && body instanceof FormData;

  const finalHeaders: Record<string, string> = { ...(headers as Record<string, string>) };
  if (hasBody && !isFormData && !finalHeaders['Content-Type']) {
    finalHeaders['Content-Type'] = 'application/json';
  }

  const res = await fetch(buildUrl(path, query), {
    ...rest,
    headers: finalHeaders,
    body: hasBody ? (isFormData ? (body as FormData) : JSON.stringify(body)) : undefined,
  });

  if (res.status === 409 && !skipSetupRedirect) {
    let body409: unknown = null;
    try {
      body409 = await res.clone().json();
    } catch {
      // non-JSON 409 — fall through as a normal error
    }
    if (body409 && typeof body409 === 'object' && (body409 as { setup_required?: boolean }).setup_required) {
      if (typeof window !== 'undefined' && !window.location.pathname.startsWith('/settings')) {
        window.location.href = '/settings';
      }
      throw new ApiError(409, 'setup_required', body409);
    }
  }

  if (!res.ok) {
    let payload: unknown;
    try {
      payload = await res.json();
    } catch {
      payload = await res.text().catch(() => null);
    }
    throw new ApiError(res.status, `HTTP ${res.status} ${res.statusText}`, payload);
  }

  if (res.status === 204) return undefined as T;
  const ct = res.headers.get('content-type') ?? '';
  if (!ct.includes('application/json')) {
    return (await res.text()) as unknown as T;
  }
  return (await res.json()) as T;
}

export const api = {
  // ---------- setup ----------

  async getSetupStatus(): Promise<SetupStatus> {
    return req<SetupStatus>('/setup/required', { skipSetupRedirect: true });
  },

  async runSetup(payload: SetupPayload): Promise<Workspace> {
    return req<Workspace>('/setup', {
      method: 'POST',
      body: payload,
      skipSetupRedirect: true,
    });
  },

  // ---------- workspace ----------

  async getWorkspace(): Promise<Workspace> {
    return req<Workspace>('/workspace');
  },

  async patchWorkspace(
    patch: Partial<Pick<Workspace, 'business_name' | 'business_dump' | 'tone_prompt'>>,
  ): Promise<Workspace> {
    return req<Workspace>('/workspace', { method: 'PATCH', body: patch });
  },

  async patchWorkspaceSettings(patch: Partial<WorkspaceSettings> & Record<string, unknown>): Promise<Workspace> {
    return req<Workspace>('/workspace/settings', { method: 'PATCH', body: patch });
  },

  /**
   * Probe connector connectivity. Pass the current (possibly unsaved)
   * form state to test against those values; omit the payload to test
   * the live saved connector. The backend always returns 200 — failures
   * are encoded as ``ok: false`` with a human-readable ``detail``.
   */
  async testConnector(payload?: ConnectorTestRequest): Promise<ConnectorTestResult> {
    return req<ConnectorTestResult>('/workspace/connector/test', {
      method: 'POST',
      body: payload ?? {},
    });
  },

  // ---------- status ----------

  async getSystemStatus(): Promise<SystemStatus> {
    return req<SystemStatus>('/status');
  },

  async pause(): Promise<SystemStatus> {
    return req<SystemStatus>('/status/pause', { method: 'POST' });
  },

  async resume(): Promise<SystemStatus> {
    return req<SystemStatus>('/status/resume', { method: 'POST' });
  },

  async getNetworkingStatus(): Promise<NetworkingStatus> {
    return req<NetworkingStatus>('/status/networking');
  },

  // ---------- campaigns ----------

  async listCampaigns(): Promise<Campaign[]> {
    return req<Campaign[]>('/campaigns');
  },

  async getCampaign(id: string): Promise<Campaign> {
    return req<Campaign>(`/campaigns/${id}`);
  },

  async createCampaign(payload: {
    name: string;
    goal: string;
    outreach_per_day?: number;
    connector_type?: string;
    followup?: FollowupConfig;
    outreach_window?: OutreachWindowConfig | null;
  }): Promise<Campaign> {
    return req<Campaign>('/campaigns', { method: 'POST', body: payload });
  },

  async patchCampaign(
    id: string,
    payload: Partial<{
      name: string;
      goal: string;
      outreach_per_day: number;
      status: Campaign['status'];
      followup: FollowupConfig;
      /**
       * ``null`` clears the per-campaign override (= inherit the
       * workspace default). Omit the field entirely for "no change".
       * The PATCH body type uses ``Partial<>`` so omission still works
       * even with an explicit ``null`` allowed for the value.
       */
      outreach_window: OutreachWindowConfig | null;
    }>,
  ): Promise<Campaign> {
    return req<Campaign>(`/campaigns/${id}`, { method: 'PATCH', body: payload });
  },

  /**
   * Hard-delete a campaign and every conversation hanging off it. Leads
   * themselves are workspace-scoped and survive — only the assignment
   * + its threads + messages + LLM-call audit rows go away. Returns
   * ``undefined`` on success (HTTP 204).
   */
  async deleteCampaign(id: string): Promise<void> {
    return req<void>(`/campaigns/${id}`, { method: 'DELETE' });
  },

  async resetCampaignSendCount(id: string): Promise<Campaign> {
    return req<Campaign>(`/campaigns/${id}/reset-send-count`, { method: 'POST' });
  },

  async kickoffCampaign(id: string, count: number): Promise<CampaignKickoffResult> {
    return req<CampaignKickoffResult>(`/campaigns/${id}/kickoff`, {
      method: 'POST',
      body: { count },
    });
  },

  async activateCampaign(id: string): Promise<Campaign> {
    return req<Campaign>(`/campaigns/${id}/activate`, { method: 'POST' });
  },

  async pauseCampaign(id: string): Promise<Campaign> {
    return req<Campaign>(`/campaigns/${id}/pause`, { method: 'POST' });
  },

  async completeCampaign(id: string): Promise<Campaign> {
    return req<Campaign>(`/campaigns/${id}/complete`, { method: 'POST' });
  },

  async assignLeads(
    id: string,
    payload: { lead_ids?: string[]; all_eligible?: boolean },
  ): Promise<CampaignAssignLeadsResult> {
    return req<CampaignAssignLeadsResult>(`/campaigns/${id}/assign-leads`, {
      method: 'POST',
      body: payload,
    });
  },

  /**
   * Per-day funnel buckets for one campaign. The response is always
   * ``days`` rows long, oldest first, padded with zeroes — the chart
   * renders a stable window even on a brand-new campaign.
   */
  async getCampaignTimeseries(
    id: string,
    days: number = 14,
  ): Promise<CampaignTimeseries> {
    return req<CampaignTimeseries>(`/campaigns/${id}/timeseries`, {
      query: { days },
    });
  },

  // ---------- leads ----------

  /**
   * Paginated browse of every lead in the workspace.
   *
   * ``assignment`` narrows by campaign membership:
   *   - ``"in_campaign"`` — leads assigned to at least one campaign.
   *   - ``"unassigned"`` — leads not yet assigned to any campaign.
   *   - omitted / ``undefined`` — no membership filter.
   */
  async listLeads(opts?: {
    status?: string;
    assignment?: 'in_campaign' | 'unassigned';
    q?: string;
    limit?: number;
    offset?: number;
  }): Promise<LeadList> {
    return req<LeadList>('/leads', {
      query: {
        status_filter: opts?.status,
        assignment: opts?.assignment,
        q: opts?.q,
        limit: opts?.limit,
        offset: opts?.offset,
      },
    });
  },

  async getLead(id: string): Promise<Lead> {
    return req<Lead>(`/leads/${id}`);
  },

  async optOutLead(id: string, reason = 'manual'): Promise<Lead> {
    return req<Lead>(`/leads/${id}/opt-out`, {
      method: 'POST',
      body: { reason },
    });
  },

  async clearLeadOptOut(id: string): Promise<Lead> {
    return req<Lead>(`/leads/${id}/opt-out`, {
      method: 'DELETE',
    });
  },

  async enrichLeads(opts: {
    since_days?: number;
    limit?: number;
    dry_run?: boolean;
  }): Promise<LeadEnrichResult> {
    return req<LeadEnrichResult>('/leads/enrich', {
      method: 'POST',
      body: opts,
    });
  },

  async devSimInbound(payload: {
    contact_uri: string;
    content: string;
  }): Promise<DevSimInboundResult> {
    return req<DevSimInboundResult>('/dev/sim-inbound', {
      method: 'POST',
      body: payload,
    });
  },

  async previewImport(
    file: File,
    mappingConfig?: MappingConfig | null,
  ): Promise<ImportPreview> {
    const form = new FormData();
    form.append('file', file);
    if (mappingConfig) {
      form.append('mapping_config', JSON.stringify(mappingConfig));
    }
    return req<ImportPreview>('/leads/import/preview', {
      method: 'POST',
      body: form,
    });
  },

  async commitImport(
    file: File,
    mappingConfig?: MappingConfig | null,
  ): Promise<ImportCommit> {
    const form = new FormData();
    form.append('file', file);
    if (mappingConfig) {
      form.append('mapping_config', JSON.stringify(mappingConfig));
    }
    return req<ImportCommit>('/leads/import/commit', {
      method: 'POST',
      body: form,
    });
  },

  // ---------- threads ----------

  async listThreads(opts?: {
    status?: string;
    campaignId?: string;
    leadId?: string;
    dismissed?: boolean;
    limit?: number;
    offset?: number;
  }): Promise<Thread[]> {
    return req<Thread[]>('/threads', {
      query: {
        status_filter: opts?.status,
        campaign_id: opts?.campaignId,
        lead_id: opts?.leadId,
        dismissed: opts?.dismissed,
        limit: opts?.limit,
        offset: opts?.offset,
      },
    });
  },

  async getHitlCount(): Promise<{ active: number; dismissed: number }> {
    return req<{ active: number; dismissed: number }>('/threads/hitl/count');
  },

  async getThread(id: string): Promise<Thread> {
    return req<Thread>(`/threads/${id}`);
  },

  async listMessages(threadId: string): Promise<Message[]> {
    return req<Message[]>(`/threads/${threadId}/messages`);
  },

  async regenerateSuggestions(threadId: string): Promise<Thread> {
    return req<Thread>(`/threads/${threadId}/regenerate-suggestions`, {
      method: 'POST',
    });
  },

  async sendDraft(
    threadId: string,
    payload: { draft: string; source: 'ai_suggested' | 'manual' },
  ): Promise<Message> {
    return req<Message>(`/threads/${threadId}/send-draft`, {
      method: 'POST',
      body: payload,
    });
  },

  async takeOverThread(threadId: string, note?: string): Promise<Thread> {
    return req<Thread>(`/threads/${threadId}/take-over`, {
      method: 'POST',
      body: { note },
    });
  },

  async closeThread(threadId: string, outcome: 'won' | 'lost'): Promise<Thread> {
    return req<Thread>(`/threads/${threadId}/close`, {
      method: 'POST',
      body: { outcome },
    });
  },

  async dismissThread(threadId: string): Promise<Thread> {
    return req<Thread>(`/threads/${threadId}/dismiss`, { method: 'POST' });
  },

  async restoreThread(threadId: string): Promise<Thread> {
    return req<Thread>(`/threads/${threadId}/restore`, { method: 'POST' });
  },

  // ---------- llm calls ----------

  async listLlmCalls(opts?: {
    threadId?: string;
    campaignId?: string;
    leadId?: string;
    purpose?: string;
    errorsOnly?: boolean;
    limit?: number;
  }): Promise<LlmCall[]> {
    return req<LlmCall[]>('/llm-calls', {
      query: {
        thread_id: opts?.threadId,
        campaign_id: opts?.campaignId,
        lead_id: opts?.leadId,
        purpose: opts?.purpose,
        errors_only: opts?.errorsOnly,
        limit: opts?.limit,
      },
    });
  },

  async llmCallsSummary(opts?: {
    threadId?: string;
    campaignId?: string;
    leadId?: string;
    purpose?: string;
    errorsOnly?: boolean;
  }): Promise<LlmCallsSummary> {
    return req<LlmCallsSummary>('/llm-calls/summary', {
      query: {
        thread_id: opts?.threadId,
        campaign_id: opts?.campaignId,
        lead_id: opts?.leadId,
        purpose: opts?.purpose,
        errors_only: opts?.errorsOnly,
      },
    });
  },

  /**
   * Static catalog of Gemini-only model blends ("MAX / Balanced /
   * Cheap"). The Settings → LLM card uses this to render one-click
   * preset buttons. The server is the source of truth so a pricing or
   * model change ships everywhere at once. See ticket 0006.
   */
  async getLlmPresets(): Promise<LlmPresetCatalog> {
    return req<LlmPresetCatalog>('/llm/presets');
  },

  // ---------- stats ----------

  async getSends14d(): Promise<SendsByDay[]> {
    const data = await req<{ days: SendsByDay[] }>('/stats/sends-14d');
    return data.days;
  },

  /**
   * Reply-rate per personalisation angle. Drives the "By angle" panel
   * on `/Logs` and the campaign-scoped variant on `CampaignDetail`.
   *
   * Defaults: workspace-wide queries get a 30-day window;
   * campaign-scoped queries get the campaign's lifetime (no time filter)
   * unless ``sinceDays`` is supplied. The server echoes the resolved
   * ``since`` so the UI can label "(last N days)" honestly.
   */
  async getAngleFunnel(opts?: {
    campaignId?: string;
    sinceDays?: number;
    enrichment?: EnrichmentFilter;
  }): Promise<AngleFunnel> {
    return req<AngleFunnel>('/stats/angle-funnel', {
      query: {
        campaign_id: opts?.campaignId,
        since_days: opts?.sinceDays,
        // Only forward the param when set to something other than the
        // server default, so the URL stays cleaner for the common case.
        enrichment:
          opts?.enrichment && opts.enrichment !== 'all' ? opts.enrichment : undefined,
      },
    });
  },

  // ---------- scans ----------

  /**
   * Paginated browse of every lead's most recent website-enrichment
   * scan. Mirrors the Leads list shape but with scan-specific
   * filtering (status / search). Powers the ``/scans`` page.
   *
   * Defaults to leads currently assigned to at least one campaign —
   * the only ones we'd actually outreach. Set
   * ``includeUnassigned`` to surface every lead in the workspace
   * (useful for auditing a fresh import before kick-off).
   */
  async listScans(opts?: {
    status?: string;
    q?: string;
    includeUnassigned?: boolean;
    limit?: number;
    offset?: number;
  }): Promise<ScanList> {
    return req<ScanList>('/scans', {
      query: {
        status_filter: opts?.status,
        q: opts?.q,
        include_unassigned: opts?.includeUnassigned ? true : undefined,
        limit: opts?.limit,
        offset: opts?.offset,
      },
    });
  },

  async getScansSummary(opts?: {
    includeUnassigned?: boolean;
  }): Promise<ScanSummary> {
    return req<ScanSummary>('/scans/summary', {
      query: {
        include_unassigned: opts?.includeUnassigned ? true : undefined,
      },
    });
  },

  /**
   * Start/stop the enrichment worker or synchronously enrich one ``lead_id``.
   *
   * - ``{ enabled }`` toggles workspace enrichment (Scans page Start/Stop).
   * - ``{ lead_id }`` runs a single synchronous scan (detail "Re-scan now").
   */
  async runScans(payload: ScanRunRequest): Promise<ScanRunResult> {
    return req<ScanRunResult>('/scans/run', { method: 'POST', body: payload });
  },

  async getScan(leadId: string): Promise<ScanDetail> {
    return req<ScanDetail>(`/scans/${leadId}`);
  },

  // ---------- push notifications (ticket 0005) ----------

  async getPushVapidPublic(): Promise<PushVapidPublic> {
    return req<PushVapidPublic>('/push/vapid-public');
  },

  async listPushSubscriptions(): Promise<PushSubscriptionsResponse> {
    return req<PushSubscriptionsResponse>('/push/subscriptions');
  },

  async subscribePush(payload: {
    endpoint: string;
    keys: { p256dh: string; auth: string };
    user_agent?: string | null;
  }): Promise<PushSubscriptionsResponse['subscriptions'][number]> {
    return req('/push/subscribe', { method: 'POST', body: payload });
  },

  async unsubscribePush(endpoint: string): Promise<void> {
    return req<void>('/push/subscribe', {
      method: 'DELETE',
      body: { endpoint },
    });
  },

  async testPushNotification(endpoint?: string): Promise<PushTestResult> {
    return req<PushTestResult>('/push/test', {
      method: 'POST',
      body: endpoint ? { endpoint } : {},
    });
  },
};

export type Api = typeof api;
export type { Suggestion };
