import { useQuery } from '@tanstack/react-query';
import { Link } from 'react-router-dom';
import { ArrowRight } from 'lucide-react';
import { api } from '@/lib/api';
import { Badge } from '@/components/ui/Badge';
import { HITL_LABEL, INTENT_LABEL, evalScoreTone, relTime } from '@/lib/format';
import { ThreadStatus, type Thread } from '@/lib/types';

/**
 * Triage view for threads awaiting a human reply.
 *
 * Every card shows the inbound message, how the classifier read it, and
 * the top AI-drafted suggestion with its eval score. Clicking opens the
 * thread where the operator can send, edit, or regenerate.
 */
export function Inbox() {
  const { data: hitl, isLoading } = useQuery({
    queryKey: ['threads', { status: ThreadStatus.PAUSED_FOR_HITL, limit: 100 }],
    queryFn: () =>
      api.listThreads({ status: ThreadStatus.PAUSED_FOR_HITL, limit: 100 }),
  });

  return (
    <div className="page-narrow gap-6">
      <header className="flex items-baseline justify-between border-b border-rule pb-4">
        <div>
          <h1 className="text-2xl font-medium">
            {hitl && hitl.length > 0 ? `${hitl.length} thread${hitl.length === 1 ? '' : 's'} waiting` : 'All clear'}
          </h1>
          <p className="text-sm text-ink-muted mt-1 max-w-prose">
            First-message-only mode: AutoSDR never answers a reply without you picking a draft.
          </p>
        </div>
      </header>

      {isLoading && <div className="text-sm text-ink-muted">Loading…</div>}

      {hitl && hitl.length > 0 && (
        <ul className="paper-card divide-y divide-rule">
          {hitl.map((t) => (
            <li key={t.id}>
              <HitlRow thread={t} />
            </li>
          ))}
        </ul>
      )}

      {hitl && hitl.length === 0 && !isLoading && (
        <div className="paper-card px-6 py-12 text-center">
          <div className="text-sm font-medium mb-1">Nothing to look at.</div>
          <p className="text-sm text-ink-muted">
            New lead replies will show up here the moment they arrive.{' '}
            <Link to="/threads" className="underline">
              Browse all threads
            </Link>
            .
          </p>
        </div>
      )}
    </div>
  );
}

function HitlRow({ thread }: { thread: Thread }) {
  const ctx = thread.hitl_context as
    | (Thread['hitl_context'] & {
        last_drafts?: string[];
        last_scores?: { overall?: number; feedback?: string | null }[];
      })
    | null;

  const topSuggestion = ctx?.suggestions?.[0];

  // Fallback preview for eval_failed / connector_failed threads: they
  // don't carry `suggestions`, but `last_drafts` + `last_scores` are
  // stashed so the operator can see the best attempt at a glance.
  const lastDrafts = ctx?.last_drafts ?? [];
  const lastScores = ctx?.last_scores ?? [];
  const lastIdx = lastDrafts.length - 1;
  const fallbackDraft = lastIdx >= 0 ? lastDrafts[lastIdx] : null;
  const fallbackScore =
    lastIdx >= 0 && lastScores[lastIdx]?.overall != null
      ? Math.round((lastScores[lastIdx]!.overall as number) * 100)
      : null;

  const previewDraft = topSuggestion?.draft ?? fallbackDraft;
  const previewScore = topSuggestion
    ? Math.round((topSuggestion.overall ?? 0) * 100)
    : fallbackScore;
  const previewLabel = topSuggestion ? 'top draft' : 'last attempt';
  const scoreTone = evalScoreTone(previewScore);

  return (
    <Link
      to={`/threads/${thread.id}`}
      className="block px-5 py-4 hover:bg-paper-deep transition-colors group"
    >
      <div className="flex items-baseline justify-between gap-4 mb-2">
        <div className="flex items-baseline gap-3 min-w-0">
          <span className="text-sm font-medium truncate">
            {thread.lead_name ?? 'Unknown lead'}
          </span>
          <span className="text-xs text-ink-muted truncate">{thread.campaign_name}</span>
        </div>
        <div className="flex items-center gap-2 shrink-0">
          <Badge tone="rust" uppercase={false}>
            {thread.hitl_reason ? (HITL_LABEL[thread.hitl_reason] ?? 'Needs you') : 'Needs you'}
          </Badge>
          <span className="text-xs text-ink-faint font-mono">
            {relTime(thread.last_message_at)}
          </span>
        </div>
      </div>

      {ctx?.incoming_message && (
        <p className="text-sm text-ink-muted mb-2 line-clamp-2">
          <span className="text-ink-faint">they said:</span> {ctx.incoming_message}
        </p>
      )}

      {previewDraft && (
        <div className="flex items-start gap-3 pt-2 mt-1 border-t border-rule">
          <Badge tone={scoreTone} uppercase={false} className="shrink-0">
            {previewLabel}
            {previewScore != null ? ` · ${previewScore}` : ''}
          </Badge>
          <p className="text-sm text-ink line-clamp-2 flex-1">{previewDraft}</p>
          <ArrowRight
            className="h-4 w-4 text-ink-muted shrink-0 mt-0.5 group-hover:translate-x-0.5 transition-transform"
            strokeWidth={1.5}
          />
        </div>
      )}

      {!previewDraft && ctx?.intent && (
        <div className="text-xs text-ink-muted mt-1">
          classified as{' '}
          <span className="text-rust">{INTENT_LABEL[ctx.intent] ?? ctx.intent}</span>
          {ctx.confidence != null && (
            <span className="text-ink-faint"> · {Math.round(ctx.confidence * 100)}% confidence</span>
          )}
        </div>
      )}
    </Link>
  );
}
