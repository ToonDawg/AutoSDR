import type { ReactNode } from "react";
import { Link } from "react-router-dom";
import { cn } from "@/lib/utils";

/**
 * Mobile-first row primitive for the table-fallback pattern from
 * ticket 0015.
 *
 * The blessed shape (per ticket 0015 OQ2 — "dense") is:
 *   - one title line (the row's primary identifier)
 *   - up to two secondary lines (`description` slot)
 *   - an optional status row (`badges` slot, rendered as a flex-wrap
 *     row of pre-styled badges)
 *   - an optional trailing chevron / metadata snippet (`trailing` slot)
 *
 * Tap target is ≥ 44 px (WCAG / Apple HIG). Wraps cleanly to 320 px.
 *
 * Use by *replacing* the data-table rows below the `md:` breakpoint:
 *
 *   <table className="hidden md:table t-table">{ ... }</table>
 *   <CardList className="md:hidden">
 *     {rows.map((row) => <CardListItem key={row.id} ... />)}
 *   </CardList>
 *
 * Per-route discretion: if a 4th line of metadata is genuinely
 * load-bearing (e.g. `Logs.tsx` needs purpose + model + latency + cost
 * to be skim-readable), pass a 4th node via `description` — the slot is
 * a `ReactNode`, not a string.
 */
export function CardList({
  className,
  children,
}: {
  className?: string;
  children: ReactNode;
}) {
  return (
    <ul className={cn("flex flex-col gap-2", className)}>{children}</ul>
  );
}

export interface CardListItemProps {
  /** Tap target. If `to` is set, the whole card is a `<Link>`. */
  to?: string;
  /** Tap target alternative — for non-route callbacks (selection, etc.). */
  onClick?: () => void;
  title: ReactNode;
  description?: ReactNode;
  badges?: ReactNode;
  trailing?: ReactNode;
  className?: string;
}

export function CardListItem({
  to,
  onClick,
  title,
  description,
  badges,
  trailing,
  className,
}: CardListItemProps) {
  const baseClass = cn(
    "block paper-card px-4 py-3 min-h-[44px] transition-colors hover:bg-paper-deep",
    "focus-visible:outline focus-visible:outline-2 focus-visible:outline-rust",
    className,
  );
  const inner = (
    <div className="flex items-start gap-3">
      <div className="flex-1 min-w-0 flex flex-col gap-1">
        <div className="text-sm font-medium text-ink truncate">{title}</div>
        {description && (
          <div className="text-xs text-ink-muted leading-relaxed **:wrap-break-word">
            {description}
          </div>
        )}
        {badges && (
          <div className="flex flex-wrap gap-1.5 pt-0.5">{badges}</div>
        )}
      </div>
      {trailing && (
        <div className="shrink-0 text-xs text-ink-muted whitespace-nowrap">
          {trailing}
        </div>
      )}
    </div>
  );

  if (to) {
    return (
      <li>
        <Link to={to} className={baseClass}>
          {inner}
        </Link>
      </li>
    );
  }
  if (onClick) {
    return (
      <li>
        <button type="button" onClick={onClick} className={cn(baseClass, "text-left w-full cursor-pointer")}>
          {inner}
        </button>
      </li>
    );
  }
  return <li className={baseClass}>{inner}</li>;
}
