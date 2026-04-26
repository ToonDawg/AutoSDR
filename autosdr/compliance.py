"""Deterministic compliance helpers — opt-out keyword detection.

Spam Act 2003 (AU) and TCPA (US) require unsubscribe requests to be honoured
without an LLM in the loop. This module is the deterministic shortcut: if a
lead's reply matches one of the opt-out keywords, we mark the lead
do-not-contact and close the thread *before* the classifier runs.

The matcher is a pure function — no I/O, no model state, no globals — so it
is trivially testable and trivially auditable. See
``tests/test_compliance_opt_out.py``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# Default keyword set. Bare ``NO`` is intentionally excluded — too many
# legitimate negative-but-not-opt-out replies ("no thanks, busy this week").
# Operators get an "Opted out" badge they can clear if a real false positive
# slips through.
#
# Each keyword is matched on a word boundary, case-insensitive. Multi-word
# phrases (e.g. ``REMOVE ME``) tolerate any whitespace between the tokens.
DEFAULT_KEYWORDS: tuple[str, ...] = (
    "STOP ALL",
    "STOP",
    "UNSUBSCRIBE",
    "UNSUB",
    "REMOVE ME",
    "OPT OUT",
    "CANCEL",
    "END",
    "QUIT",
)

# Third-party / referential phrases that contain an opt-out keyword but are
# *not* the lead asking us to stop messaging them. The classifier still sees
# these via the normal path; this list only suppresses the deterministic
# shortcut so we don't false-positive a lead who is venting about somebody
# else.
#
# Patterns are matched case-insensitively against the full message body.
_THIRD_PARTY_DENYLIST: tuple[re.Pattern[str], ...] = (
    # "STOP texting them" / "stop calling her" / "STOP messaging us"
    re.compile(
        r"\bstop\s+\w+ing\s+(?:them|him|her|us|those|these|all\s+of\s+them)\b",
        re.IGNORECASE,
    ),
    # "tell them to stop" / "ask him to stop"
    re.compile(
        r"\b(?:tell|ask|get)\s+(?:them|him|her|us|those|these)\s+to\s+stop\b",
        re.IGNORECASE,
    ),
    # "they should stop" / "she should stop"
    re.compile(
        r"\b(?:they|he|she|those|these)\s+should\s+stop\b",
        re.IGNORECASE,
    ),
)


@dataclass(frozen=True, slots=True)
class OptOutMatch:
    """Result of a successful opt-out match."""

    keyword: str
    """The canonical (UPPERCASE) keyword that matched."""

    reason: str
    """Stable string for ``Lead.do_not_contact_reason`` — ``opt_out:<keyword>``."""


def _compile_keyword_pattern(keywords: tuple[str, ...]) -> re.Pattern[str]:
    """Build a single ``\\b(KW1|KW2|...)\\b`` regex, longest-first.

    Longest-first ordering matters: ``STOP ALL`` must be tried before ``STOP``
    so the matched canonical keyword is the more specific one when both apply.
    """

    ordered = sorted(keywords, key=len, reverse=True)
    parts = [r"\s+".join(re.escape(token) for token in kw.split()) for kw in ordered]
    # Use look-around boundaries that treat hyphenated compounds as one token:
    # ``non-stop`` should not be parsed as a bare ``stop`` (compliance bias is
    # still "false-positive-ok", but an obvious compound shouldn't trigger).
    return re.compile(r"(?<![A-Za-z0-9-])(" + "|".join(parts) + r")(?![A-Za-z0-9-])", re.IGNORECASE)


_DEFAULT_PATTERN = _compile_keyword_pattern(DEFAULT_KEYWORDS)


def match_opt_out(
    text: str | None,
    *,
    keywords: tuple[str, ...] | None = None,
    locale: str | None = None,  # reserved for future jurisdiction overrides
) -> OptOutMatch | None:
    """Return an :class:`OptOutMatch` when ``text`` is a deterministic opt-out.

    Pure function. Returns ``None`` when:

    - the text is empty / whitespace,
    - no keyword matches on a word boundary,
    - the message *also* matches a third-party denylist pattern
      (``"stop texting them"`` etc.).

    The ``locale`` argument is currently unused; it exists so a future
    jurisdiction-specific keyword set can plug in without breaking call sites.
    """

    del locale  # reserved

    if not text:
        return None
    body = text.strip()
    if not body:
        return None

    pattern = _DEFAULT_PATTERN if keywords is None else _compile_keyword_pattern(keywords)
    hit = pattern.search(body)
    if hit is None:
        return None

    for denylist_pattern in _THIRD_PARTY_DENYLIST:
        if denylist_pattern.search(body):
            return None

    keyword = " ".join(hit.group(1).split()).upper()
    return OptOutMatch(keyword=keyword, reason=f"opt_out:{keyword}")


__all__ = ["DEFAULT_KEYWORDS", "OptOutMatch", "match_opt_out"]
