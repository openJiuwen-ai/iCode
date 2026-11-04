# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""ToolRegistry — dynamic tool management for profile-driven tool loading."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from chrys.foundation.text.images import MAX_IMAGE_BYTES
from chrys.service.profiles.agents.schema import ALWAYS_LOADED_TOOL_CATEGORIES
from chrys.service.tools.builtins.ask_user import ask_user
from chrys.service.tools.builtins.filesystem import FilesystemTools
from chrys.service.tools.builtins.search import SearchTools
from chrys.service.tools.builtins.shell import ShellTools
from chrys.service.tools.builtins.sleep import sleep
from chrys.service.tools.builtins.todo import todo_write
from chrys.service.vision import VIEW_IMAGE_TOOL_NAME

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from pathlib import Path

    from chrys.foundation.config.settings import Settings
    from chrys.foundation.models.session_env import SessionEnvironment
    from chrys.service.profiles.agents.schema import ShellFilterConfig
    from chrys.service.tools.background import BackgroundTasks
    from chrys.service.tools.builtins.doc_converter import DocConverterTools
    from chrys.service.tools.builtins.web.build import WebTools

# Categories that use plain function tools (no runtime context needed).
_STATIC_CATEGORIES: dict[str, list[Any]] = {
    "ask_user": [ask_user],
    "sleep": [sleep],
    "todo": [todo_write],
}

# Categories that require SessionEnvironment and/or Settings and produce instance tools.
_INSTANCE_CATEGORIES: set[str] = {
    "filesystem.write",
    "filesystem.read",
    "search",
    "shell",
    "doc_converter",
}

# Categories whose tools the caller assembles beforehand (``assemble_web_tools``).
_WEB_CATEGORIES: frozenset[str] = frozenset({"web_search", "web_fetch"})


BUILTIN_TOOL_CATEGORIES: frozenset[str] = (
    frozenset(_STATIC_CATEGORIES) | frozenset(_INSTANCE_CATEGORIES) | _WEB_CATEGORIES
)
"""Every category name :meth:`ToolRegistry.load_builtins` knows, the always-loaded ones included."""


class ToolRegistry:
    """Central registry for all available tools.

    Supports dynamic loading and unloading of tools as profiles are switched.
    Tools can come from builtins, custom modules, or MCP servers.
    """

    def __init__(self, *, vision_enabled: bool = False) -> None:
        self._tools: dict[str, Any] = {}
        self._categories: dict[str, list[str]] = {}
        self._vision_enabled = vision_enabled
        self._doc_converters: list[DocConverterTools] = []

    def register(self, tool: Any, *, name: str | None = None, category: str = "custom") -> None:
        """Register a tool."""
        tool_name = name or getattr(tool, "name", None) or getattr(tool, "__name__", str(tool))
        if tool_name in _WEB_CATEGORIES and tool_name in self._tools:
            # Approval rules match these names by kind, so a second tool must never replace one silently.
            raise ValueError(f"A tool named {tool_name!r} is already registered")
        self._tools[tool_name] = tool
        self._categories.setdefault(category, []).append(tool_name)
        self._refresh_doc_converter_image_capability()

    def unregister(self, name: str) -> None:
        """Remove a registered tool."""
        self._tools.pop(name, None)
        for names in self._categories.values():
            if name in names:
                names.remove(name)
        self._refresh_doc_converter_image_capability()

    def get(self, name: str) -> Any | None:
        """Get a tool by name."""
        return self._tools.get(name)

    def get_all(self) -> list[Any]:
        """Get all registered tools."""
        return list(self._tools.values())

    def get_by_category(self, category: str) -> list[Any]:
        """Get all tools in a category."""
        names = self._categories.get(category, [])
        return [self._tools[n] for n in names if n in self._tools]

    def load_builtins(
        self,
        categories: list[str],
        runtime: SessionEnvironment | None = None,
        settings: Settings | None = None,
        shell_filter_config: ShellFilterConfig | None = None,
        session_id: str | None = None,
        session_dir: Path | None = None,
        web: WebTools | None = None,
        background_tasks: BackgroundTasks | None = None,
        *,
        session_wide_background: bool = False,
    ) -> list[Any]:
        """Load built-in tool categories by name, then every ``ALWAYS_LOADED_TOOL_CATEGORIES`` one.

        Args:
            categories: Category names like ["filesystem", "shell", "context", "search"]; a listed
                always-loaded category changes nothing.
            runtime: SessionEnvironment for instance tools that need session-scoped state.
            settings: Settings for instance tools that need configuration (e.g. context tools).
            shell_filter_config: Optional shell command filter configuration from the profile.
            session_id: Current session ID (used by instance tools that need session dir access).
            session_dir: Current session directory, when already resolved by the caller.
            web: The build's assembled web tools; without them the web categories load nothing.
            background_tasks: The session's background commands; without them a timed-out
                shell command is killed instead of moving to the background.
            session_wide_background: Whether the agent's background commands run until the
                session ends rather than until the agent finishes.

        Returns:
            List of all loaded tool functions/instances.
        """
        # A category from a newer version (or a typo) loads nothing; the profile keeps it so
        # saving never drops it.
        unknown = [category for category in categories if category not in BUILTIN_TOOL_CATEGORIES]
        if unknown:
            logger.warning("Ignoring unknown built-in tool categories: %s", ", ".join(unknown))
        effective: list[str] = []
        requested = [category for category in categories if category not in ALWAYS_LOADED_TOOL_CATEGORIES]
        for category in [*requested, *ALWAYS_LOADED_TOOL_CATEGORIES]:
            # A web tool's name must stay unique, so a repeated web category loads once.
            if category in BUILTIN_TOOL_CATEGORIES and not (category in _WEB_CATEGORIES and category in effective):
                effective.append(category)
        loaded: list[Any] = []
        for cat in effective:
            if cat in _WEB_CATEGORIES:
                tools = web.tools(cat) if web is not None else []
            elif cat in _INSTANCE_CATEGORIES:
                tools = self._create_instance_tools(
                    cat,
                    runtime,
                    settings,
                    shell_filter_config,
                    session_id,
                    session_dir,
                    background_tasks,
                    session_wide_background,
                )
            else:
                tools = _STATIC_CATEGORIES.get(cat, [])
            for t in tools:
                self.register(t, category=cat)
                loaded.append(t)
        return loaded

    def _create_instance_tools(
        self,
        category: str,
        runtime: SessionEnvironment | None,
        settings: Settings | None = None,
        shell_filter_config: ShellFilterConfig | None = None,
        session_id: str | None = None,
        session_dir: Path | None = None,
        background_tasks: BackgroundTasks | None = None,
        session_wide_background: bool = False,
    ) -> list[Any]:
        """Create instance-based tools that need SessionEnvironment or Settings."""
        if runtime is None:
            from chrys.foundation.models.session_env import SessionEnvironment as RC

            runtime = RC.capture()

        if category == "shell":
            command_filter = _build_shell_filter(shell_filter_config)
            shell = ShellTools(
                runtime,
                command_filter=command_filter,
                session_dir=session_dir,
                background_tasks=background_tasks,
                session_wide_background=session_wide_background,
            )
            tools = shell.tools()
            # Register additional shells (e.g. Git Bash on Windows)
            for extra_shell in runtime.platform.extra_shells:
                extra = ShellTools(
                    runtime,
                    command_filter=command_filter,
                    shell=extra_shell,
                    session_dir=session_dir,
                    background_tasks=background_tasks,
                    session_wide_background=session_wide_background,
                )
                tools.extend(extra.tools())
            return tools

        if category == "filesystem.write":
            fs = FilesystemTools(runtime)
            return [fs.write_file, fs.edit_file]

        if category == "filesystem.read":
            fs = FilesystemTools(
                runtime,
                session_dir=session_dir,
                session_id=session_id or "",
                max_image_bytes=settings.image_send_limit_bytes() if settings is not None else MAX_IMAGE_BYTES,
            )
            return [fs.read_file, fs.view_image]

        if category == "search":
            search = SearchTools(runtime, respect_gitignore=settings.search_respect_gitignore if settings else True)
            return search.tools()

        if category == "doc_converter":
            from chrys.service.tools.builtins.doc_converter import DocConverterTools

            dc = DocConverterTools(runtime, session_id=session_id or "", session_dir=session_dir)
            self._doc_converters.append(dc)
            return dc.tools()

        return []

    def clear(self) -> None:
        """Remove all registered tools."""
        for converter in self._doc_converters:
            converter.set_image_extraction_enabled(False)
        self._tools.clear()
        self._categories.clear()
        self._doc_converters.clear()

    def _refresh_doc_converter_image_capability(self) -> None:
        """Make converter image extraction match final registry capabilities."""
        enabled = self._vision_enabled and self.get(VIEW_IMAGE_TOOL_NAME) is not None
        for converter in self._doc_converters:
            converter.set_image_extraction_enabled(enabled)


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
