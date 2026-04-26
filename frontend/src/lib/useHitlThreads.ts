import { useQuery } from '@tanstack/react-query';
import { api } from '@/lib/api';
import { ThreadStatus, type Thread } from '@/lib/types';

const POLL_MS = 10_000;

/**
 * Threads paused for HITL, optionally narrowed to active (undismissed) or
 * recently dismissed.
 *
 * Three call sites share this hook:
 *   - Inbox tabs: each tab passes its own ``dismissed`` flag and a
 *     growing ``limit`` for client-driven pagination.
 *   - Dashboard preview: ``{ dismissed: false, limit: 6 }`` for the
 *     "Waiting for you" panel.
 *   - (count-only callers use ``useHitlCount`` instead — fetching a list
 *     just to read ``.length`` was the original design's main scaling
 *     wart.)
 *
 * The ``dismissed`` flag is folded into the query key so each variant
 * gets its own cache slot. ``invalidateQueries(['threads', 'hitl'])``
 * still nukes them all in one go after a dismiss/restore mutation.
 */
export function useHitlThreads(
  opts: { dismissed?: boolean; limit?: number } = {},
) {
  const { dismissed, limit = 50 } = opts;
  return useQuery<Thread[]>({
    queryKey: ['threads', 'hitl', { dismissed: dismissed ?? null, limit }],
    queryFn: () =>
      api.listThreads({
        status: ThreadStatus.PAUSED_FOR_HITL,
        dismissed,
        limit,
      }),
    refetchInterval: POLL_MS,
  });
}

/**
 * Active/dismissed counters for the sidebar badge and the inbox tabs.
 *
 * Cheaper than ``useHitlThreads`` because the backend answers with two
 * integers instead of a full thread list — important for the sidebar's
 * 10-second polling.
 */
export function useHitlCount() {
  return useQuery({
    queryKey: ['threads', 'hitl', 'count'],
    queryFn: () => api.getHitlCount(),
    refetchInterval: POLL_MS,
  });
}
