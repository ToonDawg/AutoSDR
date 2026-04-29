/**
 * Per-campaign funnel panel.
 *
 * Two charts, one source of truth (``CampaignOut`` + the new
 * ``GET /api/campaigns/{id}/timeseries``):
 *
 *   1. **Pipeline proportions bar.** A single horizontal stacked bar
 *      that shows the *current* shape of the campaign: queued → sending
 *      → paused-for-HITL → contacted → replied → won → lost → skipped.
 *      Includes ``queued`` so an operator can see how much runway is
 *      still in the queue (resolved OQ4, ticket 0003).
 *   2. **14-day daily chart.** Grouped bars per UTC day:
 *      sent / replied / won / lost. Tooltip on each day group exposes
 *      the absolute counts (resolved OQ3 — no click navigation; the
 *      right drill-down surface doesn't exist yet).
 *
 * No charting library — SVG via ``viewBox`` so the layout matches the
 * existing dashboard sparkline conventions and adds zero dependencies.
 */

import { useQuery } from '@tanstack/react-query';
import { api } from '@/lib/api';
import { shortDate } from '@/lib/format';
import type { Campaign, CampaignTimeseries, CampaignTimeseriesBucket } from '@/lib/types';
import { cn } from '@/lib/utils';

type Props = {
  campaign: Campaign;
  /** Window length for the daily chart. Defaults to 14. */
  days?: number;
  className?: string;
};

const DEFAULT_DAYS = 14;

const SERIES = [
  { key: 'sent', label: 'Sent', color: 'var(--color-ink)' },
  { key: 'replied', label: 'Replied', color: 'var(--color-rust)' },
  { key: 'won', label: 'Won', color: 'var(--color-forest)' },
  { key: 'lost', label: 'Lost', color: 'var(--color-oxblood)' },
] as const;

type SeriesKey = (typeof SERIES)[number]['key'];

export function CampaignTimeseriesPanel({ campaign, days = DEFAULT_DAYS, className }: Props) {
  const query = useQuery<CampaignTimeseries>({
    queryKey: ['campaign-timeseries', campaign.id, days],
    queryFn: () => api.getCampaignTimeseries(campaign.id, days),
  });

  const buckets = query.data?.buckets ?? [];

  return (
    <section className={cn('paper-card p-5 flex flex-col gap-5', className)}>
      <header className="flex items-baseline justify-between gap-3 pb-2 border-b border-rule">
        <div className="flex flex-col gap-0.5">
          <h2 className="text-sm font-medium">Campaign funnel</h2>
          <p className="text-xs text-ink-muted">
            Pipeline shape now plus daily activity for the last {days} days (UTC).
          </p>
        </div>
        <span className="text-xs font-mono text-ink-muted tabular-nums shrink-0">
          {campaign.lead_count} {campaign.lead_count === 1 ? 'lead' : 'leads'}
        </span>
      </header>

      <FunnelProportionsBar campaign={campaign} />

      <div className="flex flex-col gap-3 pt-1">
        <div className="flex items-center justify-between gap-4">
          <h3 className="label">Daily activity</h3>
          <Legend />
        </div>

        {query.isLoading && (
          <div className="py-6 text-center text-sm text-ink-muted">Loading…</div>
        )}
        {query.isError && (
          <div className="py-6 text-center text-sm text-oxblood">
            Couldn't load the timeseries.
          </div>
        )}
        {!query.isLoading && !query.isError && buckets.length > 0 && (
          <DailyChart buckets={buckets} />
        )}
      </div>
    </section>
  );
}

/* -------------------------------------------------------------------------- */
/* Funnel proportions bar                                                     */
/* -------------------------------------------------------------------------- */

type FunnelSegment = {
  key: string;
  label: string;
  count: number;
  /** Background colour token; CSS var so dark/light themes work. */
  color: string;
};

function FunnelProportionsBar({ campaign }: { campaign: Campaign }) {
  // Order matches the campaign lifecycle: queued → in-flight → terminal.
  // Empty buckets are filtered out so the bar doesn't render zero-width
  // slivers that the browser still computes hover targets for.
  const segments: FunnelSegment[] = [
    {
      key: 'queued',
      label: 'Queued',
      count: campaign.queued_count,
      color: 'var(--color-paper-sunk)',
    },
    {
      key: 'sending',
      label: 'Sending',
      count: campaign.sending_count,
      color: 'var(--color-rust-soft)',
    },
    {
      key: 'paused_for_hitl',
      label: 'Paused — HITL',
      count: campaign.paused_for_hitl_count,
      color: 'var(--color-mustard-soft)',
    },
    {
      key: 'contacted',
      label: 'Contacted',
      count: campaign.contacted_count,
      color: 'var(--color-rust)',
    },
    {
      key: 'replied',
      label: 'Replied',
      count: campaign.replied_count,
      color: 'var(--color-rust-deep)',
    },
    {
      key: 'won',
      label: 'Won',
      count: campaign.won_count,
      color: 'var(--color-forest)',
    },
    {
      key: 'lost',
      label: 'Lost',
      count: campaign.lost_count,
      color: 'var(--color-oxblood)',
    },
    {
      key: 'skipped',
      label: 'Skipped',
      count: campaign.skipped_count,
      color: 'var(--color-ink-faint)',
    },
  ];

  const total = segments.reduce((acc, s) => acc + s.count, 0);
  const populated = segments.filter((s) => s.count > 0);

  if (total === 0) {
    return (
      <div className="flex flex-col gap-2">
        <div className="label">Pipeline</div>
        <div className="border border-dashed border-rule px-4 py-6 text-center text-xs text-ink-muted">
          No leads assigned yet. Use "Assign all eligible leads" above to seed the queue.
        </div>
      </div>
    );
  }

  return (
    <div className="flex flex-col gap-2">
      <div className="label">Pipeline</div>
      <div
        className="relative h-3 bg-paper-deep flex overflow-hidden"
        role="img"
        aria-label={`Funnel: ${populated
          .map((s) => `${s.count} ${s.label.toLowerCase()}`)
          .join(', ')}`}
      >
        {populated.map((segment) => {
          const pct = (segment.count / total) * 100;
          return (
            <div
              key={segment.key}
              className="h-full"
              style={{ width: `${pct}%`, background: segment.color }}
              title={`${segment.label}: ${segment.count} (${pct.toFixed(0)}%)`}
            />
          );
        })}
      </div>
      <ul className="flex flex-wrap gap-x-4 gap-y-1 text-[11px] font-mono text-ink-muted tabular-nums">
        {populated.map((segment) => (
          <li key={segment.key} className="flex items-center gap-1.5">
            <span
              className="inline-block h-2 w-2"
              style={{ background: segment.color }}
              aria-hidden
            />
            <span className="text-ink">{segment.count}</span>
            <span className="uppercase tracking-[0.12em]">{segment.label}</span>
          </li>
        ))}
      </ul>
    </div>
  );
}

/* -------------------------------------------------------------------------- */
/* Daily chart                                                                */
/* -------------------------------------------------------------------------- */

function DailyChart({ buckets }: { buckets: CampaignTimeseriesBucket[] }) {
  const max = Math.max(
    ...buckets.flatMap((b) => [b.sent, b.replied, b.won, b.lost]),
    1,
  );
  const totals = buckets.reduce(
    (acc, b) => {
      acc.sent += b.sent;
      acc.replied += b.replied;
      acc.won += b.won;
      acc.lost += b.lost;
      return acc;
    },
    { sent: 0, replied: 0, won: 0, lost: 0 },
  );
  const totalActivity = totals.sent + totals.replied + totals.won + totals.lost;

  if (totalActivity === 0) {
    return (
      <div className="border border-dashed border-rule px-4 py-8 text-center text-xs text-ink-muted">
        No outbound or replies in the last {buckets.length} days.
      </div>
    );
  }

  // Layout in viewBox units. Each day occupies one slot; inside the
  // slot, the four series share the inner width with 1-unit gutters.
  const w = 560;
  const h = 110;
  const padX = 4;
  const padY = 10;
  const slotW = (w - padX * 2) / buckets.length;
  const innerGap = 1;
  const barW = Math.max(1, (slotW - innerGap * (SERIES.length + 1)) / SERIES.length);

  return (
    <div className="flex flex-col gap-2">
      <svg
        viewBox={`0 0 ${w} ${h}`}
        className="w-full h-auto"
        preserveAspectRatio="none"
        role="img"
        aria-label={`Daily campaign activity: ${totals.sent} sent, ${totals.replied} replied, ${totals.won} won, ${totals.lost} lost over ${buckets.length} days.`}
      >
        <line
          x1={padX}
          x2={w - padX}
          y1={h - padY}
          y2={h - padY}
          stroke="var(--color-rule-strong)"
          strokeWidth={0.5}
        />
        {buckets.map((bucket, i) => {
          const slotX = padX + i * slotW;
          // ``title`` lives on the <title> child of a <g> so each day's
          // group renders a tooltip with the full breakdown — that's
          // OQ3's "no click navigation, tooltips instead" landing.
          return (
            <g key={bucket.date}>
              <title>{describeBucket(bucket)}</title>
              <rect
                x={slotX}
                y={padY}
                width={slotW}
                height={h - padY * 2}
                fill="transparent"
              />
              {SERIES.map((series, idx) => {
                const value = bucket[series.key as SeriesKey];
                const barH = ((h - padY * 2) * value) / max;
                const barX = slotX + innerGap + idx * (barW + innerGap);
                return (
                  <rect
                    key={series.key}
                    x={barX}
                    y={h - padY - barH}
                    width={barW}
                    height={barH}
                    fill={series.color}
                    opacity={value === 0 ? 0.15 : 1}
                  />
                );
              })}
            </g>
          );
        })}
      </svg>
      <div className="flex items-center justify-between gap-4 text-[10px] font-mono text-ink-muted">
        <span>{shortDate(buckets[0]?.date ?? null)}</span>
        <span>{shortDate(buckets[buckets.length - 1]?.date ?? null)}</span>
      </div>
    </div>
  );
}

function Legend() {
  return (
    <ul className="flex items-center gap-3 text-[10px] font-mono text-ink-muted uppercase tracking-[0.12em]">
      {SERIES.map((s) => (
        <li key={s.key} className="flex items-center gap-1.5">
          <span
            className="inline-block h-2 w-2"
            style={{ background: s.color }}
            aria-hidden
          />
          {s.label}
        </li>
      ))}
    </ul>
  );
}

function describeBucket(bucket: CampaignTimeseriesBucket): string {
  return `${bucket.date} · ${bucket.sent} sent, ${bucket.replied} replied, ${bucket.won} won, ${bucket.lost} lost`;
}
