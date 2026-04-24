import type { ReactNode } from 'react';

/**
 * Display primitives specific to the setup wizard. The shared `Field`
 * form primitive lives in `components/ui/Field.tsx` — import it from
 * there; this module only owns the wizard-style step card shell.
 */

export function StepCard({
  title,
  description,
  children,
}: {
  title: string;
  description: string;
  children: ReactNode;
}) {
  return (
    <div className="paper-card px-8 py-8">
      <h1 className="text-xl font-medium">{title}</h1>
      <p className="mt-2 text-sm text-ink-muted max-w-prose">{description}</p>
      <div className="mt-6 space-y-5">{children}</div>
    </div>
  );
}
