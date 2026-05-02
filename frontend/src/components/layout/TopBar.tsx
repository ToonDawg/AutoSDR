import { useQuery } from '@tanstack/react-query';
import { Menu, Moon, RadioTower, Sun } from 'lucide-react';
import { api } from '@/lib/api';
import { cn } from '@/lib/utils';
import { useTheme } from '@/lib/useTheme';
import { KillSwitch } from './KillSwitch';

/**
 * Top status bar. Five things live here:
 *
 * - `<md` only: hamburger button that opens `MobileDrawer`.
 * - Running / Paused indicator (mirrors the kill-switch state).
 * - Active connector (plus an override chip when rehearsal redirect is on).
 * - Theme toggle.
 * - The kill-switch button itself.
 *
 * The bar wraps below the spacer below `md:` so a 320 px viewport keeps
 * the hamburger + Running/Paused + KillSwitch on one row and pushes the
 * connector / override chips to a second row. The KillSwitch is always
 * visible — losing it on mobile would defeat the "always reachable
 * pause" affordance.
 */
export function TopBar({ onOpenMenu }: { onOpenMenu: () => void }) {
  const { data: status } = useQuery({
    queryKey: ['system-status'],
    queryFn: () => api.getSystemStatus(),
    refetchInterval: 5000,
  });

  const { isDark, toggle: toggleTheme } = useTheme();

  return (
    <header className="sticky top-0 z-30 border-b border-rule bg-paper/85 backdrop-blur-md">
      <div className="flex items-center flex-wrap gap-x-3 gap-y-2 px-3 md:px-5 py-2 min-h-12">
        <button
          type="button"
          onClick={onOpenMenu}
          aria-label="Open navigation"
          className="md:hidden h-10 w-10 -ml-1 inline-flex items-center justify-center text-ink-muted hover:text-ink border border-transparent hover:border-rule"
        >
          <Menu className="h-4 w-4" strokeWidth={1.5} />
        </button>

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

        <span className="text-ink-faint hidden sm:inline">·</span>

        <div className="inline-flex items-center gap-1.5 text-xs text-ink-muted">
          <RadioTower className="h-3 w-3" strokeWidth={1.5} />
          <span className="capitalize">{status?.active_connector ?? 'file'}</span>
        </div>

        {status?.override_to && (
          <span className="inline-flex items-center gap-1.5 px-2 h-6 border border-teal bg-teal-soft text-teal text-[10px] uppercase tracking-wide">
            <span className="hidden sm:inline">Override → </span>
            {status.override_to}
          </span>
        )}
        {!status?.auto_reply_enabled && (
          <span className="hidden md:inline-flex items-center gap-1.5 px-2 h-6 border border-rule-strong text-ink-muted text-[10px] uppercase tracking-wide">
            First-message only
          </span>
        )}

        <div className="flex-1" />

        <button
          onClick={toggleTheme}
          className="h-10 w-10 border border-rule-strong inline-flex items-center justify-center text-ink-muted hover:text-ink hover:border-ink cursor-pointer transition-colors"
          title={isDark ? 'Switch to light' : 'Switch to dark'}
          aria-label={isDark ? 'Switch to light theme' : 'Switch to dark theme'}
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
