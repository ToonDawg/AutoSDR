"""LLM configuration helpers — currently just preset blends.

The settings UI uses ``GET /api/llm/presets`` to render one-click
"MAX / Balanced / Cheap" buttons that fill the four ``model_*`` fields
on the workspace's LLM settings. Keeping the catalog server-side
means a Gemini price/model change ships in one place
(``autosdr/llm/pricing.py``) and every operator sees it at the next
page load — see ticket 0006.

This router intentionally has no PATCH/POST: applying a preset is
just a normal ``PATCH /api/workspace`` with the four model slugs the
operator chose. The frontend stays in control of when it writes.
"""

from __future__ import annotations

from fastapi import APIRouter

from autosdr.api.schemas import LlmPresetModels, LlmPresetOut, LlmPresetsOut
from autosdr.llm.pricing import LLM_PRESETS, PRICING_VERIFIED_AT

router = APIRouter(prefix="/api/llm", tags=["llm"])


@router.get("/presets", response_model=LlmPresetsOut)
def list_presets() -> LlmPresetsOut:
    """Return the static catalog of Gemini-only model presets."""

    presets = [
        LlmPresetOut(
            id=preset.id,
            label=preset.label,
            description=preset.description,
            models=LlmPresetModels(**preset.models()),
        )
        for preset in LLM_PRESETS.values()
    ]
    return LlmPresetsOut(
        pricing_verified_at=PRICING_VERIFIED_AT.isoformat(),
        presets=presets,
    )


__all__ = ["router"]
