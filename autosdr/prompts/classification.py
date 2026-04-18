"""Intent classification prompt — labels an incoming reply."""

from __future__ import annotations

PROMPT_VERSION = "classification-v1"

INTENTS = {
    "positive",
    "objection",
    "question",
    "negative",
    "unclear",
    "bot_check",
    "goal_achieved",
    "human_requested",
}


def build_system_prompt() -> str:
    return """\
You are classifying an incoming SMS reply to determine how an autonomous
outreach system should respond.

Possible intents:
  - "positive"        — interested, asking for more info, open to the goal
  - "objection"       — pushback, "not right now", "too busy"
  - "question"        — specific question that needs a factual answer
  - "negative"        — clear no, unsubscribe, stop, "remove me"
  - "unclear"         — cannot determine intent with confidence
  - "bot_check"       — asking if this is automated or a bot
  - "goal_achieved"   — lead has agreed to the campaign goal
  - "human_requested" — lead explicitly asked to speak to a person

Return a JSON object (and nothing else):
{
  "intent": "<one of the labels above>",
  "requires_human": true,
  "confidence": 0.0,
  "reason": "one sentence explaining the classification"
}

Rules for `requires_human`:
- true when intent is "bot_check", "human_requested", or "goal_achieved"
- true when intent is "question" and confidence < 0.85
- true when intent is "unclear"
- true when confidence < 0.80 for any intent
- false otherwise
"""


def _format_history(history: list[dict[str, str]], limit: int = 5) -> str:
    recent = history[-limit:] if len(history) > limit else history
    if not recent:
        return "(no prior messages)"
    lines = []
    for m in recent:
        role = m.get("role", "?")
        content = m.get("content", "")
        lines.append(f"[{role}] {content}")
    return "\n".join(lines)


def build_user_prompt(
    *,
    campaign_goal: str,
    history: list[dict[str, str]],
    incoming_message: str,
) -> str:
    return (
        f"Campaign goal: {campaign_goal}\n\n"
        f"Recent conversation:\n{_format_history(history)}\n\n"
        f'Incoming message: "{incoming_message}"'
    )


def derive_requires_human(
    *,
    intent: str,
    confidence: float,
    model_output_says: bool | None = None,
) -> bool:
    """Compute ``requires_human`` from the rules, ignoring what the model says.

    We do not trust the model's own ``requires_human`` field because the rules
    are deterministic — apply them in code so a prompt drift cannot bypass
    escalation.
    """

    if intent in {"bot_check", "human_requested", "goal_achieved"}:
        return True
    if intent == "unclear":
        return True
    if intent == "question" and confidence < 0.85:
        return True
    if confidence < 0.80:
        return True
    return False


def normalise_classification(raw: dict) -> dict:
    """Clamp confidence to [0,1] and validate the intent label."""

    intent = str(raw.get("intent", "unclear"))
    if intent not in INTENTS:
        intent = "unclear"
    try:
        confidence = float(raw.get("confidence", 0.0))
    except (TypeError, ValueError):
        confidence = 0.0
    confidence = max(0.0, min(1.0, confidence))
    reason = str(raw.get("reason") or "")
    requires_human = derive_requires_human(intent=intent, confidence=confidence)
    return {
        "intent": intent,
        "confidence": confidence,
        "reason": reason,
        "requires_human": requires_human,
    }
