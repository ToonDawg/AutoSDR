import { forwardRef, type ButtonHTMLAttributes, type ReactNode } from 'react';
import { cn } from '@/lib/utils';

type Variant = 'primary' | 'secondary' | 'ghost' | 'danger' | 'success' | 'link';
type Size = 'sm' | 'md' | 'lg';

interface ButtonProps extends ButtonHTMLAttributes<HTMLButtonElement> {
  variant?: Variant;
  size?: Size;
  iconLeft?: ReactNode;
  iconRight?: ReactNode;
}

const base =
  'inline-flex items-center justify-center gap-2 font-sans font-medium whitespace-nowrap transition-all duration-150 cursor-pointer select-none disabled:opacity-40 disabled:cursor-not-allowed active:translate-y-[0.5px]';

const variants: Record<Variant, string> = {
  primary:
    'bg-ink text-paper border border-ink hover:bg-rust hover:border-rust hover:text-paper',
  secondary:
    'bg-paper text-ink border border-ink hover:bg-ink hover:text-paper',
  ghost:
    'bg-transparent text-ink border border-transparent hover:bg-paper-deep',
  danger:
    'bg-oxblood text-paper border border-oxblood hover:bg-rust hover:border-rust',
  success:
    'bg-forest text-paper border border-forest hover:brightness-110',
  link:
    'bg-transparent text-ink border-0 px-0 py-0 hover:text-rust underline-offset-4 hover:underline',
};

const sizes: Record<Size, string> = {
  sm: 'text-xs h-7 px-3',
  md: 'text-sm h-9 px-4',
  lg: 'text-sm h-11 px-5',
};

export const Button = forwardRef<HTMLButtonElement, ButtonProps>(function Button(
  { variant = 'secondary', size = 'md', className, iconLeft, iconRight, children, ...rest },
  ref,
) {
  return (
    <button
      ref={ref}
      className={cn(base, variants[variant], variant !== 'link' && sizes[size], className)}
      {...rest}
    >
      {iconLeft && <span className="-ml-0.5 inline-flex">{iconLeft}</span>}
      <span>{children}</span>
      {iconRight && <span className="-mr-0.5 inline-flex">{iconRight}</span>}
    </button>
  );
});
