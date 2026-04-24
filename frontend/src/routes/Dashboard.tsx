import { useQuery } from '@tanstack/react-query';
import { Link } from 'react-router-dom';
import { ArrowRight } from 'lucide-react';
import { api } from '@/lib/api';
import { QuotaMeter } from '@/components/domain/QuotaMeter';
import { Badge } from '@/components/ui/Badge';
import { Stat } from '@/components/ui/Stat';
import { HITL_LABEL, relTime, shortDate } from '@/lib/format';
import { CampaignStatus, ThreadStatus, type SendsByDay } from '@/lib/types';

/**
 * Operator dashboard — the one screen an SDR checks first thing.
 *
 * Four panels, in order of urgency:
 *   1. Status strip     — paused?, active connector, LLM usage today
 *   2. HITL queue       — threads awaiting a human reply
 *   3. Sends sparkline  — AI-message volume over the last 14 days
 *   4. Campaign quotas  — 24h progress bars per active campaign
 *
 * The previous "editorial" layout (Fraunces hero, § markers, date strip)
 * has been removed. We still keep the warm paper palette because it's
 * easy on the eyes during long triage sessions.
 */
export function Dashboard() {
  const { data: status } = useQuery({
    queryKey: ['system-status'],
    queryFn: () => api.getSystemStatus(),
    refetchInterval: 10_000,
  });
  const { data: hitl } = useQuery({
    queryKey: ['threads', { status: ThreadStatus.PAUSED_FOR_HITL, limit: 10 }],
    queryFn: () => api.listThreads({ status: ThreadStatus.PAUSED_FOR_HITL, limit: 10 }),
  });
  const { data: campaigns } = useQuery({
    queryKey: ['campaigns'],
    queryFn: () => api.listCampaigns(),
  });
  const { data: sends14d } = useQuery({
    queryKey: ['sends-14d'],
    queryFn: () => api.getSends14d(),
  });

  const hitlCount = hitl?.length ?? 0;
  const activeCampaigns =
    campaigns?.filter((c) => c.status === CampaignStatus.ACTIVE) ?? [];
  const totalSent24h = activeCampaigns.reduce((a, c) => a + c.sent_24h, 0);

  return (
    <div className="page gap-8">
      <header className="flex items-baseline justify-between border-b border-rule pb-4">
        <div>
          <h1 className="text-2xl font-medium">Dashboard</h1>
          <p className="text-sm text-ink-muted mt-1">
            {status?.paused
              ? 'Scheduler is paused. Nothing is going out.'
              : `Running. ${totalSent24h} messages in the last 24 hours.`}
          </p>
        </div>
        <Link
          to="/inbox"
          className="inline-flex items-center gap-2 px-4 h-9 bg-ink text-paper text-sm hover:bg-rust transition-colors"
        >
          Open inbox
          {hitlCount > 0 && (
            <span className="px-1.5 bg-paper text-ink text-xs font-mono">{hitlCount}</span>
          )}
          <ArrowRight className="h-4 w-4" strokeWidth={1.5} />
        </Link>
      </header>

      <StatusStrip
        paused={status?.paused ?? false}
        connector={status?.active_connector ?? 'file'}
        dryRun={status?.dry_run ?? false}
        overrideTo={status?.override_to ?? null}
        autoReply={status?.auto_reply_enabled ?? false}
        callsToday={status?.llm_usage.calls_today ?? 0}
        tokensInToday={status?.llm_usage.tokens_in_today ?? 0}
        tokensOutToday={status?.llm_usage.tokens_out_today ?? 0}
      />

      <div className="grid grid-cols-12 gap-8">
        <section className="col-span-12 lg:col-span-7">
          <PanelHeader
            title={hitlCount > 0 ? 'Waiting for you' : 'Inbox is clean'}
            right={
              <Link to="/inbox" className="text-xs text-ink-muted hover:text-ink">
                All threads →
              </Link>
            }
          />
          {hitl && hitl.length > 0 ? (
            <ul className="paper-card divide-y divide-rule">
              {hitl.slice(0, 6).map((t) => (
                <li key={t.id} className="group">
                  <Link
                    to={`/threads/${t.id}`}
                    className="block px-5 py-4 hover:bg-paper-deep transition-colors"
                  >
                    <div className="flex items-baseline justify-between gap-3 mb-1.5">
                      <div className="flex items-baseline gap-2 min-w-0">
                        <span className="text-sm font-medium truncate">
                          {t.lead_name ?? 'Unknown lead'}
                        </span>
                        <span className="text-xs text-ink-muted truncate">
                          {t.campaign_name}
                        </span>
                      </div>
                      <Badge tone="rust" uppercase={false}>
                        {t.hitl_reason
                          ? (HITL_LABEL[t.hitl_reason] ?? 'Needs you')
                          : 'Needs you'}
                      </Badge>
                    </div>
                    <div className="flex items-baseline justify-between gap-3 text-xs text-ink-muted">
                      <span className="truncate">
                        {t.hitl_context?.incoming_message ?? 'Awaiting a reply.'}
                      </span>
                      <span className="shrink-0 font-mono">
                        {relTime(t.last_message_at)}
                      </span>
                    </div>
                  </Link>
                </li>
              ))}
            </ul>
          ) : (
            <div className="paper-card px-5 py-8 text-center text-sm text-ink-muted">
              No threads are waiting on a human. New replies will appear here.
            </div>
          )}
        </section>

        <section className="col-span-12 lg:col-span-5 flex flex-col gap-8">
          <div>
            <PanelHeader
              title="Sends — last 14 days"
              right={
                <span className="text-xs text-ink-muted font-mono">
                  {totalSends(sends14d)} total
                </span>
              }
            />
            <SparklineChart data={sends14d ?? []} />
          </div>

          <div>
            <PanelHeader
              title="Campaign quotas"
              right={
                <Link to="/campaigns" className="text-xs text-ink-muted hover:text-ink">
                  Manage →
                </Link>
              }
            />
            <div className="flex flex-col gap-4">
              {activeCampaigns.length === 0 && (
                <div className="paper-card px-4 py-3 text-sm text-ink-muted">
                  No active campaigns.{' '}
                  <Link to="/campaigns" className="underline">
                    Create one
                  </Link>
                  .
                </div>
              )}
              {activeCampaigns.map((c) => (
                <div key={c.id} className="flex flex-col gap-2">
                  <Link
                    to={`/campaigns/${c.id}`}
                    className="flex items-baseline justify-between gap-4 group"
                  >
                    <span className="text-sm text-ink group-hover:text-rust truncate">
                      {c.name}
                    </span>
                    <span className="font-mono text-[11px] text-ink-muted shrink-0 tabular-nums">
                      {c.sent_24h} / {c.outreach_per_day}
                    </span>
                  </Link>
                  <QuotaMeter sent={c.sent_24h} quota={c.outreach_per_day} label="SENT" />
                </div>
              ))}
            </div>
          </div>
        </section>
      </div>
    </div>
  );
}

function totalSends(data: SendsByDay[] | undefined): number {
  if (!data) return 0;
  return data.reduce((a, d) => a + d.count, 0);
}

function PanelHeader({
  title,
  right,
}: {
  title: string;
  right?: React.ReactNode;
}) {
  return (
    <div className="flex items-end justify-between mb-3 pb-2 border-b border-rule">
      <h2 className="text-sm font-medium">{title}</h2>
      {right}
    </div>
  );
}

function StatusStrip(props: {
  paused: boolean;
  connector: string;
  dryRun: boolean;
  overrideTo: string | null;
  autoReply: boolean;
  callsToday: number;
  tokensInToday: number;
  tokensOutToday: number;
}) {
  return (
    <div className="paper-card grid grid-cols-2 md:grid-cols-4 divide-x divide-y md:divide-y-0 divide-rule">
      <Stat
        label="Scheduler"
        value={props.paused ? 'Paused' : 'Running'}
        tone={props.paused ? 'rust' : 'forest'}
      />
      <Stat
        label="Connector"
        value={props.connector}
        capitalize
        hint={props.dryRun ? 'dry-run' : props.overrideTo ? `override → ${props.overrideTo}` : undefined}
      />
      <Stat
        label="Auto-reply"
        value={props.autoReply ? 'On' : 'Off'}
        tone={props.autoReply ? 'mustard' : 'neutral'}
        hint={props.autoReply ? undefined : 'first-message only'}
      />
      <Stat
        label="LLM today"
        value={`${props.callsToday} calls`}
        hint={`${props.tokensInToday}→${props.tokensOutToday} tokens`}
      />
    </div>
  );
}

function SparklineChart({ data }: { data: SendsByDay[] }) {
  if (!data.length) {
    return (
      <div className="paper-card px-4 py-6 text-sm text-ink-muted">No sends yet.</div>
    );
  }
  const w = 420;
  const h = 90;
  const padX = 4;
  const padY = 8;
  const max = Math.max(...data.map((d) => d.count), 10);
  const barW = (w - padX * 2) / data.length - 2;

  return (
    <div className="flex flex-col gap-2 paper-card px-4 py-4">
      <svg viewBox={`0 0 ${w} ${h}`} className="w-full h-auto" preserveAspectRatio="none">
        <line
          x1={padX}
          x2={w - padX}
          y1={h - padY}
          y2={h - padY}
          stroke="var(--color-rule-strong)"
          strokeWidth={0.5}
        />
        {data.map((d, i) => {
          const x = padX + i * ((w - padX * 2) / data.length) + 1;
          const sendsH = ((h - padY * 2) * d.count) / max;
          return (
            <rect
              key={d.date}
              x={x}
              y={h - padY - sendsH}
              width={barW}
              height={sendsH}
              fill="var(--color-ink)"
              opacity={i === data.length - 1 ? 1 : 0.7}
            />
          );
        })}
      </svg>
      <div className="flex items-center justify-between gap-4 text-[10px] font-mono text-ink-muted">
        <span>{shortDate(data[0]?.date)}</span>
        <span>{shortDate(data[data.length - 1]?.date)}</span>
      </div>
    </div>
  );
}
