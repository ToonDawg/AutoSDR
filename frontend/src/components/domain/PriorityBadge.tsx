import { Badge } from '@/components/ui/Badge';

/**
 * Visual marker for leads the scheduler will send before normal-tier
 * leads. Renders nothing when the lead is not priority so call sites
 * can drop it inline without conditional wrapping.
 *
 * The ``priority_reason`` token is mapped to a short label + tooltip
 * here rather than on the server so the API stays in literal-token
 * vocabulary (mirrors the ``EnrichmentStatus`` / ``HitlReason``
 * pattern). Vocabulary today (precedence top-down):
 *
 * - ``"not_found"`` (ticket 0013) — server returned 404/410.
 * - ``"social_profile_website"`` (ticket 0014) — ``Lead.website`` is
 *   a Facebook / Instagram / LinkedIn / etc. URL.
 *
 * Adding a third reason is one row in each map below.
 */

type PriorityReason = 'not_found' | 'social_profile_website' | (string & {});

const REASON_LABEL: Record<PriorityReason, string> = {
  not_found: 'Website 404',
  social_profile_website: 'Social profile',
};

const REASON_TOOLTIP: Record<PriorityReason, string> = {
  not_found:
    'Scan returned 404 from this lead\u2019s website. The pitch lands sharpest on a clearly broken site, so the scheduler sends them before normal-tier leads.',
  social_profile_website:
    'This lead\u2019s website is a social profile (Facebook, Instagram, etc.) instead of a corporate site. The scheduler sends them before normal-tier leads.',
};

interface PriorityBadgeProps {
  isPriority: boolean;
  reason: string | null;
  className?: string;
}

export function PriorityBadge({ isPriority, reason, className }: PriorityBadgeProps) {
  if (!isPriority || !reason) {
    return null;
  }
  const label = REASON_LABEL[reason as PriorityReason] ?? 'Priority';
  const tooltip =
    REASON_TOOLTIP[reason as PriorityReason] ??
    'Scheduler will send this lead before normal-tier leads.';
  return (
    <span title={tooltip} className="inline-flex">
      <Badge tone="oxblood" dot className={className}>
        {label}
      </Badge>
    </span>
  );
}
