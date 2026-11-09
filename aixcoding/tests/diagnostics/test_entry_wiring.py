# (tests — asserts are pytest idiom, Chinese prose is the data)
"""Entry wiring: app.py main() -> aixcoding_startup() -> shared config tasks."""

from __future__ import annotations

import logging
import sys
import tomllib
from pathlib import Path
from types import SimpleNamespace

from chrys.app.aixcoding import startup
from chrys.kernel.aixcoding.diagnostics import logging_support
from chrys.kernel.aixcoding.diagnostics.config import DiagnosticsConfig, LoggingConfig
from chrys.kernel.aixcoding.diagnostics.logging_support import _Marker
from chrys.service.aixcoding.config import load_config

AIX_LOGGER = logging.getLogger("aixcoding")


def make_cfg() -> DiagnosticsConfig:
    return DiagnosticsConfig(logging=LoggingConfig(level=logging.INFO, panel=False), source=None)


def markers() -> list[_Marker]:
    return [h for h in AIX_LOGGER.handlers if isinstance(h, _Marker)]


def test_ensure_file_sink_idempotent(tmp_path: Path, aix_logger_state):
    first = logging_support.ensure_file_sink(make_cfg(), config_dir=tmp_path)
    second = logging_support.ensure_file_sink(make_cfg(), config_dir=tmp_path)
    assert first is not None
    assert first == second
    assert first == tmp_path / logging_support.LOG_DIR_NAME / logging_support.LOG_FILE_NAME
    assert len(markers()) == 1


def test_ensure_file_sink_fail_safe(tmp_path: Path, monkeypatch, capsys):
    def boom(config, config_dir=None):
        raise RuntimeError("unexpected failure")

    monkeypatch.setattr(logging_support, "setup_logging", boom)
    result = logging_support.ensure_file_sink(make_cfg(), config_dir=tmp_path)
    assert result is None
    assert "file sink setup failed" in capsys.readouterr().err


def test_startup_contains_task_failures(monkeypatch, capsys):
    calls = []

    def ok():
        calls.append("ok")

    def boom():
        raise RuntimeError("task exploded")

    def after():
        calls.append("after")

    monkeypatch.setattr(startup, "_TASKS", (ok, boom, after))
    startup.aixcoding_startup()
    assert calls == ["ok", "after"]
    assert "task boom failed" in capsys.readouterr().err


def test_startup_idempotent_when_tasks_are(tmp_path: Path, monkeypatch, aix_logger_state):
    monkeypatch.setattr(
        startup,
        "_TASKS",
        (lambda: logging_support.ensure_file_sink(make_cfg(), config_dir=tmp_path),),
    )
    startup.aixcoding_startup()
    startup.aixcoding_startup()
    assert len(markers()) == 1


def test_startup_task_order_and_single_snapshot(tmp_path: Path, monkeypatch):
    import chrys.foundation.platform as platform
    import chrys.service.aixcoding.config.loader as config_loader
    from chrys.kernel.aixcoding.auth import environments

    (tmp_path / "aixcoding.toml").write_text('environment = "local"\n', encoding="utf-8")
    monkeypatch.setattr(platform, "get_platform", lambda: SimpleNamespace(config_dir=tmp_path))
    events = []
    monkeypatch.setattr(environments, "configure_environment", lambda raw: events.append(("environment", raw)))
    monkeypatch.setattr(logging_support, "ensure_file_sink", lambda config: events.append(("logging", config)))
    loads = 0
    real_loads = tomllib.loads

    def counted_loads(text: str):
        nonlocal loads
        loads += 1
        return real_loads(text)

    monkeypatch.setattr(config_loader.tomllib, "loads", counted_loads)
    load_config.cache_clear()
    try:
        startup.aixcoding_startup()
        assert events[0] == ("environment", "local")
        assert events[1][0] == "logging"
        assert events[1][1].source == tmp_path / "aixcoding.toml"
        assert loads == 1
    finally:
        load_config.cache_clear()


def test_environment_injection_precedes_default_session_construction(monkeypatch):
    import chrys.kernel.aixcoding.auth.session as session_module
    from chrys.kernel.aixcoding.auth import environments
    from chrys.service.aixcoding import config as config_package

    events = []
    snapshot = SimpleNamespace(environment="local", diagnostics=make_cfg())
    monkeypatch.setattr(config_package, "load_config", lambda: snapshot)
    monkeypatch.setattr(environments, "configure_environment", lambda raw: events.append("configure"))
    monkeypatch.setattr(logging_support, "ensure_file_sink", lambda config: None)
    monkeypatch.setattr(session_module, "_default_session", None)
    monkeypatch.setattr(session_module, "LoginSession", lambda: events.append("session") or object())

    startup.aixcoding_startup()
    session_module.get_login_session()
    assert events == ["configure", "session"]


def test_main_calls_aixcoding_startup(monkeypatch):
    from chrys.app.cli import app as cli_app

    called = []
    # The in-function import is an intentional test seam: the suite drives main()
    # and intercepts this call. If upstream moves the import to module level,
    # this test goes red by design — main() must call the startup hook.
    monkeypatch.setattr("chrys.app.aixcoding.startup.aixcoding_startup", lambda: called.append(True))
    monkeypatch.setattr(sys, "argv", ["icode", "--version"])
    rc = cli_app.main()
    assert rc == 0
    assert called == [True]


def test_startup_placement_and_task_registration():
    file = Path(startup.__file__)
    assert file.name == "__init__.py"
    assert file.parts[-4:-1] == ("app", "aixcoding", "startup")
    assert (file.parent.parent / "tui").is_dir()
    assert callable(startup.aixcoding_startup)
    assert (startup._configure_environment, startup._ensure_file_sink) == startup._TASKS
