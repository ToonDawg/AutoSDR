import { useQuery } from '@tanstack/react-query';
import { Link, useParams } from 'react-router-dom';
import { Activity, ExternalLink, Globe, MapPin, MessageSquare, Phone, Star } from 'lucide-react';
import { api } from '@/lib/api';
import { Badge } from '@/components/ui/Badge';
import { BackLink } from '@/components/ui/BackLink';
import {
  CONTACT_TYPE_LABEL,
  LEAD_STATUS_LABEL,
  absTime,
  formatDoNotContactReason,
  formatPhone,
  formatSkipReason,
  relTime,
} from '@/lib/format';
import type { LeadStatusT, Thread } from '@/lib/types';
import { cn } from '@/lib/utils';

const STATUS_TONE: Record<LeadStatusT, Parameters<typeof Badge>[0]['tone']> = {
  new: 'neutral',
  contacted: 'teal',
  replied: 'rust',
  won: 'forest',
  lost: 'oxblood',
  skipped: 'neutral',
};

/**
 * Full lead card.
 *
 * Scrapes carry far more context than the compact index row has room for
 * — ratings, review text, plus-codes, source search terms. The detail
 * route pulls the row from the API and renders those keys in a
 * structured way (rating + review excerpts separated out), then dumps
 * anything it doesn't recognise in a catch-all panel so nothing is lost.
 */
export function LeadDetail() {
  const { id = '' } = useParams();

  const { data: lead, isLoading, isError, error } = useQuery({
    queryKey: ['lead', id],
    queryFn: () => api.getLead(id),
    enabled: !!id,
  });
  const { data: threads } = useQuery({
    queryKey: ['threads', 'lead', id],
    queryFn: () => api.listThreads({ leadId: id, limit: 20 }),
    enabled: !!id,
  });

  if (isLoading) {
    return (
      <div className="page-narrow gap-6">
        <div className="h-4 bg-paper-deep animate-pulse w-48" />
        <div className="h-10 bg-paper-deep animate-pulse w-2/3" />
        <div className="h-40 bg-paper-deep animate-pulse" />
      </div>
    );
  }

  if (isError || !lead) {
    return (
      <div className="page-narrow gap-6">
        <BackLink to="/leads">All leads</BackLink>
        <div className="paper-card px-5 py-4 border-oxblood text-oxblood text-sm">
          Couldn't load this lead. {String(error ?? '')}
        </div>
      </div>
    );
  }

  const raw = lead.raw_data ?? {};
  const rating = asNumber(raw.rating);
  const reviewsCount = asNumber(raw.reviews);
  const reviewDetails = asReviewList(raw.reviewDetails);
  const searchQuery = asString(raw.searchQuery);
  const scrapedAt = asString(raw.scrapedAt);
  const plusCode = asString(raw.plusCode);

  const knownKeys = new Set([
    'name',
    'category',
    'address',
    'phone',
    'website',
    'rating',
    'reviews',
    'reviewDetails',
    'searchQuery',
    'scrapedAt',
    'plusCode',
    'webResults',
  ]);
  const extras = Object.entries(raw).filter(
    ([k, v]) => !knownKeys.has(k) && v !== null && v !== '',
  );
  const leadThreads = threads ?? [];
  const campaignLinks = uniqueCampaignLinks(leadThreads);

  return (
    <div className="page-narrow gap-6">
      <BackLink to="/leads">All leads</BackLink>

      <header className="border-b border-rule pb-5">
        <div className="flex items-center gap-3 mb-2">
          <Badge tone={STATUS_TONE[lead.status]} dot>
            {LEAD_STATUS_LABEL[lead.status]}
          </Badge>
          {lead.do_not_contact_at && (
            <Badge tone="oxblood" dot>
              Opted out
            </Badge>
          )}
          {lead.contact_type && (
            <Badge tone="outline">
              {CONTACT_TYPE_LABEL[lead.contact_type] ?? lead.contact_type}
            </Badge>
          )}
          <span className="font-mono text-[11px] text-ink-faint">
            #{String(lead.import_order).padStart(3, '0')}
          </span>
        </div>
        <h1 className="text-2xl font-medium mb-1">{lead.name ?? 'Unnamed lead'}</h1>
        {lead.category && (
          <p className="text-sm text-ink-muted">{lead.category}</p>
        )}
      </header>

      {lead.do_not_contact_at && (
        <div className="border border-oxblood/40 bg-oxblood-soft/60 px-5 py-3 text-sm text-ink">
          <div className="flex items-center justify-between gap-3">
            <span>
              <span className="label text-oxblood mr-2">Do not contact</span>
              {formatDoNotContactReason(lead.do_not_contact_reason)}
            </span>
            <span className="font-mono text-[11px] text-ink-faint shrink-0">
              {absTime(lead.do_not_contact_at)}
            </span>
          </div>
        </div>
      )}

      {lead.skip_reason && lead.skip_reason !== 'do_not_contact' && (
        <div className="border border-oxblood/30 bg-oxblood-soft/40 px-5 py-3 text-sm text-ink">
          <span className="label text-oxblood mr-2">Not messaging</span>
          {formatSkipReason(lead.skip_reason)}
        </div>
      )}

      <section className="paper-card px-5 py-4">
        <div className="flex items-start justify-between gap-4 mb-4">
          <div>
            <div className="label mb-1">Workflow trace</div>
            <p className="text-sm text-ink-muted">
              Jump from this lead to the campaign, conversation, or LLM audit trail.
            </p>
          </div>
          <Link
            to={`/logs?lead=${lead.id}`}
            className="inline-flex items-center gap-1.5 text-xs text-ink-muted hover:text-rust underline-offset-2 hover:underline shrink-0"
          >
            LLM logs
            <ExternalLink className="h-3 w-3" strokeWidth={1.5} />
          </Link>
        </div>

        {leadThreads.length > 0 ? (
          <div className="flex flex-col gap-3">
            {leadThreads.map((thread) => (
              <div
                key={thread.id}
                className="grid gap-3 border border-rule bg-paper px-4 py-3 md:grid-cols-[1fr_auto]"
              >
                <div className="min-w-0">
                  <div className="flex items-center gap-2 text-sm">
                    <MessageSquare className="h-4 w-4 text-ink-muted" strokeWidth={1.5} />
                    <Link
                      to={`/threads/${thread.id}`}
                      className="font-medium text-ink hover:text-rust truncate"
                    >
                      {thread.campaign_name || 'Conversation'}
                    </Link>
                  </div>
                  <div className="mt-1 text-xs text-ink-muted truncate">
                    {thread.angle ?? thread.hitl_reason ?? 'No angle recorded yet'}
                  </div>
                </div>
                <div className="flex items-center gap-3 text-xs">
                  {thread.campaign_id && (
                    <Link
                      to={`/campaigns/${thread.campaign_id}`}
                      className="text-ink-muted hover:text-rust underline-offset-2 hover:underline"
                    >
                      Campaign
                    </Link>
                  )}
                  <Link
                    to={`/logs?thread=${thread.id}`}
                    className="text-ink-muted hover:text-rust underline-offset-2 hover:underline"
                  >
                    Thread logs
                  </Link>
                </div>
              </div>
            ))}
            {campaignLinks.length > 1 && (
              <div className="flex flex-wrap items-center gap-2 border-t border-rule pt-3 text-xs text-ink-muted">
                <Activity className="h-3.5 w-3.5" strokeWidth={1.5} />
                Campaigns:
                {campaignLinks.map((campaign) => (
                  <Link
                    key={campaign.id}
                    to={`/campaigns/${campaign.id}`}
                    className="text-ink hover:text-rust underline-offset-2 hover:underline"
                  >
                    {campaign.name}
                  </Link>
                ))}
              </div>
            )}
          </div>
        ) : (
          <div className="border border-dashed border-rule px-4 py-5 text-sm text-ink-muted">
            No conversation has been started for this lead yet. Logs will appear once the
            analysis or generation pipeline touches it.
          </div>
        )}
      </section>

      <section className="paper-card px-5 py-4 flex flex-col gap-3">
        <InfoRow
          icon={<Phone className="h-4 w-4" strokeWidth={1.5} />}
          label="Phone"
          value={
            <span className="font-mono text-ink">{formatPhone(lead.contact_uri)}</span>
          }
        />
        {lead.website && (
          <InfoRow
            icon={<Globe className="h-4 w-4" strokeWidth={1.5} />}
            label="Website"
            value={
              <a
                href={normaliseUrl(lead.website)}
                target="_blank"
                rel="noreferrer"
                className="inline-flex items-center gap-1 text-ink hover:text-rust underline-offset-2 hover:underline truncate"
              >
                {stripProtocol(lead.website)}
                <ExternalLink className="h-3 w-3 shrink-0" strokeWidth={1.5} />
              </a>
            }
          />
        )}
        {lead.address && (
          <InfoRow
            icon={<MapPin className="h-4 w-4" strokeWidth={1.5} />}
            label="Address"
            value={<span className="text-ink">{lead.address}</span>}
          />
        )}
        {rating != null && (
          <InfoRow
            icon={<Star className="h-4 w-4" strokeWidth={1.5} />}
            label="Rating"
            value={
              <span className="text-ink">
                <span className="font-mono tabular-nums">{rating.toFixed(1)}</span>
                {reviewsCount != null && (
                  <span className="text-ink-muted">
                    {' '}
                    · {reviewsCount.toLocaleString()} review
                    {reviewsCount === 1 ? '' : 's'}
                  </span>
                )}
              </span>
            }
          />
        )}
      </section>

      {reviewDetails.length > 0 && (
        <section className="flex flex-col gap-3">
          <div className="flex items-end justify-between pb-2 border-b border-rule">
            <h2 className="text-sm font-medium">Reviews</h2>
            <span className="text-[11px] font-mono text-ink-faint">
              {reviewDetails.length} of {reviewsCount ?? reviewDetails.length}
            </span>
          </div>
          <ul className="flex flex-col gap-3">
            {reviewDetails.map((r, i) => (
              <li
                key={(r.id as string | undefined) ?? i}
                className="paper-card px-4 py-3"
              >
                <div className="flex items-baseline justify-between gap-3 mb-1">
                  <div className="text-sm font-medium text-ink truncate">
                    {r.authorName ?? 'Anonymous'}
                  </div>
                  <div className="flex items-center gap-2 shrink-0">
                    {typeof r.rating === 'number' && (
                      <span className="inline-flex items-center gap-0.5 font-mono text-[11px] text-mustard">
                        <Star className="h-3 w-3 fill-current" strokeWidth={0} />
                        {r.rating}
                      </span>
                    )}
                    {r.relativeTime && (
                      <span className="text-[11px] text-ink-faint">
                        {r.relativeTime}
                      </span>
                    )}
                  </div>
                </div>
                {r.text && (
                  <p className="text-sm text-ink-muted leading-relaxed whitespace-pre-wrap">
                    {r.text}
                  </p>
                )}
              </li>
            ))}
          </ul>
        </section>
      )}

      <section className="paper-card px-5 py-4">
        <div className="label mb-3">Import context</div>
        <dl className="grid grid-cols-[auto_1fr] gap-x-6 gap-y-2 text-sm">
          <dt className="text-ink-muted">Added</dt>
          <dd className="font-mono text-xs text-ink">{relTime(lead.created_at)}</dd>
          {lead.source_file && (
            <>
              <dt className="text-ink-muted">Source file</dt>
              <dd className="font-mono text-xs text-ink truncate">{lead.source_file}</dd>
            </>
          )}
          {searchQuery && (
            <>
              <dt className="text-ink-muted">Search query</dt>
              <dd className="text-sm text-ink">{searchQuery}</dd>
            </>
          )}
          {scrapedAt && (
            <>
              <dt className="text-ink-muted">Scraped</dt>
              <dd className="font-mono text-xs text-ink">{scrapedAt}</dd>
            </>
          )}
          {plusCode && (
            <>
              <dt className="text-ink-muted">Plus code</dt>
              <dd className="font-mono text-xs text-ink">{plusCode}</dd>
            </>
          )}
        </dl>
      </section>

      {extras.length > 0 && (
        <section className="paper-card px-5 py-4">
          <div className="label mb-3">Other fields</div>
          <dl className="grid grid-cols-[auto_1fr] gap-x-6 gap-y-2 text-sm">
            {extras.map(([k, v]) => (
              <div key={k} className="contents">
                <dt className="text-ink-muted font-mono text-[11px]">{k}</dt>
                <dd
                  className={cn(
                    'text-ink wrap-break-word',
                    typeof v === 'object' && 'font-mono text-[11px] text-ink-muted',
                  )}
                >
                  {typeof v === 'object'
                    ? JSON.stringify(v, null, 2)
                    : String(v)}
                </dd>
              </div>
            ))}
          </dl>
        </section>
      )}
    </div>
  );
}

interface ReviewDetail {
  id?: string;
  authorName?: string;
  rating?: number;
  relativeTime?: string;
  text?: string;
}

function asNumber(v: unknown): number | null {
  if (typeof v === 'number' && Number.isFinite(v)) return v;
  if (typeof v === 'string' && v !== '' && !Number.isNaN(Number(v))) return Number(v);
  return null;
}

function asString(v: unknown): string | null {
  if (typeof v === 'string' && v.trim() !== '') return v;
  return null;
}

function asReviewList(v: unknown): ReviewDetail[] {
  if (!Array.isArray(v)) return [];
  return v.filter((r): r is ReviewDetail => typeof r === 'object' && r !== null);
}

function normaliseUrl(url: string): string {
  if (/^https?:\/\//i.test(url)) return url;
  return `https://${url}`;
}

function stripProtocol(url: string): string {
  return url.replace(/^https?:\/\//i, '').replace(/\/$/, '');
}

function uniqueCampaignLinks(threads: Thread[]): Array<{ id: string; name: string }> {
  const campaigns = new Map<string, string>();
  for (const thread of threads) {
    if (thread.campaign_id && thread.campaign_name) {
      campaigns.set(thread.campaign_id, thread.campaign_name);
    }
  }
  return Array.from(campaigns, ([id, name]) => ({ id, name }));
}

function InfoRow({
  icon,
  label,
  value,
}: {
  icon: React.ReactNode;
  label: string;
  value: React.ReactNode;
}) {
  return (
    <div className="grid grid-cols-[1.5rem_6rem_1fr] items-baseline gap-3">
      <span className="text-ink-muted">{icon}</span>
      <span className="label">{label}</span>
      <span className="min-w-0 text-sm">{value}</span>
    </div>
  );
}
