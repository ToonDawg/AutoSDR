"""Kill switch — the three layers and the hot-path guard."""

from __future__ import annotations

import asyncio

import pytest

from autosdr import killswitch


def test_flag_file_roundtrip():
    assert not killswitch.is_flag_set()
    path = killswitch.touch_flag()
    assert path.exists()
    assert killswitch.is_flag_set()
    assert killswitch.is_paused()

    with pytest.raises(killswitch.KillSwitchTripped):
        killswitch.raise_if_paused()

    assert killswitch.remove_flag() is True
    assert not killswitch.is_flag_set()
    assert not killswitch.is_paused()


def test_remove_flag_when_absent():
    assert killswitch.remove_flag() is False


def test_pid_file_roundtrip():
    assert killswitch.read_pid_file() is None
    killswitch.write_pid_file()
    assert killswitch.read_pid_file() is not None
    killswitch.clear_pid_file()
    assert killswitch.read_pid_file() is None


async def test_await_shutdown_or_timeout_wakes_on_event():
    event = killswitch.shutdown_event()

    async def _trigger():
        await asyncio.sleep(0.05)
        event.set()

    asyncio.create_task(_trigger())
    fired = await killswitch.await_shutdown_or_timeout(2.0)
    assert fired is True


async def test_await_shutdown_or_timeout_times_out():
    fired = await killswitch.await_shutdown_or_timeout(0.05)
    assert fired is False


async def test_watch_flag_file_exits_on_shutdown():
    event = killswitch.shutdown_event()

    async def _trigger():
        await asyncio.sleep(0.05)
        event.set()

    asyncio.create_task(_trigger())
    # Should return before 5s because the event fires.
    await asyncio.wait_for(killswitch.watch_flag_file(poll_interval_s=0.1), timeout=2.0)
