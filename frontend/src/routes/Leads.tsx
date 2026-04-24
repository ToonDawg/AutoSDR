import { useQuery } from '@tanstack/react-query';
import { Link } from 'react-router-dom';
import { useMemo, useState } from 'react';
import { Upload } from 'lucide-react';
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
  relTime,
} from '@/lib/format';
import { LeadStatus, type Lead, type LeadStatusT } from '@/lib/types';
import { cn } from '@/lib/utils';

const FILTERS: ReadonlyArray<FilterOption<LeadStatusT | 'all'>> = [
  { id: 'all', label: 'All' },
  { id: LeadStatus.NEW, label: 'Queued' },
  { id: LeadStatus.CONTACTED, label: 'Contacted' },
  { id: LeadStatus.REPLIED, label: 'Replied' },
  { id: LeadStatus.WON, label: 'Won' },
  { id: LeadStatus.LOST, label: 'Lost' },
  { id: LeadStatus.SKIPPED, label: 'Skipped' },
];

const STATUS_TONE: Record<LeadStatusT, Parameters<typeof Badge>[0]['tone']> = {
  new: 'neutral',
  contacted: 'teal',
  replied: 'rust',
  won: 'forest',
  lost: 'oxblood',
  skipped: 'neutral',
};

/**
 * Leads index. Table-first: the importer already normalises and classifies
 * phones, so the table's job is just to surface "what's here, what's been
 * skipped, and why" without burying that in prose.
 */
export function Leads() {
  const [filter, setFilter] = useState<LeadStatusT | 'all'>('all');
  const [q, setQ] = useState('');
  const { data: leads } = useQuery({
    queryKey: ['leads'],
    queryFn: () => api.listLeads({ limit: 500 }),
  });

  const filtered = useMemo(() => {
    if (!leads) return [];
    let out: Lead[] = leads;
    if (filter !== 'all') out = out.filter((l) => l.status === filter);
    if (q.trim()) {
      const s = q.trim().toLowerCase();
      out = out.filter(
        (l) =>
          (l.name ?? '').toLowerCase().includes(s) ||
          (l.category ?? '').toLowerCase().includes(s) ||
          (l.contact_uri ?? '').includes(s),
      );
    }
    return out.sort((a, b) => a.import_order - b.import_order);
  }, [leads, filter, q]);

  const counts = useMemo(() => {
    const m = new Map<string, number>();
    m.set('all', leads?.length ?? 0);
    leads?.forEach((l) => m.set(l.status, (m.get(l.status) ?? 0) + 1));
    return m;
  }, [leads]);

  return (
    <div className="page gap-5">
      <PageHeader
        title="Leads"
        description="Import normalises phone numbers and flags landlines / toll-free numbers up front — only mobiles get messaged."
        right={
          <div className="flex items-center gap-3">
            <SearchInput
              value={q}
              onChange={setQ}
              placeholder="Search leads…"
              className="w-60"
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

      <FilterTabs options={FILTERS} active={filter} onChange={setFilter} counts={counts} />

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
            {filtered.map((l) => (
              <tr key={l.id} className={cn(l.status === LeadStatus.SKIPPED && 'stripes')}>
                <td className="font-mono text-[11px] text-ink-faint tabular-nums">
                  {String(l.import_order).padStart(3, '0')}
                </td>
                <td>
                  <div className="text-sm font-medium text-ink">{l.name ?? '—'}</div>
                  {l.address && (
                    <div className="text-[11px] text-ink-muted mt-0.5">{l.address}</div>
                  )}
                  {l.skip_reason && (
                    <div className="text-[11px] text-oxblood mt-1">{l.skip_reason}</div>
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
        {filtered.length === 0 && (
          <div className="py-14 text-center text-ink-muted text-sm">
            No leads match the filter.
          </div>
        )}
      </div>
    </div>
  );
}
