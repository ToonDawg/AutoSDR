import { useState } from 'react';
import { useMutation } from '@tanstack/react-query';
import { AlertTriangle, CheckCircle2, Plug } from 'lucide-react';
import { api, ApiError } from '@/lib/api';
import { Input } from '@/components/ui/Input';
import { Button } from '@/components/ui/Button';
import { ConnectorPicker } from '@/components/domain/ConnectorPicker';
import { usePatchForm } from '@/lib/usePatchForm';
import type {
  ConnectorTestRequest,
  ConnectorTestResult,
  ConnectorType,
  Workspace,
  WorkspaceSettings,
} from '@/lib/types';
import { Field } from '@/components/ui/Field';
import { Card, SaveRow } from './primitives';

const DEFAULT_TEXTBEE_URL = 'https://api.textbee.dev';

export function ConnectorCard({ workspace }: { workspace: Workspace }) {
  const cfg = workspace.settings.connector;

  const form = usePatchForm({
    resetKey: workspace.updated_at,
    derive: () => ({
      type: cfg.type,
      textbee_api_url: cfg.textbee?.api_url ?? DEFAULT_TEXTBEE_URL,
      textbee_api_key: cfg.textbee?.api_key ?? '',
      textbee_device_id: cfg.textbee?.device_id ?? '',
      smsgate_api_url: cfg.smsgate?.api_url ?? '',
      smsgate_username: cfg.smsgate?.username ?? '',
      smsgate_password: cfg.smsgate?.password ?? '',
    }),
    save: (s) =>
      api.patchWorkspaceSettings({
        connector: {
          type: s.type,
          textbee: {
            api_url: s.textbee_api_url,
            api_key: s.textbee_api_key || null,
            device_id: s.textbee_device_id || null,
          },
          smsgate: {
            api_url: s.smsgate_api_url,
            username: s.smsgate_username || null,
            password: s.smsgate_password || null,
          },
        },
      } as Partial<WorkspaceSettings>),
  });

  // Connection probe — runs against the current form state (not whatever's
  // saved) so the operator can verify credentials before committing them.
  // Response is always 200; failures come back as ``{ok:false, detail:...}``.
  const [testResult, setTestResult] = useState<ConnectorTestResult | null>(null);
  const testConn = useMutation({
    mutationFn: (payload: ConnectorTestRequest) => api.testConnector(payload),
    onSuccess: (data) => setTestResult(data),
    onError: (err: unknown) => {
      const detail =
        err instanceof ApiError
          ? (err.payload as { error?: string } | null)?.error ?? err.message
          : err instanceof Error
          ? err.message
          : 'unknown error';
      setTestResult({ ok: false, detail, connector_type: form.state.type });
    },
  });

  const runTest = () => {
    setTestResult(null);
    const s = form.state;
    const payload: ConnectorTestRequest = { type: s.type };
    if (s.type === 'textbee') {
      payload.textbee = {
        api_url: s.textbee_api_url,
        api_key: s.textbee_api_key || null,
        device_id: s.textbee_device_id || null,
      };
    } else if (s.type === 'smsgate') {
      payload.smsgate = {
        api_url: s.smsgate_api_url,
        username: s.smsgate_username || null,
        password: s.smsgate_password || null,
      };
    }
    testConn.mutate(payload);
  };

  return (
    <Card
      title="Connector"
      description="How outbound SMS leaves and how inbound replies come back."
      footer={
        <div className="flex items-center justify-between gap-4">
          <Button
            variant="secondary"
            size="sm"
            iconLeft={<Plug className="h-3.5 w-3.5" strokeWidth={1.5} />}
            onClick={runTest}
            disabled={testConn.isPending}
          >
            {testConn.isPending ? 'Testing…' : 'Test connection'}
          </Button>
          <SaveRow dirty={form.dirty} pending={form.saving} onSave={form.save} />
        </div>
      }
    >
      <div className="grid grid-cols-3 gap-3">
        {(['file', 'textbee', 'smsgate'] as const).map((t: ConnectorType) => (
          <ConnectorPicker
            key={t}
            type={t}
            active={form.state.type === t}
            onClick={() => form.set('type', t)}
          />
        ))}
      </div>

      {form.state.type === 'textbee' && (
        <div className="mt-5 pt-5 border-t border-rule grid grid-cols-2 gap-4">
          <Field label="API URL">
            <Input
              value={form.state.textbee_api_url}
              onChange={(e) => form.set('textbee_api_url', e.target.value)}
              className="font-mono"
            />
          </Field>
          <Field label="Device ID">
            <Input
              value={form.state.textbee_device_id}
              onChange={(e) => form.set('textbee_device_id', e.target.value)}
              className="font-mono"
            />
          </Field>
          <Field label="API key" hint="Issued in TextBee dashboard.">
            <Input
              type="password"
              value={form.state.textbee_api_key}
              onChange={(e) => form.set('textbee_api_key', e.target.value)}
              autoComplete="off"
            />
          </Field>
        </div>
      )}

      {form.state.type === 'smsgate' && (
        <div className="mt-5 pt-5 border-t border-rule grid grid-cols-2 gap-4">
          <Field
            label="API URL"
            hint="Paste the full base URL the API is mounted at. On-device (Android Local Server): the host:port the app shows, e.g. 192.168.0.13:8080 (no path — it's at the root). Docker private server: http://localhost:3000/api/3rdparty/v1. Cloud: https://api.sms-gate.app/3rdparty/v1."
          >
            <Input
              value={form.state.smsgate_api_url}
              onChange={(e) => form.set('smsgate_api_url', e.target.value)}
              placeholder="192.168.0.13:8080"
              className="font-mono"
            />
          </Field>
          <Field label="Username">
            <Input
              value={form.state.smsgate_username}
              onChange={(e) => form.set('smsgate_username', e.target.value)}
              autoComplete="off"
            />
          </Field>
          <Field label="Password">
            <Input
              type="password"
              value={form.state.smsgate_password}
              onChange={(e) => form.set('smsgate_password', e.target.value)}
              autoComplete="off"
            />
          </Field>
        </div>
      )}

      {form.state.type === 'file' && (
        <p className="mt-4 text-xs text-ink-muted">
          Outbound drafts are appended to <code className="font-mono">data/outbox.jsonl</code>.
          Use <code className="font-mono">autosdr sim</code> to inject inbound replies.
        </p>
      )}

      {testResult && (
        <div
          role="status"
          className={
            testResult.ok
              ? 'mt-4 flex items-start gap-2 border border-forest/30 bg-forest-soft rounded px-3 py-2 text-sm text-forest'
              : 'mt-4 flex items-start gap-2 border border-oxblood/30 bg-oxblood-soft rounded px-3 py-2 text-sm text-oxblood'
          }
        >
          {testResult.ok ? (
            <CheckCircle2 className="h-4 w-4 mt-0.5 shrink-0" strokeWidth={1.75} />
          ) : (
            <AlertTriangle className="h-4 w-4 mt-0.5 shrink-0" strokeWidth={1.75} />
          )}
          <div className="min-w-0">
            <div className="font-medium">
              {testResult.ok ? 'Connection OK' : 'Connection failed'}
              <span className="ml-2 text-xs font-mono text-ink-muted">
                ({testResult.connector_type})
              </span>
            </div>
            <div className="text-xs mt-0.5 wrap-break-word">{testResult.detail}</div>
          </div>
        </div>
      )}
    </Card>
  );
}
