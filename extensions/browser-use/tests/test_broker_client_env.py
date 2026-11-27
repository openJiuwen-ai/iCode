# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

from __future__ import annotations

import pytest

from icode_browser_use.broker_client import BrokerClient, _requested_wait_seconds
from icode_browser_use.config import BrowserConfig
from icode_browser_use.errors import Unavailable


def test_spawn_overrides_inherited_names(tmp_path, monkeypatch):
    monkeypatch.setenv("ICODE_BROWSER_STATE_DIR", "wrong-state")
    monkeypatch.setenv("ICODE_BROWSER_BROKER", "1")
    monkeypatch.setenv("ICODE_BROWSER_ALLOWED_DOMAINS", "wrong.example")
    monkeypatch.setenv("ICODE_BROWSER_ALLOWED_ORIGINS", "http://localhost:9999")
    captured = {}

    def popen(*args, **kwargs):
        captured.update(kwargs["env"])

    monkeypatch.setattr("icode_browser_use.broker_client.harden_private", lambda *_a, **_kw: True)
    monkeypatch.setattr("icode_browser_use.broker_client.subprocess.Popen", popen)
    cfg = BrowserConfig(state_dir=tmp_path, allowed_domains=("example.com",))
    BrokerClient(cfg)._spawn()
    child = BrowserConfig.from_env(captured)
    assert child.state_dir == tmp_path
    assert child.broker is False
    assert child.allowed_domains == ("example.com",)
    # An inherited origin grant must not leak into a broker started with none.
    assert child.allowed_origins == ()
    assert captured["ICODE_BROWSER_STATE_DIR"] == str(tmp_path)


def test_spawn_passes_configured_origins(tmp_path, monkeypatch):
    captured = {}

    def popen(*args, **kwargs):
        captured.update(kwargs["env"])

    monkeypatch.setattr("icode_browser_use.broker_client.harden_private", lambda *_a, **_kw: True)
    monkeypatch.setattr("icode_browser_use.broker_client.subprocess.Popen", popen)
    cfg = BrowserConfig(
        state_dir=tmp_path,
        allowed_domains=("example.com",),
        allowed_origins=("http://localhost:3000", "http://[::1]:8080"),
    )
    BrokerClient(cfg)._spawn()
    child = BrowserConfig.from_env(captured)
    assert child.allowed_origins == ("http://localhost:3000", "http://[::1]:8080")


def test_spawn_refuses_when_this_runtime_cannot_launch_python(tmp_path, monkeypatch):
    monkeypatch.setattr("icode_browser_use.broker_client.broker_command", lambda **_: None)
    cfg = BrowserConfig(state_dir=tmp_path, allowed_domains=("example.com",), broker_autostart=True)
    with pytest.raises(Unavailable) as info:
        BrokerClient(cfg)._spawn()
    assert "folder package" in str(info.value)
    assert "Owned or Attach" in str(info.value)
    assert not (tmp_path / "broker" / "broker.log").exists(), "a refused spawn must not leave a log"


def test_the_read_timeout_outlasts_a_tool_wait_budget() -> None:
    """browser_wait_for may legitimately wait 180 s; the Broker read must not give up at the static floor."""
    assert _requested_wait_seconds({"arguments": {"timeout_ms": 180_000}}) == 180.0
    assert _requested_wait_seconds({"arguments": {"timeout_ms": 1500.5}}) == 1.5005
    assert _requested_wait_seconds({"arguments": {"timeout_ms": 10**9}}) == 600.0, "capped: a bad value cannot hang"
    for fields in ({}, {"arguments": None}, {"arguments": {}}, {"arguments": {"timeout_ms": "180000"}}):
        assert _requested_wait_seconds(fields) == 0.0
    assert _requested_wait_seconds({"arguments": {"timeout_ms": True}}) == 0.0
    assert _requested_wait_seconds({"arguments": {"timeout_ms": -5}}) == 0.0
