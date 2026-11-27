# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""``chrys.foundation.guard``: deciding, loading, and refusing to start without the guard asked for."""

from __future__ import annotations

import importlib.metadata
import importlib.util
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from chrys.foundation import guard


@pytest.fixture(autouse=True)
def _restore_guard() -> Any:
    installed = guard.current()
    yield
    guard.set_current(installed)


def _entry(dist: str, loaded: object, *, value: str = guard.ENTRY_POINT_TARGET, files: tuple[str, ...] = ()) -> Any:
    return SimpleNamespace(
        name="sandbox",
        value=value,
        dist=SimpleNamespace(name=dist, files=files, locate_file=lambda f: f, read_text=lambda _name: None),
        load=lambda: loaded,
    )


def _module(path: str) -> Any:
    return SimpleNamespace(__name__="icode_sandbox.guard", __file__=path)


def _offer(monkeypatch: pytest.MonkeyPatch, *entries: Any) -> None:
    monkeypatch.setattr(
        importlib.metadata, "entry_points", lambda group: list(entries) if group == guard.ENTRY_POINT_GROUP else []
    )


def test_only_the_icode_sandbox_distribution_is_loaded(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ours = _module(str(tmp_path / "guard.py"))
    _offer(monkeypatch, _entry("evil-guard", object()), _entry("icode-sandbox", ours, files=(ours.__file__,)))
    assert guard._load_plugin() is ours
    assert "ignoring guards offered by other packages: evil-guard" in capsys.readouterr().err


def test_another_distribution_alone_is_not_a_guard(monkeypatch: pytest.MonkeyPatch) -> None:
    _offer(monkeypatch, _entry("evil-guard", object()))
    with pytest.raises(LookupError, match="icode-sandbox package is not installed"):
        guard._load_plugin()


def test_a_second_package_calling_itself_icode_sandbox_is_refused(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    ours = _module(str(tmp_path / "guard.py"))
    _offer(
        monkeypatch,
        _entry("icode-sandbox", ours, files=(ours.__file__,)),
        _entry("icode-sandbox", _module(str(tmp_path / "evil.py")), files=(str(tmp_path / "evil.py"),)),
    )
    with pytest.raises(LookupError, match="2 installed packages call themselves icode-sandbox"):
        guard._load_plugin()


def test_an_entry_point_aimed_elsewhere_is_refused(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    evil = _module(str(tmp_path / "evil.py"))
    _offer(monkeypatch, _entry("icode-sandbox", evil, value="evil.guard", files=(evil.__file__,)))
    with pytest.raises(LookupError, match=r"entry point names 'evil\.guard'"):
        guard._load_plugin()


def test_a_module_from_outside_the_distribution_is_refused(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """The name claims icode-sandbox, but what loaded (first on ``sys.path``) is someone else's file."""
    _offer(
        monkeypatch,
        _entry("icode-sandbox", _module(str(tmp_path / "elsewhere.py")), files=(str(tmp_path / "guard.py"),)),
    )
    with pytest.raises(LookupError, match="did not load from the icode-sandbox package"):
        guard._load_plugin()


def _missing(monkeypatch: pytest.MonkeyPatch) -> None:
    def _raise() -> Any:
        raise LookupError("the icode-sandbox package is not installed")

    monkeypatch.setattr(guard, "_load_plugin", _raise)


def test_asked_for_but_not_installed_refuses_to_start(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _missing(monkeypatch)
    monkeypatch.delenv(guard.ENV_ON_UNAVAILABLE, raising=False)
    with pytest.raises(SystemExit) as stopped:
        guard.install_at_startup(["--sandbox", "run", "x"], config_dir=tmp_path)
    assert stopped.value.code == guard.EXIT_UNAVAILABLE
    assert "not installed" in capsys.readouterr().err


def test_asked_for_but_not_installed_with_warn_runs_and_says_so(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _missing(monkeypatch)
    monkeypatch.setenv(guard.ENV_ON_UNAVAILABLE, "warn")
    rest, st = guard.install_at_startup(["--sandbox", "run", "x"], config_dir=tmp_path)
    assert rest == ["run", "x"]
    assert (st.label(), guard.current().active) == ("requested-not-active", False)
    assert guard.status_line() == "Sandbox: requested-not-active: the icode-sandbox package is not installed"


def test_a_guard_that_fails_to_start_refuses_too(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    def broken(argv: list[str], decision: guard.Decision, config_dir: Path) -> guard.Guard:
        raise OSError("broker would not start")

    monkeypatch.setattr(guard, "_load_plugin", lambda: SimpleNamespace(install=broken))
    monkeypatch.delenv(guard.ENV_ON_UNAVAILABLE, raising=False)
    with pytest.raises(SystemExit) as stopped:
        guard.install_at_startup(["--sandbox"], config_dir=tmp_path)
    assert stopped.value.code == guard.EXIT_UNAVAILABLE


def test_not_asked_for_nothing_is_loaded(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    def must_not_load() -> Any:
        raise AssertionError("loaded a guard nobody asked for")

    monkeypatch.setattr(guard, "_load_plugin", must_not_load)
    rest, st = guard.install_at_startup(["--no-sandbox", "run"], config_dir=tmp_path)
    assert rest == ["run"]
    assert (st.requested, guard.current().active) == (False, False)


def test_not_asked_for_the_package_is_never_imported(tmp_path: Path) -> None:
    """In a fresh interpreter, as ``icode`` starts: the sandbox package stays out of the process."""
    code = (
        "import sys\n"
        "from chrys.foundation import guard\n"
        "guard.install_at_startup(['run', 'x'])\n"
        "print(sorted(m for m in sys.modules if m.startswith('icode_sandbox')))\n"
    )
    env = {**os.environ, "HOME": str(tmp_path), "CHRYS_FILE_SANDBOX": "0"}
    env.pop("CHRYS_TEST_SANDBOX", None)
    done = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, env=env, stdin=subprocess.DEVNULL, check=True
    )
    assert done.stdout.strip() == "[]", done


def test_the_subcommand_without_the_package_says_it_is_missing(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _missing(monkeypatch)
    assert guard.cli(["status"]) == 2
    assert "not available" in capsys.readouterr().err


def test_with_no_guard_every_hook_does_nothing() -> None:
    nothing = guard._NoGuard()
    with nothing.user_reads(), nothing.controlling_terminal(), nothing.launching_configured("x", writes=["/"]):
        pass
    with nothing.session_scope("s", ["/"], "/"):
        pass
    assert nothing.walk_start("/x") is None
    assert (nothing.indicator(), nothing.panel(), nothing.active) == (None, None, False)


_UNTOUCHED = r"""
import builtins, importlib.abc, io, os, subprocess, sys
if sys.argv[1] == "hidden":
    # As if the sandbox package were not installed: neither its module nor its entry point is found.
    class _Hide(importlib.abc.MetaPathFinder):
        def find_spec(self, name, path=None, target=None):
            if name == "icode_sandbox" or name.startswith("icode_sandbox."):
                raise ModuleNotFoundError(name)
            return None
    sys.meta_path.insert(0, _Hide())
    import importlib.metadata as _metadata
    _entry_points = _metadata.entry_points
    _metadata.entry_points = lambda **kw: [] if kw.get("group") == "chrys.runtime_guards" else _entry_points(**kw)
# What the sandbox package would replace if it were loaded and on.
NAMES = ["getcwd", "getcwdb", "chdir", "open", "listdir", "scandir", "mkdir", "unlink", "remove",
         "rmdir", "replace", "rename", "symlink"]
def snapshot():
    found = {f"os.{n}": getattr(os, n) for n in NAMES}
    found.update({"builtins.open": builtins.open, "io.open": io.open,
                  "Popen._execute_child": subprocess.Popen._execute_child})
    return found
before = snapshot()

from chrys.foundation.platform import get_platform
from chrys.service.llm import mock as mock_mod

replies = [
    mock_mod.MockResponse(tool_calls=[("read_file", "c1", {"path": "note.txt"}),
                                      (get_platform().shell.name, "c2", {"command": "echo ran > ran.txt", "reason": "t"})]),
    mock_mod.MockResponse(text="done"),
]
original = mock_mod.MockChatClient._call_model
def scripted(self, messages, **kwargs):
    self._responses = [replies.pop(0) if replies else mock_mod.MockResponse(text="done")]
    self._call_index = 0
    return original(self, messages, **kwargs)
mock_mod.MockChatClient._call_model = scripted

from chrys.app.cli.app import main
sys.argv = ["icode", "run", "-a", "Code", "-m", "mock", "-q", "go"]
try:
    code = main()
except SystemExit as exc:
    code = exc.code
after = snapshot()
print("EXIT", code, file=sys.stderr)
print("MODULES", sorted(m for m in sys.modules if m.startswith("icode_sandbox")), file=sys.stderr)
print("REPLACED", sorted(k for k in before if after[k] is not before[k]), file=sys.stderr)
"""


@pytest.mark.parametrize("package", ["installed", "hidden"])
def test_a_whole_turn_without_the_sandbox_leaves_the_process_untouched(tmp_path: Path, package: str) -> None:
    """Off: a real ``icode run`` turn that reads a file and runs a command, with the settings file saying
    so, imports nothing of the sandbox and leaves every function it would replace as it was; the same
    with the package not installed at all, which is what upstream CI (with the package) can't otherwise see."""
    from tests.support.secure_files import plant_owner_only_bytes

    home = tmp_path / "home"
    appdata = home / "AppData" / "Roaming"
    config = appdata / "chrys" if sys.platform == "win32" else home / ".chrys"
    (config / "models").mkdir(parents=True)
    plant_owner_only_bytes(config / "models" / "mock.yaml", b"id: mock\nname: Mock\nprovider: mock\nmodel_id: mock\n")
    plant_owner_only_bytes(
        config / "settings.yaml", b"model.profile.active: mock\nsandbox:\n  files:\n    enabled: false\n"
    )
    work = tmp_path / "work"
    work.mkdir()
    (work / "note.txt").write_text("hello\n", encoding="utf-8")
    env = {k: v for k, v in os.environ.items() if not k.startswith("CHRYS_")}
    env.update(HOME=str(home), USERPROFILE=str(home), APPDATA=str(appdata))
    done = subprocess.run(
        [sys.executable, "-c", _UNTOUCHED, package],
        capture_output=True,
        text=True,
        env=env,
        cwd=work,
        stdin=subprocess.DEVNULL,
        timeout=50,
        check=False,
    )
    report = {line.split(" ", 1)[0]: line.split(" ", 1)[1] for line in done.stderr.splitlines() if " " in line}
    assert report.get("EXIT") == "0", done.stderr[-2000:]
    assert report.get("MODULES") == "[]", done.stderr[-2000:]
    assert report.get("REPLACED") == "[]", done.stderr[-2000:]
    assert (work / "ran.txt").read_text(encoding="utf-8").strip() == "ran"  # the tools really ran


def test_a_deleted_current_directory_is_left_for_the_command_to_report(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "settings.yaml").write_text("workdir:\n  config_enabled: true\n", encoding="utf-8")

    def gone() -> str:
        raise FileNotFoundError("deleted")

    monkeypatch.setattr(os, "getcwd", gone)
    rest, decision = guard.decide(["run", "x"], tmp_path, env={})
    assert (rest, decision.enabled) == (["run", "x"], False)


def test_a_workdir_can_ask_for_the_sandbox_but_never_stop_icode_starting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workdir = tmp_path / "repo"
    (workdir / ".chrys").mkdir(parents=True)
    (workdir / ".chrys" / "settings.yaml").write_text("sandbox:\n  files:\n    enabled: true\n", encoding="utf-8")
    config = tmp_path / "config"
    config.mkdir()
    (config / "settings.yaml").write_text("workdir:\n  config_enabled: true\n", encoding="utf-8")
    _missing(monkeypatch)
    monkeypatch.delenv(guard.ENV_ENABLED, raising=False)
    monkeypatch.delenv(guard.ENV_ON_UNAVAILABLE, raising=False)
    _rest, st = guard.install_at_startup(["run", "-C", str(workdir), "x"], config_dir=config)
    assert (st.requested, st.state) == (True, "inactive")  # warned, started
    # Control: the person's own request still refuses.
    (config / "settings.yaml").write_text("sandbox:\n  files:\n    enabled: true\n", encoding="utf-8")
    with pytest.raises(SystemExit):
        guard.install_at_startup(["run", "x"], config_dir=config)


@pytest.mark.parametrize(
    ("trust", "source"),
    [
        ("project:\n  config_enabled: true\n", "workdir"),  # saved before the rename
        ("project.config_enabled: true\nworkdir.config_enabled: false\n", "default"),  # disagree: off
        ("workdir:\n  config_enabled: true\n", "workdir"),
    ],
)
def test_the_trust_switch_counts_under_its_previous_name_as_the_loader_reads_it(
    tmp_path: Path, trust: str, source: str
) -> None:
    workdir = tmp_path / "repo"
    (workdir / ".chrys").mkdir(parents=True)
    (workdir / ".chrys" / "settings.yaml").write_text("sandbox:\n  files:\n    enabled: true\n", encoding="utf-8")
    (tmp_path / "settings.yaml").write_text(trust, encoding="utf-8")
    _rest, decision = guard.decide(["run", "-C", str(workdir), "x"], tmp_path, env={})
    assert decision.source == source
    assert decision.enabled is (source == "workdir")


@pytest.mark.parametrize("content", [None, b"", b"model.profile.active: mock\n"])
def test_a_missing_empty_or_unrelated_settings_file_is_no_problem(tmp_path: Path, content: bytes | None) -> None:
    if content is not None:
        (tmp_path / "settings.yaml").write_bytes(content)
    _rest, decision = guard.decide(["run", "x"], tmp_path, env={})
    assert (decision.enabled, decision.settings_problem) == (False, "")


@pytest.mark.parametrize(
    "content",
    [b"sandbox:\n  files:\n    enabled: true\n  bad: [\n", b"- a list, not settings\n", b"\xff\xfe not text"],
)
def test_a_broken_settings_file_does_not_turn_the_sandbox_off_without_a_word(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str], content: bytes
) -> None:
    (tmp_path / "settings.yaml").write_bytes(content)
    monkeypatch.delenv(guard.ENV_ENABLED, raising=False)
    monkeypatch.setattr(guard, "installed", lambda: True)
    _rest, st = guard.install_at_startup(["run", "x"], config_dir=tmp_path)
    err = capsys.readouterr().err
    assert "sandbox settings could not be read" in err
    assert str(tmp_path / "settings.yaml") in err  # which file, never its contents
    assert st.state == "inactive" and any("settings not read" in r for r in st.reasons)
    # Without the package the sandbox settings mean nothing: the loader reports the file, not us.
    monkeypatch.setattr(guard, "installed", lambda: False)
    guard.install_at_startup(["run", "x"], config_dir=tmp_path)
    assert capsys.readouterr().err == ""


def test_a_broken_working_directory_document_is_only_ignored(tmp_path: Path) -> None:
    """A repository's file is not the person's: it can't make iCode warn, as it can't stop it starting."""
    workdir = tmp_path / "repo"
    (workdir / ".chrys").mkdir(parents=True)
    (workdir / ".chrys" / "settings.yaml").write_bytes(b"sandbox: [\n")
    (tmp_path / "settings.yaml").write_text("workdir:\n  config_enabled: true\n", encoding="utf-8")
    _rest, decision = guard.decide(["run", "-C", str(workdir), "x"], tmp_path, env={})
    assert (decision.enabled, decision.settings_problem) == (False, "")


def test_settings_kept_elsewhere_through_a_link_still_count(tmp_path: Path) -> None:
    """A dotfiles setup: ``~/.chrys`` (or its settings file) is a link."""
    real = tmp_path / "dotfiles" / "chrys"
    real.mkdir(parents=True)
    (real / "settings.yaml").write_text("sandbox:\n  files:\n    enabled: true\n", encoding="utf-8")
    (tmp_path / ".chrys").symlink_to(real, target_is_directory=True)
    _rest, decision = guard.decide(["run"], tmp_path / ".chrys", env={})
    assert (decision.enabled, decision.source) == (True, "settings")


def test_only_options_are_taken_as_the_sandbox_flags() -> None:
    assert guard.strip_flags(["run", "--sandbox", "hi"]) == (["run", "hi"], True)
    assert guard.strip_flags(["--sandbox", "run", "--no-sandbox"]) == (["run"], False)
    assert guard.strip_flags(["run", "--", "--sandbox"]) == (["run", "--", "--sandbox"], None)


def test_installed_tells_without_loading(monkeypatch: pytest.MonkeyPatch) -> None:
    def must_not_load() -> Any:
        raise AssertionError("loaded")

    monkeypatch.setattr(guard, "_load_plugin", must_not_load)
    present = importlib.util.find_spec("icode_sandbox") is not None
    assert guard.installed() is present
    monkeypatch.setattr(importlib.metadata, "entry_points", lambda **_kw: [])
    assert guard.installed() is False


@pytest.mark.parametrize("policy", ["refuse", "warn"])
def test_workdir_request_preserves_the_users_failure_policy(tmp_path, monkeypatch, policy):
    config, work = tmp_path / "config", tmp_path / "work"
    config.mkdir()
    (work / ".chrys").mkdir(parents=True)
    (config / "settings.yaml").write_text(
        f"sandbox.files.enabled: true\nsandbox.files.on_unavailable: {policy}\nworkdir.config_enabled: true\n"
    )
    (work / ".chrys/settings.yaml").write_text("sandbox.files.enabled: true\n")
    _missing(monkeypatch)
    argv = ["run", "-C", str(work), "hello"]
    _, decision = guard.decide(argv, config, env={})
    assert decision.source == "settings"
    if policy == "refuse":
        with pytest.raises(SystemExit) as exc:
            guard.install_at_startup(argv, config_dir=config)
        assert exc.value.code == 78
    else:
        _, status = guard.install_at_startup(argv, config_dir=config)
        assert (status.state, status.requested) == ("inactive", True)


@pytest.mark.parametrize("value", ["1", "' true '", "yes", "on"])
@pytest.mark.parametrize("source", ["settings", "workdir"])
def test_startup_accepts_the_settings_loaders_boolean_values(tmp_path, monkeypatch, value, source):
    config, work = tmp_path / "config", tmp_path / "work"
    config.mkdir()
    (work / ".chrys").mkdir(parents=True)
    if source == "settings":
        (config / "settings.yaml").write_text(f"sandbox.files.enabled: {value}\n")
    else:
        (config / "settings.yaml").write_text(f"workdir.config_enabled: {value}\n")
        (work / ".chrys/settings.yaml").write_text(f"sandbox.files.enabled: {value}\n")
    _missing(monkeypatch)
    _, decision = guard.decide(["run", "-C", str(work)], config, env={})
    assert decision.enabled is True
    assert decision.source == source


@pytest.mark.parametrize("policy", ["refuse", "warn"])
@pytest.mark.parametrize("equals", [False, True])
def test_explicit_unavailable_policy_overrides_settings_and_environment(tmp_path, policy, equals):
    (tmp_path / "settings.yaml").write_text("sandbox.files.on_unavailable: warn\n")
    flag = [f"--sandbox-on-unavailable={policy}"] if equals else ["--sandbox-on-unavailable", policy]
    rest, decision = guard.decide(
        ["run", "--sandbox", *flag, "hello"], tmp_path, env={guard.ENV_ON_UNAVAILABLE: "refuse"}
    )
    assert rest == ["run", "hello"]
    assert (decision.enabled, decision.on_unavailable) == (True, policy)


@pytest.mark.parametrize("argv", [["--sandbox-on-unavailable"], ["--sandbox-on-unavailable=oops"]])
def test_invalid_unavailable_policy_is_a_usage_error(tmp_path, argv):
    with pytest.raises(SystemExit) as exc:
        guard.decide(argv, tmp_path, env={})
    assert exc.value.code == 2


def test_literal_unavailable_policy_is_not_consumed():
    argv = ["run", "--", "--sandbox-on-unavailable=warn"]
    assert guard.parse_flags(argv) == (argv, None, None)
