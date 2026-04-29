"""``GET /api/llm/presets`` — Gemini blend catalog.

Locks the ticket-0006 contract: every preset names all four roles
with priced Gemini slugs, and the response surfaces the pricing
snapshot date so the UI can label "Pricing as of YYYY-MM-DD".
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from autosdr.llm.pricing import GEMINI_PRICING, LLM_PRESETS, PRICING_VERIFIED_AT
from autosdr.webhook import create_app


@pytest.fixture
def client(fresh_db, workspace_factory) -> TestClient:
    workspace_factory()
    return TestClient(create_app(run_scheduler_task=False), raise_server_exceptions=False)


def test_presets_endpoint_returns_every_preset(client: TestClient) -> None:
    body = client.get("/api/llm/presets").json()
    returned = {p["id"] for p in body["presets"]}
    assert returned == set(LLM_PRESETS.keys())


def test_presets_endpoint_surfaces_pricing_verified_at(client: TestClient) -> None:
    body = client.get("/api/llm/presets").json()
    assert body["pricing_verified_at"] == PRICING_VERIFIED_AT.isoformat()


def test_every_preset_role_resolves_to_a_priced_gemini_slug(client: TestClient) -> None:
    """The settings UI applies these slugs verbatim into ``model_main``
    et al; if any of them is unpriced, the per-call cost surface goes
    silent. Lock the contract."""

    body = client.get("/api/llm/presets").json()
    for preset in body["presets"]:
        for role, slug in preset["models"].items():
            assert slug in GEMINI_PRICING, (
                f"preset {preset['id']!r} role {role!r} → {slug!r} "
                "is not in GEMINI_PRICING"
            )


def test_max_preset_uses_max_model_for_every_role(client: TestClient) -> None:
    body = client.get("/api/llm/presets").json()
    max_preset = next(p for p in body["presets"] if p["id"] == "max")
    slugs = set(max_preset["models"].values())
    assert len(slugs) == 1, "MAX should be the same model for every role"
