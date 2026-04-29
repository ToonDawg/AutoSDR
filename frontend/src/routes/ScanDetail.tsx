import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Link, useParams } from 'react-router-dom';
import { ExternalLink, RefreshCcw } from 'lucide-react';
import { api } from '@/lib/api';
import { Badge } from '@/components/ui/Badge';
import { BackLink } from '@/components/ui/BackLink';
import { Button } from '@/components/ui/Button';
import { absTime, relTime } from '@/lib/format';
import type { LeadEnrichment, ScanStatus } from '@/lib/types';

const STATUS_TONE: Record<ScanStatus, Parameters<typeof Badge>[0]['tone']> = {
  ok: 'forest',
  never_scanned: 'outline',
  timeout: 'mustard',
  blocked: 'oxblood',
  error: 'oxblood',
  not_found: 'oxblood',
  empty_shell: 'neutral',
  no_url: 'neutral',
  killswitch_aborted: 'oxblood',
  disabled: 'neutral',
};

/**
 * Full scan detail for a single lead.
 *
 * Renders both the parsed ``signals.*`` keys (title, h1, CMS,
 * sitemap, socials, blocked indicators) and the raw ``_meta`` block
 * verbatim so the operator can audit exactly what the fetcher saw —
 * status, fetched-at, robots-respected, the request URL chain
 * (``requested_url`` → ``final_url``), and the connector identity.
 *
 * The "Re-scan now" button hits ``POST /api/scans/run`` with the lead
 * id, which scans synchronously inside the request and returns the
 * fresh status. We invalidate the scan query after success so the
 * page repaints with the new envelope without an extra round-trip.
 */
export function ScanDetail() {
  const { leadId } = useParams<{ leadId: string }>();
  const qc = useQueryClient();

  const { data, isLoading, error } = useQuery({
    queryKey: ['scan', leadId],
    queryFn: () => api.getScan(leadId!),
    enabled: Boolean(leadId),
  });

  const rescan = useMutation({
    mutationFn: () => api.runScans({ lead_id: leadId! }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['scan', leadId] });
      qc.invalidateQueries({ queryKey: ['scans'] });
    },
  });

  if (isLoading) {
    return (
      <div className="page gap-5">
        <div className="h-10 bg-paper-deep animate-pulse w-2/3" />
      </div>
    );
  }
  if (error || !data) {
    return (
      <div className="page gap-5">
        <BackLink to="/scans">Back to scans</BackLink>
        <div className="paper-card px-5 py-4 text-sm text-ink-muted">
          Could not load scan details. The lead may have been removed.
        </div>
      </div>
    );
  }

  const envelope = data.enrichment;
  const signals = (envelope?.signals ?? {}) as Record<string, unknown>;
  const status = data.status;
  const tone = STATUS_TONE[status] ?? 'neutral';

  return (
    <div className="page gap-5">
      <BackLink to="/scans">Back to scans</BackLink>

      <header className="flex items-end justify-between gap-4 pb-4 border-b border-rule">
        <div>
          <h1 className="text-2xl font-medium text-ink">
            {data.lead_name ?? 'Unnamed lead'}
          </h1>
          <p className="mt-2 flex items-center gap-3 text-sm text-ink-muted">
            <Badge tone={tone} dot>
              {status}
            </Badge>
            {data.website ? (
              <a
                href={data.website}
                target="_blank"
                rel="noopener noreferrer"
                className="font-mono text-xs text-ink-muted hover:text-ink inline-flex items-center gap-1"
              >
                {data.website}
                <ExternalLink className="h-3 w-3" strokeWidth={1.5} />
              </a>
            ) : (
              <span className="text-ink-faint">no website on lead</span>
            )}
          </p>
        </div>
        <div className="flex items-center gap-3">
          <Link
            to={`/leads/${data.lead_id}`}
            className="font-mono text-[11px] tracking-[0.14em] uppercase text-ink-muted hover:text-ink"
          >
            ← Lead detail
          </Link>
          <Link
            to={`/logs?lead=${data.lead_id}`}
            className="font-mono text-[11px] tracking-[0.14em] uppercase text-ink-muted hover:text-ink"
          >
            LLM trail →
          </Link>
          <Button
            variant="primary"
            size="sm"
            iconLeft={<RefreshCcw className="h-4 w-4" strokeWidth={1.5} />}
            onClick={() => rescan.mutate()}
            disabled={rescan.isPending || !data.website}
          >
            {rescan.isPending ? 'Scanning…' : 'Re-scan now'}
          </Button>
        </div>
      </header>

      {rescan.isError && (
        <div className="paper-card px-5 py-3 text-sm text-oxblood">
          Re-scan failed. Check the server log for details.
        </div>
      )}

      {!envelope && (
        <div className="paper-card px-5 py-6 text-sm text-ink-muted">
          This lead has not been enriched yet. Press <strong>Re-scan now</strong> to enrich it
          on demand.
        </div>
      )}

      {envelope && (
        <>
          <SignalsCard signals={signals} envelope={envelope} />
          <MetaCard envelope={envelope} />
        </>
      )}
    </div>
  );
}

function SignalsCard({
  signals,
  envelope,
}: {
  signals: Record<string, unknown>;
  envelope: LeadEnrichment;
}) {
  const socials = Array.isArray(signals.external_links_to_socials)
    ? (signals.external_links_to_socials as string[])
    : [];

  const rows: Array<[string, React.ReactNode]> = [];
  if (typeof signals.title === 'string' && signals.title) {
    rows.push(['Title', signals.title]);
  }
  if (typeof signals.h1 === 'string' && signals.h1) {
    rows.push(['H1', signals.h1]);
  }
  if (typeof signals.meta_description === 'string' && signals.meta_description) {
    rows.push(['Meta description', signals.meta_description]);
  }
  if (typeof signals.cms === 'string' && signals.cms) {
    rows.push([
      'CMS',
      <span className="font-mono text-xs text-ink">{signals.cms}</span>,
    ]);
  }
  if (typeof signals.sitemap_count === 'number') {
    const lastMod =
      typeof signals.sitemap_last_modified === 'string'
        ? signals.sitemap_last_modified
        : null;
    rows.push([
      'Sitemap',
      <span className="font-mono text-xs text-ink tabular-nums">
        {signals.sitemap_count} pages{lastMod ? `, last edit ${lastMod}` : ''}
      </span>,
    ]);
  }
  if (socials.length > 0) {
    rows.push([
      'Socials',
      <span className="text-xs text-ink wrap-break-word">
        {socials.map((url, i) => (
          <span key={url} className="font-mono">
            {url}
            {i < socials.length - 1 ? ', ' : ''}
          </span>
        ))}
      </span>,
    ]);
  }

  return (
    <section className="paper-card px-5 py-4">
      <div className="flex items-baseline justify-between gap-3 mb-3">
        <span className="label">Parsed signals</span>
        <span className="font-mono text-[11px] text-ink-faint">
          fetched {absTime(envelope._meta.fetched_at)}
        </span>
      </div>
      {rows.length === 0 ? (
        <p className="text-sm text-ink-muted">
          No structural signals captured. The fetcher returned, but the page
          didn't expose anything useful (likely an SPA or a redirect to a
          third-party booking widget).
        </p>
      ) : (
        <dl className="grid grid-cols-[auto_1fr] gap-x-6 gap-y-2 text-sm">
          {rows.map(([key, value]) => (
            <FragmentRow key={key} term={key}>
              {value}
            </FragmentRow>
          ))}
        </dl>
      )}
    </section>
  );
}

function FragmentRow({ term, children }: { term: string; children: React.ReactNode }) {
  return (
    <>
      <dt className="text-ink-muted">{term}</dt>
      <dd className="text-ink wrap-break-word">{children}</dd>
    </>
  );
}

function MetaCard({ envelope }: { envelope: LeadEnrichment }) {
  const meta = envelope._meta;
  return (
    <section className="paper-card px-5 py-4">
      <div className="flex items-baseline justify-between gap-3 mb-3">
        <span className="label">Fetcher metadata</span>
        <span className="font-mono text-[11px] text-ink-faint">
          envelope v{meta.version}
        </span>
      </div>
      <dl className="grid grid-cols-[auto_1fr] gap-x-6 gap-y-2 text-sm">
        <FragmentRow term="Status">
          <span className="font-mono text-xs">{meta.status}</span>
        </FragmentRow>
        <FragmentRow term="Fetched at">
          <span className="font-mono text-xs text-ink">
            {absTime(meta.fetched_at)} <span className="text-ink-muted">({relTime(meta.fetched_at)})</span>
          </span>
        </FragmentRow>
        {meta.connector && (
          <FragmentRow term="Connector">
            <span className="font-mono text-xs">
              {meta.connector}
              {meta.connector_version ? ` v${meta.connector_version}` : ''}
            </span>
          </FragmentRow>
        )}
        {typeof meta.latency_ms === 'number' && (
          <FragmentRow term="Latency">
            <span className="font-mono text-xs tabular-nums">{meta.latency_ms}ms</span>
          </FragmentRow>
        )}
        {typeof meta.http_status === 'number' && (
          <FragmentRow term="HTTP status">
            <span className="font-mono text-xs">{meta.http_status}</span>
          </FragmentRow>
        )}
        {meta.final_url && (
          <FragmentRow term="Final URL">
            <a
              href={meta.final_url}
              target="_blank"
              rel="noopener noreferrer"
              className="font-mono text-xs text-ink-muted hover:text-ink wrap-break-word"
            >
              {meta.final_url}
            </a>
          </FragmentRow>
        )}
        {typeof meta.robots_respected === 'boolean' && (
          <FragmentRow term="Robots">
            <span className="font-mono text-xs">
              {meta.robots_respected ? 'respected' : 'overridden'}
            </span>
          </FragmentRow>
        )}
        {meta.user_agent && (
          <FragmentRow term="User agent">
            <span className="font-mono text-[11px] text-ink-muted wrap-break-word">
              {meta.user_agent}
            </span>
          </FragmentRow>
        )}
      </dl>
    </section>
  );
}
