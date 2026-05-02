import { AlertTriangle, Edit3, Send, Trash2 } from 'lucide-react';
import { HitlReason, type HitlReasonT } from '@/lib/types';
import { cn } from '@/lib/utils';
import { evalScoreTone } from '@/lib/format';

interface Props {
  reason: HitlReasonT | string;
  failedDraft: string;
  connectorError?: string | null;
  lastScore?: number | null;
  lastFeedback?: string | null;
  onRetry: (draft: string) => void;
  onEdit: (draft: string) => void;
  onDismiss: () => void;
  sending: boolean;
  dismissing: boolean;
}

/**
 * Action panel for paused threads that have a failed draft on file.
 *
 * Surfaces the actual draft the pipeline tried to send (or that failed
 * the evaluator) alongside the technical reason, plus one-click Retry /
 * Edit / Dismiss. Without this, operators land on the thread, see an
 * empty AI-suggestions strip, and have to retype the message — which is
 * how connector hiccups become stuck threads.
 */
export function HitlActionPanel({
  reason,
  failedDraft,
  connectorError,
  lastScore,
  lastFeedback,
  onRetry,
  onEdit,
  onDismiss,
  sending,
  dismissing,
}: Props) {
  const isConnectorError = reason === HitlReason.CONNECTOR_SEND_FAILED;
  const retryLabel = isConnectorError ? 'Retry send' : 'Send anyway';
  const heading = isConnectorError
    ? 'Last attempt was rejected by the connector'
    : 'Draft did not pass the evaluator';
  const scorePct =
    lastScore != null ? Math.round(lastScore * 100) : null;
  const scoreTone = scorePct != null ? evalScoreTone(scorePct) : 'neutral';

  return (
    <div className="border-t border-rust/40 bg-rust-soft/40 px-4 pt-2.5 pb-3 shrink-0">
      <div className="flex items-center gap-2 mb-1.5 px-1">
        <AlertTriangle className="h-3 w-3 text-rust" strokeWidth={1.75} />
        <span className="font-mono text-[10px] uppercase tracking-[0.14em] text-ink-muted">
          {heading}
        </span>
        {scorePct != null && (
          <span className={cn('px-1 border text-[10px] font-mono', SCORE_TONE[scoreTone])}>
            {scorePct}
          </span>
        )}
      </div>

      <div className="paper-card px-3 py-2 flex flex-col gap-1.5">
        <p className="text-xs leading-snug text-ink whitespace-pre-wrap">
          {failedDraft}
        </p>
        {(connectorError || lastFeedback) && (
          <div className="text-[11px] text-ink-muted leading-snug">
            {connectorError && (
              <span className="font-mono">{connectorError}</span>
            )}
            {!connectorError && lastFeedback && (
              <span className="italic">{lastFeedback}</span>
            )}
          </div>
        )}
        <div className="flex items-center gap-1 -mb-0.5 pt-0.5">
          <button
            type="button"
            onClick={() => onRetry(failedDraft)}
            disabled={sending}
            className="inline-flex items-center gap-1 px-2 h-6 text-[11px] bg-ink text-paper border border-ink hover:bg-rust hover:border-rust cursor-pointer disabled:opacity-40 disabled:cursor-not-allowed"
          >
            <Send className="h-3 w-3" strokeWidth={1.5} />
            {sending ? 'Sending…' : retryLabel}
          </button>
          <button
            type="button"
            onClick={() => onEdit(failedDraft)}
            className="inline-flex items-center gap-1 px-2 h-6 text-[11px] text-ink-muted hover:text-ink hover:bg-paper-deep cursor-pointer"
          >
            <Edit3 className="h-3 w-3" strokeWidth={1.5} />
            Edit & send
          </button>
          <div className="flex-1" />
          <button
            type="button"
            onClick={onDismiss}
            disabled={dismissing}
            className="inline-flex items-center gap-1 px-2 h-6 text-[11px] text-ink-muted hover:text-rust cursor-pointer disabled:opacity-40 disabled:cursor-not-allowed"
            title="Set this thread aside without sending"
          >
            <Trash2 className="h-3 w-3" strokeWidth={1.5} />
            Dismiss
          </button>
        </div>
      </div>
    </div>
  );
}

const SCORE_TONE: Record<string, string> = {
  forest: 'border-forest text-forest',
  mustard: 'border-mustard text-mustard',
  rust: 'border-rust text-rust',
  oxblood: 'border-oxblood text-oxblood',
  neutral: 'border-rule text-ink-muted',
};
