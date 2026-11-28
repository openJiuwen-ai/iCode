# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""An isolated registry shared by workspace service and dialog tests."""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from chrys.foundation.util.lock import FileLock
from chrys.kernel import Message
from chrys.service.state.store import JsonFileStateStore
from chrys.service.workspaces.registry import WorkspaceRegistry
from tests.support.symlinks import symlink_or_skip

if TYPE_CHECKING:
    from chrys.app.tui.i18n import LocaleController
    from chrys.app.tui.screens.workspaces.edit import WorkspaceEditDialog
    from chrys.foundation.models.workspace import Workspace
    from chrys.foundation.models.workspace_operations import WorkspaceDefinitionEdit


def workspace_edit_dialog(
    registry: WorkspaceRegistry,
    cwd: str,
    workspace: Workspace | None = None,
    *,
    locale_controller: LocaleController | None = None,
) -> WorkspaceEditDialog:
    """Connect a standalone form to the test registry."""
    from chrys.app.tui.screens.workspaces.edit import WorkspaceEditDialog

    async def update(edit: WorkspaceDefinitionEdit) -> Workspace:
        return await asyncio.to_thread(
            registry.update,
            edit.expected.id,
            name=edit.name,
            primary=edit.expected.primary_cwd,
            folders=edit.folders,
            expected=edit.expected,
        )

    async def delete(expected: Workspace) -> bool:
        await asyncio.to_thread(registry.delete, expected.id, expected=expected)
        return True

    return WorkspaceEditDialog(
        registry, cwd, workspace, locale_controller=locale_controller, update_workspace=update, delete_workspace=delete
    )


@pytest.fixture
def registry(tmp_path: Path) -> WorkspaceRegistry:
    return WorkspaceRegistry(tmp_path / "workspaces.json")


@pytest.fixture(name="registry")
def default_workspace_registry() -> WorkspaceRegistry:
    return WorkspaceRegistry()


@pytest.fixture(name="store")
def workspace_store(tmp_path: Path) -> JsonFileStateStore:
    return JsonFileStateStore(tmp_path / "sessions", workspace_registry=WorkspaceRegistry())


def write_workspace_shortcut(root: Path, name: str, body: str) -> Path:
    """Write a literal shortcut in one workspace folder and return its source path."""
    path = root / ".chrys" / "shortcuts" / f"{name}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    return path


def write_workspace_skill(root: Path, name: str, description: str = "Workspace fixture") -> Path:
    """Write a workspace skill with a stable description and return its directory."""
    folder = root / ".agents" / "skills" / name
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {description}\n---\nUse this skill.\n", encoding="utf-8"
    )
    return folder


def make_workdirs(root: Path, *names: str) -> tuple[Path, ...]:
    directories = tuple(root / name for name in names)
    for directory in directories:
        directory.mkdir()
    return directories


def make_workdir_alias(root: Path, first: str, second: str, alias: str) -> tuple[Path, Path, Path]:
    original, destination = make_workdirs(root, first, second)
    link = root / alias
    symlink_or_skip(link, original, target_is_directory=True)
    return original, destination, link


def retarget_workdir_alias(link: Path, target: Path) -> None:
    link.unlink()
    symlink_or_skip(link, target, target_is_directory=True)


def corrupt_workspace_registry(registry: WorkspaceRegistry) -> None:
    registry.path.write_text("broken", encoding="utf-8")


def move_profile_to_legacy_path(profiles, profile) -> tuple[Path, Path, bytes]:
    canonical = profiles.save(profile)
    legacy = canonical.with_name("legacy.yaml")
    canonical.rename(legacy)
    return canonical, legacy, legacy.read_bytes()


def choose_before_restore_lock(store, registry, session_id: str, workspace_id: str | None) -> None:
    with FileLock(store.active_lock_path(session_id), timeout=0):
        registry.set_session_workspace(session_id, workspace_id)


async def save_chat_session(
    store: JsonFileStateStore,
    session_id: str,
    primary: Path,
    *,
    profile: str = "",
    messages: Sequence[Message] = (),
    workdirs: Sequence[Path] | None = None,
) -> None:
    await store.save_session(
        session_id,
        {"messages": list(messages)},
        primary_cwd=str(primary),
        agent_profile=profile,
        working_dirs=None if workdirs is None else [str(path) for path in workdirs],
    )


def write_launch_settings(primary: Path, report: Path, *, event: str) -> None:
    """A selected folder supplies a distinguishable setting and an executable hook."""
    from chrys.foundation.platform import get_platform

    config = get_platform().config_dir
    config.mkdir(parents=True, exist_ok=True)
    (config / "settings.yaml").write_text(
        "workdir:\n  config_enabled: true\n  hooks_enabled: true\n",
        encoding="utf-8",
    )
    config = primary / ".chrys"
    write_cwd_hook(config, report, event=event, hook_id="capture-cwd")
    (config / "settings.yaml").write_text("context:\n  warn_threshold_pct: 0.73\n", encoding="utf-8")


def write_cwd_hook(config: Path, report: Path, *, event: str, hook_id: str) -> None:
    import json
    import sys

    hooks = config / "hooks"
    hooks.mkdir(parents=True)
    (hooks / "hooks.json").write_text(
        json.dumps(
            {
                "version": 1,
                "hooks": [
                    {
                        "id": hook_id,
                        "event": event,
                        "execution": {"mode": "blocking"},
                        "run": {
                            "type": "command",
                            "argv": [
                                sys.executable,
                                "-c",
                                f"from pathlib import Path; Path({str(report)!r}).write_text(str(Path.cwd()), encoding='utf-8')",
                            ],
                        },
                    }
                ],
            }
        ),
        encoding="utf-8",
    )


def registry_disk_full(*args, **kwargs):
    raise OSError("registry disk full")
