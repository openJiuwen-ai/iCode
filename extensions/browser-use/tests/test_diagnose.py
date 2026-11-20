# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

from __future__ import annotations

import importlib.metadata
import json
import os
import socket
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from browser_use import __version__, diagnose, distribution

DEAD_PID = 2_147_483_647
pytest.importorskip("chrys")  # every case here needs a host to report against


def _env(tmp_path: Path, **overrides: str) -> dict[str, str]:
    env = {
        "ICODE_BROWSER_ALLOWED_DOMAINS": "example.com",
        "ICODE_BROWSER_STATE_DIR": str(tmp_path / "state"),
        "ICODE_BROWSER_EXECUTABLE": str(tmp_path / "chrome.exe"),
    }
    env.update(overrides)
    return env


@pytest.fixture
def sandbox(tmp_path, monkeypatch):
    """Isolate the real user's folders and the optional backends behind temp paths."""
    (tmp_path / "chrome.exe").write_bytes(b"")
    (tmp_path / "state").mkdir()

    def no_entry_points(**_: object) -> tuple[()]:
        return ()

    monkeypatch.setattr(importlib.metadata, "entry_points", no_entry_points)
    monkeypatch.setattr(diagnose, "discover_chrome", lambda: str(tmp_path / "chrome.exe"))
    monkeypatch.setattr(diagnose, "default_extension_install_dir", lambda: tmp_path / "unpacked")
    monkeypatch.setattr(diagnose, "native_manifest_path", lambda: tmp_path / "native-host.json")
    monkeypatch.setattr(diagnose, "registered_manifest", lambda browser: None)
    return tmp_path


def _bridge_running(tmp_path: Path, *, pid: int, port: int) -> None:
    (tmp_path / "state" / "bridge.json").write_text(
        json.dumps({"pid": pid, "port": port, "generation": "abc"}), encoding="utf-8"
    )


def _broker_running(tmp_path: Path, *, pid: int, **extra: object) -> None:
    payload = {
        "pid": pid,
        "port": 1,
        "protocol_version": 1,
        "allowed_domains": ["example.com"],
        "allowed_origins": [],
        "headless": False,
        "bridge": False,
        "evaluate": False,
    }
    payload.update(extra)
    target = tmp_path / "state" / "broker" / "broker.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(payload), encoding="utf-8")


def _codes(document: dict) -> list[str]:
    return [problem["code"] for problem in document["problems"]]


def test_healthy_install_reports_no_errors(sandbox):
    document = diagnose.collect(env=_env(sandbox))

    assert document["schema"] == diagnose.SCHEMA
    assert set(document) == {
        "schema",
        "ok",
        "plugin",
        "host",
        "config",
        "browser",
        "state",
        "bridge",
        "broker",
        "problems",
    }
    assert document["plugin"]["version"] == __version__
    assert document["plugin"]["install"] == "distribution"
    assert document["plugin"]["import"] == "ok"
    assert document["host"]["extension_api"] == "ok"
    assert document["host"]["version"]
    assert document["config"]["ok"] is True
    assert document["config"]["backend"] == "owned"
    assert document["browser"]["executable"] == str(sandbox / "chrome.exe")
    assert document["state"]["writable"] is True
    assert document["broker"]["launchable"] is True
    assert all(problem["severity"] == "warning" for problem in document["problems"])
    assert {"bridge_not_installed", "bridge_not_running", "broker_not_running"} <= set(_codes(document))
    assert document["ok"] is True


def test_unknown_codes_are_declared(sandbox):
    for env in (
        _env(sandbox),
        _env(sandbox, ICODE_BROWSER_ALLOWED_DOMAINS=""),
        _env(sandbox, ICODE_BROWSER_BROKER="1"),
    ):
        assert set(_codes(diagnose.collect(env=env))) <= set(diagnose.CODES)


def test_no_allowlist_reports_public_web(sandbox):
    document = diagnose.collect(env={"ICODE_BROWSER_STATE_DIR": str(sandbox / "state")})

    assert document["config"]["ok"] is True
    assert document["config"]["network"] == "public-web"
    assert "config_invalid" not in _codes(document)


def test_invalid_allowlist_is_an_error(sandbox):
    document = diagnose.collect(env=_env(sandbox, ICODE_BROWSER_ALLOWED_DOMAINS="invalid"))

    assert document["ok"] is False
    assert document["config"]["ok"] is False
    assert "ICODE_BROWSER_ALLOWED_DOMAINS" in document["config"]["error"]
    assert document["config"]["backend"] is None
    assert "config_invalid" in _codes(document)


def test_missing_browser_is_an_error_except_for_attach_and_bridge(sandbox, monkeypatch):
    monkeypatch.setattr(diagnose, "discover_chrome", lambda: None)
    env = {key: value for key, value in _env(sandbox).items() if key != "ICODE_BROWSER_EXECUTABLE"}

    owned = diagnose.collect(env=env)
    assert owned["browser"]["executable"] is None
    assert owned["ok"] is False
    assert "browser_not_found" in _codes(owned)

    attached = diagnose.collect(env={**env, "ICODE_BROWSER_CDP_URL": "http://127.0.0.1:9222"})
    assert attached["config"]["backend"] == "attach"
    assert "browser_not_found" not in _codes(attached)

    # Bridge drives the user's own Chrome through the extension; it launches nothing.
    bridged = diagnose.collect(env={**env, "ICODE_BROWSER_BRIDGE": "1"})
    assert bridged["config"]["backend"] == "bridge"
    assert "browser_not_found" not in _codes(bridged)


def test_a_configured_but_missing_browser_is_reported(sandbox):
    document = diagnose.collect(env=_env(sandbox, ICODE_BROWSER_EXECUTABLE=str(sandbox / "gone.exe")))
    problem = [item for item in document["problems"] if item["code"] == "browser_not_found"]
    assert len(problem) == 1
    assert "ICODE_BROWSER_EXECUTABLE" in problem[0]["message"]
    assert document["browser"]["configured"] is True


def test_invalid_allowlist_entries_are_counted_without_their_values(sandbox):
    document = diagnose.collect(
        env=_env(
            sandbox, ICODE_BROWSER_ALLOWED_DOMAINS="example.com,!!!,http://nope", ICODE_BROWSER_ALLOWED_ORIGINS="x"
        )
    )

    assert document["config"]["ignored_entries"] == 3
    assert "config_entries_ignored" in _codes(document)
    dumped = json.dumps(document)
    assert "!!!" not in dumped and '"x"' not in dumped


def test_unwritable_state_folder_is_an_error(sandbox, monkeypatch):
    monkeypatch.setattr(diagnose.os, "access", lambda path, mode: False)
    document = diagnose.collect(env=_env(sandbox, ICODE_BROWSER_STATE_DIR=str(sandbox / "missing" / "nested")))

    assert document["state"]["exists"] is False
    assert document["state"]["writable"] is False
    assert "state_dir_unwritable" in _codes(document)
    assert document["ok"] is False


def test_one_id_installed_twice_is_reported(sandbox, monkeypatch):
    pack = sandbox / "config" / "extensions" / "browser"
    pack.mkdir(parents=True)
    (pack / "__init__.py").write_text("extension = None\n", encoding="utf-8")
    monkeypatch.setattr(
        importlib.metadata,
        "entry_points",
        lambda **_: (
            importlib.metadata.EntryPoint(
                name="browser", value="browser_use.extension:extension", group="chrys.extensions"
            ),
        ),
    )

    document = diagnose.collect(env=_env(sandbox), config_dir=sandbox / "config")

    conflict = [problem for problem in document["problems"] if problem["code"] == "extension_id_conflict"]
    assert len(conflict) == 1
    assert conflict[0]["severity"] == "error"
    assert str(pack / "__init__.py") in conflict[0]["message"]
    assert "browser_use.extension:extension" in conflict[0]["message"]
    assert document["plugin"]["folder"] == str(pack / "__init__.py")
    assert document["ok"] is False


def test_bridge_discovery_states(sandbox):
    idle = diagnose.collect(env=_env(sandbox))
    assert idle["bridge"]["discovery"] is None
    assert "bridge_not_running" in _codes(idle)

    _bridge_running(sandbox, pid=DEAD_PID, port=1)
    dead = diagnose.collect(env=_env(sandbox))
    assert dead["bridge"]["discovery"]["alive"] is False
    assert dead["bridge"]["state"] == "stale"
    assert "bridge_stale" in _codes(dead)

    _bridge_running(sandbox, pid=os.getpid(), port=1)
    alive = diagnose.collect(env=_env(sandbox))
    assert alive["bridge"]["discovery"]["alive"] is True
    assert alive["bridge"]["state"] == "running"
    assert alive["bridge"]["discovery"]["reachable"] is None


def test_connect_is_opt_in_and_probes_only_existing_files(sandbox, monkeypatch):
    _bridge_running(sandbox, pid=os.getpid(), port=1)

    def refuse(*args, **kwargs):
        raise AssertionError("collect must not open sockets without --connect")

    monkeypatch.setattr(diagnose.socket, "create_connection", refuse)
    document = diagnose.collect(env=_env(sandbox))
    assert document["bridge"]["discovery"]["reachable"] is None


def test_connect_reports_a_refused_bridge(sandbox):
    _bridge_running(sandbox, pid=os.getpid(), port=1)
    document = diagnose.collect(env=_env(sandbox), connect=True)
    assert document["bridge"]["discovery"]["reachable"] is False
    assert document["bridge"]["state"] == "running"
    assert "bridge_unreachable" in _codes(document)


def test_connect_reaches_a_listening_bridge(sandbox):
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    port = listener.getsockname()[1]
    thread = threading.Thread(target=listener.accept, daemon=True)
    thread.start()
    try:
        _bridge_running(sandbox, pid=os.getpid(), port=port)
        document = diagnose.collect(env=_env(sandbox), connect=True)
    finally:
        listener.close()
    assert document["bridge"]["discovery"]["reachable"] is True
    assert document["bridge"]["state"] == "reachable"


def test_bridge_install_state_is_reported(sandbox, monkeypatch):
    unpacked = sandbox / "unpacked"
    unpacked.mkdir()
    (unpacked / "manifest.json").write_text(json.dumps({"version": "0.0.1"}), encoding="utf-8")
    host = sandbox / "browser-use-host"
    host.write_bytes(b"")
    manifest = sandbox / "native-host.json"
    manifest.write_text(json.dumps({"path": str(host), "allowed_origins": []}), encoding="utf-8")
    monkeypatch.setattr(diagnose, "registered_manifest", lambda browser: manifest)

    document = diagnose.collect(env=_env(sandbox))

    assert document["bridge"]["extension_installed"] is True
    assert document["bridge"]["extension_version"] == "0.0.1"
    assert document["bridge"]["host"] == str(host)
    assert document["bridge"]["registered"] == ["chrome", "chromium", "edge"]
    assert document["bridge"]["registered_matches"] is True
    # Installed but not running is its own state; it must not read as "not installed".
    assert document["bridge"]["state"] == "installed"
    assert "bridge_version_mismatch" in _codes(document)
    assert "bridge_not_installed" not in _codes(document)
    assert any(line.startswith("bridge: installed") for line in diagnose.summary(document))


def test_two_folder_layouts_are_reported_as_a_conflict(sandbox):
    root = sandbox / "config" / "extensions"
    (root / "browser").mkdir(parents=True)
    (root / "browser" / "__init__.py").write_text("extension = None\n", encoding="utf-8")
    (root / "browser.py").write_text("extension = None\n", encoding="utf-8")

    document = diagnose.collect(env=_env(sandbox), config_dir=sandbox / "config")

    conflicts = [problem for problem in document["problems"] if problem["code"] == "extension_id_conflict"]
    assert len(conflicts) == 1
    assert "browser.py" in conflicts[0]["message"]
    assert document["ok"] is False


def test_broker_states(sandbox, monkeypatch):
    _broker_running(sandbox, pid=os.getpid())
    healthy = diagnose.collect(env=_env(sandbox))
    assert healthy["broker"]["state"] == "running"
    assert healthy["broker"]["config_matches"] is True
    assert "broker_not_running" not in _codes(healthy)

    _broker_running(sandbox, pid=os.getpid(), allowed_domains=["other.example"])
    differs = diagnose.collect(env=_env(sandbox))
    assert differs["broker"]["config_matches"] is False
    assert "broker_config_differs" in _codes(differs)

    _broker_running(sandbox, pid=DEAD_PID)
    stale = diagnose.collect(env=_env(sandbox))
    assert stale["broker"]["state"] == "stale"
    assert "broker_stale" in _codes(stale)


def test_broker_unreachable_is_reported(sandbox):
    _broker_running(sandbox, pid=os.getpid(), port=1)
    document = diagnose.collect(env=_env(sandbox), connect=True)
    assert document["broker"]["reachable"] is False
    assert "broker_unreachable" in _codes(document)


@pytest.mark.parametrize("relative", [("broker", "broker.json"), ("bridge.json",)])
def test_a_malformed_discovery_file_does_not_crash_the_report(sandbox, relative):
    target = sandbox / "state" / Path(*relative)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps({"pid": "x", "port": []}), encoding="utf-8")

    document = diagnose.collect(env=_env(sandbox), connect=True)

    if relative[0] == "broker":
        assert document["broker"]["reachable"] is None
        assert document["broker"]["state"] == "stale"
    else:
        assert document["bridge"]["discovery"]["reachable"] is None
        assert document["bridge"]["state"] == "stale"


def test_broker_backend_without_a_launcher_is_an_error(sandbox, monkeypatch):
    monkeypatch.setattr(diagnose.distribution, "broker_command", lambda **_: None)
    document = diagnose.collect(env=_env(sandbox, ICODE_BROWSER_BROKER="1"))
    assert document["broker"]["launchable"] is False
    assert "broker_unsupported" in _codes(document)
    assert document["ok"] is False


def test_summary_and_json_exit_codes(sandbox, capsys, monkeypatch):
    document = diagnose.collect(env=_env(sandbox))
    lines = diagnose.summary(document)
    assert lines[0].startswith("plugin: ")
    assert any(line.startswith("browser: ") for line in lines)
    assert lines[-1] == "doctor: ok"

    monkeypatch.setattr(diagnose, "collect", lambda **_: document)
    assert diagnose.main(["--json"]) == 0
    assert json.loads(capsys.readouterr().out)["schema"] == diagnose.SCHEMA

    broken = {
        **document,
        "ok": False,
        "problems": [*document["problems"], diagnose._problem("config_invalid", "m", "a")],
    }
    assert diagnose.summary(broken)[-1] == "doctor: 1 error(s)"
    monkeypatch.setattr(diagnose, "collect", lambda **_: broken)
    assert diagnose.main([]) == 1
    assert "advice: a" in capsys.readouterr().out


def test_module_entry_point_reports_a_launchable_broker(sandbox):
    """``python -m browser_use.diagnose`` must not treat its own ``__main__`` name as the core."""
    if distribution.broker_command(module_name="browser_use") is None:
        pytest.skip("this interpreter is not exposed as a python executable")
    env = {key: value for key, value in os.environ.items() if not key.startswith("ICODE_BROWSER_")}
    env.update(_env(sandbox), PYTHONPATH=str(Path(__file__).resolve().parents[1] / "src"))
    completed = subprocess.run(
        [sys.executable, "-m", "browser_use.diagnose", "--json", "--config-dir", str(sandbox / "config")],
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        stdin=subprocess.DEVNULL,
        timeout=120,
    )
    document = json.loads(completed.stdout)
    assert document["broker"]["launchable"] is True, completed.stderr
    assert "broker_unsupported" not in _codes(document)


def test_a_stale_import_is_reported_with_the_restart_advice(sandbox, monkeypatch):
    monkeypatch.setattr(diagnose.distribution, "distribution_version", lambda *_: "9.9.9")
    document = diagnose.collect(env=_env(sandbox))

    assert document["plugin"]["distribution_version"] == "9.9.9"
    problems = [problem for problem in document["problems"] if problem["code"] == "plugin_version_mismatch"]
    assert len(problems) == 1
    assert problems[0]["severity"] == "warning"
    assert "restart iCode" in problems[0]["advice"]


def test_the_extension_folder_is_compared_with_the_installed_distribution(sandbox, monkeypatch):
    unpacked = sandbox / "unpacked"
    unpacked.mkdir()
    (unpacked / "manifest.json").write_text(json.dumps({"version": "0.1.0"}), encoding="utf-8")
    monkeypatch.setattr(diagnose.distribution, "distribution_version", lambda *_: "9.9.9")
    document = diagnose.collect(env=_env(sandbox))

    assert document["bridge"]["plugin_version"] == "9.9.9"
    problems = [problem for problem in document["problems"] if problem["code"] == "bridge_version_mismatch"]
    assert len(problems) == 1
    assert "0.1.0" in problems[0]["message"] and "9.9.9" in problems[0]["message"]
    assert "restart iCode" in problems[0]["advice"]


class _ProbeClient:
    async def send(self, method, params=None, session_id=None, timeout=None):
        return {"protocol": 1, "extensionVersion": "0.3.3", "browser": "Chrome/157", "capabilities": ["page_log_gated"]}

    async def aclose(self) -> None:
        return None


class _ProbeServer:
    fail_start: BaseException | None = None
    fail_wait: BaseException | None = None
    timeout: float | None = None
    closed = 0

    def __init__(self, _path) -> None:
        self.path = _path

    async def start(self) -> None:
        if _ProbeServer.fail_start is not None:
            raise _ProbeServer.fail_start

    async def wait_for_client(self, timeout: float = 15.0):
        _ProbeServer.timeout = timeout
        if _ProbeServer.fail_wait is not None:
            raise _ProbeServer.fail_wait
        return _ProbeClient()

    async def aclose(self) -> None:
        _ProbeServer.closed += 1


def _probe_server(monkeypatch, *, fail_start: BaseException | None = None, fail_wait: BaseException | None = None):
    import browser_use.bridge.server as bridge_server

    _ProbeServer.fail_start = fail_start
    _ProbeServer.fail_wait = fail_wait
    _ProbeServer.timeout = None
    _ProbeServer.closed = 0
    monkeypatch.setattr(bridge_server, "BridgeServer", _ProbeServer)


def test_probe_bridge_reports_the_extension_version(sandbox, monkeypatch):
    _probe_server(monkeypatch)
    result, problems = diagnose.probe_bridge(env=_env(sandbox))

    assert result["connected"] is True
    assert result["protocol"] == 1 and result["extension_version"] == "0.3.3"
    assert result["capabilities"] == ["page_log_gated"]
    assert result["waited_seconds"] is not None
    assert _ProbeServer.timeout == 35.0, "an explicit probe waits the full bridge_wait"
    assert problems == []
    assert _ProbeServer.closed == 1, "the probe must close its server"


def test_probe_bridge_honours_a_short_configured_wait(sandbox, monkeypatch):
    _probe_server(monkeypatch)
    result, _problems = diagnose.probe_bridge(env=_env(sandbox, ICODE_BROWSER_BRIDGE_WAIT="3"))

    assert result["connected"] is True
    assert _ProbeServer.timeout == 3.0


def test_probe_bridge_failure_is_a_stable_code(sandbox, monkeypatch):
    from browser_use.errors import Unavailable

    _probe_server(monkeypatch, fail_wait=Unavailable("extension bridge host did not connect within 5s"))
    result, problems = diagnose.probe_bridge(env=_env(sandbox))

    assert result["connected"] is False and "did not connect" in result["error"]
    assert result["waited_seconds"] is not None
    assert [problem["code"] for problem in problems] == ["bridge_not_connected"]
    assert problems[0]["severity"] == "error"
    assert _ProbeServer.closed == 1, "the started server must be closed again"


def test_probe_bridge_reports_a_state_folder_held_by_another_instance(sandbox, monkeypatch):
    from browser_use.errors import Unavailable

    _probe_server(monkeypatch, fail_start=Unavailable("another plugin instance is using the state folder"))
    result, problems = diagnose.probe_bridge(env=_env(sandbox))

    assert result["connected"] is False and "another plugin instance" in result["error"]
    assert [problem["code"] for problem in problems] == ["bridge_not_connected"]
    assert problems[0]["severity"] == "warning"
    assert "held by another plugin instance" in problems[0]["message"]
    assert _ProbeServer.closed == 0, "a server that never started has nothing to close"


def test_probe_bridge_reports_another_live_bridge_server(sandbox, monkeypatch):
    from browser_use.errors import Unavailable

    _probe_server(monkeypatch, fail_wait=Unavailable("no client"))
    monkeypatch.setattr(diagnose, "_discovery", lambda _path: ({"pid": 1}, None))
    monkeypatch.setattr(diagnose, "pid_alive", lambda _pid: True)
    result, problems = diagnose.probe_bridge(env=_env(sandbox))

    assert result["connected"] is False
    assert "another bridge server already owns" in problems[0]["message"]
    assert problems[0]["severity"] == "warning"


def test_doctor_bridge_is_opt_in_and_lands_in_the_document(sandbox, monkeypatch, capsys):
    document = diagnose.collect(env=_env(sandbox))
    monkeypatch.setattr(diagnose, "collect", lambda **_: document)
    monkeypatch.setattr(diagnose, "probe_bridge", lambda: ({"connected": False, "error": "e"}, []))
    assert diagnose.main(["--json", "--bridge"]) == 0
    printed = json.loads(capsys.readouterr().out)
    assert printed["bridge"]["probe"] == {"connected": False, "error": "e"}


def test_a_successful_probe_clears_the_install_and_running_problems(sandbox, monkeypatch, capsys):
    document = diagnose.collect(env=_env(sandbox))
    document["problems"] = [
        diagnose._problem("bridge_not_installed", "m", "a", "error"),
        diagnose._problem("bridge_not_running", "m", "a", "error"),
        diagnose._problem("bridge_unreachable", "m", "a", "error"),
        diagnose._problem("broker_not_running", "m", "a", "warning"),
    ]
    probe = {"connected": True, "extension_version": "0.3.2", "waited_seconds": 1.0, "protocol": 1, "browser": "C"}
    monkeypatch.setattr(diagnose, "collect", lambda **_: document)
    monkeypatch.setattr(diagnose, "probe_bridge", lambda: (probe, []))

    assert diagnose.main(["--json", "--bridge"]) == 0
    printed = json.loads(capsys.readouterr().out)
    assert printed["bridge"]["state"] == "connected"
    assert [problem["code"] for problem in printed["problems"]] == ["broker_not_running"]
    assert printed["ok"] is True
