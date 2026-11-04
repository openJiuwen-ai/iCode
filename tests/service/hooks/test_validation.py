# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Static checks of hooks files: every bad entry reported with its line, and what a session would load."""

from __future__ import annotations

import errno
import json
import sys
import textwrap
from pathlib import Path

import pytest

from chrys.foundation.platform import script_commands
from chrys.service.hooks import validation
from chrys.service.hooks.loader import HookEntryError, HooksConfigError, load_hooks_file, parse_hooks_dict
from chrys.service.hooks.validation import (
    HookDiagnostic,
    HooksReport,
    check_default_sources,
    check_path,
    default_sources,
)


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(text).lstrip("\n"), encoding="utf-8")
    return path


def _check(tmp_path: Path, text: str) -> HooksReport:
    return check_path(_write(tmp_path / "hooks" / "hooks.yaml", text), config_dir=tmp_path / "config")


def _codes(report: HooksReport) -> list[tuple[str, str]]:
    return [(diagnostic.severity, diagnostic.code) for diagnostic in report.diagnostics]


def _only(report: HooksReport, code: str) -> HookDiagnostic:
    found = [diagnostic for diagnostic in report.diagnostics if diagnostic.code == code]
    assert len(found) == 1, report.diagnostics
    return found[0]


def test_every_bad_entry_is_reported_on_its_line(tmp_path: Path) -> None:
    report = _check(
        tmp_path,
        """
        hooks:
          - id: first
            event: after_turn
            run: {type: shell, shell: s}
            execution: {mode: block}
          - id: fine
            event: after_turn
            run: {type: shell, shell: s}
          - event: after_turn
            run: {type: shell, shell: s}
          - id: fine
            event: after_turn
            run: {type: shell, shell: s}
        """,
    )

    schema = [diagnostic for diagnostic in report.diagnostics if diagnostic.code == "schema"]
    assert [(d.line, d.entry, d.hook_id) for d in schema] == [(2, 0, "first"), (9, 2, None), (11, 3, "fine")]
    assert "execution.mode must be" in schema[0].message
    assert schema[2].message == "duplicate id 'fine'"
    assert not report.passed
    assert report.files[0].hooks_file is None
    assert report.files[0].rejection == schema[0]
    assert report.hook_count == 4


def test_collecting_and_raising_agree_on_the_first_error(tmp_path: Path) -> None:
    raw = {
        "hooks": [
            {"id": "ok", "event": "after_turn", "run": {"type": "shell", "shell": "s"}},
            {"id": "bad", "event": "after_turn", "run": {"type": "nope"}},
            {"id": "worse", "event": "nope", "run": {"type": "shell", "shell": "s"}},
        ]
    }
    errors: list[HookEntryError] = []
    collected = parse_hooks_dict(raw, entry_errors=errors)

    assert [hook.id for hook in collected.hooks] == ["ok"]
    assert [(error.index, error.hook_id) for error in errors] == [(1, "bad"), (2, "worse")]
    with pytest.raises(HooksConfigError) as raised:
        parse_hooks_dict(raw)
    assert str(raised.value) == f"hooks[1]: {errors[0].detail}"


def test_a_session_still_refuses_the_whole_file(tmp_path: Path) -> None:
    path = _write(
        tmp_path / "hooks.yaml",
        """
        hooks:
          - id: fine
            event: after_turn
            run: {type: shell, shell: s}
          - id: bad
            event: after_turn
            run: {type: nope}
        """,
    )
    with pytest.raises(HooksConfigError, match=r"hooks\[1\]: run.type must be"):
        load_hooks_file(path)


@pytest.mark.parametrize(
    ("name", "text", "line", "column"),
    [
        pytest.param("hooks.yaml", "hooks:\n  - id: a\n   event: after_turn\n", 3, 4, id="yaml"),
        pytest.param("hooks.json", '{"hooks": [\n  {"id": "a",}\n]}', 2, 13, id="json"),
    ],
)
def test_a_syntax_error_names_its_place_without_quoting_the_file(
    tmp_path: Path, name: str, text: str, line: int, column: int
) -> None:
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")

    diagnostic = _only(check_path(path, config_dir=tmp_path), "syntax")

    assert (diagnostic.severity, diagnostic.line, diagnostic.column) == ("error", line, column)
    assert "\n" not in diagnostic.message
    assert "event" not in diagnostic.message


@pytest.mark.parametrize(
    ("value", "message"),
    [
        pytest.param("*SECRET_TOKEN", "found undefined alias '…'", id="alias"),
        pytest.param("!SECRET_TOKEN", "could not determine a constructor for the tag '…'", id="tag"),
        # repr() quotes a tag holding a single quote in double quotes.
        pytest.param("!SECRET'TOKEN", "could not determine a constructor for the tag '…'", id="double-quoted"),
        pytest.param('"ab\\qSECRET"', "found unknown escape character 'q'", id="one-character"),
    ],
)
def test_a_syntax_error_leaves_out_the_value_it_quotes(tmp_path: Path, value: str, message: str) -> None:
    text = f"hooks:\n  - id: a\n    event: after_turn\n    run:\n      type: shell\n      shell: s\n      env:\n        TOKEN: {value}\n"

    diagnostic = _only(_check(tmp_path, text), "syntax")

    assert diagnostic.line == 8
    assert message in diagnostic.message
    assert "SECRET" not in diagnostic.message


@pytest.mark.parametrize(
    "value",
    ["2026-13-01", "!!int SECRET", "!!bool SECRET", "!!timestamp SECRET"],
    ids=["date", "number", "bool", "tag"],
)
def test_a_value_that_is_not_what_it_is_written_as_rejects_the_file(tmp_path: Path, value: str) -> None:
    text = f"hooks:\n  - id: a\n    event: after_turn\n    description: {value}\n    run: {{type: shell, shell: s}}\n"

    diagnostic = _only(_check(tmp_path, text), "syntax")

    assert diagnostic.message.startswith("a value is not valid as the type it is written as")
    assert "SECRET" not in diagnostic.message
    with pytest.raises(HooksConfigError, match="a value is not valid as the type it is written as"):
        load_hooks_file(tmp_path / "hooks" / "hooks.yaml")


def test_an_undecodable_file_is_an_error(tmp_path: Path) -> None:
    path = tmp_path / "hooks.yaml"
    path.write_bytes("hooks:\n  - id: 中文说明中文说明中文说明中文说明\n".encode() + b"    event: abc\xffdef\n")

    diagnostic = _only(check_path(path, config_dir=tmp_path), "decode")

    assert diagnostic.severity == "error"


def test_a_root_error_rejects_the_file(tmp_path: Path) -> None:
    report = _check(tmp_path, "hooks: {}\n")

    assert _codes(report) == [("error", "schema")]
    assert report.diagnostics[0].line is None
    assert report.files[0].hooks_file is None


def test_a_bad_regex_leaves_only_its_hook_out(tmp_path: Path) -> None:
    report = _check(
        tmp_path,
        """
        hooks:
          - id: fine
            event: after_tool_call
            run: {type: shell, shell: s}
          - id: bad-regex
            event: after_tool_call
            match: {args: {command: {regex: "("}}}
            run: {type: shell, shell: s}
        """,
    )

    diagnostic = _only(report, "regex_invalid")
    assert (diagnostic.severity, diagnostic.line, diagnostic.hook_id) == ("error", 5, "bad-regex")
    hooks_file = report.files[0].hooks_file
    assert hooks_file is not None
    assert [hook.id for hook in hooks_file.hooks] == ["fine"]
    assert report.files[0].rejection is None


def test_a_bad_regex_is_an_error_in_a_hook_that_is_off(tmp_path: Path) -> None:
    report = _check(
        tmp_path,
        """
        hooks:
          - id: paused
            enabled: false
            event: after_tool_call
            match: {args: {command: {regex: "("}}}
            run: {type: shell, shell: s}
        """,
    )

    diagnostic = _only(report, "regex_invalid")
    assert (diagnostic.severity, diagnostic.line, diagnostic.hook_id) == ("error", 2, "paused")
    assert diagnostic.message.startswith("match.args.command.regex is not a valid regular expression: ")
    assert not report.passed


@pytest.mark.parametrize(
    ("entry", "message"),
    [
        pytest.param(
            "run: {type: shell, shell: s, argv: 1}", "run.argv must be a list of strings", id="argv-not-a-list"
        ),
        # YAML 1.1 reads the key ``on`` as true, which does not sort with the other, a string.
        pytest.param("run: {type: shell, shell: s}\n    on: x\n    command: y", "unknown keys", id="keys-of-two-types"),
    ],
)
def test_a_field_of_the_wrong_type_is_a_schema_error_a_session_reports(
    tmp_path: Path, entry: str, message: str
) -> None:
    text = f"hooks:\n  - id: h\n    event: after_turn\n    {entry}\n"

    diagnostic = _only(_check(tmp_path, text), "schema")

    assert (diagnostic.line, diagnostic.hook_id) == (2, "h")
    assert message in diagnostic.message
    with pytest.raises(HooksConfigError, match=message):
        load_hooks_file(tmp_path / "hooks" / "hooks.yaml")


def test_a_tab_indented_json_file_keeps_its_line_numbers(tmp_path: Path) -> None:
    path = tmp_path / "hooks.json"
    path.write_text(
        '{"hooks": [\n'
        '\t{"id": "a", "event": "after_turn", "run": {"type": "shell", "shell": "s"}},\n'
        '\t{"id": "b", "event": "nope", "run": {"type": "shell", "shell": "s"}}\n'
        "]}\n",
        encoding="utf-8",
    )

    diagnostic = _only(check_path(path, config_dir=tmp_path), "schema")

    assert (diagnostic.line, diagnostic.hook_id) == (3, "b")


def test_an_expression_in_a_variable_is_not_quoted(tmp_path: Path) -> None:
    text = _hook("after_turn", run="{type: shell, shell: s, env: {TOKEN: '${API_TOKEN:-SECRET}'}}")

    diagnostic = _only(_check(tmp_path, text), "unknown_template_var")

    assert diagnostic.message.startswith("run.env.TOKEN uses a ${...} expression, which is passed on as written")
    assert "SECRET" not in diagnostic.message


def test_a_missing_script_is_an_error_unless_the_hook_is_off(tmp_path: Path) -> None:
    _write(tmp_path / "hooks" / "scripts" / "here.py", "print('hi')\n")
    report = _check(
        tmp_path,
        """
        hooks:
          - id: here
            event: after_turn
            run: {type: script, path: scripts/here.py}
          - id: gone
            event: after_turn
            run: {type: script, path: scripts/gone.py}
          - id: gone-but-off
            enabled: false
            event: after_turn
            run: {type: script, path: scripts/gone.py}
        """,
    )

    diagnostic = _only(report, "script_missing")
    assert (diagnostic.severity, diagnostic.hook_id) == ("error", "gone")
    assert diagnostic.message == (
        "the script in run.path was not found; a relative path starts from the folder of this file"
    )
    assert report.files[0].rejection is None


def _hook(event: str, *, match: str = "{}", execution: str = "{}", run: str = "{type: shell, shell: s}") -> str:
    return f"hooks:\n  - id: h\n    event: {event}\n    match: {match}\n    execution: {execution}\n    run: {run}\n"


_WARNINGS = {
    "tool-match-on-a-turn-event": (
        _hook("after_turn", match="{tool_kind: shell}"),
        "match_never_fires",
        "match.tool_kind never matches on after_turn",
    ),
    "misspelt-tool-kind": (
        _hook("after_tool_call", match="{tool_kind: shel}"),
        "unknown_tool_kind",
        "did you mean 'shell'?",
    ),
    "malformed-extension-kind": (
        _hook("after_tool_call", match="{tool_kind: extension.Acme-Release}"),
        "unknown_tool_kind",
        "match.tool_kind 'extension.Acme-Release' is not a tool kind",
    ),
    "profile-in-the-wrong-case": (
        _hook("after_turn", match="{profile: code}"),
        "unknown_profile",
        "did you mean 'Code'?",
    ),
    "unknown-profile-in-a-list": (
        _hook("after_turn", match="{profiles: [QA, Nobody]}"),
        "unknown_profile",
        "match.profiles 'Nobody'",
    ),
    "block-on-error-of-an-async-hook": (
        _hook("before_tool_call", match="{tool_kind: shell}", execution="{mode: async, on_error: block}"),
        "on_error_block_ignored",
        "handled as 'warn'",
    ),
    "block-on-error-of-an-event-that-cannot-stop": (
        _hook("after_turn", execution="{mode: blocking, on_error: block}"),
        "on_error_block_ignored",
        "handled as 'warn'",
    ),
    "durable-blocking": (
        _hook("after_turn", execution="{mode: blocking, delivery: durable}"),
        "durable_blocking",
        "blocking hooks are awaited inline",
    ),
    "unfiltered-before-tool-call": (_hook("before_tool_call"), "broad_before_tool_call", "every tool call"),
    "retired-shell-argument-name": (
        _hook("before_tool_call", match="{tool_kind: shell, args: {working_dir: {contains: prod}}}"),
        "retired_shell_arg",
        "'working_dir' is now 'workdir'",
    ),
    "unknown-variable-in-cwd": (
        _hook("after_turn", run="{type: shell, shell: s, cwd: '${home}/x'}"),
        "unknown_template_var",
        "run.cwd uses ${home}",
    ),
    "unknown-variable-in-env": (
        _hook("after_turn", run="{type: shell, shell: s, env: {A: '${HOME}'}}"),
        "unknown_template_var",
        "run.env.A uses ${HOME}",
    ),
    "variable-in-the-program": (
        _hook("after_turn", run="{type: command, argv: ['${chrys_home}/tool']}"),
        "unknown_template_var",
        "run.argv[0] uses ${chrys_home}, which is passed on as written; variables are filled in only in run.cwd",
    ),
    "variable-in-an-argument": (
        _hook("after_turn", run="{type: command, argv: [./bin/tool], args: [--dir, '${workspace_cwd}']}"),
        "unknown_template_var",
        "run.args[1] uses ${workspace_cwd}, which is passed on as written",
    ),
    "program-not-on-path": (
        _hook("after_turn", run="{type: command, argv: [no-such-program-for-hooks]}"),
        "program_not_found",
        "the program in run.argv[0] was not found on PATH",
    ),
}


@pytest.mark.parametrize(("text", "code", "message"), list(_WARNINGS.values()), ids=list(_WARNINGS))
def test_warnings_name_what_will_not_work_as_written(tmp_path: Path, text: str, code: str, message: str) -> None:
    report = _check(tmp_path, text)

    diagnostic = _only(report, code)
    assert diagnostic.severity == "warning"
    assert message in diagnostic.message
    assert (diagnostic.line, diagnostic.hook_id) == (2, "h")
    assert report.passed


_FINE = {
    "tool-match-on-a-tool-event": _hook("after_tool_call", match="{tool_kind: shell, profile: Code}"),
    # An extension's kind is fine whether or not the extension is installed here.
    "extension-kind": _hook("after_tool_call", match="{tool_kind: extension.acme_release}"),
    "empty-profile": _hook("after_turn", match="{profile: ''}"),
    "block-on-error-of-a-gate": _hook(
        "before_tool_call", match="{tool_name: write_file}", execution="{mode: blocking, on_error: block}"
    ),
    "known-variables": _hook(
        "after_turn", run="{type: shell, shell: s, cwd: '${workspace_cwd}/x', env: {A: '${profile}'}}"
    ),
    "program-on-the-hooks-own-path": _hook(
        "after_turn", run="{type: command, argv: [no-such-program-for-hooks], env: {PATH: /opt/bin}}"
    ),
    "program-relative-to-the-working-folder": _hook("after_turn", run="{type: command, argv: [./bin/tool]}"),
    "arguments-a-shell-hook-does-not-read": _hook(
        "after_turn", run="{type: shell, shell: s, argv: ['${x}'], args: ['${x}']}"
    ),
}


@pytest.mark.parametrize("text", list(_FINE.values()), ids=list(_FINE))
def test_a_hook_that_works_as_written_draws_no_warning(tmp_path: Path, text: str) -> None:
    assert _check(tmp_path, text).diagnostics == ()


def test_a_user_agent_profile_is_a_known_name(tmp_path: Path) -> None:
    _write(tmp_path / "config" / "agents" / "Reviewer.yaml", "name: Reviewer\ndescription: Reviews\n")

    report = _check(tmp_path, _hook("after_turn", match="{profile: Reviewer}"))

    assert report.diagnostics == ()


def test_an_absolute_program_that_does_not_exist_is_reported(tmp_path: Path) -> None:
    program = json.dumps(str(tmp_path / "missing-tool"))
    report = _check(tmp_path, _hook("after_turn", run=f"{{type: command, argv: [{program}]}}"))

    assert _only(report, "program_not_found").message == (
        "the program in run.argv[0] does not exist or is not executable"
    )


@pytest.mark.skipif(sys.platform == "win32", reason="the script interpreter lookup differs on Windows")
def test_a_script_whose_interpreter_is_missing_is_named(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write(tmp_path / "hooks" / "notify.rb", "puts 1\n")
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))

    report = _check(tmp_path, _hook("after_turn", run="{type: script, path: notify.rb}"))

    assert "'ruby', which runs .rb scripts, was not found on PATH" in _only(report, "program_not_found").message


def test_a_shell_script_with_no_bash_to_run_it_says_why(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def no_bash() -> str:
        raise FileNotFoundError(errno.ENOENT, script_commands._NO_WINDOWS_BASH)

    _write(tmp_path / "hooks" / "notify.sh", "echo 1\n")
    monkeypatch.setattr(script_commands, "_find_bash", no_bash)

    report = _check(tmp_path, _hook("after_turn", run="{type: script, path: notify.sh}"))

    diagnostic = _only(report, "program_not_found")
    assert (diagnostic.severity, diagnostic.message) == ("warning", script_commands._NO_WINDOWS_BASH)


def test_a_command_that_is_the_store_python_placeholder_is_reported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    placeholder = r"C:\Users\u\AppData\Local\Microsoft\WindowsApps\python3.exe"
    monkeypatch.setattr(validation.shutil, "which", lambda program: placeholder if program == "python3" else None)
    monkeypatch.setattr(validation, "is_windows_store_python_stub", lambda path: path == placeholder)

    report = _check(tmp_path, _hook("after_turn", run="{type: command, argv: [python3, guard.py]}"))

    assert _only(report, "program_not_found").message == (
        "on PATH, the program in run.argv[0] is only Windows' placeholder that offers Python from the Microsoft "
        "Store; write the full path of an installed Python"
    )


@pytest.mark.parametrize(
    "argv",
    [
        pytest.param("['notify --token SECRET']", id="command-in-one-item"),
        pytest.param("['API_TOKEN=SECRET notify']", id="variable-before-the-command"),
        pytest.param("[API_TOKEN=SECRET, notify]", id="variable-as-the-program"),
    ],
)
def test_a_program_item_holding_more_than_a_program_is_not_quoted(tmp_path: Path, argv: str) -> None:
    report = _check(tmp_path, _hook("after_turn", run=f"{{type: command, argv: {argv}}}"))

    assert _only(report, "program_not_found").message == (
        "the program in run.argv[0] was not found on PATH; if run.argv[0] also holds arguments or sets variables, "
        "put each argument in its own run.argv item and variables in run.env"
    )


@pytest.mark.parametrize(
    ("script", "hint"),
    [
        pytest.param(
            "'scripts/n.py --token SECRET'", "; if run.path also holds arguments, put them in run.args", id="args"
        ),
        pytest.param("scripts/n.py", "", id="no-args"),
    ],
)
def test_a_missing_script_path_is_not_quoted(tmp_path: Path, script: str, hint: str) -> None:
    # The hint reads only what was written, not the folder's own path, which may hold spaces.
    path = _write(tmp_path / "my hooks" / "hooks.yaml", _hook("after_turn", run=f"{{type: script, path: {script}}}"))

    assert _only(check_path(path, config_dir=tmp_path / "config"), "script_missing").message == (
        f"the script in run.path was not found; a relative path starts from the folder of this file{hint}"
    )


def _project_and_global(tmp_path: Path, project_text: str, global_text: str) -> tuple[Path, Path]:
    project_root = tmp_path / "project"
    config_dir = tmp_path / "config"
    _write(project_root / ".chrys" / "hooks" / "hooks.yaml", project_text)
    _write(config_dir / "hooks" / "hooks.yaml", global_text)
    return project_root, config_dir


@pytest.mark.parametrize("enabled", [True, False])
def test_the_project_file_comes_first_and_loads_only_when_enabled(tmp_path: Path, enabled: bool) -> None:
    project_root, config_dir = _project_and_global(tmp_path, _hook("after_turn"), _hook("after_turn"))

    report = check_default_sources(config_dir, project_root, project_hooks_enabled=enabled)

    assert [(f.source.scope, f.source.loaded) for f in report.files] == [("project", enabled), ("global", True)]
    # Both use the id ``h``: a session runs both only while both load.
    assert [d.code for d in report.diagnostics] == (["duplicate_id_across_files"] if enabled else [])
    assert report.searched == (str(project_root / ".chrys" / "hooks"), str(config_dir / "hooks"))


@pytest.mark.parametrize("turned_off", ["project", "global"])
def test_a_hook_turned_off_in_either_file_does_not_run_twice(tmp_path: Path, turned_off: str) -> None:
    off = "hooks:\n  - id: h\n    event: after_turn\n    enabled: false\n    run: {type: shell, shell: s}\n"
    on = _hook("after_turn")
    project_root, config_dir = _project_and_global(
        tmp_path, off if turned_off == "project" else on, off if turned_off == "global" else on
    )

    report = check_default_sources(config_dir, project_root, project_hooks_enabled=True)

    assert report.diagnostics == ()


def test_a_project_folder_that_is_the_users_own_names_one_file(tmp_path: Path) -> None:
    home = tmp_path / "home"
    _write(home / ".chrys" / "hooks" / "hooks.yaml", _hook("after_turn"))

    sources = default_sources(home / ".chrys", home, project_hooks_enabled=True)

    assert [(source.scope, source.path) for source in sources] == [("global", home / ".chrys" / "hooks" / "hooks.yaml")]


def test_a_second_hooks_file_in_the_folder_is_ignored(tmp_path: Path) -> None:
    _write(tmp_path / "hooks" / "hooks.json", "{}")
    _write(tmp_path / "hooks" / "hooks.yml", _hook("after_turn"))

    report = check_path(tmp_path / "hooks", config_dir=tmp_path / "config")

    diagnostic = _only(report, "shadowed_file")
    assert (diagnostic.severity, diagnostic.file) == ("warning", str(tmp_path / "hooks" / "hooks.json"))
    assert "hooks.yml" in diagnostic.message


def test_a_folder_is_read_like_a_session_reads_it(tmp_path: Path) -> None:
    _write(tmp_path / "hooks" / "hooks.yaml", _hook("after_turn"))

    report = check_path(tmp_path / "hooks", config_dir=tmp_path / "config")

    assert [checked.source.path for checked in report.files] == [tmp_path / "hooks" / "hooks.yaml"]
    assert report.passed


@pytest.mark.parametrize(("make_dir", "code"), [(True, "no_config"), (False, "not_found")])
def test_nothing_to_check_fails(tmp_path: Path, make_dir: bool, code: str) -> None:
    target = tmp_path / "hooks"
    if make_dir:
        target.mkdir()

    report = check_path(target, config_dir=tmp_path)

    assert _codes(report) == [("error", code)]
    assert not report.passed


def test_no_hooks_file_anywhere_fails(tmp_path: Path) -> None:
    report = check_default_sources(tmp_path / "config", tmp_path / "project", project_hooks_enabled=True)

    assert _codes(report) == [("error", "no_config")]
    assert report.files == ()
