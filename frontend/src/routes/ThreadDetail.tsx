import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import { Link, useParams, useNavigate } from 'react-router-dom';
import { useState } from 'react';
import { ArrowLeft } from 'lucide-react';
import { api } from '@/lib/api';
import { MessageBubble } from '@/components/domain/MessageBubble';
import { AngleTag } from '@/components/domain/AngleTag';
import { ThreadStatusBadge } from '@/components/domain/ThreadStatusBadge';
import { Badge } from '@/components/ui/Badge';
import { HITL_LABEL, formatPhone, relTime } from '@/lib/format';
import { ThreadStatus, type Suggestion } from '@/lib/types';
import { SuggestedReplies } from './thread/SuggestedReplies';
import { ComposeBar } from './thread/ComposeBar';
import { LlmTrail } from './thread/LlmTrail';

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
  });
  const { data: messages } = useQuery({
    queryKey: ['messages', id],
    queryFn: () => api.listMessages(id),
    refetchInterval: 8_000,
  });
  const { data: campaign } = useQuery({
    queryKey: ['campaign', thread?.campaign_id],
    queryFn: () => (thread ? api.getCampaign(thread.campaign_id) : null),
    enabled: !!thread,
  });
  const { data: llmCalls } = useQuery({
    queryKey: ['llm-calls', id],
    queryFn: () => api.listLlmCalls({ threadId: id, limit: 12 }),
  });

  const sendDraft = useMutation({
    mutationFn: (payload: { draft: string; source: 'ai_suggested' | 'manual' }) =>
      api.sendDraft(id, payload),
    onSuccess: () => {
      setManualDraft('');
      qc.invalidateQueries({ queryKey: ['messages', id] });
      qc.invalidateQueries({ queryKey: ['thread', id] });
      qc.invalidateQueries({ queryKey: ['threads'] });
    },
  });

  const regenerate = useMutation({
    mutationFn: () => api.regenerateSuggestions(id),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['thread', id] });
    },
  });

  const close = useMutation({
    mutationFn: (outcome: 'won' | 'lost') => api.closeThread(id, outcome),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['thread', id] });
      qc.invalidateQueries({ queryKey: ['threads'] });
    },
  });

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

        {pausedForHitl && thread.hitl_reason && (
          <div className="px-8 py-3 border-b border-rust/40 bg-rust-soft/60 flex items-start gap-4">
            <Badge tone="rust" dot>
              Paused for you
            </Badge>
            <div className="flex-1 text-sm text-ink">
              {HITL_LABEL[thread.hitl_reason] ?? thread.hitl_reason}
              {thread.hitl_context?.incoming_message && (
                <div className="mt-1 text-sm text-ink-muted">
                  Last from lead: &ldquo;{thread.hitl_context.incoming_message}&rdquo;
                </div>
              )}
            </div>
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
            onSend={(draft) => sendDraft.mutate({ draft, source: 'ai_suggested' })}
            onEdit={(draft) => setManualDraft(draft)}
            onRegenerate={() => regenerate.mutate()}
            regenerating={regenerate.isPending}
            sendingDraft={sendDraft.isPending}
          />
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
