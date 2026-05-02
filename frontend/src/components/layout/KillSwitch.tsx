import { Pause, Play } from 'lucide-react';
import { useMutation, useQueryClient } from '@tanstack/react-query';
import { api } from '@/lib/api';
import type { SystemStatus } from '@/lib/types';
import { cn } from '@/lib/utils';

/**
 * Global pause / resume button. Maps to the kill-switch flag file the
 * scheduler checks every tick — so flipping this stops new sends within
 * one scheduler tick without killing the process.
 *
 * Sized to the WCAG / Apple HIG ≥ 44 px touch-target rule (h-11) so the
 * primary "stop AutoSDR" affordance stays reliably tappable on mobile.
 *
 * Ticket 0009: when the killswitch is on, inbound webhooks are queued
 * to ``paused_inbound`` instead of being silently dropped. This badge
 * surfaces the queue depth so the operator's "pause means pause, not
 * delete" mental model is honoured. The badge tooltip also flags
 * stale queues via ``oldest_pending_at``.
 */
export function KillSwitch({ status }: { status: SystemStatus }) {
  const qc = useQueryClient();
  const toggle = useMutation({
    mutationFn: () => (status.paused ? api.resume() : api.pause()),
    onSuccess: (data) => qc.setQueryData(['system-status'], data),
  });

  const paused = status.paused;
  const pendingCount = status.paused_inbound?.pending_count ?? 0;
  const oldestPendingAt = status.paused_inbound?.oldest_pending_at ?? null;

  return (
    <div className="flex items-center gap-2">
      {pendingCount > 0 && (
        <span
          role="status"
          aria-label={`${pendingCount} inbound message${
            pendingCount === 1 ? '' : 's'
          } waiting for resume`}
          className={cn(
            'inline-flex items-center gap-1.5 px-2 h-11 border text-[11px] uppercase tracking-wide',
            'border-mustard bg-mustard-soft text-mustard',
          )}
          title={
            oldestPendingAt
              ? `${pendingCount} inbound waiting since ${new Date(
                  oldestPendingAt,
                ).toLocaleString()} — click Resume to replay`
              : `${pendingCount} inbound waiting for resume`
          }
        >
          <span className="font-semibold">{pendingCount}</span>
          <span className="hidden sm:inline">
            inbound{pendingCount === 1 ? '' : 's'} waiting
          </span>
          <span className="sm:hidden">waiting</span>
        </span>
      )}
      <button
        onClick={() => toggle.mutate()}
        disabled={toggle.isPending}
        className={cn(
          'group relative flex items-center gap-2 px-3 h-11 min-w-[88px] border text-xs cursor-pointer transition-colors',
          paused
            ? 'border-mustard bg-mustard-soft text-mustard hover:bg-mustard hover:text-paper'
            : 'border-rule-strong bg-paper text-ink hover:border-oxblood hover:text-oxblood',
        )}
        title={paused ? 'Resume AutoSDR' : 'Pause AutoSDR'}
      >
        {paused ? (
          <Play className="h-3.5 w-3.5" strokeWidth={2} />
        ) : (
          <Pause className="h-3.5 w-3.5" strokeWidth={2} />
        )}
        <span>{paused ? 'Resume' : 'Pause'}</span>
      </button>
    </div>
  );
}
