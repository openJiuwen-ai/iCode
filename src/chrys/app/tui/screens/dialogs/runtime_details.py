# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""RuntimeDetailsDialog — modal with active model, tool, prompt shortcut, skill, hook, and file details."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, ClassVar

from rich.cells import cell_len
from rich.text import Text
from textual.containers import ScrollableContainer, VerticalGroup
from textual.widgets import Static, TabbedContent, TabPane

from chrys.app.tui.binding_display import CLOSE_BINDING, localized_binding
from chrys.app.tui.i18n import render_str, widget_localizer
from chrys.app.tui.screens.dialogs.base import BaseDialog
from chrys.app.tui.util.removal import remove_children_shielded
from chrys.foundation.events.types import AgentRuntimeDetails, RuntimeModelDetails
from chrys.foundation.i18n import MessageDef, MessageRef, msg
from chrys.foundation.i18n.formatting import format_message, sanitize_legacy_scalar
from chrys.foundation.platform.files import surrogate_safe_text

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable
    from pathlib import Path

    from textual.app import ComposeResult

    from chrys.service.prompt_shortcuts import PromptShortcut

    ShortcutSources = list[tuple[Path, list[PromptShortcut]]]

logger = logging.getLogger(__name__)


_INLINE_SKILLS_SOURCE = msg("tui.runtime_details.inline_skills_source", fallback="Inline profile skills")
_PROVIDER_LABELS: dict[str, str] = {
    "openai": "OpenAI",
    "anthropic": "Anthropic",
    "deepseek-openai": "DeepSeek (OpenAI)",
    "glm-openai": "GLM (OpenAI)",
}
_CHAT_COMPLETIONS = msg(
    "tui.runtime_details.api_style.chat_completions",
    fallback="Chat Completions",
)
_RESPONSES = msg("tui.runtime_details.api_style.responses", fallback="Responses")
_API_STYLE_LABELS: dict[str, MessageDef] = {
    "chat_completions": _CHAT_COMPLETIONS,
    "responses": _RESPONSES,
}

_RUNTIME_DETAILS = msg("tui.runtime_details.title", fallback="Runtime Details")
_MODEL = msg("tui.runtime_details.model", fallback="Model")
_TOOLS = msg("tui.runtime_details.tools", fallback="Tools")
_MCP = msg("tui.runtime_details.mcp", fallback="MCP")
_SKILLS = msg("tui.runtime_details.skills", fallback="Skills")
_SHORTCUTS = msg("tui.runtime_details.shortcuts", fallback="Shortcuts")
_LOADING_SHORTCUTS = msg("tui.runtime_details.loading.shortcuts", fallback="Reading prompt shortcuts…")
_NO_SHORTCUTS = msg(
    "tui.runtime_details.empty.shortcuts",
    fallback="No prompt shortcuts loaded.",
)
_HOOKS = msg("tui.runtime_details.hooks", fallback="Hooks")
RUNTIME_DETAILS_HOOKS_TAB = "runtime-hooks"
"""The Hooks tab's id, for opening the dialog on it."""
_FILES = msg("tui.runtime_details.files", fallback="Files")
_PROFILE_ID = msg("tui.runtime_details.label.profile_id", fallback="Profile ID")
_NAME = msg("tui.runtime_details.label.name", fallback="Name")
_PROVIDER = msg("tui.runtime_details.label.provider", fallback="Provider")
_API_STYLE = msg("tui.runtime_details.label.api_style", fallback="API Style")
_MODEL_ID = msg("tui.runtime_details.label.model_id", fallback="Model ID")
_CONTEXT = msg("tui.runtime_details.label.context", fallback="Context")
_BASE_URL = msg("tui.runtime_details.label.base_url", fallback="Base URL")
_STREAMING = msg("tui.runtime_details.label.streaming", fallback="Streaming")
_VISION = msg("tui.runtime_details.label.vision", fallback="Vision")
_MODEL_PROFILE = msg("tui.runtime_details.section.model_profile", fallback="Model Profile")
_NO_MODEL_PROFILE = msg(
    "tui.runtime_details.empty.model_profile",
    fallback="No active model profile details are available.",
)
_CONTEXT_TOOLS = msg("tui.runtime_details.section.context_tools", fallback="Context management tools")
_SUB_AGENT_TOOLS = msg("tui.runtime_details.section.sub_agent_tools", fallback="Sub-agent tools")
_NO_TOOLS = msg(
    "tui.runtime_details.empty.tools",
    fallback="No built-in or sub-agent tools loaded.",
)
_NO_EXPOSED_TOOLS = msg(
    "tui.runtime_details.empty.exposed_tools",
    fallback="Connected, but no tools are available to the model.",
)
_FAILED_MCP_SERVERS = msg("tui.runtime_details.section.failed_mcp_servers", fallback="Failed MCP servers")
_MCP_SERVERS = msg("tui.runtime_details.section.mcp_servers", fallback="MCP servers")
_NO_MCP_TOOLS = msg("tui.runtime_details.empty.mcp_tools", fallback="No MCP tools loaded.")
_INLINE_SKILLS = msg("tui.runtime_details.section.inline_skills", fallback="Inline skills")
_NO_SKILLS = msg("tui.runtime_details.empty.skills", fallback="No skills loaded.")
_NO_HOOKS = msg("tui.runtime_details.empty.hooks", fallback="No hooks loaded.")
_NO_HOOKS_IN_SOURCE = msg(
    "tui.runtime_details.empty.hooks_in_source",
    fallback="No hooks configured in this source.",
)
_PROJECT_HOOKS = msg("tui.runtime_details.section.project_hooks", fallback="Project hooks")
_GLOBAL_HOOKS = msg("tui.runtime_details.section.global_hooks", fallback="Global hooks")
_EVENT = msg("tui.runtime_details.label.event", fallback="Event")
_MODE = msg("tui.runtime_details.label.mode", fallback="Mode")
_ENABLED = msg("tui.runtime_details.label.enabled", fallback="Enabled")
_DESCRIPTION = msg("tui.runtime_details.label.description", fallback="Description")
_AUTO_LOADED_FILES = msg("tui.runtime_details.section.auto_loaded_files", fallback="Auto-loaded files")
_NO_PRECONFIGURED_FILES = msg(
    "tui.runtime_details.empty.preconfigured_files",
    fallback="No preconfigured files loaded.",
)
_NO_ENTRIES = msg("tui.runtime_details.empty.entries", fallback="No entries.")
_TOKEN_COUNT = msg("tui.runtime_details.token_count", fallback="{count_text} tokens")
_ON = msg("tui.runtime_details.boolean.on", fallback="ON")
_OFF = msg("tui.runtime_details.boolean.off", fallback="OFF")


def _context_label(
    tokens: int,
    render_message: Callable[[MessageRef], str] = format_message,
) -> str:
    if tokens <= 0:
        return "-"
    if tokens >= 1_000_000:
        count = tokens / 1_000_000
        unit = "m"
    elif tokens >= 1_000:
        count = tokens / 1_000
        unit = "k"
    else:
        return render_message(_TOKEN_COUNT.bind(count_text=str(tokens)))
    formatted = f"{count:.0f}" if count == int(count) else f"{count:.1f}"
    return render_message(_TOKEN_COUNT.bind(count_text=f"{formatted}{unit}"))


def _bool_label(
    value: bool,
    render_message: Callable[[MessageRef], str] = format_message,
) -> str:
    return render_message((_ON if value else _OFF).bind())


def _provider_label(provider: str) -> str:
    return _PROVIDER_LABELS.get(provider, provider)


def _api_style_label(
    api_style: str,
    render_message: Callable[[MessageRef], str] = format_message,
) -> str:
    definition = _API_STYLE_LABELS.get(api_style)
    return render_message(definition.bind()) if definition is not None else api_style or "-"


def _cell_clip(value: str, budget: int, *, from_end: bool = False) -> str:
    characters: list[str] = []
    used = 0
    for character in reversed(value) if from_end else value:
        width = cell_len(character)
        if used + width > budget:
            break
        characters.append(character)
        used += width
    if from_end:
        characters.reverse()
    return "".join(characters)


def _shorten(value: str, *, limit: int = 72) -> str:
    if cell_len(value) <= limit:
        return value
    head = max(8, (limit - 3) // 2)
    tail = max(8, limit - 3 - head)
    return f"{_cell_clip(value, head)}...{_cell_clip(value, tail, from_end=True)}"


def _unique(values: Iterable[str]) -> list[str]:
    return list(dict.fromkeys(v for v in values if v))


def _kv_lines(pairs: list[tuple[str, str]]) -> list[str]:
    width = max((cell_len(label) for label, _value in pairs), default=0)
    return [f"{label}{' ' * (width - cell_len(label))}  {value or '-'}" for label, value in pairs]


def _section(title: str, lines: list[str], *, empty: str) -> Static:
    body = "\n".join(lines or [empty])
    widget = Static(Text(body), classes="runtime-detail-section")
    widget.border_title = Text(_shorten(title))
    return widget


class RuntimeDetailsDialog(BaseDialog[None]):
    """Modal dialog for active agent runtime metadata."""

    CSS_PATH = "runtime_details.tcss"

    BINDINGS: ClassVar[list] = [
        localized_binding("escape", "close", CLOSE_BINDING, show=False, priority=True),
        localized_binding("q", "close", CLOSE_BINDING, show=False),
    ]

    def __init__(
        self,
        details: AgentRuntimeDetails,
        *,
        initial_tab: str | None = None,
        load_shortcuts: Callable[[], ShortcutSources] | None = None,
        shortcut_model: Callable[[str], str] | None = None,
    ) -> None:
        """*shortcut_model* turns a shortcut's ``model`` into the text listed for it; as written without one."""
        self._details = details
        self._initial_tab = initial_tab
        self._load_shortcuts = load_shortcuts
        self._shortcut_model_text = shortcut_model
        super().__init__()

    def on_mount(self) -> None:
        # Read as the / list reads them: off the event loop, every time the dialog opens.
        self.run_worker(self._show_shortcuts(), group="runtime-shortcuts", exit_on_error=False)

    def compose(self) -> ComposeResult:
        localizer = widget_localizer(self)
        with VerticalGroup(id="runtime-details-container") as container:
            container.border_title = Text(render_str(localizer, _RUNTIME_DETAILS.bind()))
            with TabbedContent(id="runtime-details-tabs", initial=self._initial_tab or ""):
                with (
                    TabPane(render_str(localizer, _MODEL.bind()), id="runtime-model"),
                    ScrollableContainer(classes="runtime-detail-scroll"),
                ):
                    for section in self._model_sections(self._details.model):
                        yield section
                with (
                    TabPane(render_str(localizer, _TOOLS.bind()), id="runtime-tools"),
                    ScrollableContainer(classes="runtime-detail-scroll"),
                ):
                    for section in self._tool_sections():
                        yield section
                with (
                    TabPane(render_str(localizer, _MCP.bind()), id="runtime-mcp"),
                    ScrollableContainer(classes="runtime-detail-scroll"),
                ):
                    for section in self._mcp_sections():
                        yield section
                with (
                    TabPane(render_str(localizer, _SHORTCUTS.bind()), id="runtime-shortcuts"),
                    ScrollableContainer(classes="runtime-detail-scroll", id="runtime-shortcuts-scroll"),
                ):
                    yield self._section(_SHORTCUTS.bind(), [self._render_message(_LOADING_SHORTCUTS.bind())])
                with (
                    TabPane(render_str(localizer, _SKILLS.bind()), id="runtime-skills"),
                    ScrollableContainer(classes="runtime-detail-scroll"),
                ):
                    for section in self._skill_sections():
                        yield section
                with (
                    TabPane(render_str(localizer, _HOOKS.bind()), id=RUNTIME_DETAILS_HOOKS_TAB),
                    ScrollableContainer(classes="runtime-detail-scroll"),
                ):
                    for section in self._hook_sections():
                        yield section
                with (
                    TabPane(render_str(localizer, _FILES.bind()), id="runtime-files"),
                    ScrollableContainer(classes="runtime-detail-scroll"),
                ):
                    for section in self._file_sections():
                        yield section

    def _model_sections(self, model: RuntimeModelDetails) -> list[Static]:
        if not model.name and not model.model_id:
            return [self._section(_MODEL.bind(), [self._render_message(_NO_MODEL_PROFILE.bind())])]
        return [
            self._section(
                _MODEL_PROFILE.bind(),
                _kv_lines(
                    [
                        (self._render_message(_PROFILE_ID.bind()), model.profile_id),
                        (self._render_message(_NAME.bind()), model.name),
                        (self._render_message(_PROVIDER.bind()), _provider_label(model.provider)),
                        (
                            self._render_message(_API_STYLE.bind()),
                            _api_style_label(model.api_style, self._render_message),
                        ),
                        (self._render_message(_MODEL_ID.bind()), model.model_id),
                        (
                            self._render_message(_CONTEXT.bind()),
                            _context_label(model.max_context_tokens, self._render_message),
                        ),
                        (self._render_message(_BASE_URL.bind()), model.base_url),
                        (self._render_message(_STREAMING.bind()), _bool_label(model.stream, self._render_message)),
                        (self._render_message(_VISION.bind()), _bool_label(model.vision, self._render_message)),
                    ]
                ),
            )
        ]

    def _tool_sections(self) -> list[Static]:
        sections: list[Static] = []
        web_providers = [f"{name}: {origin}" for name, origin in self._details.web_search_providers.items()]
        for category, names in self._details.builtin_tools.items():
            lines = [*names, *web_providers] if category == "web_search" else names
            sections.append(self._section(category, _unique(lines)))
        if web_providers and "web_search" not in self._details.builtin_tools:
            sections.append(self._section("web_search", web_providers))
        if self._details.context_tools:
            sections.append(self._section(_CONTEXT_TOOLS.bind(), _unique(self._details.context_tools)))
        if self._details.sub_agent_tools:
            sections.append(self._section(_SUB_AGENT_TOOLS.bind(), _unique(self._details.sub_agent_tools)))
        if not sections:
            sections.append(self._section(_TOOLS.bind(), [self._render_message(_NO_TOOLS.bind())]))
        return sections

    def _mcp_sections(self) -> list[Static]:
        sections: list[Static] = []
        failed_servers = set(self._details.mcp_failures)
        for server_name, names in self._details.mcp_tools.items():
            if server_name in failed_servers:
                continue
            sections.append(
                self._section(
                    server_name,
                    _unique(names),
                    empty=self._render_message(_NO_EXPOSED_TOOLS.bind()),
                )
            )
        if self._details.mcp_failures:
            lines = [f"{name}: {message}" for name, message in sorted(self._details.mcp_failures.items())]
            sections.append(self._section(_FAILED_MCP_SERVERS.bind(), lines))
        if not sections:
            sections.append(self._section(_MCP_SERVERS.bind(), [self._render_message(_NO_MCP_TOOLS.bind())]))
        return sections

    def _skill_sections(self) -> list[Static]:
        sections: list[Static] = []
        for source, names in self._details.skill_sources.items():
            is_inline = source == _INLINE_SKILLS_SOURCE.fallback
            title: MessageRef | str = _INLINE_SKILLS.bind() if is_inline else source
            lines = _unique(names)
            if is_inline:
                lines = [self._render_message(_INLINE_SKILLS_SOURCE.bind()), "", *lines]
            sections.append(self._section(title, lines))
        if not sections:
            sections.append(self._section(_SKILLS.bind(), [self._render_message(_NO_SKILLS.bind())]))
        return sections

    async def _show_shortcuts(self) -> None:
        sources: ShortcutSources = []
        if self._load_shortcuts is not None:
            try:
                sources = await asyncio.to_thread(self._load_shortcuts)
            except Exception:
                logger.exception("Runtime details: reading prompt shortcuts failed")
        # The dialog may have closed while the folders were read.
        if not self._composed_live:
            return
        container = self.query_one("#runtime-shortcuts-scroll", ScrollableContainer)
        sections = self._shortcut_sections(sources)
        with self.app.batch_update():
            await remove_children_shielded(container)
            await container.mount(*sections)

    def _shortcut_sections(self, sources: ShortcutSources) -> list[Static]:
        """One section per folder, in the order the / list reads them, so the first of a name is the one used."""
        sections: list[Static] = []
        for folder, commands in sources:
            names = [f"/{sanitize_legacy_scalar(command.name)}" for command in commands]
            width = max((cell_len(name) for name in names), default=0)
            lines = []
            for name, command in zip(names, commands, strict=True):
                agent = sanitize_legacy_scalar(command.agent).strip()
                parts = [
                    sanitize_legacy_scalar(command.description).strip(),
                    " · ".join(value for value in (agent, self._shortcut_model(command.model)) if value),
                ]
                detail = "  ".join(part for part in parts if part)
                lines.append(f"{name}{' ' * (width - cell_len(name))}  {detail}".rstrip())
            sections.append(self._section(sanitize_legacy_scalar(surrogate_safe_text(str(folder))), lines))
        if not sections:
            sections.append(self._section(_SHORTCUTS.bind(), [self._render_message(_NO_SHORTCUTS.bind())]))
        return sections

    def _shortcut_model(self, spec: str) -> str:
        model = spec.strip()
        if model and self._shortcut_model_text is not None:
            model = self._shortcut_model_text(model)
        return sanitize_legacy_scalar(model).strip()

    def _hook_sections(self) -> list[Static]:
        sections: list[Static] = []
        for source in self._details.hook_sources:
            title = _PROJECT_HOOKS.bind() if source.scope == "project" else _GLOBAL_HOOKS.bind()
            lines: list[str] = []
            if source.hooks:
                for hook in source.hooks:
                    if lines:
                        lines.append("")
                    lines.extend(
                        [
                            hook.id,
                            *_kv_lines(
                                [
                                    (self._render_message(_EVENT.bind()), hook.event),
                                    (self._render_message(_MODE.bind()), hook.execution_mode),
                                    (
                                        self._render_message(_ENABLED.bind()),
                                        _bool_label(hook.enabled, self._render_message),
                                    ),
                                    (self._render_message(_DESCRIPTION.bind()), hook.description),
                                ]
                            ),
                        ]
                    )
            else:
                lines.append(self._render_message(_NO_HOOKS_IN_SOURCE.bind()))
            section = self._section(title, lines)
            section.border_subtitle = Text(surrogate_safe_text(source.source_path))
            sections.append(section)
        if not sections:
            sections.append(self._section(_HOOKS.bind(), [self._render_message(_NO_HOOKS.bind())]))
        return sections

    def _file_sections(self) -> list[Static]:
        sections: list[Static] = []
        for source, files in self._details.memory_sources.items():
            sections.append(self._section(source, _unique(files)))
        if not sections:
            sections.append(
                self._section(
                    _AUTO_LOADED_FILES.bind(),
                    [self._render_message(_NO_PRECONFIGURED_FILES.bind())],
                )
            )
        return sections

    def _render_message(self, reference: MessageRef) -> str:
        return render_str(widget_localizer(self), reference)

    def _section(
        self,
        title: MessageRef | str,
        lines: list[str],
        *,
        empty: str | None = None,
    ) -> Static:
        rendered_title = title if isinstance(title, str) else self._render_message(title)
        rendered_empty = empty or self._render_message(_NO_ENTRIES.bind())
        return _section(rendered_title, lines, empty=rendered_empty)

    def action_close(self) -> None:
        """Close the dialog."""
        self.dismiss(None)
