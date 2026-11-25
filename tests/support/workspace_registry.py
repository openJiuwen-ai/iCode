# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""An isolated registry shared by workspace service and dialog tests."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

import pytest

from chrys.foundation.util.lock import FileLock
from chrys.kernel import Message
from chrys.service.state.store import JsonFileStateStore
from chrys.service.workspaces.registry import WorkspaceRegistry
from tests.support.symlinks import symlink_or_skip


@pytest.fixture
def registry(tmp_path: Path) -> WorkspaceRegistry:
    return WorkspaceRegistry(tmp_path / "workspaces.json")


@pytest.fixture(name="registry")
def default_workspace_registry() -> WorkspaceRegistry:
    return WorkspaceRegistry()


@pytest.fixture(name="store")
def workspace_store(tmp_path: Path) -> JsonFileStateStore:
    return JsonFileStateStore(tmp_path / "sessions", workspace_registry=WorkspaceRegistry())


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
