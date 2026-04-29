/**
 * "By angle" panel — renders the per-angle funnel returned by
 * ``GET /api/stats/angle-funnel``.
 *
 * Shape: one row per ``Thread.angle_type`` bucket (server-sorted by
 * ``threads`` desc), with a horizontal bar showing
 * ``replied / threads`` and the absolute counts on the right. We
 * deliberately render bars with ``<div>`` + width percentages instead
 * of pulling in Recharts/Visx — there are at most ~8 buckets and a
 * full chart lib is not justified by the data shape.
 *
 * Bar widths are clamped to a 4 % minimum so a dominant angle (90 % of
 * threads) doesn't render the long-tail bars as invisible 1-px
 * smudges.
 *
 * Accepts an optional ``campaignId`` to scope the funnel to a single
 * campaign — used by both ``/Logs`` (when the URL carries
 * ``?campaign=…``) and ``/CampaignDetail`` (always scoped).
 */

import { useQuery } from '@tanstack/react-query';
import { api } from '@/lib/api';
import type { AngleFunnel, AngleFunnelRow, EnrichmentFilter } from '@/lib/types';
import { cn } from '@/lib/utils';

type Props = {
  /** Scope the funnel to one campaign. When set, the server applies
   *  no time filter (campaign-lifetime) unless overridden. */
  campaignId?: string;
  /** Override the default time window (days). Default: server picks
   *  30 days workspace-scoped, no time filter campaign-scoped. */
  sinceDays?: number;
  /** Stratify by lead-website enrichment outcome. Defaults to ``"all"``.
   *  Mirrors the ``?enrichment=`` query param on the API. */
  enrichment?: EnrichmentFilter;
  /** Called when the operator clicks a segment in the Enriched / All /
   *  Unenriched control. Hosts that own the URL state should mirror
   *  the value into their query params. Optional — when omitted, the
   *  control hides. */
  onEnrichmentChange?: (next: EnrichmentFilter) => void;
  /** Optional class for the outer wrapper — lets the host page slot
   *  the panel into either a stacked or grid layout. */
  className?: string;
};

const MIN_BAR_PERCENT = 4;

const ENRICHMENT_SEGMENTS: { id: EnrichmentFilter; label: string }[] = [
  { id: 'all', label: 'All' },
  { id: 'enriched', label: 'Enriched' },
  { id: 'unenriched', label: 'Unenriched' },
];

export function AngleFunnelPanel({
  campaignId,
  sinceDays,
  enrichment = 'all',
  onEnrichmentChange,
  className,
}: Props) {
  const query = useQuery<AngleFunnel>({
    // Cache key includes the scope so /Logs and /CampaignDetail don't
    // share a cell — they really are different aggregations.
    queryKey: [
      'angle-funnel',
      {
        campaignId: campaignId ?? null,
        sinceDays: sinceDays ?? null,
        enrichment,
      },
    ],
    queryFn: () => api.getAngleFunnel({ campaignId, sinceDays, enrichment }),
  });

  const data = query.data;
  const rows = data?.rows ?? [];
  const totals = sumRows(rows);
  const scopeHint = formatScope(data, campaignId);

  return (
    <section className={cn('paper-card p-5 flex flex-col gap-4', className)}>
      <header className="flex items-baseline justify-between gap-3 pb-2 border-b border-rule">
        <div className="flex flex-col gap-0.5">
          <h2 className="text-sm font-medium">By angle</h2>
          <p className="text-xs text-ink-muted">
            Reply rate per personalisation angle. {scopeHint}
          </p>
        </div>
        {totals.threads > 0 && (
          <span className="text-xs font-mono text-ink-muted tabular-nums shrink-0">
            {totals.replied} / {totals.threads} replied
          </span>
        )}
      </header>

      {onEnrichmentChange && (
        <div
          className="flex items-center gap-1 text-xs"
          role="group"
          aria-label="Filter by enrichment status"
        >
          {ENRICHMENT_SEGMENTS.map((segment) => {
            const active = enrichment === segment.id;
            return (
              <button
                key={segment.id}
                type="button"
                onClick={() => onEnrichmentChange(segment.id)}
                className={cn(
                  'px-2.5 py-1 font-mono uppercase tracking-[0.14em] border transition-colors',
                  active
                    ? 'border-rust bg-rust/10 text-ink'
                    : 'border-rule text-ink-muted hover:text-ink hover:border-ink-muted',
                )}
                aria-pressed={active}
              >
                {segment.label}
              </button>
            );
          })}
        </div>
      )}

      {query.isLoading && (
        <div className="py-6 text-center text-sm text-ink-muted">Loading…</div>
      )}

      {query.isError && (
        <div className="py-6 text-center text-sm text-oxblood">
          Couldn't load the angle funnel.
        </div>
      )}

      {!query.isLoading && !query.isError && rows.length === 0 && (
        <div className="py-6 text-center text-sm text-ink-muted">
          No threads in this scope yet. Send some outreach and check back.
        </div>
      )}

      {rows.length > 0 && (
        <ul className="flex flex-col gap-2.5">
          {rows.map((row) => (
            <BarRow key={row.angle} row={row} />
          ))}
        </ul>
      )}
    </section>
  );
}

function BarRow({ row }: { row: AngleFunnelRow }) {
  const rate = row.threads > 0 ? row.replied / row.threads : 0;
  const widthPct = clampWidth(rate * 100);
  const rateLabel = row.threads > 0 ? `${(rate * 100).toFixed(1)}%` : '—';
  // Tooltip carries the absolute counts so a hover on the bar yields
  // the won/lost slice without spending screen real estate on extra
  // columns.
  const title = `${row.replied} replied of ${row.threads} threads · ${row.won} won · ${row.lost} lost`;

  return (
    <li className="flex flex-col gap-1.5" title={title}>
      <div className="flex items-baseline justify-between gap-3 text-xs">
        <span className="font-mono uppercase tracking-[0.14em] text-ink truncate">
          {row.angle}
        </span>
        <span className="font-mono text-ink-muted tabular-nums shrink-0">
          {row.replied}/{row.threads}
          <span className="ml-2 text-ink">{rateLabel}</span>
        </span>
      </div>
      <div className="relative h-2 bg-paper-deep">
        <div
          className="absolute inset-y-0 left-0 bg-rust"
          style={{ width: `${widthPct}%` }}
          aria-hidden
        />
      </div>
    </li>
  );
}

function sumRows(rows: AngleFunnelRow[]): { threads: number; replied: number } {
  return rows.reduce(
    (acc, r) => ({
      threads: acc.threads + r.threads,
      replied: acc.replied + r.replied,
    }),
    { threads: 0, replied: 0 },
  );
}

function formatScope(data: AngleFunnel | undefined, campaignId: string | undefined): string {
  if (!data) return '';
  if (data.since) {
    const since = new Date(data.since);
    const days = Math.max(1, Math.round((Date.now() - since.getTime()) / 86_400_000));
    return `Last ${days} days.`;
  }
  if (campaignId || data.campaign_id) {
    return 'Campaign lifetime.';
  }
  return '';
}

function clampWidth(pct: number): number {
  if (!Number.isFinite(pct) || pct <= 0) return 0;
  if (pct < MIN_BAR_PERCENT) return MIN_BAR_PERCENT;
  if (pct > 100) return 100;
  return pct;
}
