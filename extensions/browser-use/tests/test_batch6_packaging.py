# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Unit regression tests for Batch 6 config, CLI, install, chunking & diagnostics."""

from __future__ import annotations

import base64
import json
import os
from pathlib import Path

import pytest

from icode_browser_use import config, diagnose, distribution, logging as plugin_log, policy
from icode_browser_use.bridge import install as bridge_install
from icode_browser_use.bridge.protocol import ChunkAssembler, ProtocolError


def test_ipv6_non_canonical_origin_normalisation_and_localhost_domain_rejection() -> None:
    assert config.normalise_origin("http://[0:0:0:0:0:0:0:1]:3000") == "http://[::1]:3000"
    assert policy.canonical_origin("http://[0:0:0:0:0:0:0:1]:3000/a") == "http://[::1]:3000"
    assert config.is_valid_domain("app.localhost") is False
    assert config.is_valid_domain(".localhost") is False
    assert config.is_valid_domain("localhost") is False
    assert config.is_valid_domain("example.com") is True


def test_state_dir_strips_whitespace_and_bridge_file_expands_user() -> None:
    env = {
        "ICODE_BROWSER_STATE_DIR": "  ~/custom-state  ",
        "ICODE_BROWSER_BRIDGE_FILE": "~/custom-bridge.json",
    }
    cfg = config.BrowserConfig.from_env(env)
    assert cfg.state_dir == Path("~/custom-state").expanduser()
    assert cfg.bridge_file == str(Path("~/custom-bridge.json").expanduser())


def test_from_env_uses_log_sink_instead_of_printing_stderr(capsys: pytest.CaptureFixture[str]) -> None:
    captured: list[str] = []
    previous_sink = plugin_log._LOG_SINK
    plugin_log.set_log_sink(captured.append)
    try:
        cfg = config.BrowserConfig.from_env({"ICODE_BROWSER_ALLOWED_DOMAINS": "example.com,bad_host"})
        assert cfg.allowed_domains == ("example.com",)
    finally:
        plugin_log.set_log_sink(previous_sink)
    assert capsys.readouterr().err == ""
    assert any("ignoring 1 invalid allowlist entries" in line for line in captured)


def test_install_extension_and_pack_safety_guards(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(distribution, "wheel_entry_point", lambda: None)
    src = bridge_install.packaged_extension_dir()
    with pytest.raises(ValueError, match="different directories"):
        bridge_install.install_extension(src, src)

    unrelated = tmp_path / "user-docs"
    unrelated.mkdir()
    (unrelated / "important.txt").write_text("keep", encoding="utf-8")
    with pytest.raises(ValueError, match="manifest.json"):
        bridge_install.install_extension(src, unrelated)
    assert (unrelated / "important.txt").is_file()

    fake_pack = tmp_path / "pack"
    (fake_pack / "icode_browser_use").mkdir(parents=True)
    (fake_pack / "__init__.py").write_text("", encoding="utf-8")
    (fake_pack / "icode_browser_use" / "__init__.py").write_text("", encoding="utf-8")

    cfg_dir = tmp_path / "cfg"
    target = cfg_dir / "extensions" / "browser"
    target.mkdir(parents=True)
    (target / "not-an-ext.txt").write_text("keep", encoding="utf-8")
    with pytest.raises(ValueError, match="installed extension folder"):
        distribution.install_pack(fake_pack, config_dir=cfg_dir, replace=True)
    assert (target / "not-an-ext.txt").is_file()

    (target / "icode_browser_use").mkdir(parents=True)
    (target / "__init__.py").write_text("", encoding="utf-8")
    (target / "icode_browser_use" / "__init__.py").write_text("", encoding="utf-8")
    with pytest.raises(ValueError, match="different directories"):
        distribution.install_pack(target, config_dir=cfg_dir, replace=True)


def test_chunk_assembler_rejects_duplicate_index_and_excess_pending_ids() -> None:
    asm = ChunkAssembler()
    piece = base64.b64encode(b'{"a":1}').decode("ascii")
    assert asm.add({"__bridge_chunk": {"id": "c1", "index": 0, "total": 2}, "data": piece}) is None
    with pytest.raises(ProtocolError, match="duplicate"):
        asm.add({"__bridge_chunk": {"id": "c1", "index": 0, "total": 2}, "data": piece})

    asm2 = ChunkAssembler()
    for i in range(8):
        assert asm2.add({"__bridge_chunk": {"id": f"m{i}", "index": 0, "total": 2}, "data": piece}) is None
    with pytest.raises(ProtocolError, match="too many concurrent"):
        asm2.add({"__bridge_chunk": {"id": "m8", "index": 0, "total": 2}, "data": piece})


def test_doctor_external_live_broker_downgrades_broker_unsupported_to_warning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    broker_dir = tmp_path / "broker"
    broker_dir.mkdir(parents=True)
    (broker_dir / "broker.json").write_text(
        json.dumps(
            {
                "port": 12345,
                "token": "t",
                "pid": os.getpid(),
                "protocol_version": 1,
                "allowed_domains": [],
                "allowed_origins": [],
                "headless": False,
                "bridge": False,
                "evaluate": False,
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(distribution, "broker_command", lambda **_kw: None)
    cfg = config.BrowserConfig(state_dir=tmp_path, broker=True, broker_autostart=False)
    _section, problems = diagnose._broker(cfg, config.BrowserEnv({}), "broker", False)
    unsupported = [p for p in problems if p["code"] == "broker_unsupported"]
    assert len(unsupported) == 1
    assert unsupported[0]["severity"] == "warning"


def test_install_pack_normal_replace_rollback_and_symlink_refusal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(distribution, "wheel_entry_point", lambda: None)

    def _make_pack(root: Path, marker: str) -> Path:
        (root / "icode_browser_use").mkdir(parents=True, exist_ok=True)
        (root / "__init__.py").write_text(f"# {marker}\n", encoding="utf-8")
        (root / "icode_browser_use" / "__init__.py").write_text(f"VERSION = {marker!r}\n", encoding="utf-8")
        return root

    pack_v1 = _make_pack(tmp_path / "pack_v1", "v1")
    pack_v2 = _make_pack(tmp_path / "pack_v2", "v2")
    cfg_dir = tmp_path / "cfg"

    # Initial install + normal replace
    installed = distribution.install_pack(pack_v1, config_dir=cfg_dir, replace=False)
    assert "v1" in (installed / "icode_browser_use" / "__init__.py").read_text(encoding="utf-8")
    replaced = distribution.install_pack(pack_v2, config_dir=cfg_dir, replace=True)
    assert "v2" in (replaced / "icode_browser_use" / "__init__.py").read_text(encoding="utf-8")
    assert not list((cfg_dir / "extensions").glob("*.bak-*"))

    # Rollback when copy fails mid-replace preserves v2 intact
    def _boom(*_a: object, **_kw: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(distribution.shutil, "copytree", _boom)
    with pytest.raises(OSError, match="disk full"):
        distribution.install_pack(pack_v1, config_dir=cfg_dir, replace=True)
    assert "v2" in (replaced / "icode_browser_use" / "__init__.py").read_text(encoding="utf-8")
    monkeypatch.undo()
    monkeypatch.setattr(distribution, "wheel_entry_point", lambda: None)

    # Symlink target pointing to a developer checkout must be refused without deleting checkout!
    dev_checkout = _make_pack(tmp_path / "dev_checkout", "dev")
    (dev_checkout / "UNCOMMITTED.txt").write_text("precious", encoding="utf-8")
    link_cfg = tmp_path / "link_cfg"
    (link_cfg / "extensions").mkdir(parents=True)
    link_target = link_cfg / "extensions" / "browser"
    try:
        link_target.symlink_to(dev_checkout, target_is_directory=True)
    except OSError:
        # Symlink creation may require Developer Mode on some Windows configs
        pass
    else:
        with pytest.raises(ValueError, match="symlink|junction"):
            distribution.install_pack(pack_v2, config_dir=link_cfg, replace=True)
        assert (dev_checkout / "UNCOMMITTED.txt").read_text(encoding="utf-8") == "precious"


def test_extension_id_regex_and_doctor_host_sibling_lookup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import sys

    assert bridge_install._EXTENSION_ID_RE.match("a" * 32) is not None
    assert bridge_install._EXTENSION_ID_RE.match("z" * 32) is None
    assert bridge_install._EXTENSION_ID_RE.match("a" * 31) is None

    bin_dir = tmp_path / "venv" / "bin"
    bin_dir.mkdir(parents=True)
    fake_py = bin_dir / ("python.exe" if sys.platform == "win32" else "python")
    fake_py.write_text("", encoding="utf-8")
    host_name = "icode-browser-use-host.exe" if sys.platform == "win32" else "icode-browser-use-host"
    fake_host = bin_dir / host_name
    fake_host.write_text("", encoding="utf-8")

    monkeypatch.setenv("PATH", "")
    monkeypatch.setattr(diagnose.sys, "executable", str(fake_py))
    found = diagnose._find_host_executable()
    assert found is not None and Path(found) == fake_host.resolve()
