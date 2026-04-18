"""Lead analysis prompt — extracts the personalisation angle from raw_data."""

from __future__ import annotations

import json
from typing import Any

PROMPT_VERSION = "analysis-v1"

SYSTEM_PROMPT = """\
You are analysing a business lead to find the single strongest personalisation
angle for a cold outreach message.

Review the lead data below and identify one specific, concrete detail that:
1. Suggests a pain point, opportunity, or context relevant to the sender's offering.
2. Would make the recipient think "they actually looked at us".
3. Can be referenced naturally in a short SMS message.

Prefer concrete signals over generic ones: a specific review complaint beats
"they have good ratings"; a named service beats "they're in aged care". If the
available data is too thin to support a specific angle, fall back to the lead's
category and location.

Return a single JSON object. Nothing else.

Schema:
{
  "angle":  "2-3 sentences describing the hook and why it is relevant",
  "signal": "the specific data point from the lead that supports this angle",
  "confidence": 0.0-1.0  // how strong the signal is
}
"""


def _truncate_raw_data(raw_data: dict[str, Any], max_bytes: int) -> tuple[dict, bool]:
    """Shrink a raw_data blob to fit within the size limit.

    Strategy: longest string values are truncated first. We keep all keys so
    the structure is obvious; only values are shortened. Returns the new blob
    and a flag indicating whether truncation occurred.
    """

    encoded = json.dumps(raw_data, ensure_ascii=False)
    if len(encoded.encode("utf-8")) <= max_bytes:
        return raw_data, False

    clone = json.loads(encoded)  # deep copy
    # Collect (path, value) for every string value so we can target the longest.
    string_refs: list[tuple[list[Any], Any]] = []

    def _walk(node: Any, path: list[Any]) -> None:
        if isinstance(node, dict):
            for k, v in node.items():
                _walk(v, path + [k])
        elif isinstance(node, list):
            for idx, v in enumerate(node):
                _walk(v, path + [idx])
        elif isinstance(node, str):
            string_refs.append((path, node))

    _walk(clone, [])
    string_refs.sort(key=lambda r: -len(r[1]))

    def _set_at(root: Any, path: list[Any], value: Any) -> None:
        cursor: Any = root
        for segment in path[:-1]:
            cursor = cursor[segment]
        cursor[path[-1]] = value

    for path, value in string_refs:
        _set_at(clone, path, value[:200] + "…[truncated]" if len(value) > 200 else value)
        if len(json.dumps(clone, ensure_ascii=False).encode("utf-8")) <= max_bytes:
            break

    # Final fallback: drop the longest array entries if still too big.
    while len(json.dumps(clone, ensure_ascii=False).encode("utf-8")) > max_bytes:
        longest_list_path: list[Any] | None = None
        longest_list_len = -1

        def _find_longest(node: Any, path: list[Any]) -> None:
            nonlocal longest_list_path, longest_list_len
            if isinstance(node, dict):
                for k, v in node.items():
                    _find_longest(v, path + [k])
            elif isinstance(node, list):
                if len(node) > longest_list_len:
                    longest_list_len = len(node)
                    longest_list_path = path
                for idx, v in enumerate(node):
                    _find_longest(v, path + [idx])

        _find_longest(clone, [])
        if longest_list_path is None or longest_list_len <= 1:
            break
        cursor: Any = clone
        for segment in longest_list_path[:-1] if longest_list_path else []:
            cursor = cursor[segment]
        if longest_list_path:
            tail_key = longest_list_path[-1] if longest_list_path else None
            target = cursor[tail_key] if tail_key is not None else cursor
            # Keep the first half, drop the rest.
            del target[len(target) // 2 :]

    return clone, True


def build_user_prompt(
    *,
    business_data: dict[str, Any] | str,
    business_dump: str,
    campaign_goal: str,
    lead_name: str | None,
    lead_category: str | None,
    lead_address: str | None,
    raw_data: dict[str, Any],
    raw_data_size_limit_kb: int,
) -> tuple[str, bool]:
    """Render the user prompt. Returns (prompt, raw_data_truncated)."""

    truncated_data, truncated = _truncate_raw_data(
        raw_data, max_bytes=raw_data_size_limit_kb * 1024
    )

    business_block = (
        json.dumps(business_data, indent=2, ensure_ascii=False)
        if isinstance(business_data, dict) and business_data
        else business_dump
    )

    lead_block = {
        "name": lead_name,
        "category": lead_category,
        "address": lead_address,
        "raw_data": truncated_data,
    }

    user = (
        "The sender's business:\n"
        f"{business_block}\n\n"
        f"Outreach goal: {campaign_goal}\n\n"
        "Lead:\n"
        f"{json.dumps(lead_block, indent=2, ensure_ascii=False)}"
    )
    return user, truncated
