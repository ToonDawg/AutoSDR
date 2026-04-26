import { api } from '@/lib/api';
import { Badge } from '@/components/ui/Badge';
import { Input } from '@/components/ui/Input';
import { usePatchForm } from '@/lib/usePatchForm';
import type { Workspace, WorkspaceSettings } from '@/lib/types';
import { Field } from '@/components/ui/Field';
import { Card, SaveRow } from './primitives';

/**
 * Rehearsal = "dress-rehearse on a phone you own."
 *
 * There is no separate dry-run toggle: the file connector is the dry-run
 * (pick it in the Connector card). Override is the knob you flip when you
 * want the real SMS path to fire but every outbound to land on your own
 * phone — one-slot redirect, incoming replies from that number are mapped
 * back to the most recent real target so threads still route correctly.
 */
export function RehearsalCard({ workspace }: { workspace: Workspace }) {
  const r = workspace.settings.rehearsal;

  const form = usePatchForm({
    resetKey: workspace.updated_at,
    derive: () => ({
      override_to: r.override_to ?? '',
    }),
    save: (s) =>
      api.patchWorkspaceSettings({
        rehearsal: {
          override_to: s.override_to || null,
        },
      } as Partial<WorkspaceSettings>),
  });

  return (
    <Card
      title="Rehearsal"
      description="Redirect every outbound to a phone you own while you test the pipeline end-to-end. To skip the wire entirely, switch the connector above to File."
      footer={<SaveRow dirty={form.dirty} pending={form.saving} onSave={form.save} />}
    >
      <Field
        label="Override recipient"
        hint="Redirects every outbound to this number. Replies from it are rewritten back to the real lead."
      >
        <div className="flex items-center gap-2">
          <Input
            value={form.state.override_to}
            onChange={(e) => form.set('override_to', e.target.value)}
            placeholder="+61 4XX XXX XXX"
            className="font-mono max-w-xs"
          />
          {r.override_to && (
            <Badge tone="teal" uppercase={false}>
              active → {r.override_to}
            </Badge>
          )}
        </div>
      </Field>
    </Card>
  );
}
