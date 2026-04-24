import type { ReactNode } from 'react';
import * as SwitchPrimitive from '@radix-ui/react-switch';
import { Button } from '@/components/ui/Button';
import { cn } from '@/lib/utils';

/**
 * Card shell used on Settings. Renders a header, the children as a
 * vertical stack, and an optional footer (conventionally a SaveRow).
 */
export function Card({
  title,
  description,
  children,
  footer,
}: {
  title: string;
  description?: string;
  children: ReactNode;
  footer?: ReactNode;
}) {
  return (
    <section className="paper-card px-6 py-5">
      <header className="mb-4 pb-3 border-b border-rule">
        <h2 className="text-base font-medium">{title}</h2>
        {description && <p className="mt-1 text-xs text-ink-muted">{description}</p>}
      </header>
      <div className="flex flex-col gap-5">{children}</div>
      {footer && (
        <div className="mt-5 pt-4 border-t border-rule">{footer}</div>
      )}
    </section>
  );
}

/** On/off slide switch with label + description. */
export function Toggle({
  label,
  description,
  checked,
  onToggle,
}: {
  label: string;
  description: string;
  checked: boolean;
  onToggle: () => void;
}) {
  return (
    <div className="flex items-start gap-4">
      <div className="flex-1">
        <div className="text-sm font-medium">{label}</div>
        <p className="text-xs text-ink-muted leading-relaxed mt-1">{description}</p>
      </div>
      <SwitchPrimitive.Root
        checked={checked}
        onCheckedChange={onToggle}
        className={cn(
          'shrink-0 relative h-6 w-11 rounded-full border transition-colors cursor-pointer',
          'data-[state=checked]:bg-ink data-[state=checked]:border-ink',
          'data-[state=unchecked]:bg-paper data-[state=unchecked]:border-rule-strong',
        )}
      >
        <SwitchPrimitive.Thumb
          className={cn(
            'block h-4 w-4 rounded-full transition-transform duration-150 will-change-transform',
            'data-[state=checked]:bg-paper data-[state=checked]:translate-x-[22px]',
            'data-[state=unchecked]:bg-ink data-[state=unchecked]:translate-x-[2px]',
          )}
        />
      </SwitchPrimitive.Root>
    </div>
  );
}

/** Save button + "unsaved changes" hint, shared across all cards. */
export function SaveRow({
  dirty,
  pending,
  onSave,
}: {
  dirty: boolean;
  pending: boolean;
  onSave: () => void;
}) {
  return (
    <div className="flex items-center justify-end gap-3">
      {dirty && <span className="text-xs text-ink-muted">Unsaved changes.</span>}
      <Button variant={dirty ? 'primary' : 'ghost'} disabled={!dirty || pending} onClick={onSave}>
        {pending ? 'Saving…' : 'Save'}
      </Button>
    </div>
  );
}
