import { useQuery, useMutation, useQueryClient, keepPreviousData } from '@tanstack/react-query';
import { Link, useNavigate } from 'react-router-dom';
import { useMemo, useState } from 'react';
import { ChevronLeft, ChevronRight, Database, Upload } from 'lucide-react';
import { api } from '@/lib/api';
import { Badge } from '@/components/ui/Badge';
import { Button } from '@/components/ui/Button';
import { FilterTabs, type FilterOption } from '@/components/ui/FilterTabs';
import { PageHeader } from '@/components/ui/PageHeader';
import { SearchInput } from '@/components/ui/SearchInput';
import { Input } from '@/components/ui/Input';
import { useDebouncedValue } from '@/lib/useDebouncedValue';
import {
  CONTACT_TYPE_LABEL,
  LEAD_STATUS_LABEL,
  formatPhone,
  formatSkipReason,
  relTime,
} from '@/lib/format';
import {
  LeadStatus,
  type LeadEnrichResult,
  type LeadStatusT,
} from '@/lib/types';
import { cn } from '@/lib/utils';

type LeadFilterId = LeadStatusT | 'all' | 'do_not_contact';
type AssignmentFilter = 'all' | 'in_campaign' | 'unassigned';

const ASSIGNMENT_OPTIONS: ReadonlyArray<FilterOption<AssignmentFilter>> = [
  { id: 'all', label: 'All' },
  { id: 'in_campaign', label: 'In a campaign' },
  { id: 'unassigned', label: 'Unassigned' },
];

const FILTERS: ReadonlyArray<FilterOption<LeadFilterId>> = [
  { id: 'all', label: 'All' },
  { id: LeadStatus.NEW, label: 'Queued' },
  { id: LeadStatus.CONTACTED, label: 'Contacted' },
  { id: LeadStatus.REPLIED, label: 'Replied' },
  { id: LeadStatus.WON, label: 'Won' },
  { id: LeadStatus.LOST, label: 'Lost' },
  { id: LeadStatus.SKIPPED, label: 'Skipped' },
  { id: 'do_not_contact', label: 'Do not contact' },
];

const STATUS_TONE: Record<LeadStatusT, Parameters<typeof Badge>[0]['tone']> = {
  new: 'neutral',
  contacted: 'teal',
  replied: 'rust',
  won: 'forest',
  lost: 'oxblood',
  skipped: 'neutral',
};

const PAGE_SIZE = 100;

/**
 * Leads index. The server owns pagination and counts because a single
 * regional scrape can produce tens of thousands of rows — showing just
 * the first N would silently hide the rest (which is what the old
 * ``limit=500`` client-side filter did, and why 34k imports looked like
 * 500 on the page).
 *
 * Search is debounced so every keystroke doesn't fire a fresh round-trip.
 * Each row is a link to the lead detail page so the operator can see the
 * full raw payload (category, address, reviews, etc.) without loading all
 * leads just to search client-side.
 */
export function Leads() {
  const navigate = useNavigate();
  const qc = useQueryClient();
  const [filter, setFilter] = useState<LeadFilterId>('all');
  const [assignment, setAssignment] = useState<AssignmentFilter>('all');
  const [qDraft, setQDraft] = useState('');
  const [page, setPage] = useState(0);
  const [enrichOpen, setEnrichOpen] = useState(false);
  const [sinceDays, setSinceDays] = useState('30');
  const [enrichLimit, setEnrichLimit] = useState('50');
  const [enrichDryRun, setEnrichDryRun] = useState(false);
  const [enrichResult, setEnrichResult] = useState<LeadEnrichResult | null>(null);

  const enrichMut = useMutation({
    mutationFn: () =>
      api.enrichLeads({
        since_days: Number(sinceDays) || 30,
        limit: Number(enrichLimit) || 50,
        dry_run: enrichDryRun,
      }),
    onSuccess: (data) => {
      setEnrichResult(data);
      qc.invalidateQueries({ queryKey: ['leads'] });
    },
  });

  const q = useDebouncedValue(qDraft.trim(), 200);

  // Reset to page 0 in the change handlers themselves — keeps state
  // derivation out of ``useEffect``. Setting page to 0 on every
  // keystroke is a no-op when we're already on page 0 and avoids
  // the "showing 1101–1200 of 50" overshoot when the result set
  // shrinks.
  const handleSearchChange = (next: string) => {
    setQDraft(next);
    setPage(0);
  };
  const handleFilterChange = (next: LeadFilterId) => {
    setFilter(next);
    setPage(0);
  };
  const handleAssignmentChange = (next: AssignmentFilter) => {
    setAssignment(next);
    setPage(0);
  };

  const offset = page * PAGE_SIZE;

  const { data, isLoading, isFetching } = useQuery({
    queryKey: ['leads', { status: filter, assignment, q, offset, limit: PAGE_SIZE }],
    queryFn: () =>
      api.listLeads({
        status: filter === 'all' ? undefined : filter,
        assignment: assignment === 'all' ? undefined : assignment,
        q: q || undefined,
        limit: PAGE_SIZE,
        offset,
      }),
    placeholderData: keepPreviousData,
  });

  const counts = useMemo(() => {
    const fallback: Record<string, number> = { all: 0 };
    return data?.counts_by_status ?? fallback;
  }, [data]);

  const leads = data?.leads ?? [];
  const total = data?.total ?? 0;
  const pageCount = Math.max(1, Math.ceil(total / PAGE_SIZE));
  const rangeStart = total === 0 ? 0 : offset + 1;
  const rangeEnd = Math.min(offset + leads.length, total);

  return (
    <div className="page gap-5">
      <PageHeader
        title="Leads"
        description="Import normalises phone numbers and flags landlines / toll-free numbers up front — only mobiles get messaged."
        right={
          <div className="flex items-center gap-3">
            <SearchInput
              value={qDraft}
              onChange={handleSearchChange}
              placeholder="Search name, category, phone…"
              className="w-72"
            />
            <Link to="/leads/import">
              <Button
                variant="primary"
                iconLeft={<Upload className="h-4 w-4" strokeWidth={1.5} />}
              >
                Import
              </Button>
            </Link>
            <Button
              type="button"
              variant="secondary"
              iconLeft={<Database className="h-4 w-4" strokeWidth={1.5} />}
              onClick={() => {
                setEnrichResult(null);
                setEnrichOpen(true);
              }}
            >
              Enrich stale leads
            </Button>
          </div>
        }
      />

      <FilterTabs options={FILTERS} active={filter} onChange={handleFilterChange} counts={counts} />

      <FilterTabs
        options={ASSIGNMENT_OPTIONS}
        active={assignment}
        onChange={handleAssignmentChange}
      />

      <div className="flex items-center justify-between text-xs font-mono text-ink-muted">
        <span>
          {total === 0 ? (
            '0 leads'
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

      <div className="paper-card">
        <table className="t-table">
          <thead>
            <tr>
              <th style={{ width: '6%' }}>#</th>
              <th style={{ width: '26%' }}>Name</th>
              <th style={{ width: '16%' }}>Category</th>
              <th style={{ width: '16%' }}>Phone</th>
              <th style={{ width: '10%' }}>Type</th>
              <th style={{ width: '12%' }}>Status</th>
              <th style={{ width: '14%' }}>Imported</th>
            </tr>
          </thead>
          <tbody>
            {leads.map((l) => (
              <tr
                key={l.id}
                className={cn(
                  'cursor-pointer',
                  l.status === LeadStatus.SKIPPED && 'stripes',
                )}
                onClick={() => navigate(`/leads/${l.id}`)}
              >
                <td className="font-mono text-[11px] text-ink-faint tabular-nums">
                  {String(l.import_order).padStart(3, '0')}
                </td>
                <td>
                  <div className="flex items-center gap-2">
                    <span className="text-sm font-medium text-ink truncate">
                      {l.name ?? '—'}
                    </span>
                    {l.do_not_contact_at && (
                      <Badge tone="oxblood" dot>
                        Opted out
                      </Badge>
                    )}
                  </div>
                  {l.address && (
                    <div className="text-[11px] text-ink-muted mt-0.5">{l.address}</div>
                  )}
                  {l.skip_reason && l.skip_reason !== 'do_not_contact' && (
                    <div className="text-[11px] text-oxblood mt-1">
                      {formatSkipReason(l.skip_reason)}
                    </div>
                  )}
                </td>
                <td className="text-xs text-ink-muted">{l.category ?? '—'}</td>
                <td className="font-mono text-xs text-ink">{formatPhone(l.contact_uri)}</td>
                <td>
                  {l.contact_type ? (
                    <span className="font-mono text-[10px] tracking-[0.14em] uppercase text-ink-muted">
                      {CONTACT_TYPE_LABEL[l.contact_type]}
                    </span>
                  ) : (
                    <span className="text-ink-faint">—</span>
                  )}
                </td>
                <td>
                  <Badge tone={STATUS_TONE[l.status]} dot>
                    {LEAD_STATUS_LABEL[l.status]}
                  </Badge>
                </td>
                <td className="font-mono text-[11px] text-ink-muted">
                  {relTime(l.created_at)}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
        {leads.length === 0 && !isLoading && (
          <div className="py-14 text-center text-ink-muted text-sm">
            {q || filter !== 'all'
              ? 'No leads match this filter.'
              : 'No leads yet — import a file to get started.'}
          </div>
        )}
        {isLoading && leads.length === 0 && (
          <div className="py-14 text-center text-ink-muted text-sm">Loading leads…</div>
        )}
      </div>

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
      {enrichOpen && (
        <div
          className="fixed inset-0 z-50 flex items-center justify-center bg-ink/40 px-4"
          onClick={() => !enrichMut.isPending && setEnrichOpen(false)}
        >
          <div
            role="dialog"
            aria-modal="true"
            className="paper-card w-full max-w-md p-5 flex flex-col gap-4"
            onClick={(e) => e.stopPropagation()}
          >
            <div>
              <h2 className="text-base font-medium text-ink">Enrich stale leads</h2>
              <p className="text-xs text-ink-muted mt-1 leading-relaxed">
                Pre-fetch public website signals for leads whose cache is empty or older than the
                window you set. Dry run lists who would be fetched without mutating data.
              </p>
            </div>
            <div className="grid grid-cols-2 gap-3">
              <label className="flex flex-col gap-1.5">
                <span className="label">Since (days)</span>
                <Input
                  type="number"
                  min={1}
                  max={365}
                  value={sinceDays}
                  onChange={(e) => setSinceDays(e.target.value)}
                  disabled={enrichMut.isPending}
                />
              </label>
              <label className="flex flex-col gap-1.5">
                <span className="label">Max leads</span>
                <Input
                  type="number"
                  min={1}
                  max={200}
                  value={enrichLimit}
                  onChange={(e) => setEnrichLimit(e.target.value)}
                  disabled={enrichMut.isPending}
                />
              </label>
            </div>
            <label className="flex items-center gap-2 text-sm">
              <input
                type="checkbox"
                checked={enrichDryRun}
                onChange={(e) => setEnrichDryRun(e.target.checked)}
                disabled={enrichMut.isPending}
                className="rounded border-rule"
              />
              <span>Dry run (no HTTP, no DB writes)</span>
            </label>
            {enrichResult && (
              <div className="text-xs font-mono text-ink-muted border border-rule rounded px-3 py-2">
                {enrichResult.dry_run ? (
                  <>
                    would process {enrichResult.total} lead(s)
                    {enrichResult.candidates && enrichResult.candidates.length > 0 && (
                      <ul className="mt-2 max-h-32 overflow-y-auto list-disc pl-4">
                        {enrichResult.candidates.map((c) => (
                          <li key={c.lead_id}>
                            {c.name ?? c.lead_id} — {c.last_fetched ?? 'never'}
                          </li>
                        ))}
                      </ul>
                    )}
                  </>
                ) : (
                  <>
                    ok={enrichResult.ok} failed={enrichResult.failed} total={enrichResult.total}
                  </>
                )}
              </div>
            )}
            {enrichMut.isError && (
              <div className="text-xs text-oxblood">
                {enrichMut.error instanceof Error
                  ? enrichMut.error.message
                  : 'Request failed'}
              </div>
            )}
            <div className="flex justify-end gap-2">
              <Button
                variant="ghost"
                onClick={() => setEnrichOpen(false)}
                disabled={enrichMut.isPending}
              >
                Close
              </Button>
              <Button
                variant="primary"
                onClick={() => enrichMut.mutate()}
                disabled={enrichMut.isPending}
              >
                {enrichMut.isPending ? 'Running…' : 'Run'}
              </Button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
