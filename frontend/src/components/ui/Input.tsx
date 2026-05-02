import { forwardRef, type InputHTMLAttributes, type TextareaHTMLAttributes } from 'react';
import { cn } from '@/lib/utils';

// Mobile floor (<md): min-h-11 = 44px tappable. Desktop (md+) returns to
// the natural py-2 / line-height height so dense forms aren't bloated.
const inputBase =
  'w-full bg-paper border border-rule-strong px-3 py-2 min-h-11 md:min-h-0 text-sm font-sans text-ink placeholder:text-ink-faint focus:outline-none focus:border-ink transition-colors';

export const Input = forwardRef<HTMLInputElement, InputHTMLAttributes<HTMLInputElement>>(
  function Input({ className, ...rest }, ref) {
    return <input ref={ref} className={cn(inputBase, className)} {...rest} />;
  },
);

export const Textarea = forwardRef<HTMLTextAreaElement, TextareaHTMLAttributes<HTMLTextAreaElement>>(
  function Textarea({ className, ...rest }, ref) {
    return (
      <textarea
        ref={ref}
        className={cn(inputBase, 'resize-y min-h-20 leading-relaxed', className)}
        {...rest}
      />
    );
  },
);
