"""Tone-snapshot capping — shared between ``generation`` and ``evaluation``.

The workspace tone block is injected verbatim into:

- ``generation.build_system_prompt(tone_snapshot=...)`` — drives the gen
  call's voice on every outreach attempt.
- ``evaluation.build_user_prompt(tone_snapshot=...)`` — gives the eval
  the same voice spec to score the draft against.

Without a cap the block grows unboundedly as the operator iterates on
their tone settings; the audit at ``docs/prompt-audit-2026-05-02.md``
measured a typical 3,276-char block, repeated in both prompts (so 6,552
chars of tone alone per generate-then-evaluate round-trip).

This module exposes :func:`cap_tone_snapshot`, the single place that
applies the budget. Both prompt builders call it before injection so the
cap is enforced once and is easy to audit. The cap value is documented
on :data:`MAX_TONE_CHARS`.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


# The cap. 1,500 chars is large enough for an opinionated one-paragraph
# voice intro plus 5–7 bullet points; more than that is repetition (the
# audit found voice + avoid sections re-state each other). Operators who
# need more context should encode it in the angle / campaign goal, not
# the global tone block.
MAX_TONE_CHARS = 1500


# A short trailing paragraph (e.g. "Length: aim for ~200-240 chars...") is
# typically a terminal rule that downstream code-side checks rely on. The
# cap preserves it when it fits within this fraction of the budget.
_TAIL_RESERVE_CHARS = 300


def cap_tone_snapshot(
    tone: str | None,
    *,
    max_chars: int = MAX_TONE_CHARS,
) -> str | None:
    """Return ``tone`` cut to at most ``max_chars`` while preserving terminal rules.

    Behaviour:

    - ``None`` / empty in -> returned as-is (no allocation).
    - Already under the cap -> returned unchanged after a ``strip()``.
    - Over the cap -> the head of the tone is trimmed on a paragraph
      boundary, but if the FINAL paragraph is short (<= ``_TAIL_RESERVE_CHARS``)
      it is always preserved verbatim. This protects terminal rules that
      gate downstream behaviour — e.g. a "Length: aim for ~200-240 chars"
      block at the end of the workspace tone settings — which would
      otherwise be dropped silently and produce oversized drafts.
    - A ``[...truncated to fit tone budget]`` marker marks the cut so the
      LLM can tell content was dropped, and so a reader eyeballing the
      rendered prompt can spot the cap firing.

    Logs a warning the first time a particular cap is applied per process
    so operators are alerted that their tone settings are over budget
    without spamming the logs on every subsequent call.
    """

    if not tone:
        return tone

    text = tone.strip()
    if len(text) <= max_chars:
        return text

    # Look for a short trailing paragraph (separated by a blank line) that
    # holds terminal rules. Reserve room for it.
    last_split = text.rfind("\n\n")
    tail = text[last_split + 2 :].strip() if last_split != -1 else ""
    has_reserve = bool(tail) and len(tail) <= _TAIL_RESERVE_CHARS
    head_budget = (
        max_chars - len(tail) - len("\n\n[...truncated to fit tone budget]\n\n")
        if has_reserve
        else max_chars
    )
    if head_budget < max_chars // 3:
        # Tail too greedy — don't reserve, just hard-cap from the front.
        has_reserve = False
        head_budget = max_chars

    head_text = text[:last_split] if has_reserve else text
    minimum_kept = max(1, head_budget // 2)
    cut = head_text.rfind("\n\n", 0, head_budget)
    if cut < minimum_kept:
        cut = head_text.rfind("\n", 0, head_budget)
    if cut < minimum_kept:
        cut = head_budget

    head = head_text[:cut].rstrip()
    capped = head + "\n\n[...truncated to fit tone budget]"
    if has_reserve:
        capped = capped + "\n\n" + tail
    _log_cap_once(original_len=len(text), capped_len=len(capped), cap=max_chars)
    return capped


_seen_caps: set[int] = set()


def _log_cap_once(*, original_len: int, capped_len: int, cap: int) -> None:
    """Avoid log spam — warn once per (original_len, cap) pair per process."""

    key = (original_len, cap).__hash__()
    if key in _seen_caps:
        return
    _seen_caps.add(key)
    logger.warning(
        "tone_snapshot capped: %d chars -> %d chars (budget %d). "
        "Trim the workspace tone settings to keep prompts predictable.",
        original_len,
        capped_len,
        cap,
    )


__all__ = ["MAX_TONE_CHARS", "cap_tone_snapshot"]
