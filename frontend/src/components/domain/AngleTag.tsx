/**
 * Compact badge showing which personalisation angle the analysis LLM
 * picked for a lead. Angles are freeform strings generated per-industry
 * by ``autosdr/prompts/analysis.py``, so we don't try to map them to a
 * fixed label — the string itself is the label.
 */

export function AngleTag({ angle }: { angle: string | null }) {
  if (!angle) {
    return (
      <span className="inline-flex items-center gap-2 font-mono text-[11px] uppercase tracking-[0.14em] text-ink-faint">
        no angle
      </span>
    );
  }
  return (
    <span className="inline-flex items-baseline gap-2 font-mono text-[11px] uppercase tracking-[0.14em]">
      <span className="text-ink-muted">angle ·</span>
      <span className="text-ink font-medium">{angle}</span>
    </span>
  );
}
