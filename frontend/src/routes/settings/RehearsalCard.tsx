import { api } from '@/lib/api';
import { Badge } from '@/components/ui/Badge';
import { Input } from '@/components/ui/Input';
import { usePatchForm } from '@/lib/usePatchForm';
import type { Workspace, WorkspaceSettings } from '@/lib/types';
import { Field } from '@/components/ui/Field';
import { Card, SaveRow, Toggle } from './primitives';

export function RehearsalCard({ workspace }: { workspace: Workspace }) {
  const r = workspace.settings.rehearsal;

  const form = usePatchForm({
    resetKey: workspace.updated_at,
    derive: () => ({
      dry_run: r.dry_run,
      override_to: r.override_to ?? '',
    }),
    save: (s) =>
      api.patchWorkspaceSettings({
        rehearsal: {
          dry_run: s.dry_run,
          override_to: s.override_to || null,
        },
      } as Partial<WorkspaceSettings>),
  });

  return (
    <Card
      title="Rehearsal"
      description="Safe modes for testing without bothering real leads."
      footer={<SaveRow dirty={form.dirty} pending={form.saving} onSave={form.save} />}
    >
      <Toggle
        label="Dry-run"
        description="Forces the file connector. LLM still runs end-to-end; nothing hits the wire."
        checked={form.state.dry_run}
        onToggle={() => form.set('dry_run', !form.state.dry_run)}
      />
      <div className="mt-5 pt-5 border-t border-rule">
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
      </div>
    </Card>
  );
}
