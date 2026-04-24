import { cn } from '@/lib/utils';

type StatTone = 'neutral' | 'rust' | 'forest' | 'mustard';

/**
 * Size variant. Controls container padding, value text size, and
 * whether the value uses tabular numerals. Picked to match the three
 * existing usages at a glance:
 *   - `sm`: tight row cells (Campaigns list)
 *   - `md`: divided grid cells (Dashboard overview)
 *   - `lg`: header stat cards (CampaignDetail)
 */
type StatSize = 'sm' | 'md' | 'lg';

const TONE: Record<StatTone, string> = {
  neutral: 'text-ink',
  rust: 'text-rust',
  forest: 'text-forest',
  mustard: 'text-mustard',
};

const SIZE: Record<StatSize, { wrap: string; value: string }> = {
  sm: {
    wrap: 'flex flex-col gap-0.5 min-w-0',
    value: 'text-xl font-medium tabular-nums truncate',
  },
  md: {
    wrap: 'px-5 py-4 flex flex-col gap-1',
    value: 'text-base font-medium',
  },
  lg: {
    wrap: 'p-4 flex flex-col gap-1',
    value: 'text-2xl font-medium tabular-nums',
  },
};

/**
 * Label + big number cell. The single primitive behind the three
 * different stat tiles we used to have (Dashboard, Campaigns list,
 * CampaignDetail header). Accept any displayable value — strings
 * ("Paused") and numbers (lead counts) both make sense here.
 */
export function Stat({
  label,
  value,
  hint,
  tone = 'neutral',
  size = 'md',
  capitalize,
}: {
  label: string;
  value: string | number;
  hint?: string;
  tone?: StatTone;
  size?: StatSize;
  capitalize?: boolean;
}) {
  const s = SIZE[size];
  return (
    <div className={s.wrap}>
      <span className={cn('label', size === 'sm' && 'truncate')} title={size === 'sm' ? label : undefined}>
        {label}
      </span>
      <span className={cn(s.value, TONE[tone], capitalize && 'capitalize')}>{value}</span>
      {hint && (
        <span
          className={cn(
            'text-ink-faint',
            size === 'sm' ? 'text-[10px] tabular-nums truncate' : 'text-[11px]',
          )}
        >
          {hint}
        </span>
      )}
    </div>
  );
}
