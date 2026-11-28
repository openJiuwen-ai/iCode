# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Workspace definitions can be managed from a single CLI invocation."""

from __future__ import annotations

import json
import sys
from types import SimpleNamespace
from unittest.mock import create_autospec

import pytest

from chrys.app.cli import app, workspaces
from chrys.foundation.platform.files import atomic_write_owner_only_text
from chrys.service.workspaces import registry as registry_module
from chrys.service.workspaces.registry import WorkspaceRegistry


@pytest.fixture
def registry(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    for variable in ("HOME", "USERPROFILE", "APPDATA"):
        monkeypatch.setenv(variable, str(home))
    monkeypatch.setenv("COLUMNS", "4096")
    monkeypatch.setenv("NO_COLOR", "1")
    monkeypatch.setattr(
        workspaces,
        "bootstrap_runtime",
        create_autospec(
            workspaces.bootstrap_runtime,
            return_value=SimpleNamespace(warnings=[], loaded=SimpleNamespace(trust_conflicts=())),
        ),
    )
    return WorkspaceRegistry()


def test_new_then_list_text_and_json(registry, tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    assert workspaces.main(["new", "My Team"]) == 0
    (created,) = registry.snapshot().workspaces
    assert capsys.readouterr() == (
        f"Created workspace “My Team”. To add reference folders, run: icode workspace add-folder {created.id} <path>\n",
        "",
    )
    assert created.primary_cwd == str(tmp_path)
    assert created.working_dirs == []
    assert workspaces.main([]) == 0
    width = max(len(str(tmp_path)), len("Primary folder"))
    assert [line.rstrip() for line in capsys.readouterr().out.splitlines()] == [
        f"{'Name':8} {'Primary folder':{width}}  Reference folders",
        f"{'My Team':8} {tmp_path}",
    ]
    assert workspaces.main(["--json"]) == 0
    output = capsys.readouterr()
    assert output.err == ""
    assert json.loads(output.out) == {
        "workspaces": [
            {
                "id": created.id,
                "name": "My Team",
                "primary_folder": str(tmp_path),
                "reference_folders": [],
                "missing_folders": [],
            }
        ]
    }
    workspaces.bootstrap_runtime.assert_called_with(configure_stdio=True, setup_telemetry=False)


def test_rename_add_remove_and_delete_clears_membership(registry, tmp_path, monkeypatch, capsys):
    primary = tmp_path / "primary"
    reference = tmp_path / "Reference Files"
    primary.mkdir()
    reference.mkdir()
    target = registry.create("Team", str(primary))
    registry.set_session_workspace("chat", target.id)
    monkeypatch.chdir(tmp_path)
    assert workspaces.main(["rename", target.id, " Renamed Team "]) == 0
    assert capsys.readouterr() == ("Renamed the workspace to “Renamed Team”.\n", "")
    renamed = registry.snapshot().workspace(target.id)
    assert renamed.name == "Renamed Team"
    assert renamed.primary_cwd == str(primary)
    assert workspaces.main(["add-folder", "Renamed Team", "Reference Files"]) == 0
    assert capsys.readouterr() == (f"Added reference folder {reference}.\n", "")
    assert [folder.path for folder in registry.snapshot().workspace(target.id).working_dirs] == [str(reference)]
    assert workspaces.main(["remove-folder", target.id, "Reference Files"]) == 0
    assert capsys.readouterr() == (f"Removed reference folder {reference}.\n", "")
    assert registry.snapshot().workspace(target.id).working_dirs == []
    assert workspaces.main(["delete", target.id]) == 0
    assert capsys.readouterr() == ("Deleted workspace “Renamed Team”.\n", "")
    assert registry.snapshot().workspaces == ()
    assert dict(registry.snapshot().sessions) == {}
    assert primary.is_dir() and reference.is_dir()


@pytest.mark.parametrize("selector", ["tEaM", "id", "prefix"])
def test_selectors_accept_name_full_id_and_unique_prefix(registry, tmp_path, capsys, selector):
    target = registry.create("Team", str(tmp_path))
    selectors = {"id": target.id, "prefix": target.id[:-1]}
    assert workspaces.main(["rename", selectors.get(selector, selector), "Selected"]) == 0
    assert capsys.readouterr() == ("Renamed the workspace to “Selected”.\n", "")
    assert registry.snapshot().workspace(target.id).name == "Selected"


@pytest.mark.parametrize("selector", ["ambiguous", "unknown", "", "w", "ws", "ws_"])
def test_selector_errors_are_exact(registry, tmp_path, monkeypatch, capsys, selector):
    primary = tmp_path / "other"
    primary.mkdir()
    ids = iter(["a" * 31 + "1", "a" * 31 + "2"])
    monkeypatch.setattr(registry_module, "uuid4", lambda: SimpleNamespace(hex=next(ids)))
    first = registry.create("Team", str(tmp_path))
    registry.create("Other", str(primary))
    if selector == "ambiguous":
        selector = first.id[:-1]
        message = f"Workspace selector is ambiguous: {selector}"
    else:
        message = f"No workspace matches {selector}."
    before = registry.snapshot()
    assert workspaces.main(["delete", selector]) == 1
    assert capsys.readouterr() == ("", f"Error: {message}\n")
    assert registry.snapshot() == before


def test_exact_name_wins_over_a_short_id_prefix(registry, tmp_path, capsys):
    other = tmp_path / "other"
    other.mkdir()
    named = registry.create("ws", str(tmp_path))
    registry.create("Team", str(other))
    assert workspaces.main(["rename", "ws", "Renamed"]) == 0
    assert capsys.readouterr() == ("Renamed the workspace to “Renamed”.\n", "")
    assert registry.snapshot().workspace(named.id).name == "Renamed"


def test_folder_validation_matches_interactive_commands(registry, tmp_path, monkeypatch, capsys):
    target = registry.create("Team", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    missing = tmp_path / "missing"
    assert workspaces.main(["add-folder", "Team", str(missing)]) == 1
    assert capsys.readouterr() == ("", f"Error: Folder does not exist: {missing}.\n")
    assert registry.snapshot().workspace(target.id) == target


def test_duplicate_new_reports_registry_error(registry, tmp_path, monkeypatch, capsys):
    registry.create("Team", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    assert workspaces.main(["new", "tEaM"]) == 1
    assert capsys.readouterr() == ("", "Error: A workspace with the name tEaM already exists.\n")
    assert len(registry.snapshot().workspaces) == 1


@pytest.mark.parametrize("action", ["rename", "add-folder", "remove-folder", "delete"])
def test_expected_target_refuses_concurrent_edit(registry, tmp_path, monkeypatch, capsys, action):
    primary = tmp_path / "primary"
    primary.mkdir()
    reference = tmp_path / "reference"
    reference.mkdir()
    target = registry.create("Team", str(primary), [str(reference)] if action == "remove-folder" else [])
    registry.set_session_workspace("chat", target.id)
    resolve = workspaces.resolve_workspace_selector

    def resolve_then_change(selector: str):
        result = resolve(selector)
        registry.update(target.id, name="Changed elsewhere", primary=target.primary_cwd, folders=[str(reference)])
        return result

    monkeypatch.setattr(workspaces, "resolve_workspace_selector", resolve_then_change)
    args = [action, target.id]
    if action == "rename":
        args.append("My rename")
    elif action != "delete":
        args.append(str(reference))
    assert workspaces.main(args) == 1
    assert capsys.readouterr() == ("", "Error: The workspace changed. Try again.\n")
    (current,) = registry.snapshot().workspaces
    assert current.id == target.id
    assert current.name == "Changed elsewhere"
    assert current.primary_cwd == str(primary)
    assert [folder.path for folder in current.working_dirs] == [str(reference)]
    assert dict(registry.snapshot().sessions) == {"chat": target.id}


def test_empty_plural_defaults_to_text_and_json_lists(registry, monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["icode", "workspaces"])
    assert app.main() == 0
    assert capsys.readouterr() == ("No workspaces.\n", "")
    assert workspaces.main(["list", "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == {"workspaces": []}


@pytest.mark.parametrize(
    "args",
    [
        ["new"],
        ["rename", "Team"],
        ["add-folder", "Team"],
        ["remove-folder", "Team"],
        ["add-folder", "Team", ""],
        ["remove-folder", "Team", ""],
        ["delete"],
        ["switch", "Team"],
        ["new", "Team", "--json"],
        ["delete", "Team", "--json"],
        ["new", "Team", "-C", "."],
    ],
)
def test_invalid_arguments_exit_two_before_bootstrap(registry, capsys, args):
    with pytest.raises(SystemExit) as error:
        workspaces.main(args)
    assert error.value.code == 2
    assert capsys.readouterr().out == ""
    workspaces.bootstrap_runtime.assert_not_called()


def test_new_refuses_missing_launch_directory(registry, monkeypatch, capsys):
    def missing_cwd() -> str:
        raise FileNotFoundError

    monkeypatch.setattr(workspaces, "os", SimpleNamespace(getcwd=missing_cwd))
    from chrys.app.cli import launch_cwd

    monkeypatch.setattr(launch_cwd, "os", SimpleNamespace(getcwd=missing_cwd))
    assert workspaces.main(["new", "Team"]) == 1
    assert capsys.readouterr() == ("", "Error: The current directory no longer exists; cd to an existing directory.\n")
    assert registry.snapshot().workspaces == ()


@pytest.mark.parametrize("as_json", [False, True])
def test_unreadable_registry_reports_its_own_error(registry, capsys, as_json):
    registry.path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_owner_only_text(registry.path, "broken")
    assert workspaces.main(["list", *(["--json"] if as_json else [])]) == 1
    output = capsys.readouterr()
    assert output.out == ""
    message = f"Cannot read the workspace list: {registry.path}"
    if as_json:
        assert json.loads(output.err) == {"error": message, "code": "workspace_registry_unreadable"}
    else:
        assert output.err == f"Error: {message}\n"


def test_list_keeps_registry_order_and_missing_folders(registry, tmp_path, capsys):
    primary, first, second = (tmp_path / name for name in ("primary", "first", "second"))
    for folder in (primary, first, second):
        folder.mkdir()
    target = registry.create("Z first", str(primary), [str(first), str(second)])
    other = registry.create("A second", str(second))
    primary.rmdir()
    second.rmdir()
    assert workspaces.main(["list", "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == {
        "workspaces": [
            {
                "id": target.id,
                "name": "Z first",
                "primary_folder": str(primary),
                "reference_folders": [str(first), str(second)],
                "missing_folders": [str(primary), str(second)],
            },
            {
                "id": other.id,
                "name": "A second",
                "primary_folder": str(second),
                "reference_folders": [],
                "missing_folders": [str(second)],
            },
        ]
    }
    assert workspaces.main(["list"]) == 0
    lines = capsys.readouterr().out.splitlines()
    name_width, primary_width = len("A second"), max(len(str(primary)), len(str(second))) + len(" (missing)")
    assert [line.rstrip() for line in lines] == [
        f"{'Name':{name_width}}  {'Primary folder':{primary_width}}  Reference folders",
        f"{'Z first':{name_width}}  {str(primary) + ' (missing)':{primary_width}}  {first}",
        f"{'':{name_width}}  {'':{primary_width}}  {second} (missing)",
        f"{'A second':{name_width}}  {second} (missing)",
    ]


def test_terminal_paths_are_sanitized_but_json_retains_identity(registry, tmp_path, capsys):
    # Control characters are valid POSIX filenames; probe before using them on Windows.
    primary = tmp_path / "primary\n\x1b[red]"
    try:
        primary.mkdir()
    except OSError:
        pytest.skip("Filesystem does not support control characters in paths")
    registry.create("[red]Team", str(primary))
    assert workspaces.main(["list"]) == 0
    text = capsys.readouterr().out
    assert "[red]Team" in text
    assert str(primary).replace("\n", "�").replace("\x1b", "�") in text
    assert "\x1b" not in text
    assert workspaces.main(["list", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["workspaces"][0]["primary_folder"] == str(primary)


def test_bootstrap_warnings_reach_stderr(registry, capsys):
    workspaces.bootstrap_runtime.return_value.warnings = [
        SimpleNamespace(message="Check config", code="config_warning")
    ]
    assert workspaces.main([]) == 0
    assert capsys.readouterr() == ("No workspaces.\n", "Warning: Check config\n")
