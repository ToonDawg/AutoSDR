import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import { Link } from 'react-router-dom';
import { useState } from 'react';
import { Plus } from 'lucide-react';
import { api } from '@/lib/api';
import { QuotaMeter } from '@/components/domain/QuotaMeter';
import { Badge } from '@/components/ui/Badge';
import { Button } from '@/components/ui/Button';
import { Input, Textarea } from '@/components/ui/Input';
import { Stat } from '@/components/ui/Stat';
import { CAMPAIGN_STATUS_LABEL, relTime } from '@/lib/format';
import type { Campaign } from '@/lib/types';

/**
 * Campaigns index. Each row links to the per-campaign detail; the
 * inline "New campaign" form drops into place because the feature has
 * no dedicated CLI anymore — everything campaign-related lives in the UI.
 */
export function Campaigns() {
  const qc = useQueryClient();
  const { data: campaigns } = useQuery({
    queryKey: ['campaigns'],
    queryFn: () => api.listCampaigns(),
  });
  const [creating, setCreating] = useState(false);

  return (
    <div className="page-narrow gap-5">
      <header className="border-b border-rule pb-4 flex items-end justify-between gap-4">
        <div>
          <h1 className="text-2xl font-medium">Campaigns</h1>
          <p className="text-sm text-ink-muted mt-1 max-w-prose">
            Each campaign has its own goal, lead queue, and daily send quota. Sends are capped
            per rolling 24 hours.
          </p>
        </div>
        <Button
          variant="primary"
          iconLeft={<Plus className="h-4 w-4" strokeWidth={1.5} />}
          onClick={() => setCreating((v) => !v)}
        >
          {creating ? 'Cancel' : 'New campaign'}
        </Button>
      </header>

      {creating && (
        <CreateCampaignForm
          onDone={() => {
            setCreating(false);
            qc.invalidateQueries({ queryKey: ['campaigns'] });
          }}
        />
      )}

      <div className="flex flex-col gap-3">
        {(campaigns ?? []).map((c) => (
          <CampaignRow key={c.id} campaign={c} />
        ))}
        {campaigns && campaigns.length === 0 && (
          <div className="paper-card px-6 py-10 text-center text-sm text-ink-muted">
            No campaigns yet. Create one to start outbound.
          </div>
        )}
      </div>
    </div>
  );
}

function CampaignRow({ campaign }: { campaign: Campaign }) {
  const replyRate =
    campaign.contacted_count > 0
      ? (campaign.replied_count / campaign.contacted_count) * 100
      : 0;
  const conversion =
    campaign.contacted_count > 0
      ? (campaign.won_count / campaign.contacted_count) * 100
      : 0;

  const tones = {
    active: 'forest',
    paused: 'mustard',
    draft: 'neutral',
    completed: 'ink',
  } as const;

  return (
    <Link
      to={`/campaigns/${campaign.id}`}
      className="grid grid-cols-12 gap-5 px-5 py-4 border border-rule hover:border-ink bg-paper transition-colors"
    >
      <div className="col-span-12 md:col-span-4 flex flex-col gap-1.5 min-w-0">
        <div className="flex items-center gap-2">
          <Badge tone={tones[campaign.status]} dot>
            {CAMPAIGN_STATUS_LABEL[campaign.status]}
          </Badge>
          <span className="text-xs text-ink-faint">
            started {relTime(campaign.created_at)}
          </span>
        </div>
        <h3 className="text-base font-medium text-ink truncate">{campaign.name}</h3>
        <p className="text-xs text-ink-muted line-clamp-2">{campaign.goal}</p>
      </div>

      <div className="col-span-12 md:col-span-5 grid grid-cols-4 gap-x-3 gap-y-1 items-end min-w-0">
        <Stat size="sm" label="Leads" value={campaign.lead_count} />
        <Stat size="sm" label="Sent" value={campaign.contacted_count} />
        <Stat
          size="sm"
          label="Replies"
          value={campaign.replied_count}
          hint={`${replyRate.toFixed(0)}%`}
        />
        <Stat
          size="sm"
          label="Won"
          value={campaign.won_count}
          tone="forest"
          hint={`${conversion.toFixed(1)}%`}
        />
      </div>

      <div className="col-span-12 md:col-span-3 flex flex-col justify-center min-w-0">
        <QuotaMeter
          sent={campaign.sent_24h}
          quota={campaign.outreach_per_day}
          label="24H SENT"
          compact
        />
      </div>
    </Link>
  );
}

function CreateCampaignForm({ onDone }: { onDone: () => void }) {
  const [name, setName] = useState('');
  const [goal, setGoal] = useState('');
  const [perDay, setPerDay] = useState('50');

  const create = useMutation({
    mutationFn: () =>
      api.createCampaign({
        name: name.trim(),
        goal: goal.trim(),
        outreach_per_day: Number(perDay) || 50,
      }),
    onSuccess: () => {
      setName('');
      setGoal('');
      setPerDay('50');
      onDone();
    },
  });

  const disabled = !name.trim() || !goal.trim() || create.isPending;

  return (
    <div className="paper-card px-5 py-4 flex flex-col gap-4">
      <div className="grid grid-cols-2 gap-4">
        <label className="flex flex-col gap-1.5">
          <span className="label">Name</span>
          <Input
            value={name}
            onChange={(e) => setName(e.target.value)}
            placeholder="Roofing — QLD suburbs"
          />
        </label>
        <label className="flex flex-col gap-1.5">
          <span className="label">Sends per day</span>
          <Input
            type="number"
            min={1}
            max={500}
            value={perDay}
            onChange={(e) => setPerDay(e.target.value)}
            className="font-mono"
          />
        </label>
      </div>
      <label className="flex flex-col gap-1.5">
        <span className="label">Goal</span>
        <Textarea
          value={goal}
          onChange={(e) => setGoal(e.target.value)}
          rows={2}
          placeholder="Book a free inspection."
        />
      </label>
      <div className="flex items-center justify-end gap-2">
        <Button variant="ghost" onClick={onDone} disabled={create.isPending}>
          Cancel
        </Button>
        <Button
          variant="primary"
          onClick={() => create.mutate()}
          disabled={disabled}
        >
          {create.isPending ? 'Creating…' : 'Create'}
        </Button>
      </div>
    </div>
  );
}
