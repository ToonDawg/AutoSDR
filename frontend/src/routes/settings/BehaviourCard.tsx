import { api } from '@/lib/api';
import { Input } from '@/components/ui/Input';
import { usePatchForm } from '@/lib/usePatchForm';
import type { Workspace, WorkspaceSettings } from '@/lib/types';
import { Field } from '@/components/ui/Field';
import { Card, SaveRow, Toggle } from './primitives';

export function BehaviourCard({ workspace }: { workspace: Workspace }) {
  const s = workspace.settings;

  const form = usePatchForm({
    resetKey: workspace.updated_at,
    derive: () => ({
      auto_reply_enabled: s.auto_reply_enabled,
      suggestions_count: String(s.suggestions_count),
      eval_threshold: String(s.eval_threshold),
      eval_max_attempts: String(s.eval_max_attempts),
      scheduler_tick_s: String(s.scheduler_tick_s),
      min_inter_send_delay_s: String(s.min_inter_send_delay_s),
      max_batch_per_tick: String(s.max_batch_per_tick),
      inbound_poll_s: String(s.inbound_poll_s),
    }),
    save: (v) =>
      api.patchWorkspaceSettings({
        auto_reply_enabled: v.auto_reply_enabled,
        suggestions_count: Number(v.suggestions_count) || 3,
        eval_threshold: Number(v.eval_threshold) || 0.85,
        eval_max_attempts: Number(v.eval_max_attempts) || 3,
        scheduler_tick_s: Number(v.scheduler_tick_s) || 60,
        min_inter_send_delay_s: Number(v.min_inter_send_delay_s) || 30,
        max_batch_per_tick: Number(v.max_batch_per_tick) || 2,
        inbound_poll_s: Number(v.inbound_poll_s) || 20,
      } as Partial<WorkspaceSettings>),
  });

  return (
    <Card
      title="AI behaviour"
      description="First-message-only is the default. Flip the switch to re-enable the old auto-reply loop."
      footer={<SaveRow dirty={form.dirty} pending={form.saving} onSave={form.save} />}
    >
      <Toggle
        label="Auto-reply after first contact"
        description={
          form.state.auto_reply_enabled
            ? 'Legacy mode. AutoSDR will classify, generate, and send follow-ups without a human.'
            : 'Recommended. After the initial message, replies stash AI suggestions and park for your review.'
        }
        checked={form.state.auto_reply_enabled}
        onToggle={() => form.set('auto_reply_enabled', !form.state.auto_reply_enabled)}
      />

      <div className="grid grid-cols-2 gap-4 mt-5 pt-5 border-t border-rule">
        <Field label="Suggestions per reply" hint="2-3 is plenty. More just burns tokens.">
          <Input
            type="number"
            min={1}
            max={6}
            value={form.state.suggestions_count}
            onChange={(e) => form.set('suggestions_count', e.target.value)}
            className="font-mono"
          />
        </Field>
        <Field label="Evaluator threshold" hint="Overall score required to pass (0–1).">
          <Input
            type="number"
            step="0.01"
            min={0}
            max={1}
            value={form.state.eval_threshold}
            onChange={(e) => form.set('eval_threshold', e.target.value)}
            className="font-mono"
          />
        </Field>
        <Field label="Max eval attempts">
          <Input
            type="number"
            min={1}
            max={10}
            value={form.state.eval_max_attempts}
            onChange={(e) => form.set('eval_max_attempts', e.target.value)}
            className="font-mono"
          />
        </Field>
        <Field label="Scheduler tick (s)">
          <Input
            type="number"
            min={10}
            value={form.state.scheduler_tick_s}
            onChange={(e) => form.set('scheduler_tick_s', e.target.value)}
            className="font-mono"
          />
        </Field>
        <Field label="Min inter-send delay (s)" hint="Throttle. Lower = bursty.">
          <Input
            type="number"
            min={0}
            value={form.state.min_inter_send_delay_s}
            onChange={(e) => form.set('min_inter_send_delay_s', e.target.value)}
            className="font-mono"
          />
        </Field>
        <Field label="Max sends per tick">
          <Input
            type="number"
            min={1}
            value={form.state.max_batch_per_tick}
            onChange={(e) => form.set('max_batch_per_tick', e.target.value)}
            className="font-mono"
          />
        </Field>
        <Field label="Inbound poll (s)" hint="TextBee connector only.">
          <Input
            type="number"
            min={5}
            value={form.state.inbound_poll_s}
            onChange={(e) => form.set('inbound_poll_s', e.target.value)}
            className="font-mono"
          />
        </Field>
      </div>
    </Card>
  );
}
