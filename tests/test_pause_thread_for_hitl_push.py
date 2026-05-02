"""HITL escalation seam → Web Push fanout — ticket 0005 unit 5.

This test only covers the new ``schedule_hitl_push`` helper that wraps
``pause_thread_for_hitl`` at every escalation site. The full pipeline-
level coverage (``test_reply_pipeline.py``,
``test_outreach_pipeline.py``) already pins the HITL state transition;
here we want to assert two narrow contracts:

* On a real escalation (running event loop), the helper schedules a
  fanout task that calls into :func:`autosdr.push.fanout_hitl_push`
  with the right lead name + thread id + reason.
* If :func:`fanout_hitl_push` raises (transient gateway failure,
  programming error in a future SW update), the background task
  swallows the exception so the reply pipeline never observes it.
* In a sync context (no running loop — replay scripts, CLI smoke
  tools, sync test code), the helper silently no-ops.
"""

from __future__ import annotations

import asyncio
from unittest.mock import patch

import pytest

from autosdr.pipeline._shared import schedule_hitl_push


def test_schedule_hitl_push_is_noop_outside_event_loop():
    """A sync caller (CLI / replay script / sync test) doesn't raise."""

    with patch("autosdr.pipeline._shared.fanout_hitl_push") as mock:
        schedule_hitl_push(
            thread_id="t-1",
            lead_name="Sarah Chen",
            hitl_reason="confused",
        )
    assert mock.call_count == 0


@pytest.mark.asyncio
async def test_schedule_hitl_push_dispatches_in_event_loop():
    """With a running loop, the helper fires fanout in the background."""

    completed = asyncio.Event()
    captured: dict[str, object] = {}

    async def _fake_fanout(**kwargs):
        captured.update(kwargs)
        completed.set()
        return 1

    with patch(
        "autosdr.pipeline._shared.fanout_hitl_push",
        side_effect=_fake_fanout,
    ):
        schedule_hitl_push(
            thread_id="t-7",
            lead_name="Sarah Chen",
            hitl_reason="connector_send_failed",
        )
        await asyncio.wait_for(completed.wait(), timeout=1.0)

    assert captured["thread_id"] == "t-7"
    assert captured["lead_name"] == "Sarah Chen"
    assert captured["hitl_reason"] == "connector_send_failed"
    assert captured["escalated_at"] is not None


@pytest.mark.asyncio
async def test_schedule_hitl_push_swallows_fanout_failures():
    """A flaky push gateway must not propagate into the reply pipeline."""

    seen = asyncio.Event()
    callback_calls: list[BaseException | None] = []

    async def _boom(**_kwargs):
        seen.set()
        raise RuntimeError("push gateway 502")

    def _capture_done(task: asyncio.Task) -> None:
        callback_calls.append(task.exception())

    with patch("autosdr.pipeline._shared.fanout_hitl_push", side_effect=_boom):
        # We patch the done-callback to verify the task observed the
        # exception (and thus the production logger ran with it). If the
        # exception had propagated up out of the helper / scheduling
        # call, we'd never reach this point.
        with patch(
            "autosdr.pipeline._shared._on_push_task_done", _capture_done
        ):
            schedule_hitl_push(
                thread_id="t-9",
                lead_name="Test Lead",
                hitl_reason="eval_failed_after_max_attempts",
            )
            await asyncio.wait_for(seen.wait(), timeout=1.0)
            await asyncio.sleep(0)

    assert callback_calls, "fanout task callback was never invoked"
    exc = callback_calls[0]
    assert isinstance(exc, RuntimeError)
    assert "push gateway 502" in str(exc)
