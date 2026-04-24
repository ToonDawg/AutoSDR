import { api } from '@/lib/api';
import { Input, Textarea } from '@/components/ui/Input';
import { usePatchForm } from '@/lib/usePatchForm';
import type { Workspace } from '@/lib/types';
import { Field } from '@/components/ui/Field';
import { Card, SaveRow } from './primitives';

export function WorkspaceCard({ workspace }: { workspace: Workspace }) {
  const form = usePatchForm({
    resetKey: workspace.updated_at,
    derive: () => ({
      business_name: workspace.business_name,
      business_dump: workspace.business_dump,
      tone_prompt: workspace.tone_prompt ?? '',
    }),
    save: (s) =>
      api.patchWorkspace({
        business_name: s.business_name,
        business_dump: s.business_dump,
        tone_prompt: s.tone_prompt || null,
      }),
  });

  return (
    <Card
      title="Workspace"
      description="Business context the AI cites in every draft."
      footer={<SaveRow dirty={form.dirty} pending={form.saving} onSave={form.save} />}
    >
      <Field label="Business name">
        <Input
          value={form.state.business_name}
          onChange={(e) => form.set('business_name', e.target.value)}
        />
      </Field>
      <Field label="Business description" hint="What you do, who you sell to, what 'won' means.">
        <Textarea
          value={form.state.business_dump}
          onChange={(e) => form.set('business_dump', e.target.value)}
          rows={6}
        />
      </Field>
      <Field label="Tone" hint="Optional — how messages should sound.">
        <Textarea
          value={form.state.tone_prompt}
          onChange={(e) => form.set('tone_prompt', e.target.value)}
          rows={3}
        />
      </Field>
    </Card>
  );
}
