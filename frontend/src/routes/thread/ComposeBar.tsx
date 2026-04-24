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
    <div className="border-t border-rule px-8 py-4 bg-paper">
      <div className="flex items-center justify-between mb-2">
        <span className="label">Compose</span>
        <span className="font-mono text-[10px] uppercase tracking-[0.14em] text-ink-faint">
          ⌘⏎ send · Shift⏎ newline
        </span>
      </div>
      <div className="flex flex-col gap-3">
        <Textarea
          value={draft}
          onChange={(e) => onDraftChange(e.target.value)}
          placeholder="Type a reply as yourself…"
          rows={3}
          onKeyDown={(e) => {
            if (e.key === 'Enter' && (e.metaKey || e.ctrlKey) && trimmed) {
              onSendManual();
            }
          }}
        />
        <div className="flex items-center gap-2">
          <Button
            variant="primary"
            iconLeft={<Send className="h-3.5 w-3.5" strokeWidth={1.5} />}
            disabled={!trimmed || sending}
            onClick={onSendManual}
          >
            Send manually
          </Button>
          <div className="flex-1" />
          <Button
            variant="ghost"
            iconLeft={<CheckCircle className="h-3.5 w-3.5" strokeWidth={1.5} />}
            onClick={() => onClose('won')}
          >
            Mark won
          </Button>
          <Button
            variant="ghost"
            iconLeft={<XCircle className="h-3.5 w-3.5" strokeWidth={1.5} />}
            onClick={() => onClose('lost')}
          >
            Mark lost
          </Button>
        </div>
      </div>
    </div>
  );
}
