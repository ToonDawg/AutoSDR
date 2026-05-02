import { History } from 'lucide-react';
import { Link } from 'react-router-dom';
import { absTime } from '@/lib/format';
import { cn } from '@/lib/utils';
import type { LlmCall } from '@/lib/types';

interface Props {
  threadId: string;
  calls: LlmCall[] | undefined;
}

/**
 * Right-rail LLM call log for this thread. We only show the eight most
 * recent; the full history link jumps to `/logs?thread=:id`.
 *
 * Layout:
 * - `≥lg`: rendered inline as a labeled section in the right rail.
 * - `<lg`: rendered inside a `<details>` disclosure, collapsed by
 *   default. The trail is debugging-grade context — operators don't
 *   need to see it on every visit, especially on a 390-px viewport
 *   where vertical real estate is precious.
 */
export function LlmTrail({ threadId, calls }: Props) {
  const rows = (calls ?? []).slice(0, 8);
  const body = (
    <ol className="flex flex-col">
      {rows.map((c) => (
        <li
          key={c.id}
          className="flex items-baseline gap-3 py-2 border-b border-rule last:border-0 text-[12px] font-mono"
        >
          <span className="text-ink-faint shrink-0 w-12">
            {absTime(c.created_at, 'HH:mm')}
          </span>
          <span
            className={cn(
              'shrink-0 uppercase tracking-[0.14em] text-[10px] px-1.5 py-px',
              c.error ? 'bg-oxblood-soft text-oxblood' : 'bg-paper-deep text-ink-muted',
            )}
          >
            {c.purpose}
          </span>
          <span className="flex-1" />
          <span className="text-ink-faint">
            {c.tokens_in}→{c.tokens_out}
          </span>
          <span className="text-ink-faint">{c.latency_ms}ms</span>
        </li>
      ))}
      {rows.length === 0 && (
        <li className="text-[12px] italic text-ink-faint py-2">No calls on this thread.</li>
      )}
    </ol>
  );

  return (
    <>
      {/* `<lg`: collapsed disclosure so the trail doesn't push the
           ComposeBar off the viewport on mobile. */}
      <details className="lg:hidden border-b border-rule">
        <summary className="px-4 md:px-6 py-4 cursor-pointer flex items-center justify-between min-h-[44px] select-none">
          <div className="label flex items-center gap-1.5">
            <History className="h-3 w-3" strokeWidth={1.5} />
            LLM trail
            {rows.length > 0 && (
              <span className="ml-1 font-mono text-[10px] text-ink-faint normal-case tracking-normal">
                ({rows.length})
              </span>
            )}
          </div>
          <Link
            to={`/logs?thread=${threadId}`}
            className="text-[10px] uppercase tracking-[0.14em] text-ink-muted hover:text-ink"
            onClick={(e) => e.stopPropagation()}
          >
            Full log →
          </Link>
        </summary>
        <div className="px-4 md:px-6 pb-5">{body}</div>
      </details>

      {/* `≥lg`: classic inline rail section. */}
      <section className="hidden lg:block px-6 py-5 border-b border-rule">
        <div className="flex items-center justify-between mb-3">
          <div className="label flex items-center gap-1.5">
            <History className="h-3 w-3" strokeWidth={1.5} />
            LLM trail
          </div>
          <Link
            to={`/logs?thread=${threadId}`}
            className="text-[10px] uppercase tracking-[0.14em] text-ink-muted hover:text-ink"
          >
            Full log →
          </Link>
        </div>
        {body}
      </section>
    </>
  );
}
