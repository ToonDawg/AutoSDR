import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import { Link, useParams, useNavigate } from 'react-router-dom';
import { useCallback, useState } from 'react';
import { AlertTriangle, ArrowLeft, RotateCcw, Trash2 } from 'lucide-react';
import { api, ApiError } from '@/lib/api';
import { MessageBubble } from '@/components/domain/MessageBubble';
import { AngleTag } from '@/components/domain/AngleTag';
import { ThreadStatusBadge } from '@/components/domain/ThreadStatusBadge';
import { Badge } from '@/components/ui/Badge';
import {
  HITL_LABEL,
  absTime,
  formatDoNotContactReason,
  formatPhone,
  relTime,
} from '@/lib/format';
import { MessageRole, ThreadStatus, type Suggestion } from '@/lib/types';
import { SuggestedReplies } from './thread/SuggestedReplies';
import { ComposeBar } from './thread/ComposeBar';
import { LlmTrail } from './thread/LlmTrail';

/** Turn the structured API error payload into an operator-friendly line. */
function sendDraftErrorMessage(err: unknown): string {
  if (err instanceof ApiError) {
    const body = err.payload as { error?: string; reason?: string } | null;
    const code = body?.error;
    if (code === 'system_shutting_down') {
      return 'AutoSDR is shutting down — your message was not sent.';
    }
    if (code === 'connector_send_failed') {
      return `Connector rejected the send${body?.reason ? `: ${body.reason}` : '.'}`;
    }
    if (code === 'empty_draft') {
      return 'Nothing to send — the draft is empty.';
    }
    if (code === 'lead_missing_contact_uri') {
      return 'This lead has no phone number on file.';
    }
    if (code) return code.replace(/_/g, ' ');
    return err.message;
  }
  return err instanceof Error ? err.message : 'Send failed.';
}

/**
 * Single-thread workspace.
 *
 * The whole page is centred on the "first-message-only" flow:
 *
 *   outreach sent → lead replies → AI classifies + generates 2-3 drafts
 *   → thread parks in `paused_for_hitl` with suggestions stashed on
 *   `hitl_context.suggestions` → human picks one, edits, or types their
 *   own → `POST /api/threads/:id/send-draft` pushes it out.
 *
 * This file is deliberately thin — it wires queries/mutations and
 * delegates to three focused children: `SuggestedReplies`, `ComposeBar`,
 * and `LlmTrail`.
 */
export function ThreadDetail() {
  const { id = '' } = useParams();
  const navigate = useNavigate();
  const qc = useQueryClient();
  const [manualDraft, setManualDraft] = useState('');

  const { data: thread } = useQuery({
    queryKey: ['thread', id],
    queryFn: () => api.getThread(id),
    enabled: !!id,
  });
  const { data: messages } = useQuery({
    queryKey: ['messages', id],
    queryFn: () => api.listMessages(id),
    refetchInterval: 8_000,
    enabled: !!id,
  });
  const { data: campaign } = useQuery({
    queryKey: ['campaign', thread?.campaign_id],
    queryFn: () => (thread ? api.getCampaign(thread.campaign_id) : null),
    enabled: !!thread,
  });
  const { data: lead } = useQuery({
    queryKey: ['lead', thread?.lead_id],
    queryFn: () => (thread ? api.getLead(thread.lead_id) : null),
    enabled: !!thread?.lead_id,
  });
  const { data: llmCalls } = useQuery({
    queryKey: ['llm-calls', id],
    queryFn: () => api.listLlmCalls({ threadId: id, limit: 12 }),
    enabled: !!id,
  });

  // Invalidate the specific thread record + the user-facing list views.
  // The campaign-scoped list and HITL list both move when a thread sends/closes.
  const invalidateAffectedThreadLists = () => {
    qc.invalidateQueries({ queryKey: ['thread', id] });
    qc.invalidateQueries({ queryKey: ['threads'], exact: true });
    qc.invalidateQueries({ queryKey: ['threads', 'hitl'] });
    if (thread?.campaign_id) {
      qc.invalidateQueries({
        queryKey: ['threads', 'campaign', thread.campaign_id],
      });
    }
  };

  const sendDraft = useMutation({
    mutationFn: (payload: { draft: string; source: 'ai_suggested' | 'manual' }) =>
      api.sendDraft(id, payload),
    onSuccess: () => {
      setManualDraft('');
      qc.invalidateQueries({ queryKey: ['messages', id] });
      invalidateAffectedThreadLists();
    },
  });
  const sendError = sendDraft.isError ? sendDraftErrorMessage(sendDraft.error) : null;

  const regenerate = useMutation({
    mutationFn: () => api.regenerateSuggestions(id),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['thread', id] });
    },
  });

  const close = useMutation({
    mutationFn: (outcome: 'won' | 'lost') => api.closeThread(id, outcome),
    onSuccess: () => {
      invalidateAffectedThreadLists();
    },
  });

  // Dismiss / restore live alongside take-over and close, but they don't
  // change the thread's outcome — the thread stays paused, it just stops
  // showing up in the operator's "Needs your eye" queue. A new HITL
  // event clears the flag automatically (see ``pause_thread_for_hitl``).
  const dismiss = useMutation({
    mutationFn: () => api.dismissThread(id),
    onSuccess: () => {
      invalidateAffectedThreadLists();
    },
  });
  const restore = useMutation({
    mutationFn: () => api.restoreThread(id),
    onSuccess: () => {
      invalidateAffectedThreadLists();
    },
  });

  const sendMutate = sendDraft.mutate;
  const handleSendSuggestion = useCallback(
    (draft: string) => sendMutate({ draft, source: 'ai_suggested' }),
    [sendMutate],
  );
  const handleEditSuggestion = useCallback(
    (draft: string) => setManualDraft(draft),
    [],
  );

  if (!thread) {
    return (
      <div className="px-8 py-10">
        <div className="h-4 bg-paper-deep animate-pulse w-48 mb-4" />
        <div className="h-12 bg-paper-deep animate-pulse w-2/3" />
      </div>
    );
  }

  const suggestions: Suggestion[] = thread.hitl_context?.suggestions ?? [];
  const pausedForHitl = thread.status === ThreadStatus.PAUSED_FOR_HITL;
  const showSuggestions = pausedForHitl || suggestions.length > 0;

  // When the thread was closed because the lead opted out, surface the
  // matched inbound + keyword instead of the generic "Closed lost" frame.
  // Detection: thread is LOST and the lead is DNC-flagged. The matched
  // message is the most recent inbound (role=lead) message.
  const optOutClosed =
    thread.status === ThreadStatus.LOST && !!lead?.do_not_contact_at;
  const optOutInbound = optOutClosed
    ? [...(messages ?? [])].reverse().find((m) => m.role === MessageRole.LEAD)
    : null;

  return (
    <div className="grid grid-cols-12 min-h-[calc(100vh-3rem)]">
      <div className="col-span-12 lg:col-span-8 xl:col-span-8 border-r border-rule flex flex-col min-w-0">
        <div className="border-b border-rule px-8 pt-5 pb-4">
          <button
            onClick={() => navigate(-1)}
            className="inline-flex items-center gap-1.5 text-[11px] text-ink-muted hover:text-ink cursor-pointer mb-3"
          >
            <ArrowLeft className="h-3 w-3" strokeWidth={1.5} />
            Back
          </button>
          <div className="flex items-baseline justify-between gap-4 mb-2">
            <h1 className="text-xl font-medium">{thread.lead_name ?? 'Unknown lead'}</h1>
            <ThreadStatusBadge status={thread.status} />
          </div>
          <div className="flex flex-wrap items-center gap-x-3 gap-y-1 text-xs text-ink-muted">
            <Link to={`/campaigns/${thread.campaign_id}`} className="hover:text-ink">
              {thread.campaign_name}
            </Link>
            <span className="text-ink-faint">·</span>
            <span>Started {relTime(thread.created_at)}</span>
            <span className="text-ink-faint">·</span>
            <span className="font-mono">{formatPhone(thread.lead_phone)}</span>
            {thread.lead_category && (
              <>
                <span className="text-ink-faint">·</span>
                <span>{thread.lead_category}</span>
              </>
            )}
          </div>
        </div>

        {optOutClosed && (
          <div className="px-8 py-3 border-b border-oxblood/40 bg-oxblood-soft/60 flex items-start gap-4">
            <Badge tone="oxblood" dot>
              Opted out
            </Badge>
            <div className="flex-1 text-sm text-ink">
              <div>
                {formatDoNotContactReason(lead?.do_not_contact_reason ?? null)}
                {lead?.do_not_contact_at && (
                  <span className="text-ink-muted">
                    {' '}
                    · {absTime(lead.do_not_contact_at)}
                  </span>
                )}
              </div>
              {optOutInbound?.content && (
                <div className="mt-1 text-sm text-ink-muted">
                  Lead message: &ldquo;{optOutInbound.content}&rdquo;
                </div>
              )}
            </div>
          </div>
        )}

        {pausedForHitl && thread.hitl_reason && (
          <div className="px-8 py-3 border-b border-rust/40 bg-rust-soft/60 flex items-start gap-4">
            <Badge tone={thread.hitl_dismissed_at ? 'neutral' : 'rust'} dot>
              {thread.hitl_dismissed_at ? 'Dismissed' : 'Paused for you'}
            </Badge>
            <div className="flex-1 text-sm text-ink">
              {HITL_LABEL[thread.hitl_reason] ?? thread.hitl_reason}
              {thread.hitl_dismissed_at && (
                <span className="text-ink-muted">
                  {' '}
                  · set aside {relTime(thread.hitl_dismissed_at)}
                </span>
              )}
              {thread.hitl_context?.incoming_message && (
                <div className="mt-1 text-sm text-ink-muted">
                  Last from lead: &ldquo;{thread.hitl_context.incoming_message}&rdquo;
                </div>
              )}
            </div>
            {thread.hitl_dismissed_at ? (
              <button
                type="button"
                onClick={() => restore.mutate()}
                disabled={restore.isPending}
                className="text-xs text-ink-muted hover:text-ink px-2 py-1 cursor-pointer disabled:opacity-40 disabled:cursor-not-allowed inline-flex items-center gap-1 shrink-0"
                title="Pull this thread back to the inbox"
              >
                <RotateCcw className="h-3 w-3" strokeWidth={1.5} />
                Restore
              </button>
            ) : (
              <button
                type="button"
                onClick={() => dismiss.mutate()}
                disabled={dismiss.isPending}
                className="text-xs text-ink-muted hover:text-rust px-2 py-1 cursor-pointer disabled:opacity-40 disabled:cursor-not-allowed inline-flex items-center gap-1 shrink-0"
                title="Set this thread aside without changing its outcome"
              >
                <Trash2 className="h-3 w-3" strokeWidth={1.5} />
                Dismiss
              </button>
            )}
          </div>
        )}

        <div className="flex-1 overflow-y-auto px-8 py-6">
          <div className="flex flex-col gap-6">
            {messages?.map((m) => (
              <MessageBubble key={m.id} message={m} leadName={thread.lead_name ?? 'Lead'} />
            ))}
            {(!messages || messages.length === 0) && (
              <div className="mx-auto max-w-md text-center py-16 flex flex-col items-center gap-3">
                <div className="text-sm font-medium">Nothing has gone out yet.</div>
                <p className="text-sm text-ink-muted leading-relaxed">
                  The scheduler will pick this lead up on its next tick, or you can draft a
                  message below.
                </p>
              </div>
            )}
          </div>
        </div>

        {showSuggestions && (
          <SuggestedReplies
            suggestions={suggestions}
            pausedForHitl={pausedForHitl}
            onSend={handleSendSuggestion}
            onEdit={handleEditSuggestion}
            onRegenerate={() => regenerate.mutate()}
            regenerating={regenerate.isPending}
            sendingDraft={sendDraft.isPending}
          />
        )}

        {sendError && (
          <div
            role="alert"
            className="border-t border-oxblood/30 bg-oxblood-soft px-8 py-3 flex items-start gap-2 text-sm text-oxblood"
          >
            <AlertTriangle className="h-4 w-4 mt-0.5 shrink-0" strokeWidth={1.75} />
            <div>
              <div className="font-medium">Send failed</div>
              <div className="text-xs mt-0.5">{sendError}</div>
            </div>
          </div>
        )}

        <ComposeBar
          draft={manualDraft}
          onDraftChange={setManualDraft}
          onSendManual={() => sendDraft.mutate({ draft: manualDraft, source: 'manual' })}
          onClose={(outcome) => close.mutate(outcome)}
          sending={sendDraft.isPending}
        />
      </div>

      <aside className="col-span-12 lg:col-span-4 xl:col-span-4 flex flex-col min-w-0 overflow-y-auto">
        <section className="px-6 py-5 border-b border-rule">
          <div className="label mb-2">Angle</div>
          <AngleTag angle={thread.angle} />
          {campaign && (
            <div className="mt-4 pt-4 border-t border-rule">
              <div className="label mb-1.5">Campaign goal</div>
              <p className="text-sm text-ink leading-relaxed">{campaign.goal}</p>
            </div>
          )}
        </section>

        <LlmTrail threadId={thread.id} calls={llmCalls} />

        <section className="px-6 py-5">
          <div className="label mb-3">Stats</div>
          <dl className="grid grid-cols-2 gap-y-2 gap-x-5 text-sm">
            <dt className="text-ink-muted">Messages sent</dt>
            <dd className="font-mono tabular-nums">{messages?.length ?? 0}</dd>
            <dt className="text-ink-muted">Connector</dt>
            <dd className="capitalize">{thread.connector_type}</dd>
            <dt className="text-ink-muted">Last activity</dt>
            <dd>{relTime(thread.last_message_at)}</dd>
          </dl>
        </section>
      </aside>
    </div>
  );
}
