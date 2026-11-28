# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""The same syntax is accepted in the terminal and editor."""

from __future__ import annotations

import pytest

from chrys.app import workspace_commands
from chrys.app.workspace_commands import (
    WorkspaceCommand,
    WorkspaceCommandReply,
    manage_workspace,
    parse_workspace_command,
)
from chrys.foundation.events.types import WorkspaceChangeFinished
from chrys.foundation.i18n.formatting import format_message
from chrys.service.workspaces.registry import WorkspaceRegistry
from tests.support.event_capture import capture_events
from tests.support.workspace_engine import workspace_case as workspace_case


@pytest.mark.parametrize(
    ("text", "action", "argument"),
    [
        ("  /workspace switch \r\n", "switch", ""),
        ("/workspace switch switch", "switch", "switch"),
        ("/workspace switch new", "switch", "new"),
        ("/workspace  switch  My Workspace  ", "switch", "My Workspace"),
        ("/workspace new My  Workspace", "new", "My  Workspace"),
        ("/workspace\trename\tNew Name ", "rename", "New Name"),
        ("/workspace add-folder ../Reference Files", "add-folder", "../Reference Files"),
        ("/workspace remove-folder ~/Ref Files", "remove-folder", "~/Ref Files"),
        ("/workspace remove-folder ~/Ref Files \r\n ", "remove-folder", "~/Ref Files "),
        ("/workspace add-folder  Reference Files ", "add-folder", " Reference Files "),
        ('/workspace delete "My Workspace"', "delete", '"My Workspace"'),
        ("\uff0fworkspace switch Team", "switch", "Team"),
    ],
)
def test_workspace_command_syntax(text, action, argument):
    assert parse_workspace_command(text) == WorkspaceCommand(action, argument)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("/workspace new", "Usage: /workspace new <name>"),
        ("/workspace rename ", "Usage: /workspace rename <new name>"),
        ("/workspace add-folder", "Usage: /workspace add-folder <path>"),
        ("/workspace remove-folder", "Usage: /workspace remove-folder <path>"),
        ("/workspace delete ", "Usage: /workspace delete <name>"),
        ("/workspace leave", "Unknown workspace action “leave”. Run /workspace for usage."),
        ("/workspace status", "Unknown workspace action “status”. Run /workspace for usage."),
        ("/workspace-manage new Team", None),
    ],
)
def test_bad_workspace_command_explains_the_expected_syntax(text, expected):
    result = parse_workspace_command(text)
    if expected is None:
        assert result is None
        return
    assert isinstance(result, WorkspaceCommandReply) and result.error
    assert [format_message(message) for message in result.messages] == [expected]


@pytest.mark.parametrize(
    ("candidate", "message"),
    [
        ("other/reference", "{path} is already a reference folder."),
        ("work", "{path} is already the primary folder."),
        ("work/child", "{path} overlaps with the primary folder {primary}."),
        ("other", "{path} overlaps with the reference folder {reference}."),
    ],
)
def test_add_reference_folder_explains_folder_conflicts(tmp_path, workspace_case, candidate, message):
    primary = tmp_path / "work"
    reference = tmp_path / "other" / "reference"
    (primary / "child").mkdir(parents=True)
    reference.mkdir(parents=True)
    registry = workspace_case.registry
    workspace = registry.create("Team", str(primary), [str(reference)])
    path = tmp_path / candidate

    reply = manage_workspace(
        registry,
        WorkspaceCommand("add-folder", str(path)),
        current=workspace,
        cwd=str(primary),
        session_id="chat",
    )

    assert reply.error and reply.change is None
    assert [format_message(ref) for ref in reply.messages] == [
        message.format(path=path.resolve(), primary=primary, reference=reference)
    ]
    assert registry.snapshot().workspace(workspace.id) == workspace


async def _dispatch(engine, registry, current, action, argument):
    reply = manage_workspace(
        registry,
        WorkspaceCommand(action, argument),
        current=current,
        cwd=current.primary_cwd,
        session_id=engine.session_id,
    )
    if reply.change is None:
        return reply
    finished = await capture_events(engine.event_bus, WorkspaceChangeFinished)
    await engine.event_bus.publish(reply.change, raise_handler_errors=True)
    (result,) = finished
    assert result.request_id == reply.change.event_id
    if result.error is not None:
        return WorkspaceCommandReply((result.error.display_message,), error=True)
    assert result.workspace_update is not None
    return reply


async def test_workspace_folder_commands_expand_home_and_store_normalized_absolute_paths(
    tmp_path, monkeypatch, workspace_case
):
    cwd = tmp_path / "work"
    home = tmp_path / "home"
    reference = home / "Reference Files"
    cwd.mkdir()
    reference.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    registry = workspace_case.registry
    current = registry.create("Team", str(cwd))
    engine = await workspace_case(cwd, workspace_id=current.id)

    reply = await _dispatch(engine, registry, current, "add-folder", "../home/Reference Files")
    assert format_message(reply.messages[0]) == f"Added reference folder {reference}."
    assert registry.snapshot().workspace(current.id).working_dirs[0].path == str(reference)

    reply = await _dispatch(engine, registry, engine.workspace, "remove-folder", "~/Reference Files")
    assert format_message(reply.messages[0]) == f"Removed reference folder {reference}."
    assert registry.snapshot().workspace(current.id).working_dirs == []


@pytest.mark.parametrize("locale", ["en", "zh-Hans"])
def test_deleting_unspecified_explains_the_reserved_item(workspace_case, tmp_path, locale):
    from chrys.foundation.i18n import Localizer

    reply = manage_workspace(
        workspace_case.registry,
        WorkspaceCommand("delete", "Unspecified"),
        current=None,
        cwd=str(tmp_path),
        session_id="chat",
    )
    assert reply.error is True and reply.change is None
    assert (
        Localizer(locale).render(reply.messages[0])
        == {
            "en": "Unspecified is a reserved item and cannot be deleted.",
            "zh-Hans": "未指定是保留项，不能删除。",  # noqa: RUF001
        }[locale]
    )


def test_delete_refuses_a_name_that_now_belongs_to_another_workspace(workspace_case, tmp_path):
    """The list the user chose from names one workspace; a same-named replacement is not deleted."""
    registry = workspace_case.registry
    old_dir, new_dir = tmp_path / "old", tmp_path / "new"
    old_dir.mkdir()
    new_dir.mkdir()
    listed = registry.create("Target", str(old_dir))
    shown = registry.snapshot().workspaces
    registry.delete(listed.id)
    replacement = registry.create("Target", str(new_dir))
    registry.set_session_workspace("other-chat", replacement.id)

    def delete(name: str, shown_now):
        return manage_workspace(
            registry,
            WorkspaceCommand("delete", name),
            current=None,
            cwd=str(tmp_path),
            session_id="chat",
            shown=shown_now,
        )

    reply = delete("target", shown)
    assert reply.error is True and reply.change is None
    assert format_message(reply.messages[0]) == (
        "Workspace “target” was replaced elsewhere, so nothing was deleted. Check the workspace list and try again."
    )
    assert registry.snapshot().workspace(replacement.id) is not None
    assert registry.snapshot().maps_session("other-chat")

    # Listed as it is now, or with no list (typed in ACP), the name deletes the workspace it names.
    for shown_now in (registry.snapshot().workspaces, None):
        reply = delete("Target", shown_now)
        assert reply.error is False and reply.change is not None
        assert reply.change.delete_workspace == replacement


def test_remove_folder_takes_the_listed_entry_when_two_point_to_one_directory(tmp_path, monkeypatch):
    primary, shared, old_target = (tmp_path / name for name in ("primary", "shared", "old-target"))
    for path in (primary, shared, old_target):
        path.mkdir()
    alias, other_alias = tmp_path / "shortcut", tmp_path / "another"
    try:
        alias.symlink_to(old_target, target_is_directory=True)
        other_alias.symlink_to(shared, target_is_directory=True)
    except OSError:
        pytest.skip("symlinks are unavailable")
    registry = WorkspaceRegistry(tmp_path / "workspaces.json")
    team = registry.create("Team", str(primary), (str(shared), str(alias)))
    alias.unlink()
    alias.symlink_to(shared, target_is_directory=True)

    def remove(path: str) -> WorkspaceCommandReply:
        return manage_workspace(
            registry, WorkspaceCommand("remove-folder", path), current=team, cwd=str(primary), session_id=None
        )

    for path, kept in ((str(alias), str(shared)), (str(shared), str(alias))):
        reply = remove(path)
        assert reply.change is not None and reply.change.edit_workspace is not None
        assert reply.change.edit_workspace.folders == (kept,)
        assert format_message(reply.messages[0]) == f"Removed reference folder {path}."

    ambiguous = remove(str(other_alias))
    assert ambiguous.error and ambiguous.change is None
    assert format_message(ambiguous.messages[0]) == (
        f"Several reference folders point to {shared}. Enter the folder as the workspace lists it."
    )

    # A listed spelling is removed without reading any folder (one may sit on a mount that stopped answering).
    def unreadable(*_args: object) -> None:
        raise AssertionError("a listed spelling must not read folders")

    monkeypatch.setattr(workspace_commands, "path_identity", unreadable)
    monkeypatch.setattr(workspace_commands, "workspace_folder_path", unreadable)
    assert remove(str(alias)).change.edit_workspace.folders == (str(shared),)
