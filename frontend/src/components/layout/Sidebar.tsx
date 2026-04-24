import { NavLink } from 'react-router-dom';
import {
  BarChart3,
  Home,
  Inbox,
  MessageSquare,
  Settings,
  Sparkles,
  Users,
} from 'lucide-react';
import { useQuery } from '@tanstack/react-query';
import type { LucideIcon } from 'lucide-react';
import { cn } from '@/lib/utils';
import { api } from '@/lib/api';
import { ThreadStatus } from '@/lib/types';

/**
 * Primary navigation. Flat list, no section markers. The "needs your eye"
 * count re-queries every 10s so the badge stays roughly live.
 */

interface NavItem {
  label: string;
  to: string;
  icon: LucideIcon;
  badge?: 'hitl';
}

const NAV: NavItem[] = [
  { label: 'Dashboard', to: '/', icon: Home },
  { label: 'Needs your eye', to: '/inbox', icon: Inbox, badge: 'hitl' },
  { label: 'Threads', to: '/threads', icon: MessageSquare },
  { label: 'Campaigns', to: '/campaigns', icon: Sparkles },
  { label: 'Leads', to: '/leads', icon: Users },
  { label: 'LLM calls', to: '/logs', icon: BarChart3 },
  { label: 'Settings', to: '/settings', icon: Settings },
];

export function Sidebar() {
  const { data: threads } = useQuery({
    queryKey: ['threads', { status: ThreadStatus.PAUSED_FOR_HITL }],
    queryFn: () => api.listThreads({ status: ThreadStatus.PAUSED_FOR_HITL }),
    refetchInterval: 10_000,
  });
  const { data: workspace } = useQuery({
    queryKey: ['workspace'],
    queryFn: () => api.getWorkspace(),
  });

  const hitlCount = threads?.length ?? 0;

  return (
    <aside className="flex flex-col gap-6 h-full w-56 shrink-0 border-r border-rule bg-paper px-4 py-5">
      <div className="flex items-center gap-2">
        <div className="h-7 w-7 bg-ink text-paper flex items-center justify-center font-mono text-xs font-semibold">
          AS
        </div>
        <div className="flex flex-col leading-tight min-w-0">
          <span className="text-sm font-medium truncate">
            {workspace?.business_name ?? 'AutoSDR'}
          </span>
          <span className="text-[10px] text-ink-muted uppercase tracking-wide">
            Operator console
          </span>
        </div>
      </div>

      <nav className="flex flex-col gap-0.5 flex-1">
        {NAV.map((item) => (
          <NavLink
            key={item.to}
            to={item.to}
            end={item.to === '/'}
            className={({ isActive }) =>
              cn(
                'group flex items-center gap-2.5 px-2.5 py-2 border border-transparent text-sm transition-colors',
                isActive
                  ? 'bg-ink text-paper border-ink'
                  : 'text-ink-muted hover:text-ink hover:bg-paper-deep',
              )
            }
          >
            <item.icon className="h-3.5 w-3.5" strokeWidth={1.5} />
            <span className="flex-1">{item.label}</span>
            {item.badge === 'hitl' && hitlCount > 0 && (
              <span className="px-1.5 h-5 inline-flex items-center justify-center font-mono text-[10px] bg-rust text-paper">
                {hitlCount}
              </span>
            )}
          </NavLink>
        ))}
      </nav>

      <div className="text-[10px] text-ink-faint font-mono">AutoSDR · v0.4</div>
    </aside>
  );
}
