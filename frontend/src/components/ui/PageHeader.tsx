import type { ReactNode } from 'react';

interface PageHeaderProps {
  title: ReactNode;
  description?: ReactNode;
  right?: ReactNode;
}

/**
 * The title / description / right-rail header shared by every index page.
 *
 * Keep this stupid-simple: one rule below, title on the left, an optional
 * action slot on the right. Pages that want a different hero (Dashboard,
 * ThreadDetail) intentionally don't use this.
 */
export function PageHeader({ title, description, right }: PageHeaderProps) {
  return (
    <header className="border-b border-rule pb-4 flex items-end justify-between gap-6">
      <div>
        <h1 className="text-2xl font-medium">{title}</h1>
        {description && <p className="text-sm text-ink-muted mt-1 max-w-prose">{description}</p>}
      </div>
      {right && <div className="shrink-0">{right}</div>}
    </header>
  );
}
