"""Thin wrapper over LiteLLM.

The wrapper adds six behaviours the pipelines expect:

1. ``raise_if_paused`` before every dispatch, so a tripped kill switch aborts
   the call cleanly rather than burning tokens.
2. Exponential backoff on retryable errors (HTTP 429 / 5xx from the provider).
3. In-memory token / call counting exposed via :func:`get_usage_snapshot` for
   ``autosdr status``.
4. JSON-only completion helper that parses (with one self-heal retry) so
   callers never see raw text for structured prompts.
5. Persistent call log: every attempt — successful or failed, including
   self-heal retries — is written to the ``llm_call`` DB table and mirrored
   to ``data/logs/llm-YYYYMMDD.jsonl`` so the owner can review and refine
   prompts after the fact.
6. Call context (workspace / campaign / thread / lead IDs, purpose) is
   attached to every persisted row so ``autosdr logs llm`` and
   ``autosdr logs thread`` can filter and stitch transcripts cheaply.
"""

from __future__ import annotations

# NOTE: SSL patching MUST happen before ``import litellm`` (which imports
# aiohttp, which in turn pre-builds an SSL context at import time and caches
# it on ``aiohttp.connector._SSL_CONTEXT_VERIFIED``). If we patch after, the
# cached context wins and the patch is a no-op.
#
# macOS Homebrew Python uses its own OpenSSL that may be missing root CAs,
# and corporate networks (Zscaler, Netskope, etc.) often MITM outbound TLS
# with a root whose Basic Constraints are not marked critical — which modern
# OpenSSL rejects under VERIFY_X509_STRICT. We therefore patch
# ``ssl.create_default_context`` to:
#   1. load certifi's public CA bundle (needed on fresh Homebrew installs),
#   2. load any extra corporate root listed in AUTOSDR_EXTRA_CA_CERTS (or,
#      as a convenience, NODE_EXTRA_CA_CERTS / REQUESTS_CA_BUNDLE /
#      SSL_CERT_FILE — whichever the shell already has set),
#   3. clear VERIFY_X509_STRICT so a non-critical basic-constraints MITM
#      root still validates.
# We then refresh aiohttp's pre-built contexts if aiohttp is already loaded.
import os as _os
import ssl as _ssl
import certifi as _certifi

_orig_create_default_context = _ssl.create_default_context


def _extra_ca_bundle_paths() -> list[str]:
    seen: set[str] = set()
    bundles: list[str] = []
    for var in (
        "AUTOSDR_EXTRA_CA_CERTS",
        "NODE_EXTRA_CA_CERTS",
        "REQUESTS_CA_BUNDLE",
        "SSL_CERT_FILE",
    ):
        value = _os.environ.get(var)
        if not value:
            continue
        for candidate in value.split(_os.pathsep):
            candidate = candidate.strip()
            if candidate and candidate not in seen and _os.path.isfile(candidate):
                seen.add(candidate)
                bundles.append(candidate)
    return bundles


def _patched_create_default_context(purpose=_ssl.Purpose.SERVER_AUTH, **kwargs):
    ctx = _orig_create_default_context(purpose, **kwargs)
    ctx.load_verify_locations(_certifi.where())
    for bundle in _extra_ca_bundle_paths():
        try:
            ctx.load_verify_locations(bundle)
        except Exception:
            pass
    ctx.verify_flags &= ~_ssl.VERIFY_X509_STRICT
    return ctx


_ssl.create_default_context = _patched_create_default_context

# aiohttp caches a pre-built SSL context at module import time. If aiohttp
# was imported before the patch above (e.g. by another package), rebuild the
# cache now so our trusted roots actually take effect.
import sys as _sys

if "aiohttp.connector" in _sys.modules:
    _aiohttp_connector = _sys.modules["aiohttp.connector"]
    try:
        _aiohttp_connector._SSL_CONTEXT_VERIFIED = _ssl.create_default_context()
    except Exception:  # pragma: no cover - best effort
        pass

import asyncio
import json
import logging
import re
import time
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock
from typing import Any

import litellm

# Rebuild aiohttp's cached verified context now that aiohttp has definitely
# been imported (either just now via litellm or earlier in the process).
try:
    import aiohttp.connector as _aiohttp_connector_post

    _aiohttp_connector_post._SSL_CONTEXT_VERIFIED = _ssl.create_default_context()
except Exception:  # pragma: no cover - best effort
    pass

from autosdr import killswitch
from autosdr.config import get_settings
from autosdr.db import session_scope
from autosdr.models import LlmCall, LlmCallPurpose

logger = logging.getLogger(__name__)


class LLMError(RuntimeError):
    """Unrecoverable LLM error, or retries exhausted."""


@dataclass
class LlmCallContext:
    """Tags attached to every LLM call for observability.

    All fields are optional — the client never fails if a caller omits them.
    Pipelines pass as much as they know (``workspace_id`` is always known,
    the rest depend on stage).
    """

    purpose: str = LlmCallPurpose.OTHER
    workspace_id: str | None = None
    campaign_id: str | None = None
    thread_id: str | None = None
    lead_id: str | None = None


@dataclass
class CompletionResult:
    text: str
    model: str
    prompt_version: str
    tokens_in: int
    tokens_out: int
    attempts: int
    latency_ms: int
    llm_call_id: str | None = None


@dataclass
class _Usage:
    total_calls: int = 0
    total_tokens_in: int = 0
    total_tokens_out: int = 0
    per_model: dict[str, dict[str, int]] = field(default_factory=dict)


_usage = _Usage()
_usage_lock = Lock()


def _record_usage(model: str, tokens_in: int, tokens_out: int) -> None:
    with _usage_lock:
        _usage.total_calls += 1
        _usage.total_tokens_in += tokens_in
        _usage.total_tokens_out += tokens_out
        bucket = _usage.per_model.setdefault(
            model, {"calls": 0, "tokens_in": 0, "tokens_out": 0}
        )
        bucket["calls"] += 1
        bucket["tokens_in"] += tokens_in
        bucket["tokens_out"] += tokens_out


def get_usage_snapshot() -> dict[str, Any]:
    with _usage_lock:
        return {
            "total_calls": _usage.total_calls,
            "total_tokens_in": _usage.total_tokens_in,
            "total_tokens_out": _usage.total_tokens_out,
            "per_model": {k: dict(v) for k, v in _usage.per_model.items()},
        }


def reset_usage() -> None:
    with _usage_lock:
        _usage.total_calls = 0
        _usage.total_tokens_in = 0
        _usage.total_tokens_out = 0
        _usage.per_model.clear()


# ---------------------------------------------------------------------------
# Persistent call log
# ---------------------------------------------------------------------------


_LOG_FILE_LOCK = Lock()


def _truncate(text: str | None, limit: int) -> str | None:
    if text is None:
        return None
    if limit <= 0 or len(text) <= limit:
        return text
    return text[:limit] + f"\n... [truncated {len(text) - limit} chars]"


def _log_call(
    *,
    context: LlmCallContext,
    model: str,
    prompt_version: str,
    temperature: float,
    response_format: str,
    attempt: int,
    messages: list[dict[str, Any]],
    response_text: str | None,
    response_parsed: dict | None,
    tokens_in: int,
    tokens_out: int,
    latency_ms: int,
    error: str | None,
) -> str | None:
    """Persist one LLM call to DB + JSONL. Best-effort; never raises."""

    settings = get_settings()
    if not settings.llm_log_enabled:
        return None

    system_prompt = next(
        (m.get("content") for m in messages if m.get("role") == "system"), None
    )
    user_prompt_parts: list[str] = []
    for m in messages:
        if m.get("role") in {"user", "assistant"}:
            user_prompt_parts.append(f"[{m.get('role')}] {m.get('content', '')}")
    user_prompt = "\n\n".join(user_prompt_parts) if user_prompt_parts else None

    limit = settings.llm_log_max_prompt_chars
    system_prompt = _truncate(system_prompt, limit)
    user_prompt = _truncate(user_prompt, limit)
    response_text_trunc = _truncate(response_text, limit)

    call_id: str | None = None

    try:
        with session_scope() as session:
            row = LlmCall(
                workspace_id=context.workspace_id,
                campaign_id=context.campaign_id,
                thread_id=context.thread_id,
                lead_id=context.lead_id,
                purpose=context.purpose,
                model=model,
                prompt_version=prompt_version,
                temperature=temperature,
                attempt=attempt,
                response_format=response_format,
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                response_text=response_text_trunc,
                response_parsed=response_parsed,
                tokens_in=tokens_in,
                tokens_out=tokens_out,
                latency_ms=latency_ms,
                error=error,
            )
            session.add(row)
            session.flush()
            call_id = row.id
    except Exception:  # pragma: no cover - persistence is best-effort
        logger.exception("failed to persist LLM call to database")

    try:
        log_dir: Path = settings.log_dir
        log_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(tz=timezone.utc)
        path = log_dir / f"llm-{stamp.strftime('%Y%m%d')}.jsonl"
        record = {
            "id": call_id,
            "created_at": stamp.isoformat(),
            "purpose": context.purpose,
            "workspace_id": context.workspace_id,
            "campaign_id": context.campaign_id,
            "thread_id": context.thread_id,
            "lead_id": context.lead_id,
            "model": model,
            "prompt_version": prompt_version,
            "temperature": temperature,
            "attempt": attempt,
            "response_format": response_format,
            "system_prompt": system_prompt,
            "user_prompt": user_prompt,
            "response_text": response_text_trunc,
            "response_parsed": response_parsed,
            "tokens_in": tokens_in,
            "tokens_out": tokens_out,
            "latency_ms": latency_ms,
            "error": error,
        }
        with _LOG_FILE_LOCK:
            with path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
    except Exception:  # pragma: no cover - best effort
        logger.exception("failed to write LLM call to JSONL log")

    return call_id


# ---------------------------------------------------------------------------
# Low-level completion with retries
# ---------------------------------------------------------------------------


_RETRYABLE_STATUS = {408, 429, 500, 502, 503, 504}


async def _do_completion(
    *,
    model: str,
    messages: list[dict[str, Any]],
    temperature: float,
    response_format: dict | None,
) -> tuple[str, int, int]:
    """Single dispatch — returns (text, tokens_in, tokens_out)."""

    kwargs: dict[str, Any] = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
    }
    if response_format is not None:
        kwargs["response_format"] = response_format

    response = await litellm.acompletion(**kwargs)

    text = response["choices"][0]["message"]["content"] or ""
    usage = response.get("usage") or {}
    return (
        text,
        int(usage.get("prompt_tokens") or 0),
        int(usage.get("completion_tokens") or 0),
    )


async def _complete_with_retries(
    *,
    model: str,
    messages: list[dict[str, Any]],
    temperature: float,
    prompt_version: str,
    context: LlmCallContext,
    response_format: dict | None = None,
    response_format_name: str = "text",
    attempt_offset: int = 0,
    max_attempts: int = 3,
) -> CompletionResult:
    """Run the completion, retrying on transient errors and logging every attempt."""

    last_error: Exception | None = None
    for relative_attempt in range(1, max_attempts + 1):
        killswitch.raise_if_paused()
        start = time.monotonic()
        attempt_absolute = attempt_offset + relative_attempt
        try:
            text, tokens_in, tokens_out = await _do_completion(
                model=model,
                messages=messages,
                temperature=temperature,
                response_format=response_format,
            )
        except Exception as exc:  # pragma: no cover - provider-specific
            last_error = exc
            latency_ms = int((time.monotonic() - start) * 1000)
            status = _status_code(exc)
            _log_call(
                context=context,
                model=model,
                prompt_version=prompt_version,
                temperature=temperature,
                response_format=response_format_name,
                attempt=attempt_absolute,
                messages=messages,
                response_text=None,
                response_parsed=None,
                tokens_in=0,
                tokens_out=0,
                latency_ms=latency_ms,
                error=f"{type(exc).__name__}: {exc}",
            )
            if status in _RETRYABLE_STATUS and relative_attempt < max_attempts:
                backoff = min(2 ** (relative_attempt - 1), 8)
                logger.warning(
                    "LLM call %s (%s) failed (status=%s, attempt=%d); retrying in %ds",
                    model,
                    context.purpose,
                    status,
                    attempt_absolute,
                    backoff,
                )
                await asyncio.sleep(backoff)
                continue
            raise LLMError(f"LLM call to {model} failed: {exc}") from exc
        else:
            latency_ms = int((time.monotonic() - start) * 1000)
            _record_usage(model, tokens_in, tokens_out)
            call_id = _log_call(
                context=context,
                model=model,
                prompt_version=prompt_version,
                temperature=temperature,
                response_format=response_format_name,
                attempt=attempt_absolute,
                messages=messages,
                response_text=text,
                response_parsed=None,
                tokens_in=tokens_in,
                tokens_out=tokens_out,
                latency_ms=latency_ms,
                error=None,
            )
            return CompletionResult(
                text=text,
                model=model,
                prompt_version=prompt_version,
                tokens_in=tokens_in,
                tokens_out=tokens_out,
                attempts=attempt_absolute,
                latency_ms=latency_ms,
                llm_call_id=call_id,
            )

    raise LLMError(
        f"LLM call to {model} failed after {max_attempts} attempts: {last_error}"
    )


def _status_code(exc: Exception) -> int | None:
    status = getattr(exc, "status_code", None)
    if isinstance(status, int):
        return status
    resp = getattr(exc, "response", None)
    if resp is not None and hasattr(resp, "status_code"):
        with suppress(Exception):
            return int(resp.status_code)
    return None


# ---------------------------------------------------------------------------
# Public helpers
# ---------------------------------------------------------------------------


async def complete_text(
    *,
    system: str,
    user: str,
    model: str,
    prompt_version: str,
    temperature: float = 1.0,
    context: LlmCallContext | None = None,
) -> CompletionResult:
    """Free-form text completion."""

    ctx = context or LlmCallContext()
    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]
    return await _complete_with_retries(
        model=model,
        messages=messages,
        temperature=temperature,
        prompt_version=prompt_version,
        context=ctx,
        response_format_name="text",
    )


_JSON_OBJECT_RE = re.compile(r"\{.*\}", re.DOTALL)


def _extract_json(text: str) -> dict[str, Any]:
    """Parse a JSON object from a model response.

    Tolerates leading / trailing whitespace and ``` fences. Raises
    :class:`ValueError` if no JSON object is found.
    """

    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = stripped.strip("`")
        if stripped.lower().startswith("json"):
            stripped = stripped[4:]
        stripped = stripped.strip()

    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        pass

    match = _JSON_OBJECT_RE.search(stripped)
    if match is None:
        raise ValueError("response contained no JSON object")
    return json.loads(match.group(0))


async def complete_json(
    *,
    system: str,
    user: str,
    model: str,
    prompt_version: str,
    temperature: float = 1.0,
    context: LlmCallContext | None = None,
) -> tuple[dict[str, Any], CompletionResult]:
    """JSON-structured completion with one self-heal retry on parse failure."""

    ctx = context or LlmCallContext()
    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]
    result = await _complete_with_retries(
        model=model,
        messages=messages,
        temperature=temperature,
        prompt_version=prompt_version,
        context=ctx,
        response_format={"type": "json_object"},
        response_format_name="json",
    )
    try:
        parsed = _extract_json(result.text)
        _update_parsed_json(result.llm_call_id, parsed)
        return parsed, result
    except (ValueError, json.JSONDecodeError) as exc:
        logger.warning("JSON parse failure from %s: %s — retrying with self-heal", model, exc)

    heal_messages = messages + [
        {"role": "assistant", "content": result.text},
        {
            "role": "user",
            "content": (
                "Your last response was not valid JSON. "
                "Respond again with a single valid JSON object and nothing else."
            ),
        },
    ]
    retry = await _complete_with_retries(
        model=model,
        messages=heal_messages,
        temperature=temperature,
        prompt_version=prompt_version,
        context=ctx,
        response_format={"type": "json_object"},
        response_format_name="json",
        attempt_offset=result.attempts,
    )
    try:
        parsed = _extract_json(retry.text)
        _update_parsed_json(retry.llm_call_id, parsed)
        return parsed, retry
    except (ValueError, json.JSONDecodeError) as exc:
        raise LLMError(f"LLM returned unparseable JSON after self-heal: {exc}") from exc


def _update_parsed_json(call_id: str | None, parsed: dict[str, Any]) -> None:
    """Backfill the ``response_parsed`` column once JSON parsing has succeeded."""

    if not call_id:
        return
    try:
        with session_scope() as session:
            row = session.get(LlmCall, call_id)
            if row is not None:
                row.response_parsed = parsed
    except Exception:  # pragma: no cover - best effort
        logger.exception("failed to backfill response_parsed on llm_call %s", call_id)
