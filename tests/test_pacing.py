"""Unit tests for ``autosdr.pacing`` — window resolution + allowance maths.

These are pure-function tests, no DB. The DB-touching half
(``count_sends_in_today_window``) is exercised by the scheduler
integration tests in ``tests/test_scheduler_window.py`` instead.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from autosdr.pacing import (
    OutreachWindow,
    is_in_window,
    resolve_window,
    today_window_bounds,
    window_allowance,
)


# ---------------------------------------------------------------------------
# resolve_window
# ---------------------------------------------------------------------------


def test_resolve_falls_back_to_hardcoded_default_when_nothing_set():
    window = resolve_window(campaign_window=None, workspace_settings=None)
    assert window == OutreachWindow(enabled=True, start_hour=8, end_hour=17)


def test_resolve_uses_workspace_default_when_no_campaign_override():
    ws = {"outreach_window": {"enabled": True, "start_hour": 9, "end_hour": 18}}
    window = resolve_window(campaign_window=None, workspace_settings=ws)
    assert window.start_hour == 9
    assert window.end_hour == 18


def test_resolve_prefers_campaign_over_workspace():
    ws = {"outreach_window": {"enabled": True, "start_hour": 9, "end_hour": 18}}
    cw = {"enabled": True, "start_hour": 6, "end_hour": 22}
    window = resolve_window(campaign_window=cw, workspace_settings=ws)
    assert window.start_hour == 6
    assert window.end_hour == 22


def test_resolve_disabled_campaign_overrides_enabled_workspace():
    """A campaign that explicitly disables the window beats the workspace default."""

    ws = {"outreach_window": {"enabled": True, "start_hour": 8, "end_hour": 17}}
    cw = {"enabled": False, "start_hour": 8, "end_hour": 17}
    window = resolve_window(campaign_window=cw, workspace_settings=ws)
    assert window.enabled is False


def test_resolve_clamps_pathological_hours():
    cw = {"enabled": True, "start_hour": -3, "end_hour": 99}
    window = resolve_window(campaign_window=cw, workspace_settings=None)
    assert 0 <= window.start_hour <= 23
    assert 1 <= window.end_hour <= 24


def test_resolve_string_hours_coerced():
    """The frontend posts numeric inputs as strings; we should accept them."""

    cw = {"enabled": True, "start_hour": "9", "end_hour": "17"}
    window = resolve_window(campaign_window=cw, workspace_settings=None)
    assert window.start_hour == 9
    assert window.end_hour == 17


def test_resolve_inverted_hours_get_a_minimum_one_hour_window():
    """``end <= start`` shouldn't crash or produce a zero-width window."""

    cw = {"enabled": True, "start_hour": 17, "end_hour": 8}
    window = resolve_window(campaign_window=cw, workspace_settings=None)
    assert window.end_hour > window.start_hour


def test_resolve_empty_blob_falls_through_to_workspace():
    """Empty ``{}`` on the campaign means inherit, not 'reset to defaults'."""

    ws = {"outreach_window": {"enabled": True, "start_hour": 9, "end_hour": 18}}
    window = resolve_window(campaign_window={}, workspace_settings=ws)
    assert window.start_hour == 9
    assert window.end_hour == 18


# ---------------------------------------------------------------------------
# today_window_bounds + is_in_window
# ---------------------------------------------------------------------------


def _local(hour: int, minute: int = 0) -> datetime:
    """Build a tz-aware local datetime at a fixed reference date."""

    return datetime(2026, 4, 28, hour, minute, tzinfo=timezone.utc)


def test_today_window_bounds_uses_window_hours():
    window = OutreachWindow(enabled=True, start_hour=8, end_hour=17)
    start, end = today_window_bounds(window, _local(12))
    assert start == _local(8)
    assert end == _local(17)


def test_is_in_window_inclusive_start_exclusive_end():
    window = OutreachWindow(enabled=True, start_hour=8, end_hour=17)
    assert is_in_window(window, _local(8))
    assert is_in_window(window, _local(8, 1))
    assert is_in_window(window, _local(16, 59))
    assert not is_in_window(window, _local(7, 59))
    assert not is_in_window(window, _local(17))
    assert not is_in_window(window, _local(23))


# ---------------------------------------------------------------------------
# window_allowance
# ---------------------------------------------------------------------------


def _window() -> OutreachWindow:
    return OutreachWindow(enabled=True, start_hour=8, end_hour=17)


def test_allowance_zero_outside_window_morning():
    allowance = window_allowance(
        window=_window(),
        daily_quota=50,
        sent_in_window=0,
        now_local=_local(7, 30),
    )
    assert allowance == 0


def test_allowance_zero_outside_window_evening():
    allowance = window_allowance(
        window=_window(),
        daily_quota=50,
        sent_in_window=0,
        now_local=_local(18),
    )
    assert allowance == 0


def test_allowance_disabled_window_returns_full_quota():
    """``enabled=False`` is the escape hatch — pace nothing, just gate on 24h quota."""

    window = OutreachWindow(enabled=False, start_hour=8, end_hour=17)
    allowance = window_allowance(
        window=window,
        daily_quota=50,
        sent_in_window=0,
        now_local=_local(3),
    )
    assert allowance == 50


def test_allowance_at_window_start_allows_one_send():
    """At t = window_start, ``ceil(50 * 0)`` is 0, but the next instant is 1."""

    window = _window()
    # Right on the boundary: zero elapsed so target_sent is 0.
    at_start = window_allowance(
        window=window,
        daily_quota=50,
        sent_in_window=0,
        now_local=_local(8, 0),
    )
    assert at_start == 0
    # One second in, ceil() gives us our first send.
    just_after = window_allowance(
        window=window,
        daily_quota=50,
        sent_in_window=0,
        now_local=_local(8, 0) + timedelta(seconds=1),
    )
    assert just_after == 1


def test_allowance_at_midpoint_targets_half_the_quota():
    """At 12:30 (half-way through 8–17), target should be ceil(50 * 0.5) = 25."""

    window = _window()
    midpoint = _local(12, 30)
    allowance = window_allowance(
        window=window,
        daily_quota=50,
        sent_in_window=0,
        now_local=midpoint,
    )
    assert allowance == 25


def test_allowance_subtracts_already_sent():
    """If we've already sent N, the allowance is target - N."""

    window = _window()
    allowance = window_allowance(
        window=window,
        daily_quota=50,
        sent_in_window=20,
        now_local=_local(12, 30),
    )
    assert allowance == 5  # target=25, already sent 20


def test_allowance_caps_at_zero_when_ahead_of_pace():
    """Sending faster than pace should saturate at 0, not go negative."""

    window = _window()
    allowance = window_allowance(
        window=window,
        daily_quota=50,
        sent_in_window=40,
        now_local=_local(12, 30),
    )
    assert allowance == 0


def test_allowance_capped_at_daily_quota():
    """Even at the very end of the window, allowance can't exceed remaining quota."""

    window = _window()
    allowance = window_allowance(
        window=window,
        daily_quota=10,
        sent_in_window=0,
        now_local=_local(16, 59),
    )
    assert allowance <= 10


def test_allowance_zero_when_quota_zero():
    window = _window()
    allowance = window_allowance(
        window=window,
        daily_quota=0,
        sent_in_window=0,
        now_local=_local(12),
    )
    assert allowance == 0


@pytest.mark.parametrize(
    "now_hour,expected_target",
    [
        (8, 0),       # exactly at start: ceil(50*0) = 0
        (10, 12),     # 2/9 elapsed: ceil(50*2/9) = ceil(11.11) = 12
        (12, 23),     # 4/9 elapsed: ceil(50*4/9) = ceil(22.22) = 23
        (14, 34),     # 6/9 elapsed: ceil(50*6/9) = ceil(33.33) = 34
        (16, 45),     # 8/9 elapsed: ceil(50*8/9) = ceil(44.44) = 45
    ],
)
def test_allowance_pacing_curve_for_50_per_day(now_hour, expected_target):
    window = _window()
    allowance = window_allowance(
        window=window,
        daily_quota=50,
        sent_in_window=0,
        now_local=_local(now_hour),
    )
    assert allowance == expected_target


def test_allowance_short_window_one_hour():
    """Tight 12–13 window stress-tests the maths at the boundaries."""

    window = OutreachWindow(enabled=True, start_hour=12, end_hour=13)
    allowance = window_allowance(
        window=window,
        daily_quota=10,
        sent_in_window=0,
        now_local=_local(12, 30),
    )
    # Half of 10 = 5
    assert allowance == 5
