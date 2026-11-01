# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""``icode hook``: list what a session would load and check hooks files, never printing what a hook passes on."""

from __future__ import annotations

import json
import os
import sys
import textwrap
from pathlib import Path
from unittest.mock import create_autospec

import pytest

from chrys.app.cli import app as cli_app
from chrys.app.cli import headless
from chrys.app.cli import hooks as hooks_cli
from chrys.foundation.config.env_layers import freeze_process_env
from chrys.foundation.config.settings_store import load_settings
from chrys.foundation.config.warnings import settings_warning_events
from chrys.foundation.i18n import Localizer
from chrys.foundation.platform import get_platform
from chrys.service.hooks.events import HookEvent
from tests.support.symlinks import symlink_or_skip

_GLOBAL = """
hooks:
  - id: notify
    event: after_turn
    run:
      type: command
      argv: [PROGRAM, SECRET_ARGUMENT]
      env: {API_TOKEN: SECRET_VALUE}
  - id: lint
    event: after_tool_call
    match:
      tool_kind: shell
      args: {command: {regex: '\\.py$', contains: pytest}}
    run: {type: shell, shell: echo SECRET_SHELL}
  - id: check
    event: before_tool_call
    match: {tool_name: write_file}
    enabled: false
    run: {type: script, path: scripts/check.py}
""".replace("PROGRAM", json.dumps(sys.executable))


@pytest.fixture(autouse=True)
def _settings_without_bootstrap(monkeypatch: pytest.MonkeyPatch) -> None:
    """The bootstrap rewrites process-wide state; the command gets this directory's settings and their warnings."""

    def prepare(*, restoring_session: bool = False) -> headless.PreparedRuntime:
        freeze_process_env()
        loaded = load_settings(project_root=Path.cwd())
        warnings = settings_warning_events(loaded, skip=headless.speaks_for_the_env_var)
        return headless.PreparedRuntime(loaded, Localizer("en"), warnings)

    monkeypatch.setattr(headless, "prepare_runtime", create_autospec(headless.prepare_runtime, side_effect=prepare))


def _shown(*parts: str) -> str:
    """How the command shows a path below the current directory."""
    return f".{os.sep}{Path(*parts)}"


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(text).lstrip("\n"), encoding="utf-8")
    return path


def _global_hooks(text: str = _GLOBAL) -> Path:
    return _write(get_platform().config_dir / "hooks" / "hooks.yaml", text)


def _project_hooks(text: str) -> Path:
    return _write(Path.cwd() / ".chrys" / "hooks" / "hooks.yaml", text)


def _enable_project_hooks() -> None:
    _write(get_platform().config_dir / "settings.yaml", "project:\n  hooks_enabled: true\n")


@pytest.mark.parametrize("word", ["hook", "hooks"])
def test_the_bare_command_lists_without_printing_what_hooks_pass_on(
    word: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # No line is folded to the terminal's width, however narrow.
    monkeypatch.setenv("COLUMNS", "40")
    path = _global_hooks()
    _write(path.parent / "scripts" / "check.py", "")
    monkeypatch.setattr(sys, "argv", ["icode", word])

    assert cli_app.main() == 0

    assert capsys.readouterr().out.splitlines() == [
        f"Global hooks: {path} (loaded)",
        "  notify (after_turn, fire_and_forget)",
        f"    run: command {sys.executable}",
        "  lint (after_tool_call, fire_and_forget)",
        "    match: tool_kind=shell, args.command contains 'pytest' and matches '\\.py$'",
        "    run: shell",
        "  check (before_tool_call, fire_and_forget, turned off)",
        "    match: tool_name=write_file",
        f"    run: script {Path('scripts', 'check.py')}",
    ]


def test_with_no_hooks_file_the_list_names_where_it_looked(capsys: pytest.CaptureFixture[str]) -> None:
    assert hooks_cli.main([]) == 0

    assert capsys.readouterr().out.splitlines() == [
        "No hooks configuration found. Searched:",
        f"  {_shown('.chrys', 'hooks')}",
        f"  {get_platform().config_dir / 'hooks'}",
    ]


@pytest.mark.parametrize("enabled", [True, False])
def test_the_list_says_whether_project_hooks_load(enabled: bool, capsys: pytest.CaptureFixture[str]) -> None:
    _project_hooks("hooks:\n  - id: p\n    event: after_turn\n    run: {type: shell, shell: s}\n")
    if enabled:
        _enable_project_hooks()

    assert hooks_cli.main(["list"]) == 0

    captured = capsys.readouterr()
    state = "loaded" if enabled else "not loaded: turn on Load project hooks under Settings → Safety → Project trust"
    assert captured.out.splitlines()[0] == f"Project hooks: {_shown('.chrys', 'hooks', 'hooks.yaml')} ({state})"
    # The listing says it on the file's own line; the startup warning would repeat it.
    assert captured.err == ""


def test_arguments_written_into_a_program_or_script_are_not_listed(capsys: pytest.CaptureFixture[str]) -> None:
    _global_hooks(
        """
        hooks:
          - id: n
            event: after_turn
            run: {type: command, argv: ['notify --token SECRET']}
          - id: s
            event: after_turn
            run: {type: script, path: 'scripts/s.py --token SECRET'}
        """
    )

    assert hooks_cli.main(["list"]) == 0
    out = capsys.readouterr().out
    assert "SECRET" not in out
    lines = out.splitlines()
    assert (lines[2], lines[4]) == ("    run: command …", "    run: script …")
    assert hooks_cli.main(["list", "--json"]) == 0
    out = capsys.readouterr().out
    assert "SECRET" not in out
    (global_,) = json.loads(out)["files"]
    assert [hook["run"] for hook in global_["hooks"]] == [
        {"type": "command", "path": None, "program": None},
        {"type": "script", "path": None, "program": None},
    ]


def test_a_program_or_script_that_can_be_found_is_listed_whole(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    program = _write(tmp_path / "my tools" / "notify", "")
    on_path = Path(sys.executable)
    monkeypatch.setenv("PATH", str(on_path.parent))
    path = _global_hooks(
        "hooks:\n"
        "  - id: n\n    event: after_turn\n"
        f"    run: {{type: command, argv: [{json.dumps(str(program))}]}}\n"
        "  - id: s\n    event: after_turn\n    run: {type: script, path: 'my scripts/s.py'}\n"
        "  - id: p\n    event: after_turn\n"
        f"    run: {{type: command, argv: [{json.dumps(on_path.name)}]}}\n"
    )
    _write(path.parent / "my scripts" / "s.py", "")

    assert hooks_cli.main(["list"]) == 0

    lines = capsys.readouterr().out.splitlines()
    assert (lines[2], lines[4], lines[6]) == (
        f"    run: command {program}",
        f"    run: script {Path('my scripts', 's.py')}",
        f"    run: command {on_path.name}",
    )


def test_the_list_names_a_hook_both_files_define(capsys: pytest.CaptureFixture[str]) -> None:
    project_path = _project_hooks("hooks:\n  - id: notify\n    event: after_turn\n    run: {type: shell, shell: s}\n")
    _enable_project_hooks()
    global_path = _global_hooks()

    assert hooks_cli.main(["list"]) == 0

    assert capsys.readouterr().out.splitlines()[-1] == (
        f"note: hook 'notify': defined in both project ({project_path}) and global ({global_path}) hooks configs. "
        "Both will run, project first. To suppress the global hook, set 'enabled: false' in the global file."
    )


def test_validate_warns_that_project_hooks_are_off(capsys: pytest.CaptureFixture[str]) -> None:
    _project_hooks("hooks:\n  - id: p\n    event: after_turn\n    run: {type: shell, shell: s}\n")

    assert hooks_cli.main(["validate"]) == 0

    captured = capsys.readouterr()
    assert captured.err.startswith("Warning: Found project hooks in ")
    assert captured.out.splitlines() == ["PASS 1 file · 1 hook"]


def test_an_invalid_project_file_that_is_off_says_it_is_off_first(capsys: pytest.CaptureFixture[str]) -> None:
    _project_hooks("hooks: {}\n")

    assert hooks_cli.main(["list"]) == 0
    assert capsys.readouterr().out.splitlines() == [
        (
            f"Project hooks: {_shown('.chrys', 'hooks', 'hooks.yaml')} (not loaded: turn on Load project hooks under "
            "Settings → Safety → Project trust; the file is also invalid: 'hooks' must be a list, got dict; "
            "run 'icode hook validate' for details)"
        )
    ]
    assert hooks_cli.main(["list", "--json"]) == 0
    (project,) = json.loads(capsys.readouterr().out)["files"]
    assert (project["state"], project["error"]["code"]) == ("not_loaded", "schema")


def test_an_invalid_file_is_listed_with_its_first_error(capsys: pytest.CaptureFixture[str]) -> None:
    path = _global_hooks(
        "hooks:\n  - id: x\n    event: after_turn\n    execution: {mode: block}\n    run: {type: shell, shell: s}\n"
    )

    assert hooks_cli.main(["list"]) == 0

    assert capsys.readouterr().out.splitlines() == [
        (
            f"Global hooks: {path} (invalid, not loaded: line 2: hook 'x': execution.mode must be 'blocking' / "
            "'async' / 'fire_and_forget', got 'block'; run 'icode hook validate' for details)"
        )
    ]


def test_the_list_as_json(capsys: pytest.CaptureFixture[str]) -> None:
    global_path = _global_hooks()
    _write(global_path.parent / "scripts" / "check.py", "")
    project_path = _project_hooks("hooks: {}\n")
    _enable_project_hooks()

    assert hooks_cli.main(["--json"]) == 0

    out = capsys.readouterr().out
    assert "SECRET" not in out
    data = json.loads(out)
    assert data["version"] == 1
    assert data["searched"] == [str(project_path.parent), str(global_path.parent)]
    project, global_ = data["files"]
    assert (project["scope"], project["path"], project["state"], project["hooks"]) == (
        "project",
        str(project_path),
        "invalid",
        [],
    )
    assert project["error"]["code"] == "schema"
    assert (global_["state"], global_["error"], global_["skipped"]) == ("loaded", None, [])
    notify, lint, check = global_["hooks"]
    assert notify == {
        "id": "notify",
        "event": "after_turn",
        "enabled": True,
        "description": "",
        "execution": {
            "mode": "fire_and_forget",
            "detach": False,
            "delivery": "best_effort",
            "timeout_seconds": 30.0,
            "on_error": "warn",
        },
        "match": {"profile": None, "profiles": [], "tool_kind": None, "tool_name": None, "args": {}},
        "run": {"type": "command", "path": None, "program": sys.executable},
    }
    assert lint["match"]["args"] == {"command": {"equals": None, "contains": "pytest", "regex": "\\.py$"}}
    assert lint["run"] == {"type": "shell", "path": None, "program": None}
    assert check["run"] == {"type": "script", "path": str(global_path.parent / "scripts" / "check.py"), "program": None}


def test_validate_passes_with_a_warning(capsys: pytest.CaptureFixture[str]) -> None:
    path = _global_hooks(
        "hooks:\n  - id: h\n    event: after_turn\n    match: {tool_name: x}\n    run: {type: shell, shell: s}\n"
    )

    assert hooks_cli.main(["validate"]) == 0

    assert capsys.readouterr().out.splitlines() == [
        f"{path}:2: warning: hook 'h': match.tool_name never matches on after_turn, which has no tool [match_never_fires]",
        "PASS 1 file · 1 hook · 1 warning",
    ]


def test_validate_reports_every_error_in_a_named_file(capsys: pytest.CaptureFixture[str]) -> None:
    _write(
        Path.cwd() / "my-hooks.yaml",
        """
        hooks:
          - id: a
            event: after_turn
            run: {type: nope}
          - id: b
            event: nope
            run: {type: shell, shell: s}
        """,
    )

    assert hooks_cli.main(["validate", "my-hooks.yaml"]) == 1

    shown = _shown("my-hooks.yaml")
    assert capsys.readouterr().out.splitlines() == [
        f"{shown}:2: error: hook 'a': run.type must be 'script', 'command', or 'shell', got 'nope' [schema]",
        f"{shown}:5: error: hook 'b': unknown event 'nope'.  Valid: " + _valid_events() + " [schema]",
        "FAIL 1 file · 2 hooks: 2 errors",
    ]


def _valid_events() -> str:
    return ", ".join(sorted(event.value for event in HookEvent))


def test_validate_as_json(capsys: pytest.CaptureFixture[str]) -> None:
    folder = _write(
        Path.cwd() / "hooks" / "hooks.yaml",
        "hooks:\n  - id: h\n    event: before_tool_call\n    run: {type: shell, shell: s}\n",
    ).parent

    assert hooks_cli.main(["validate", "hooks", "--json"]) == 0

    data = json.loads(capsys.readouterr().out)
    assert data == {
        "version": 1,
        "status": "pass",
        "files": [{"scope": "path", "path": str(folder / "hooks.yaml"), "loaded": None, "hook_count": 1}],
        "searched": [str(folder)],
        "diagnostics": [
            {
                "severity": "warning",
                "code": "broad_before_tool_call",
                "message": "a before_tool_call hook with no match filters; it will run for every tool call",
                "file": str(folder / "hooks.yaml"),
                "line": 2,
                "column": None,
                "hook_id": "h",
                "entry": 0,
            }
        ],
    }


def test_validate_as_json_says_a_rejected_file_does_not_load(capsys: pytest.CaptureFixture[str]) -> None:
    path = _global_hooks("hooks: {}\n")

    assert hooks_cli.main(["validate", "--json"]) == 1

    files = json.loads(capsys.readouterr().out)["files"]
    assert files == [{"scope": "global", "path": str(path), "loaded": False, "hook_count": 0}]


def test_a_script_in_a_linked_hooks_folder_is_shown_as_written(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    linked = _write(
        tmp_path / "dotfiles" / "hooks.yaml",
        "hooks:\n  - id: s\n    event: after_turn\n    run: {type: script, path: scripts/s.py}\n",
    ).parent
    _write(linked / "scripts" / "s.py", "")
    (Path.cwd() / ".chrys").mkdir()
    symlink_or_skip(Path.cwd() / ".chrys" / "hooks", linked, target_is_directory=True)
    _enable_project_hooks()

    assert hooks_cli.main(["list"]) == 0

    assert capsys.readouterr().out.splitlines()[2] == f"    run: script {Path('scripts', 's.py')}"


def test_validate_fails_on_a_path_that_does_not_exist(capsys: pytest.CaptureFixture[str]) -> None:
    assert hooks_cli.main(["validate", "nope.yaml"]) == 1

    shown = _shown("nope.yaml")
    assert capsys.readouterr().out.splitlines() == [
        f"{shown}: error: no such file or folder: {Path.cwd() / 'nope.yaml'} [not_found]",
        "FAIL 0 files · 0 hooks: 1 error",
    ]
