import { Badge } from '@/components/ui/Badge';

/**
 * Informational chip that shows the social platform when a lead's
 * ``website`` is a social-profile URL (Facebook page, LinkedIn page,
 * etc., ticket 0014). Renders nothing when the platform token is
 * absent so call sites can drop it inline without conditional
 * wrapping.
 *
 * Independent of priority — a 404'd Facebook URL still shows as
 * ``"Facebook profile"`` here even though ``PriorityBadge`` will
 * read ``"Website 404"`` (precedence). Operators see both signals
 * separately so they can tell *why* a lead is in the priority tier
 * vs. just *that* it is.
 *
 * Adding an eighth platform is one row in ``PLATFORM_LABELS`` and
 * one line in ``autosdr.enrichment_vocab.SOCIAL_HOSTS``.
 */

const PLATFORM_LABELS: Record<string, string> = {
  facebook: 'Facebook profile',
  instagram: 'Instagram profile',
  linkedin: 'LinkedIn profile',
  twitter: 'Twitter profile',
  x: 'X profile',
  tiktok: 'TikTok profile',
  youtube: 'YouTube profile',
};

interface SocialProfileTagProps {
  platform: string | null | undefined;
  className?: string;
}

export function SocialProfileTag({ platform, className }: SocialProfileTagProps) {
  if (!platform) {
    return null;
  }
  const label = PLATFORM_LABELS[platform] ?? `${platform} profile`;
  const tooltip =
    'This lead\u2019s website is a social profile, not a corporate site.';
  return (
    <span title={tooltip} className="inline-flex">
      <Badge tone="mustard" dot className={className}>
        {label}
      </Badge>
    </span>
  );
}
