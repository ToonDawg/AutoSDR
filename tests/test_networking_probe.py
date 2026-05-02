"""Tailscale probe + bind-interface warning — ticket 0005 unit 8.

Covers:

* :func:`probe_tailscale_state` returns ``not_detected`` (never raises)
  when the binary is not on PATH.
* :func:`probe_tailscale_state` returns ``running`` when ``tailscale
  status`` exits 0.
* :func:`probe_tailscale_state` returns ``not_detected`` when the
  shelled-out subprocess raises ``OSError`` (sandbox / permission).
* :func:`probe_tailscale_state` clamps a misbehaving subprocess via
  the ``TimeoutExpired`` branch.
* :func:`networking_status` produces the *PC-bind-interface footgun*
  warning when ``HOST=127.0.0.1`` and Tailscale is up; produces
  ``warning=None`` when AutoSDR is bound for remote access.
* :func:`log_host_bind_warning` emits exactly one ``warning`` log
  line in the footgun case and zero log lines otherwise.
"""

from __future__ import annotations

import logging
import subprocess
from unittest.mock import patch

import pytest

from autosdr.networking import (
    TailscaleProbe,
    log_host_bind_warning,
    networking_status,
    probe_tailscale_state,
)


def test_probe_returns_not_detected_when_binary_missing():
    with patch("autosdr.networking.shutil.which", return_value=None):
        result = probe_tailscale_state()
    assert result.state == "not_detected"
    assert "PATH" in (result.detail or "")


def test_probe_returns_running_when_subprocess_exits_zero():
    fake = subprocess.CompletedProcess(
        args=["tailscale", "status"],
        returncode=0,
        stdout="100.64.0.1   autosdr-pc   user@example   linux   -\n",
        stderr="",
    )
    with patch("autosdr.networking.shutil.which", return_value="/usr/bin/tailscale"), \
        patch("autosdr.networking.subprocess.run", return_value=fake):
        result = probe_tailscale_state()
    assert result.state == "running"
    assert "100.64.0.1" in (result.detail or "")


def test_probe_returns_not_detected_when_subprocess_raises_oserror():
    with patch("autosdr.networking.shutil.which", return_value="/usr/bin/tailscale"), \
        patch(
            "autosdr.networking.subprocess.run",
            side_effect=OSError("permission denied"),
        ):
        result = probe_tailscale_state()
    assert result.state == "not_detected"
    assert "permission denied" in (result.detail or "")


def test_probe_returns_not_detected_on_timeout():
    with patch("autosdr.networking.shutil.which", return_value="/usr/bin/tailscale"), \
        patch(
            "autosdr.networking.subprocess.run",
            side_effect=subprocess.TimeoutExpired(cmd="tailscale status", timeout=1.5),
        ):
        result = probe_tailscale_state()
    assert result.state == "not_detected"
    assert "timed out" in (result.detail or "")


def test_networking_status_warns_when_localhost_and_tailscale_up(monkeypatch):
    monkeypatch.setenv("HOST", "127.0.0.1")
    with patch(
        "autosdr.networking.probe_tailscale_state",
        return_value=TailscaleProbe(state="running", detail="100.64.0.1"),
    ):
        state = networking_status()
    assert state.host == "127.0.0.1"
    assert state.bound_for_remote_access is False
    assert state.tailscale.state == "running"
    assert state.warning is not None
    assert "HOST=0.0.0.0" in state.warning


def test_networking_status_no_warning_when_bound_for_remote(monkeypatch):
    monkeypatch.setenv("HOST", "0.0.0.0")
    with patch(
        "autosdr.networking.probe_tailscale_state",
        return_value=TailscaleProbe(state="running"),
    ):
        state = networking_status()
    assert state.bound_for_remote_access is True
    assert state.warning is None


def test_networking_status_no_warning_when_tailscale_not_detected(monkeypatch):
    """Probe failure must NOT produce a false-positive warning."""

    monkeypatch.setenv("HOST", "127.0.0.1")
    with patch(
        "autosdr.networking.probe_tailscale_state",
        return_value=TailscaleProbe(state="not_detected"),
    ):
        state = networking_status()
    assert state.warning is None


def test_log_host_bind_warning_emits_single_warning_in_footgun(
    monkeypatch, caplog: pytest.LogCaptureFixture
):
    monkeypatch.setenv("HOST", "127.0.0.1")
    caplog.set_level(logging.WARNING, logger="autosdr.networking")
    with patch(
        "autosdr.networking.probe_tailscale_state",
        return_value=TailscaleProbe(state="running"),
    ):
        log_host_bind_warning()
    warnings = [r for r in caplog.records if r.levelname == "WARNING"]
    assert len(warnings) == 1
    assert "HOST=0.0.0.0" in warnings[0].getMessage()


def test_log_host_bind_warning_silent_when_safe(
    monkeypatch, caplog: pytest.LogCaptureFixture
):
    monkeypatch.setenv("HOST", "0.0.0.0")
    caplog.set_level(logging.WARNING, logger="autosdr.networking")
    with patch(
        "autosdr.networking.probe_tailscale_state",
        return_value=TailscaleProbe(state="running"),
    ):
        log_host_bind_warning()
    assert not [r for r in caplog.records if r.levelname == "WARNING"]
