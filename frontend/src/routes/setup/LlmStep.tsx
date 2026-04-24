import { Input } from '@/components/ui/Input';
import { Field } from '@/components/ui/Field';
import { StepCard } from './primitives';
import type { FormState, SetField } from './types';

/**
 * Step 1 of the setup wizard: Gemini key + primary model.
 *
 * We hard-code Gemini here because that's the only provider the
 * backend currently wires via LiteLLM on first boot. The Settings
 * `LlmCard` is richer — it lets operators swap models per role
 * (evaluator, classifier, etc) after setup — but during onboarding
 * we want to keep the field count short.
 */
export function LlmStep({
  state,
  set,
}: {
  state: FormState;
  set: SetField;
}) {
  return (
    <StepCard
      title="Connect an LLM"
      description="AutoSDR currently ships with Gemini. You need a Google AI Studio key. Model choice affects cost and latency — defaults are fine."
    >
      <Field label="Gemini API key" required>
        <Input
          type="password"
          value={state.llm_api_key}
          onChange={(e) => set('llm_api_key', e.target.value)}
          placeholder="AIzaSy..."
          autoComplete="off"
        />
      </Field>
      <Field label="Primary model" hint="Used for outreach drafting. LiteLLM model slug.">
        <Input
          value={state.model_main}
          onChange={(e) => set('model_main', e.target.value)}
        />
      </Field>
      <p className="text-xs text-ink-muted">
        The same key is used for the analysis / evaluation / classification models. You can swap
        models per-role later in Settings.
      </p>
    </StepCard>
  );
}
