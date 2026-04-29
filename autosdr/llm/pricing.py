"""Gemini pricing table, alias resolver, cost helper, and named presets.

This is the **single source of truth** for translating an LLM call's
``(model, tokens_in, tokens_out)`` into a USD cost estimate, plus the
canonical "MAX / BALANCED / CHEAP" Gemini blends the operator can apply
from the Settings page.

Per ticket 0006 we compute cost at read time rather than persisting it
on ``llm_call``. Trade-off accepted: a pricing-map edit retroactively
reprices historical Logs rows. We label every cost surface "estimated"
and surface :data:`PRICING_VERIFIED_AT` so the operator knows the
snapshot date.

If/when a real spend-audit consumer appears (monthly close, tax export)
add a nullable ``llm_call.cost_usd`` column populated at write-time and
keep this module as the read-side fallback for historical rows.

Pricing is **standard / paid tier, text only**. Audio, image,
batch / flex / priority tiers are out of scope; if a future ticket needs
them, extend :class:`ModelPrice` with optional fields rather than
forking the table.

Sourced from https://ai.google.dev/gemini-api/docs/pricing — see
:data:`PRICING_VERIFIED_AT` for the snapshot date.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date


PRICING_VERIFIED_AT: date = date(2026, 4, 27)
"""Date the pricing snapshot below was last reconciled with Google's docs."""


@dataclass(frozen=True)
class ModelPrice:
    """Standard-tier paid pricing in USD per 1M text tokens.

    ``input_per_1m_usd`` / ``output_per_1m_usd`` are the small-prompt
    rates. Models that price 2x above 200k tokens (currently Gemini
    Pro families) carry the small-prompt rate here — at AutoSDR's
    prompt sizes (one analysis call, one generation call, both well
    under 10k tokens) the >200k tier never applies.
    """

    input_per_1m_usd: float
    output_per_1m_usd: float


# ---------------------------------------------------------------------------
# Pricing map. Keys are LiteLLM-style slugs (with the ``gemini/`` prefix the
# rest of the codebase uses in ``workspace.settings.llm.model_*``). Slugs
# without the prefix are also accepted by :func:`cost_for` so an operator
# typing ``gemini-2.5-pro`` directly still gets a number.
# ---------------------------------------------------------------------------


GEMINI_PRICING: dict[str, ModelPrice] = {
    # Gemini 3.x — current AutoSDR defaults (preview slugs are the rolling
    # target for now; -latest aliases will replace them at GA).
    "gemini/gemini-3.1-pro-preview": ModelPrice(2.00, 12.00),
    "gemini/gemini-3-flash-preview": ModelPrice(0.50, 3.00),
    "gemini/gemini-3.1-flash-lite-preview": ModelPrice(0.25, 1.50),
    # Gemini 2.5 — stable family (operators may pin these for predictable
    # pricing while 3.x is in preview).
    "gemini/gemini-2.5-pro": ModelPrice(1.25, 10.00),
    "gemini/gemini-2.5-flash": ModelPrice(0.30, 2.50),
    "gemini/gemini-2.5-flash-lite": ModelPrice(0.10, 0.40),
}


# ---------------------------------------------------------------------------
# Alias map. Google ships ``-latest`` suffixes that always point at the most
# recent stable version of each family — useful when an operator wants
# "always on the newest 2.5 Flash" without tracking minor-version slugs.
# Resolution is a pure dict normaliser; unknown slugs fall through unchanged.
# ---------------------------------------------------------------------------


GEMINI_LATEST_ALIASES: dict[str, str] = {
    "gemini/gemini-2.5-pro-latest": "gemini/gemini-2.5-pro",
    "gemini/gemini-2.5-flash-latest": "gemini/gemini-2.5-flash",
    "gemini/gemini-2.5-flash-lite-latest": "gemini/gemini-2.5-flash-lite",
}


def resolve_model_alias(model: str) -> str:
    """Normalise a model slug.

    Adds the ``gemini/`` prefix when missing (so ``gemini-2.5-pro`` is
    treated the same as ``gemini/gemini-2.5-pro``), then dereferences any
    ``-latest`` alias from :data:`GEMINI_LATEST_ALIASES`. Slugs with no
    matching alias are returned unchanged.
    """

    if not model:
        return model
    if "/" not in model and model.startswith("gemini-"):
        model = f"gemini/{model}"
    return GEMINI_LATEST_ALIASES.get(model, model)


def cost_for(model: str, tokens_in: int, tokens_out: int) -> float | None:
    """Return the estimated USD cost for one LLM call, or ``None``.

    ``None`` is the explicit signal "we don't know how to price this
    model" — callers should render it as ``—`` rather than ``$0.00`` so
    the UI doesn't lie.

    Calls with both token counts at zero (e.g. the ``(deterministic-
    opt-out)`` sentinel from ticket 0001, or pre-flight failures that
    never reached the provider) cost zero whether we know the model or
    not, so we shortcut to ``0.0`` before the pricing-map lookup. This
    keeps the sentinel rows consistent across every cost surface.
    """

    tokens_in = max(0, int(tokens_in or 0))
    tokens_out = max(0, int(tokens_out or 0))
    if tokens_in == 0 and tokens_out == 0:
        return 0.0

    canonical = resolve_model_alias(model)
    price = GEMINI_PRICING.get(canonical)
    if price is None:
        return None

    return (
        tokens_in * price.input_per_1m_usd / 1_000_000.0
        + tokens_out * price.output_per_1m_usd / 1_000_000.0
    )


# ---------------------------------------------------------------------------
# Named presets. The operator picks one in Settings → LLM and the four
# model-role inputs (model_main / _analysis / _eval / _classification —
# see ``autosdr/config.py``) get filled accordingly. Free-text edits
# afterwards are still allowed — see ticket 0006 § "Resolved: preset
# surface".
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Preset:
    id: str
    label: str
    description: str
    model_main: str
    model_analysis: str
    model_eval: str
    model_classification: str

    def models(self) -> dict[str, str]:
        """Return the four role → slug mappings as a plain dict.

        The shape matches ``WorkspaceSettings.llm`` so the frontend can
        spread it directly into the patch payload.
        """

        return {
            "model_main": self.model_main,
            "model_analysis": self.model_analysis,
            "model_eval": self.model_eval,
            "model_classification": self.model_classification,
        }


_MAX_MODEL = "gemini/gemini-3.1-pro-preview"
_BALANCED_HEAVY = "gemini/gemini-3.1-pro-preview"
_BALANCED_MID = "gemini/gemini-3-flash-preview"
_BALANCED_LIGHT = "gemini/gemini-3.1-flash-lite-preview"
_CHEAP_MODEL = "gemini/gemini-3.1-flash-lite-preview"


LLM_PRESETS: dict[str, Preset] = {
    "max": Preset(
        id="max",
        label="MAX",
        description=(
            "Best-quality Gemini for every role. Highest cost, slowest, "
            "best message + classifier accuracy. Use when reply rate "
            "and tone fidelity matter more than spend."
        ),
        model_main=_MAX_MODEL,
        model_analysis=_MAX_MODEL,
        model_eval=_MAX_MODEL,
        model_classification=_MAX_MODEL,
    ),
    "balanced": Preset(
        id="balanced",
        label="BALANCED",
        description=(
            "Pro for outreach drafts, Flash for analysis, Flash-Lite for "
            "evaluator + classifier. Tone where it matters; cheap where it "
            "doesn't."
        ),
        model_main=_BALANCED_HEAVY,
        model_analysis=_BALANCED_MID,
        model_eval=_BALANCED_LIGHT,
        model_classification=_BALANCED_LIGHT,
    ),
    "cheap": Preset(
        id="cheap",
        label="CHEAP",
        description=(
            "Flash-Lite everywhere. Cheapest possible Gemini blend; useful "
            "for rehearsals and for low-stakes campaigns. Expect a quality "
            "dip on outreach drafts."
        ),
        model_main=_CHEAP_MODEL,
        model_analysis=_CHEAP_MODEL,
        model_eval=_CHEAP_MODEL,
        model_classification=_CHEAP_MODEL,
    ),
}


__all__ = [
    "GEMINI_LATEST_ALIASES",
    "GEMINI_PRICING",
    "LLM_PRESETS",
    "ModelPrice",
    "PRICING_VERIFIED_AT",
    "Preset",
    "cost_for",
    "resolve_model_alias",
]
