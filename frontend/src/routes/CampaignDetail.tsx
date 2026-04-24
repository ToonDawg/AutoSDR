import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Link, useParams, useNavigate } from 'react-router-dom';
import { Pause, Play, Plus } from 'lucide-react';
import { api } from '@/lib/api';
import { BackLink } from '@/components/ui/BackLink';
import { QuotaMeter } from '@/components/domain/QuotaMeter';
import { ThreadStatusBadge } from '@/components/domain/ThreadStatusBadge';
import { Badge } from '@/components/ui/Badge';
import { Button } from '@/components/ui/Button';
import { Stat } from '@/components/ui/Stat';
import { CAMPAIGN_STATUS_LABEL, relTime } from '@/lib/format';
import { CampaignStatus, type Campaign } from '@/lib/types';

/**
 * Per-campaign detail. Header summarises + exposes the activate / pause
 * controls, a compact stat strip shows throughput, and the table below
 * is every thread that belongs to this campaign.
 */
export function CampaignDetail() {
  const { id = '' } = useParams();
  const navigate = useNavigate();
  const qc = useQueryClient();

  const { data: campaign } = useQuery({
    queryKey: ['campaign', id],
    queryFn: () => api.getCampaign(id),
  });
  const { data: threads } = useQuery({
    queryKey: ['threads', 'campaign', id],
    queryFn: () => api.listThreads({ campaignId: id, limit: 500 }),
  });

  const toggle = useMutation({
    mutationFn: () => {
      if (!campaign) throw new Error('no campaign');
      return campaign.status === CampaignStatus.ACTIVE
        ? api.pauseCampaign(id)
        : api.activateCampaign(id);
    },
    onSuccess: (data: Campaign) => {
      qc.setQueryData(['campaign', id], data);
      qc.invalidateQueries({ queryKey: ['campaigns'] });
    },
  });

  const assignAll = useMutation({
    mutationFn: () => api.assignLeads(id, { all_eligible: true }),
    onSuccess: (data: Campaign) => {
      qc.setQueryData(['campaign', id], data);
      qc.invalidateQueries({ queryKey: ['leads'] });
    },
  });

  if (!campaign) {
    return (
      <div className="px-8 py-8">
        <div className="h-10 bg-paper-deep animate-pulse w-2/3" />
      </div>
    );
  }

  const isActive = campaign.status === CampaignStatus.ACTIVE;
  const isPaused = campaign.status === CampaignStatus.PAUSED;
  const isDraft = campaign.status === CampaignStatus.DRAFT;

  return (
    <div className="page gap-6">
      <BackLink onClick={() => navigate(-1)}>Back to campaigns</BackLink>

      <header className="border-b border-rule pb-5 flex items-start justify-between gap-6">
        <div className="min-w-0">
          <div className="flex items-center gap-3 mb-2">
            <Badge
              tone={isActive ? 'forest' : isPaused ? 'mustard' : isDraft ? 'neutral' : 'ink'}
              dot
            >
              {CAMPAIGN_STATUS_LABEL[campaign.status]}
            </Badge>
            <span className="text-xs text-ink-faint">
              started {relTime(campaign.created_at)}
            </span>
          </div>
          <h1 className="text-2xl font-medium text-ink mb-2 truncate">{campaign.name}</h1>
          <p className="text-sm text-ink-muted max-w-prose">{campaign.goal}</p>
        </div>
        <div className="flex flex-col gap-2 shrink-0">
          <Button
            variant={isActive ? 'secondary' : 'primary'}
            iconLeft={
              isActive ? (
                <Pause className="h-3.5 w-3.5" strokeWidth={1.5} />
              ) : (
                <Play className="h-3.5 w-3.5" strokeWidth={1.5} />
              )
            }
            onClick={() => toggle.mutate()}
            disabled={toggle.isPending}
          >
            {isActive ? 'Pause campaign' : 'Activate'}
          </Button>
          <Button
            variant="ghost"
            iconLeft={<Plus className="h-3.5 w-3.5" strokeWidth={1.5} />}
            onClick={() => assignAll.mutate()}
            disabled={assignAll.isPending}
          >
            {assignAll.isPending ? 'Assigning…' : 'Assign all eligible leads'}
          </Button>
        </div>
      </header>

      <div className="grid grid-cols-5 border border-rule divide-x divide-rule">
        <Stat size="lg" label="Leads assigned" value={campaign.lead_count} />
        <Stat size="lg" label="Contacted" value={campaign.contacted_count} />
        <Stat size="lg" label="Replied" value={campaign.replied_count} />
        <Stat size="lg" label="Won" value={campaign.won_count} tone="forest" />
        <div className="p-4 flex flex-col gap-2">
          <div className="label">Daily capacity</div>
          <QuotaMeter sent={campaign.sent_24h} quota={campaign.outreach_per_day} label="24H" />
        </div>
      </div>

      <section className="flex flex-col gap-3">
        <div className="flex items-end justify-between pb-2 border-b border-rule">
          <h2 className="text-sm font-medium text-ink">Conversations</h2>
          <span className="text-xs text-ink-faint font-mono">
            {threads?.length ?? 0} threads
          </span>
        </div>

        <div className="paper-card">
          <table className="t-table">
            <thead>
              <tr>
                <th style={{ width: '32%' }}>Lead</th>
                <th style={{ width: '24%' }}>Angle</th>
                <th style={{ width: '14%' }}>Messages</th>
                <th style={{ width: '15%' }}>Status</th>
                <th style={{ width: '15%' }}>Last activity</th>
              </tr>
            </thead>
            <tbody>
              {(threads ?? []).map((t) => (
                <tr key={t.id}>
                  <td>
                    <Link to={`/threads/${t.id}`} className="group">
                      <div className="text-sm font-medium text-ink group-hover:text-rust">
                        {t.lead_name ?? 'Unknown'}
                      </div>
                      <div className="text-[11px] text-ink-faint font-mono mt-0.5">
                        {t.lead_category ?? '—'}
                      </div>
                    </Link>
                  </td>
                  <td className="text-xs text-ink-muted">{t.angle ?? '—'}</td>
                  <td className="font-mono text-xs text-ink-muted">
                    {t.auto_reply_count ?? 0} replies
                  </td>
                  <td>
                    <ThreadStatusBadge status={t.status} />
                  </td>
                  <td className="font-mono text-[11px] text-ink-muted">
                    {relTime(t.last_message_at)}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
          {(!threads || threads.length === 0) && (
            <div className="py-14 text-center text-ink-muted text-sm">
              No threads yet for this campaign.
            </div>
          )}
        </div>
      </section>
    </div>
  );
}
