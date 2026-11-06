# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Entry points started from a working directory that was deleted, and resumes of a session whose directory is gone."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from types import ModuleType

import pytest

from chrys.app.cli import acp as acp_cli
from chrys.app.cli import headless, launch_cwd
from chrys.app.cli import hooks as hooks_cli
from chrys.app.cli import profiles as profiles_cli
from chrys.app.cli import run as run_cli
from chrys.app.cli import trajectory as trajectory_cli
from chrys.app.cli import workflow as workflow_cli
from chrys.app.cli.run import PreparedRuntimeHolder
from chrys.foundation.config.settings import Settings
from chrys.foundation.config.settings_store import LoadedSettings
from chrys.foundation.events.types import Error
from chrys.foundation.i18n import DisplayPath, Localizer
from chrys.orchestration.engine.session_lifecycle import _RESTORE_SESSION_CWD_MISSING
from chrys.orchestration.session_host import HeadlessRunError
from tests.app.cli.test_run import FakeHost, _patch_runtime

_MISSING_WITH_WORKDIR = (
    "The current directory no longer exists; cd to an existing directory or pass -C with an absolute path."
)
_MISSING_WITHOUT_WORKDIR = "The current directory no longer exists; cd to an existing directory."


class _PastLaunchCheck(Exception):
    """Raised by the step right after the launch-directory check, to prove the check let the command through."""


@pytest.fixture(autouse=True)
def _pin_locale_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CHRYS_LOCALE", "en")


def _enter_deleted_directory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Make the process cwd a directory that no longer exists, as a shell keeps it after ``rm -r``.

    Windows refuses to remove a process's current directory, so the scenario
    only exists where the removal succeeds. The monkeypatch fixture changes
    back to the original directory at teardown.
    """
    gone = tmp_path / "gone"
    gone.mkdir()
    monkeypatch.chdir(gone)
    try:
        gone.rmdir()
    except OSError:
        pytest.skip("this OS cannot remove the current directory")
    return gone


def _shadow_os(getcwd: object) -> ModuleType:
    """An ``os`` stand-in for ``launch_cwd`` only, so the real module keeps working for everything else."""
    shadow = ModuleType("os")
    shadow.getcwd = getcwd  # type: ignore[attr-defined]
    shadow.path = os.path  # type: ignore[attr-defined]
    return shadow


# --- launch_cwd_missing_message ----------------------------------------------


def test_launch_cwd_message_is_none_while_the_directory_exists() -> None:
    assert launch_cwd.launch_cwd_missing_message(workdir_flag=True) is None
    assert launch_cwd.launch_cwd_missing_message(workdir_flag=False) is None


@pytest.mark.parametrize(
    ("workdir_flag", "expected"),
    [
        pytest.param(True, _MISSING_WITH_WORKDIR, id="accepts-C"),
        pytest.param(False, _MISSING_WITHOUT_WORKDIR, id="no-C"),
    ],
)
def test_launch_cwd_message_names_only_options_the_command_accepts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, workdir_flag: bool, expected: str
) -> None:
    _enter_deleted_directory(tmp_path, monkeypatch)

    assert launch_cwd.launch_cwd_missing_message(workdir_flag=workdir_flag) == expected


@pytest.mark.parametrize("workdir", [".", "project", "./project"])
def test_launch_cwd_message_rejects_a_relative_workdir_under_a_deleted_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, workdir: str
) -> None:
    """A relative ``-C`` resolves against the missing directory; POSIX may still report ``.`` as a directory."""
    _enter_deleted_directory(tmp_path, monkeypatch)

    assert launch_cwd.launch_cwd_missing_message(workdir_flag=True, workdir=workdir) == _MISSING_WITH_WORKDIR


def test_launch_cwd_message_accepts_an_existing_absolute_workdir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workdir = tmp_path / "project"
    workdir.mkdir()
    _enter_deleted_directory(tmp_path, monkeypatch)

    assert launch_cwd.launch_cwd_missing_message(workdir_flag=True, workdir=str(workdir)) is None
    assert (
        launch_cwd.launch_cwd_missing_message(workdir_flag=True, workdir=str(tmp_path / "absent"))
        == _MISSING_WITH_WORKDIR
    )


def test_launch_cwd_message_when_getcwd_still_names_the_removed_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    gone = str(tmp_path / "gone")
    monkeypatch.setattr(launch_cwd, "os", _shadow_os(lambda: gone))

    assert launch_cwd.launch_cwd_missing_message(workdir_flag=True) == _MISSING_WITH_WORKDIR


def test_launch_cwd_message_when_getcwd_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    def getcwd() -> str:
        raise FileNotFoundError(2, "No such file or directory")

    monkeypatch.setattr(launch_cwd, "os", _shadow_os(getcwd))

    assert launch_cwd.launch_cwd_missing_message(workdir_flag=False) == _MISSING_WITHOUT_WORKDIR


# --- icode run ----------------------------------------------------------------


def _record_run_command(monkeypatch: pytest.MonkeyPatch) -> list[argparse.Namespace]:
    calls: list[argparse.Namespace] = []

    async def run_command(args: argparse.Namespace, holder: PreparedRuntimeHolder) -> int:
        _ = holder
        calls.append(args)
        return 0

    monkeypatch.setattr(run_cli, "run_command", run_command)
    return calls


def test_run_from_a_deleted_directory_stops_with_one_line_before_startup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _enter_deleted_directory(tmp_path, monkeypatch)
    calls = _record_run_command(monkeypatch)

    rc = run_cli.main(["hello", "--agent", "Headless"])

    output = capsys.readouterr()
    assert rc == 1
    assert calls == []
    assert output.out == ""
    assert output.err == f"Error: {_MISSING_WITH_WORKDIR}\n"


def test_run_from_a_deleted_directory_reports_json_code(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _enter_deleted_directory(tmp_path, monkeypatch)
    calls = _record_run_command(monkeypatch)

    rc = run_cli.main(["hello", "--agent", "Headless", "--json"])

    output = capsys.readouterr()
    assert rc == 1
    assert calls == []
    assert output.out == ""
    assert json.loads(output.err) == {"error": _MISSING_WITH_WORKDIR, "code": "working_dir_missing"}


def test_run_from_a_deleted_directory_with_dot_workdir_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _enter_deleted_directory(tmp_path, monkeypatch)
    calls = _record_run_command(monkeypatch)

    rc = run_cli.main(["hello", "--agent", "Headless", "-C", "."])

    assert rc == 1
    assert calls == []
    assert _MISSING_WITH_WORKDIR in capsys.readouterr().err


def test_run_from_a_deleted_directory_with_workdir_starts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    workdir = tmp_path / "project"
    workdir.mkdir()
    _enter_deleted_directory(tmp_path, monkeypatch)
    calls = _record_run_command(monkeypatch)

    rc = run_cli.main(["hello", "--agent", "Headless", "-C", str(workdir)])

    assert rc == 0
    assert [args.cwd for args in calls] == [str(workdir)]
    assert capsys.readouterr().err == ""


# --- icode workflow -----------------------------------------------------------


def _stub_workflow_commands(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    calls: list[str] = []

    def list_command(args: argparse.Namespace) -> int:
        calls.append(args.command)
        return 0

    def validate_main(args: argparse.Namespace) -> int:
        calls.append(args.command)
        return 0

    async def run_command(args: argparse.Namespace) -> int:
        calls.append(args.command)
        return 0

    monkeypatch.setattr(workflow_cli, "_list_command", list_command)
    monkeypatch.setattr(workflow_cli, "_validate_main", validate_main)
    monkeypatch.setattr(workflow_cli, "_run_command", run_command)
    return calls


_WORKFLOW_ARGVS = [
    pytest.param(["list"], id="list"),
    pytest.param(["validate", "flow.py"], id="validate"),
    pytest.param(["run", "demo-workflow"], id="run"),
]


@pytest.mark.parametrize("argv", _WORKFLOW_ARGVS)
def test_workflow_from_a_deleted_directory_stops_before_every_command(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], argv: list[str]
) -> None:
    _enter_deleted_directory(tmp_path, monkeypatch)
    calls = _stub_workflow_commands(monkeypatch)

    rc = workflow_cli.main(argv)

    output = capsys.readouterr()
    assert rc == 1
    assert calls == []
    assert output.err == f"Error: {_MISSING_WITHOUT_WORKDIR}\n"


@pytest.mark.parametrize("argv", _WORKFLOW_ARGVS)
def test_workflow_from_a_deleted_directory_reports_json_code(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], argv: list[str]
) -> None:
    _enter_deleted_directory(tmp_path, monkeypatch)
    calls = _stub_workflow_commands(monkeypatch)

    rc = workflow_cli.main([*argv, "--json"])

    assert rc == 1
    assert calls == []
    assert json.loads(capsys.readouterr().err) == {"error": _MISSING_WITHOUT_WORKDIR, "code": "working_dir_missing"}


@pytest.mark.parametrize("argv", _WORKFLOW_ARGVS)
def test_workflow_in_an_existing_directory_reaches_the_command(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], argv: list[str]
) -> None:
    calls = _stub_workflow_commands(monkeypatch)

    assert workflow_cli.main(argv) == 0

    assert calls == [argv[0]]
    assert capsys.readouterr().err == ""


# --- icode acp ----------------------------------------------------------------


def _record_acp_command(monkeypatch: pytest.MonkeyPatch) -> list[argparse.Namespace]:
    calls: list[argparse.Namespace] = []

    async def run_command(args: argparse.Namespace) -> int:
        calls.append(args)
        return 0

    monkeypatch.setattr(acp_cli, "_configure_logging", lambda: None)
    monkeypatch.setattr(acp_cli, "run_command", run_command)
    return calls


def test_acp_from_a_deleted_directory_stops_before_serving(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _enter_deleted_directory(tmp_path, monkeypatch)
    calls = _record_acp_command(monkeypatch)

    rc = acp_cli.main([])

    output = capsys.readouterr()
    assert rc == 1
    assert calls == []
    # stdout is the JSON-RPC channel: nothing may reach it.
    assert output.out == ""
    assert output.err == f"Error: {_MISSING_WITH_WORKDIR}\n"


def test_acp_from_a_deleted_directory_with_workdir_serves(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    workdir = tmp_path / "project"
    workdir.mkdir()
    _enter_deleted_directory(tmp_path, monkeypatch)
    calls = _record_acp_command(monkeypatch)

    rc = acp_cli.main(["-C", str(workdir)])

    assert rc == 0
    assert [args.cwd for args in calls] == [str(workdir)]
    assert capsys.readouterr().err == ""


# --- icode trajectory ---------------------------------------------------------


def _stub_trajectory_runtime(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    calls: list[str] = []

    def prepare_runtime() -> Settings:
        calls.append("prepare")
        raise _PastLaunchCheck

    monkeypatch.setattr(trajectory_cli, "_prepare_runtime", prepare_runtime)
    return calls


def test_trajectory_from_a_deleted_directory_stops_before_loading_settings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    events = tmp_path / "events.jsonl"
    events.write_text("", encoding="utf-8")
    _enter_deleted_directory(tmp_path, monkeypatch)
    calls = _stub_trajectory_runtime(monkeypatch)

    rc = trajectory_cli.main(["export", "--events", str(events), "--out", str(tmp_path / "trace.json")])

    output = capsys.readouterr()
    assert rc == 1
    assert calls == []
    assert output.err == f"Error: {_MISSING_WITHOUT_WORKDIR}\n"


def test_trajectory_in_an_existing_directory_loads_settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    events = tmp_path / "events.jsonl"
    events.write_text("", encoding="utf-8")
    calls = _stub_trajectory_runtime(monkeypatch)

    with pytest.raises(_PastLaunchCheck):
        trajectory_cli.main(["export", "--events", str(events), "--out", str(tmp_path / "trace.json")])

    assert calls == ["prepare"]


# --- icode agent / icode model -----------------------------------------------


@pytest.mark.parametrize("as_json", [False, True], ids=["text", "json"])
@pytest.mark.parametrize("command", ["agents", "models"])
def test_profile_listing_from_a_deleted_directory_uses_user_settings(
    command: str, as_json: bool, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from chrys.foundation.config.user_settings import user_settings_path
    from chrys.service.profiles.models.schema import ModelProfile
    from chrys.service.profiles.models.serializer import save_profile

    save_profile(ModelProfile(id="local", name="Local"))
    user_settings_path().write_text(
        "agent:\n  default_profile: QA\nmodel:\n  profile:\n    active: local\n", encoding="utf-8"
    )
    for key in ("NO_PROXY", "no_proxy", "CHRYS_MODEL_PROFILE"):
        monkeypatch.setenv(key, "")
        monkeypatch.delenv(key)
    monkeypatch.setenv("COLUMNS", "120")
    monkeypatch.setattr("chrys.orchestration.startup.configure_utf8_stdio", lambda: None)
    monkeypatch.setattr("chrys.orchestration.startup.set_process_title", lambda title="chrys": None)
    monkeypatch.setattr("chrys.foundation.patches.apply_all", lambda: None)
    _enter_deleted_directory(tmp_path, monkeypatch)

    main = profiles_cli.agents_main if command == "agents" else profiles_cli.models_main
    rc = main(["--json"] if as_json else [])

    output = capsys.readouterr()
    assert rc == 0
    assert output.err == ""
    if as_json:
        payload = json.loads(output.out)
        if command == "agents":
            assert payload["default"] == "QA"
            assert {profile["name"] for profile in payload["agents"]} >= {"Code", "QA"}
        else:
            assert payload["active"] == "local"
            assert [profile["id"] for profile in payload["models"] if profile["active"]] == ["local"]
    else:
        selected = "QA" if command == "agents" else "local"
        assert any(line.split()[:2] == ["*", selected] for line in output.out.splitlines())


# --- icode hook ---------------------------------------------------------------


def _stub_hook_runtime(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    calls: list[str] = []

    def prepare_runtime(*, restoring_session: bool = False) -> headless.PreparedRuntime:
        calls.append("prepare")
        raise _PastLaunchCheck

    monkeypatch.setattr(headless, "prepare_runtime", prepare_runtime)
    return calls


_HOOK_ARGVS = [pytest.param(["list"], id="list"), pytest.param(["validate"], id="validate")]


@pytest.mark.parametrize("as_json", [False, True], ids=["text", "json"])
@pytest.mark.parametrize("argv", _HOOK_ARGVS)
def test_hook_from_a_deleted_directory_stops_before_loading_settings(
    argv: list[str], as_json: bool, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _enter_deleted_directory(tmp_path, monkeypatch)
    calls = _stub_hook_runtime(monkeypatch)

    rc = hooks_cli.main([*argv, "--json"] if as_json else argv)

    output = capsys.readouterr()
    assert rc == 1
    assert calls == []
    assert output.out == ""
    if as_json:
        assert json.loads(output.err) == {"error": _MISSING_WITHOUT_WORKDIR, "code": "working_dir_missing"}
    else:
        assert output.err == f"Error: {_MISSING_WITHOUT_WORKDIR}\n"


@pytest.mark.parametrize("argv", _HOOK_ARGVS)
def test_hook_in_an_existing_directory_loads_settings(argv: list[str], monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _stub_hook_runtime(monkeypatch)

    hooks_cli.main(argv)

    assert calls == ["prepare"]


# --- icode (TUI) --------------------------------------------------------------


def _stub_tui_startup(monkeypatch: pytest.MonkeyPatch, argv: list[str]) -> list[str]:
    """Stop the TUI entry point at the first step after its launch-directory check."""
    from chrys.orchestration import startup as startup_mod

    calls: list[str] = []

    def get_platform() -> object:
        calls.append("platform")
        raise _PastLaunchCheck

    monkeypatch.setattr(sys, "argv", argv)
    monkeypatch.setattr(startup_mod, "configure_utf8_stdio", lambda: None)
    monkeypatch.setattr("chrys.foundation.platform.get_platform", get_platform)
    return calls


def test_tui_from_a_deleted_directory_exits_with_one_line(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from chrys.app.tui import app as tui_app

    _enter_deleted_directory(tmp_path, monkeypatch)
    calls = _stub_tui_startup(monkeypatch, ["icode"])

    with pytest.raises(SystemExit) as exit_info:
        tui_app.main()

    assert exit_info.value.code == 1
    assert calls == []
    assert capsys.readouterr().err == f"Error: {_MISSING_WITH_WORKDIR}\n"


def test_tui_from_a_deleted_directory_with_dot_workdir_exits_with_one_line(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from chrys.app.tui import app as tui_app

    _enter_deleted_directory(tmp_path, monkeypatch)
    calls = _stub_tui_startup(monkeypatch, ["icode", "-C", "."])

    with pytest.raises(SystemExit) as exit_info:
        tui_app.main()

    assert exit_info.value.code == 1
    assert calls == []
    assert capsys.readouterr().err == f"Error: {_MISSING_WITH_WORKDIR}\n"


def test_tui_from_a_deleted_directory_with_workdir_starts_there(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from chrys.app.tui import app as tui_app

    workdir = tmp_path / "project"
    workdir.mkdir()
    _enter_deleted_directory(tmp_path, monkeypatch)
    calls = _stub_tui_startup(monkeypatch, ["icode", "-C", str(workdir)])

    with pytest.raises(_PastLaunchCheck):
        tui_app.main()

    assert calls == ["platform"]
    assert os.path.samefile(os.getcwd(), workdir)
    assert capsys.readouterr().err == ""


# --- icode run -s <session whose directory is gone> ---------------------------


def _session_cwd_missing_failure() -> HeadlessRunError:
    return HeadlessRunError(
        Error(
            code="session_cwd_missing",
            message="Working directory of session abcd1234 no longer exists: /gone",
            display_message=_RESTORE_SESSION_CWD_MISSING.bind(path=DisplayPath("/gone")),
            session_id="session-1",
        )
    )


class _RestoreFailsHost(FakeHost):
    """The engine refuses the restore while the host starts, as it does for a session whose directory is gone."""

    async def start(self) -> None:
        raise _session_cwd_missing_failure()


def _patch_restore_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_runtime(monkeypatch)
    monkeypatch.setattr(run_cli, "ChrysSessionHost", _RestoreFailsHost)


def _use_locale(monkeypatch: pytest.MonkeyPatch, locale: str) -> None:
    def prepare_runtime(**_kwargs: object) -> headless.PreparedRuntime:
        return headless.PreparedRuntime(
            loaded=LoadedSettings(settings=Settings(), provenance={}),
            localizer=Localizer(locale),
            pending_warnings=[],
        )

    monkeypatch.setattr(headless, "prepare_runtime", prepare_runtime)


@pytest.mark.parametrize(
    ("locale", "expected"),
    [
        pytest.param(
            "en",
            "Error: The working directory of this session no longer exists: /gone "
            "Pass -C <dir> to continue it in another directory.\n",
            id="en",
        ),
        pytest.param(
            "zh-Hans",
            "Error: 该会话的工作目录已不存在：/gone使用 -C <dir> 在另一个目录中继续该会话。\n",  # noqa: RUF001
            id="zh-Hans",
        ),
    ],
)
def test_run_resuming_a_session_whose_directory_is_gone_suggests_workdir(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], locale: str, expected: str
) -> None:
    _patch_restore_failure(monkeypatch)
    _use_locale(monkeypatch, locale)

    rc = run_cli.main(["hello", "--agent", "Headless", "-s", "abcd1234", "--quiet"])

    output = capsys.readouterr()
    assert rc == 1
    assert output.out == ""
    assert output.err == expected


def test_run_resuming_a_session_whose_directory_is_gone_reports_english_json(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _patch_restore_failure(monkeypatch)
    _use_locale(monkeypatch, "zh-Hans")

    rc = run_cli.main(["hello", "--agent", "Headless", "-s", "abcd1234", "--json"])

    assert rc == 1
    assert json.loads(capsys.readouterr().err) == {
        "error": "Working directory of session abcd1234 no longer exists: /gone "
        "Pass -C <dir> to continue it in another directory.",
        "code": "session_cwd_missing",
        "session_id": "session-1",
    }
