import { api } from '@/lib/api';
import { Input } from '@/components/ui/Input';
import { usePatchForm } from '@/lib/usePatchForm';
import type { Workspace, WorkspaceSettings } from '@/lib/types';
import { Field } from '@/components/ui/Field';
import { Card, SaveRow, Toggle } from './primitives';

const _DEFAULT_WINDOW = { enabled: true, start_hour: 8, end_hour: 17 };
const _DEFAULT_ENRICHMENT = {
  enabled: true,
  budget_s: 4.0,
  cache_ttl_days: 30,
  respect_robots: true,
};

function _clampHour(value: string, fallback: number): number {
  const n = Number(value);
  if (!Number.isFinite(n)) return fallback;
  return Math.max(0, Math.min(24, Math.floor(n)));
}

export function BehaviourCard({ workspace }: { workspace: Workspace }) {
  const s = workspace.settings;
  const window = s.outreach_window ?? _DEFAULT_WINDOW;
  const enrichment = s.enrichment ?? _DEFAULT_ENRICHMENT;

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
      outreach_window_enabled: window.enabled,
      outreach_window_start_hour: String(window.start_hour),
      outreach_window_end_hour: String(window.end_hour),
      enrichment_enabled: enrichment.enabled,
      enrichment_budget_s: String(enrichment.budget_s),
      enrichment_cache_ttl_days: String(enrichment.cache_ttl_days),
      enrichment_respect_robots: enrichment.respect_robots,
    }),
    save: (v) => {
      const startHour = _clampHour(v.outreach_window_start_hour, 8);
      let endHour = _clampHour(v.outreach_window_end_hour, 17);
      if (endHour <= startHour) endHour = Math.min(24, startHour + 1);
      const budgetS = Math.max(
        1,
        Math.min(15, Number(v.enrichment_budget_s) || 4),
      );
      const cacheTtl = Math.max(
        0,
        Math.min(365, Math.floor(Number(v.enrichment_cache_ttl_days) || 30)),
      );
      return api.patchWorkspaceSettings({
        auto_reply_enabled: v.auto_reply_enabled,
        suggestions_count: Number(v.suggestions_count) || 3,
        eval_threshold: Number(v.eval_threshold) || 0.85,
        eval_max_attempts: Number(v.eval_max_attempts) || 3,
        scheduler_tick_s: Number(v.scheduler_tick_s) || 60,
        min_inter_send_delay_s: Number(v.min_inter_send_delay_s) || 30,
        max_batch_per_tick: Number(v.max_batch_per_tick) || 2,
        inbound_poll_s: Number(v.inbound_poll_s) || 20,
        outreach_window: {
          enabled: v.outreach_window_enabled,
          start_hour: startHour,
          end_hour: endHour,
        },
        enrichment: {
          enabled: v.enrichment_enabled,
          budget_s: budgetS,
          cache_ttl_days: cacheTtl,
          respect_robots: v.enrichment_respect_robots,
        },
      } as Partial<WorkspaceSettings>);
    },
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

      <div className="mt-5 pt-5 border-t border-rule">
        <Toggle
          label="Pace outreach across business hours"
          description={
            form.state.outreach_window_enabled
              ? `Spreads each campaign's daily quota evenly between ${form.state.outreach_window_start_hour}:00 and ${form.state.outreach_window_end_hour}:00 (server local). Replies, manual kickoff, and follow-up beats are unaffected.`
              : 'Disabled — the scheduler will use the existing burst-then-quota behaviour around the clock.'
          }
          checked={form.state.outreach_window_enabled}
          onToggle={() =>
            form.set('outreach_window_enabled', !form.state.outreach_window_enabled)
          }
        />
        {form.state.outreach_window_enabled && (
          <div className="grid grid-cols-2 gap-4 mt-4">
            <Field label="Start hour" hint="0-23, server-local.">
              <Input
                type="number"
                min={0}
                max={23}
                value={form.state.outreach_window_start_hour}
                onChange={(e) =>
                  form.set('outreach_window_start_hour', e.target.value)
                }
                className="font-mono"
              />
            </Field>
            <Field label="End hour" hint="1-24, server-local. Exclusive.">
              <Input
                type="number"
                min={1}
                max={24}
                value={form.state.outreach_window_end_hour}
                onChange={(e) =>
                  form.set('outreach_window_end_hour', e.target.value)
                }
                className="font-mono"
              />
            </Field>
          </div>
        )}
      </div>

      <div className="mt-5 pt-5 border-t border-rule">
        <Toggle
          label="Lead-website enrichment"
          description={
            form.state.enrichment_enabled
              ? `Fetches each lead's homepage, robots.txt, and sitemap before the analysis LLM call so the angle has structural signal to ground in. Hard caps: ≤3 HTTP requests per lead, ≤1.5 s/request, ≤${form.state.enrichment_budget_s} s wall-clock.`
              : "Disabled — outreach falls back to the un-enriched raw_data. Existing enrichment blobs already on the lead are still fed to the LLM (operator stays in control)."
          }
          checked={form.state.enrichment_enabled}
          onToggle={() =>
            form.set('enrichment_enabled', !form.state.enrichment_enabled)
          }
        />
        {form.state.enrichment_enabled && (
          <>
            <div className="grid grid-cols-2 gap-4 mt-4">
              <Field
                label="Budget (s)"
                hint="Total wall-clock per lead. Clamped to 1–15."
              >
                <Input
                  type="number"
                  step="0.5"
                  min={1}
                  max={15}
                  value={form.state.enrichment_budget_s}
                  onChange={(e) =>
                    form.set('enrichment_budget_s', e.target.value)
                  }
                  className="font-mono"
                />
              </Field>
              <Field
                label="Cache TTL (days)"
                hint="Re-enrichment skipped inside this window. 0 = always re-enrich."
              >
                <Input
                  type="number"
                  min={0}
                  max={365}
                  value={form.state.enrichment_cache_ttl_days}
                  onChange={(e) =>
                    form.set('enrichment_cache_ttl_days', e.target.value)
                  }
                  className="font-mono"
                />
              </Field>
            </div>
            <div className="mt-4">
              <Toggle
                label="Respect robots.txt"
                description={
                  form.state.enrichment_respect_robots
                    ? "Polite default. A Disallow: / for AutoSDR's user-agent shortcuts the run with status='blocked'."
                    : 'Aggressive scrape. The fetcher ignores Disallow rules. Use with care — the user-agent still identifies AutoSDR.'
                }
                checked={form.state.enrichment_respect_robots}
                onToggle={() =>
                  form.set(
                    'enrichment_respect_robots',
                    !form.state.enrichment_respect_robots,
                  )
                }
              />
            </div>
          </>
        )}
      </div>
    </Card>
  );
}
