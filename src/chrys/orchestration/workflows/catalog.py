# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Workflow browsing, preview, confirmation and file operations shared by session frontends.

The catalog owns previews observed in this process. Discovery and history only
read files; preview is the explicit operation that executes a workflow module.
Deletion does not touch run history. The caller must check the coordinator's
active_source before deleting a workflow that might be running.
"""

from __future__ import annotations

import asyncio
import os
import stat
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from chrys.foundation.events.types import WorkflowPreviewProgress
from chrys.foundation.models.workflow_session import WorkflowIdentity
from chrys.foundation.platform.files import can_unlink_owner_verified, secure_unlink_owner_verified
from chrys.orchestration.workflows.preview import (
    REJECT_NOT_CONFIRMED,
    REJECT_SPEC_CHANGED,
    WorkflowInspection,
    WorkflowPreview,
    WorkflowPreviewError,
    WorkflowTrustDeclined,
    materialize_runtime_sdk,
    preview_workflow,
    worker_bytecode_cache_dir,
)
from chrys.service.workflows import discovery as discovery_module
from chrys.service.workflows.discovery import (
    LAYOUT_PACKAGE,
    PRECEDENCE,
    RESERVED_GLOBAL_NAME,
    SOURCE_KIND_BUILTIN,
    SOURCE_KIND_GLOBAL,
    SOURCE_KIND_WORKDIR,
    Discovery,
    WorkflowSource,
    discover_workflows,
    global_workflows_dir,
    is_global_workflows_dir,
    is_link_stat,
    package_signature,
    precedence,
    read_builtin_manifest,
    read_entry_bytes,
    workdir_workflows_dir,
    workflow_entry_path,
)
from chrys.service.workflows.ledger import ConfirmationLedger, ledger_path

if TYPE_CHECKING:
    from chrys.foundation.events.bus import EventBus
    from chrys.service.workflows.environment import PreparedEnvironment


_DELETE_SCOPE = "Only workflows directly in a global or working directory workflow directory can be deleted."

type _PathState = tuple[int, ...] | None


class WorkflowNotFoundError(KeyError):
    """No discovered workflow has the requested id."""


@dataclass(frozen=True, slots=True)
class _Observed:
    """A preview this catalog made, with the state of every path that could shadow it when it was discovered."""

    preview: WorkflowPreview
    shadowing: tuple[_PathState, ...]


class WorkflowCatalog:
    """Operations scoped to the session's config directory and current working directory."""

    def __init__(self, *, config_dir: Path, workdir: Path, bus: EventBus | None = None) -> None:
        self.config_dir = config_dir
        self.workdir = workdir
        self._bus = bus
        self._previews: dict[str, _Observed] = {}

    def discover(self) -> Discovery:
        return discover_workflows(config_dir=self.config_dir, workdir=self.workdir)

    def ledger(self) -> ConfirmationLedger:
        return ConfirmationLedger(ledger_path(self.config_dir))

    def title(self, source: WorkflowSource, *, ledger: ConfirmationLedger | None = None) -> str | None:
        """Previously observed metadata, without loading the workflow on a worker."""
        if source.source_kind == SOURCE_KIND_BUILTIN:
            manifest = read_builtin_manifest(source.workflow_id)
            title = manifest.get("title") if manifest is not None else None
            return title if isinstance(title, str) else None
        observed = self._previews.get(source.canonical_path)
        if observed is not None and observed.preview.source.source_digest == source.source_digest:
            return observed.preview.title
        entry = (ledger if ledger is not None else self.ledger()).recorded(source.canonical_path, source.source_kind)
        return entry.title if entry is not None else None

    async def preview(
        self,
        workflow_id: str,
        *,
        timeout: float | None = None,
        request_id: str = "",
        expected_identity: WorkflowIdentity | None = None,
        trust: bool = False,
        authorize: Callable[[WorkflowInspection], Awaitable[bool]] | None = None,
    ) -> WorkflowPreview:
        """Require source trust before any interpreter runs, then load on a throwaway worker.

        ``trust`` is an explicit caller authorization (e.g. CLI ``--trust``).
        Interactive callers may instead supply ``authorize``. Human decision time
        is excluded from the preview timeout. Timed-out filesystem awaits release
        the caller; their threads may finish later. Cancellation drains any worker.
        """
        async with asyncio.timeout(timeout) as deadline:
            source, shadowing = await asyncio.to_thread(self._discover_one, workflow_id)
            if source is None:
                raise WorkflowNotFoundError(f"Workflow not found: {workflow_id}")
            if expected_identity is not None and expected_identity != source.identity:
                raise WorkflowPreviewError(
                    REJECT_SPEC_CHANGED, "This session belongs to another workflow source. Start a new session."
                )
            approved = trust or source.source_kind == SOURCE_KIND_BUILTIN
            recorded = None

            async def authorize_source(environment: PreparedEnvironment | None = None) -> None:
                nonlocal approved
                if authorize is None:
                    raise WorkflowPreviewError(
                        REJECT_NOT_CONFIRMED, "Trust the workflow source and environment before previewing it."
                    )
                inspection = await asyncio.to_thread(WorkflowInspection.read, source)
                inspection = replace(inspection, prepared_environment=environment)
                loop = asyncio.get_running_loop()
                paused_at, expires = loop.time(), deadline.when()
                if expires is not None and paused_at >= expires:
                    raise TimeoutError
                deadline.reschedule(None)
                try:
                    accepted = await authorize(inspection)
                finally:
                    deadline.reschedule(None if expires is None else expires + loop.time() - paused_at)
                if not accepted:
                    raise WorkflowTrustDeclined
                if (await asyncio.to_thread(self.discover)).find(workflow_id) != source:
                    raise WorkflowPreviewError(REJECT_SPEC_CHANGED, "The workflow source changed during confirmation.")
                approved = True

            if not approved:
                ledger = await asyncio.to_thread(self.ledger)
                recorded = ledger.recorded(source.canonical_path, source.source_kind)
                if recorded is None or recorded.entry_digest != source.source_digest:
                    await authorize_source()

            async def report(
                stage: Literal["definition", "environment", "graph", "ready"],
                *,
                title: str = "",
                node_count: int = 0,
            ) -> None:
                if self._bus is not None and request_id:
                    await self._bus.publish(
                        WorkflowPreviewProgress(
                            request_id=request_id,
                            workflow_id=workflow_id,
                            stage=stage,
                            title=title,
                            node_count=node_count,
                        )
                    )

            await report("definition")
            await report("environment")
            sdk = await materialize_runtime_sdk(self.config_dir)

            async def environment_ready(environment: PreparedEnvironment) -> None:
                if (
                    not approved
                    and recorded is not None
                    and recorded.environment_fingerprint != environment.environment_fingerprint
                ):
                    await authorize_source(environment)
                await report("graph")

            preview = await preview_workflow(
                source,
                sdk=sdk,
                workspace=self.workdir,
                bytecode_cache=worker_bytecode_cache_dir(self.config_dir),
                on_environment_ready=environment_ready,
            )
            await report("ready", title=preview.title, node_count=len(preview.manifest["nodes"]))
        self._previews[source.canonical_path] = _Observed(preview, shadowing)
        return preview

    def confirm(self, preview: WorkflowPreview) -> None:
        self.ledger().confirm(preview.ledger_entry())

    def candidate_paths(self, source: WorkflowSource) -> tuple[Path, ...]:
        """The selected entry (and its folder), followed by every path whose change could shadow it."""
        entry = Path(source.canonical_path)
        own = (entry, entry.parent) if source.package is not None else (entry,)
        return (*own, *self._shadowing_paths(source))

    def is_current(self, preview: WorkflowPreview) -> bool:
        """Whether a run would still load what *preview* shows.

        Reads only the entry: a folder's other files are compared by their
        metadata and the paths that could shadow the preview by their ``lstat``
        state when it was discovered, so a candidate that was skipped then is
        not read again on every check. Admission rediscovers and compares the
        confirmed digest, so a change this misses can't run unconfirmed.
        """
        source = preview.source
        try:
            if read_entry_bytes(Path(source.canonical_path), source.source_kind) != source.source:
                return False
            package = source.package
            if package is not None and package_signature(Path(package.directory)) != package.signature:
                return False
        except OSError:
            return False
        shadowing = _path_states(self._shadowing_paths(source))
        observed = self._previews.get(source.canonical_path)
        if observed is None or observed.preview is not preview:
            return all(state is None for state in shadowing)
        return shadowing == observed.shadowing

    def check_delete(self, canonical_path: str) -> None:
        """Raise the ``ValueError`` or ``OSError`` :meth:`delete` would raise for *canonical_path*, deleting nothing."""
        self._deletion(canonical_path)

    def delete(self, canonical_path: str) -> None:
        """Delete a user workflow, then forget its confirmation; run history stays.

        A file workflow is unlinked (a symlink itself, never its target). A
        folder workflow loses only its entry file, so the folder stops being a
        workflow while everything else in it (a ``.git``, a ``.venv``, data)
        stays; *canonical_path* may name the entry or the folder.
        """
        path, folder_entry = self._deletion(canonical_path)
        if not folder_entry:
            path.unlink()
        elif not secure_unlink_owner_verified(path):
            raise ValueError(f"{path} could not be deleted safely; remove it yourself.")
        self.ledger().remove(str(path))
        self._previews.pop(str(path), None)

    def _deletion(self, canonical_path: str) -> tuple[Path, bool]:
        """The file :meth:`delete` removes and whether it is a folder's entry; raises when it would refuse."""
        path = Path(canonical_path)
        roots = {
            global_workflows_dir(self.config_dir).resolve(),
            workdir_workflows_dir(self.workdir).resolve(),
        }
        if not path.is_absolute() or path.is_relative_to(discovery_module.BUILTIN_DIR.resolve()):
            raise ValueError(_DELETE_SCOPE)
        if path.parent in roots and not path.name.startswith((".", "_")):
            # A folder argument stands for its entry, found by its exact listed name as discovery finds it.
            info = path.lstat()
            if is_link_stat(info) and path.suffix != ".py":
                raise ValueError(f"{path} is a link; remove the link yourself.")
            if stat.S_ISDIR(info.st_mode) and not is_link_stat(info) and f"{path.name}.py" in os.listdir(path):
                path = path / f"{path.name}.py"
        folder = path.parent
        if folder.parent in roots and path.name == f"{folder.name}.py" and not folder.name.startswith((".", "_")):
            if is_link_stat(folder.lstat()):
                raise ValueError(f"{folder} is a link; remove the link yourself.")
            info = path.lstat()
            if is_link_stat(info):
                raise ValueError(f"{path} is a link; remove the link yourself.")
            if not stat.S_ISREG(info.st_mode):
                raise ValueError(f"{path} is not a regular file; remove it yourself.")
            if not can_unlink_owner_verified(path):
                # The deletion's own gates; what they leave in practice is a file another user owns.
                raise ValueError(f"{path} could not be verified as yours to delete; remove it yourself.")
            return path, True
        if folder in roots and path.suffix == ".py" and path.stem and not path.name.startswith((".", "_")):
            mode = path.lstat().st_mode
            if not stat.S_ISREG(mode) and not stat.S_ISLNK(mode):
                raise ValueError("Only a regular workflow file or symlink can be deleted.")
            return path, False
        raise ValueError(_DELETE_SCOPE)

    def _discover_one(self, workflow_id: str) -> tuple[WorkflowSource | None, tuple[_PathState, ...]]:
        source = self.discover().find(workflow_id)
        return source, (() if source is None else _path_states(self._shadowing_paths(source)))

    def _shadowing_paths(self, source: WorkflowSource) -> tuple[Path, ...]:
        """Every place above *source* in ``PRECEDENCE`` where the same id could appear (a folder and its entry)."""
        roots = {
            SOURCE_KIND_GLOBAL: global_workflows_dir(self.config_dir),
            SOURCE_KIND_WORKDIR: workdir_workflows_dir(self.workdir),
        }
        paths: list[Path] = []
        for kind, layout in PRECEDENCE[: precedence(source.source_kind, source.layout)]:
            entry = workflow_entry_path(roots[kind], source.workflow_id, layout)
            if layout != LAYOUT_PACKAGE:
                paths.append(entry)
            elif not self._is_sdk_folder(entry.parent):
                paths.extend((entry.parent, entry))
        return tuple(paths)

    def _is_sdk_folder(self, folder: Path) -> bool:
        """Whether *folder* is where previews write the SDK, which discovery never takes for a workflow."""
        return folder.name.casefold() == RESERVED_GLOBAL_NAME and is_global_workflows_dir(
            folder.parent, self.config_dir
        )


def _path_states(paths: Iterable[Path]) -> tuple[_PathState, ...]:
    """What ``lstat`` says about each path, without following a final link; ``None`` for a missing path."""
    states: list[_PathState] = []
    for path in paths:
        try:
            info = os.lstat(path)
        except FileNotFoundError, NotADirectoryError:
            states.append(None)
        except OSError as exc:
            states.append((-1, exc.errno or 0))
        else:
            states.append((info.st_mode, info.st_dev, info.st_ino, info.st_mtime_ns, info.st_size))
    return tuple(states)
