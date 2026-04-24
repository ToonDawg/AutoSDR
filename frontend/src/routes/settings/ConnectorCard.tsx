import { api } from '@/lib/api';
import { Input } from '@/components/ui/Input';
import { ConnectorPicker } from '@/components/domain/ConnectorPicker';
import { usePatchForm } from '@/lib/usePatchForm';
import type { ConnectorType, Workspace, WorkspaceSettings } from '@/lib/types';
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

  return (
    <Card
      title="Connector"
      description="How outbound SMS leaves and how inbound replies come back."
      footer={<SaveRow dirty={form.dirty} pending={form.saving} onSave={form.save} />}
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
          <Field label="API URL">
            <Input
              value={form.state.smsgate_api_url}
              onChange={(e) => form.set('smsgate_api_url', e.target.value)}
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
    </Card>
  );
}
