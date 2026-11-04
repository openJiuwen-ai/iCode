# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Python extensions: finding, importing and binding them.

An extension is a :class:`chrys.extensions.Extension` named ``extension`` in a
module installed one of two ways: an installed distribution's
``chrys.extensions`` entry point, whose name is the extension id, or
``<config_dir>/extensions/<id>.py`` (or ``<id>/__init__.py``). Finding
extensions reads entry point metadata, file names and module docstrings
(parsed, never run). An extension's code is imported the first time a build
enables it, off the event loop and once per process, so a changed extension
takes effect after a restart. An id that two sources claim is a conflict:
neither loads.

Binding turns each declared tool into a ``FunctionTool`` of kind
``extension.<id>`` carrying the tool's own approval declaration. The binder
passes the kernel's validated arguments through unchanged, adds the
``ToolContext``, turns a ``ToolError`` into ``tool_error`` and bounds the result
text as MCP results are.
"""

from __future__ import annotations

import ast
import asyncio
import importlib.metadata
import importlib.util
import logging
import sys
import threading
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from chrys.extensions import Extension, ToolContext, ToolError
from chrys.foundation.branding import APP_DISPLAY_NAME
from chrys.foundation.platform import get_platform
from chrys.foundation.tool_kinds import EXTENSION_ID_PATTERN, extension_kind

if TYPE_CHECKING:
    from chrys.extensions import ToolDeclaration
    from chrys.kernel import FunctionTool
    from chrys.service.tools.packs import ToolBuild

logger = logging.getLogger(__name__)

ENTRY_POINT_GROUP = "chrys.extensions"
EXTENSIONS_DIR_NAME = "extensions"
"""``<config_dir>/extensions``: the user's own extension files."""
EXTENSION_DATA_DIR_NAME = "extension-data"
"""Each extension's private folder lives at ``<config_dir or session dir>/extension-data/<id>``."""

_SPILL_PREFIX = "extension"


@dataclass(frozen=True, slots=True)
class ExtensionSource:
    """Where one extension is installed; finding it ran none of its code."""

    id: str
    origin: Literal["distribution", "file"]
    location: str
    """The distribution's name and version, or the extension's file or folder."""
    summary: str
    """The distribution's summary, or the first line of the module's docstring."""
    entry_point: importlib.metadata.EntryPoint | None = None
    path: Path | None = None
    """The module a file extension runs: ``<id>.py`` or ``<id>/__init__.py``."""


@dataclass(frozen=True, slots=True)
class ExtensionCatalog:
    """The extensions this process can load."""

    sources: dict[str, ExtensionSource]
    conflicts: dict[str, tuple[ExtensionSource, ...]]
    """Ids more than one source claims; none of the copies loads."""
    skipped: tuple[str, ...] = ()
    """Why entries were not offered (an invalid or first-party id), as log text."""


@dataclass(frozen=True, slots=True)
class LoadedExtension:
    """One enabled extension's import: what it declares, or why it cannot load."""

    id: str
    extension: Extension | None
    problem: str = ""


def user_extensions_dir() -> Path:
    """The folder the user's own extension files live in."""
    return get_platform().config_dir / EXTENSIONS_DIR_NAME


def discover_extensions(extensions_dir: Path | None = None) -> ExtensionCatalog:
    """Find the installed extensions without importing any; it reads files, so run it off the event loop."""
    from chrys.service.tools.packs import BUILTIN_PACK_IDS

    found: dict[str, list[ExtensionSource]] = {}
    skipped: list[str] = []
    for source in (*_distribution_extensions(), *_file_extensions(extensions_dir or user_extensions_dir(), skipped)):
        if not EXTENSION_ID_PATTERN.fullmatch(source.id):
            skipped.append(
                f"{source.location}: extension id {source.id!r} must be lowercase letters, digits and '_', "
                "starting with a letter"
            )
        elif source.id in BUILTIN_PACK_IDS:
            skipped.append(
                f"{source.location}: {source.id!r} is the name of one of {APP_DISPLAY_NAME}'s own tool categories"
            )
        else:
            found.setdefault(source.id, []).append(source)
    for problem in skipped:
        logger.warning("Skipping extension %s", problem)
    return ExtensionCatalog(
        sources={extension_id: copies[0] for extension_id, copies in found.items() if len(copies) == 1},
        conflicts={extension_id: tuple(copies) for extension_id, copies in found.items() if len(copies) > 1},
        skipped=tuple(skipped),
    )


def _distribution_extensions() -> list[ExtensionSource]:
    sources: list[ExtensionSource] = []
    for entry_point in importlib.metadata.entry_points(group=ENTRY_POINT_GROUP):
        dist = entry_point.dist
        name = dist.name if dist is not None else entry_point.value
        version = dist.version if dist is not None else ""
        summary = (dist.metadata.get("Summary") or "") if dist is not None else ""
        sources.append(
            ExtensionSource(
                id=entry_point.name,
                origin="distribution",
                location=f"{name} {version}".strip(),
                summary=summary,
                entry_point=entry_point,
            )
        )
    return sources


def _file_extensions(extensions_dir: Path, skipped: list[str]) -> list[ExtensionSource]:
    try:
        entries = sorted(extensions_dir.iterdir()) if extensions_dir.is_dir() else []
    except OSError as exc:
        skipped.append(f"{extensions_dir}: the folder cannot be read ({exc})")
        return []
    sources: list[ExtensionSource] = []
    for entry in entries:
        if entry.name.startswith(("_", ".")):
            continue
        if entry.suffix == ".py" and entry.is_file():
            extension_id, module = entry.stem, entry
        elif entry.is_dir() and (entry / "__init__.py").is_file():
            extension_id, module = entry.name, entry / "__init__.py"
        else:
            continue
        sources.append(
            ExtensionSource(
                id=extension_id, origin="file", location=str(entry), summary=_docstring_summary(module), path=module
            )
        )
    return sources


def _docstring_summary(module: Path) -> str:
    try:
        docstring = ast.get_docstring(ast.parse(module.read_text(encoding="utf-8")))
    except OSError, SyntaxError, UnicodeDecodeError, ValueError:
        return ""
    return docstring.strip().splitlines()[0] if docstring and docstring.strip() else ""


_import_lock = threading.Lock()
_imported: dict[tuple[str, str, str], Extension | str] = {}
"""Each source's import, or why it failed, for the life of the process."""


async def load_extensions(extension_ids: Sequence[str]) -> list[LoadedExtension]:
    """Import the listed extensions, in order, off the event loop; one that cannot load says why."""
    return await asyncio.to_thread(_load_extensions, list(dict.fromkeys(extension_ids)))


def _load_extensions(extension_ids: list[str]) -> list[LoadedExtension]:
    catalog = discover_extensions()
    loaded: list[LoadedExtension] = []
    for extension_id in extension_ids:
        source = catalog.sources.get(extension_id)
        if source is None:
            copies = catalog.conflicts.get(extension_id)
            problem = (
                f"it is installed more than once ({'; '.join(copy.location for copy in copies)})"
                if copies
                else "it is not installed"
            )
            loaded.append(LoadedExtension(extension_id, None, problem))
            continue
        result = _import(source)
        loaded.append(
            LoadedExtension(extension_id, result)
            if isinstance(result, Extension)
            else LoadedExtension(extension_id, None, result)
        )
    return loaded


def _import(source: ExtensionSource) -> Extension | str:
    key = (source.id, source.origin, source.location)
    with _import_lock:
        if (cached := _imported.get(key)) is None:
            try:
                cached = _import_uncached(source)
            except (Exception, SystemExit) as exc:
                logger.warning("Extension %s (%s) failed to import", source.id, source.location, exc_info=True)
                cached = f"importing it failed: {type(exc).__name__}: {exc}"
            _imported[key] = cached
        return cached


def _import_uncached(source: ExtensionSource) -> Extension:
    if source.entry_point is not None:
        value = source.entry_point.load()
    elif source.path is not None:
        value = _exec_file_extension(source.id, source.path)
    else:
        raise RuntimeError("the extension names no module")
    if not isinstance(value, Extension):
        raise TypeError(f"it must provide 'extension = Extension()', not a {type(value).__name__}")
    return value


def _exec_file_extension(extension_id: str, module_path: Path) -> object:
    name = f"_chrys_extension_{extension_id}"
    package_dir = [str(module_path.parent)] if module_path.name == "__init__.py" else None
    spec = importlib.util.spec_from_file_location(name, module_path, submodule_search_locations=package_dir)
    if spec is None or spec.loader is None:
        raise ImportError(f"{module_path} cannot be loaded as a module")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        sys.modules.pop(name, None)
        raise
    return vars(module).get("extension")


def bind_extension_tools(extension_id: str, extension: Extension, build: ToolBuild) -> list[FunctionTool]:
    """The extension's tools for one agent build, in declaration order."""
    runtime = build.runtime
    folders = tuple(Path(path) for path in dict.fromkeys([runtime.cwd, *(wd.path for wd in runtime.working_dirs)]))
    context = partial(
        ToolContext,
        cwd=folders[0],
        workspace_dirs=folders,
        agent_name=build.agent_name,
        session_id=build.session_id or None,
        _data_dir=partial(_private_dir, get_platform().config_dir / EXTENSION_DATA_DIR_NAME / extension_id),
        _session_data_dir=(
            partial(_private_dir, build.session_dir / EXTENSION_DATA_DIR_NAME / extension_id)
            if build.session_dir is not None
            else _no_dir
        ),
    )
    return [_bind(extension_id, declaration, context, build.session_dir) for declaration in extension.tools]


def _private_dir(path: Path) -> Path:
    from chrys.foundation.trajectory.keys import ensure_owner_only_directory

    ensure_owner_only_directory(path)
    return path


def _no_dir() -> None:
    return None


def _bind(
    extension_id: str,
    declaration: ToolDeclaration,
    context: Callable[[], ToolContext],
    spill_dir: Path | None,
) -> FunctionTool:
    from chrys.foundation.platform.files import surrogate_safe_text
    from chrys.foundation.text.model_json import model_json
    from chrys.foundation.tool_approval import declare_requires_approval
    from chrys.foundation.tool_kinds import set_tool_kind
    from chrys.kernel import tool
    from chrys.service.tools.result_metadata import tool_error
    from chrys.service.tools.spill import bound_result_text

    func = declaration.func
    context_parameter = declaration.context_parameter
    budget = declaration.max_result_tokens

    async def call(**arguments: Any) -> str:
        if context_parameter is not None:
            arguments[context_parameter] = context()
        try:
            result = await func(**arguments)
        except ToolError as exc:
            text = tool_error(str(exc.code) or "tool_error", str(exc.message))
        else:
            # A lone surrogate (a POSIX file name that is not UTF-8) cannot be sent to the model as is.
            text = (
                surrogate_safe_text(result) if isinstance(result, str) else "" if result is None else model_json(result)
            )
        return await bound_result_text(text, budget, spill_dir, _SPILL_PREFIX) if budget else text

    # The kernel builds the tool's schema and argument model from these.
    vars(call)["__signature__"] = declaration.signature
    call.__annotations__ = {
        **{parameter.name: parameter.annotation for parameter in declaration.signature.parameters.values()},
        "return": str,
    }
    bound = tool(call, name=declaration.name, description=declaration.description)
    set_tool_kind(bound, extension_kind(extension_id))
    if declaration.requires_approval:
        declare_requires_approval(bound)
    return bound
