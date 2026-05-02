import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import { Link } from 'react-router-dom';
import {
  useCallback,
  useEffect,
  useId,
  useRef,
  useState,
  type MouseEvent,
} from 'react';
import { MoreHorizontal, Plus, Trash2 } from 'lucide-react';
import { api, ApiError } from '@/lib/api';
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
            Each campaign has its own goal, lead queue, and daily send quota. The send count
            resets at midnight each day.
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
  // ``CampaignOut`` exposes one count per ``CampaignLeadStatus`` bucket;
  // the row's headline numbers are rollups computed here. See ticket
  // 0003 — the API used to lie about ``contacted_count`` /
  // ``replied_count`` semantics, so the rollup math now lives at the
  // call site.
  const everContacted =
    campaign.contacted_count +
    campaign.replied_count +
    campaign.won_count +
    campaign.lost_count;
  const everReplied =
    campaign.replied_count + campaign.won_count + campaign.lost_count;
  const replyRate = everContacted > 0 ? (everReplied / everContacted) * 100 : 0;
  const conversion =
    everContacted > 0 ? (campaign.won_count / everContacted) * 100 : 0;

  const tones = {
    active: 'forest',
    paused: 'mustard',
    draft: 'neutral',
    completed: 'ink',
  } as const;

  return (
    <div className="relative">
      <Link
        to={`/campaigns/${campaign.id}`}
        className="grid grid-cols-12 gap-5 px-5 py-4 pr-12 border border-rule hover:border-ink bg-paper transition-colors"
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
          <Stat size="sm" label="Sent" value={everContacted} />
          <Stat
            size="sm"
            label="Replies"
            value={everReplied}
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
            sent={campaign.sent_today}
            quota={campaign.outreach_per_day}
            label="TODAY"
            compact
          />
        </div>
      </Link>
      <CampaignActionsMenu campaign={campaign} />
    </div>
  );
}

/**
 * Per-row "actions" affordance. Opens an inline menu that today only
 * has Delete, but the trigger is generic on purpose — Pause / Resume /
 * Duplicate will land here next without changing the row layout. The
 * trigger lives inside the row's relative wrapper (not the `<Link>`)
 * so that clicking it never navigates.
 */
function CampaignActionsMenu({ campaign }: { campaign: Campaign }) {
  const [open, setOpen] = useState(false);
  const [confirming, setConfirming] = useState(false);
  const wrapperRef = useRef<HTMLDivElement | null>(null);
  const menuId = useId();

  const close = useCallback(() => setOpen(false), []);

  useEffect(() => {
    if (!open) return;
    const onPointerDown = (e: PointerEvent) => {
      if (!wrapperRef.current) return;
      if (!wrapperRef.current.contains(e.target as Node)) close();
    };
    const onKeyDown = (e: KeyboardEvent) => {
      if (e.key === 'Escape') close();
    };
    document.addEventListener('pointerdown', onPointerDown);
    document.addEventListener('keydown', onKeyDown);
    return () => {
      document.removeEventListener('pointerdown', onPointerDown);
      document.removeEventListener('keydown', onKeyDown);
    };
  }, [open, close]);

  return (
    <div
      ref={wrapperRef}
      className="absolute top-2 right-2"
      onClick={(e) => e.stopPropagation()}
    >
      <button
        type="button"
        aria-label={`Actions for ${campaign.name}`}
        aria-haspopup="menu"
        aria-expanded={open}
        aria-controls={open ? menuId : undefined}
        className="flex h-8 w-8 items-center justify-center text-ink-muted hover:text-ink hover:bg-paper-deep border border-transparent hover:border-rule transition-colors cursor-pointer"
        onClick={(e) => {
          e.preventDefault();
          e.stopPropagation();
          setOpen((v) => !v);
        }}
      >
        <MoreHorizontal className="h-4 w-4" strokeWidth={1.75} />
      </button>
      {open && (
        <div
          id={menuId}
          role="menu"
          className="absolute right-0 top-full mt-1 z-20 min-w-[180px] paper-card shadow-md py-1"
        >
          <button
            type="button"
            role="menuitem"
            className="w-full flex items-center gap-2 px-3 py-2 text-sm text-oxblood hover:bg-oxblood-soft/40 cursor-pointer"
            onClick={(e) => {
              e.preventDefault();
              e.stopPropagation();
              setOpen(false);
              setConfirming(true);
            }}
          >
            <Trash2 className="h-3.5 w-3.5" strokeWidth={1.75} />
            Delete campaign
          </button>
        </div>
      )}
      {confirming && (
        <DeleteCampaignDialog
          campaign={campaign}
          onClose={() => setConfirming(false)}
        />
      )}
    </div>
  );
}

/**
 * Modal confirmation for hard-delete. The native ``window.prompt``
 * approach we used to use silently failed on whitespace mismatches and
 * was easy to miss inside the collapsed Danger Zone card on the detail
 * page, so the gate now lives in a real dialog with a live name match
 * indicator. Cascading semantics live in the API handler — see
 * ``autosdr/api/campaigns.py::delete_campaign``.
 */
function DeleteCampaignDialog({
  campaign,
  onClose,
}: {
  campaign: Campaign;
  onClose: () => void;
}) {
  const qc = useQueryClient();
  const [name, setName] = useState('');
  const totalThreads =
    campaign.contacted_count +
    campaign.replied_count +
    campaign.won_count +
    campaign.lost_count +
    campaign.paused_for_hitl_count +
    campaign.sending_count;

  const matched = name.trim() === campaign.name.trim();

  const deleteMutation = useMutation({
    mutationFn: () => api.deleteCampaign(campaign.id),
    onSuccess: () => {
      qc.removeQueries({ queryKey: ['campaign', campaign.id] });
      qc.removeQueries({ queryKey: ['threads', 'campaign', campaign.id] });
      qc.invalidateQueries({ queryKey: ['campaigns'] });
      qc.invalidateQueries({ queryKey: ['status'] });
      qc.invalidateQueries({ queryKey: ['threads'] });
      qc.invalidateQueries({ queryKey: ['leads'] });
      onClose();
    },
  });

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape' && !deleteMutation.isPending) onClose();
    };
    document.addEventListener('keydown', onKey);
    return () => document.removeEventListener('keydown', onKey);
  }, [onClose, deleteMutation.isPending]);

  const errorMessage =
    deleteMutation.error instanceof ApiError
      ? `Could not delete campaign (HTTP ${deleteMutation.error.status}).`
      : deleteMutation.error
        ? 'Could not delete campaign — try again.'
        : null;

  const stop = (e: MouseEvent) => e.stopPropagation();

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-ink/40 px-4"
      onClick={() => !deleteMutation.isPending && onClose()}
    >
      <div
        role="dialog"
        aria-modal="true"
        aria-labelledby={`delete-${campaign.id}-title`}
        className="paper-card w-full max-w-md p-5 flex flex-col gap-4"
        onClick={stop}
      >
        <div className="flex flex-col gap-1.5">
          <h2
            id={`delete-${campaign.id}-title`}
            className="text-base font-medium text-ink"
          >
            Delete &ldquo;{campaign.name}&rdquo;?
          </h2>
          <p className="text-xs text-ink-muted leading-relaxed">
            Removes {campaign.lead_count} lead assignment
            {campaign.lead_count === 1 ? '' : 's'}, {totalThreads} conversation
            {totalThreads === 1 ? '' : 's'}, every message in those threads,
            and the campaign&apos;s LLM-call history. Leads themselves stay in
            your workspace.
          </p>
        </div>

        <label className="flex flex-col gap-1.5">
          <span className="label">
            Type the campaign name to confirm
          </span>
          <Input
            autoFocus
            value={name}
            onChange={(e) => setName(e.target.value)}
            placeholder={campaign.name}
            disabled={deleteMutation.isPending}
          />
        </label>

        {errorMessage && (
          <div className="border border-oxblood/30 bg-oxblood-soft/40 px-3 py-2 text-xs text-oxblood">
            {errorMessage}
          </div>
        )}

        <div className="flex items-center justify-end gap-2">
          <Button
            variant="ghost"
            onClick={onClose}
            disabled={deleteMutation.isPending}
          >
            Cancel
          </Button>
          <Button
            variant="danger"
            iconLeft={<Trash2 className="h-3.5 w-3.5" strokeWidth={1.5} />}
            onClick={() => deleteMutation.mutate()}
            disabled={!matched || deleteMutation.isPending}
          >
            {deleteMutation.isPending ? 'Deleting…' : 'Delete campaign'}
          </Button>
        </div>
      </div>
    </div>
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
