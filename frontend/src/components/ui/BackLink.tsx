import { ArrowLeft } from 'lucide-react';
import { Link } from 'react-router-dom';
import type { MouseEventHandler, ReactNode } from 'react';

interface BackLinkProps {
  children: ReactNode;
  to?: string;
  onClick?: MouseEventHandler<HTMLButtonElement>;
}

/**
 * Breadcrumb-style "back to X" control. Pass `to` for history-safe links,
 * or `onClick` for behaviours like "go back one step" or "clear filter"
 * that shouldn't own a URL.
 */
export function BackLink({ children, to, onClick }: BackLinkProps) {
  const content = (
    <>
      <ArrowLeft className="h-3 w-3" strokeWidth={1.5} />
      {children}
    </>
  );
  const className =
    'inline-flex items-center gap-1.5 font-mono text-[11px] tracking-[0.18em] uppercase text-ink-muted hover:text-ink cursor-pointer self-start';

  if (to) {
    return (
      <Link to={to} className={className}>
        {content}
      </Link>
    );
  }
  return (
    <button type="button" onClick={onClick} className={className}>
      {content}
    </button>
  );
}
