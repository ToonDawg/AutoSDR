"""Message generation prompt — drafts an outreach SMS."""

from __future__ import annotations

import json
from typing import Any

PROMPT_VERSION = "generation-v1"


def build_system_prompt(tone_snapshot: str | None) -> str:
    tone_block = tone_snapshot.strip() if tone_snapshot else (
        "Write in a casual, direct tone. Keep sentences short. Avoid corporate "
        "language. Open with a grounded observation about the recipient's "
        "situation; close with a single low-pressure question."
    )
    return f"""\
{tone_block}

You are writing a short outreach SMS on behalf of a small business owner.

Requirements for the message you produce:
- Maximum 160 characters (one SMS).
- Feels personal, not templated.
- References or is informed by the personalisation angle provided.
- Includes a soft call to action aligned with the campaign goal.
- Matches the tone described above exactly.
- Does NOT introduce yourself by full name or company name in the first message.
- Output ONLY the message text. No quotes, no labels, no explanation, no preamble.
"""


def build_user_prompt(
    *,
    business_data: dict[str, Any] | str,
    business_dump: str,
    campaign_goal: str,
    angle: str,
    lead_name: str | None,
    lead_category: str | None,
    lead_address: str | None,
    previous_feedback: str | None = None,
    message_history: list[dict[str, str]] | None = None,
) -> str:
    business_block = (
        json.dumps(business_data, indent=2, ensure_ascii=False)
        if isinstance(business_data, dict) and business_data
        else business_dump
    )

    parts: list[str] = [
        "About the sender's business:",
        business_block,
        "",
        f"Campaign goal: {campaign_goal}",
        "",
        "Personalisation angle for this recipient:",
        angle,
        "",
        "Recipient:",
        f"- Name: {lead_name or 'unknown'}",
        f"- Category: {lead_category or 'unknown'}",
        f"- Location: {lead_address or 'unknown'}",
    ]

    if message_history:
        parts.extend(
            [
                "",
                "Conversation so far (most recent last):",
            ]
        )
        for m in message_history:
            role = m.get("role", "?")
            content = m.get("content", "")
            parts.append(f"[{role}] {content}")
        parts.extend(
            [
                "",
                "This is a follow-up reply, not a first-contact message. Address "
                "what the lead just said directly. Stay oriented toward the "
                "campaign goal without being pushy. 160 character maximum.",
            ]
        )

    if previous_feedback:
        parts.extend(
            [
                "",
                "Previous attempt failed evaluation. Feedback:",
                previous_feedback,
                "Rewrite the message addressing this weakness.",
            ]
        )

    return "\n".join(parts)
