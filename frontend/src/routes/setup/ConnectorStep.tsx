import { ConnectorPicker } from '@/components/domain/ConnectorPicker';
import { Input } from '@/components/ui/Input';
import { Field } from '@/components/ui/Field';
import { StepCard } from './primitives';
import type { FormState, SetField } from './types';

const CONNECTOR_CHOICES = ['file', 'textbee', 'smsgate'] as const;

/**
 * Step 2 of the setup wizard: pick + configure an SMS connector.
 *
 * The per-connector field blocks intentionally mirror
 * :mod:`frontend/src/routes/settings/ConnectorCard.tsx` so an
 * operator who walks the wizard recognises the same form when they
 * later edit it in Settings. The "File" connector is shown first
 * because it's the zero-config rehearsal option.
 */
export function ConnectorStep({
  state,
  set,
}: {
  state: FormState;
  set: SetField;
}) {
  return (
    <StepCard
      title="Pick an SMS gateway"
      description="How do outbound messages actually leave the building? Start with File if you just want to see AutoSDR write drafts to disk without sending anything."
    >
      <div className="grid grid-cols-3 gap-3">
        {CONNECTOR_CHOICES.map((t) => (
          <ConnectorPicker
            key={t}
            type={t}
            active={state.connector_type === t}
            onClick={() => set('connector_type', t)}
          />
        ))}
      </div>

      {state.connector_type === 'file' && (
        <p className="mt-4 text-xs text-ink-muted">
          Drafts are appended to <code className="font-mono">data/outbox.jsonl</code>. Nothing goes
          over the wire. Good for rehearsal.
        </p>
      )}

      {state.connector_type === 'textbee' && (
        <div className="mt-4 space-y-4">
          <Field label="API URL">
            <Input
              value={state.textbee_api_url}
              onChange={(e) => set('textbee_api_url', e.target.value)}
            />
          </Field>
          <Field label="API key" required>
            <Input
              type="password"
              value={state.textbee_api_key}
              onChange={(e) => set('textbee_api_key', e.target.value)}
              autoComplete="off"
            />
          </Field>
          <Field label="Device ID" required>
            <Input
              value={state.textbee_device_id}
              onChange={(e) => set('textbee_device_id', e.target.value)}
            />
          </Field>
        </div>
      )}

      {state.connector_type === 'smsgate' && (
        <div className="mt-4 space-y-4">
          <Field
            label="API URL"
            required
            hint="Full base URL the API is mounted at. On-device (Android Local Server): the host:port the app shows, e.g. 192.168.0.13:8080 (the API is at the root — no path needed). Docker private server: http://localhost:3000/api/3rdparty/v1. Cloud: https://api.sms-gate.app/3rdparty/v1."
          >
            <Input
              value={state.smsgate_api_url}
              onChange={(e) => set('smsgate_api_url', e.target.value)}
              placeholder="192.168.0.13:8080"
            />
          </Field>
          <Field label="Username" required>
            <Input
              value={state.smsgate_username}
              onChange={(e) => set('smsgate_username', e.target.value)}
              autoComplete="off"
            />
          </Field>
          <Field label="Password" required>
            <Input
              type="password"
              value={state.smsgate_password}
              onChange={(e) => set('smsgate_password', e.target.value)}
              autoComplete="off"
            />
          </Field>
        </div>
      )}
    </StepCard>
  );
}
