"""``GET /api/llm-calls`` — cost_usd serialisation.

The endpoint already existed; this test locks the ticket-0006
contract: every returned row carries a ``cost_usd`` field, computed
from the row's ``model`` + ``tokens_in`` + ``tokens_out`` via
:func:`autosdr.llm.pricing.cost_for`. Known-priced rows return a
positive float; unknown / sentinel rows return ``null``.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from autosdr.db import session_scope
from autosdr.models import LlmCall, LlmCallPurpose, Workspace
from autosdr.webhook import create_app


@pytest.fixture
def client(fresh_db, workspace_factory) -> TestClient:
    workspace_factory()
    return TestClient(create_app(run_scheduler_task=False), raise_server_exceptions=False)


def _add_call(
    workspace_id: str, *, model: str, tokens_in: int, tokens_out: int
) -> str:
    with session_scope() as session:
        row = LlmCall(
            workspace_id=workspace_id,
            purpose=LlmCallPurpose.GENERATION,
            model=model,
            prompt_version="generation@v1",
            attempt=1,
            response_format="text",
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            latency_ms=120,
        )
        session.add(row)
        session.flush()
        return row.id


def _workspace_id() -> str:
    with session_scope() as session:
        return session.query(Workspace).first().id


def test_llm_calls_known_model_returns_positive_cost(client: TestClient) -> None:
    ws = _workspace_id()
    _add_call(
        ws, model="gemini/gemini-2.5-flash-lite",
        tokens_in=1_000_000, tokens_out=1_000_000,
    )

    body = client.get("/api/llm-calls").json()
    assert len(body) == 1
    assert body[0]["model"] == "gemini/gemini-2.5-flash-lite"
    # 1M * 0.10 + 1M * 0.40 = 0.50
    assert body[0]["cost_usd"] == pytest.approx(0.50, rel=1e-9)


def test_llm_calls_unknown_model_returns_null_cost(client: TestClient) -> None:
    """Avoid lying with ``$0.00`` for a model we don't know how to price."""

    ws = _workspace_id()
    _add_call(
        ws, model="openai/gpt-99-imaginary",
        tokens_in=1234, tokens_out=567,
    )

    body = client.get("/api/llm-calls").json()
    assert len(body) == 1
    assert body[0]["cost_usd"] is None


def test_llm_calls_zero_token_sentinel_costs_zero(client: TestClient) -> None:
    """Ticket 0001's ``(deterministic-opt-out)`` rows have zero tokens.
    They must report ``cost_usd == 0.0`` (not ``null``) so summing the
    column on the frontend doesn't NaN."""

    ws = _workspace_id()
    _add_call(
        ws, model="(deterministic-opt-out)",
        tokens_in=0, tokens_out=0,
    )

    body = client.get("/api/llm-calls").json()
    assert len(body) == 1
    assert body[0]["cost_usd"] == 0.0


def test_llm_calls_summary_aggregates_all_rows(client: TestClient) -> None:
    """Server-side total stays accurate past the list endpoint's row cap.

    The list response is capped at 500 rows for UI virtualisation, so a
    busy workspace would silently underreport spend if the frontend
    summed visible rows. The dedicated summary endpoint aggregates
    every row in the workspace through the same pricing function.
    """

    ws = _workspace_id()
    # 1M in + 1M out on flash-lite = $0.50 per row.
    for _ in range(3):
        _add_call(
            ws,
            model="gemini/gemini-2.5-flash-lite",
            tokens_in=1_000_000,
            tokens_out=1_000_000,
        )
    # Unknown model — should bump unpriced_calls and contribute $0.
    _add_call(
        ws, model="openai/gpt-99-imaginary", tokens_in=500, tokens_out=500
    )

    body = client.get("/api/llm-calls/summary").json()
    assert body["total_calls"] == 4
    assert body["total_tokens_in"] == 3_000_000 + 500
    assert body["total_tokens_out"] == 3_000_000 + 500
    assert body["total_cost_usd"] == pytest.approx(1.50, rel=1e-9)
    assert body["unpriced_calls"] == 1


def test_llm_calls_summary_respects_filters(client: TestClient) -> None:
    """Filter params on /summary mirror the list endpoint contract.

    The Logs page deep-links to per-thread/per-campaign filters; the
    total in the header should reflect only what the user is currently
    looking at, not the whole workspace.
    """

    ws = _workspace_id()
    with session_scope() as session:
        thread_call = LlmCall(
            workspace_id=ws,
            thread_id="thread-a",
            purpose=LlmCallPurpose.GENERATION,
            model="gemini/gemini-2.5-flash-lite",
            prompt_version="generation@v1",
            attempt=1,
            response_format="text",
            tokens_in=1_000_000,
            tokens_out=1_000_000,
            latency_ms=120,
        )
        other_call = LlmCall(
            workspace_id=ws,
            thread_id="thread-b",
            purpose=LlmCallPurpose.GENERATION,
            model="gemini/gemini-2.5-flash-lite",
            prompt_version="generation@v1",
            attempt=1,
            response_format="text",
            tokens_in=2_000_000,
            tokens_out=2_000_000,
            latency_ms=120,
        )
        session.add_all([thread_call, other_call])
        session.flush()

    body = client.get(
        "/api/llm-calls/summary", params={"thread_id": "thread-a"}
    ).json()
    assert body["total_calls"] == 1
    assert body["total_cost_usd"] == pytest.approx(0.50, rel=1e-9)
