import { useQuery } from '@tanstack/react-query';
import { useMemo, useState } from 'react';
import { Link, useSearchParams } from 'react-router-dom';
import { ChevronDown, ChevronRight } from 'lucide-react';
import { api } from '@/lib/api';
import { BackLink } from '@/components/ui/BackLink';
import { FilterTabs, type FilterOption } from '@/components/ui/FilterTabs';
import { PageHeader } from '@/components/ui/PageHeader';
import { SearchInput } from '@/components/ui/SearchInput';
import { absTime } from '@/lib/format';
import type { LlmCall, LlmCallPurposeT } from '@/lib/types';
import { cn } from '@/lib/utils';

const PURPOSES: ReadonlyArray<FilterOption<LlmCallPurposeT | 'all'>> = [
  { id: 'all', label: 'All' },
  { id: 'analysis', label: 'Analysis' },
  { id: 'generation', label: 'Generation' },
  { id: 'evaluation', label: 'Evaluation' },
  { id: 'classification', label: 'Classification' },
];

const PURPOSE_TONE: Record<LlmCallPurposeT, string> = {
  analysis: 'bg-teal-soft text-teal',
  generation: 'bg-mustard-soft text-mustard',
  evaluation: 'bg-rust-soft text-rust-deep',
  classification: 'bg-forest-soft text-forest',
  other: 'bg-paper-deep text-ink-muted',
};

/**
 * LLM call audit log. A row per call with prompt/response expansion
 * inline; an optional `?thread=` filter deep-links from ThreadDetail.
 */
export function Logs() {
  const [params, setParams] = useSearchParams();
  const threadFilter = params.get('thread');
  const [purpose, setPurpose] = useState<LlmCallPurposeT | 'all'>('all');
  const [q, setQ] = useState('');
  const [expanded, setExpanded] = useState<Set<string>>(new Set());

  const { data: calls } = useQuery({
    queryKey: ['llm-calls', threadFilter],
    queryFn: () => api.listLlmCalls({ threadId: threadFilter ?? undefined, limit: 200 }),
  });
  const { data: threads } = useQuery({
    queryKey: ['threads'],
    queryFn: () => api.listThreads({ limit: 500 }),
    enabled: !!threadFilter,
  });

  const threadName = useMemo(() => {
    if (!threadFilter || !threads) return null;
    return threads.find((t) => t.id === threadFilter)?.lead_name ?? 'Unknown thread';
  }, [threadFilter, threads]);

  const filtered = useMemo(() => {
    if (!calls) return [];
    let out: LlmCall[] = calls;
    if (purpose !== 'all') out = out.filter((c) => c.purpose === purpose);
    if (q.trim()) {
      const s = q.trim().toLowerCase();
      out = out.filter(
        (c) =>
          c.model.toLowerCase().includes(s) ||
          (c.prompt_version ?? '').toLowerCase().includes(s) ||
          (c.response_text ?? '').toLowerCase().includes(s),
      );
    }
    return out;
  }, [calls, purpose, q]);

  const counts = useMemo(() => {
    const m = new Map<string, number>();
    m.set('all', calls?.length ?? 0);
    calls?.forEach((c) => m.set(c.purpose, (m.get(c.purpose) ?? 0) + 1));
    return m;
  }, [calls]);

  const toggle = (id: string) => {
    setExpanded((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  };

  return (
    <div className="page gap-4">
      {threadFilter && (
        <BackLink onClick={() => setParams({})}>Clear thread filter</BackLink>
      )}

      <PageHeader
        title={
          threadFilter ? (
            <>
              Calls for <span className="text-rust">{threadName ?? threadFilter.slice(0, 8)}</span>
            </>
          ) : (
            'LLM calls'
          )
        }
        description="One row per LLM attempt — analysis, generation, evaluation, classification. Click to expand the full prompt and response."
        right={
          <SearchInput
            value={q}
            onChange={setQ}
            placeholder="Search prompts, models…"
            className="w-60"
          />
        }
      />

      <FilterTabs options={PURPOSES} active={purpose} onChange={setPurpose} counts={counts} />

      <div className="paper-card font-mono">
        <div className="grid grid-cols-12 text-[10px] tracking-[0.14em] uppercase text-ink-muted px-3 py-2.5 border-b border-rule bg-paper-deep">
          <div className="col-span-2">Time</div>
          <div className="col-span-2">Purpose</div>
          <div className="col-span-2">Model</div>
          <div className="col-span-2">Prompt</div>
          <div className="col-span-1 text-right">Tokens</div>
          <div className="col-span-1 text-right">Latency</div>
          <div className="col-span-1 text-right">Attempt</div>
          <div className="col-span-1 text-right">Thread</div>
        </div>
        {filtered.map((c) => {
          const isOpen = expanded.has(c.id);
          return (
            <div key={c.id} className="border-b border-rule last:border-0">
              <button
                onClick={() => toggle(c.id)}
                className="w-full grid grid-cols-12 items-center text-xs px-3 py-2.5 text-left hover:bg-paper-deep cursor-pointer"
              >
                <div className="col-span-2 flex items-center gap-1.5 text-ink-muted">
                  {isOpen ? (
                    <ChevronDown className="h-3 w-3 shrink-0" strokeWidth={1.5} />
                  ) : (
                    <ChevronRight className="h-3 w-3 shrink-0" strokeWidth={1.5} />
                  )}
                  {absTime(c.created_at, 'd MMM HH:mm:ss')}
                </div>
                <div className="col-span-2">
                  <span
                    className={cn(
                      'px-1.5 py-px text-[10px] tracking-[0.14em] uppercase',
                      PURPOSE_TONE[c.purpose],
                    )}
                  >
                    {c.purpose}
                  </span>
                </div>
                <div className="col-span-2 text-ink truncate">{c.model}</div>
                <div className="col-span-2 text-ink-muted">{c.prompt_version ?? '—'}</div>
                <div className="col-span-1 text-right text-ink-muted tabular-nums">
                  {c.tokens_in}→{c.tokens_out}
                </div>
                <div className="col-span-1 text-right text-ink-muted tabular-nums">
                  {c.latency_ms}ms
                </div>
                <div className="col-span-1 text-right text-ink-muted tabular-nums">
                  {c.attempt}
                </div>
                <div className="col-span-1 text-right">
                  {c.thread_id && (
                    <Link
                      to={`/threads/${c.thread_id}`}
                      className="text-ink-muted hover:text-rust underline"
                      onClick={(e) => e.stopPropagation()}
                    >
                      {c.thread_id.slice(0, 8)}
                    </Link>
                  )}
                </div>
              </button>

              {isOpen && (
                <div className="grid grid-cols-2 gap-6 p-5 bg-paper-deep border-t border-rule font-mono text-xs">
                  <div>
                    <div className="label mb-2">Response</div>
                    <pre className="whitespace-pre-wrap leading-relaxed text-ink">
                      {c.response_text ||
                        (c.response_parsed ? JSON.stringify(c.response_parsed, null, 2) : '—')}
                    </pre>
                  </div>
                  <div className="flex flex-col gap-4">
                    <div>
                      <div className="label mb-2">Linked to</div>
                      <dl className="grid grid-cols-[1fr_2fr] gap-y-1.5 text-[11px]">
                        <dt className="text-ink-muted uppercase tracking-[0.14em]">workspace</dt>
                        <dd className="text-ink truncate">{c.workspace_id ?? '—'}</dd>
                        <dt className="text-ink-muted uppercase tracking-[0.14em]">campaign</dt>
                        <dd className="text-ink truncate">{c.campaign_id ?? '—'}</dd>
                        <dt className="text-ink-muted uppercase tracking-[0.14em]">thread</dt>
                        <dd className="text-ink truncate">{c.thread_id ?? '—'}</dd>
                        <dt className="text-ink-muted uppercase tracking-[0.14em]">lead</dt>
                        <dd className="text-ink truncate">{c.lead_id ?? '—'}</dd>
                        <dt className="text-ink-muted uppercase tracking-[0.14em]">call id</dt>
                        <dd className="text-ink truncate">{c.id}</dd>
                        <dt className="text-ink-muted uppercase tracking-[0.14em]">format</dt>
                        <dd className="text-ink">{c.response_format}</dd>
                        {c.temperature != null && (
                          <>
                            <dt className="text-ink-muted uppercase tracking-[0.14em]">temp</dt>
                            <dd className="text-ink">{c.temperature}</dd>
                          </>
                        )}
                      </dl>
                    </div>
                    {c.error && (
                      <div className="p-3 bg-oxblood-soft border border-oxblood text-oxblood">
                        <div className="label text-oxblood mb-1">Error</div>
                        <div className="text-xs">{c.error}</div>
                      </div>
                    )}
                  </div>
                </div>
              )}
            </div>
          );
        })}
        {filtered.length === 0 && (
          <div className="py-14 text-center text-ink-muted text-sm">No calls match.</div>
        )}
      </div>
    </div>
  );
}
