import { useQuery } from '@tanstack/react-query';
import { api } from '@/lib/api';
import { Input } from '@/components/ui/Input';
import { usePatchForm } from '@/lib/usePatchForm';
import type { LlmPreset, Workspace, WorkspaceSettings } from '@/lib/types';
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

  // Presets are static catalog data — fetch once and let the cache
  // hold them for the page lifetime. Failure is non-blocking: the
  // four free-text fields still work without buttons.
  const { data: presetCatalog } = useQuery({
    queryKey: ['llm-presets'],
    queryFn: () => api.getLlmPresets(),
    staleTime: Infinity,
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

      {presetCatalog && presetCatalog.presets.length > 0 && (
        <div className="mt-6 pt-5 border-t border-rule">
          <div className="flex items-baseline justify-between mb-3">
            <div className="label">Presets</div>
            <div className="text-[11px] text-ink-muted font-mono">
              Pricing as of {presetCatalog.pricing_verified_at}
            </div>
          </div>
          <div className="flex flex-wrap gap-2">
            {presetCatalog.presets.map((preset) => (
              <PresetButton
                key={preset.id}
                preset={preset}
                active={isPresetActive(preset, form.state)}
                onApply={() =>
                  form.patch({
                    model_main: preset.models.model_main,
                    model_analysis: preset.models.model_analysis,
                    model_eval: preset.models.model_eval,
                    model_classification: preset.models.model_classification,
                  })
                }
              />
            ))}
          </div>
        </div>
      )}

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

interface ModelState {
  model_main: string;
  model_analysis: string;
  model_eval: string;
  model_classification: string;
}

function isPresetActive(preset: LlmPreset, state: ModelState): boolean {
  return (
    preset.models.model_main === state.model_main &&
    preset.models.model_analysis === state.model_analysis &&
    preset.models.model_eval === state.model_eval &&
    preset.models.model_classification === state.model_classification
  );
}

function PresetButton({
  preset,
  active,
  onApply,
}: {
  preset: LlmPreset;
  active: boolean;
  onApply: () => void;
}) {
  return (
    <button
      type="button"
      onClick={onApply}
      title={preset.description}
      aria-pressed={active}
      className={
        'inline-flex flex-col items-start gap-0.5 px-3 py-2 border text-left transition-colors ' +
        (active
          ? 'border-ink bg-ink text-paper'
          : 'border-rule bg-paper text-ink hover:bg-paper-deep')
      }
    >
      <span className="text-xs tracking-[0.14em] uppercase font-mono">
        {preset.label}
      </span>
      <span
        className={
          'text-[11px] leading-snug max-w-[18rem] ' +
          (active ? 'text-paper' : 'text-ink-muted')
        }
      >
        {preset.description}
      </span>
    </button>
  );
}
