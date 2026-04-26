import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useVirtualizer } from '@tanstack/react-virtual';
import { Link, useParams, useNavigate } from 'react-router-dom';
import { ChevronDown, Pause, Play, Plus, RotateCcw } from 'lucide-react';
import { type ReactNode, useRef, useState } from 'react';
import { api } from '@/lib/api';
import { BackLink } from '@/components/ui/BackLink';
import { QuotaMeter } from '@/components/domain/QuotaMeter';
import { ThreadStatusBadge } from '@/components/domain/ThreadStatusBadge';
import { Badge } from '@/components/ui/Badge';
import { Button } from '@/components/ui/Button';
import { Stat } from '@/components/ui/Stat';
import { Input, Textarea } from '@/components/ui/Input';
import { Field } from '@/components/ui/Field';
import { SaveRow, Toggle } from '@/routes/settings/primitives';
import { CAMPAIGN_STATUS_LABEL, relTime } from '@/lib/format';
import {
  CampaignStatus,
  type Campaign,
  type CampaignKickoffResult,
  type FollowupConfig,
} from '@/lib/types';

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
    enabled: !!id,
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
  const isCompleted = campaign.status === CampaignStatus.COMPLETED;
  const scheduleActionLabel = isActive
    ? 'Pause scheduled sends'
    : isDraft
      ? 'Start scheduled sends'
      : 'Resume scheduled sends';
  const settingsKey = [
    'settings',
    campaign.id,
    campaign.name,
    campaign.goal,
    campaign.outreach_per_day,
  ].join(':');
  const followupKey = [
    'followup',
    campaign.id,
    campaign.followup.enabled,
    campaign.followup.template,
    campaign.followup.delay_s,
    campaign.followup.delay_jitter_s,
  ].join(':');

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
            disabled={toggle.isPending || isCompleted}
          >
            {scheduleActionLabel}
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

      <ManualKickoffSection campaign={campaign} />

      <CampaignSettingsSection key={settingsKey} campaign={campaign} />

      <FollowupSection key={followupKey} campaign={campaign} />

      <ConversationsSection campaignId={id} />
    </div>
  );
}

const CONVERSATIONS_GRID =
  'grid grid-cols-[32%_24%_14%_15%_15%] items-start';
const CONVERSATIONS_ROW_HEIGHT = 60;

/**
 * Conversations live behind a collapsed card on purpose: pulling 500
 * threads is the heaviest read on this page. We only fire the
 * `listThreads` query once the operator opens the section, then
 * TanStack keeps it warm for the rest of the visit. Rows are
 * virtualized so even a busy campaign stays snappy.
 */
function ConversationsSection({ campaignId }: { campaignId: string }) {
  const [open, setOpen] = useState(false);
  const { data: threads, isFetching } = useQuery({
    queryKey: ['threads', 'campaign', campaignId],
    queryFn: () => api.listThreads({ campaignId, limit: 500 }),
    enabled: open,
  });

  const scrollRef = useRef<HTMLDivElement | null>(null);
  const rowVirtualizer = useVirtualizer({
    count: threads?.length ?? 0,
    getScrollElement: () => scrollRef.current,
    estimateSize: () => CONVERSATIONS_ROW_HEIGHT,
    overscan: 8,
  });

  const meta = !open
    ? 'open to load'
    : isFetching && !threads
      ? 'loading…'
      : `${threads?.length ?? 0} shown`;

  const isEmpty = open && !isFetching && (!threads || threads.length === 0);

  return (
    <CollapsibleCard
      title="Conversations"
      description="Every thread attached to this campaign."
      meta={meta}
      open={open}
      onOpenChange={setOpen}
    >
      <div className="paper-card">
        <div
          className={`${CONVERSATIONS_GRID} label px-3 py-2.5 border-b border-rule bg-paper-deep`}
        >
          <div>Lead</div>
          <div>Angle</div>
          <div>Messages</div>
          <div>Status</div>
          <div>Last activity</div>
        </div>
        {isEmpty ? (
          <div className="py-14 text-center text-ink-muted text-sm">
            No threads yet for this campaign.
          </div>
        ) : (
          <div
            ref={scrollRef}
            className="overflow-auto"
            style={{ maxHeight: 'min(60vh, 32rem)' }}
          >
            <div
              className="relative"
              style={{ height: rowVirtualizer.getTotalSize() }}
            >
              {rowVirtualizer.getVirtualItems().map((vRow) => {
                const t = threads![vRow.index];
                return (
                  <div
                    key={t.id}
                    data-index={vRow.index}
                    className={`${CONVERSATIONS_GRID} absolute inset-x-0 border-b border-rule px-3 py-3 hover:bg-paper-deep`}
                    style={{
                      transform: `translateY(${vRow.start}px)`,
                      height: CONVERSATIONS_ROW_HEIGHT,
                    }}
                  >
                    <div className="min-w-0 pr-3">
                      <Link to={`/threads/${t.id}`} className="group block">
                        <div className="text-sm font-medium text-ink group-hover:text-rust truncate">
                          {t.lead_name ?? 'Unknown'}
                        </div>
                        <div className="text-[11px] text-ink-faint font-mono mt-0.5 truncate">
                          {t.lead_category ?? '—'}
                        </div>
                      </Link>
                    </div>
                    <div className="text-xs text-ink-muted truncate pr-3">
                      {t.angle ?? '—'}
                    </div>
                    <div className="font-mono text-xs text-ink-muted">
                      {t.auto_reply_count ?? 0} replies
                    </div>
                    <div>
                      <ThreadStatusBadge status={t.status} />
                    </div>
                    <div className="font-mono text-[11px] text-ink-muted">
                      {relTime(t.last_message_at)}
                    </div>
                  </div>
                );
              })}
            </div>
          </div>
        )}
      </div>
    </CollapsibleCard>
  );
}

function ManualKickoffSection({ campaign }: { campaign: Campaign }) {
  const qc = useQueryClient();
  const [count, setCount] = useState(5);
  const [lastResult, setLastResult] = useState<CampaignKickoffResult | null>(null);
  const isCompleted = campaign.status === CampaignStatus.COMPLETED;

  const kickoff = useMutation({
    mutationFn: () => api.kickoffCampaign(campaign.id, clampInt(count, 1, 100, 1)),
    onSuccess: (data: CampaignKickoffResult) => {
      setLastResult(data);
      qc.setQueryData(['campaign', campaign.id], data.campaign);
      qc.invalidateQueries({ queryKey: ['campaigns'] });
      qc.invalidateQueries({ queryKey: ['status'] });
      qc.invalidateQueries({ queryKey: ['threads', 'campaign', campaign.id] });
    },
  });

  return (
    <CollapsibleCard
      title="Manual kick-off"
      description="Send the next queued leads now. This bypasses the 24-hour cap, but every send still counts afterward."
    >
      <div className="flex flex-col gap-3 md:flex-row md:items-end md:justify-between">
        <Field label="Send next N" hint="Pulls from this campaign's queue in order.">
          <Input
            type="number"
            min={1}
            max={100}
            value={String(count)}
            onChange={(e) => setCount(Number(e.target.value) || 1)}
            className="font-mono md:w-32"
          />
        </Field>
        <Button
          variant="primary"
          iconLeft={<Play className="h-3.5 w-3.5" strokeWidth={1.5} />}
          onClick={() => kickoff.mutate()}
          disabled={kickoff.isPending || isCompleted}
        >
          {kickoff.isPending ? 'Sending…' : 'Send now'}
        </Button>
      </div>

      {lastResult && (
        <div className="border border-rule bg-paper px-4 py-3 text-xs text-ink-muted">
          Sent {lastResult.sent} of {lastResult.requested}.{' '}
          {lastResult.failed > 0 ? `${lastResult.failed} failed. ` : ''}
          {lastResult.remaining_queued} queued lead
          {lastResult.remaining_queued === 1 ? '' : 's'} remain.
        </div>
      )}

      {kickoff.isError && (
        <div className="border border-rust/30 bg-rust/5 px-4 py-3 text-xs text-rust">
          Could not kick off this batch. Check the connector and try again.
        </div>
      )}
    </CollapsibleCard>
  );
}

function CampaignSettingsSection({ campaign }: { campaign: Campaign }) {
  const qc = useQueryClient();
  const [draft, setDraft] = useState(() => ({
    name: campaign.name,
    goal: campaign.goal,
    outreach_per_day: campaign.outreach_per_day,
  }));

  const save = useMutation({
    mutationFn: () =>
      api.patchCampaign(campaign.id, {
        name: draft.name.trim(),
        goal: draft.goal.trim(),
        outreach_per_day: clampInt(draft.outreach_per_day, 1, 5000, 50),
      }),
    onSuccess: (data: Campaign) => {
      qc.setQueryData(['campaign', campaign.id], data);
      qc.invalidateQueries({ queryKey: ['campaigns'] });
      qc.invalidateQueries({ queryKey: ['status'] });
    },
  });

  const resetSendCount = useMutation({
    mutationFn: () => api.resetCampaignSendCount(campaign.id),
    onSuccess: (data: Campaign) => {
      qc.setQueryData(['campaign', campaign.id], data);
      qc.invalidateQueries({ queryKey: ['campaigns'] });
      qc.invalidateQueries({ queryKey: ['status'] });
    },
  });

  const dirty =
    draft.name !== campaign.name ||
    draft.goal !== campaign.goal ||
    Number(draft.outreach_per_day) !== campaign.outreach_per_day;

  return (
    <CollapsibleCard
      title="Campaign settings"
      description="Edit the campaign brief and daily send capacity. Resetting the send count starts a fresh 24-hour quota window for this campaign."
      footer={<SaveRow dirty={dirty} pending={save.isPending} onSave={() => save.mutate()} />}
    >
      <div className="grid grid-cols-2 gap-4">
        <Field label="Campaign name">
          <Input
            value={draft.name}
            onChange={(e) => setDraft((d) => ({ ...d, name: e.target.value }))}
          />
        </Field>
        <Field label="Daily send capacity" hint="Rolling 24-hour limit for first-contact and follow-up sends.">
          <Input
            type="number"
            min={1}
            max={5000}
            value={String(draft.outreach_per_day)}
            onChange={(e) =>
              setDraft((d) => ({ ...d, outreach_per_day: Number(e.target.value) || 1 }))
            }
            className="font-mono"
          />
        </Field>
      </div>

      <Field label="Goal">
        <Textarea
          rows={3}
          value={draft.goal}
          onChange={(e) => setDraft((d) => ({ ...d, goal: e.target.value }))}
        />
      </Field>

      <div className="flex items-center justify-between gap-4 border border-rule bg-paper px-4 py-3">
        <div>
          <div className="text-sm font-medium text-ink">24-hour send count</div>
          <p className="text-xs text-ink-muted mt-1">
            Current count is {campaign.sent_24h}/{campaign.outreach_per_day}.
            {campaign.quota_reset_at ? ` Last reset ${relTime(campaign.quota_reset_at)}.` : ''}
          </p>
        </div>
        <Button
          variant="ghost"
          iconLeft={<RotateCcw className="h-3.5 w-3.5" strokeWidth={1.5} />}
          onClick={() => {
            if (window.confirm('Reset the 24-hour send count for this campaign?')) {
              resetSendCount.mutate();
            }
          }}
          disabled={resetSendCount.isPending || campaign.sent_24h === 0}
        >
          {resetSendCount.isPending ? 'Resetting…' : 'Reset send count'}
        </Button>
      </div>
    </CollapsibleCard>
  );
}

/**
 * Follow-up beat config. Lives per-campaign because different campaigns
 * tend to want different second-message voices (plumbers vs cafes sign
 * off differently). See ``autosdr/pipeline/followup.py`` for the send
 * semantics — this form is purely config, all the timing / guarding
 * logic lives server-side.
 */
function FollowupSection({ campaign }: { campaign: Campaign }) {
  const qc = useQueryClient();
  const [draft, setDraft] = useState<FollowupConfig>(() => ({ ...campaign.followup }));

  const save = useMutation({
    mutationFn: () =>
      api.patchCampaign(campaign.id, {
        followup: {
          enabled: draft.enabled,
          template: draft.template,
          delay_s: clampInt(draft.delay_s, 0, 600, 10),
          delay_jitter_s: clampInt(draft.delay_jitter_s, 0, 120, 5),
        },
      }),
    onSuccess: (data: Campaign) => {
      qc.setQueryData(['campaign', campaign.id], data);
      qc.invalidateQueries({ queryKey: ['campaigns'] });
    },
  });

  const dirty =
    draft.enabled !== campaign.followup.enabled ||
    draft.template !== campaign.followup.template ||
    Number(draft.delay_s) !== campaign.followup.delay_s ||
    Number(draft.delay_jitter_s) !== campaign.followup.delay_jitter_s;

  return (
    <CollapsibleCard
      title="Follow-up beat"
      description="Optional second message, fired a few seconds after the first outbound on every thread in this campaign. Literal template — no LLM. Skipped automatically if the lead replies first."
      footer={<SaveRow dirty={dirty} pending={save.isPending} onSave={() => save.mutate()} />}
    >
      <Toggle
        label="Send a follow-up after first contact"
        description={
          draft.enabled
            ? `On. Fires ~${draft.delay_s}s (± ${draft.delay_jitter_s}s) after the first message sends.`
            : 'Off. Leads receive the single first-contact message only.'
        }
        checked={draft.enabled}
        onToggle={() => setDraft((d) => ({ ...d, enabled: !d.enabled }))}
      />

      <Field
        label="Template"
        hint="Placeholders: {name}, {short_name}, {owner_first_name}. Unknown tokens render literally."
      >
        <Textarea
          rows={4}
          value={draft.template}
          onChange={(e) => setDraft((d) => ({ ...d, template: e.target.value }))}
          placeholder={
            "or more generally, if you have any issues with your website or need any help, I can solve that for you. Cheers, Jaclyn"
          }
        />
      </Field>

      <div className="grid grid-cols-2 gap-4">
        <Field label="Delay (seconds)" hint="Target gap between the two messages.">
          <Input
            type="number"
            min={0}
            max={600}
            value={String(draft.delay_s)}
            onChange={(e) =>
              setDraft((d) => ({ ...d, delay_s: Number(e.target.value) || 0 }))
            }
            className="font-mono"
          />
        </Field>
        <Field label="Jitter (± seconds)" hint="Randomness on top of the delay. Texture.">
          <Input
            type="number"
            min={0}
            max={120}
            value={String(draft.delay_jitter_s)}
            onChange={(e) =>
              setDraft((d) => ({ ...d, delay_jitter_s: Number(e.target.value) || 0 }))
            }
            className="font-mono"
          />
        </Field>
      </div>
    </CollapsibleCard>
  );
}

/**
 * Sections start closed by default — the operator opens the one they
 * want to look at. `open` / `onOpenChange` make the card controllable so
 * children can subscribe to the expansion state and lazy-fetch on first
 * open (see `ConversationsSection`).
 */
function CollapsibleCard({
  title,
  description,
  meta,
  children,
  footer,
  defaultOpen = false,
  open: controlledOpen,
  onOpenChange,
}: {
  title: string;
  description?: string;
  meta?: string;
  children: ReactNode;
  footer?: ReactNode;
  defaultOpen?: boolean;
  open?: boolean;
  onOpenChange?: (open: boolean) => void;
}) {
  const [uncontrolledOpen, setUncontrolledOpen] = useState(defaultOpen);
  const isControlled = controlledOpen !== undefined;
  const open = isControlled ? controlledOpen : uncontrolledOpen;

  const toggle = () => {
    const next = !open;
    if (!isControlled) setUncontrolledOpen(next);
    onOpenChange?.(next);
  };

  return (
    <section className="paper-card px-6 py-5">
      <button
        type="button"
        className="w-full flex items-start justify-between gap-4 text-left"
        aria-expanded={open}
        onClick={toggle}
      >
        <span>
          <span className="block text-base font-medium text-ink">{title}</span>
          {description && <span className="block mt-1 text-xs text-ink-muted">{description}</span>}
        </span>
        <span className="flex items-center gap-3 text-xs text-ink-faint font-mono">
          {meta}
          <ChevronDown
            aria-hidden="true"
            className={`h-4 w-4 transition-transform ${open ? 'rotate-180' : ''}`}
            strokeWidth={1.5}
          />
        </span>
      </button>
      {open && (
        <div className="mt-4 pt-4 border-t border-rule flex flex-col gap-5">{children}</div>
      )}
      {open && footer && <div className="mt-5 pt-4 border-t border-rule">{footer}</div>}
    </section>
  );
}

function clampInt(n: number, lo: number, hi: number, fallback: number): number {
  if (!Number.isFinite(n)) return fallback;
  return Math.max(lo, Math.min(hi, Math.trunc(n)));
}
