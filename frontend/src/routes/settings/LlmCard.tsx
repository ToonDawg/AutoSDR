import { api } from '@/lib/api';
import { Input } from '@/components/ui/Input';
import { usePatchForm } from '@/lib/usePatchForm';
import type { Workspace, WorkspaceSettings } from '@/lib/types';
import { Field } from '@/components/ui/Field';
import { Card, SaveRow } from './primitives';

export function LlmCard({ workspace }: { workspace: Workspace }) {
  const llm = workspace.settings.llm;

  const form = usePatchForm({
    resetKey: workspace.updated_at,
    derive: () => ({
      gemini: llm.provider_api_keys.gemini ?? '',
      openai: llm.provider_api_keys.openai ?? '',
      anthropic: llm.provider_api_keys.anthropic ?? '',
      model_main: llm.model_main,
      model_analysis: llm.model_analysis,
      model_eval: llm.model_eval,
      model_classification: llm.model_classification,
    }),
    save: (s) =>
      api.patchWorkspaceSettings({
        llm: {
          provider_api_keys: {
            gemini: s.gemini || null,
            openai: s.openai || null,
            anthropic: s.anthropic || null,
          },
          model_main: s.model_main,
          model_analysis: s.model_analysis,
          model_eval: s.model_eval,
          model_classification: s.model_classification,
        },
      } as Partial<WorkspaceSettings>),
  });

  return (
    <Card
      title="LLM"
      description="Provider keys are hot-applied — LiteLLM picks them up on save."
      footer={<SaveRow dirty={form.dirty} pending={form.saving} onSave={form.save} />}
    >
      <div className="grid grid-cols-1 gap-4">
        <Field label="Gemini API key">
          <Input
            type="password"
            value={form.state.gemini}
            onChange={(e) => form.set('gemini', e.target.value)}
            autoComplete="off"
            placeholder="AIzaSy..."
          />
        </Field>
        <Field label="OpenAI API key" hint="Only needed if a model slug starts with openai/.">
          <Input
            type="password"
            value={form.state.openai}
            onChange={(e) => form.set('openai', e.target.value)}
            autoComplete="off"
          />
        </Field>
        <Field
          label="Anthropic API key"
          hint="Only needed if a model slug starts with anthropic/."
        >
          <Input
            type="password"
            value={form.state.anthropic}
            onChange={(e) => form.set('anthropic', e.target.value)}
            autoComplete="off"
          />
        </Field>
      </div>

      <div className="mt-6 pt-5 border-t border-rule grid grid-cols-2 gap-4">
        <Field
          label="Outreach (main)"
          hint="LiteLLM model slug — e.g. gemini/gemini-3-flash-preview"
        >
          <Input
            value={form.state.model_main}
            onChange={(e) => form.set('model_main', e.target.value)}
            className="font-mono"
          />
        </Field>
        <Field label="Analysis">
          <Input
            value={form.state.model_analysis}
            onChange={(e) => form.set('model_analysis', e.target.value)}
            className="font-mono"
          />
        </Field>
        <Field label="Evaluator">
          <Input
            value={form.state.model_eval}
            onChange={(e) => form.set('model_eval', e.target.value)}
            className="font-mono"
          />
        </Field>
        <Field label="Classification">
          <Input
            value={form.state.model_classification}
            onChange={(e) => form.set('model_classification', e.target.value)}
            className="font-mono"
          />
        </Field>
      </div>
    </Card>
  );
}
