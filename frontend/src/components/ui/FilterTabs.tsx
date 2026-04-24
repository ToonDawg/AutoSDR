import { cn } from '@/lib/utils';

export interface FilterOption<T extends string> {
  id: T;
  label: string;
}

interface FilterTabsProps<T extends string> {
  options: ReadonlyArray<FilterOption<T>>;
  active: T;
  onChange: (id: T) => void;
  counts?: Map<string, number> | Record<string, number>;
}

/**
 * Tab-bar used on the Threads / Leads / Logs / Inbox index pages.
 *
 * The counts lookup is `string`-keyed so the caller can stash an "all"
 * bucket alongside the enum ids without faking a discriminator.
 */
export function FilterTabs<T extends string>({
  options,
  active,
  onChange,
  counts,
}: FilterTabsProps<T>) {
  const lookup = counts instanceof Map ? counts : counts ? new Map(Object.entries(counts)) : null;
  return (
    <div className="flex items-center gap-6 border-b border-rule">
      {options.map((opt) => (
        <button
          key={opt.id}
          onClick={() => onChange(opt.id)}
          className={cn('tab', active === opt.id && 'is-active')}
        >
          {opt.label}
          {lookup && (
            <span className="ml-1.5 text-ink-faint font-mono text-[11px]">
              {lookup.get(opt.id) ?? 0}
            </span>
          )}
        </button>
      ))}
    </div>
  );
}
