import { useQuery, keepPreviousData } from '@tanstack/react-query';
import { Link, useNavigate } from 'react-router-dom';
import { useEffect, useMemo, useState } from 'react';
import { ChevronLeft, ChevronRight, Upload } from 'lucide-react';
import { api } from '@/lib/api';
import { Badge } from '@/components/ui/Badge';
import { Button } from '@/components/ui/Button';
import { FilterTabs, type FilterOption } from '@/components/ui/FilterTabs';
import { PageHeader } from '@/components/ui/PageHeader';
import { SearchInput } from '@/components/ui/SearchInput';
import {
  CONTACT_TYPE_LABEL,
  LEAD_STATUS_LABEL,
  formatPhone,
  formatSkipReason,
  relTime,
} from '@/lib/format';
import { LeadStatus, type LeadStatusT } from '@/lib/types';
import { cn } from '@/lib/utils';

type LeadFilterId = LeadStatusT | 'all' | 'do_not_contact';

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
  const [filter, setFilter] = useState<LeadFilterId>('all');
  const [qDraft, setQDraft] = useState('');
  const [q, setQ] = useState('');
  const [page, setPage] = useState(0);

  // Debounce search input → committed query used for the fetch.
  useEffect(() => {
    const id = setTimeout(() => {
      setQ(qDraft.trim());
      setPage(0);
    }, 200);
    return () => clearTimeout(id);
  }, [qDraft]);

  const handleFilterChange = (next: LeadFilterId) => {
    setFilter(next);
    setPage(0);
  };

  const offset = page * PAGE_SIZE;

  const { data, isLoading, isFetching } = useQuery({
    queryKey: ['leads', { status: filter, q, offset, limit: PAGE_SIZE }],
    queryFn: () =>
      api.listLeads({
        status: filter === 'all' ? undefined : filter,
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
              onChange={setQDraft}
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
          </div>
        }
      />

      <FilterTabs options={FILTERS} active={filter} onChange={handleFilterChange} counts={counts} />

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
    </div>
  );
}
