import type { ConnectorType } from "@/lib/types";
import { cn } from "@/lib/utils";

interface ConnectorPickerProps {
  type: ConnectorType;
  active: boolean;
  onClick: () => void;
}

/**
 * Card-style selector for the SMS connector, shared by the first-run
 * setup wizard and the Settings page.
 *
 * The copy deliberately lives here (not in call sites) so both entry
 * points describe each backend identically — there used to be two
 * different blurbs for the same connector, which was confusing when
 * a user compared them.
 */
const META: Record<ConnectorType, { title: string; blurb: string }> = {
  file: {
    title: "File",
    blurb: "Append drafts to data/outbox.jsonl. No SMS leaves the box.",
  },
  textbee: {
    title: "TextBee",
    blurb: "Send via a linked Android phone acting as a gateway.",
  },
  smsgate: {
    title: "SMSGate",
    blurb: "Self-hosted SMSGate server on your network.",
  },
};

export function ConnectorPicker({
  type,
  active,
  onClick,
}: ConnectorPickerProps) {
  const { title, blurb } = META[type];
  return (
    <button
      type="button"
      onClick={onClick}
      className={cn(
        "text-left px-4 py-3 border transition-colors",
        active
          ? "border-ink bg-paper-deep"
          : "border-rule hover:border-rule-strong",
      )}
    >
      <div className="text-sm font-medium">{title}</div>
      <div className="mt-0.5 text-xs text-ink-muted leading-snug">{blurb}</div>
    </button>
  );
}
