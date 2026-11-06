# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tool-specific renderers and the factory that selects them.

The factory returns a ToolCall (or compatible widget) based on the tool_name.
Unknown tools fall back to the base ToolCall (plain text rendering).

Custom renderers must be Widgets and implement the same interface as ToolCall:
    - ``__init__(call_id, tool_name, args_summary, args=None)``
    - ``set_complete(result, duration_ms)``
    - ``set_error(error)``
    - ``status`` reactive (running/complete/error)
    - ``call_id``, ``tool_name``, ``result_text`` attributes
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any, Protocol

from textual.widget import Widget

from chrys.app.tui.widgets.chat.tool_call import BaseToolCard, ToolCall
from chrys.foundation.hosted_tools import HostedToolFamily
from chrys.foundation.i18n import MessageDef, MessageRef, msg
from chrys.foundation.i18n.formatting import format_message
from chrys.foundation.tool_kinds import KIND_MCP, KIND_SUB_AGENT, is_extension_kind
from chrys.foundation.tool_result_metadata import TOOL_FAILURE_TEXT_SYNTHESIZED_METADATA_KEY

# Registry maps tool names to widget factories that follow the ToolCall protocol.
_REGISTRY: dict[str, _ToolRendererFactory] = {}
# Fallback registry maps tool kinds to widget classes.
_KIND_REGISTRY: dict[str, _ToolRendererFactory] = {}
_HOSTED_FAMILY_REGISTRY: dict[str, _ToolRendererFactory] = {}

_HOSTED_FAILURE_FALLBACK = msg(
    "hosted_tools.failure_fallback",
    fallback="Error: Provider-hosted tool failed.",
)
_HOSTED_TITLE_SEARCH = msg("hosted_tools.title_search", fallback="Hosted Search")
_HOSTED_TITLE_FETCH = msg("hosted_tools.title_fetch", fallback="Hosted Fetch")
_HOSTED_TITLE_MCP = msg("hosted_tools.title_mcp", fallback="Hosted MCP")
_HOSTED_TITLE_CODE = msg("hosted_tools.title_code", fallback="Hosted Code")
_HOSTED_TITLE_IMAGE = msg("hosted_tools.title_image", fallback="Hosted Image")
_HOSTED_TITLE_SHELL = msg("hosted_tools.title_shell", fallback="Hosted Shell")
_HOSTED_TITLE_TOOL_DISCOVERY = msg(
    "hosted_tools.title_tool_discovery",
    fallback="Hosted Tool Discovery",
)
_HOSTED_TITLE_FILE_OPERATION = msg(
    "hosted_tools.title_file_operation",
    fallback="Hosted File Operation",
)
_HOSTED_TITLE_GENERIC = msg("hosted_tools.title_generic", fallback="Hosted Tool")

_HOSTED_TITLE_BY_FAMILY: dict[str, MessageDef] = {
    HostedToolFamily.SEARCH: _HOSTED_TITLE_SEARCH,
    HostedToolFamily.FETCH: _HOSTED_TITLE_FETCH,
    HostedToolFamily.MCP: _HOSTED_TITLE_MCP,
    HostedToolFamily.CODE: _HOSTED_TITLE_CODE,
    HostedToolFamily.IMAGE: _HOSTED_TITLE_IMAGE,
    HostedToolFamily.SHELL: _HOSTED_TITLE_SHELL,
    HostedToolFamily.TOOL_DISCOVERY: _HOSTED_TITLE_TOOL_DISCOVERY,
    HostedToolFamily.FILE_OPERATION: _HOSTED_TITLE_FILE_OPERATION,
    HostedToolFamily.GENERIC: _HOSTED_TITLE_GENERIC,
}

# Kinds whose tool names are foreign- or user-chosen (MCP server tools,
# sub-agent profiles, and every extension's ``extension.<id>``): the
# chrys-owned name registry must not resolve them —
# a sub-agent or MCP tool legitimately named e.g. "bash" is not our shell
# tool. KIND_SHELL stays name-first: its names are chrys-stamped and the kind
# is deliberately split across renderers (execute vs. list_workspace_dirs).
_NAME_OPAQUE_KINDS = frozenset({KIND_MCP, KIND_SUB_AGENT})


def hosted_failure_display_text(
    text: str,
    metadata: Mapping[str, Any] | None,
    render_message: Callable[[MessageRef], str] = format_message,
) -> str:
    """Resolve Chrys-authored hosted failure text while preserving provider text."""
    if metadata and metadata.get(TOOL_FAILURE_TEXT_SYNTHESIZED_METADATA_KEY) is True:
        return render_message(_HOSTED_FAILURE_FALLBACK.bind())
    return text


def hosted_family_display_title(
    family: str,
    render_message: Callable[[MessageRef], str] = format_message,
) -> str | None:
    """Return the English catalog fallback for one known hosted family."""
    definition = _HOSTED_TITLE_BY_FAMILY.get(family)
    return render_message(definition.bind()) if definition is not None else None


class _ToolRendererFactory(Protocol):
    """Constructor shared by built-in and structurally compatible renderers."""

    def __call__(
        self,
        call_id: str,
        tool_name: str,
        args_summary: str = "",
        args: dict[str, Any] | None = None,
    ) -> Widget: ...


def register_renderer(tool_name: str, cls: _ToolRendererFactory) -> None:
    """Register a custom renderer class for a tool name."""
    _REGISTRY[tool_name] = cls


def register_kind_renderer(kind: str, cls: _ToolRendererFactory) -> None:
    """Register a fallback renderer class for a tool kind."""
    _KIND_REGISTRY[kind] = cls


def register_hosted_renderer(family: str, cls: _ToolRendererFactory) -> None:
    """Register a renderer for one provider-hosted family."""
    _HOSTED_FAMILY_REGISTRY[family] = cls


def register_dynamic_renderer(tool_name: str, cls: _ToolRendererFactory) -> None:
    """Register a renderer for a runtime-discovered, user-chosen tool name.

    Unlike :func:`register_renderer`, never overwrites an existing
    registration — a sub-agent profile exposed as e.g. ``bash`` must not
    hijack the builtin shell renderer for the rest of the process.
    """
    _ensure_loaded()
    _REGISTRY.setdefault(tool_name, cls)


def create_tool_widget(
    call_id: str,
    tool_name: str,
    tool_kind: str,
    args_summary: str = "",
    args: dict[str, Any] | None = None,
    *,
    provider_hosted: bool = False,
    hosted_family: str = "",
    provider: str = "",
    provider_item_type: str = "",
    provider_status: str = "",
    provider_call_id: str = "",
    canonical_status: str = "running",
    artifacts: list[dict[str, Any]] | None = None,
) -> Widget:
    """Create the appropriate widget for a given tool.

    Returns a specialized widget if one is registered for *tool_name*,
    falls back to *tool_kind* registry, otherwise returns the base ToolCall.
    """
    _ensure_loaded()
    from chrys.app.tui.widgets.chat.renderers.hosted_generic import HostedToolCall

    if provider_hosted:
        cls = _HOSTED_FAMILY_REGISTRY.get(hosted_family) or HostedToolCall
    else:
        name_opaque = tool_kind in _NAME_OPAQUE_KINDS or is_extension_kind(tool_kind)
        named = _REGISTRY.get(tool_name) if not name_opaque else None
        cls = named or _KIND_REGISTRY.get(tool_kind) or ToolCall
    widget = cls(call_id, tool_name, args_summary, args=args)
    if isinstance(widget, BaseToolCard):
        widget.tool_kind = tool_kind
        widget.configure_hosted(
            provider_hosted=provider_hosted,
            hosted_family=hosted_family,
            provider=provider,
            provider_item_type=provider_item_type,
            provider_status=provider_status,
            provider_call_id=provider_call_id,
            canonical_status=canonical_status,
            artifacts=artifacts,
        )
    return widget


_loaded = False


def _ensure_loaded() -> None:
    """Register built-in renderer classes on first use (lazy)."""
    global _loaded
    if _loaded:
        return

    from chrys.app.tui.widgets.chat.renderers.ask_user import AskUserToolCall
    from chrys.app.tui.widgets.chat.renderers.execute import ExecuteToolCall
    from chrys.app.tui.widgets.chat.renderers.file_edit import EditFileToolCall, WriteFileToolCall
    from chrys.app.tui.widgets.chat.renderers.hosted_code import HostedCodeToolCall
    from chrys.app.tui.widgets.chat.renderers.hosted_generic import HostedToolCall, HostedToolDiscoveryCall
    from chrys.app.tui.widgets.chat.renderers.hosted_image import HostedImageToolCall
    from chrys.app.tui.widgets.chat.renderers.hosted_mcp import HostedMcpToolCall
    from chrys.app.tui.widgets.chat.renderers.hosted_search import HostedSearchToolCall
    from chrys.app.tui.widgets.chat.renderers.hosted_shell import HostedShellToolCall
    from chrys.app.tui.widgets.chat.renderers.read_file import ReadFileToolCall
    from chrys.app.tui.widgets.chat.renderers.search import GlobToolCall, GrepToolCall
    from chrys.app.tui.widgets.chat.renderers.skill import SkillToolCall
    from chrys.app.tui.widgets.chat.renderers.sleep import SleepToolCall
    from chrys.app.tui.widgets.chat.renderers.sub_agent import SubAgentToolCall
    from chrys.app.tui.widgets.chat.renderers.todo import TodoToolCall
    from chrys.foundation.hosted_tools import HostedToolFamily
    from chrys.foundation.tool_kinds import KIND_ASK_USER, KIND_SHELL, KIND_SLEEP, KIND_SUB_AGENT, KIND_TODO
    from chrys.service.tools.builtins.shell import SHELL_TOOL_NAMES

    register_hosted_renderer(HostedToolFamily.SEARCH, HostedSearchToolCall)
    register_hosted_renderer(HostedToolFamily.FETCH, HostedSearchToolCall)
    register_hosted_renderer(HostedToolFamily.MCP, HostedMcpToolCall)
    register_hosted_renderer(HostedToolFamily.CODE, HostedCodeToolCall)
    register_hosted_renderer(HostedToolFamily.IMAGE, HostedImageToolCall)
    register_hosted_renderer(HostedToolFamily.SHELL, HostedShellToolCall)
    register_hosted_renderer(HostedToolFamily.TOOL_DISCOVERY, HostedToolDiscoveryCall)
    register_hosted_renderer(HostedToolFamily.FILE_OPERATION, HostedToolCall)
    register_hosted_renderer(HostedToolFamily.GENERIC, HostedToolCall)

    register_renderer("ask_user", AskUserToolCall)
    register_kind_renderer(KIND_ASK_USER, AskUserToolCall)

    # Name registrations cover legacy sessions with no persisted kind; the
    # kind registration covers any shell name (e.g. an exotic $SHELL basename).
    for name in SHELL_TOOL_NAMES:
        register_renderer(name, ExecuteToolCall)
    register_kind_renderer(KIND_SHELL, ExecuteToolCall)
    # Shares KIND_SHELL but is a workspace listing, not command execution —
    # the name pin keeps it off ExecuteToolCall's command/timeout chrome.
    register_renderer("list_workspace_dirs", ToolCall)

    register_renderer("edit_file", EditFileToolCall)
    register_renderer("write_file", WriteFileToolCall)
    register_renderer("read_file", ReadFileToolCall)
    from chrys.app.tui.widgets.chat.renderers.web import WebToolCall

    register_renderer("web_search", WebToolCall)
    register_renderer("web_fetch", WebToolCall)
    register_renderer("grep", GrepToolCall)
    register_renderer("glob", GlobToolCall)
    register_renderer("load_skill", SkillToolCall)
    register_renderer("read_skill_resource", SkillToolCall)
    register_renderer("run_skill_script", SkillToolCall)
    register_renderer("sleep", SleepToolCall)
    register_kind_renderer(KIND_SLEEP, SleepToolCall)
    register_kind_renderer(KIND_SUB_AGENT, SubAgentToolCall)
    register_renderer("todo_write", TodoToolCall)
    register_kind_renderer(KIND_TODO, TodoToolCall)

    _loaded = True
