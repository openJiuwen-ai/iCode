# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Check one workflow file or folder the way a run would load it, and say precisely what is wrong.

The stages run in order and the first that fails ends the check; the rest are
``skipped``. ``resolve`` decides what the path names by discovery's own rules,
``read`` takes the bytes a confirmation would cover, ``metadata`` parses the
PEP 723 ``script`` block, ``environment`` and ``load`` are one diagnose
preview (the interpreter is probed and the file's top level runs in a
throwaway worker), ``graph`` reads the manifest as the scheduler does and
``bindings`` resolves agent nodes against this machine's profiles and models.
No node runs, nothing is confirmed and no session is created.
"""

from __future__ import annotations

import asyncio
import io
import os
import stat
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final, Literal

from chrys.foundation.config.settings import Settings
from chrys.orchestration.workflows.preview import (
    PREVIEW_ENVIRONMENT_ERROR,
    PREVIEW_LOAD_FAILED,
    WorkflowPreview,
    WorkflowPreviewError,
    materialize_runtime_sdk,
    preview_workflow,
    worker_bytecode_cache_dir,
)
from chrys.orchestration.workflows.settings import admission_settings
from chrys.orchestration.workflows.worker_client import CapturedOutput, LoadDiagnostic, LoadNote
from chrys.service.profiles.agents.registry import AgentProfileRegistry
from chrys.service.profiles.models.registry import ModelProfileRegistry
from chrys.service.profiles.models.schema import is_model_profile_selectable
from chrys.service.workflows.admission import (
    REJECT_AGENT_PROFILE_MISSING,
    REJECT_MODEL_UNRESOLVABLE,
    AdmissionError,
    admit_manifest,
)
from chrys.service.workflows.discovery import (
    BUILTIN_DIR,
    CHRYS_DIR_NAME,
    LAYOUT_PACKAGE,
    MAX_PACKAGE_DEPTH,
    SOURCE_KIND_BUILTIN,
    SOURCE_KIND_GLOBAL,
    SOURCE_KIND_WORKDIR,
    WORKFLOWS_DIR_NAME,
    SourceLayout,
    WorkflowCandidate,
    WorkflowSource,
    WorkflowSourceError,
    discover_workflows,
    global_workflows_dir,
    is_global_workflows_dir,
    is_link_stat,
    read_source,
    recognize_candidate,
    workdir_workflows_dir,
)
from chrys.service.workflows.environment import WorkflowEnvironmentError, metadata_block_line, parse_environment_request
from chrys.service.workflows.graph import GraphSpec, ManifestError, manifest_warnings
from chrys.service.workflows.protocol import LIMITS, ProtocolError

STAGES: Final = ("resolve", "read", "metadata", "environment", "load", "graph", "bindings")
StageStatus = Literal["pass", "fail", "skipped"]
Severity = Literal["error", "warning"]
_SOURCE_LINE_CHARS: Final = 1024
"""As in the worker: a longer line is not quoted, because a cut excerpt would misplace the caret."""


@dataclass(frozen=True, slots=True)
class Diagnostic:
    """One finding; lines and character columns count from 1, ``end_column`` is just past the range."""

    severity: Severity
    code: str
    stage: str
    message: str
    file: str | None = None
    line: int | None = None
    column: int | None = None
    end_line: int | None = None
    end_column: int | None = None
    node: str | None = None
    """The node or edge at fault, when the finding is about one."""
    source_line: str | None = None
    notes: tuple[LoadNote, ...] = ()
    hint: str | None = None
    traceback: str | None = None


@dataclass(frozen=True, slots=True)
class WorkflowSummary:
    title: str
    node_count: int
    edge_count: int
    outputs: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ValidationReport:
    """What the check found; ``path`` is the file or folder validated, the other target fields what it holds."""

    path: str
    stages: tuple[tuple[str, StageStatus], ...]
    diagnostics: tuple[Diagnostic, ...]
    layout: SourceLayout | None = None
    workflow_id: str | None = None
    entry: str | None = None
    package_dir: str | None = None
    source_digest: str | None = None
    files: int | None = None
    diagnostics_truncated: bool = False
    """The worker left some load diagnostics or notes out."""
    sites_truncated: bool = False
    """The worker left out where nodes were declared, so findings about a node name only the entry file."""
    workflow: WorkflowSummary | None = None
    output: CapturedOutput = field(default_factory=lambda: CapturedOutput("", False))
    """What the workflow's top level printed while it loaded."""

    @property
    def passed(self) -> bool:
        return not any(diagnostic.severity == "error" for diagnostic in self.diagnostics)


class _StageFailed(Exception):
    def __init__(self, *diagnostics: Diagnostic) -> None:
        super().__init__(diagnostics[0].message)
        self.diagnostics = diagnostics


class _Check:
    """The report being built; ``stage`` is the stage running now."""

    def __init__(self, path: str) -> None:
        self.path = path
        self.stage = STAGES[0]
        self.statuses: dict[str, StageStatus] = {}
        self.diagnostics: list[Diagnostic] = []
        self.candidate: WorkflowCandidate | None = None
        self.source: WorkflowSource | None = None
        self.preview: WorkflowPreview | None = None
        self.workflow: WorkflowSummary | None = None
        self.diagnostics_truncated = False
        self.output = CapturedOutput("", False)

    def passed(self, stage: str) -> None:
        self.statuses[stage] = "pass"
        index = STAGES.index(stage) + 1
        self.stage = STAGES[index] if index < len(STAGES) else stage

    def failed(self, diagnostics: tuple[Diagnostic, ...]) -> None:
        self.statuses[self.stage] = "fail"
        self.diagnostics.extend(diagnostics)

    def report(self) -> ValidationReport:
        source, candidate, preview = self.source, self.candidate, self.preview
        package = source.package if source is not None else None
        packaged = candidate is not None and candidate.layout == LAYOUT_PACKAGE
        return ValidationReport(
            path=str(candidate.location) if candidate is not None else self.path,
            stages=tuple((stage, self.statuses.get(stage, "skipped")) for stage in STAGES),
            diagnostics=tuple(self.diagnostics),
            layout=candidate.layout if candidate is not None else None,
            workflow_id=candidate.workflow_id if candidate is not None else None,
            entry=str(candidate.path) if candidate is not None else None,
            package_dir=str(candidate.path.parent) if candidate is not None and packaged else None,
            source_digest=source.source_digest if source is not None else None,
            files=(package.file_count if package is not None else 1) if source is not None else None,
            diagnostics_truncated=self.diagnostics_truncated,
            sites_truncated=preview.load.sites_truncated if preview is not None else False,
            workflow=self.workflow,
            output=self.output,
        )


async def validate_workflow_path(
    path: str, *, config_dir: Path, workspace: Path, settings: Settings
) -> ValidationReport:
    """Check the workflow file or folder at *path* (relative to *workspace*, which is also the run's workspace)."""
    check = _Check(os.path.join(workspace, path))
    try:
        check.candidate = await asyncio.to_thread(_resolve, check, path, workspace, config_dir)
        check.passed("resolve")
        check.source = source = await asyncio.to_thread(_read, check.candidate)
        check.passed("read")
        _check_metadata(source)
        check.passed("metadata")
        check.preview = preview = await _load(check, source, config_dir=config_dir, workspace=workspace)
        check.passed("load")
        check.workflow = _check_graph(check, preview)
        check.passed("graph")
        await _check_bindings(preview, settings)
        check.passed("bindings")
    except _StageFailed as failure:
        check.failed(failure.diagnostics)
    except Exception as exc:
        check.failed((_error("internal_error", check.stage, f"{type(exc).__name__}: {exc}"),))
    return check.report()


def _error(
    code: str,
    stage: str,
    message: str,
    *,
    file: str | None = None,
    line: int | None = None,
    column: int | None = None,
    node: str | None = None,
    source_line: str | None = None,
    hint: str | None = None,
    traceback: str | None = None,
) -> Diagnostic:
    return Diagnostic(
        "error",
        code,
        stage,
        message,
        file=file,
        line=line,
        column=column,
        node=node,
        source_line=source_line,
        hint=hint,
        traceback=traceback,
    )


# --------------------------------------------------------------------------- resolve


def _resolve(check: _Check, argument: str, workspace: Path, config_dir: Path) -> WorkflowCandidate:
    """The workflow *argument* names, judged by the rules discovery applies to the same directory entry.

    Only the directories above the workflow are resolved, so a workflow that
    is itself a link is reported as one rather than validated through it.
    ``x/x.py`` names the folder ``x`` unless ``x`` is a workflow directory.
    """
    # Path drops "." parts and trailing separators but keeps "..", which is never folded lexically past a link.
    path = Path(workspace, argument)
    if path.name == "..":
        path = Path(os.path.realpath(path))
    if not path.name:
        raise _StageFailed(
            _error("path_not_workflow", "resolve", "a workflow is a .py file or a folder", file=str(path))
        )
    target = Path(os.path.realpath(path.parent), path.name)
    info = _lstat(target)
    if not _is_folder(info) and path.name == f"{path.parent.name}.py":
        folder = Path(os.path.realpath(path.parent.parent), path.parent.name)
        folder_info = _lstat(folder)
        is_link = folder_info is not None and is_link_stat(folder_info)
        if is_link or (_is_folder(folder_info) and not _is_workflow_dir(folder, config_dir)):
            target, info = folder, folder_info
    if info is None:
        raise _StageFailed(_not_found(argument, target, workspace, config_dir))
    if is_link_stat(info):
        message = "workflows can't be links; pass the folder or file it points to"
        raise _StageFailed(_error("path_is_link", "resolve", message, file=str(target)))
    if not (stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode)):
        raise _StageFailed(
            _error("path_not_workflow", "resolve", "a workflow is a .py file or a folder", file=str(target))
        )
    inner = _workflow_dir_of(target, config_dir) if stat.S_ISDIR(info.st_mode) else None
    if inner == target:
        message = "this folder holds workflows; pass one of the workflow files or folders in it"
        raise _StageFailed(_error("path_not_workflow", "resolve", message, file=str(target)))
    folder = _enclosing_workflow_folder(target, config_dir) if stat.S_ISREG(info.st_mode) else None
    if folder is not None:
        message = f"{target.name} is a file of the workflow folder {folder.name}, not a workflow of its own"
        hint = f"validate the folder: {folder}"
        raise _StageFailed(_error("path_not_workflow", "resolve", message, file=str(target), hint=hint))
    root = target.parent
    entry = _listed(root, target.name)
    if entry is None:
        message = f"{target.name} is not spelled as its folder lists it; pass the name exactly as listed"
        raise _StageFailed(_error("path_not_workflow", "resolve", message, file=str(target)))
    kind = _kind_of(root, config_dir)
    candidate = recognize_candidate(root, entry, kind, reserves_sdk=kind == SOURCE_KIND_GLOBAL)
    if candidate is None:
        if inner is not None:  # a directory, or its .chrys folder; a workflow folder may hold one of its own
            hint = f"pass one of the workflow files or folders in {inner}"
            raise _StageFailed(
                _error("path_not_workflow", "resolve", "this folder is not a workflow", file=str(target), hint=hint)
            )
        listed = _is_workflow_dir(root, config_dir)
        raise _StageFailed(
            _unrecognized(root / entry.name, folder=stat.S_ISDIR(info.st_mode), kind=kind, listed=listed)
        )
    problem = candidate.problem
    if problem is not None:
        raise _StageFailed(_error(_problem_code(problem), "resolve", str(problem), file=problem.path))
    check.diagnostics.extend(_shadowing(candidate, root, workspace, config_dir))
    return candidate


def _problem_code(problem: WorkflowSourceError) -> str:
    if problem.reason == "reserved":
        return "name_reserved"
    if problem.reason == "not_regular":  # the folder's entry is a link, a folder or a special file
        info = _lstat(Path(problem.path))
        return "path_is_link" if info is not None and is_link_stat(info) else "path_not_workflow"
    return "source_unreadable"


def _lstat(path: Path) -> os.stat_result | None:
    try:
        return os.lstat(path)
    except OSError:
        return None


def _is_folder(info: os.stat_result | None) -> bool:
    return info is not None and stat.S_ISDIR(info.st_mode) and not is_link_stat(info)


def _same(left: Path, right: Path) -> bool:
    try:
        return left == right or os.path.samefile(left, right)
    except OSError:
        return False


def _is_workflow_dir(folder: Path, config_dir: Path) -> bool:
    """A directory discovery lists workflows in: a ``.chrys/workflows``, the global one or the builtin one."""
    if folder.name == WORKFLOWS_DIR_NAME and folder.parent.name == CHRYS_DIR_NAME:
        return True
    return is_global_workflows_dir(folder, config_dir) or _same(folder, BUILTIN_DIR)


def _workflow_dir_of(folder: Path, config_dir: Path) -> Path | None:
    """*folder* when discovery lists workflows in it, else the workflow directory it holds as a working directory or ``.chrys`` folder."""
    for inner in (folder, folder / WORKFLOWS_DIR_NAME, folder / CHRYS_DIR_NAME / WORKFLOWS_DIR_NAME):
        if _is_workflow_dir(inner, config_dir) and _is_folder(_lstat(inner)):
            return inner
    return None


def _enclosing_workflow_folder(path: Path, config_dir: Path) -> Path | None:
    """The workflow folder *path* is a file of: its nearest folder ``F`` holding ``F/F.py``, below any workflow directory."""
    for folder in list(path.parents)[:MAX_PACKAGE_DEPTH]:
        if _is_workflow_dir(folder, config_dir):
            return None
        info = _lstat(folder / f"{folder.name}.py")
        if info is not None and stat.S_ISREG(info.st_mode):
            return folder
    return None


def _kind_of(root: Path, config_dir: Path) -> str:
    if _same(root, BUILTIN_DIR):
        return SOURCE_KIND_BUILTIN
    if is_global_workflows_dir(root, config_dir):
        return SOURCE_KIND_GLOBAL
    return SOURCE_KIND_WORKDIR


def _listed(root: Path, name: str) -> os.DirEntry[str] | None:
    """The entry of *root* spelled *name*, else the one entry that matches it ignoring case."""
    try:
        with os.scandir(root) as listing:
            entries = list(listing)
    except OSError:
        return None
    exact = [entry for entry in entries if entry.name == name]
    if exact:
        return exact[0]
    folded = [entry for entry in entries if entry.name.casefold() == name.casefold()]
    return folded[0] if len(folded) == 1 else None


def _not_found(argument: str, target: Path, workspace: Path, config_dir: Path) -> Diagnostic:
    hint = None
    named = discover_workflows(config_dir=config_dir, workdir=workspace).find(argument)
    if named is not None:
        location = Path(named.canonical_path).parent if named.package is not None else named.canonical_path
        hint = f"to check workflow '{argument}', pass its path: {location}"
    return _error("path_not_found", "resolve", "no such file or folder", file=str(target), hint=hint)


def _unrecognized(path: Path, *, folder: bool, kind: str, listed: bool) -> Diagnostic:
    """Why *path* is no workflow; *listed* says whether it sits in a directory discovery lists workflows in."""
    name = path.name
    if name.startswith((".", "_")):
        message = "names that start with '.' or '_' are never loaded as workflows"
        return _error("name_ignored", "resolve", message, file=str(path))
    if not folder:
        message = f"{name} is not a workflow file: its name must end in .py"
        return _error("path_not_workflow", "resolve", message, file=str(path))
    if kind == SOURCE_KIND_BUILTIN:
        message = "the builtin workflow directory holds single files only"
        return _error("name_ignored", "resolve", message, file=str(path))
    wanted = f"{name}.py"
    hint = None
    try:
        python = sorted(entry.name for entry in path.iterdir() if entry.name.casefold().endswith(".py"))
    except OSError:
        python = []
    alike = [found for found in python if found.casefold() == wanted.casefold()]
    if alike:
        hint = f"found {alike[0]}; the entry must be named exactly {wanted}"
    elif len(python) == 1 and listed:  # elsewhere the one Python file may be a project's own module
        hint = f"rename {python[0]} to {wanted}"
    message = f"a workflow folder needs an entry file named {wanted}"
    return _error("entry_missing", "resolve", message, file=str(path), hint=hint)


def _shadowing(candidate: WorkflowCandidate, root: Path, workspace: Path, config_dir: Path) -> list[Diagnostic]:
    """A warning when *candidate* sits where discovery looks but another workflow of its id would run instead."""
    roots = (BUILTIN_DIR, global_workflows_dir(config_dir), workdir_workflows_dir(workspace))
    if not any(_same(root, other) for other in roots):
        return []
    winner = discover_workflows(config_dir=config_dir, workdir=workspace).find(candidate.workflow_id)
    if winner is None or _same(Path(winner.canonical_path), candidate.path):
        return []
    location = Path(winner.canonical_path).parent if winner.package is not None else Path(winner.canonical_path)
    message = f"another workflow named '{candidate.workflow_id}' takes precedence; a run by name loads {location}"
    return [Diagnostic("warning", "shadowed", "resolve", message, file=str(candidate.location))]


# --------------------------------------------------------------------------- read


def _read(candidate: WorkflowCandidate) -> WorkflowSource:
    try:
        source = read_source(candidate.path, candidate.source_kind, layout=candidate.layout)
    except WorkflowSourceError as exc:
        raise _StageFailed(_error(_read_code(exc, candidate), "read", str(exc), file=exc.path)) from exc
    try:
        source.source.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise _StageFailed(_not_utf8(exc, source)) from exc
    return source


def _read_code(exc: WorkflowSourceError, candidate: WorkflowCandidate) -> str:
    if exc.reason == "package_too_large":
        return "package_too_large"
    if Path(exc.path) == candidate.path:
        return "source_too_large" if exc.reason == "too_large" else "source_unreadable"
    return {"link": "package_link", "unsupported_file": "package_unsupported_file"}.get(
        exc.reason, "package_unreadable"
    )


def _not_utf8(exc: UnicodeDecodeError, source: WorkflowSource) -> Diagnostic:
    payload = source.source
    before = payload[: exc.start]
    start = before.rfind(b"\n") + 1
    end = payload.find(b"\n", exc.start)
    line = before.count(b"\n") + 1
    prefix = before[start:].decode("utf-8")
    text = payload[start : None if end < 0 else end].decode("utf-8", errors="replace").rstrip("\r")
    if line == 1:
        prefix, text = prefix.removeprefix("\ufeff"), text.removeprefix("\ufeff")
    return _error(
        "source_not_utf8",
        "read",
        "the workflow file is not UTF-8",
        file=source.canonical_path,
        line=line,
        column=len(prefix) + 1,
        source_line=_quotable(text),
    )


# --------------------------------------------------------------------------- metadata, environment, load


def _check_metadata(source: WorkflowSource) -> None:
    try:
        parse_environment_request(source.source)
    except WorkflowEnvironmentError as exc:
        raise _StageFailed(
            _error(
                "metadata_invalid",
                "metadata",
                str(exc),
                file=source.canonical_path,
                line=exc.line,
                column=exc.column,
                source_line=_entry_line(source, exc.line),
            )
        ) from exc


async def _load(check: _Check, source: WorkflowSource, *, config_dir: Path, workspace: Path) -> WorkflowPreview:
    sdk = await materialize_runtime_sdk(config_dir)

    async def environment_ready(_environment: object) -> None:
        check.passed("environment")

    try:
        preview = await preview_workflow(
            source,
            sdk=sdk,
            workspace=workspace,
            bytecode_cache=worker_bytecode_cache_dir(config_dir),
            on_environment_ready=environment_ready,
            diagnose=True,
        )
    except WorkflowPreviewError as exc:
        check.output = exc.stdout
        check.diagnostics_truncated = exc.diagnostics_truncated
        raise _StageFailed(*_preview_failure(exc, source)) from exc
    except ProtocolError as exc:
        message = f"the workflow worker sent a malformed reply: {exc}"
        raise _StageFailed(_error("worker_failed", "load", message)) from exc
    check.output = preview.load.stdout
    return preview


def _preview_failure(exc: WorkflowPreviewError, source: WorkflowSource) -> tuple[Diagnostic, ...]:
    entry = source.canonical_path
    if exc.code == PREVIEW_ENVIRONMENT_ERROR:
        line = metadata_block_line(source.source)
        return (
            _error(
                "environment_invalid",
                "environment",
                exc.message,
                file=entry,
                line=line,
                source_line=_entry_line(source, line),
            ),
        )
    if exc.code != PREVIEW_LOAD_FAILED:
        return (_error("worker_failed", "load", exc.message),)
    traceback = exc.traceback or None
    if exc.timed_out:
        return (
            _error(
                "load_timeout",
                "load",
                f"the workflow's top level did not finish within {LIMITS.load_timeout:g} s",
                file=entry,
                hint="code at the top level runs on every load; move long work into a node",
                traceback=traceback,
            ),
        )
    if not exc.diagnostics:
        return (_error("load_error", "load", exc.message, file=entry, traceback=traceback),)
    return tuple(
        _load_diagnostic(diagnostic, traceback if index == 0 else None)
        for index, diagnostic in enumerate(exc.diagnostics)
    )


def _load_diagnostic(found: LoadDiagnostic, traceback: str | None) -> Diagnostic:
    return Diagnostic(
        "error",
        found.code,
        "load",
        found.message,
        file=found.file,
        line=found.line,
        column=found.column,
        end_line=found.end_line,
        end_column=found.end_column,
        node=found.node,
        source_line=found.source_line,
        notes=found.notes,
        hint=found.hint,
        traceback=traceback,
    )


# --------------------------------------------------------------------------- graph, bindings


def _check_graph(check: _Check, preview: WorkflowPreview) -> WorkflowSummary:
    manifest = preview.manifest
    try:
        graph = GraphSpec.from_manifest(manifest)
        warnings = manifest_warnings(manifest)
    except ManifestError as exc:
        raise _StageFailed(_error("manifest_invalid", "graph", str(exc), file=preview.source.canonical_path)) from exc
    for warning in warnings:
        file, line, text = _site(preview, warning.node_id)
        check.diagnostics.append(
            Diagnostic(
                "warning",
                warning.code,
                "graph",
                warning.message,
                file=file,
                line=line,
                node=warning.node_id,
                source_line=text,
            )
        )
    return WorkflowSummary(graph.title, len(graph.nodes), len(graph.edges), graph.outputs)


async def _check_bindings(preview: WorkflowPreview, settings: Settings) -> None:
    agents, models = await asyncio.to_thread(_registries)
    admitted_settings, _model = admission_settings(settings, None, models)
    try:
        admit_manifest(preview.manifest, agent_registry=agents, model_registry=models, settings=admitted_settings)
    except AdmissionError as exc:
        hint = None
        if exc.code == REJECT_AGENT_PROFILE_MISSING:
            hint = "agent profiles here: " + ", ".join(sorted(agents.list_names()))
        elif exc.code == REJECT_MODEL_UNRESOLVABLE:
            hint = _model_hint(models)
        file, line, text = _site(preview, exc.node_id)
        raise _StageFailed(
            _error(
                exc.code, "bindings", exc.message, file=file, line=line, node=exc.node_id, source_line=text, hint=hint
            )
        ) from exc


def _model_hint(models: ModelProfileRegistry) -> str:
    usable = [profile for profile in models.list_profiles() if is_model_profile_selectable(profile)]
    if not usable:
        return "set up a model profile on this machine"
    labels = sorted(f"{profile.name} ({profile.id})" for profile in usable)
    return "model= takes one of the model profiles here: " + ", ".join(labels)


def _registries() -> tuple[AgentProfileRegistry, ModelProfileRegistry]:
    agents = AgentProfileRegistry()
    agents.load_all()
    models = ModelProfileRegistry()
    models.load_all()
    return agents, models


def _site(preview: WorkflowPreview, node_id: str | None) -> tuple[str, int | None, str | None]:
    """Where *node_id* was declared, and that line; the entry file, without a line, when the worker did not say."""
    file, line = preview.load.sites.get(node_id, (None, None)) if node_id is not None else (None, None)
    if file is None:
        return preview.source.canonical_path, None, None
    text = _entry_line(preview.source, line) if file == preview.source.canonical_path else _file_line(file, line)
    return file, line, text


# --------------------------------------------------------------------------- source lines


def _entry_line(source: WorkflowSource, line: int | None) -> str | None:
    """Line *line* of the entry as it was read (and run), not as the file may hold it by now."""
    if line is None:
        return None
    # Python's own line breaks: str.splitlines() also breaks at U+2028 and form feeds.
    text = source.source.decode("utf-8", errors="replace").removeprefix("\ufeff")
    lines = io.StringIO(text, newline=None).readlines()
    return _quotable(lines[line - 1].rstrip("\n")) if 0 < line <= len(lines) else None


def _file_line(path: str, line: int | None) -> str | None:
    if line is None:
        return None
    try:
        with open(path, encoding="utf-8", errors="replace") as handle:
            for number, text in enumerate(handle, start=1):
                if number == line:
                    return _quotable(text.rstrip("\n"))
    except OSError:
        return None
    return None


def _quotable(text: str) -> str | None:
    return text if text and len(text) <= _SOURCE_LINE_CHARS else None
