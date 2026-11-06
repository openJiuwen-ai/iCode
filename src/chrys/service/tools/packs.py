# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tool packs: the unit every local tool is registered and assembled in.

A pack is a record the catalog owns: an id (the value ``tools.builtins`` lists),
the label and description the agent editor shows, and ``create``, which builds
the pack's tools for one agent build from a :class:`ToolBuild`. iCode's own tool
categories are the first-party packs below. Every agent build — the main
agent, a sub-agent, a workflow node — assembles its local tools through
:func:`assemble_tools`, then, after every other tool it has (its sub-agents' and
MCP servers' included), the tools of the Python extensions its profile's
``extensions`` lists through :func:`assemble_extension_tools`
(:mod:`chrys.service.extensions` finds, imports and binds them).

This module stays light: each ``create`` imports its implementation when it
runs, so reading the catalog (the agent editor does) loads no tool code. A
build imports :data:`IMPLEMENTATION_MODULES` off the event loop first.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any

from chrys.foundation.events.types import Warning
from chrys.foundation.i18n import MessageDef, msg
from chrys.foundation.tool_kinds import KIND_ASK_USER, get_tool_kind
from chrys.service.profiles.agents.schema import ALWAYS_LOADED_TOOL_CATEGORIES

if TYPE_CHECKING:
    from pathlib import Path

    from chrys.foundation.config.settings import Settings
    from chrys.foundation.events.bus import EventBus
    from chrys.foundation.models.session_env import SessionEnvironment
    from chrys.kernel import FunctionTool
    from chrys.service.profiles.agents.schema import ShellFilterConfig, ToolsConfig
    from chrys.service.todos.tracker import TodoTracker
    from chrys.service.tools.background import BackgroundTasks
    from chrys.service.tools.builtins.web.build import WebTools

logger = logging.getLogger(__name__)

_EXTENSION_UNAVAILABLE = msg(
    "builder.extension_unavailable",
    fallback="Agent {agent} starts without extension {extension}: {reason}",
)

_FILESYSTEM_READ = msg("tui.agent_config.tools.filesystem_read", fallback="Filesystem Read")
_FILESYSTEM_READ_DESCRIPTION = msg(
    "tui.agent_config.tools.filesystem_read_description",
    fallback="Read files from the filesystem",
)
_FILESYSTEM_WRITE = msg("tui.agent_config.tools.filesystem_write", fallback="Filesystem Write")
_FILESYSTEM_WRITE_DESCRIPTION = msg(
    "tui.agent_config.tools.filesystem_write_description",
    fallback="Create and edit files on the filesystem",
)
_SEARCH = msg("tui.agent_config.tools.search", fallback="File search")
_SEARCH_DESCRIPTION = msg(
    "tui.agent_config.tools.search_description",
    fallback="Search file contents and find files by pattern",
)
_SHELL = msg("tui.agent_config.tools.shell", fallback="Shell")
_SHELL_DESCRIPTION = msg(
    "tui.agent_config.tools.shell_description",
    fallback="Execute shell commands in a subprocess",
)
_ASK_USER = msg("tui.agent_config.tools.ask_user", fallback="Ask User")
_ASK_USER_DESCRIPTION = msg(
    "tui.agent_config.tools.ask_user_description",
    fallback="Prompt the user with one or more questions for input or clarification",
)
_DOCUMENT_CONVERTER = msg(
    "tui.agent_config.tools.document_converter",
    fallback="Document Converter (PDF, Office)",
)
_DOCUMENT_CONVERTER_DESCRIPTION = msg(
    "tui.agent_config.tools.document_converter_description",
    fallback="Convert PDF, DOCX, PPTX, XLSX, XLS to Markdown",
)
_TODO_LIST = msg("tui.agent_config.tools.todo_list", fallback="Todo List")
_TODO_LIST_DESCRIPTION = msg(
    "tui.agent_config.tools.todo_list_description",
    fallback="Track multi-step work as a live task checklist",
)
_WEB_SEARCH = msg("tui.agent_config.tools.web_search", fallback="Web search")
_WEB_SEARCH_DESCRIPTION = msg(
    "tui.agent_config.tools.web_search_description",
    fallback=(
        "Search the web for links and snippets. Uses Exa (exa.ai) unless the agent profile names other providers; "
        "queries are sent to that service and may carry details from your conversation, so mind your data and privacy"
    ),
)
_WEB_FETCH = msg("tui.agent_config.tools.web_fetch", fallback="Web fetch")
_WEB_FETCH_DESCRIPTION = msg(
    "tui.agent_config.tools.web_fetch_description",
    fallback="Read a page's text by URL",
)


@dataclass(frozen=True, slots=True)
class ToolBuild:
    """What one agent build gives its tool packs."""

    tools: ToolsConfig
    """The agent profile's tool configuration: which packs, and their options."""
    agent_name: str
    runtime: SessionEnvironment
    settings: Settings
    session_id: str | None = None
    session_dir: Path | None = None
    model_profile_id: str = ""
    chat_options: dict[str, Any] | None = None
    """The model's effective chat options; provider-hosted web tools displace local ones."""
    vision: bool = False
    """Whether the model reads images: a text-only agent gets no image-reading tool."""
    allow_user_interaction: bool = True
    """False leaves out ``ask_user``: nobody is there to answer."""
    background_tasks: BackgroundTasks | None = None
    """The session's background commands; without them a timed-out command is killed."""
    session_wide_background: bool = False
    """Whether background commands run until the session ends rather than until the agent finishes."""
    todo_tracker: TodoTracker | None = None
    """The session's todo list, which only the main agent keeps; without it ``todo_write`` reports it unavailable."""
    event_bus: EventBus | None = None
    """Where ``todo_write`` announces a changed list."""
    web: WebTools | None = None
    """Set by :func:`assemble_tools` before any pack is created; callers leave it None."""


@dataclass(frozen=True, slots=True)
class ToolPackRecord:
    """One pack in the catalog."""

    id: str
    create: Callable[[ToolBuild], list[FunctionTool]]
    title: MessageDef | None = None
    """The agent editor's label; None for a pack every agent loads, which has no switch."""
    description: MessageDef | None = None


@dataclass(frozen=True, slots=True)
class AssembledTools:
    """One agent build's local tools."""

    tools: list[FunctionTool]
    web: WebTools
    """The build's web tools, and what its caller publishes, reserves and checks."""
    by_pack: dict[str, list[str]] = field(default_factory=dict)
    """Tool names per first-party pack, in load order; a pack that contributed nothing is absent."""
    warnings: tuple[Warning, ...] = ()
    """What the caller publishes: the web tools' warnings."""


@dataclass(frozen=True, slots=True)
class ExtensionTools:
    """One agent build's extension tools."""

    tools: list[FunctionTool] = field(default_factory=list)
    by_extension: dict[str, list[str]] = field(default_factory=dict)
    """Tool names per extension, in the profile's order."""
    unavailable: dict[str, str] = field(default_factory=dict)
    """Why each extension the profile enables was left out."""
    warnings: tuple[Warning, ...] = ()
    """What the caller publishes: one per extension left out."""


def _create_filesystem_read(build: ToolBuild) -> list[FunctionTool]:
    from chrys.service.tools.builtins.filesystem import FilesystemTools

    fs = FilesystemTools(
        build.runtime,
        session_dir=build.session_dir,
        session_id=build.session_id or "",
        max_image_bytes=build.settings.image_send_limit_bytes(),
    )
    return [fs.read_file, fs.view_image]


def _create_filesystem_write(build: ToolBuild) -> list[FunctionTool]:
    from chrys.service.tools.builtins.filesystem import FilesystemTools

    fs = FilesystemTools(build.runtime)
    return [fs.write_file, fs.edit_file]


def _create_search(build: ToolBuild) -> list[FunctionTool]:
    from chrys.service.tools.builtins.search import SearchTools

    return SearchTools(build.runtime, respect_gitignore=build.settings.search_respect_gitignore).tools()


def _create_shell(build: ToolBuild) -> list[FunctionTool]:
    from chrys.service.tools.builtins.shell import ShellTools

    command_filter = _build_shell_filter(build.tools.shell_filter)
    # The platform shell first, then any extra one (e.g. Git Bash on Windows). Every shell offers
    # list_workspace_dirs, which lists the same folders whatever the shell, so a name comes once.
    tools: dict[str, FunctionTool] = {}
    for shell in (None, *build.runtime.platform.extra_shells):
        for tool in ShellTools(
            build.runtime,
            command_filter=command_filter,
            session_dir=build.session_dir,
            background_tasks=build.background_tasks,
            shell=shell,
            session_wide_background=build.session_wide_background,
        ).tools():
            tools.setdefault(tool.name, tool)
    return list(tools.values())


def _create_ask_user(_build: ToolBuild) -> list[FunctionTool]:
    from chrys.service.tools.builtins.ask_user import ask_user

    return [ask_user]


def _create_doc_converter(build: ToolBuild) -> list[FunctionTool]:
    from chrys.service.tools.builtins.doc_converter import DocConverterTools

    converter = DocConverterTools(build.runtime, session_id=build.session_id or "", session_dir=build.session_dir)
    # Images a document holds are only useful to an agent that can view them.
    converter.set_image_extraction_enabled(build.vision and "filesystem.read" in build.tools.builtins)
    return converter.tools()


def _create_todo(build: ToolBuild) -> list[FunctionTool]:
    from chrys.service.tools.builtins.todo import TodoTools

    return TodoTools(build.todo_tracker, build.event_bus, build.session_id).tools()


def _create_sleep(_build: ToolBuild) -> list[FunctionTool]:
    from chrys.service.tools.builtins.sleep import sleep

    return [sleep]


def _web_creator(category: str) -> Callable[[ToolBuild], list[FunctionTool]]:
    def create(build: ToolBuild) -> list[FunctionTool]:
        return build.web.tools(category) if build.web is not None else []

    return create


IMPLEMENTATION_MODULES: tuple[str, ...] = (
    "chrys.service.tools.builtins.filesystem",
    "chrys.service.tools.builtins.search",
    "chrys.service.tools.builtins.web.build",
    "chrys.service.tools.builtins.shell",
    "chrys.service.tools.builtins.ask_user",
    "chrys.service.tools.builtins.todo",
    "chrys.service.tools.builtins.sleep",
    "chrys.service.vision",
)
"""What creating the first-party packs imports (the document converter, enabled by few agents, loads when created)."""

BUILTIN_PACKS: tuple[ToolPackRecord, ...] = (
    ToolPackRecord("filesystem.read", _create_filesystem_read, _FILESYSTEM_READ, _FILESYSTEM_READ_DESCRIPTION),
    ToolPackRecord("filesystem.write", _create_filesystem_write, _FILESYSTEM_WRITE, _FILESYSTEM_WRITE_DESCRIPTION),
    ToolPackRecord("search", _create_search, _SEARCH, _SEARCH_DESCRIPTION),
    ToolPackRecord("web_search", _web_creator("web_search"), _WEB_SEARCH, _WEB_SEARCH_DESCRIPTION),
    ToolPackRecord("web_fetch", _web_creator("web_fetch"), _WEB_FETCH, _WEB_FETCH_DESCRIPTION),
    ToolPackRecord("shell", _create_shell, _SHELL, _SHELL_DESCRIPTION),
    ToolPackRecord("ask_user", _create_ask_user, _ASK_USER, _ASK_USER_DESCRIPTION),
    ToolPackRecord("doc_converter", _create_doc_converter, _DOCUMENT_CONVERTER, _DOCUMENT_CONVERTER_DESCRIPTION),
    ToolPackRecord("todo", _create_todo, _TODO_LIST, _TODO_LIST_DESCRIPTION),
    # Every agent loads it (``ALWAYS_LOADED_TOOL_CATEGORIES``), so the editor shows no switch.
    ToolPackRecord("sleep", _create_sleep),
)
"""iCode's own packs, in the order the agent editor lists them."""

_BUILTIN_PACKS_BY_ID: dict[str, ToolPackRecord] = {pack.id: pack for pack in BUILTIN_PACKS}

BUILTIN_PACK_IDS: frozenset[str] = frozenset(_BUILTIN_PACKS_BY_ID)
"""Every first-party pack id, the always-loaded ones included."""


def builtin_pack(pack_id: str) -> ToolPackRecord | None:
    """The first-party pack with *pack_id*, or None."""
    return _BUILTIN_PACKS_BY_ID.get(pack_id)


async def assemble_tools(build: ToolBuild) -> AssembledTools:
    """Build an agent's local tools from its profile's packs, in the profile's order.

    The listed first-party packs load in order, then every always-loaded one; a
    repeated or unknown id loads nothing more (an unknown one from a newer
    version, or a typo, stays in the profile so a save never drops it). A
    text-only model gets no image-reading tool, and ``ask_user`` is left out
    when nobody can answer.
    """
    requested = build.tools.builtins
    unknown = [pack_id for pack_id in requested if pack_id not in BUILTIN_PACK_IDS]
    if unknown:
        logger.warning("Ignoring unknown built-in tool categories: %s", ", ".join(unknown))
    pack_ids = list(
        dict.fromkeys(
            [
                *(pack_id for pack_id in requested if pack_id not in ALWAYS_LOADED_TOOL_CATEGORIES),
                *ALWAYS_LOADED_TOOL_CATEGORIES,
            ]
        )
    )
    web = _assemble_web(build, pack_ids)
    build = replace(build, web=web)
    from chrys.service.vision import filter_image_tools

    tools: list[FunctionTool] = []
    by_pack: dict[str, list[str]] = {}
    for pack_id, created in create_packs(build, pack_ids):
        created = filter_image_tools(created, vision_enabled=build.vision)
        if not build.allow_user_interaction:
            created = [tool for tool in created if get_tool_kind(tool) != KIND_ASK_USER]
        if created:
            tools.extend(created)
            by_pack[pack_id] = [tool.name for tool in created]
    return AssembledTools(tools=tools, web=web, by_pack=by_pack, warnings=web.warnings)


async def assemble_extension_tools(
    build: ToolBuild, extension_ids: Sequence[str], *, taken: Iterable[str]
) -> ExtensionTools:
    """The tools of the extensions the profile enables (*extension_ids*), in its order, after every other tool.

    *taken* is every name the agent's other tools take: its local, sub-agent and
    provider-hosted tools and all its MCP servers' (deferred ones included). The
    extensions come last so that they yield: one that cannot load, or one of
    whose tools repeats a name in *taken*, one of iCode's own tool names or an
    earlier extension's, is left out whole, and the agent starts either way.
    """
    if not extension_ids:
        return ExtensionTools()
    from chrys.service.extensions import bind_extension_tools, load_extensions
    from chrys.service.tools.names import chrys_reserved_tool_names

    taken = chrys_reserved_tool_names().union(taken)
    tools: list[FunctionTool] = []
    by_extension: dict[str, list[str]] = {}
    unavailable: dict[str, str] = {}
    for loaded in await load_extensions(extension_ids):
        if loaded.extension is None:
            unavailable[loaded.id] = loaded.problem
            continue
        try:
            created = bind_extension_tools(loaded.id, loaded.extension, build)
        except Exception as exc:
            logger.warning("Extension %s could not be set up", loaded.id, exc_info=True)
            unavailable[loaded.id] = f"setting up its tools failed: {type(exc).__name__}: {exc}"
            continue
        names = [tool.name for tool in created]
        if clashes := sorted(taken.intersection(names)):
            unavailable[loaded.id] = f"its tools {', '.join(clashes)} have names already in use"
            continue
        tools.extend(created)
        by_extension[loaded.id] = names
        taken.update(names)
    return ExtensionTools(
        tools=tools,
        by_extension=by_extension,
        unavailable=unavailable,
        warnings=tuple(
            _extension_unavailable_warning(build, extension_id, reason) for extension_id, reason in unavailable.items()
        ),
    )


def _extension_unavailable_warning(build: ToolBuild, extension_id: str, reason: str) -> Warning:
    logger.warning("Agent %s starts without extension %s: %s", build.agent_name, extension_id, reason)
    return Warning(
        code="extension_unavailable",
        message=f"Agent {build.agent_name} starts without extension {extension_id}: {reason}",
        display_message=_EXTENSION_UNAVAILABLE.bind(agent=build.agent_name, extension=extension_id, reason=reason),
        session_id=build.session_id,
    )


def create_packs(build: ToolBuild, pack_ids: list[str]) -> list[tuple[str, list[FunctionTool]]]:
    """Create each listed pack's tools, in order, before the build's filters apply."""
    return [(pack.id, pack.create(build)) for pack_id in pack_ids if (pack := builtin_pack(pack_id)) is not None]


def _assemble_web(build: ToolBuild, pack_ids: list[str]) -> WebTools:
    from chrys.service.tools.builtins.web.build import assemble_web_tools

    return assemble_web_tools(
        pack_ids,
        build.settings,
        build.tools.web_search,
        build.tools.web_fetch,
        chat_options=build.chat_options,
        agent=build.agent_name,
        model_profile_id=build.model_profile_id,
        session_id=build.session_id,
    )


def _build_shell_filter(config: ShellFilterConfig | None) -> Any:
    """Build a :class:`ShellCommandFilter` from profile config, or ``None``."""
    if config is None:
        return None

    from chrys.service.tools.builtins.shell_filter import ShellCommandFilter, get_preset

    # Preset takes priority
    if config.preset:
        preset = get_preset(config.preset)
        if preset is not None:
            return preset

    # Custom filter from explicit command list
    if not config.commands:
        return None

    cmd_set = frozenset(config.commands)
    return ShellCommandFilter(
        whitelist=cmd_set if config.mode == "whitelist" else None,
        blacklist=cmd_set if config.mode == "blacklist" else None,
        allow_redirections=config.allow_redirections,
        allow_subshells=config.allow_subshells,
    )
