import { useQuery } from '@tanstack/react-query';
import { useVirtualizer } from '@tanstack/react-virtual';
import { useMemo, useRef, useState } from 'react';
import { Link, useSearchParams } from 'react-router-dom';
import { ChevronDown, ChevronRight } from 'lucide-react';
import { api } from '@/lib/api';
import { AngleFunnelPanel } from '@/components/domain/AngleFunnelPanel';
import { BackLink } from '@/components/ui/BackLink';
import { FilterTabs, type FilterOption } from '@/components/ui/FilterTabs';
import { PageHeader } from '@/components/ui/PageHeader';
import { SearchInput } from '@/components/ui/SearchInput';
import { absTime } from '@/lib/format';
import type {
  EnrichmentFilter,
  LlmCall,
  LlmCallPurposeT,
  LlmCallsSummary,
} from '@/lib/types';
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
 * inline; optional entity filters deep-link from thread, lead, and campaign views.
 */
export function Logs() {
  const [params, setParams] = useSearchParams();
  const threadFilter = params.get('thread');
  const campaignFilter = params.get('campaign');
  const leadFilter = params.get('lead');
  const enrichmentFilter = (params.get('enrichment') ?? 'all') as EnrichmentFilter;
  const [purpose, setPurpose] = useState<LlmCallPurposeT | 'all'>('all');
  const [q, setQ] = useState('');
  const [expanded, setExpanded] = useState<Set<string>>(new Set());

  const { data: calls } = useQuery({
    queryKey: ['llm-calls', { threadFilter, campaignFilter, leadFilter }],
    queryFn: () =>
      api.listLlmCalls({
        threadId: threadFilter ?? undefined,
        campaignId: campaignFilter ?? undefined,
        leadId: leadFilter ?? undefined,
        limit: 200,
      }),
  });
  // Server-aggregated total — the list endpoint caps at 200 rows, so summing
  // ``calls`` on the client would silently underreport spend on any older
  // workspace. Filters mirror the list query so the figure tracks whatever
  // scope the operator is currently viewing.
  const { data: summary } = useQuery({
    queryKey: ['llm-calls-summary', { threadFilter, campaignFilter, leadFilter }],
    queryFn: () =>
      api.llmCallsSummary({
        threadId: threadFilter ?? undefined,
        campaignId: campaignFilter ?? undefined,
        leadId: leadFilter ?? undefined,
      }),
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
  const filterLabel = useMemo(() => {
    if (threadFilter) return `thread ${threadName ?? threadFilter.slice(0, 8)}`;
    if (campaignFilter) return `campaign ${campaignFilter.slice(0, 8)}`;
    if (leadFilter) return `lead ${leadFilter.slice(0, 8)}`;
    return null;
  }, [campaignFilter, leadFilter, threadFilter, threadName]);

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

  // Variable-height virtualization: collapsed rows ~40px, expanded rows
  // are measured dynamically. limit:200 means most renders skip nearly
  // every row.
  const scrollRef = useRef<HTMLDivElement | null>(null);
  const rowVirtualizer = useVirtualizer({
    count: filtered.length,
    getScrollElement: () => scrollRef.current,
    estimateSize: (i) => (expanded.has(filtered[i]?.id ?? '') ? 360 : 40),
    overscan: 6,
  });

  return (
    <div className="page gap-4">
      {filterLabel && (
        <BackLink onClick={() => setParams({})}>Clear log filter</BackLink>
      )}

      <PageHeader
        title={
          filterLabel ? (
            <>
              Calls for <span className="text-rust">{filterLabel}</span>
            </>
          ) : (
            'LLM calls'
          )
        }
        description="One row per LLM attempt — analysis, generation, evaluation, classification. Click to expand the full prompt and response."
        right={
          <div className="flex items-center gap-4">
            <LlmCostTotal summary={summary} />
            <SearchInput
              value={q}
              onChange={setQ}
              placeholder="Search prompts, models…"
              className="w-60"
            />
          </div>
        }
      />

      <AngleFunnelPanel
        campaignId={campaignFilter ?? undefined}
        enrichment={enrichmentFilter}
        onEnrichmentChange={(next) => {
          const merged = new URLSearchParams(params);
          if (next === 'all') {
            merged.delete('enrichment');
          } else {
            merged.set('enrichment', next);
          }
          setParams(merged);
        }}
      />

      <FilterTabs options={PURPOSES} active={purpose} onChange={setPurpose} counts={counts} />

      <div className="paper-card font-mono">
        <div className="grid grid-cols-12 text-[10px] tracking-[0.14em] uppercase text-ink-muted px-3 py-2.5 border-b border-rule bg-paper-deep">
          <div className="col-span-2">Time</div>
          <div className="col-span-2">Purpose</div>
          <div className="col-span-2">Model</div>
          <div className="col-span-1">Prompt</div>
          <div className="col-span-1 text-right">Tokens</div>
          <div className="col-span-1 text-right">Cost</div>
          <div className="col-span-1 text-right">Latency</div>
          <div className="col-span-1 text-right">Attempt</div>
          <div className="col-span-1 text-right">Thread</div>
        </div>
        {filtered.length === 0 ? (
          <div className="py-14 text-center text-ink-muted text-sm">No calls match.</div>
        ) : (
          <div
            ref={scrollRef}
            className="overflow-auto"
            style={{ maxHeight: 'calc(100vh - 22rem)' }}
          >
            <div
              className="relative"
              style={{ height: rowVirtualizer.getTotalSize() }}
            >
              {rowVirtualizer.getVirtualItems().map((vRow) => {
                const c = filtered[vRow.index];
                const isOpen = expanded.has(c.id);
                return (
                  <div
                    key={c.id}
                    data-index={vRow.index}
                    ref={rowVirtualizer.measureElement}
                    className="absolute inset-x-0 border-b border-rule"
                    style={{ transform: `translateY(${vRow.start}px)` }}
                  >
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
                      <div className="col-span-1 text-ink-muted truncate">
                        {c.prompt_version ?? '—'}
                      </div>
                      <div className="col-span-1 text-right text-ink-muted tabular-nums">
                        {c.tokens_in}→{c.tokens_out}
                      </div>
                      <div
                        className="col-span-1 text-right text-ink-muted tabular-nums"
                        title={
                          c.cost_usd == null
                            ? 'No pricing data for this model'
                            : `Estimated cost at current pricing snapshot`
                        }
                      >
                        {c.cost_usd == null ? '—' : `$${c.cost_usd.toFixed(4)}`}
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
                              (c.response_parsed
                                ? JSON.stringify(c.response_parsed, null, 2)
                                : '—')}
                          </pre>
                        </div>
                        <div className="flex flex-col gap-4">
                          <div>
                            <div className="label mb-2">Linked to</div>
                            <dl className="grid grid-cols-[1fr_2fr] gap-y-1.5 text-[11px]">
                              <dt className="text-ink-muted uppercase tracking-[0.14em]">
                                workspace
                              </dt>
                              <dd className="text-ink truncate">{c.workspace_id ?? '—'}</dd>
                              <dt className="text-ink-muted uppercase tracking-[0.14em]">
                                campaign
                              </dt>
                              <dd className="text-ink truncate">
                                {c.campaign_id ? (
                                  <Link
                                    to={`/campaigns/${c.campaign_id}`}
                                    className="hover:text-rust underline-offset-2 hover:underline"
                                  >
                                    {c.campaign_id}
                                  </Link>
                                ) : (
                                  '—'
                                )}
                              </dd>
                              <dt className="text-ink-muted uppercase tracking-[0.14em]">
                                thread
                              </dt>
                              <dd className="text-ink truncate">
                                {c.thread_id ? (
                                  <Link
                                    to={`/threads/${c.thread_id}`}
                                    className="hover:text-rust underline-offset-2 hover:underline"
                                  >
                                    {c.thread_id}
                                  </Link>
                                ) : (
                                  '—'
                                )}
                              </dd>
                              <dt className="text-ink-muted uppercase tracking-[0.14em]">
                                lead
                              </dt>
                              <dd className="text-ink truncate">
                                {c.lead_id ? (
                                  <Link
                                    to={`/leads/${c.lead_id}`}
                                    className="hover:text-rust underline-offset-2 hover:underline"
                                  >
                                    {c.lead_id}
                                  </Link>
                                ) : (
                                  '—'
                                )}
                              </dd>
                              <dt className="text-ink-muted uppercase tracking-[0.14em]">
                                call id
                              </dt>
                              <dd className="text-ink truncate">{c.id}</dd>
                              <dt className="text-ink-muted uppercase tracking-[0.14em]">
                                format
                              </dt>
                              <dd className="text-ink">{c.response_format}</dd>
                              {c.temperature != null && (
                                <>
                                  <dt className="text-ink-muted uppercase tracking-[0.14em]">
                                    temp
                                  </dt>
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
            </div>
          </div>
        )}
      </div>
    </div>
  );
}

/**
 * Total spend for the current Logs scope — workspace-wide by default,
 * or scoped to whatever filter the URL is currently carrying (thread,
 * campaign, lead). Sourced from ``GET /api/llm-calls/summary`` so the
 * figure stays accurate past the list endpoint's row cap.
 *
 * When ``unpriced_calls`` is non-zero we render ``≥ $X`` and surface the
 * count via title — those rows contribute $0 to the sum (no rate card)
 * and we'd rather show "at least" than imply precision we don't have.
 */
function LlmCostTotal({ summary }: { summary?: LlmCallsSummary }) {
  if (!summary) {
    return (
      <div className="flex flex-col items-end font-mono text-[10px] tracking-[0.14em] uppercase text-ink-faint">
        <span>Total</span>
        <span className="text-ink tabular-nums text-base normal-case tracking-normal">—</span>
      </div>
    );
  }
  const isFloor = summary.unpriced_calls > 0;
  const dollars = `$${summary.total_cost_usd.toFixed(4)}`;
  const titleParts = [
    `${summary.total_calls.toLocaleString()} call${summary.total_calls === 1 ? '' : 's'}`,
    `${summary.total_tokens_in.toLocaleString()} in / ${summary.total_tokens_out.toLocaleString()} out`,
  ];
  if (isFloor) {
    titleParts.push(
      `${summary.unpriced_calls.toLocaleString()} call${
        summary.unpriced_calls === 1 ? '' : 's'
      } with no rate card — true total is at least this figure`,
    );
  }
  return (
    <div
      className="flex flex-col items-end font-mono text-[10px] tracking-[0.14em] uppercase text-ink-muted"
      title={titleParts.join(' · ')}
    >
      <span>Total spend</span>
      <span className="text-ink tabular-nums text-base normal-case tracking-normal">
        {isFloor && <span className="text-ink-muted mr-1">≥</span>}
        {dollars}
      </span>
    </div>
  );
}
