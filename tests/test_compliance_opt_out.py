"""Deterministic opt-out matcher — fixture-driven coverage of Spam Act / TCPA cases.

The matcher must:

- catch the literal-keyword case in any case / any surrounding noise,
- NOT fire on third-party / referential phrases,
- NOT fire on the empty string or non-keyword negatives,
- return the *canonical* (UPPERCASE) keyword for audit & UI surfacing.
"""

from __future__ import annotations

import pytest

from autosdr.compliance import DEFAULT_KEYWORDS, match_opt_out


# ---------------------------------------------------------------------------
# Positive cases — every one of these must match, with the expected keyword.
# ---------------------------------------------------------------------------

POSITIVE_CASES: list[tuple[str, str]] = [
    # 1-9: each default keyword on its own.
    ("STOP", "STOP"),
    ("stop", "STOP"),
    ("Stop.", "STOP"),
    ("UNSUBSCRIBE", "UNSUBSCRIBE"),
    ("Please unsubscribe me.", "UNSUBSCRIBE"),
    ("UNSUB", "UNSUB"),
    ("unsub from this list", "UNSUB"),
    ("REMOVE ME", "REMOVE ME"),
    ("remove me from your list", "REMOVE ME"),
    # 10-15: more keywords + variants.
    ("OPT OUT", "OPT OUT"),
    ("I'd like to opt out, thanks.", "OPT OUT"),
    ("CANCEL", "CANCEL"),
    ("Please cancel my subscription.", "CANCEL"),
    ("END", "END"),
    ("end please", "END"),
    # 16-19: QUIT + STOP ALL.
    ("QUIT", "QUIT"),
    ("quit messaging me", "QUIT"),
    ("STOP ALL", "STOP ALL"),
    ("stop all of these messages", "STOP ALL"),
    # 20-23: whitespace, punctuation, casing noise around the keyword.
    ("    STOP    ", "STOP"),
    ("STOP!!!", "STOP"),
    ("...stop...", "STOP"),
    ("'STOP'", "STOP"),
    # 24-26: success-criterion fixture — surrounding sentence noise.
    ("please STOP, this is annoying", "STOP"),
    ("Yeah no — STOP texting me, mate", "STOP"),
    ("dude, just STOP. seriously.", "STOP"),
    # 27-30: multi-word + punctuation between tokens.
    ("REMOVE   ME", "REMOVE ME"),
    ("opt   out please", "OPT OUT"),
    ("stop  all", "STOP ALL"),
    ("Hi, please remove me from this campaign", "REMOVE ME"),
    # 31-32: keyword embedded in longer sentence (still word-boundary).
    ("I am asking you to UNSUBSCRIBE me right now.", "UNSUBSCRIBE"),
    ("If this is automated please CANCEL going forward", "CANCEL"),
]


@pytest.mark.parametrize("text,expected_keyword", POSITIVE_CASES)
def test_positive_match(text: str, expected_keyword: str) -> None:
    result = match_opt_out(text)
    assert result is not None, f"expected an opt-out match for: {text!r}"
    assert result.keyword == expected_keyword
    assert result.reason == f"opt_out:{expected_keyword}"


def test_positive_count_is_at_least_thirty() -> None:
    """Success criterion explicitly says ≥ 30 fixture inbounds."""

    assert len(POSITIVE_CASES) >= 30


# ---------------------------------------------------------------------------
# Negative cases — these must NOT match, by design.
# ---------------------------------------------------------------------------

NEGATIVE_CASES: list[str] = [
    "",
    "   ",
    "Sounds good — let's chat tomorrow.",
    "no thanks, busy this week",  # bare "no" is excluded from defaults
    "NO",  # bare NO excluded
    "I'd love to hear more about pricing.",
    "stopover in Sydney next week",  # 'stop' is not a separate token
    "I run a non-stop schedule.",  # 'stop' inside another token
    "endless meetings today",  # 'end' inside another token
    "stopping by next week",  # 'stop' inside 'stopping' — no boundary
    "STOP texting them, not me",  # third-party denylist
    "stop calling her, please",  # third-party denylist
    "tell them to stop spamming us",  # third-party denylist
    "they should stop sending these",  # third-party denylist
    "ask him to stop messaging us",  # third-party denylist
]


@pytest.mark.parametrize("text", NEGATIVE_CASES)
def test_negative_no_match(text: str) -> None:
    assert match_opt_out(text) is None, f"unexpected opt-out match for: {text!r}"


# ---------------------------------------------------------------------------
# Behavioural tests — public surface.
# ---------------------------------------------------------------------------


def test_default_keyword_set_excludes_bare_no() -> None:
    assert "NO" not in DEFAULT_KEYWORDS


def test_match_returns_canonical_uppercase_keyword() -> None:
    """The reason field is what lands in ``Lead.do_not_contact_reason``."""

    result = match_opt_out("please unsubscribe me thanks")
    assert result is not None
    assert result.keyword == "UNSUBSCRIBE"
    assert result.reason == "opt_out:UNSUBSCRIBE"


def test_stop_all_wins_over_stop_when_both_present() -> None:
    """Longest-first matching: 'STOP ALL' is more specific than 'STOP'."""

    result = match_opt_out("stop all messages please")
    assert result is not None
    assert result.keyword == "STOP ALL"


def test_caller_provided_keyword_set_overrides_defaults() -> None:
    """Per-jurisdiction / Settings override path."""

    result = match_opt_out("désinscrire", keywords=("DÉSINSCRIRE",))
    assert result is not None
    assert result.keyword == "DÉSINSCRIRE"


def test_none_input_is_safe() -> None:
    assert match_opt_out(None) is None
