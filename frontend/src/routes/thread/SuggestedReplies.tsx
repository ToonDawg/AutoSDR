import { memo } from 'react';
import { Edit3, RefreshCcw, Send, Sparkles } from 'lucide-react';
import { evalScoreTone } from '@/lib/format';
import { cn } from '@/lib/utils';
import type { Suggestion } from '@/lib/types';

interface Props {
  suggestions: Suggestion[];
  pausedForHitl: boolean;
  onSend: (draft: string) => void;
  onEdit: (draft: string) => void;
  onRegenerate: () => void;
  regenerating: boolean;
  sendingDraft: boolean;
}

/**
 * Smart-reply strip above the compose bar.
 *
 * Patterned after Messenger / Gmail Smart Reply: a thin, horizontally
 * scrollable row of compact cards. Operator scans the drafts at a
 * glance, taps Send to fire one, or Edit to load it into Compose for
 * tweaking.
 *
 * A thread enters this state when an inbound reply lands while
 * `auto_reply_enabled` is off — `reply.py` captures the inbound,
 * classifies it, generates candidate drafts, evaluates them, and stashes
 * the whole set on `thread.hitl_context.suggestions`.
 */
export function SuggestedReplies({
  suggestions,
  pausedForHitl,
  onSend,
  onEdit,
  onRegenerate,
  regenerating,
  sendingDraft,
}: Props) {
  const empty = suggestions.length === 0;

  return (
    <div className="border-t border-rule px-4 pt-2 pb-2.5 bg-paper-deep/40 shrink-0">
      <div className="flex items-center gap-2 mb-1.5 px-1">
        <Sparkles className="h-3 w-3 text-rust" strokeWidth={1.5} />
        <span className="font-mono text-[10px] uppercase tracking-[0.14em] text-ink-muted">
          AI suggestions
        </span>
        <div className="flex-1" />
        <button
          type="button"
          onClick={onRegenerate}
          disabled={regenerating}
          className="inline-flex items-center gap-1 text-[11px] text-ink-muted hover:text-ink cursor-pointer disabled:opacity-40 disabled:cursor-not-allowed"
        >
          <RefreshCcw
            className={cn('h-3 w-3', regenerating && 'animate-spin')}
            strokeWidth={1.5}
          />
          {regenerating ? 'Regenerating…' : 'Regenerate'}
        </button>
      </div>

      {empty ? (
        <div className="text-xs text-ink-muted italic px-1 py-1.5">
          {regenerating
            ? 'Running the generator…'
            : pausedForHitl
              ? 'No drafts on file. Click Regenerate to ask the model.'
              : 'Generate drafts for the next outbound, without waiting for an inbound.'}
        </div>
      ) : (
        <ol className="flex gap-2 overflow-x-auto pb-1 px-1 snap-x snap-mandatory">
          {suggestions.map((s, idx) => (
            <SuggestionChip
              key={s.gen_llm_call_id ?? `${idx}`}
              index={idx}
              suggestion={s}
              onSend={onSend}
              onEdit={onEdit}
              disabled={sendingDraft}
            />
          ))}
        </ol>
      )}
    </div>
  );
}

const SuggestionChip = memo(function SuggestionChip({
  index,
  suggestion,
  onSend,
  onEdit,
  disabled,
}: {
  index: number;
  suggestion: Suggestion;
  onSend: (draft: string) => void;
  onEdit: (draft: string) => void;
  disabled: boolean;
}) {
  const hasEvalScore = suggestion.overall != null;
  const score = hasEvalScore ? Math.round((suggestion.overall ?? 0) * 100) : null;
  const tone = score != null ? evalScoreTone(score) : 'neutral';
  return (
    <li className="paper-card w-72 shrink-0 snap-start px-3 py-2 flex flex-col gap-1.5">
      <div className="flex items-center gap-1.5 text-[10px] font-mono uppercase tracking-[0.12em] text-ink-muted">
        <span>#{index + 1}</span>
        {score != null && (
          <span className={cn('px-1 border', SCORE_TONE[tone])}>{score}</span>
        )}
        {suggestion.pass === true && (
          <span className="px-1 border border-forest text-forest">ok</span>
        )}
        {suggestion.temperature != null && (
          <span className="text-ink-faint">t {suggestion.temperature.toFixed(1)}</span>
        )}
      </div>
      <p className="text-xs leading-snug text-ink line-clamp-3 min-h-[2.6rem]">
        {suggestion.draft}
      </p>
      <div className="flex items-center gap-1 -mb-0.5">
        <button
          type="button"
          onClick={() => onSend(suggestion.draft)}
          disabled={disabled}
          className="inline-flex items-center gap-1 px-2 h-6 text-[11px] bg-ink text-paper border border-ink hover:bg-rust hover:border-rust cursor-pointer disabled:opacity-40 disabled:cursor-not-allowed"
        >
          <Send className="h-3 w-3" strokeWidth={1.5} />
          Send
        </button>
        <button
          type="button"
          onClick={() => onEdit(suggestion.draft)}
          className="inline-flex items-center gap-1 px-2 h-6 text-[11px] text-ink-muted hover:text-ink hover:bg-paper-deep cursor-pointer"
        >
          <Edit3 className="h-3 w-3" strokeWidth={1.5} />
          Edit
        </button>
      </div>
    </li>
  );
});

const SCORE_TONE: Record<string, string> = {
  forest: 'border-forest text-forest',
  mustard: 'border-mustard text-mustard',
  rust: 'border-rust text-rust',
  oxblood: 'border-oxblood text-oxblood',
  neutral: 'border-rule text-ink-muted',
};
