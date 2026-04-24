import type { ReactNode } from 'react';

/**
 * Labelled form field with an optional hint below the control. Marks
 * the label with a rust asterisk when `required` is set (used by the
 * first-run Setup wizard; other forms leave it off). Style follows
 * the global `.label` utility in `index.css`: tiny mono uppercase.
 */
export function Field({
  label,
  hint,
  required,
  children,
}: {
  label: string;
  hint?: string;
  required?: boolean;
  children: ReactNode;
}) {
  return (
    <label className="flex flex-col gap-1.5">
      <span className="label">
        {label}
        {required && <span className="ml-1 text-rust">*</span>}
      </span>
      {children}
      {hint && <span className="text-xs text-ink-faint">{hint}</span>}
    </label>
  );
}
