/**
 * Thin fetch wrapper around the AutoSDR REST API.
 *
 * Conventions:
 * - Every path here is relative to `/api/...`; Vite proxies that prefix
 *   to the FastAPI process in dev and the backend serves the UI itself
 *   in prod (single-process boot).
 * - The backend returns `409 { setup_required: true }` when the workspace
 *   row is missing. `req()` intercepts that and redirects to `/setup` so
 *   callers don't need special-case handling — only the Setup route is
 *   allowed to see it, because it starts with `/setup` itself.
 * - The one exception is `getSetupStatus`, which hits the setup endpoint
 *   directly and never triggers the redirect.
 */

import type {
  Campaign,
  CampaignAssignLeadsResult,
  CampaignKickoffResult,
  ConnectorTestRequest,
  ConnectorTestResult,
  FollowupConfig,
  ImportCommit,
  ImportPreview,
  Lead,
  LeadList,
  LlmCall,
  Message,
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
      if (typeof window !== 'undefined' && !window.location.pathname.startsWith('/setup')) {
        window.location.href = '/setup';
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
    }>,
  ): Promise<Campaign> {
    return req<Campaign>(`/campaigns/${id}`, { method: 'PATCH', body: payload });
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

  // ---------- leads ----------

  async listLeads(opts?: {
    status?: string;
    q?: string;
    limit?: number;
    offset?: number;
  }): Promise<LeadList> {
    return req<LeadList>('/leads', {
      query: {
        status_filter: opts?.status,
        q: opts?.q,
        limit: opts?.limit,
        offset: opts?.offset,
      },
    });
  },

  async getLead(id: string): Promise<Lead> {
    return req<Lead>(`/leads/${id}`);
  },

  async previewImport(file: File): Promise<ImportPreview> {
    const form = new FormData();
    form.append('file', file);
    return req<ImportPreview>('/leads/import/preview', {
      method: 'POST',
      body: form,
    });
  },

  async commitImport(file: File): Promise<ImportCommit> {
    const form = new FormData();
    form.append('file', file);
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

  // ---------- stats ----------

  async getSends14d(): Promise<SendsByDay[]> {
    const data = await req<{ days: SendsByDay[] }>('/stats/sends-14d');
    return data.days;
  },
};

export type Api = typeof api;
export type { Suggestion };
