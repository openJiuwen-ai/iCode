# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from browser_use.broker_client import BrokerClient
from browser_use.config import BrowserConfig
from browser_use.errors import Unavailable


def _cfg(tmp_path: Path) -> BrowserConfig:
    return BrowserConfig(allowed_domains=("example.com",), state_dir=tmp_path)


def _write(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def test_dead_broker_discovery_is_removed(tmp_path: Path) -> None:
    path = tmp_path / "broker" / "broker.json"
    _write(path, {"pid": 999_999_999, "port": 1, "token": "x", "protocol_version": 1})
    client = BrokerClient(_cfg(tmp_path), owner="o", session_id="s")
    with pytest.raises(Unavailable):
        asyncio.run(client.acquire())
    assert not path.exists()


def test_malformed_broker_discovery_is_removed(tmp_path: Path) -> None:
    path = tmp_path / "broker" / "broker.json"
    path.parent.mkdir(parents=True)
    path.write_text("{not json", encoding="utf-8")
    client = BrokerClient(_cfg(tmp_path), owner="o", session_id="s")
    with pytest.raises(Unavailable):
        asyncio.run(client.acquire())
    assert not path.exists()


def test_broker_with_different_allowed_origins_is_refused(tmp_path: Path) -> None:
    import os

    path = tmp_path / "broker" / "broker.json"
    _write(
        path,
        {
            "pid": os.getpid(),
            "port": 1,
            "token": "x",
            "protocol_version": 1,
            "allowed_domains": ["example.com"],
            "allowed_origins": [],
            "headless": False,
            "bridge": False,
            "evaluate": False,
        },
    )
    client = BrokerClient(
        BrowserConfig(
            allowed_domains=("example.com",),
            allowed_origins=("http://localhost:3000",),
            state_dir=tmp_path,
        ),
        owner="o",
        session_id="s",
    )
    with pytest.raises(Unavailable, match="config differs"):
        asyncio.run(client.acquire())
    assert path.exists()
