import { useCallback, useMemo, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { useMutation } from '@tanstack/react-query';
import { api } from '@/lib/api';
import { Button } from '@/components/ui/Button';
import { BusinessStep } from './setup/BusinessStep';
import { ConnectorStep } from './setup/ConnectorStep';
import { LlmStep } from './setup/LlmStep';
import { StepIndicator } from './setup/StepIndicator';
import { INITIAL_STATE, type FormState, type SetField, type Step } from './setup/types';
import type { SetupPayload } from '@/lib/types';

/**
 * First-run setup wizard coordinator.
 *
 * Gated in `App.tsx` via `GET /api/setup/required`: if no workspace row
 * exists yet, every other API call 409s and the fetch wrapper redirects
 * here. The user walks through three short steps — business, LLM, SMS
 * connector — and on submit we call `POST /api/setup` which creates the
 * workspace row, writes the initial settings blob, hot-applies the LLM
 * keys, and builds the chosen connector. Then we send them to `/`.
 *
 * Each step lives in its own file under `./setup/`; this component only
 * orchestrates state, navigation, payload shaping, and the submit call.
 */
export function Setup() {
  const navigate = useNavigate();
  const [step, setStep] = useState<Step>(0);
  const [state, setState] = useState<FormState>(INITIAL_STATE);
  const [error, setError] = useState<string | null>(null);

  const set: SetField = useCallback(
    (key, value) => setState((prev) => ({ ...prev, [key]: value })),
    [],
  );

  const payload = useMemo<SetupPayload>(() => buildPayload(state), [state]);

  const submit = useMutation({
    mutationFn: () => api.runSetup(payload),
    onSuccess: () => navigate('/', { replace: true }),
    onError: (err: unknown) => {
      setError(err instanceof Error ? err.message : 'Setup failed.');
    },
  });

  const canAdvanceFromStep0 =
    state.business_name.trim().length > 0 && state.business_dump.trim().length > 0;
  const canAdvanceFromStep1 = state.llm_api_key.trim().length > 0;
  const canSubmit =
    state.connector_type === 'file' ||
    (state.connector_type === 'textbee' &&
      state.textbee_api_key.trim().length > 0 &&
      state.textbee_device_id.trim().length > 0) ||
    (state.connector_type === 'smsgate' &&
      state.smsgate_api_url.trim().length > 0 &&
      state.smsgate_username.trim().length > 0 &&
      state.smsgate_password.trim().length > 0);

  return (
    <div className="min-h-screen bg-paper text-ink flex flex-col">
      <header className="border-b border-rule px-8 py-5 flex items-center justify-between">
        <div className="flex items-center gap-3">
          <div className="h-7 w-7 rounded-sm bg-ink text-paper flex items-center justify-center font-mono text-xs font-semibold">
            AS
          </div>
          <div>
            <div className="text-sm font-medium">AutoSDR</div>
            <div className="text-[11px] text-ink-muted font-mono tracking-wide uppercase">
              First-run setup
            </div>
          </div>
        </div>
        <StepIndicator step={step} />
      </header>

      <main className="flex-1 flex items-start justify-center py-12 px-6">
        <div className="w-full max-w-2xl">
          {step === 0 && <BusinessStep state={state} set={set} />}
          {step === 1 && <LlmStep state={state} set={set} />}
          {step === 2 && <ConnectorStep state={state} set={set} />}

          {error && (
            <div className="mt-6 border border-[color:var(--color-oxblood)]/30 bg-oxblood-soft text-oxblood px-4 py-3 text-sm">
              {error}
            </div>
          )}

          <div className="mt-8 flex items-center justify-between">
            <Button
              variant="ghost"
              onClick={() => setStep((s) => (s > 0 ? ((s - 1) as Step) : s))}
              disabled={step === 0 || submit.isPending}
            >
              Back
            </Button>
            {step < 2 ? (
              <Button
                variant="primary"
                onClick={() => {
                  setError(null);
                  setStep((s) => (s + 1) as Step);
                }}
                disabled={
                  (step === 0 && !canAdvanceFromStep0) ||
                  (step === 1 && !canAdvanceFromStep1)
                }
              >
                Continue
              </Button>
            ) : (
              <Button
                variant="primary"
                onClick={() => {
                  setError(null);
                  submit.mutate();
                }}
                disabled={!canSubmit || submit.isPending}
              >
                {submit.isPending ? 'Creating workspace…' : 'Finish setup'}
              </Button>
            )}
          </div>
        </div>
      </main>
    </div>
  );
}

function buildPayload(state: FormState): SetupPayload {
  const base: SetupPayload = {
    business_name: state.business_name.trim(),
    business_dump: state.business_dump.trim(),
    tone_prompt: state.tone_prompt.trim() || undefined,
    llm_provider: 'gemini',
    llm_api_key: state.llm_api_key.trim(),
    model_main: state.model_main.trim() || undefined,
    connector_type: state.connector_type,
  };
  if (state.connector_type === 'textbee') {
    base.textbee = {
      api_url: state.textbee_api_url.trim() || undefined,
      api_key: state.textbee_api_key.trim(),
      device_id: state.textbee_device_id.trim(),
    };
  }
  if (state.connector_type === 'smsgate') {
    base.smsgate = {
      api_url: state.smsgate_api_url.trim(),
      username: state.smsgate_username.trim(),
      password: state.smsgate_password.trim(),
    };
  }
  return base;
}
