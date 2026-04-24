import { Edit3, RefreshCcw, Send, Sparkles } from 'lucide-react';
import { Badge } from '@/components/ui/Badge';
import { Button } from '@/components/ui/Button';
import { evalScoreTone } from '@/lib/format';
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
 * "Paused for you" card that fans out the 2-3 stashed AI drafts.
 *
 * A thread enters this state when an inbound reply lands while
 * `auto_reply_enabled` is off — `reply.py` captures the inbound,
 * classifies it, generates candidate drafts, evaluates them, and stashes
 * the whole set on `thread.hitl_context.suggestions`. The operator then
 * either sends one as-is, loads it into Compose to tweak, or asks for a
 * new batch via Regenerate.
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
    <div className="border-t border-rule px-8 py-5 bg-paper-deep/40">
      <div className="flex items-center justify-between mb-3">
        <div className="flex items-center gap-2">
          <Sparkles className="h-3.5 w-3.5 text-rust" strokeWidth={1.5} />
          <span className="text-sm font-medium">Suggested replies</span>
          {pausedForHitl && !empty && (
            <Badge tone="neutral" uppercase={false}>
              pick one, edit, or regenerate
            </Badge>
          )}
        </div>
        <Button
          variant="ghost"
          size="sm"
          iconLeft={<RefreshCcw className="h-3.5 w-3.5" strokeWidth={1.5} />}
          onClick={onRegenerate}
          disabled={regenerating}
        >
          {regenerating ? 'Regenerating…' : 'Regenerate'}
        </Button>
      </div>

      {empty && (
        <div className="text-sm text-ink-muted italic">
          {regenerating
            ? 'Running the generator…'
            : pausedForHitl
              ? 'No drafts on file yet. Click Regenerate to ask the model for a few options.'
              : 'Generate drafts for the next outbound, without waiting for an inbound.'}
        </div>
      )}

      {!empty && (
        <ol className="flex flex-col gap-3">
          {suggestions.map((s, idx) => (
            <SuggestionRow
              key={s.gen_llm_call_id ?? `${idx}`}
              index={idx}
              suggestion={s}
              onSend={() => onSend(s.draft)}
              onEdit={() => onEdit(s.draft)}
              disabled={sendingDraft}
            />
          ))}
        </ol>
      )}
    </div>
  );
}

function SuggestionRow({
  index,
  suggestion,
  onSend,
  onEdit,
  disabled,
}: {
  index: number;
  suggestion: Suggestion;
  onSend: () => void;
  onEdit: () => void;
  disabled: boolean;
}) {
  const score = Math.round((suggestion.overall ?? 0) * 100);
  const tone = evalScoreTone(score);
  return (
    <li className="paper-card-deep px-4 py-3 flex flex-col gap-3">
      <div className="flex items-center gap-2 text-[11px] font-mono uppercase tracking-[0.14em] text-ink-muted">
        <span>Option {index + 1}</span>
        <span className="text-ink-faint">·</span>
        <Badge tone={tone} uppercase={false}>
          eval {score}
        </Badge>
        {suggestion.pass && (
          <Badge tone="forest" uppercase={false}>
            passed
          </Badge>
        )}
        {suggestion.temperature != null && (
          <span className="text-ink-faint">temp {suggestion.temperature.toFixed(2)}</span>
        )}
      </div>
      <p className="text-sm leading-relaxed text-ink">{suggestion.draft}</p>
      {suggestion.feedback && (
        <div className="text-xs text-ink-muted italic leading-snug">{suggestion.feedback}</div>
      )}
      <div className="flex items-center gap-2">
        <Button
          variant="primary"
          size="sm"
          iconLeft={<Send className="h-3.5 w-3.5" strokeWidth={1.5} />}
          onClick={onSend}
          disabled={disabled}
        >
          Send this
        </Button>
        <Button
          variant="ghost"
          size="sm"
          iconLeft={<Edit3 className="h-3.5 w-3.5" strokeWidth={1.5} />}
          onClick={onEdit}
        >
          Edit
        </Button>
      </div>
    </li>
  );
}
