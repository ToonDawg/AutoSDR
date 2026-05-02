import { useEffect, useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { api, ApiError } from '@/lib/api';
import { Button } from '@/components/ui/Button';
import { Field } from '@/components/ui/Field';
import { Input } from '@/components/ui/Input';
import { Badge } from '@/components/ui/Badge';
import type { PushSubscriptionRow, Workspace } from '@/lib/types';
import { Card, Toggle, SaveRow } from './primitives';
import { usePatchForm } from '@/lib/usePatchForm';

/**
 * Settings → Notifications (ticket 0005).
 *
 * The ticket's success criterion is "operator can toggle notifications
 * off / on without leaving the app." This card is the only place that
 * surfaces SW state to the operator, so it has to do a lot in a small
 * footprint:
 *
 *   1. Show whether push is even available (VAPID generated, browser
 *      supports SW + Notifications, served over a secure context).
 *   2. Let the operator subscribe / unsubscribe *this* device.
 *   3. Show every device that's currently subscribed (so a phone can
 *      see "yes, I am receiving HITL pushes here").
 *   4. Toggle HITL escalation on/off (workspace-wide).
 *   5. Allow setting a `dashboard_origin` override for the SMSGate-cloud
 *      fallback path documented in § *Remote-access architecture*.
 *   6. Fire a test notification to confirm the loop end-to-end.
 *
 * The actual subscription dance lives in `subscribeOnThisDevice` /
 * `unsubscribeOnThisDevice`. Errors are captured into `lastError` so
 * the operator never has to open DevTools to see why the prompt was
 * dismissed.
 */

const PUSH_QUERY_KEY = ['push', 'subscriptions'] as const;
const VAPID_QUERY_KEY = ['push', 'vapid-public'] as const;

type DeviceState =
  | { status: 'unsupported'; reason: string }
  | { status: 'idle' }
  | { status: 'subscribed'; endpoint: string }
  | { status: 'pending' }
  | { status: 'error'; error: string };

function urlBase64ToUint8Array(base64String: string): Uint8Array<ArrayBuffer> {
  const padding = '='.repeat((4 - (base64String.length % 4)) % 4);
  const base64 = (base64String + padding).replace(/-/g, '+').replace(/_/g, '/');
  const raw = window.atob(base64);
  const buffer = new ArrayBuffer(raw.length);
  const view = new Uint8Array(buffer);
  for (let i = 0; i < raw.length; i += 1) view[i] = raw.charCodeAt(i);
  return view;
}

function arrayBufferToBase64(buffer: ArrayBuffer | null): string {
  if (!buffer) return '';
  const bytes = new Uint8Array(buffer);
  let binary = '';
  for (let i = 0; i < bytes.byteLength; i += 1) binary += String.fromCharCode(bytes[i]);
  return window.btoa(binary);
}

async function getActiveRegistration(): Promise<ServiceWorkerRegistration | null> {
  if (!('serviceWorker' in navigator)) return null;
  return navigator.serviceWorker.ready.catch(() => null);
}

async function readDeviceSubscription(): Promise<PushSubscription | null> {
  const reg = await getActiveRegistration();
  if (!reg) return null;
  return reg.pushManager.getSubscription();
}

export function NotificationsCard({ workspace }: { workspace: Workspace }) {
  const qc = useQueryClient();
  const push = workspace.settings.push ?? {};
  const [device, setDevice] = useState<DeviceState>({ status: 'idle' });
  const [testFeedback, setTestFeedback] = useState<string | null>(null);

  const { data: vapid } = useQuery({
    queryKey: VAPID_QUERY_KEY,
    queryFn: () => api.getPushVapidPublic(),
  });
  const { data: subs, refetch: refetchSubs } = useQuery({
    queryKey: PUSH_QUERY_KEY,
    queryFn: () => api.listPushSubscriptions(),
  });

  useEffect(() => {
    let cancelled = false;
    async function detect() {
      if (typeof window === 'undefined') return;
      if (!('serviceWorker' in navigator)) {
        setDevice({ status: 'unsupported', reason: 'service worker unavailable' });
        return;
      }
      if (!('PushManager' in window)) {
        setDevice({ status: 'unsupported', reason: 'PushManager not in this browser' });
        return;
      }
      if (!window.isSecureContext) {
        setDevice({
          status: 'unsupported',
          reason: 'push requires HTTPS or localhost',
        });
        return;
      }
      const sub = await readDeviceSubscription();
      if (cancelled) return;
      setDevice(
        sub ? { status: 'subscribed', endpoint: sub.endpoint } : { status: 'idle' },
      );
    }
    void detect();
    return () => {
      cancelled = true;
    };
  }, []);

  const subscribeMutation = useMutation({
    mutationFn: async () => {
      if (!vapid?.public_key) {
        throw new Error('Push is not configured yet — VAPID key missing.');
      }
      const reg = await getActiveRegistration();
      if (!reg) throw new Error('Service worker is not active. Try a hard reload.');
      const permission = await Notification.requestPermission();
      if (permission !== 'granted') {
        throw new Error(`Notification permission ${permission}.`);
      }
      const sub = await reg.pushManager.subscribe({
        userVisibleOnly: true,
        applicationServerKey: urlBase64ToUint8Array(vapid.public_key),
      });
      const json = sub.toJSON();
      if (!json.endpoint || !json.keys?.p256dh || !json.keys?.auth) {
        throw new Error('Browser returned an incomplete subscription.');
      }
      const p256 = arrayBufferToBase64(sub.getKey('p256dh'));
      const auth = arrayBufferToBase64(sub.getKey('auth'));
      await api.subscribePush({
        endpoint: json.endpoint,
        keys: { p256dh: json.keys.p256dh ?? p256, auth: json.keys.auth ?? auth },
        user_agent: navigator.userAgent,
      });
      return sub.endpoint;
    },
    onSuccess: (endpoint) => {
      setDevice({ status: 'subscribed', endpoint });
      void refetchSubs();
    },
    onError: (err: unknown) => {
      const message =
        err instanceof ApiError
          ? `${err.status}: ${err.message}`
          : err instanceof Error
            ? err.message
            : 'unexpected failure';
      setDevice({ status: 'error', error: message });
    },
  });

  const unsubscribeMutation = useMutation({
    mutationFn: async () => {
      const sub = await readDeviceSubscription();
      const endpoint = sub?.endpoint;
      if (sub) await sub.unsubscribe();
      if (endpoint) await api.unsubscribePush(endpoint);
    },
    onSuccess: () => {
      setDevice({ status: 'idle' });
      void refetchSubs();
    },
    onError: (err: unknown) => {
      const message =
        err instanceof Error ? err.message : 'unexpected failure';
      setDevice({ status: 'error', error: message });
    },
  });

  const testMutation = useMutation({
    mutationFn: () => api.testPushNotification(),
    onSuccess: (result) => {
      setTestFeedback(
        `sent ${result.sent} · gone ${result.gone} · failed ${result.failed}`,
      );
      void refetchSubs();
    },
    onError: (err: unknown) => {
      setTestFeedback(
        err instanceof ApiError
          ? `error ${err.status}: ${err.message}`
          : err instanceof Error
            ? `error: ${err.message}`
            : 'error: unexpected failure',
      );
    },
  });

  const settingsForm = usePatchForm({
    resetKey: workspace.updated_at,
    derive: () => ({
      hitl_escalations: push.hitl_escalations !== false,
      dashboard_origin: push.dashboard_origin ?? '',
    }),
    save: async (s) => {
      const updated = await api.patchWorkspaceSettings({
        push: {
          hitl_escalations: s.hitl_escalations,
          dashboard_origin: s.dashboard_origin.trim() || null,
        },
      });
      await Promise.all([
        qc.invalidateQueries({ queryKey: PUSH_QUERY_KEY }),
        qc.invalidateQueries({ queryKey: VAPID_QUERY_KEY }),
      ]);
      return updated;
    },
  });

  const vapidReady = Boolean(vapid?.public_key);
  const supportNote = supportLine(device);

  return (
    <Card
      title="Notifications (Web Push)"
      description="Pushes a notification when a thread escalates to HITL. Privacy: payloads carry only the lead's first name + thread id; no message content leaves your network. Single-operator install — subscriptions live on your workspace."
      footer={
        <SaveRow
          dirty={settingsForm.dirty}
          pending={settingsForm.saving}
          onSave={settingsForm.save}
        />
      }
    >
      <Toggle
        label="HITL escalations"
        description="Send a push when the pipeline pauses a thread for human review."
        checked={settingsForm.state.hitl_escalations}
        onToggle={() =>
          settingsForm.set('hitl_escalations', !settingsForm.state.hitl_escalations)
        }
      />

      <Field
        label="Dashboard origin (optional)"
        hint="The URL the notification deep-link should open. Leave blank to use this device's URL — only set it if you serve the dashboard on a different host (e.g. SMSGate-cloud + tailnet hostname)."
      >
        <Input
          value={settingsForm.state.dashboard_origin}
          onChange={(e) => settingsForm.set('dashboard_origin', e.target.value)}
          placeholder={vapid?.dashboard_origin ?? 'http://autosdr-pc.tail-scale.ts.net:8000'}
          className="font-mono max-w-md"
        />
      </Field>

      <div className="flex flex-col gap-2 border-t border-rule pt-4">
        <div className="text-sm font-medium">This device</div>
        <p className="text-xs text-ink-muted">
          {vapidReady
            ? supportNote
            : 'Push is not yet configured — VAPID keys generate on first server boot.'}
        </p>
        <div className="flex flex-wrap gap-2 items-center">
          {device.status === 'subscribed' ? (
            <>
              <Badge tone="teal" uppercase={false}>
                Subscribed
              </Badge>
              <Button
                variant="secondary"
                size="sm"
                onClick={() => unsubscribeMutation.mutate()}
                disabled={unsubscribeMutation.isPending}
              >
                {unsubscribeMutation.isPending ? 'Unsubscribing…' : 'Unsubscribe this device'}
              </Button>
            </>
          ) : (
            <Button
              variant="primary"
              size="sm"
              onClick={() => subscribeMutation.mutate()}
              disabled={
                !vapidReady ||
                device.status === 'unsupported' ||
                subscribeMutation.isPending
              }
            >
              {subscribeMutation.isPending ? 'Requesting…' : 'Enable on this device'}
            </Button>
          )}
          <Button
            variant="ghost"
            size="sm"
            onClick={() => testMutation.mutate()}
            disabled={!subs?.subscriptions.length || testMutation.isPending}
          >
            {testMutation.isPending ? 'Firing…' : 'Send test notification'}
          </Button>
          {testFeedback && (
            <span className="text-xs text-ink-muted font-mono">{testFeedback}</span>
          )}
        </div>
        {device.status === 'error' && (
          <p className="text-xs text-mustard font-mono">{device.error}</p>
        )}
      </div>

      <SubscriptionList
        rows={subs?.subscriptions ?? []}
        meEndpoint={
          device.status === 'subscribed' ? device.endpoint : null
        }
      />
    </Card>
  );
}

function supportLine(device: DeviceState): string {
  switch (device.status) {
    case 'unsupported':
      return `Push is not available here: ${device.reason}.`;
    case 'subscribed':
      return 'This device is subscribed.';
    case 'pending':
      return 'Talking to the browser…';
    case 'error':
      return `Last error: ${device.error}`;
    case 'idle':
    default:
      return 'Browser supports push, but this device is not subscribed yet.';
  }
}

function SubscriptionList({
  rows,
  meEndpoint,
}: {
  rows: PushSubscriptionRow[];
  meEndpoint: string | null;
}) {
  if (!rows.length) {
    return (
      <div className="border-t border-rule pt-4">
        <div className="text-sm font-medium mb-2">Devices</div>
        <p className="text-xs text-ink-muted">No subscribed devices yet.</p>
      </div>
    );
  }
  return (
    <div className="border-t border-rule pt-4">
      <div className="text-sm font-medium mb-2">Devices</div>
      <ul className="flex flex-col gap-3">
        {rows.map((row) => {
          const isMe = meEndpoint && meEndpoint.includes(row.endpoint_host);
          return (
            <li key={row.id} className="flex flex-col sm:flex-row sm:items-center gap-2">
              <div className="flex-1 min-w-0">
                <div className="text-sm">
                  {row.user_agent ?? '(unknown user agent)'}{' '}
                  {isMe && (
                    <Badge tone="teal" uppercase={false}>
                      this device
                    </Badge>
                  )}
                </div>
                <div className="text-xs text-ink-muted font-mono truncate">
                  {row.endpoint_host}
                </div>
              </div>
              <div className="flex flex-col items-end gap-1 text-xs text-ink-muted">
                <span className="font-mono">
                  last seen {new Date(row.last_seen_at ?? row.created_at).toLocaleString()}
                </span>
                {row.last_error && (
                  <span className="font-mono text-mustard max-w-[40ch] truncate">
                    {row.last_error}
                  </span>
                )}
              </div>
            </li>
          );
        })}
      </ul>
    </div>
  );
}
