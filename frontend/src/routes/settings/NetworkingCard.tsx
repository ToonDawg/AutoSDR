import { useQuery } from '@tanstack/react-query';
import { api } from '@/lib/api';
import { Badge } from '@/components/ui/Badge';
import { Card } from './primitives';

/**
 * Settings → Networking (ticket 0005 unit 8).
 *
 * Read-only at v0; only surfaces what the operator needs to spot the
 * *PC-bind-interface footgun* before the phone can't reach the
 * dashboard. Writes (HOST/.env edits) deliberately stay outside the
 * UI: those changes live in the operator's `.env` and require a
 * server restart, which the README walks through.
 */
export function NetworkingCard() {
  const { data, isLoading, error } = useQuery({
    queryKey: ['networking-status'],
    queryFn: () => api.getNetworkingStatus(),
    refetchInterval: 30_000,
    staleTime: 30_000,
  });

  return (
    <Card
      title="Networking"
      description="How the phone reaches AutoSDR. The README's “Remote access” section walks through Tailscale + the SMSGate cloud fallback."
    >
      {isLoading ? (
        <p className="text-xs text-ink-muted">Probing…</p>
      ) : error || !data ? (
        <p className="text-xs text-mustard">Networking probe failed.</p>
      ) : (
        <NetworkingDetails data={data} />
      )}
    </Card>
  );
}

function NetworkingDetails({
  data,
}: {
  data: NonNullable<Awaited<ReturnType<typeof api.getNetworkingStatus>>>;
}) {
  const tailscaleTone =
    data.tailscale.state === 'running'
      ? 'teal'
      : data.tailscale.state === 'not_running'
        ? 'mustard'
        : 'neutral';
  const bindTone = data.bound_for_remote_access ? 'teal' : 'mustard';

  return (
    <div className="grid grid-cols-1 sm:grid-cols-[max-content_1fr] gap-x-4 gap-y-3 text-sm">
      <Label>HOST bind</Label>
      <Value>
        <code className="font-mono">
          {data.host}:{data.port}
        </code>{' '}
        <Badge tone={bindTone} uppercase={false}>
          {data.bound_for_remote_access ? 'remote-reachable' : 'localhost only'}
        </Badge>
      </Value>

      <Label>Tailscale</Label>
      <Value>
        <Badge tone={tailscaleTone} uppercase={false}>
          {data.tailscale.state}
        </Badge>{' '}
        {data.tailscale.detail && (
          <span className="font-mono text-xs text-ink-muted">
            {data.tailscale.detail}
          </span>
        )}
      </Value>

      <Label>Push deep-link origin</Label>
      <Value>
        <code className="font-mono">
          {data.dashboard_origin_resolved ?? '(unknown)'}
        </code>
        {data.dashboard_origin_override ? (
          <span className="text-xs text-ink-muted ml-2">
            (override)
          </span>
        ) : data.request_origin ? (
          <span className="text-xs text-ink-muted ml-2">
            (this device)
          </span>
        ) : null}
      </Value>

      {data.warning && (
        <>
          <Label>Warning</Label>
          <Value>
            <p className="text-xs text-mustard leading-relaxed">{data.warning}</p>
          </Value>
        </>
      )}
    </div>
  );
}

function Label({ children }: { children: React.ReactNode }) {
  return <div className="text-xs uppercase tracking-wide text-ink-muted">{children}</div>;
}

function Value({ children }: { children: React.ReactNode }) {
  return <div className="text-sm flex items-center flex-wrap gap-2">{children}</div>;
}
