import { useQuery } from '@tanstack/react-query';
import { Moon, RadioTower, Sun } from 'lucide-react';
import { api } from '@/lib/api';
import { cn } from '@/lib/utils';
import { useTheme } from '@/lib/useTheme';
import { KillSwitch } from './KillSwitch';

/**
 * Top status bar. Four things live here:
 *
 * - Running / Paused indicator (mirrors the kill-switch state).
 * - Active connector (plus an override chip when rehearsal redirect is on).
 * - Theme toggle.
 * - The kill-switch button itself.
 *
 * "Dry-run" isn't its own chip any more — it was redundant with picking the
 * file connector, which already visibly renders as ``file`` on this bar.
 */
export function TopBar() {
  const { data: status } = useQuery({
    queryKey: ['system-status'],
    queryFn: () => api.getSystemStatus(),
    refetchInterval: 5000,
  });

  const { isDark, toggle: toggleTheme } = useTheme();

  return (
    <header className="sticky top-0 z-30 border-b border-rule bg-paper/85 backdrop-blur-md">
      <div className="flex items-center gap-4 px-5 h-12">
        <div className="flex items-center gap-2 text-xs">
          <span
            className={cn(
              'h-1.5 w-1.5 rounded-full',
              status?.paused ? 'bg-mustard' : 'bg-forest dot-pulse',
            )}
          />
          <span className="text-ink font-medium">
            {status?.paused ? 'Paused' : 'Running'}
          </span>
        </div>

        <span className="text-ink-faint">·</span>

        <div className="inline-flex items-center gap-1.5 text-xs text-ink-muted">
          <RadioTower className="h-3 w-3" strokeWidth={1.5} />
          <span className="capitalize">{status?.active_connector ?? 'file'}</span>
        </div>

        {status?.override_to && (
          <span className="inline-flex items-center gap-1.5 px-2 h-6 border border-teal bg-teal-soft text-teal text-[10px] uppercase tracking-wide">
            Override → {status.override_to}
          </span>
        )}
        {!status?.auto_reply_enabled && (
          <span className="inline-flex items-center gap-1.5 px-2 h-6 border border-rule-strong text-ink-muted text-[10px] uppercase tracking-wide">
            First-message only
          </span>
        )}

        <div className="flex-1" />

        <button
          onClick={toggleTheme}
          className="h-8 w-8 border border-rule-strong inline-flex items-center justify-center text-ink-muted hover:text-ink hover:border-ink cursor-pointer transition-colors"
          title={isDark ? 'Switch to light' : 'Switch to dark'}
        >
          {isDark ? (
            <Sun className="h-3.5 w-3.5" strokeWidth={1.5} />
          ) : (
            <Moon className="h-3.5 w-3.5" strokeWidth={1.5} />
          )}
        </button>

        {status && <KillSwitch status={status} />}
      </div>
    </header>
  );
}
