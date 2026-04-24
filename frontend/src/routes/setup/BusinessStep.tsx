import { Input, Textarea } from '@/components/ui/Input';
import { Field } from '@/components/ui/Field';
import { StepCard } from './primitives';
import type { FormState, SetField } from './types';

/**
 * Step 0 of the setup wizard: capture business identity.
 *
 * The `business_dump` copy ends up inside every outreach prompt, so
 * the input copy here is deliberately pushy about "paragraph is
 * fine" — the junior who wrote the original filled it with a
 * one-liner and all outreach came out sounding identical.
 */
export function BusinessStep({
  state,
  set,
}: {
  state: FormState;
  set: SetField;
}) {
  return (
    <StepCard
      title="Tell us about your business"
      description="This goes into every generated message so the AI can sound like you. You can change it later."
    >
      <Field label="Business name" required>
        <Input
          value={state.business_name}
          onChange={(e) => set('business_name', e.target.value)}
          placeholder="Acme Roofing"
        />
      </Field>

      <Field
        label="What you do and what a good lead looks like"
        hint="A paragraph is fine. Products, who you sell to, what 'won' means."
        required
      >
        <Textarea
          rows={8}
          value={state.business_dump}
          onChange={(e) => set('business_dump', e.target.value)}
          placeholder="We install metal roofing for suburban homeowners in QLD. A good lead has a 15+ year old roof and owns the house. 'Won' means they booked a free inspection."
        />
      </Field>

      <Field
        label="Tone (optional)"
        hint="How messages should sound. Plain English, no marketing-speak."
      >
        <Textarea
          rows={3}
          value={state.tone_prompt}
          onChange={(e) => set('tone_prompt', e.target.value)}
          placeholder="Short, direct, no emojis. Sign off with 'Mark'."
        />
      </Field>
    </StepCard>
  );
}
