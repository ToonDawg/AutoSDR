import { Badge } from '@/components/ui/Badge';
import type { ToneRegister } from '@/lib/types';

/**
 * Compact chip showing the tone register the analysis LLM picked for a
 * lead. The register drives the per-register voice block injected into
 * the generation prompt — the chip lets the operator see which voice
 * the draft was built against without opening the LLM call audit.
 *
 * Renders nothing for ``null`` (legacy thread / pre-analysis row /
 * analysis returned ``"unknown"``) so call sites can drop it inline
 * without conditional wrapping. Single-source labels live here so the
 * API stays in literal-token vocabulary (mirrors the
 * ``PriorityBadge`` / ``EnrichmentStatus`` / ``HitlReason`` pattern).
 *
 * Vocabulary mirrors ``ToneRegisterT`` in
 * ``autosdr/prompts/generation.py``. Adding a seventh register is one
 * row in each map below + one new literal in ``ToneRegister``.
 *
 * Ticket 0017.
 */

type Tone = 'mustard' | 'forest' | 'teal' | 'rust' | 'oxblood' | 'neutral';

const REGISTER_LABEL: Record<ToneRegister, string> = {
  tradie: 'Tradie',
  professional: 'Professional',
  hospitality: 'Hospitality',
  retail: 'Retail',
  personal_services: 'Personal services',
  aged_care: 'Aged care',
  unknown: 'Unknown',
};

const REGISTER_TONE: Record<ToneRegister, Tone> = {
  tradie: 'rust',
  professional: 'teal',
  hospitality: 'mustard',
  retail: 'mustard',
  personal_services: 'forest',
  aged_care: 'oxblood',
  unknown: 'neutral',
};

const REGISTER_TOOLTIP: Record<ToneRegister, string> = {
  tradie:
    'Tradie register \u2014 lowercase opener (\u201chey,\u201d, \u201chey mate,\u201d), contractions, dropped final full stop. The default for hands-on trades.',
  professional:
    'Professional register \u2014 capital-case opener (\u201cHi there,\u201d, \u201cHello,\u201d), clean punctuation, capitalised proper nouns. For lawyers, accountants, vets, dentists, real estate, etc.',
  hospitality:
    'Hospitality register \u2014 loose and warm, lowercase openers, references the food / drink / atmosphere. For cafes, pubs, restaurants, food trucks.',
  retail:
    'Retail register \u2014 loose and casual, sounds like a local customer not a vendor. For shops, boutiques, florists, pharmacies, butchers.',
  personal_services:
    'Personal services register \u2014 warm and approachable, lowercase \u201chey,\u201d / \u201chi there,\u201d but no \u201chey mate,\u201d. For salons, spas, gyms, yoga, photographers.',
  aged_care:
    'Aged care register \u2014 casual but precise, no \u201chey mate,\u201d. Empathy clauses land well. For aged care, GP / medical, allied health, schools, childcare.',
  unknown:
    'Register the analysis LLM couldn\u2019t classify. The generation prompt skips the per-register voice block and falls back to the workspace tone + base rules.',
};

interface RegisterChipProps {
  register: ToneRegister | string | null;
  className?: string;
}

export function RegisterChip({ register, className }: RegisterChipProps) {
  if (!register || register === 'unknown') {
    return null;
  }
  // Defensive: any out-of-vocab token (e.g. a typo persisted from an
  // older client cache) renders as a quiet neutral chip rather than
  // crashing the row.
  const known = (register in REGISTER_LABEL ? register : 'unknown') as ToneRegister;
  const label = REGISTER_LABEL[known];
  const tone = REGISTER_TONE[known];
  const tooltip = REGISTER_TOOLTIP[known];
  return (
    <span title={tooltip} className="inline-flex">
      <Badge tone={tone} dot className={className}>
        {label}
      </Badge>
    </span>
  );
}
