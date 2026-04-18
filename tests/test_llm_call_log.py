"""LLM call persistence — every attempt lands in DB + JSONL."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from autosdr.config import get_settings
from autosdr.llm import client as llm_client
from autosdr.llm.client import LlmCallContext, complete_json, complete_text
from autosdr.models import LlmCall, LlmCallPurpose


@pytest.fixture
def llm_stub(monkeypatch):
    """Capture calls to _do_completion and serve canned responses."""

    calls: list[dict] = []
    canned: list[tuple[str, int, int]] = []

    async def _fake(*, model, messages, temperature, response_format):
        calls.append(
            {
                "model": model,
                "messages": messages,
                "temperature": temperature,
                "response_format": response_format,
            }
        )
        if canned:
            return canned.pop(0)
        return ("ok", 5, 7)

    monkeypatch.setattr(llm_client, "_do_completion", _fake)
    return {"calls": calls, "canned": canned}


async def test_complete_text_writes_row_and_jsonl(fresh_db, llm_stub):
    fresh_db()  # ensure tables

    ctx = LlmCallContext(
        purpose=LlmCallPurpose.ANALYSIS,
        workspace_id="ws-1",
        thread_id="thread-1",
    )
    result = await complete_text(
        system="you are a helpful SDR",
        user="write a greeting",
        model="gemini/gemini-3-flash-preview",
        prompt_version="analysis-v1",
        temperature=0.2,
        context=ctx,
    )

    assert result.text == "ok"
    assert result.llm_call_id  # DB row id returned
    assert result.tokens_in == 5
    assert result.tokens_out == 7

    with fresh_db() as session:
        rows = session.query(LlmCall).all()
        assert len(rows) == 1
        row = rows[0]
        assert row.purpose == LlmCallPurpose.ANALYSIS
        assert row.workspace_id == "ws-1"
        assert row.thread_id == "thread-1"
        assert row.model == "gemini/gemini-3-flash-preview"
        assert row.prompt_version == "analysis-v1"
        assert row.attempt == 1
        assert row.response_text == "ok"
        assert row.error is None
        assert "helpful SDR" in (row.system_prompt or "")
        assert "write a greeting" in (row.user_prompt or "")

    settings = get_settings()
    log_files = list(Path(settings.log_dir).glob("llm-*.jsonl"))
    assert len(log_files) == 1
    records = [
        json.loads(line) for line in log_files[0].read_text().splitlines() if line.strip()
    ]
    assert len(records) == 1
    assert records[0]["purpose"] == LlmCallPurpose.ANALYSIS
    assert records[0]["thread_id"] == "thread-1"
    assert records[0]["error"] is None


async def test_complete_json_parses_and_backfills_parsed(fresh_db, llm_stub):
    fresh_db()
    llm_stub["canned"].append(('{"angle": "rating is 3", "confidence": 0.6}', 10, 20))

    ctx = LlmCallContext(
        purpose=LlmCallPurpose.ANALYSIS,
        workspace_id="ws-1",
        thread_id="thread-2",
        lead_id="lead-2",
    )
    parsed, result = await complete_json(
        system="extract",
        user="raw data dump",
        model="gemini/gemini-3-flash-preview",
        prompt_version="analysis-v1",
        context=ctx,
    )

    assert parsed == {"angle": "rating is 3", "confidence": 0.6}
    assert result.llm_call_id

    with fresh_db() as session:
        row = session.get(LlmCall, result.llm_call_id)
        assert row is not None
        assert row.response_parsed == parsed
        assert row.response_format == "json"
        assert row.thread_id == "thread-2"
        assert row.lead_id == "lead-2"


async def test_complete_json_self_heal_logs_both_attempts(fresh_db, llm_stub):
    fresh_db()
    llm_stub["canned"].append(("not json at all", 3, 4))
    llm_stub["canned"].append(('{"ok": true}', 6, 8))

    parsed, result = await complete_json(
        system="s",
        user="u",
        model="gemini/gemini-3-flash-preview",
        prompt_version="classification-v1",
        context=LlmCallContext(
            purpose=LlmCallPurpose.CLASSIFICATION,
            workspace_id="ws-9",
            thread_id="thread-9",
        ),
    )
    assert parsed == {"ok": True}

    with fresh_db() as session:
        rows = (
            session.query(LlmCall)
            .filter(LlmCall.prompt_version == "classification-v1")
            .order_by(LlmCall.attempt.asc())
            .all()
        )
        assert len(rows) == 2
        assert [r.attempt for r in rows] == [1, 2]
        assert rows[0].response_text == "not json at all"
        assert rows[0].response_parsed is None  # parse failed on first
        assert rows[1].response_parsed == {"ok": True}


async def test_disabled_logging_skips_db_and_jsonl(
    fresh_db, llm_stub, monkeypatch, tmp_path
):
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    from autosdr import config as config_module

    config_module.reset_settings_for_tests()
    fresh_db()

    result = await complete_text(
        system="s",
        user="u",
        model="gemini/gemini-3-flash-preview",
        prompt_version="analysis-v1",
        context=LlmCallContext(purpose=LlmCallPurpose.ANALYSIS),
    )
    assert result.llm_call_id is None

    with fresh_db() as session:
        assert session.query(LlmCall).count() == 0

    log_dir = get_settings().log_dir
    assert not any(log_dir.glob("llm-*.jsonl")) if log_dir.exists() else True


async def test_provider_error_persists_failed_row(fresh_db, llm_stub, monkeypatch):
    fresh_db()

    async def _boom(*, model, messages, temperature, response_format):
        raise RuntimeError("provider on fire")

    monkeypatch.setattr(llm_client, "_do_completion", _boom)

    with pytest.raises(llm_client.LLMError):
        await complete_text(
            system="s",
            user="u",
            model="gemini/gemini-3-flash-preview",
            prompt_version="analysis-v1",
            context=LlmCallContext(
                purpose=LlmCallPurpose.ANALYSIS,
                workspace_id="ws-1",
            ),
        )

    with fresh_db() as session:
        rows = session.query(LlmCall).all()
        assert len(rows) == 1
        row = rows[0]
        assert row.error is not None
        assert "provider on fire" in row.error
        assert row.response_text is None
