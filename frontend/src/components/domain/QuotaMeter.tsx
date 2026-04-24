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
    <div className={cn('flex items-center gap-3', compact && 'gap-2')}>
      <div className="flex flex-col gap-1">
        <div className="flex items-baseline gap-1.5 font-mono text-[10px] uppercase tracking-[0.14em] text-ink-muted whitespace-nowrap">
          <span>{label}</span>
          <span className="text-ink">
            {sent} <span className="text-ink-faint">/ {quota}</span>
          </span>
        </div>
        <div className="flex gap-[2px] h-3 items-stretch">
          {Array.from({ length: bars }).map((_, i) => (
            <span
              key={i}
              className={cn(
                'w-[3px]',
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
