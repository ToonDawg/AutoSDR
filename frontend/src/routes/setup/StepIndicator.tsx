import type { Step } from './types';

const LABELS = ['Business', 'LLM', 'Connector'];

/**
 * Three-dot step indicator shown in the setup wizard's header.
 *
 * Pure display, no state. Completed steps get inverted chrome, the
 * current step gets a solid outline, upcoming steps render muted.
 */
export function StepIndicator({ step }: { step: Step }) {
  return (
    <ol className="flex items-center gap-2 text-[11px] font-mono tracking-wide uppercase text-ink-muted">
      {LABELS.map((label, i) => (
        <li key={label} className="flex items-center gap-2">
          <span
            className={
              'h-5 w-5 flex items-center justify-center border ' +
              (i < step
                ? 'bg-ink text-paper border-ink'
                : i === step
                  ? 'border-ink text-ink'
                  : 'border-rule text-ink-faint')
            }
          >
            {i + 1}
          </span>
          <span className={i === step ? 'text-ink' : ''}>{label}</span>
          {i < LABELS.length - 1 && <span className="text-ink-faint">·</span>}
        </li>
      ))}
    </ol>
  );
}
