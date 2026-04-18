"""LLM client layer built on LiteLLM.

Use :func:`complete_text` for free-form generation and :func:`complete_json`
for structured output. Both honour the kill switch, attribute token usage to
the in-memory cost counter, and persist every attempt (including failed
retries and self-heals) to the ``llm_call`` table and ``data/logs/llm-*.jsonl``
for after-the-fact prompt review via ``autosdr logs llm``.

Callers pass an :class:`LlmCallContext` identifying which workspace / campaign
/ thread / lead the call belongs to, so logs can be filtered and stitched.
"""

from autosdr.llm.client import (
    CompletionResult,
    LLMError,
    LlmCallContext,
    complete_json,
    complete_text,
    get_usage_snapshot,
    reset_usage,
)

__all__ = [
    "CompletionResult",
    "LLMError",
    "LlmCallContext",
    "complete_json",
    "complete_text",
    "get_usage_snapshot",
    "reset_usage",
]
