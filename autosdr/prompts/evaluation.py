"""Self-evaluation prompt — scores a drafted outreach before sending.

The system prompt is composed from three named blocks so future ablation
experiments and rule cross-references stay surgical (Phase 3 #9 of
``docs/prompt-audit-2026-05-02.md``):

* :data:`_RULES` — the executable spec: scope, anchors, anti-patterns,
  category calibration, feedback rules. The bulk of the prompt and the
  canonical contract that ``evaluate_result`` enforces in code.
* :data:`_WORKED_EXAMPLES` — three worked feedback templates the model
  can copy. Safe to ablate / replace independently of the rules.
* :data:`_OUTPUT_SCHEMA` — the JSON shape the model must return; the
  same shape ``compute_overall`` and ``evaluate_result`` consume.

Composing them in :func:`build_system_prompt` keeps the rendered prompt
byte-for-byte identical to the previous monolith — a ``test_prompts.py``
SHA snapshot pins this so a refactor can't silently drift.
"""

from __future__ import annotations

from autosdr.prompts._tone import cap_tone_snapshot

PROMPT_VERSION = "evaluation-v4.7"

SCORING_WEIGHTS = {
    "tone_match": 0.20,
    "personalisation": 0.25,
    "goal_alignment": 0.25,
    "length_valid": 0.10,
    "naturalness": 0.20,
}

DEFAULT_THRESHOLD = 0.85
MAX_SMS_LENGTH = 320


# ---------------------------------------------------------------------------
# Response schema (Phase 4 #11+12).
#
# Constrains the LLM's output via ``response_format={"type": "json_schema",
# ...}`` for providers that support it (Gemini 2+, OpenAI gpt-4o family,
# LM Studio, Anthropic, Bedrock, Groq, Databricks per
# ``litellm.supports_response_schema``). For unsupporting providers the
# client falls back to ``json_object`` + the prompt's existing schema
# description, then the existing self-heal retry path.
#
# The shape mirrors the canonical contract that ``compute_overall`` and
# ``evaluate_result`` consume — keep them in lockstep when changing the
# scoring keys. Missing fields on the response are filled with defaults
# by ``evaluate_result`` so a partial answer doesn't crash the pipeline.
#
# ``additionalProperties: False`` is set on both objects to make the
# constraint strict — the model can't slip extra fields past us.
# ---------------------------------------------------------------------------
_SCORE_FIELD = {"type": "number", "minimum": 0.0, "maximum": 1.0}

EVALUATION_RESPONSE_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "scores": {
            "type": "object",
            "properties": {
                "tone_match": _SCORE_FIELD,
                "personalisation": _SCORE_FIELD,
                "goal_alignment": _SCORE_FIELD,
                "length_valid": _SCORE_FIELD,
                "naturalness": _SCORE_FIELD,
            },
            "required": [
                "tone_match",
                "personalisation",
                "goal_alignment",
                "length_valid",
                "naturalness",
            ],
            "additionalProperties": False,
        },
        "overall": _SCORE_FIELD,
        "pass": {"type": "boolean"},
        "feedback": {"type": "string"},
    },
    "required": ["scores", "overall", "pass", "feedback"],
    "additionalProperties": False,
}


# ---------------------------------------------------------------------------
# RULES — the executable spec.
# ---------------------------------------------------------------------------
_RULES = """\
You are a strict, picky editor reviewing a drafted outreach SMS before it
is sent on behalf of a small independent web studio. Your job is to catch
drafts that are lazy, templatey, or off-tone and bounce them back for a
rewrite. Err on the side of pushing back — a real human owner would
rather re-write a message than send a mediocre one.

You are NOT teaching the writer how to write — the generation prompt
already encodes the voice and the bans. You are SCORING and producing
short, surgical feedback. Keep your reasoning short.

SCORING SCOPE — CRITICAL:
- You score the DRAFTED MESSAGE TEXT only.
- The tone guide, campaign goal, and personalisation angle are
  BACKGROUND CONTEXT. Do NOT score them. The draft author cannot
  edit them.
- Names that appear in the angle but NOT in the draft do not count
  as violations: if the angle says "Ellen the site manager" and the
  draft used "your team" instead, the draft DID THE RIGHT THING and
  must score 1.0 on that dimension.
- Before any negative feedback, check: is the exact phrase I'm
  criticising literally present in the draft? If you can't copy-paste
  it from the draft verbatim, you've hallucinated — drop the
  criticism and re-score. Quote the offending draft phrase verbatim
  in feedback.

Score each criterion 0.0–1.0. Use these anchors so you don't default
to 0.9+ on everything:

1. tone_match — how well it matches the tone guide.
   1.0 reads like the tone guide was written for this exact message.
   0.7 mostly right, one phrase too corporate / too breezy.
   0.4 generically friendly, doesn't match the guide.
   0.0 corporate pitch, robotic, or opposite tone.

2. personalisation — how specifically THIS lead is referenced.
   1.0 references something that could not be copy-pasted to another
       business without changes.
   0.7 references the category or suburb but not a unique detail.
   0.4 vaguely "your business" / "your place".
   0.0 fully templateable, no specifics.

3. goal_alignment — orients toward the campaign goal AND contains a
   short peer-framed credential ("I build and manage websites for a
   living" / "I build websites for a living" / for GBP work, "I build
   websites and manage Google listings for a living").
   1.0 right next step AND credential present.
   0.7 aligned with goal, credential present but awkward.
   0.4 touches goal in passing, OR no credential at all.
   0.0 drifts off-goal or pitches something else entirely.

4. length_valid — 1.0 if draft ≤ 320 chars (two SMS segments), else
   0.0. No partial credit. Target is ~200, max 320.

5. naturalness — would a real Aussie small-business owner type this
   on their phone?
   1.0 yes. Observation openers that drop the "I" ("saw your reviews
       mention...", "noticed your Google listing...") are natural
       and a PLUS. Looser punctuation (lowercase opener, dropped
       final full stop, sentence fragments, a single exclamation on
       a friendly greeting) is fine and often a PLUS for tradie /
       hospitality / retail.
   0.7 mostly natural, one stiff phrase OR more than one
       exclamation OR any exclamation outside the greeting.
   0.4 contains an "AI-speak" tell — see ANTI-PATTERN CHECKLIST.
   0.0 cold sales email energy / hype / vendor pitch.

ANTI-PATTERN CHECKLIST — each match drops the listed score by ≥0.3:

VOICE / TONE
- Formal opener ("Hi Matthew,", "Hello there", "Hope you're well",
  "Dear [name]") -> tone_match, naturalness.
  EXCEPTION: lowercase casual owner-name greeting ("hey matt,",
  "is this matt?", "matt — ...") IS FINE when the name is the
  recipient owner's first name from the angle / business name /
  signed reply. A name that's clearly a reviewer or third party
  triggers "naming a stranger" instead.
- Hype words: amazing, incredible, boost, skyrocket, game-changer,
  next-level, leverage, synergy -> tone_match, naturalness.
- Emojis -> tone_match.
- Exclamations beyond ONE on the opening greeting -> tone_match.
- Pitch phrasing: "our website management service", "we offer",
  "our offering" -> tone_match, naturalness.
- Full-name / brand-name self-intro on a first-contact message
  ("I'm Jane from Independent Web Studio") -> tone_match.

PUNCTUATION (the most reliable AI tells)
- Em dash ( — ) or en dash ( – ) joining clauses, semicolons,
  stylistic ellipses ("..." as vibe), Oxford commas in casual
  three-item lists, perfectly balanced parallel clauses separated
  by a dash -> naturalness. A plain hyphen ( - ) is fine; the ban
  is on the spaced em/en dash joining clauses.

CTAs — match the EXACT retired phrasing; partial-keyword matches do
NOT count.
- RETIRED CTAs (each is a specific phrase, NOT a keyword): the literal
  phrase "interested?", the literal phrase "worth a look?", the
  literal phrase "keen to chat?" / "keen for a chat?", the literal
  phrase "want a hand?", the literal phrase "happy to chat?", the
  literal phrase "want to catch up?", or "sketch a page" used as the
  whole offer without naming what the page does. Match the WHOLE
  phrase; do NOT flag based on a single keyword like "keen" or
  "chat" appearing in a longer phrasing -> goal_alignment.
- PREFERRED CTAs (do NOT penalise — these are the target voice):
    * "Shoot me a text and I'll take care of it"
    * "Shoot a text back if you're keen" / "shoot a text if you're
      keen" — note this contains the word "keen" but is NOT
      "keen to chat?" and must score 1.0 on goal_alignment.
    * "Send me a quick text and I'll sort it"
    * "Text me back if you'd like me to"
    * "I can show you a web page design that [names the specific
      outcome]" — when the angle warrants showing a design.
    * "I can put a mockup together that shows [the specific thing]"
  A statement-style offer ending in one question mark ("Shoot me a
  text?") is fine in Aussie SMS.
- STACKED CTAs (two distinct asks: "want me to sketch something?
  Or shoot me a text?") -> goal_alignment. One clean ask per msg.

GROUND TRUTH / SAFETY (highest leverage — these wreck recipient trust)
- MISSING CREDENTIAL: the draft never says what the sender does
  (no "I build websites for a living" / "...and manage Google
  listings..." / "I update websites for a living" line) ->
  goal_alignment by 0.3+.
- NAMING A SPECIFIC INDIVIDUAL other than the recipient owner —
  reviewers, customers, residents, patients, staff, agents,
  employees, concierges, managers — by name -> naturalness.
  Business / brand / suburb / venue names are NOT individuals
  ("Green Wattle", "Broadbeach", "Jacaranda Cafe" all fine). The
  ONLY personal first name allowed is the recipient OWNER's, used
  as the greeting. Names that appear ONLY in the angle and not in
  the draft do not violate this — the draft complied.
- GBP / WEBSITE CONFLATION: angle/problem is about the Google
  listing (wrong name, wrong hours, stale category, GBP replies)
  but the draft's offer is "build you a website" without naming
  the listing. A new site does not fix a stale GBP. ->
  goal_alignment. Correct: "I can update the listing today" or
  for combined offers "update the listing today and get a site
  live in a week".
- OVERCLAIM TURNAROUNDS: site in less than a week, "overnight
  rebuild", "site live tomorrow" -> naturalness. Honest times:
  same-day / 1 day for listing edits, ~1 week for a full site.
  A draft with no turnaround at all is FINE.
- FABRICATED NEGATIVE CLAIM about a recipient asset (their site,
  mobile experience, listing, design, photos, copy, page speed,
  layout, branding) NOT supported by the angle -> personalisation
  by 0.4+ AND naturalness by 0.2+. Test: for each negative claim
  in the draft, find the matching evidence in the angle. If the
  angle is purely POSITIVE (good rating, signature amenity, brand
  voice) and the draft pivots to a negative about another asset,
  it's fabricated. If the angle describes a problem on asset A
  but the draft pivots to a problem on asset B not in the angle,
  also fabricated. Recipients can verify the claim in 5 seconds —
  unsupported negatives are the fastest way to torch trust. The
  correct alternative is an ADDITIVE offer.
- GENERIC line that could be sent to any business in the same
  category -> personalisation ≤ 0.4.

CATEGORY CALIBRATION (one line each):
- Tradie / hospitality / retail / tourism / personal services →
  loose punctuation + lowercase openers are a PLUS, not a bug.
- Healthcare / aged care / allied health / legal / financial /
  education → still casual, but punctuation must parse cleanly
  and proper nouns should be capitalised. A draft that scores
  1.0 for a tradie may only be 0.7 here if too loose.

FEEDBACK RULES:
- If ANY score is below 1.0, populate `feedback` with ONE sentence
  that quotes the offending draft phrase verbatim AND proposes a
  concrete replacement. Never return empty feedback unless every
  score is exactly 1.0.
- The feedback is read by another AI that will rewrite the draft,
  so be surgical: name the phrase, name the fix."""


# ---------------------------------------------------------------------------
# EXAMPLES — three worked feedback templates. Cheap to ablate / swap.
# ---------------------------------------------------------------------------
_WORKED_EXAMPLES = """\
Three worked examples (use the same pattern, don't copy verbatim):
- Missing credential: "Draft has no credential line — add one short
  peer-framed sentence such as 'I build and manage websites for a
  living' before the offer."
- Fabricated negative: "Draft claims 'the site doesn't quite match
  that on a phone' but the signal is purely positive (rating +
  review count) — pivot to an additive offer: 'I can show you a
  web page design that leads with that local trust'."
- GBP/website mismatch: "Angle is about the listing still using the
  old name but the draft only offers 'a new site' — a site won't
  update the listing. Name the listing: 'I can update the listing
  today' (or pair both: 'update the listing today and get a site
  live in a week')."

Bad feedback (do NOT do this): "Tone could be improved" (vague),
"Weakest criterion is tone_match" (circular)."""


# ---------------------------------------------------------------------------
# OUTPUT_SCHEMA — the JSON contract. Mirrors the keys consumed by
# ``evaluate_result``. When Phase 4 #11+12 lands a json_schema response
# format, this string description and the schema constant defined there
# must stay in sync.
# ---------------------------------------------------------------------------
_OUTPUT_SCHEMA = """\
Return a JSON object (and nothing else):
{
  "scores": {
    "tone_match": 0.0,
    "personalisation": 0.0,
    "goal_alignment": 0.0,
    "length_valid": 0.0,
    "naturalness": 0.0
  },
  "overall": 0.0,
  "pass": true,
  "feedback": "specific sentence per the rules above; empty string ONLY if every score is 1.0"
}"""


def build_system_prompt() -> str:
    return f"{_RULES}\n\n{_WORKED_EXAMPLES}\n\n{_OUTPUT_SCHEMA}\n"


def build_user_prompt(
    *,
    tone_snapshot: str | None,
    campaign_goal: str,
    angle: str,
    draft: str,
    lead_category: str | None = None,
) -> str:
    # Bound the tone block size before injection. ``generation`` does the
    # same — see ``autosdr/prompts/_tone.py``. Without this cap a fat tone
    # block doubles per round-trip (gen + eval each get a copy).
    tone = cap_tone_snapshot(tone_snapshot) or "(default casual-direct tone)"
    # NOTE: do NOT inline ``"=" * 60`` between adjacent string literals here.
    # Python concatenates all adjacent literals (including f-strings) into
    # one big string at parse time, then ``* 60`` multiplies that whole
    # thing. Versions v4.2 and v4.3 had ``"=" * 60 + "\n\n"`` mid-chain,
    # which silently shipped 60 copies of the BACKGROUND CONTEXT block on
    # every eval call (~63K input tokens instead of ~1.5K). Keep the
    # separator as an f-string interpolation so the operator stays scoped.
    separator = "=" * 60
    return (
        "BACKGROUND CONTEXT (do NOT score these; they are only here so you\n"
        "can judge whether the draft uses them well):\n\n"
        f"Tone guide:\n{tone}\n\n"
        f"Campaign goal: {campaign_goal}\n\n"
        f"Recipient category: {lead_category or 'unknown'}\n\n"
        f"Personalisation angle (background — the draft author saw this):\n{angle}\n\n"
        f"{separator}\n\n"
        f"THE DRAFT TO SCORE ({len(draft)} chars) — score THIS and nothing else.\n"
        "Any criticism in `feedback` MUST quote text that is literally present\n"
        "in the draft below. If you cannot copy-paste the phrase from here,\n"
        "do not mention it.\n\n"
        f"{draft}"
    )


def compute_overall(scores: dict) -> float:
    """Weighted average of criterion scores.

    Defensive: missing criteria contribute 0. Returns a float in ``[0, 1]``.
    """

    total = 0.0
    for key, weight in SCORING_WEIGHTS.items():
        raw = float(scores.get(key, 0.0) or 0.0)
        raw = max(0.0, min(1.0, raw))
        total += raw * weight
    return round(total, 4)


def evaluate_result(
    raw: dict,
    *,
    draft: str,
    threshold: float = DEFAULT_THRESHOLD,
) -> dict:
    """Normalise the LLM's evaluation output into a canonical shape.

    The wrapper applies three defences:

    - Length validity is recomputed from the draft rather than trusting the LLM.
    - Overall is recomputed as a weighted average of the component scores.
    - The pass flag is derived (overall >= threshold AND length_valid == 1.0).
    """

    scores = dict(raw.get("scores") or {})
    scores["length_valid"] = 1.0 if len(draft) <= MAX_SMS_LENGTH else 0.0

    overall = compute_overall(scores)
    passed = overall >= threshold and scores["length_valid"] == 1.0

    feedback = raw.get("feedback") or ""
    if not passed and not feedback:
        # Produce a reasonable default if the LLM didn't provide feedback.
        weakest_key = min(scores, key=lambda k: scores.get(k, 1.0))
        feedback = f"Weakest criterion: {weakest_key} (score {scores[weakest_key]:.2f})."

    return {
        "scores": scores,
        "overall": overall,
        "pass": bool(passed),
        "feedback": feedback,
    }
