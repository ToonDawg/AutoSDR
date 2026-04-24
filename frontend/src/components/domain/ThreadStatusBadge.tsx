import { Badge } from '@/components/ui/Badge';
import { THREAD_STATUS_LABEL } from '@/lib/format';
import type { ThreadStatusT } from '@/lib/types';

const TONE: Record<ThreadStatusT, Parameters<typeof Badge>[0]['tone']> = {
  active: 'forest',
  paused: 'mustard',
  paused_for_hitl: 'rust',
  won: 'ink',
  lost: 'oxblood',
  skipped: 'neutral',
};

export function ThreadStatusBadge({ status }: { status: ThreadStatusT }) {
  return (
    <Badge tone={TONE[status]} dot>
      {THREAD_STATUS_LABEL[status]}
    </Badge>
  );
}
