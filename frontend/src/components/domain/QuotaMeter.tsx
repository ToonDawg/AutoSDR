import { cn } from '@/lib/utils';

export function QuotaMeter({
  sent,
  quota,
  compact = false,
  label = 'TODAY',
}: {
  sent: number;
  quota: number;
  compact?: boolean;
  label?: string;
}) {
  const pct = quota > 0 ? Math.min(1, sent / quota) : 0;
  const bars = compact ? 20 : 40;
  const filledCount = Math.round(pct * bars);

  return (
    <div className={cn('flex items-center gap-3 w-full min-w-0', compact && 'gap-2')}>
      <div className="flex flex-col gap-1 w-full min-w-0">
        <div className="flex items-baseline gap-1.5 font-mono text-[10px] uppercase tracking-[0.14em] text-ink-muted whitespace-nowrap">
          <span>{label}</span>
          <span className="text-ink">
            {sent} <span className="text-ink-faint">/ {quota}</span>
          </span>
        </div>
        <div className="flex gap-[2px] h-3 items-stretch w-full">
          {Array.from({ length: bars }).map((_, i) => (
            <span
              key={i}
              className={cn(
                'flex-1 min-w-px',
                i < filledCount
                  ? pct >= 0.9
                    ? 'bg-rust'
                    : pct >= 0.6
                      ? 'bg-ink'
                      : 'bg-forest'
                  : 'bg-paper-deep border-l border-rule',
              )}
            />
          ))}
        </div>
      </div>
    </div>
  );
}
