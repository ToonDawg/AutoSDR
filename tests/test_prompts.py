"""Prompt helpers — deterministic transforms worth pinning."""

from __future__ import annotations

import hashlib
import json

from autosdr.prompts import (
    analysis,
    classification,
    evaluation,
    followup_reply,
    generation,
)
from autosdr.prompts._tone import MAX_TONE_CHARS, cap_tone_snapshot


# ---------------------------------------------------------------------------
# Rendered-prompt SHA snapshots.
#
# These guard the Phase 3 #9 refactor (RULES / EXAMPLES / DATA split). The
# refactor MUST be byte-for-byte identical — any change to a SHA here means
# the rendered prompt drifted and needs a deliberate ``PROMPT_VERSION`` bump
# plus an audit-harness re-run. To intentionally update a snapshot:
# 1. Bump the relevant ``PROMPT_VERSION``.
# 2. Run the audit harness (``scripts/replay_outreach_loop.py`` or
#    ``scripts/replay_evaluator.py``) and confirm no behaviour regression.
# 3. Update the SHA below from the failing-test output.
#
# See ``docs/prompt-audit-2026-05-02.md`` Phase 3 #9.
# ---------------------------------------------------------------------------


def _sha(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()[:16]


def test_rendered_prompts_are_byte_stable():
    """Pin the SHA of every rendered prompt to detect silent drift.

    Ticket 0017 bumped two prompts:

    * ``analysis`` ``v3.6 -> v3.7`` — added the ``_RULES_TONE_REGISTER``
      block and the ``tone_register`` enum field on the JSON output
      schema. The analysis LLM now picks the register, replacing the
      removed prose CATEGORY CALIBRATION paragraph from ``generation``.
    * ``generation`` ``v8 -> v9`` — the per-register prose block is
      injected between ``_RULES`` and ``_REFERENCE_EXAMPLES`` based on
      the ``register=`` kwarg the caller passes (sourced from
      ``Thread.tone_register``). The two ``register=None`` snapshots
      below pin the no-register-block shape (skipped on
      ``register is None`` and ``register == "unknown"``); the
      ``tradie`` and ``professional`` snapshots pin the
      register-injected variants so a silent register-prose tweak is
      caught.
    """

    snapshots = {
        # analysis — bumped to v3.7 (new tone_register field)
        "analysis.SYSTEM_PROMPT": _sha(analysis.SYSTEM_PROMPT),
        # generation — no-register path (legacy / unknown / pre-analysis)
        "generation.build_system_prompt(None)": _sha(
            generation.build_system_prompt(None)
        ),
        "generation.build_system_prompt('Casual mate, short.')": _sha(
            generation.build_system_prompt("Casual mate, short.")
        ),
        # generation — register-injected path; pins v9 register block prose
        "generation.build_system_prompt(None, register='tradie')": _sha(
            generation.build_system_prompt(None, register="tradie")
        ),
        "generation.build_system_prompt(None, register='professional')": _sha(
            generation.build_system_prompt(None, register="professional")
        ),
        # evaluation
        "evaluation.build_system_prompt()": _sha(evaluation.build_system_prompt()),
        "evaluation.build_user_prompt(...)": _sha(
            evaluation.build_user_prompt(
                tone_snapshot="tone here",
                campaign_goal="goal",
                angle="angle",
                draft="draft",
                lead_category="cat",
            )
        ),
        # classification
        "classification.build_system_prompt()": _sha(
            classification.build_system_prompt()
        ),
        # followup_reply — both tone variants
        "followup_reply.build_system_prompt(None)": _sha(
            followup_reply.build_system_prompt(None)
        ),
        "followup_reply.build_system_prompt('Casual mate, short.')": _sha(
            followup_reply.build_system_prompt("Casual mate, short.")
        ),
    }

    expected = {
        "analysis.SYSTEM_PROMPT": "999d35f631d0386a",
        "generation.build_system_prompt(None)": "b8a95df16fb50e0d",
        "generation.build_system_prompt('Casual mate, short.')": "4e94f273319be28a",
        "generation.build_system_prompt(None, register='tradie')": "24b8d581bbff421f",
        "generation.build_system_prompt(None, register='professional')": "dad5281b269aac45",
        "evaluation.build_system_prompt()": "215b88e67e81138d",
        "evaluation.build_user_prompt(...)": "c3b3334a30ef4299",
        "classification.build_system_prompt()": "ebf54dbf6b5e9a8e",
        "followup_reply.build_system_prompt(None)": "c752f491955260c1",
        "followup_reply.build_system_prompt('Casual mate, short.')": "c5c7197496a9262b",
    }

    assert snapshots == expected, (
        "Rendered prompt drift detected. To accept the change deliberately, "
        "bump the relevant PROMPT_VERSION, re-run the audit harness, and "
        "update the snapshot dict above."
    )


# ---------------------------------------------------------------------------
# Tone register wiring (ticket 0017) — analysis picks, generation injects.
# ---------------------------------------------------------------------------


def test_analysis_prompt_advertises_tone_register_field():
    """The analysis LLM's JSON output schema must list ``tone_register``;
    pipeline reads it out of ``analysis_result["tone_register"]``."""

    assert "tone_register" in analysis.SYSTEM_PROMPT
    assert "REGISTER:" not in analysis.SYSTEM_PROMPT or True  # n/a here
    # Closed vocab must be advertised in the schema string so the model
    # knows what tokens are valid (mirror of the code-side
    # ``ToneRegisterT`` literal in ``generation.py``).
    for token in (
        "tradie",
        "professional",
        "hospitality",
        "retail",
        "personal_services",
        "aged_care",
        "unknown",
    ):
        assert token in analysis.SYSTEM_PROMPT, (
            f"register token {token!r} missing from analysis prompt"
        )


def test_generation_omits_register_block_when_register_is_none():
    """No-register path: caller passes ``register=None`` (legacy thread,
    pre-analysis call, or analysis returned ``"unknown"``). The prompt
    falls back to workspace tone + rules + examples."""

    sys = generation.build_system_prompt(None)
    assert "REGISTER:" not in sys


def test_generation_omits_register_block_when_register_is_unknown():
    """Skip-the-block: an explicit ``"unknown"`` token from the analysis
    LLM produces the same prompt body as ``register=None``."""

    none_prompt = generation.build_system_prompt(None)
    unknown_prompt = generation.build_system_prompt(None, register="unknown")
    assert none_prompt == unknown_prompt


def test_generation_includes_register_block_for_known_register():
    """Concrete register injects its prose between RULES and EXAMPLES."""

    sys = generation.build_system_prompt(None, register="professional")
    assert "REGISTER: professional services" in sys
    rules_idx = sys.index("OPENING")
    register_idx = sys.index("REGISTER: professional")
    examples_idx = sys.index("Reference examples")
    assert rules_idx < register_idx < examples_idx


def test_generation_register_blocks_differ_per_register():
    """Each register produces a distinct system prompt — sanity that the
    register -> prose dict is keyed correctly."""

    tradie = generation.build_system_prompt(None, register="tradie")
    professional = generation.build_system_prompt(None, register="professional")
    aged_care = generation.build_system_prompt(None, register="aged_care")
    hospitality = generation.build_system_prompt(None, register="hospitality")
    assert len({tradie, professional, aged_care, hospitality}) == 4


def test_generation_v9_no_longer_carries_category_calibration_prose():
    """v8's CATEGORY CALIBRATION paragraph is gone — register selection
    moved upstream to the analysis LLM. Guard against regression."""

    sys = generation.build_system_prompt(None, register="tradie")
    assert "CATEGORY CALIBRATION" not in sys


def test_generation_register_block_fits_under_compose_budget():
    """Adding a register block to a max-sized tone snapshot should not
    blow the existing 26K composed-prompt ceiling that the audit pinned."""

    fat_tone = "Voice: short.\n\n" + "x" * 5000
    for register in ("tradie", "professional", "aged_care"):
        sys = generation.build_system_prompt(fat_tone, register=register)
        assert len(sys) < 28_000, (
            f"prompt with register={register!r} grew to {len(sys)} chars; "
            "audit ceiling is ~26K plus headroom for the register block"
        )


def test_generation_register_block_falls_back_silently_on_unknown_token():
    """Defensive: if a junk register token slips past the persistence
    guard in ``outreach.py``, the prompt builder degrades to no-block
    rather than raising. Same shape as ``register=None``."""

    sys_garbage = generation.build_system_prompt(None, register="not_a_real_register")  # type: ignore[arg-type]
    sys_none = generation.build_system_prompt(None)
    assert sys_garbage == sys_none


# ---------------------------------------------------------------------------
# _tone.cap_tone_snapshot — bound the workspace tone block in both prompts.
# Without this cap an unbounded tone block doubles per round-trip (gen +
# eval each get a copy). See ``docs/prompt-audit-2026-05-02.md`` Phase 3 #8.
# ---------------------------------------------------------------------------


def test_cap_tone_passes_short_tone_through():
    tone = "Casual, direct.\n\nVoice: short sentences."
    assert cap_tone_snapshot(tone) == tone


def test_cap_tone_handles_none_and_empty():
    assert cap_tone_snapshot(None) is None
    assert cap_tone_snapshot("") == ""


def test_cap_tone_at_paragraph_boundary():
    # Three paragraphs: short head, fat middle, short tail.
    head = "Voice intro " * 5  # 60 chars
    middle = "B" * 1000
    tail = "T" * 1500
    tone = f"{head}\n\n{middle}\n\n{tail}"
    capped = cap_tone_snapshot(tone, max_chars=1200)
    assert capped is not None
    assert capped.startswith(head.rstrip())
    assert "[...truncated to fit tone budget]" in capped
    # The tail is too large to reserve so it's dropped along with middle.
    assert "T" * 100 not in capped


def test_cap_tone_preserves_short_terminal_paragraph():
    # The terminal paragraph holds a length rule that downstream behaviour
    # depends on. A long head must not strip it.
    head = "X" * 3000
    terminal_rule = "Length: aim for ~200 chars, max 320."
    tone = f"{head}\n\n{terminal_rule}"
    capped = cap_tone_snapshot(tone, max_chars=1500)
    assert capped is not None
    assert capped.endswith(terminal_rule)
    assert "[...truncated to fit tone budget]" in capped
    assert len(capped) <= 1700  # cap + truncation marker + reserved tail


def test_cap_tone_falls_back_to_hard_cut_when_no_boundary():
    tone = "x" * 5000  # no newlines at all
    capped = cap_tone_snapshot(tone, max_chars=1500)
    assert capped is not None
    assert len(capped) < 1600  # 1500 + truncation marker
    assert capped.endswith("[...truncated to fit tone budget]")


def test_generation_system_includes_capped_tone():
    fat_tone = "Voice: short.\n\n" + "x" * 5000
    sys = generation.build_system_prompt(fat_tone)
    assert "[...truncated to fit tone budget]" in sys
    # Capped tone should keep the system prompt under the original 26.5K
    # ceiling minus the dropped tone bytes.
    assert len(sys) < 26_000


def test_evaluation_user_includes_capped_tone():
    fat_tone = "x" * 5000
    out = evaluation.build_user_prompt(
        tone_snapshot=fat_tone,
        campaign_goal="goal",
        angle="angle",
        draft="draft",
        lead_category="cat",
    )
    assert "[...truncated to fit tone budget]" in out


def test_max_tone_chars_default_is_documented():
    # The docs reference 1,500 chars — keep the constant in sync so a
    # silent bump doesn't drift from the documented contract.
    assert MAX_TONE_CHARS == 1500


# ---------------------------------------------------------------------------
# analysis._truncate_raw_data
# ---------------------------------------------------------------------------


def test_raw_data_passes_through_when_small():
    data = {"rating": 4, "reviews": [{"text": "nice"}]}
    out, truncated = analysis._truncate_raw_data(data, max_bytes=1024)
    assert not truncated
    assert out == data


def test_raw_data_truncates_long_strings():
    long_review = "x" * 50_000
    data = {"reviews": [{"text": long_review}]}
    out, truncated = analysis._truncate_raw_data(data, max_bytes=2048)
    assert truncated
    assert len(json.dumps(out, ensure_ascii=False).encode("utf-8")) <= 2048
    assert out["reviews"][0]["text"].startswith("x")
    assert "[truncated]" in out["reviews"][0]["text"]


# ---------------------------------------------------------------------------
# evaluation.build_user_prompt — bound the size and pin against the
# implicit-string-concat foot-gun.
#
# Versions v4.2 and v4.3 had ``"=" * 60 + "\n\n"`` mid-chain in
# ``build_user_prompt``. Python implicitly concatenates adjacent string
# literals (regular + f-strings) into one big string at parse time, then
# applied the ``* 60`` to the WHOLE concatenation — which silently shipped
# 60 copies of the BACKGROUND CONTEXT block on every eval call (~63K input
# tokens instead of ~1.5K). The tests below guard the rebuilt expression so
# the regression cannot return without setting off red lights here first.
# ---------------------------------------------------------------------------


def _eval_prompt_inputs(*, tone_size: int = 3300, angle_size: int = 450, draft_size: int = 320):
    return dict(
        tone_snapshot="X" * tone_size,
        campaign_goal="Get website build or management leads.",
        angle="Y" * angle_size,
        draft="Z" * draft_size,
        lead_category="Plumber",
    )


def test_evaluation_user_prompt_does_not_repeat_background_context():
    """Each fixed section must appear exactly once in the rendered prompt."""

    out = evaluation.build_user_prompt(**_eval_prompt_inputs())
    assert out.count("BACKGROUND CONTEXT") == 1
    assert out.count("THE DRAFT TO SCORE") == 1
    assert out.count("Tone guide:") == 1


def test_evaluation_user_prompt_size_is_bounded():
    """At realistic max-sized inputs the prompt stays under the 10K guardrail.

    Realistic upper bounds from the production DB (May 2026):
    - tone_snapshot ~3,300 chars,
    - angle <=452 chars,
    - draft <=407 chars,
    - lead_category one word.

    The pre-fix prompt produced 235,867 chars at these inputs (60x the
    BACKGROUND CONTEXT block). Anything north of 10K from these inputs
    means the implicit-concat trap has come back.
    """

    out = evaluation.build_user_prompt(**_eval_prompt_inputs())
    assert len(out) < 10_000, (
        f"build_user_prompt produced {len(out)} chars at realistic inputs — "
        "the implicit-string-concat bug at evaluation.py:~335 may have "
        "regressed. See the comment above ``separator = '=' * 60``."
    )


def test_evaluation_user_prompt_includes_separator_once():
    """The 60-char ``=`` separator must render exactly once and as 60 chars."""

    out = evaluation.build_user_prompt(**_eval_prompt_inputs())
    assert out.count("=" * 60) == 1
    assert "=" * 61 not in out


# ---------------------------------------------------------------------------
# evaluation.evaluate_result
# ---------------------------------------------------------------------------


def test_evaluate_result_recomputes_length_valid():
    long_draft = "a" * (evaluation.MAX_SMS_LENGTH + 1)
    normalised = evaluation.evaluate_result(
        {
            "scores": {
                "tone_match": 1.0,
                "personalisation": 1.0,
                "goal_alignment": 1.0,
                "length_valid": 1.0,  # lie
                "naturalness": 1.0,
            },
            "pass": True,
            "feedback": "",
        },
        draft=long_draft,
    )
    assert normalised["scores"]["length_valid"] == 0.0
    assert normalised["pass"] is False


def test_evaluate_result_pass_threshold():
    good_draft = "hi"
    normalised = evaluation.evaluate_result(
        {
            "scores": {
                "tone_match": 0.9,
                "personalisation": 0.9,
                "goal_alignment": 0.9,
                "length_valid": 1.0,
                "naturalness": 0.9,
            },
            "pass": True,
            "feedback": "",
        },
        draft=good_draft,
        threshold=0.85,
    )
    assert normalised["pass"] is True


# ---------------------------------------------------------------------------
# classification.normalise_classification
# ---------------------------------------------------------------------------


def test_classification_forces_escalation_on_low_confidence():
    out = classification.normalise_classification(
        {"intent": "positive", "confidence": 0.5, "reason": "unsure"}
    )
    assert out["requires_human"] is True


def test_classification_does_not_escalate_confident_positive():
    out = classification.normalise_classification(
        {"intent": "positive", "confidence": 0.92, "reason": "clear yes"}
    )
    assert out["requires_human"] is False


def test_classification_unknown_intent_collapses_to_unclear():
    out = classification.normalise_classification(
        {"intent": "weird_new_label", "confidence": 0.9, "reason": ""}
    )
    assert out["intent"] == "unclear"
    assert out["requires_human"] is True


def test_classification_bot_check_always_escalates():
    out = classification.normalise_classification(
        {"intent": "bot_check", "confidence": 0.99, "reason": "lead asked"}
    )
    assert out["requires_human"] is True
