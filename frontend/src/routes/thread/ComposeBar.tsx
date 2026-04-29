import { CheckCircle, Send, XCircle } from 'lucide-react';
import { Button } from '@/components/ui/Button';
import { Textarea } from '@/components/ui/Input';

interface Props {
  draft: string;
  onDraftChange: (v: string) => void;
  onSendManual: () => void;
  onClose: (outcome: 'won' | 'lost') => void;
  sending: boolean;
}

/**
 * Unified compose rail for the thread view. Handles "send as me" plus
 * the two close-out actions. Keyboard ⌘⏎ / Ctrl⏎ fires the manual send
 * — Shift⏎ remains a regular newline.
 */
export function ComposeBar({ draft, onDraftChange, onSendManual, onClose, sending }: Props) {
  const trimmed = draft.trim();
  return (
    <div className="border-t border-rule px-4 py-2.5 bg-paper shrink-0">
      <div className="flex items-end gap-2">
        <Textarea
          value={draft}
          onChange={(e) => onDraftChange(e.target.value)}
          placeholder="Type a reply as yourself…"
          rows={2}
          className="min-h-11 flex-1"
          onKeyDown={(e) => {
            if (e.key === 'Enter' && (e.metaKey || e.ctrlKey) && trimmed) {
              onSendManual();
            }
          }}
        />
        <Button
          variant="primary"
          size="sm"
          iconLeft={<Send className="h-3.5 w-3.5" strokeWidth={1.5} />}
          disabled={!trimmed || sending}
          onClick={onSendManual}
        >
          Send
        </Button>
      </div>
      <div className="flex items-center gap-3 mt-1.5 px-0.5">
        <span className="font-mono text-[10px] uppercase tracking-[0.14em] text-ink-faint">
          ⌘⏎ send · Shift⏎ newline
        </span>
        <div className="flex-1" />
        <button
          type="button"
          onClick={() => onClose('won')}
          className="inline-flex items-center gap-1 text-[11px] text-ink-muted hover:text-forest cursor-pointer"
        >
          <CheckCircle className="h-3 w-3" strokeWidth={1.5} />
          Mark won
        </button>
        <button
          type="button"
          onClick={() => onClose('lost')}
          className="inline-flex items-center gap-1 text-[11px] text-ink-muted hover:text-oxblood cursor-pointer"
        >
          <XCircle className="h-3 w-3" strokeWidth={1.5} />
          Mark lost
        </button>
      </div>
    </div>
  );
}
