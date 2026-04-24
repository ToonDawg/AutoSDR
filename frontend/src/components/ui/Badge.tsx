import type { ReactNode } from 'react';
import { cn } from '@/lib/utils';

type Tone = 'neutral' | 'rust' | 'forest' | 'mustard' | 'oxblood' | 'teal' | 'outline' | 'ink';

interface BadgeProps {
  tone?: Tone;
  children: ReactNode;
  className?: string;
  dot?: boolean;
  uppercase?: boolean;
}

const tones: Record<Tone, string> = {
  neutral: 'bg-paper-deep text-ink-muted border-rule',
  rust: 'bg-rust-soft text-rust-deep border-[color:var(--color-rust-deep)]/30',
  forest: 'bg-forest-soft text-forest border-[color:var(--color-forest)]/30',
  mustard: 'bg-mustard-soft text-mustard border-[color:var(--color-mustard)]/30',
  oxblood: 'bg-oxblood-soft text-oxblood border-[color:var(--color-oxblood)]/30',
  teal: 'bg-teal-soft text-teal border-[color:var(--color-teal)]/30',
  outline: 'bg-transparent text-ink border-rule-strong',
  ink: 'bg-ink text-paper border-ink',
};

const dotColours: Record<Tone, string> = {
  neutral: 'bg-ink-muted',
  rust: 'bg-rust',
  forest: 'bg-forest',
  mustard: 'bg-mustard',
  oxblood: 'bg-oxblood',
  teal: 'bg-teal',
  outline: 'bg-ink',
  ink: 'bg-paper',
};

export function Badge({ tone = 'neutral', children, className, dot, uppercase = true }: BadgeProps) {
  return (
    <span
      className={cn(
        'inline-flex items-center gap-1.5 px-2 py-0.5 text-[10px] tracking-[0.14em] font-mono font-medium border',
        uppercase && 'uppercase',
        tones[tone],
        className,
      )}
    >
      {dot && <span className={cn('h-1.5 w-1.5 rounded-full', dotColours[tone])} />}
      {children}
    </span>
  );
}
