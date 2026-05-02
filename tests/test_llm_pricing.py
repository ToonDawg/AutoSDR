"""Pricing map, alias resolver, ``cost_for``, and named-preset shape.

These are pure-function tests — no DB, no LLM, no connector. They lock
the contract every other ticket-0006 unit binds to: the pricing-map
keys, the ``cost_for`` return-type semantics, and the closure property
"every model named in a preset is in the pricing map".
"""

from __future__ import annotations

import pytest

from autosdr.llm.pricing import (
    GEMINI_LATEST_ALIASES,
    GEMINI_PRICING,
    LLM_PRESETS,
    ModelPrice,
    PRICING_VERIFIED_AT,
    Preset,
    cost_for,
    resolve_model_alias,
)


# ---------------------------------------------------------------------------
# Pricing map shape
# ---------------------------------------------------------------------------


def test_pricing_map_keys_use_litellm_prefix() -> None:
    """All canonical keys must be ``gemini/...`` so they round-trip through
    LiteLLM and ``workspace.settings.llm.model_*`` without extra logic."""

    for slug in GEMINI_PRICING:
        assert slug.startswith("gemini/"), slug


def test_pricing_map_values_are_modelprice_with_positive_rates() -> None:
    for slug, price in GEMINI_PRICING.items():
        assert isinstance(price, ModelPrice), slug
        assert price.input_per_1m_usd > 0, slug
        assert price.output_per_1m_usd > 0, slug
        assert price.output_per_1m_usd >= price.input_per_1m_usd, (
            f"{slug}: output should not undercut input on Gemini text models"
        )


def test_pricing_verified_at_is_a_real_date() -> None:
    """The snapshot date is what the operator reads in the UI; it must
    be a sane, recent value, not e.g. ``date.min``."""

    assert PRICING_VERIFIED_AT.year >= 2026


# ---------------------------------------------------------------------------
# Alias resolver
# ---------------------------------------------------------------------------


def test_aliases_resolve_to_known_pricing_entries() -> None:
    """Every ``-latest`` alias must point at a slug in the pricing map —
    otherwise a preset using the alias would silently price as ``None``."""

    for alias, target in GEMINI_LATEST_ALIASES.items():
        assert alias.endswith("-latest"), alias
        assert target in GEMINI_PRICING, (
            f"alias {alias} → {target} is not in GEMINI_PRICING"
        )


def test_resolve_passes_through_unknown_slugs() -> None:
    assert resolve_model_alias("gemini/gemini-9000-experimental") == (
        "gemini/gemini-9000-experimental"
    )


def test_resolve_adds_litellm_prefix_when_missing() -> None:
    """Operators sometimes type the bare Google slug. We accept it."""

    assert resolve_model_alias("gemini-2.5-pro") == "gemini/gemini-2.5-pro"


def test_resolve_dereferences_latest_alias() -> None:
    assert (
        resolve_model_alias("gemini/gemini-2.5-flash-latest")
        == "gemini/gemini-2.5-flash"
    )


def test_resolve_handles_empty_string() -> None:
    """Defensive: never hand a misnamed model to the pricing lookup."""

    assert resolve_model_alias("") == ""


# ---------------------------------------------------------------------------
# cost_for
# ---------------------------------------------------------------------------


def test_cost_for_known_model_matches_per_million_arithmetic() -> None:
    """1M input + 1M output on Flash-Lite should equal exactly the rate
    card. Anything else is a unit error."""

    cost = cost_for("gemini/gemini-2.5-flash-lite", 1_000_000, 1_000_000)
    expected = 0.10 + 0.40
    assert cost == pytest.approx(expected, rel=1e-9)


def test_cost_for_uses_alias_resolution() -> None:
    """``-latest`` must price the same as its canonical target."""

    via_alias = cost_for(
        "gemini/gemini-2.5-flash-latest", 200_000, 100_000
    )
    via_canonical = cost_for(
        "gemini/gemini-2.5-flash", 200_000, 100_000
    )
    assert via_alias == pytest.approx(via_canonical)


def test_cost_for_unknown_model_returns_none() -> None:
    """``None`` is the explicit "we don't know" signal — UI must show
    ``—``, not a misleading ``$0.00``."""

    assert cost_for("openai/gpt-99-imaginary", 1000, 1000) is None


def test_cost_for_zero_tokens_is_zero_even_for_unknown_model() -> None:
    """Sentinel rows from ticket 0001 (deterministic-opt-out) carry an
    unrecognised ``model`` and zero tokens. Cost must collapse to
    ``0.0`` so the dashboard total is still summable."""

    assert cost_for("(deterministic-opt-out)", 0, 0) == 0.0


def test_cost_for_handles_none_token_counts() -> None:
    """Pre-flight failures pass ``None`` rather than an int for tokens.
    We must not raise — the call still gets logged with cost 0."""

    assert (
        cost_for("gemini/gemini-3-flash-preview", None, None)  # type: ignore[arg-type]
        == 0.0
    )


def test_cost_for_negative_tokens_clamped_to_zero() -> None:
    """Defensive: never let an upstream bug turn into a negative cost."""

    assert (
        cost_for("gemini/gemini-3-flash-preview", -5, -10) == 0.0
    )


# ---------------------------------------------------------------------------
# Presets
# ---------------------------------------------------------------------------


def test_presets_have_expected_ids() -> None:
    """The frontend hard-codes button order against these ids; if they
    change, that ticket must change too."""

    assert set(LLM_PRESETS.keys()) == {"max", "balanced", "cheap"}


def test_every_preset_names_all_four_roles_with_priced_models() -> None:
    """Closure property: presets must reference only models we know how
    to price. Otherwise a preset would surface ``cost_usd: null`` and
    defeat its own purpose."""

    for preset_id, preset in LLM_PRESETS.items():
        assert isinstance(preset, Preset)
        models = preset.models()
        assert set(models.keys()) == {
            "model_main",
            "model_analysis",
            "model_eval",
            "model_classification",
        }, preset_id
        for role, slug in models.items():
            canonical = resolve_model_alias(slug)
            assert canonical in GEMINI_PRICING, (
                f"preset {preset_id} role {role} → {slug} not in pricing map"
            )


def test_max_preset_is_strictly_priciest_per_call_at_a_balanced_payload() -> None:
    """Sanity-check the labels: at any reasonable payload, MAX should
    cost more than CHEAP for the same call. If this ever fails, the
    preset definitions have rotted relative to the pricing map."""

    tokens_in, tokens_out = 800, 400
    max_main = cost_for(LLM_PRESETS["max"].model_main, tokens_in, tokens_out)
    cheap_main = cost_for(LLM_PRESETS["cheap"].model_main, tokens_in, tokens_out)
    assert max_main is not None
    assert cheap_main is not None
    assert max_main > cheap_main


def test_preset_models_dict_shape_matches_workspace_settings_keys() -> None:
    """The frontend spreads ``preset.models`` into the LLM settings
    PATCH; the keys must therefore match the four canonical role
    fields (see ``autosdr/config.py::DEFAULT_WORKSPACE_SETTINGS``).
    """

    expected = {"model_main", "model_analysis", "model_eval", "model_classification"}
    for preset in LLM_PRESETS.values():
        assert set(preset.models().keys()) == expected


# ---------------------------------------------------------------------------
# In-memory usage counter — cost accumulation
# ---------------------------------------------------------------------------


def test_record_usage_accumulates_cost_into_total_and_per_model() -> None:
    """The status dashboard reads ``total_cost_usd`` and the per-model
    bucket cost via ``get_usage_snapshot()``. Both must add up across
    multiple calls without re-querying the DB."""

    from autosdr.llm.client import _record_usage, get_usage_snapshot, reset_usage

    reset_usage()
    try:
        _record_usage(
            "gemini/gemini-2.5-flash-lite", 1_000_000, 1_000_000, 0.50
        )
        _record_usage(
            "gemini/gemini-2.5-flash-lite", 500_000, 500_000, 0.25
        )

        snap = get_usage_snapshot()
        assert snap["total_calls"] == 2
        assert snap["total_tokens_in"] == 1_500_000
        assert snap["total_tokens_out"] == 1_500_000
        # 1.5x (0.10 + 0.40) = 0.75
        assert snap["total_cost_usd"] == pytest.approx(0.75, rel=1e-9)

        bucket = snap["per_model"]["gemini/gemini-2.5-flash-lite"]
        assert bucket["calls"] == 2
        assert bucket["cost_usd"] == pytest.approx(0.75, rel=1e-9)
    finally:
        reset_usage()


def test_record_usage_unknown_model_contributes_zero_cost() -> None:
    """Unknown model: tokens are counted, cost stays at 0 so the
    aggregate is still summable. Per-call surfaces still see ``None``
    via :func:`cost_for` directly."""

    from autosdr.llm.client import _record_usage, get_usage_snapshot, reset_usage

    reset_usage()
    try:
        _record_usage("openai/gpt-99-imaginary", 1000, 1000, 0.0)
        snap = get_usage_snapshot()
        assert snap["total_calls"] == 1
        assert snap["total_cost_usd"] == 0.0
        assert snap["per_model"]["openai/gpt-99-imaginary"]["cost_usd"] == 0.0
    finally:
        reset_usage()


def test_reset_usage_clears_cost_too() -> None:
    """Test fixture relies on this — every test starts with cost=0."""

    from autosdr.llm.client import _record_usage, get_usage_snapshot, reset_usage

    _record_usage("gemini/gemini-3-flash-preview", 2000, 1000, 0.004)
    reset_usage()
    snap = get_usage_snapshot()
    assert snap["total_calls"] == 0
    assert snap["total_cost_usd"] == 0.0
    assert snap["per_model"] == {}
