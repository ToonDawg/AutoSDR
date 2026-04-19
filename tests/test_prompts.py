"""Prompt helpers — deterministic transforms worth pinning."""

from __future__ import annotations

import json

from autosdr.prompts import analysis, classification, evaluation


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
