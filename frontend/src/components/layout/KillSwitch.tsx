import { Pause, Play } from 'lucide-react';
import { useMutation, useQueryClient } from '@tanstack/react-query';
import { api } from '@/lib/api';
import type { SystemStatus } from '@/lib/types';
import { cn } from '@/lib/utils';

/**
 * Global pause / resume button. Maps to the kill-switch flag file the
 * scheduler checks every tick — so flipping this stops new sends within
 * one scheduler tick without killing the process.
 */
export function KillSwitch({ status }: { status: SystemStatus }) {
  const qc = useQueryClient();
  const toggle = useMutation({
    mutationFn: () => (status.paused ? api.resume() : api.pause()),
    onSuccess: (data) => qc.setQueryData(['system-status'], data),
  });

  const paused = status.paused;
  return (
    <button
      onClick={() => toggle.mutate()}
      disabled={toggle.isPending}
      className={cn(
        'group relative flex items-center gap-2 px-3 h-9 border text-xs cursor-pointer transition-colors',
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
  );
}
