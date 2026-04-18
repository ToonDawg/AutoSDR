"""Self-evaluation prompt — scores a drafted outreach before sending."""

from __future__ import annotations

PROMPT_VERSION = "evaluation-v1"

SCORING_WEIGHTS = {
    "tone_match": 0.25,
    "personalisation": 0.30,
    "goal_alignment": 0.20,
    "length_valid": 0.15,
    "naturalness": 0.10,
}

DEFAULT_THRESHOLD = 0.85
MAX_SMS_LENGTH = 160


def build_system_prompt() -> str:
    return """\
You are evaluating a drafted outreach SMS message before it is sent.

Score the message on each criterion from 0.0 to 1.0:

1. tone_match: Does the message match the tone guide exactly?
2. personalisation: Does the message reference something specific to this
   recipient rather than feeling like a template?
3. goal_alignment: Is the message clearly oriented toward the campaign goal?
4. length_valid: Is the message 160 characters or fewer?
   (1.0 if yes, 0.0 if no — no partial credit.)
5. naturalness: Would a real person send this? Does it avoid robotic phrasing,
   excessive formality, or hollow superlatives?

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
  "feedback": "one sentence explaining the biggest weakness if pass is false; empty string otherwise"
}
"""


def build_user_prompt(
    *,
    tone_snapshot: str | None,
    campaign_goal: str,
    angle: str,
    draft: str,
) -> str:
    tone = tone_snapshot or "(default casual-direct tone)"
    return (
        "Tone guide:\n"
        f"{tone}\n\n"
        f"Campaign goal: {campaign_goal}\n\n"
        "Personalisation angle:\n"
        f"{angle}\n\n"
        f"Drafted message ({len(draft)} chars):\n"
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
