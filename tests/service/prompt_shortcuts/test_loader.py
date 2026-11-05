# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Spec tests for loading prompt shortcut files from global and project folders."""

from __future__ import annotations

import dataclasses
import logging
import os
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

import chrys.service.prompt_shortcuts.loader as loader_module
from chrys.service.prompt_shortcuts import PromptShortcut, load_prompt_shortcuts, project_roots, prompt_shortcut_sources
from chrys.service.prompt_shortcuts.loader import working_folder_dirs
from tests.support.symlinks import symlink_or_skip


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _by_name(commands: list[PromptShortcut]) -> dict[str, PromptShortcut]:
    names = [command.name for command in commands]
    assert len(names) == len(set(names)), names
    return {command.name: command for command in commands}


def _same(a: str | Path, b: Path) -> bool:
    return Path(a).resolve() == b.resolve()


def _under(path: str, folder: Path) -> bool:
    """Whether the shortcut file *path* was read from inside *folder*."""
    return Path(path).resolve().is_relative_to(folder.resolve())


@pytest.fixture
def config_dir(tmp_path: Path) -> Path:
    path = tmp_path / "config"
    path.mkdir()
    return path


def test_the_shortcuts_folder_loads_with_nested_names(config_dir: Path) -> None:
    review = _write(config_dir / "shortcuts" / "review.md", "Review the change.\n")
    nested = _write(config_dir / "shortcuts" / "git" / "commit-msg.md", "Write a commit message.")
    chinese = _write(config_dir / "shortcuts" / "周报.md", "写周报。")

    commands = _by_name(load_prompt_shortcuts(config_dir=config_dir, workspace=None))

    assert set(commands) == {"review", "git/commit-msg", "周报"}
    for name, path in (("review", review), ("git/commit-msg", nested), ("周报", chinese)):
        command = commands[name]
        assert _same(command.path, path)
    assert commands["review"].template == "Review the change."


def test_workspace_none_turns_project_shortcuts_off(config_dir: Path, tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    _write(workspace / ".chrys" / "shortcuts" / "proj.md", "Project prompt")
    _write(config_dir / "shortcuts" / "mine.md", "My prompt")

    assert [c.name for c in load_prompt_shortcuts(config_dir=config_dir, workspace=None)] == ["mine"]
    names = [c.name for c in load_prompt_shortcuts(config_dir=config_dir, workspace=str(workspace))]
    assert sorted(names) == ["mine", "proj"]


def test_unsupported_argument_hint_is_ignored(config_dir: Path) -> None:
    """Foreign metadata leaves the prompt body intact."""
    _write(config_dir / "shortcuts" / "review.md", "---\nargument-hint: [关注点]\n---\nFocus on $1.\n")

    (command,) = load_prompt_shortcuts(config_dir=config_dir, workspace=None)

    assert command.template == "Focus on $1."


def test_frontmatter_keys_are_read_and_others_ignored(config_dir: Path) -> None:
    _write(
        config_dir / "shortcuts" / "review.md",
        "---\n"
        "description: Review uncommitted changes\n"
        "argument-hint: [focus]\n"
        "agent: QA\n"
        "model: anthropic/claude-opus-5-5\n"
        "subtask: true\n"
        "allowed-tools: Bash(git diff:*)\n"
        "---\n"
        "\n"
        "Review the changes, focusing on $1.\n"
        "!`git diff --stat`\n\n",
    )

    (command,) = load_prompt_shortcuts(config_dir=config_dir, workspace=None)

    assert command.description == "Review uncommitted changes"
    assert command.agent == "QA"
    assert command.model == "anthropic/claude-opus-5-5"
    assert command.template == "Review the changes, focusing on $1.\n!`git diff --stat`"


@pytest.mark.parametrize("newline", ["\r\n", "\r"], ids=["windows", "old-mac"])
def test_a_file_saved_with_other_line_endings_fills_the_same_prompt(config_dir: Path, newline: str) -> None:
    path = config_dir / "shortcuts" / "review.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    text = "---\ndescription: Review\nagent: QA\n---\n\nReview $1.\n!`git diff --stat`\n"
    path.write_bytes(text.replace("\n", newline).encode("utf-8"))

    (command,) = load_prompt_shortcuts(config_dir=config_dir, workspace=None)

    assert (command.description, command.agent) == ("Review", "QA")
    assert command.template == "Review $1.\n!`git diff --stat`"


def test_file_without_frontmatter_has_empty_metadata(config_dir: Path) -> None:
    _write(config_dir / "shortcuts" / "plain.md", "\n\n  Just a prompt with $ARGUMENTS  \n\n")

    (command,) = load_prompt_shortcuts(config_dir=config_dir, workspace=None)

    assert command.template == "Just a prompt with $ARGUMENTS"
    assert (command.description, command.agent, command.model) == ("", "", "")


def test_empty_bodies_broken_frontmatter_and_bad_names_are_skipped(config_dir: Path) -> None:
    root = config_dir / "shortcuts"
    _write(root / "good.md", "Good")
    _write(root / "empty.md", "")
    _write(root / "blank.md", "  \n\t\n")
    _write(root / "frontmatter-only.md", "---\ndescription: nothing else\n---\n\n")
    _write(root / "not-a-mapping.md", "---\n- just\n- a list\n---\nBody text\n")
    _write(root / "has space.md", "Body")
    _write(root / "dir with space" / "x.md", "Body")
    _write(root / ".hidden.md", "Body")
    _write(root / ".private" / "x.md", "Body")
    _write(root / "sub" / ".dot.md", "Body")
    _write(root / "notes.txt", "Body")

    names = [c.name for c in load_prompt_shortcuts(config_dir=config_dir, workspace=None)]

    assert names == ["good"]


@pytest.mark.parametrize(
    "value",
    ["2024-02-30", "!!int 'abc'", "!!float 'x'", "!!timestamp 'nope'"],
    ids=["no-such-day", "bad-int", "bad-float", "bad-timestamp"],
)
def test_a_frontmatter_value_yaml_cannot_build_skips_only_its_file(
    config_dir: Path, caplog: pytest.LogCaptureFixture, value: str
) -> None:
    """YAML that parses but cannot be built into a value breaks one file, never the whole list."""
    root = config_dir / "shortcuts"
    _write(root / "good.md", "---\ndescription: fine\n---\nGood")
    bad = _write(root / "bad.md", f"---\ndescription: {value}\n---\nBad")

    with caplog.at_level(logging.WARNING, logger=loader_module.__name__):
        first = load_prompt_shortcuts(config_dir=config_dir, workspace=None)
        second = load_prompt_shortcuts(config_dir=config_dir, workspace=None)

    assert [c.name for c in first] == ["good"]
    assert [c.name for c in second] == ["good"]
    # Skipped like any other broken file: warned once, until the file changes.
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING and os.fspath(bad) in r.getMessage()]
    assert len(warnings) == 1


@pytest.mark.skipif(sys.platform == "win32", reason="Windows file names cannot hold control characters")
def test_names_with_control_characters_are_skipped(config_dir: Path) -> None:
    root = config_dir / "shortcuts"
    _write(root / "good.md", "Good")
    _write(root / "tab\tname.md", "Body")
    _write(root / "bell\x07.md", "Body")

    assert [c.name for c in load_prompt_shortcuts(config_dir=config_dir, workspace=None)] == ["good"]


def test_opencode_folder_names_are_not_read(config_dir: Path, tmp_path: Path) -> None:
    """The folder is named as the settings tab is: ``commands`` and ``command`` are someone else's."""
    _write(config_dir / "commands" / "global-plural.md", "Global plural")
    _write(config_dir / "command" / "global-singular.md", "Global singular")
    workspace = tmp_path / "ws"
    _write(workspace / ".chrys" / "commands" / "project-plural.md", "Project plural")
    _write(workspace / ".chrys" / "command" / "project-singular.md", "Project singular")
    _write(workspace / ".chrys" / "shortcuts" / "project.md", "Project")

    commands = load_prompt_shortcuts(config_dir=config_dir, workspace=str(workspace))

    assert [c.name for c in commands] == ["project"]


def test_outside_a_repo_only_the_workspace_is_read(config_dir: Path, tmp_path: Path) -> None:
    parent = tmp_path / "outside"
    workspace = parent / "child"
    workspace.mkdir(parents=True)
    _write(parent / ".chrys" / "shortcuts" / "parent.md", "Parent")
    _write(workspace / ".chrys" / "shortcuts" / "child.md", "Child")

    assert [p.resolve() for p in project_roots(str(workspace))] == [workspace.resolve()]
    commands = load_prompt_shortcuts(config_dir=config_dir, workspace=str(workspace))
    assert [c.name for c in commands] == ["child"]
    assert _under(commands[0].path, workspace / ".chrys")


@pytest.mark.parametrize("marker", ["dir", "file"])
def test_project_roots_walk_up_to_the_nearest_git_marker(tmp_path: Path, marker: str) -> None:
    above = tmp_path / "above"
    repo = above / "repo"
    workspace = repo / "pkg" / "sub"
    workspace.mkdir(parents=True)
    if marker == "dir":
        (repo / ".git").mkdir()
    else:
        (repo / ".git").write_text("gitdir: ../elsewhere/.git\n", encoding="utf-8")

    roots = [p.resolve() for p in project_roots(str(workspace))]

    assert roots == [workspace.resolve(), (repo / "pkg").resolve(), repo.resolve()]


def test_project_roots_at_repo_root_and_nested_repo(tmp_path: Path) -> None:
    outer = tmp_path / "outer"
    inner = outer / "inner"
    workspace = inner / "x"
    workspace.mkdir(parents=True)
    (outer / ".git").mkdir()
    (inner / ".git").write_text("gitdir: ../.git/modules/inner\n", encoding="utf-8")

    assert [p.resolve() for p in project_roots(str(outer))] == [outer.resolve()]
    assert [p.resolve() for p in project_roots(str(workspace))] == [workspace.resolve(), inner.resolve()]


def test_project_shortcuts_from_every_level_load_nearest_first(config_dir: Path, tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    pkg = repo / "pkg"
    workspace = pkg / "sub"
    workspace.mkdir(parents=True)
    (repo / ".git").mkdir()
    _write(tmp_path / ".chrys" / "shortcuts" / "above.md", "Above the repo")
    _write(repo / ".chrys" / "shortcuts" / "root-only.md", "Root")
    _write(pkg / ".chrys" / "shortcuts" / "pkg-only.md", "Pkg")
    _write(config_dir / "shortcuts" / "global-only.md", "Global")

    commands = load_prompt_shortcuts(config_dir=config_dir, workspace=str(workspace))

    assert [c.name for c in commands] == ["global-only", "pkg-only", "root-only"]
    by_name = _by_name(commands)
    assert _under(by_name["global-only"].path, config_dir / "shortcuts")
    assert _under(by_name["pkg-only"].path, pkg / ".chrys")
    assert _under(by_name["root-only"].path, repo / ".chrys")


def test_same_names_dedupe_global_first_then_nearest_project(config_dir: Path, tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    workspace = repo / "pkg"
    workspace.mkdir(parents=True)
    (repo / ".git").mkdir()
    global_review = _write(config_dir / "shortcuts" / "review.md", "Global review")
    _write(workspace / ".chrys" / "shortcuts" / "review.md", "Near review")
    _write(repo / ".chrys" / "shortcuts" / "review.md", "Far review")
    near_fix = _write(workspace / ".chrys" / "shortcuts" / "fix.md", "Near fix")
    _write(repo / ".chrys" / "shortcuts" / "fix.md", "Far fix")

    commands = _by_name(load_prompt_shortcuts(config_dir=config_dir, workspace=str(workspace)))

    assert set(commands) == {"review", "fix"}
    assert _same(commands["review"].path, global_review)
    assert commands["review"].template == "Global review"
    assert commands["fix"].template == "Near fix"
    assert _same(commands["fix"].path, near_fix)


def test_same_name_comparison_is_case_insensitive(config_dir: Path, tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    _write(config_dir / "shortcuts" / "Review.md", "Global review")
    _write(workspace / ".chrys" / "shortcuts" / "review.md", "Project review")
    _write(workspace / ".chrys" / "shortcuts" / "Git" / "Commit.md", "Project commit")

    commands = load_prompt_shortcuts(config_dir=config_dir, workspace=str(workspace))

    assert [c.template for c in commands] == ["Project commit", "Global review"]
    assert [_under(c.path, workspace) for c in commands] == [True, False]


def test_shortcuts_are_sorted_by_name_whatever_their_folder(config_dir: Path, tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    _write(workspace / ".chrys" / "shortcuts" / "aaa.md", "Project")
    _write(workspace / ".chrys" / "shortcuts" / "Mmm.md", "Project upper")
    _write(config_dir / "shortcuts" / "zzz.md", "Global")
    _write(config_dir / "shortcuts" / "bbb.md", "Global b")

    commands = load_prompt_shortcuts(config_dir=config_dir, workspace=str(workspace))

    assert [(c.name, _under(c.path, workspace)) for c in commands] == [
        ("aaa", True),
        ("bbb", False),
        ("Mmm", True),
        ("zzz", False),
    ]


def test_workspace_at_home_reads_config_dir_only_as_global(tmp_path: Path) -> None:
    home = tmp_path / "home"
    config = home / ".chrys"
    _write(config / "shortcuts" / "mine.md", "Mine")

    commands = load_prompt_shortcuts(config_dir=config, workspace=str(home))

    assert [c.name for c in commands] == ["mine"]
    assert _under(commands[0].path, config / "shortcuts")


def test_paths_are_unique_and_edits_show_on_next_load(config_dir: Path, tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    target = _write(config_dir / "shortcuts" / "edit-me.md", "Before")
    _write(workspace / ".chrys" / "shortcuts" / "proj.md", "Project")

    first = load_prompt_shortcuts(config_dir=config_dir, workspace=str(workspace))
    assert len({c.path for c in first}) == len(first) == 2

    target.write_text("---\ndescription: changed\n---\nAfter", encoding="utf-8")
    _write(config_dir / "shortcuts" / "added.md", "New")
    second = _by_name(load_prompt_shortcuts(config_dir=config_dir, workspace=str(workspace)))

    assert second["edit-me"].template == "After"
    assert second["edit-me"].description == "changed"
    assert "added" in second


def test_opencode_and_claude_code_style_files_load_unchanged(config_dir: Path, tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    _write(
        workspace / ".chrys" / "shortcuts" / "test.md",
        "---\n"
        "description: Run tests with coverage\n"
        "agent: build\n"
        "model: anthropic/claude-3-5-sonnet-20241022\n"
        "subtask: true\n"
        "---\n"
        "\n"
        "Run the full test suite with coverage report and show any failures.\n"
        "Focus on the failing tests in $ARGUMENTS.\n",
    )
    _write(
        workspace / ".chrys" / "shortcuts" / "fix-issue.md",
        "---\n"
        "argument-hint: [issue-number] [priority]\n"
        "description: Fix a GitHub issue\n"
        "allowed-tools: Bash(git add:*), Bash(git status:*)\n"
        "model: claude-3-5-haiku-20241022\n"
        "---\n"
        "\n"
        "Fix issue #$1 with priority $2.\n",
    )

    commands = _by_name(load_prompt_shortcuts(config_dir=config_dir, workspace=str(workspace)))

    assert commands["test"].description == "Run tests with coverage"
    assert commands["test"].agent == "build"
    assert commands["test"].model == "anthropic/claude-3-5-sonnet-20241022"
    assert commands["test"].template.endswith("in $ARGUMENTS.")
    assert commands["fix-issue"].model == "claude-3-5-haiku-20241022"
    assert commands["fix-issue"].template == "Fix issue #$1 with priority $2."


def _sources_view(sources: list[tuple[Path, list[PromptShortcut]]]) -> list[tuple[Path, list[str]]]:
    return [(folder.resolve(), [command.name for command in found]) for folder, found in sources]


def test_sources_list_each_contributing_folder_in_reading_order(config_dir: Path, tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    workspace = repo / "pkg"
    workspace.mkdir(parents=True)
    (repo / ".git").mkdir()
    _write(config_dir / "shortcuts" / "zeta.md", "Global zeta")
    _write(config_dir / "shortcuts" / "Banana.md", "Global banana")
    _write(config_dir / "shortcuts" / "apple.md", "Global apple")
    _write(workspace / ".chrys" / "shortcuts" / "near.md", "Near")
    _write(repo / ".chrys" / "shortcuts" / "far.md", "Far")

    sources = prompt_shortcut_sources(config_dir=config_dir, workspace=str(workspace))

    assert _sources_view(sources) == [
        ((config_dir / "shortcuts").resolve(), ["apple", "Banana", "zeta"]),
        ((workspace / ".chrys" / "shortcuts").resolve(), ["near"]),
        ((repo / ".chrys" / "shortcuts").resolve(), ["far"]),
    ]


def test_sources_drop_shadowed_names_and_folders_left_empty(config_dir: Path, tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    _write(config_dir / "shortcuts" / "Review.md", "Global review")
    _write(config_dir / "shortcuts" / "fix.md", "Global fix")
    _write(workspace / ".chrys" / "shortcuts" / "REVIEW.md", "Project review")
    _write(workspace / ".chrys" / "shortcuts" / "Fix.md", "Project fix")

    sources = prompt_shortcut_sources(config_dir=config_dir, workspace=str(workspace))

    assert _sources_view(sources) == [((config_dir / "shortcuts").resolve(), ["fix", "Review"])]
    assert [command.template for _folder, found in sources for command in found] == ["Global fix", "Global review"]


def test_load_prompt_shortcuts_is_the_sorted_union_of_the_sources(config_dir: Path, tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    workspace = repo / "pkg"
    workspace.mkdir(parents=True)
    (repo / ".git").mkdir()
    _write(config_dir / "shortcuts" / "Mmm.md", "Global m")
    _write(config_dir / "shortcuts" / "dup.md", "Global dup")
    _write(config_dir / "shortcuts" / "aaa.md", "Global a")
    _write(workspace / ".chrys" / "shortcuts" / "bbb.md", "Near b")
    _write(workspace / ".chrys" / "shortcuts" / "DUP.md", "Near dup")
    _write(repo / ".chrys" / "shortcuts" / "Ccc.md", "Far c")
    kwargs = {"config_dir": config_dir, "workspace": str(workspace)}

    sources = prompt_shortcut_sources(**kwargs)
    loaded = load_prompt_shortcuts(**kwargs)

    union = [command for _folder, found in sources for command in found]
    assert [c.name for c in loaded] == ["aaa", "bbb", "Ccc", "dup", "Mmm"]
    assert [(c.name, c.path, c.template) for c in loaded] == [
        (c.name, c.path, c.template) for c in sorted(union, key=lambda c: c.name.casefold())
    ]
    assert next(c for c in loaded if c.name == "dup").template == "Global dup"


# -- what a shortcut carries ---------------------------------------------------


def test_a_shortcut_carries_no_scope_or_root() -> None:
    assert [field.name for field in dataclasses.fields(PromptShortcut)] == [
        "name",
        "template",
        "path",
        "description",
        "agent",
        "model",
    ]


# -- caps --------------------------------------------------------------------------


def test_a_folder_reads_at_most_max_command_files(
    config_dir: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(loader_module, "MAX_COMMAND_FILES", 3)
    for name in ("a", "b", "c", "d", "e"):
        _write(config_dir / "shortcuts" / f"{name}.md", name)

    with caplog.at_level(logging.WARNING, logger=loader_module.__name__):
        commands = load_prompt_shortcuts(config_dir=config_dir, workspace=None)

    assert [c.name for c in commands] == ["a", "b", "c"]
    assert any("stopped after 3 files" in record.getMessage() for record in caplog.records)


def test_a_folder_walk_stops_at_max_walked_entries(
    config_dir: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Entries that are not shortcuts count too: a folder linked to ``~`` or ``/usr`` cannot be walked whole."""
    monkeypatch.setattr(loader_module, "MAX_WALKED_ENTRIES", 6)
    folder = config_dir / "shortcuts"
    _write(folder / "a.md", "A")
    for index in range(3):
        _write(folder / "a-junk" / f"{index}.txt", "junk")
    for index in range(3):
        _write(folder / "z-junk" / f"{index}.txt", "junk")
    _write(folder / "z-junk" / "late.md", "Late")

    with caplog.at_level(logging.WARNING, logger=loader_module.__name__):
        commands = load_prompt_shortcuts(config_dir=config_dir, workspace=None)

    assert [c.name for c in commands] == ["a"]
    assert "stopped after 6 entries" in caplog.text


def test_a_folder_walk_stops_at_max_walked_dirs(
    config_dir: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(loader_module, "MAX_WALKED_DIRS", 3)
    folder = config_dir / "shortcuts"
    # Walked in name order, each folder before the next: the top, a, a/b, then c is past the cap.
    _write(folder / "top.md", "Top")
    _write(folder / "a" / "x.md", "X")
    _write(folder / "a" / "b" / "y.md", "Y")
    _write(folder / "c" / "z.md", "Z")

    with caplog.at_level(logging.WARNING, logger=loader_module.__name__):
        commands = load_prompt_shortcuts(config_dir=config_dir, workspace=None)

    assert [c.name for c in commands] == ["a/b/y", "a/x", "top"]
    assert "stopped after 3 folders" in caplog.text


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    return repo


def test_a_project_folder_linked_out_of_its_repository_is_skipped_with_one_warning(
    config_dir: Path, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    repo = _repo(tmp_path)
    outside = tmp_path / "elsewhere"
    _write(outside / "leak.md", "Leak")
    (repo / ".chrys").mkdir()
    symlink_or_skip(repo / ".chrys" / "shortcuts", outside, target_is_directory=True)

    with caplog.at_level(logging.WARNING, logger=loader_module.__name__):
        first = load_prompt_shortcuts(config_dir=config_dir, workspace=str(repo))
        second = load_prompt_shortcuts(config_dir=config_dir, workspace=str(repo))

    assert first == second == []
    assert working_folder_dirs(str(repo)) == []
    assert caplog.text.count("leads out of") == 1


def test_a_project_config_folder_linked_out_of_its_repository_is_skipped(config_dir: Path, tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    outside = tmp_path / "elsewhere"
    _write(outside / "shortcuts" / "leak.md", "Leak")
    symlink_or_skip(repo / ".chrys", outside, target_is_directory=True)

    assert load_prompt_shortcuts(config_dir=config_dir, workspace=str(repo)) == []


def test_a_project_folder_linked_inside_its_repository_loads(config_dir: Path, tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    shared = repo / "tools" / "shared-shortcuts"
    _write(shared / "review.md", "Review")
    package = repo / "packages" / "app"
    (package / ".chrys").mkdir(parents=True)
    symlink_or_skip(package / ".chrys" / "shortcuts", shared, target_is_directory=True)

    commands = load_prompt_shortcuts(config_dir=config_dir, workspace=str(package))

    assert [c.name for c in commands] == ["review"]
    assert [_same(directory, package / ".chrys" / "shortcuts") for directory in working_folder_dirs(str(package))] == [
        True
    ]


def test_a_project_shortcut_file_linked_out_of_its_repository_is_skipped_with_one_warning(
    config_dir: Path, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    repo = _repo(tmp_path)
    secret = tmp_path / "elsewhere" / "credentials"
    _write(secret, "TOKEN=abc")
    folder = repo / ".chrys" / "shortcuts"
    _write(folder / "real.md", "Real")
    symlink_or_skip(folder / "review.md", secret)

    with caplog.at_level(logging.WARNING, logger=loader_module.__name__):
        first = load_prompt_shortcuts(config_dir=config_dir, workspace=str(repo))
        second = load_prompt_shortcuts(config_dir=config_dir, workspace=str(repo))

    assert [c.name for c in first] == [c.name for c in second] == ["real"]
    assert caplog.text.count("leads out of") == 1


def test_a_project_shortcut_file_linked_inside_its_repository_loads(config_dir: Path, tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    shared = repo / "prompts" / "review.md"
    _write(shared, "Review")
    (repo / ".chrys" / "shortcuts").mkdir(parents=True)
    symlink_or_skip(repo / ".chrys" / "shortcuts" / "review.md", shared)

    assert [c.name for c in load_prompt_shortcuts(config_dir=config_dir, workspace=str(repo))] == ["review"]


def test_a_linked_file_in_the_users_own_folder_may_point_anywhere(config_dir: Path, tmp_path: Path) -> None:
    target = tmp_path / "dotfiles" / "mine.md"
    _write(target, "Mine")
    (config_dir / "shortcuts").mkdir(parents=True, exist_ok=True)
    symlink_or_skip(config_dir / "shortcuts" / "mine.md", target)

    assert [c.name for c in load_prompt_shortcuts(config_dir=config_dir, workspace=None)] == ["mine"]


def test_a_junction_below_a_folder_is_not_walked(
    config_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A Windows junction is a folder link: like a symlink, the walk does not go into it."""
    folder = config_dir / "shortcuts"
    _write(folder / "top.md", "Top")
    _write(folder / "junction" / "inner.md", "Inner")
    _write(folder / "plain" / "kept.md", "Kept")
    real_scandir = os.scandir

    class _Entry:
        def __init__(self, entry: os.DirEntry[str]) -> None:
            self._entry = entry
            self.name = entry.name

        def is_dir(self, *, follow_symlinks: bool = True) -> bool:
            return self._entry.is_dir(follow_symlinks=follow_symlinks)

        def is_junction(self) -> bool:
            return self.name == "junction"

    class _Listing:
        def __init__(self, path: Any) -> None:
            self._listing = real_scandir(path)

        def __enter__(self) -> list[_Entry]:
            return [_Entry(entry) for entry in self._listing.__enter__()]

        def __exit__(self, *exc: object) -> None:
            self._listing.__exit__(*exc)

    shadow = ModuleType("os")
    shadow.__dict__.update(vars(os), scandir=_Listing)
    monkeypatch.setattr(loader_module, "os", shadow)

    commands = load_prompt_shortcuts(config_dir=config_dir, workspace=None)

    assert [c.name for c in commands] == ["plain/kept", "top"]


def test_the_users_own_folder_may_be_a_link_anywhere(config_dir: Path, tmp_path: Path) -> None:
    dotfiles = tmp_path / "dotfiles" / "shortcuts"
    _write(dotfiles / "mine.md", "Mine")
    symlink_or_skip(config_dir / "shortcuts", dotfiles, target_is_directory=True)

    assert [c.name for c in load_prompt_shortcuts(config_dir=config_dir, workspace=None)] == ["mine"]


def test_a_file_over_max_command_file_bytes_is_skipped(config_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(loader_module, "MAX_COMMAND_FILE_BYTES", 8)
    _write(config_dir / "shortcuts" / "fits.md", "x" * 8)
    _write(config_dir / "shortcuts" / "big.md", "x" * 9)

    assert [c.name for c in load_prompt_shortcuts(config_dir=config_dir, workspace=None)] == ["fits"]


# -- the parse cache -------------------------------------------------------------


class _ParseCount:
    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.paths: list[str] = []
        original = loader_module._parse_file

        def parse(path: Path, name: str, *, size: int) -> PromptShortcut | None:
            self.paths.append(name)
            return original(path, name, size=size)

        monkeypatch.setattr(loader_module, "_parse_file", parse)


@pytest.mark.parametrize("change", ["mtime", "size"])
def test_a_file_is_parsed_again_only_when_its_mtime_or_size_changes(
    config_dir: Path, monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    target = _write(config_dir / "shortcuts" / "edit.md", "AAAA")
    parsed = _ParseCount(monkeypatch)
    first = os.stat(target)

    assert [c.template for c in load_prompt_shortcuts(config_dir=config_dir, workspace=None)] == ["AAAA"]
    assert [c.template for c in load_prompt_shortcuts(config_dir=config_dir, workspace=None)] == ["AAAA"]
    assert parsed.paths == ["edit"]

    if change == "mtime":
        # Same size, a new modification time.
        target.write_text("BBBB", encoding="utf-8")
        os.utime(target, ns=(first.st_atime_ns, first.st_mtime_ns + 5_000_000_000))
        expected = "BBBB"
    else:
        # A new size, the modification time put back as it was.
        target.write_text("BBBBBB", encoding="utf-8")
        os.utime(target, ns=(first.st_atime_ns, first.st_mtime_ns))
        expected = "BBBBBB"

    assert [c.template for c in load_prompt_shortcuts(config_dir=config_dir, workspace=None)] == [expected]
    assert parsed.paths == ["edit", "edit"]


def test_an_unchanged_file_keeps_its_parse_even_when_its_text_differs(
    config_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The cache key is the file's mtime and size: the same pair means the same parse."""
    target = _write(config_dir / "shortcuts" / "same.md", "AAAA")
    first = os.stat(target)
    load_prompt_shortcuts(config_dir=config_dir, workspace=None)
    parsed = _ParseCount(monkeypatch)

    target.write_text("BBBB", encoding="utf-8")
    os.utime(target, ns=(first.st_atime_ns, first.st_mtime_ns))

    assert [c.template for c in load_prompt_shortcuts(config_dir=config_dir, workspace=None)] == ["AAAA"]
    assert parsed.paths == []


# -- the user folder is never a project folder -------------------------------------


def test_workspace_at_home_reads_the_config_dir_once_as_the_users_own(tmp_path: Path) -> None:
    home = tmp_path / "home"
    config = home / ".chrys"
    _write(config / "shortcuts" / "mine.md", "Mine")

    sources = prompt_shortcut_sources(config_dir=config, workspace=str(home))

    assert _sources_view(sources) == [((config / "shortcuts").resolve(), ["mine"])]


# -- what a folder can hold ----------------------------------------------------------


@pytest.mark.skipif(sys.platform == "win32", reason="FIFOs and /dev/zero are POSIX")
@pytest.mark.parametrize("kind", ["fifo", "link-to-dev-zero"])
def test_a_special_file_named_like_a_shortcut_is_skipped_and_never_read(
    config_dir: Path, kind: str, caplog: pytest.LogCaptureFixture
) -> None:
    """A pipe would block the read and ``/dev/zero`` would never end it: only regular files are read."""
    folder = config_dir / "shortcuts"
    _write(folder / "good.md", "Good")
    special = folder / "special.md"
    if kind == "fifo":
        os.mkfifo(special)
    else:
        special.symlink_to("/dev/zero")

    with caplog.at_level(logging.WARNING, logger=loader_module.__name__):
        commands = load_prompt_shortcuts(config_dir=config_dir, workspace=None)

    assert [c.name for c in commands] == ["good"]
    assert "not a regular file" in caplog.text
