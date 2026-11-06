# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Hook configuration shared by Chat rebuilds and Workflow run admission."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from chrys.foundation.events.types import Warning
from chrys.foundation.i18n import DisplayBlock, DisplayPath, msg
from chrys.foundation.platform import conventional_config_dir, get_platform

if TYPE_CHECKING:
    from pathlib import Path

    from chrys.foundation.events.bus import EventBus
    from chrys.service.hooks.confirmation import HookConfirmationService
    from chrys.service.hooks.manager import HookManager
    from chrys.service.hooks.schema import HooksFile, MergedHooksFile

_CONSTRUCTION_GLOBAL_HOOKS_INVALID = msg(
    "construction.global_hooks_invalid",
    fallback="Global hooks config could not be loaded: {detail}.  Global hooks disabled.",
    multiline=True,
)

_CONSTRUCTION_PROJECT_HOOKS_INVALID = msg(
    "construction.project_hooks_invalid",
    fallback="Project hooks config could not be loaded: {detail}. Project hooks disabled; global hooks unaffected.",
    multiline=True,
)

_CONSTRUCTION_HOOK_SKIPPED = msg(
    "construction.hook_skipped",
    fallback="Hook '{hook_id}' in {path} was skipped: {detail}. The other hooks in this file still run.",
    multiline=True,
)


@dataclass(frozen=True)
class HookFileProblem:
    """A hooks file, or one rule in it (*hook_id*), that a strict read could not take."""

    path: str
    detail: str
    hook_id: str = ""


@dataclass(frozen=True)
class StrictHooks:
    """What a strict read of the hooks files found.

    With any problem, *merged* leaves out what could not be read, so it must not
    replace rules in force: a dropped blocking rule would stop blocking unseen.
    """

    merged: MergedHooksFile
    problems: tuple[HookFileProblem, ...]


def _read_hooks_folder(hooks_dir: Path, *named: Path) -> tuple[HooksFile | None, tuple[HookFileProblem, ...]]:
    """The file a session would load from *hooks_dir*; a folder that cannot be read is a problem, not an absence.

    *named* are other spellings of the path to it: a path with its links followed has none left to check.
    """
    from chrys.service.hooks.loader import HOOKS_FILE_NAMES, HooksConfigError, load_hooks_file
    from chrys.service.hooks.validation import hooks_config_problem

    # A link to a missing target (an unmounted disk, say), anywhere on the way to the
    # file, is not the deletion it would otherwise read as.
    for path in (*(hooks_dir / name for name in HOOKS_FILE_NAMES), *named):
        for step in (path, *path.parents):
            if (step.is_symlink() or step.is_junction()) and not step.exists():
                return None, (HookFileProblem(str(step), "the link's target is missing"),)
    for name in HOOKS_FILE_NAMES:
        path = hooks_dir / name
        try:
            path.stat()
            loaded = load_hooks_file(path)
        except FileNotFoundError:
            continue
        except HooksConfigError as exc:
            return None, (HookFileProblem(str(path), hooks_config_problem(exc)),)
        except OSError as exc:
            return None, (HookFileProblem(str(path), str(exc)),)
        return loaded, tuple(HookFileProblem(loaded.source, s.reason, s.id) for s in loaded.skipped_hooks)
    return None, ()


def read_hooks_strict(*, project_root: str, project_hooks_enabled: bool) -> StrictHooks:
    """Read the global hooks and, when trusted, the project hooks, reporting instead of skipping anything."""
    from pathlib import Path

    from chrys.service.hooks.loader import merge_hooks_files, project_hooks_dir

    global_hooks, problems = _read_hooks_folder(get_platform().config_dir / "hooks", conventional_config_dir())
    project_hooks: HooksFile | None = None
    if project_hooks_enabled:
        project_hooks, project_problems = _read_hooks_folder(project_hooks_dir(Path(project_root)))
        problems = (*project_problems, *problems)
    return StrictHooks(merge_hooks_files(project=project_hooks, global_=global_hooks), problems)


def hooks_unchanged(manager: HookManager | None, merged: MergedHooksFile) -> bool:
    """Whether *manager* already runs exactly the sources, order, rules and settings of *merged*."""
    return manager.file == merged if manager is not None else not merged.sources


class SessionHookFactory:
    """Builds every hook manager of an engine: a chat session's and each workflow run's.

    Each manager it builds asks the user through *confirmations*, the
    engine's one confirmation service, so every entry asks the same way.
    """

    def __init__(self, bus: EventBus, *, confirmations: HookConfirmationService | None = None) -> None:
        self._bus = bus
        self._confirmations = confirmations

    async def __call__(
        self, *, project_root: str, project_hooks_enabled: bool, session_id: str | None, request_id: str = ""
    ) -> HookManager | None:
        """Resolve hooks for the target workspace and report warnings to its session."""
        from pathlib import Path

        from chrys.service.hooks.loader import (
            HooksConfigError,
            load_hooks_dir,
            load_hooks_project,
            merge_hooks_files,
        )

        config_dir = get_platform().config_dir
        root = Path(project_root)

        global_hooks: HooksFile | None = None
        try:
            global_hooks = load_hooks_dir(config_dir)
            if not global_hooks.source:
                global_hooks = None
        except (HooksConfigError, OSError) as exc:
            await self._bus.publish(
                Warning(
                    code="hooks_config_invalid",
                    message=f"Global hooks config could not be loaded: {exc}.  Global hooks disabled.",
                    display_message=_CONSTRUCTION_GLOBAL_HOOKS_INVALID.bind(detail=DisplayBlock(str(exc))),
                    session_id=session_id,
                    request_id=request_id,
                )
            )
            global_hooks = None

        project_hooks: HooksFile | None = None
        try:
            if project_hooks_enabled:
                project_hooks = load_hooks_project(root)
        except (HooksConfigError, OSError) as exc:
            await self._bus.publish(
                Warning(
                    code="project_hooks_config_invalid",
                    message=(
                        f"Project hooks config could not be loaded: {exc}. Project hooks disabled; global hooks unaffected."
                    ),
                    display_message=_CONSTRUCTION_PROJECT_HOOKS_INVALID.bind(detail=DisplayBlock(str(exc))),
                    session_id=session_id,
                    request_id=request_id,
                )
            )
            project_hooks = None

        merged = merge_hooks_files(project=project_hooks, global_=global_hooks)
        for loaded in (merged.project, merged.global_):
            if loaded is None:
                continue
            for skipped in loaded.skipped_hooks:
                await self._bus.publish(
                    Warning(
                        code="hook_skipped",
                        message=(
                            f"Hook '{skipped.id}' in {loaded.source} was skipped: {skipped.reason}. "
                            "The other hooks in this file still run."
                        ),
                        display_message=_CONSTRUCTION_HOOK_SKIPPED.bind(
                            hook_id=skipped.id, path=DisplayPath(loaded.source), detail=DisplayBlock(skipped.reason)
                        ),
                        session_id=session_id,
                        request_id=request_id,
                    )
                )
        return self.manager_for(merged)

    def manager_for(self, merged: MergedHooksFile) -> HookManager | None:
        """The manager running *merged*: None when no hooks file exists, so hooks stay a true no-op."""
        from chrys.service.hooks.manager import HookManager

        if not merged.sources:
            return None
        manager = HookManager(file=merged, hooks_dir=get_platform().config_dir / "hooks")
        manager.confirmations = self._confirmations
        return manager
