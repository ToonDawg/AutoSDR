import { useMemo, useState } from 'react';
import { useMutation, useQuery, useQueryClient, keepPreviousData } from '@tanstack/react-query';
import { Link } from 'react-router-dom';
import { ChevronLeft, ChevronRight, ExternalLink, Loader2, Play } from 'lucide-react';
import { api } from '@/lib/api';
import { useDebouncedValue } from '@/lib/useDebouncedValue';
import { Badge } from '@/components/ui/Badge';
import { Button } from '@/components/ui/Button';
import { CardList, CardListItem } from '@/components/ui/CardList';
import { FilterTabs, type FilterOption } from '@/components/ui/FilterTabs';
import { PageHeader } from '@/components/ui/PageHeader';
import { SearchInput } from '@/components/ui/SearchInput';
import { relTime } from '@/lib/format';
import type { ScanStatus } from '@/lib/types';

type ScanFilterId = ScanStatus | 'all';

const FILTERS: ReadonlyArray<FilterOption<ScanFilterId>> = [
  { id: 'all', label: 'All' },
  { id: 'ok', label: 'OK' },
  { id: 'never_scanned', label: 'Never scanned' },
  { id: 'timeout', label: 'Timeout' },
  { id: 'blocked', label: 'Blocked' },
  { id: 'error', label: 'Error' },
  { id: 'not_found', label: 'Not found' },
  { id: 'empty_shell', label: 'Empty shell' },
  { id: 'no_url', label: 'No URL' },
];

const STATUS_TONE: Record<ScanStatus, Parameters<typeof Badge>[0]['tone']> = {
  ok: 'forest',
  never_scanned: 'outline',
  timeout: 'mustard',
  blocked: 'oxblood',
  error: 'oxblood',
  not_found: 'oxblood',
  empty_shell: 'neutral',
  no_url: 'neutral',
  killswitch_aborted: 'oxblood',
  disabled: 'neutral',
};

const PAGE_SIZE = 100;

/**
 * Scans index — every campaign-assigned lead's most recent
 * website-enrichment outcome.
 *
 * Defaults to leads in at least one campaign; "Include unassigned" widens scope.
 * The batch scan runner is started and stopped with Start / Stop; counts and
 * rows refresh on a light poll.
 */
export function Scans() {
  const qc = useQueryClient();
  const [filter, setFilter] = useState<ScanFilterId>('all');
  const [qDraft, setQDraft] = useState('');
  const [page, setPage] = useState(0);
  const [includeUnassigned, setIncludeUnassigned] = useState(false);

  const q = useDebouncedValue(qDraft.trim(), 200);

  const handleSearchChange = (next: string) => {
    setQDraft(next);
    setPage(0);
  };
  const handleFilterChange = (next: ScanFilterId) => {
    setFilter(next);
    setPage(0);
  };
  const handleScopeChange = (next: boolean) => {
    setIncludeUnassigned(next);
    setPage(0);
  };

  const offset = page * PAGE_SIZE;

  const { data, isLoading, isFetching } = useQuery({
    queryKey: [
      'scans',
      { status: filter, q, includeUnassigned, offset, limit: PAGE_SIZE },
    ],
    queryFn: () =>
      api.listScans({
        status: filter === 'all' ? undefined : filter,
        q: q || undefined,
        includeUnassigned,
        limit: PAGE_SIZE,
        offset,
      }),
    placeholderData: keepPreviousData,
    refetchInterval: 15_000,
  });

  const summaryQuery = useQuery({
    queryKey: ['scans', 'summary', { includeUnassigned }],
    queryFn: () => api.getScansSummary({ includeUnassigned }),
    refetchInterval: 15_000,
  });

  const workerMutation = useMutation({
    mutationFn: (enabled: boolean) => api.runScans({ enabled }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['scans'] });
    },
  });

  const counts = useMemo(() => data?.counts_by_status ?? { all: 0 }, [data]);
  const scans = data?.scans ?? [];
  const total = data?.total ?? 0;
  const pageCount = Math.max(1, Math.ceil(total / PAGE_SIZE));
  const rangeStart = total === 0 ? 0 : offset + 1;
  const rangeEnd = Math.min(offset + scans.length, total);
  const summary = summaryQuery.data;

  const pct =
    summary && summary.total_leads > 0
      ? summary.runner_running && summary.runner_total > 0
        ? Math.min(100, (summary.runner_done / summary.runner_total) * 100)
        : Math.min(
            100,
            ((summary.total_leads - summary.never_scanned) / summary.total_leads) * 100,
          )
      : 0;

  return (
    <div className="page gap-5">
      <PageHeader
        title="Scans"
        description={
          includeUnassigned
            ? 'Website enrichment applies to leads in scope below; toggle “Include…” to widen to every lead.'
            : 'Default view: leads in at least one campaign. Expand scope with the checkbox.'
        }
        right={
          <div className="flex flex-wrap items-center justify-end gap-3">
            <SearchInput
              value={qDraft}
              onChange={handleSearchChange}
              placeholder="Search name or website…"
              className="w-full max-w-[min(18rem,100%)] shrink-0 sm:w-72"
            />
            {summary && (
              <Button
                variant={summary.runner_running ? 'secondary' : 'primary'}
                iconLeft={
                  workerMutation.isPending ? (
                    <Loader2 className="h-4 w-4 animate-spin" strokeWidth={1.5} />
                  ) : summary.runner_running ? (
                    <Loader2 className="h-4 w-4 animate-spin" strokeWidth={1.5} />
                  ) : (
                    <Play className="h-4 w-4" strokeWidth={1.5} />
                  )
                }
                onClick={() => workerMutation.mutate(!summary.runner_running)}
                disabled={workerMutation.isPending}
              >
                {workerMutation.isPending
                  ? 'Saving…'
                  : summary.runner_running
                    ? `Stop · ${summary.runner_done.toLocaleString()} / ${summary.runner_total.toLocaleString()}`
                    : 'Start scanning'}
              </Button>
            )}
          </div>
        }
      />

      {summary && (
        <div className="paper-card px-5 py-4">
          <div className="flex flex-col gap-3 sm:flex-row sm:items-start sm:justify-between sm:gap-6">
            <div className="min-w-0 flex-1 space-y-2">
              <div className="flex flex-wrap items-baseline gap-x-2 gap-y-1 text-sm">
                <span className="text-ink">
                  <span className="tabular-nums font-medium">
                    {summary.runner_running
                      ? `${summary.runner_done.toLocaleString()}`
                      : (summary.total_leads - summary.never_scanned).toLocaleString()}
                  </span>{' '}
                  <span className="text-ink-muted">of</span>{' '}
                  <span className="tabular-nums font-medium">
                    {summary.runner_running
                      ? summary.runner_total.toLocaleString()
                      : summary.total_leads.toLocaleString()}
                  </span>
                </span>
                <span className="text-ink-muted" aria-hidden>
                  ·
                </span>
                <span className="tabular-nums text-ink-muted">
                  {summary.runner_running
                    ? `${summary.runner_ok.toLocaleString()} ok · ${summary.runner_failed.toLocaleString()} failed`
                    : `${summary.never_scanned.toLocaleString()} never scanned`}
                </span>
              </div>
              <div
                className="h-2 w-full overflow-hidden rounded-full bg-paper-deep"
                role="progressbar"
                aria-valuenow={pct}
                aria-valuemin={0}
                aria-valuemax={100}
                aria-label={`Scan coverage ${pct.toFixed(1)} percent`}
              >
                <div
                  className="h-full rounded-full bg-forest transition-[width]"
                  style={{ width: `${pct}%` }}
                />
              </div>
            </div>
            <span className="shrink-0 font-mono text-[11px] text-ink-faint whitespace-nowrap">
              Last enrichment:{' '}
              {summary.last_run_at ? relTime(summary.last_run_at) : 'never'}
            </span>
          </div>
        </div>
      )}

      <div className="flex flex-col gap-4 sm:flex-row sm:items-center sm:justify-between">
        <FilterTabs
          options={FILTERS}
          active={filter}
          onChange={handleFilterChange}
          counts={counts}
        />
        <label className="flex shrink-0 items-center gap-2 text-xs font-mono text-ink-muted">
          <input
            type="checkbox"
            checked={includeUnassigned}
            onChange={(e) => handleScopeChange(e.target.checked)}
            className="rounded border-rule"
          />
          <span>Include leads not in a campaign</span>
        </label>
      </div>

      <div className="flex items-center justify-between text-xs font-mono text-ink-muted">
        <span>
          {total === 0 ? (
            '0 scans'
          ) : (
            <>
              <span className="text-ink tabular-nums">{rangeStart.toLocaleString()}</span>
              {'–'}
              <span className="text-ink tabular-nums">{rangeEnd.toLocaleString()}</span>
              {' of '}
              <span className="text-ink tabular-nums">{total.toLocaleString()}</span>
              {q && <span className="text-ink-faint"> · matching "{q}"</span>}
            </>
          )}
        </span>
        {isFetching && !isLoading && <span className="text-ink-faint">Refreshing…</span>}
      </div>

      <div className="paper-card hidden md:block overflow-x-auto">
        <table className="t-table min-w-[700px] w-full table-fixed">
          <thead>
            <tr>
              <th className="w-[22%]">Lead</th>
              <th className="w-[22%]">Website</th>
              <th className="w-[14%]">Status</th>
              <th className="hidden w-[10%] md:table-cell">CMS</th>
              <th className="hidden w-[8%] text-right md:table-cell">Sitemap</th>
              <th className="hidden w-[8%] text-right md:table-cell">Latency</th>
              <th className="w-[16%]">Fetched</th>
            </tr>
          </thead>
          <tbody>
            {scans.map((row) => (
              <tr key={row.lead_id}>
                <td className="max-w-0 min-w-24">
                  <Link
                    to={`/scans/${row.lead_id}`}
                    className="block truncate text-sm font-medium text-ink hover:text-rust"
                    title={row.lead_name ?? undefined}
                  >
                    {row.lead_name ?? '—'}
                  </Link>
                </td>
                <td className="max-w-0 min-w-24">
                  {row.website ? (
                    <a
                      href={row.website}
                      target="_blank"
                      rel="noopener noreferrer"
                      title={row.website}
                      className="flex min-w-0 items-center gap-1 font-mono text-[11px] text-ink-muted hover:text-ink"
                      onClick={(e) => e.stopPropagation()}
                    >
                      <span className="truncate">{row.website}</span>
                      <ExternalLink className="h-3 w-3 shrink-0" strokeWidth={1.5} />
                    </a>
                  ) : (
                    <span className="text-ink-faint">—</span>
                  )}
                </td>
                <td className="max-w-0">
                  <Badge tone={STATUS_TONE[row.status] ?? 'neutral'} dot>
                    {row.status}
                  </Badge>
                </td>
                <td className="hidden max-w-0 md:table-cell">
                  {row.cms ? (
                    <span className="block truncate font-mono text-xs text-ink">{row.cms}</span>
                  ) : (
                    <span className="text-ink-faint">—</span>
                  )}
                </td>
                <td className="hidden text-right font-mono text-xs text-ink-muted tabular-nums md:table-cell">
                  {row.sitemap_count != null ? row.sitemap_count : '—'}
                </td>
                <td className="hidden text-right font-mono text-xs text-ink-muted tabular-nums md:table-cell">
                  {row.latency_ms != null ? `${row.latency_ms}ms` : '—'}
                </td>
                <td
                  className="max-w-40 font-mono text-[11px] text-ink-muted"
                  title={row.fetched_at ?? undefined}
                >
                  {row.fetched_at ? relTime(row.fetched_at) : '—'}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
        {scans.length === 0 && !isLoading && (
          <div className="py-14 text-center text-ink-muted text-sm">
            {q || filter !== 'all'
              ? 'No scans match this filter.'
              : includeUnassigned
                ? 'No leads yet — import some to start scanning.'
                : 'No campaign leads yet — assign leads to a campaign or include unassigned.'}
          </div>
        )}
        {isLoading && scans.length === 0 && (
          <div className="py-14 text-center text-ink-muted text-sm">Loading scans…</div>
        )}
      </div>

      <CardList className="md:hidden">
        {scans.map((row) => (
          <CardListItem
            key={row.lead_id}
            to={`/scans/${row.lead_id}`}
            title={row.lead_name ?? '—'}
            description={
              <>
                {row.website && (
                  <div className="font-mono text-[11px] truncate">{row.website}</div>
                )}
                <div className="flex flex-wrap gap-x-3 gap-y-0.5 text-[11px]">
                  {row.cms && <span className="font-mono">{row.cms}</span>}
                  {row.sitemap_count != null && (
                    <span className="font-mono tabular-nums">
                      {row.sitemap_count} sitemap
                    </span>
                  )}
                  {row.latency_ms != null && (
                    <span className="font-mono tabular-nums">{row.latency_ms}ms</span>
                  )}
                </div>
              </>
            }
            badges={
              <Badge tone={STATUS_TONE[row.status] ?? 'neutral'} dot>
                {row.status}
              </Badge>
            }
            trailing={
              <span className="font-mono text-[10px] text-ink-muted">
                {row.fetched_at ? relTime(row.fetched_at) : '—'}
              </span>
            }
          />
        ))}
        {scans.length === 0 && !isLoading && (
          <li className="paper-card py-10 text-center text-ink-muted text-sm">
            {q || filter !== 'all'
              ? 'No scans match this filter.'
              : includeUnassigned
                ? 'No leads yet — import some to start scanning.'
                : 'No campaign leads yet — assign leads to a campaign or include unassigned.'}
          </li>
        )}
      </CardList>

      {total > PAGE_SIZE && (
        <nav className="flex items-center justify-between pt-2">
          <Button
            variant="ghost"
            size="sm"
            iconLeft={<ChevronLeft className="h-3.5 w-3.5" strokeWidth={1.5} />}
            onClick={() => setPage((p) => Math.max(0, p - 1))}
            disabled={page === 0}
          >
            Previous
          </Button>
          <span className="font-mono text-[11px] text-ink-muted tabular-nums">
            Page {page + 1} of {pageCount.toLocaleString()}
          </span>
          <Button
            variant="ghost"
            size="sm"
            iconRight={<ChevronRight className="h-3.5 w-3.5" strokeWidth={1.5} />}
            onClick={() => setPage((p) => Math.min(pageCount - 1, p + 1))}
            disabled={page >= pageCount - 1}
          >
            Next
          </Button>
        </nav>
      )}
    </div>
  );
}
