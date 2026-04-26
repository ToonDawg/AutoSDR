import { format, formatDistanceToNowStrict, parseISO } from 'date-fns';
import type {
  CampaignStatusT,
  ContactTypeT,
  HitlReasonT,
  LeadStatusT,
  ReplyIntentT,
  ThreadStatusT,
} from './types';

function parseApiDate(iso: string): Date {
  const value = iso.trim();
  const hasTimezone = /(?:Z|[+-]\d{2}:?\d{2})$/i.test(value);
  return parseISO(hasTimezone ? value : `${value.replace(' ', 'T')}Z`);
}

export function relTime(iso: string | null | undefined): string {
  if (!iso) return '—';
  try {
    return formatDistanceToNowStrict(parseApiDate(iso), { addSuffix: true });
  } catch {
    return '—';
  }
}

export function absTime(iso: string | null | undefined, pattern = 'd MMM · HH:mm'): string {
  if (!iso) return '—';
  try {
    return format(parseApiDate(iso), pattern);
  } catch {
    return '—';
  }
}

/** Day-and-month axis label, e.g. "4 Apr". Used by the dashboard sparkline. */
export function shortDate(iso: string | null | undefined): string {
  return absTime(iso, 'd MMM');
}

export function formatPhone(p: string | null | undefined): string {
  if (!p) return '—';
  if (p.startsWith('+61')) {
    const rest = p.slice(3);
    if (rest.length === 9) return `+61 ${rest.slice(0, 3)} ${rest.slice(3, 6)} ${rest.slice(6)}`;
    if (rest.length === 10) return `+61 ${rest.slice(0, 1)} ${rest.slice(1, 5)} ${rest.slice(5)}`;
  }
  return p;
}

/**
 * Map a 0-100 evaluator score onto the shared Badge tone vocabulary.
 * The threshold (85 = forest, 70 = mustard, else oxblood) is lifted
 * from the suggestions UI — keep it in one place so the Dashboard,
 * Inbox, and ThreadDetail can't drift on colour grading.
 */
export type ScoreTone = 'forest' | 'mustard' | 'oxblood' | 'neutral';

export function evalScoreTone(score: number | null | undefined): ScoreTone {
  if (score == null) return 'neutral';
  if (score >= 85) return 'forest';
  if (score >= 70) return 'mustard';
  return 'oxblood';
}

export const INTENT_LABEL: Partial<Record<ReplyIntentT, string>> & Record<string, string | undefined> = {
  positive: 'Positive',
  objection: 'Objection',
  question: 'Question',
  negative: 'Not interested',
  unclear: 'Unclear',
  bot_check: 'Suspects bot',
  goal_achieved: 'Goal achieved',
  human_requested: 'Asked for human',
};

/**
 * Human-friendly label per HITL reason. Keys mirror ``HitlReason`` in
 * ``types.ts`` which in turn mirror the exact strings emitted by the
 * Python pipelines. The default-mode reason (``awaiting_human_reply``)
 * is what the first-message-only flow always lands on; everything else
 * covers outreach eval failure, connector errors, legacy auto-reply
 * escalation, and manual human takeover. Typed loose so unknown backend
 * values fall through to `undefined` rather than causing compile errors
 * at every call site.
 */
export const HITL_LABEL: Partial<Record<HitlReasonT, string>> & Record<string, string | undefined> = {
  awaiting_human_reply: 'Lead replied — pick a response',
  eval_failed_after_max_attempts: 'Draft failed evaluator (max attempts)',
  reply_eval_failed: 'Reply draft failed evaluator',
  connector_send_failed: 'Could not send — connector error',
  unclear: "Couldn't read the reply",
  bot_check: 'Lead suspects a bot',
  human_requested: 'Lead asked to speak with a human',
  low_confidence: 'Classification confidence too low',
  max_auto_replies_reached: 'Hit the auto-reply ceiling',
  escalated: 'Escalated for review',
  taken_over_by_human: 'Taken over — awaiting your send',
};

export const THREAD_STATUS_LABEL: Record<ThreadStatusT, string> = {
  active: 'Active',
  paused: 'Paused',
  paused_for_hitl: 'Needs you',
  won: 'Won',
  lost: 'Lost',
  skipped: 'Skipped',
};

export const LEAD_STATUS_LABEL: Record<LeadStatusT, string> = {
  new: 'Queued',
  contacted: 'Contacted',
  replied: 'Replied',
  won: 'Won',
  lost: 'Lost',
  skipped: 'Skipped',
};

export const CAMPAIGN_STATUS_LABEL: Record<CampaignStatusT, string> = {
  draft: 'Draft',
  active: 'Active',
  paused: 'Paused',
  completed: 'Completed',
};

export const CONTACT_TYPE_LABEL: Partial<Record<ContactTypeT, string>> & Record<string, string | undefined> = {
  mobile: 'Mobile',
  landline: 'Landline',
  toll_free: 'Toll-free',
  unknown: 'Unknown',
  email: 'Email',
};

/**
 * Humanise the ``skip_reason`` strings that the Python importer emits.
 *
 * The server stores machine-tags like ``not_a_mobile_number:landline`` so
 * the reason column in the leads table is explicit and greppable. The UI
 * shouldn't leak those into prose — operators see "Landline — SMS won't
 * reach this lead" instead. Unknown reasons fall through with underscores
 * converted to spaces so new backend tags don't render as garbage.
 */
export function formatSkipReason(reason: string | null | undefined): string {
  if (!reason) return '—';

  if (reason.startsWith('not_a_mobile_number:')) {
    const kind = reason.slice('not_a_mobile_number:'.length);
    switch (kind) {
      case 'landline':
        return 'Landline — SMS won\u2019t reach this lead';
      case 'toll_free':
        return 'Toll-free — SMS won\u2019t reach this lead';
      case 'unknown':
        return 'Unverified number type — not messaging to be safe';
      default:
        return `Not a mobile (${kind.replace(/_/g, ' ')}) — not messaging`;
    }
  }

  switch (reason) {
    case 'no_contact_uri':
      return 'No phone number in the source row';
    case 'invalid_phone_format':
      return 'Phone number could not be parsed';
    case 'duplicate_no_new_data':
      return 'Duplicate — no new data to merge';
    default:
      return reason.replace(/_/g, ' ');
  }
}

