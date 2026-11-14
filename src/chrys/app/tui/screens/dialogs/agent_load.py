# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""AgentLoadDialog — modal shown while agent infrastructure is loading."""

from __future__ import annotations

from contextlib import suppress
from dataclasses import dataclass
from typing import TYPE_CHECKING, ClassVar

from rich.style import Style
from rich.text import Text
from textual import on
from textual.binding import Binding
from textual.containers import HorizontalGroup, VerticalGroup
from textual.widgets import Button, Static

from chrys.app.tui.i18n import render_str
from chrys.app.tui.screens.dialogs.base import BaseDialog
from chrys.app.tui.util.rich_style import rich_style_from_textual_color
from chrys.app.tui.widgets import ChrysLoadingIndicator, DialogButtonRow, DialogButtonSpec
from chrys.foundation.events.types import (
    AGENT_LOAD_PHASE_AGENT,
    AGENT_LOAD_PHASE_MCP,
    AGENT_LOAD_PHASE_MODEL,
    AGENT_LOAD_PHASE_RUNTIME,
    AGENT_LOAD_PHASE_SESSION,
    AGENT_LOAD_PHASE_SKILLS,
    AGENT_LOAD_PHASE_SUB_AGENTS,
    AGENT_LOAD_PHASE_TOOLS,
    AGENT_LOAD_STATUS_DONE,
    AGENT_LOAD_STATUS_FAILED,
)
from chrys.foundation.i18n import MessageDef, MessageRef, msg
from chrys.foundation.i18n.formatting import format_message

if TYPE_CHECKING:
    from textual.app import ComposeResult

    from chrys.app.tui.i18n import LocaleController


_STAGE_PREPARING_AGENT = msg("tui.agent_load.stage.preparing_agent", fallback="Preparing agent")
_STAGE_CHECKING_SESSION = msg(
    "tui.agent_load.stage.checking_session_availability",
    fallback="Checking session availability",
)
_STAGE_SESSION_CHECKED = msg(
    "tui.agent_load.stage.session_availability_checked",
    fallback="Session availability checked",
)
_STAGE_RESOLVING_MODEL = msg("tui.agent_load.stage.resolving_model_profile", fallback="Resolving model profile")
_STAGE_MODEL_RESOLVED = msg("tui.agent_load.stage.model_profile_resolved", fallback="Model profile resolved")
_STAGE_LOADING_TOOLS = msg("tui.agent_load.stage.loading_builtin_tools", fallback="Loading built-in tools")
_STAGE_TOOLS_LOADED = msg("tui.agent_load.stage.builtin_tools_loaded", fallback="Built-in tools loaded")
_STAGE_LOADING_SUB_AGENTS = msg("tui.agent_load.stage.loading_sub_agents", fallback="Loading sub-agents")
_STAGE_SUB_AGENTS_LOADED = msg("tui.agent_load.stage.sub_agents_loaded", fallback="Sub-agents loaded")
_STAGE_CONNECTING_MCP = msg("tui.agent_load.stage.connecting_mcp_servers", fallback="Connecting MCP servers")
_STAGE_MCP_CONNECTED = msg("tui.agent_load.stage.mcp_servers_connected", fallback="MCP servers connected")
_STAGE_MCP_FAILED = msg("tui.agent_load.stage.mcp_servers_failed", fallback="MCP servers failed")
_STAGE_LOADING_SKILLS = msg("tui.agent_load.stage.loading_skills", fallback="Loading skills")
_STAGE_SKILLS_LOADED = msg("tui.agent_load.stage.skills_loaded", fallback="Skills loaded")
_STAGE_AGENT_FINALIZED = msg("tui.agent_load.stage.agent_finalized", fallback="Agent finalized")
_STAGE_CAPTURING_WORKSPACE = msg(
    "tui.agent_load.stage.capturing_workspace_context",
    fallback="Capturing workspace context",
)
_STAGE_FINALIZING_AGENT = msg("tui.agent_load.stage.finalizing_agent", fallback="Finalizing agent")
_STAGE_LOADING_SUB_AGENT = msg("tui.agent_load.stage.loading_sub_agent", fallback="Loading sub-agent {name}")
_STAGE_SUB_AGENT_MCP_CONNECTING = msg(
    "tui.agent_load.stage.sub_agent_mcp_connecting",
    fallback="Loading sub-agent {name} → Connecting MCP server {server}",
)
_STAGE_SUB_AGENT_MCP_CONNECTED = msg(
    "tui.agent_load.stage.sub_agent_mcp_connected",
    fallback="Loading sub-agent {name} → Connected MCP server {server}",
)
_STAGE_SUB_AGENT_MCP_FAILED = msg(
    "tui.agent_load.stage.sub_agent_mcp_failed",
    fallback="Loading sub-agent {name} → Failed MCP server {server}",
)
_STAGE_SUB_AGENT_SKILLS_LOADING = msg(
    "tui.agent_load.stage.sub_agent_skills_loading",
    fallback="Loading sub-agent {name} → Loading skills",
)
_STAGE_SUB_AGENT_SKILLS_LOADED = msg(
    "tui.agent_load.stage.sub_agent_skills_loaded",
    fallback="Loading sub-agent {name} → Skills loaded",
)
_STAGE_SUB_AGENT_SKILLS_FAILED = msg(
    "tui.agent_load.stage.sub_agent_skills_failed",
    fallback="Loading sub-agent {name} → Failed to load skills",
)
_STAGE_LOADED_SUB_AGENT = msg("tui.agent_load.stage.loaded_sub_agent", fallback="Loaded sub-agent {name}")
_STAGE_SKIPPED_SUB_AGENT = msg("tui.agent_load.stage.skipped_sub_agent", fallback="Skipped sub-agent {name}")
_STAGE_SKIPPED_SUB_AGENT_REASON = msg(
    "tui.agent_load.stage.skipped_sub_agent_reason",
    fallback="Skipped sub-agent {name}: {reason}",
)
_STAGE_CONNECTING_MCP_SERVER = msg(
    "tui.agent_load.stage.connecting_mcp_server",
    fallback="Connecting MCP server {name}",
)
_STAGE_CONNECTED_MCP_SERVER = msg(
    "tui.agent_load.stage.connected_mcp_server",
    fallback="Connected MCP server {name}",
)
_STAGE_FAILED_MCP_SERVER = msg("tui.agent_load.stage.failed_mcp_server", fallback="Failed MCP server {name}")
_STAGE_LOADING_MCP_SERVER = msg("tui.agent_load.stage.loading_mcp_server", fallback="Loading MCP server {name}")
_STATUS_COUNT_FAILED = msg(
    "tui.agent_load.status.count_failed",
    fallback="{current}/{total}, failed: {failed}",
)
_RESULT_OK = msg("tui.agent_load.button.ok", fallback="Ok")
_RESULT_UNABLE_TO_LOAD = msg("tui.agent_load.result.unable_to_load", fallback="Unable to Load Agent")
_RESULT_AGENT_LOADED = msg("tui.agent_load.result.agent_loaded", fallback="Agent Loaded")
_PROGRESS_WITH_COUNT = msg("tui.agent_load.progress.with_count", fallback="{label}: {count_text}")
_PROGRESS_WITH_COUNT_FAILED = msg(
    "tui.agent_load.progress.with_count_failed",
    fallback="{label}: {count_text}, failed: {failed}",
)
_LOADING_AGENT_TITLE = msg("tui.agent_load.default_title", fallback="Loading Agent")
_DEFAULT_TITLE = _LOADING_AGENT_TITLE.bind()

_KIND_PREPARING_AGENT = "preparing_agent"
_KIND_SESSION_CHECKING = "session_checking"
_KIND_SESSION_CHECKED = "session_checked"
_KIND_MODEL_RESOLVING = "model_resolving"
_KIND_TOOLS_LOADING = "tools_loading"
_KIND_SUB_AGENTS_LOADING = "sub_agents_loading"
_KIND_MCP_CONNECTING = "mcp_connecting"
_KIND_SKILLS_LOADING = "skills_loading"
_KIND_AGENT_FINALIZING = "agent_finalizing"
_KIND_RAW = "raw"

# Backend progress prose is a protocol. Detection remains exact and English;
# only the mapped display definition is localized.
_PROTOCOL_MESSAGE_KINDS = {
    "Preparing agent": _KIND_PREPARING_AGENT,
    "Checking session availability": _KIND_SESSION_CHECKING,
    "Session availability checked": _KIND_SESSION_CHECKED,
    "Resolving model profile": _KIND_MODEL_RESOLVING,
    "Loading built-in tools": _KIND_TOOLS_LOADING,
    "Loading sub-agents": _KIND_SUB_AGENTS_LOADING,
    "Connecting MCP servers": _KIND_MCP_CONNECTING,
    "Loading skills": _KIND_SKILLS_LOADING,
}
_REFERENCE_MESSAGE_KINDS = {
    "tui.agent_load.flow.preparing_agent": _KIND_PREPARING_AGENT,
    "tui.agent_load.flow.checking_session_availability": _KIND_SESSION_CHECKING,
    "tui.agent_load.flow.session_availability_checked": _KIND_SESSION_CHECKED,
}

# Status-bar mapping for the same protocol prose: exact stage sentences plus
# the name-carrying families the backend formats per sub-agent / MCP server.
_STATUS_PROSE_EXACT: dict[str, MessageDef] = {
    "Preparing agent": _STAGE_PREPARING_AGENT,
    "Checking session availability": _STAGE_CHECKING_SESSION,
    "Session availability checked": _STAGE_SESSION_CHECKED,
    "Resolving model profile": _STAGE_RESOLVING_MODEL,
    "Model profile resolved": _STAGE_MODEL_RESOLVED,
    "Capturing workspace context": _STAGE_CAPTURING_WORKSPACE,
    "Loading built-in tools": _STAGE_LOADING_TOOLS,
    "Built-in tools loaded": _STAGE_TOOLS_LOADED,
    "Loading sub-agents": _STAGE_LOADING_SUB_AGENTS,
    "Loading sub-agent tools": _STAGE_LOADING_SUB_AGENTS,
    "Sub-agents loaded": _STAGE_SUB_AGENTS_LOADED,
    "Connecting MCP servers": _STAGE_CONNECTING_MCP,
    "MCP servers connected": _STAGE_MCP_CONNECTED,
    "MCP servers failed": _STAGE_MCP_FAILED,
    "Loading skills": _STAGE_LOADING_SKILLS,
    "Skills loaded": _STAGE_SKILLS_LOADED,
    "Finalizing agent": _STAGE_FINALIZING_AGENT,
    "Agent finalized": _STAGE_AGENT_FINALIZED,
}
_STATUS_PROSE_NAME_PREFIXES: tuple[tuple[str, MessageDef], ...] = (
    ("Loading sub-agent ", _STAGE_LOADING_SUB_AGENT),
    ("Loaded sub-agent ", _STAGE_LOADED_SUB_AGENT),
    ("Skipped sub-agent ", _STAGE_SKIPPED_SUB_AGENT),
    ("Connecting MCP server ", _STAGE_CONNECTING_MCP_SERVER),
    ("Connected MCP server ", _STAGE_CONNECTED_MCP_SERVER),
    ("Failed MCP server ", _STAGE_FAILED_MCP_SERVER),
    ("Loading MCP server ", _STAGE_LOADING_MCP_SERVER),
)


def map_load_progress_prose(message: str) -> MessageRef | None:
    """Map backend load-progress prose onto its localized display definition.

    Backend progress prose is a protocol: detection stays exact and English.
    Unknown prose returns None so callers keep the raw text verbatim.
    """
    definition = _STATUS_PROSE_EXACT.get(message)
    if definition is not None:
        return definition.bind()
    for prefix, family in _STATUS_PROSE_NAME_PREFIXES:
        remainder = message.removeprefix(prefix)
        if remainder == message or not remainder:
            continue
        if family is _STAGE_SKIPPED_SUB_AGENT and ": " in remainder:
            name, reason = remainder.split(": ", 1)
            return _STAGE_SKIPPED_SUB_AGENT_REASON.bind(name=name, reason=reason)
        return family.bind(name=remainder)
    return None


def load_count_failed_message(current: int, total: int, failed: int) -> MessageRef:
    """Return the localized "{current}/{total}, failed: {failed}" count text."""
    return _STATUS_COUNT_FAILED.bind(current=current, total=total, failed=failed)


def sub_agent_load_stage_message(name: str, stage: str, subject: str, status: str) -> MessageRef | None:
    """Localize structured child progress independently of the backend prose."""
    if not name:
        return None
    if stage == AGENT_LOAD_PHASE_MCP and subject:
        definition = {
            AGENT_LOAD_STATUS_DONE: _STAGE_SUB_AGENT_MCP_CONNECTED,
            AGENT_LOAD_STATUS_FAILED: _STAGE_SUB_AGENT_MCP_FAILED,
        }.get(status, _STAGE_SUB_AGENT_MCP_CONNECTING)
        return definition.bind(name=name, server=subject)
    if stage == AGENT_LOAD_PHASE_SKILLS:
        definition = {
            AGENT_LOAD_STATUS_DONE: _STAGE_SUB_AGENT_SKILLS_LOADED,
            AGENT_LOAD_STATUS_FAILED: _STAGE_SUB_AGENT_SKILLS_FAILED,
        }.get(status, _STAGE_SUB_AGENT_SKILLS_LOADING)
        return definition.bind(name=name)
    return None


type AgentLoadMessage = MessageRef | str


@dataclass
class _ProgressEntry:
    message: AgentLoadMessage
    key: str
    semantic_kind: str
    status: str = "active"
    current: int = 0
    total: int = 0
    failed: int = 0
    has_count: bool = False
    count_current: int = 0
    count_total: int = 0
    include_failed: bool = False


class AgentLoadDialog(BaseDialog[None]):
    """Modal dialog shown while building agent tools and context."""

    ALLOW_SELECT: ClassVar[bool] = False
    """Transient loading chrome — no text selection anywhere in the dialog."""

    MAX_PROGRESS_LINES: ClassVar[int] = 8
    COMPLETE_STEP_HOLD_SECONDS: ClassVar[float] = 0.03
    FINISH_HOLD_SECONDS: ClassVar[float] = 0.25
    FINISH_ENTRY_KEY: ClassVar[str] = "finish"

    BINDINGS: ClassVar[list] = [
        Binding("escape", "dismiss_if_allowed", show=False, priority=True),
    ]

    CSS_PATH = "agent_load.tcss"

    def __init__(
        self,
        title: AgentLoadMessage = _DEFAULT_TITLE,
        subtitle: str = "",
        *,
        locale_controller: LocaleController | None = None,
    ) -> None:
        self._locale_controller = locale_controller
        self._title_message = title
        self._title = self._render_message(title)
        self._subtitle = subtitle
        self._message = ""
        self._messages: list[str] = []
        self._progress_entries: list[_ProgressEntry] = []
        self._resolved = False
        self._esc_allowed = False
        self._mounted = False
        self._dismiss_pending = False
        self._pending_result: tuple[bool, AgentLoadMessage, bool] | None = None
        super().__init__()

    def compose(self) -> ComposeResult:
        with VerticalGroup(id="agent-load-container") as container:
            container.border_title = Text(self._render_message(self._title_message))
            if self._subtitle:
                container.border_subtitle = Text(self._subtitle)
            with VerticalGroup(id="agent-load-inner"):
                yield ChrysLoadingIndicator(id="agent-load-loading")
                result_title = Static("", id="agent-load-result-title", markup=False)
                result_title.display = False
                yield result_title
                yield Static(
                    self._active_line(self._render_message(_STAGE_PREPARING_AGENT.bind())),
                    id="agent-load-message",
                    markup=False,
                )
            buttons = DialogButtonRow(
                DialogButtonSpec(
                    Text(self._render_message(_RESULT_OK.bind())),
                    id="agent-load-ok",
                    variant="primary",
                ),
                id="agent-load-buttons",
            )
            buttons.display = False
            yield buttons

    def on_mount(self) -> None:
        self._mounted = True
        if not self._live:
            # Pushed as the app shuts down: its children were pruned or never mounted; nothing will render it.
            return
        if self._pending_result is not None:
            success, message, allow_esc = self._pending_result
            self._pending_result = None
            self._apply_result(success, message, allow_esc)
        elif self._dismiss_pending:
            self._apply_finish_state()
            self._schedule_finish_after_refresh()
        else:
            self._apply_progress()

    @property
    def _live(self) -> bool:
        # Owners may still report once its children went, as removal starts, or never mounted, as
        # the app shuts down. Buffer those updates as before mount; nothing will render them.
        return self._mounted and self._composed_live

    def update_progress(
        self,
        message: AgentLoadMessage,
        subtitle: str = "",
        phase: str = "",
        server_name: str = "",
        current: int = 0,
        total: int = 0,
        failed: int = 0,
        status: str = "",
    ) -> bool:
        """Update the visible loading message."""
        self._message = self._render_message(message)
        completed = self._record_progress(
            message,
            phase=phase,
            server_name=server_name,
            current=current,
            total=total,
            failed=failed,
            status=status,
        )
        if subtitle:
            self._subtitle = subtitle
        if not self._live or self._resolved:
            return completed
        self._apply_progress()
        return completed

    def update_title(self, title: AgentLoadMessage) -> None:
        """Update the operation title after its display name has been read."""
        self._title_message = title
        self._title = self._render_message(title)
        if self._live:
            self.query_one("#agent-load-container").border_title = Text(self._title)

    def update_finish_progress(self, message: AgentLoadMessage) -> None:
        """Show the final post-build step, which will be replaced by the result."""
        self._message = self._render_message(message)
        self._record_finish_message(message, status="active")
        if not self._live or self._resolved:
            return
        self._apply_progress()

    def _record_progress(
        self,
        message: AgentLoadMessage,
        *,
        phase: str = "",
        server_name: str = "",
        current: int = 0,
        total: int = 0,
        failed: int = 0,
        status: str = "",
    ) -> bool:
        if not message:
            return False

        summary = self._summary_entry(
            message,
            phase=phase,
            server_name=server_name,
            current=current,
            total=total,
            failed=failed,
            status=status,
        )
        if summary is None:
            return False

        completed = self._finish_superseded_entries(key=summary.key)

        for entry in self._progress_entries:
            if entry.key == summary.key:
                status_changed_to_done = entry.status != "done" and summary.status == "done"
                entry.message = summary.message
                entry.semantic_kind = summary.semantic_kind
                entry.status = summary.status
                entry.current = summary.current
                entry.total = summary.total
                entry.failed = summary.failed
                entry.has_count = summary.has_count
                entry.count_current = summary.count_current
                entry.count_total = summary.count_total
                entry.include_failed = summary.include_failed
                self._sync_messages()
                return completed or status_changed_to_done

        self._progress_entries.append(summary)
        self._progress_entries = self._progress_entries[-self.MAX_PROGRESS_LINES :]
        self._sync_messages()
        return completed or summary.status == "done"

    def _summary_entry(
        self,
        message: AgentLoadMessage,
        *,
        phase: str,
        server_name: str,
        current: int,
        total: int,
        failed: int,
        status: str,
    ) -> _ProgressEntry | None:
        del server_name
        semantic_kind = self._semantic_kind(message)

        if not phase and semantic_kind == _KIND_PREPARING_AGENT:
            return None

        if phase == AGENT_LOAD_PHASE_RUNTIME:
            return None

        if phase == AGENT_LOAD_PHASE_SESSION:
            if semantic_kind == _KIND_SESSION_CHECKED:
                return _ProgressEntry(
                    _STAGE_SESSION_CHECKED.bind(),
                    AGENT_LOAD_PHASE_SESSION,
                    _KIND_SESSION_CHECKED,
                    "done",
                )
            return _ProgressEntry(
                _STAGE_CHECKING_SESSION.bind(),
                AGENT_LOAD_PHASE_SESSION,
                _KIND_SESSION_CHECKING,
            )

        if phase == AGENT_LOAD_PHASE_MODEL:
            return _ProgressEntry(
                _STAGE_RESOLVING_MODEL.bind(),
                AGENT_LOAD_PHASE_MODEL,
                _KIND_MODEL_RESOLVING,
            )

        if phase == AGENT_LOAD_PHASE_TOOLS:
            if status == AGENT_LOAD_STATUS_DONE:
                return _ProgressEntry(
                    _STAGE_TOOLS_LOADED.bind(),
                    AGENT_LOAD_PHASE_TOOLS,
                    _KIND_TOOLS_LOADING,
                    "done",
                    current,
                    total,
                    failed,
                    True,
                    current,
                    total,
                )
            return _ProgressEntry(
                _STAGE_LOADING_TOOLS.bind(),
                AGENT_LOAD_PHASE_TOOLS,
                _KIND_TOOLS_LOADING,
                current=current,
                total=total,
                failed=failed,
            )

        if phase == AGENT_LOAD_PHASE_SUB_AGENTS:
            nested_stage = isinstance(message, MessageRef) and message.definition.key.startswith(
                "tui.agent_load.stage.sub_agent_"
            )
            if total:
                entry_status = "done" if current >= total else "active"
                action = _STAGE_SUB_AGENTS_LOADED if entry_status == "done" else _STAGE_LOADING_SUB_AGENTS
                return _ProgressEntry(
                    message if nested_stage and entry_status != "done" else action.bind(),
                    AGENT_LOAD_PHASE_SUB_AGENTS,
                    _KIND_SUB_AGENTS_LOADING,
                    entry_status,
                    current,
                    total,
                    failed,
                    True,
                    current,
                    total,
                )
            return _ProgressEntry(
                _STAGE_LOADING_SUB_AGENTS.bind(),
                AGENT_LOAD_PHASE_SUB_AGENTS,
                _KIND_SUB_AGENTS_LOADING,
                current=current,
                total=total,
                failed=failed,
            )

        if phase == AGENT_LOAD_PHASE_MCP:
            if total and failed:
                finished = current + failed
                if finished >= total:
                    if current:
                        label = _STAGE_MCP_CONNECTED
                        count_current = current
                        include_failed = True
                    else:
                        label = _STAGE_MCP_FAILED
                        count_current = failed
                        include_failed = False
                else:
                    label = _STAGE_CONNECTING_MCP
                    count_current = current
                    include_failed = True
                return _ProgressEntry(
                    label.bind(),
                    AGENT_LOAD_PHASE_MCP,
                    _KIND_MCP_CONNECTING,
                    "error",
                    current,
                    total,
                    failed,
                    True,
                    count_current,
                    total,
                    include_failed,
                )
            if total:
                entry_status = "done" if current >= total else "active"
                action = _STAGE_MCP_CONNECTED if entry_status == "done" else _STAGE_CONNECTING_MCP
                return _ProgressEntry(
                    action.bind(),
                    AGENT_LOAD_PHASE_MCP,
                    _KIND_MCP_CONNECTING,
                    entry_status,
                    current,
                    total,
                    failed,
                    True,
                    current,
                    total,
                )
            if status == AGENT_LOAD_STATUS_FAILED:
                return _ProgressEntry(
                    _STAGE_MCP_FAILED.bind(),
                    AGENT_LOAD_PHASE_MCP,
                    _KIND_MCP_CONNECTING,
                    "error",
                    current,
                    total,
                    failed,
                    True,
                    current,
                    total,
                )
            return _ProgressEntry(
                _STAGE_CONNECTING_MCP.bind(),
                AGENT_LOAD_PHASE_MCP,
                _KIND_MCP_CONNECTING,
                "active",
                current,
                total,
                failed,
                True,
                current,
                total,
            )

        if phase == AGENT_LOAD_PHASE_SKILLS:
            if status == AGENT_LOAD_STATUS_DONE:
                return _ProgressEntry(
                    _STAGE_SKILLS_LOADED.bind(),
                    AGENT_LOAD_PHASE_SKILLS,
                    _KIND_SKILLS_LOADING,
                    "done",
                    current,
                    total,
                    failed,
                    True,
                    current,
                    total,
                )
            if current or total:
                entry_status = "done" if current >= total else "active"
                action = _STAGE_SKILLS_LOADED if entry_status == "done" else _STAGE_LOADING_SKILLS
                return _ProgressEntry(
                    action.bind(),
                    AGENT_LOAD_PHASE_SKILLS,
                    _KIND_SKILLS_LOADING,
                    entry_status,
                    current,
                    total,
                    failed,
                    True,
                    current,
                    total,
                )
            return _ProgressEntry(
                _STAGE_LOADING_SKILLS.bind(),
                AGENT_LOAD_PHASE_SKILLS,
                _KIND_SKILLS_LOADING,
                current=current,
                total=total,
                failed=failed,
            )

        if phase == AGENT_LOAD_PHASE_AGENT:
            return None

        key = phase or (message.definition.key if isinstance(message, MessageRef) else message)
        return _ProgressEntry(
            message,
            key,
            semantic_kind,
            self._progress_status(status),
            current,
            total,
            failed,
        )

    def _finish_superseded_entries(self, *, key: str) -> bool:
        completed = False
        for entry in self._progress_entries:
            if entry.status != "active" or entry.key == key:
                continue
            if entry.semantic_kind == _KIND_MODEL_RESOLVING:
                entry.message = _STAGE_MODEL_RESOLVED.bind()
                entry.status = "done"
            elif entry.semantic_kind == _KIND_TOOLS_LOADING:
                entry.message = _STAGE_TOOLS_LOADED.bind()
                entry.has_count = True
                entry.count_current = entry.current or entry.total
                entry.count_total = entry.total
                entry.status = "done"
            elif entry.semantic_kind == _KIND_SUB_AGENTS_LOADING:
                entry.message = _STAGE_SUB_AGENTS_LOADED.bind()
                entry.has_count = True
                entry.count_current = entry.current or entry.total
                entry.count_total = entry.total
                entry.status = "done"
            elif entry.semantic_kind == _KIND_MCP_CONNECTING:
                entry.has_count = True
                entry.count_total = entry.total
                if entry.failed and entry.total:
                    connected = entry.current
                    if connected:
                        entry.message = _STAGE_MCP_CONNECTED.bind()
                        entry.count_current = connected
                        entry.include_failed = True
                    else:
                        entry.message = _STAGE_MCP_FAILED.bind()
                        entry.count_current = entry.failed
                        entry.include_failed = False
                    entry.status = "error"
                else:
                    entry.message = _STAGE_MCP_CONNECTED.bind()
                    entry.count_current = entry.current or entry.total
                    entry.status = "done"
            elif entry.semantic_kind == _KIND_SKILLS_LOADING:
                entry.message = _STAGE_SKILLS_LOADED.bind()
                entry.has_count = True
                entry.count_current = entry.current or entry.total
                entry.count_total = entry.total
                entry.status = "done"
            elif entry.semantic_kind == _KIND_AGENT_FINALIZING:
                entry.message = _STAGE_AGENT_FINALIZED.bind()
                entry.status = "done"
            else:
                entry.status = "done"
            completed = True
        if completed:
            self._sync_messages()
        return completed

    @staticmethod
    def _progress_status(status: str) -> str:
        if status == AGENT_LOAD_STATUS_FAILED:
            return "error"
        if status == AGENT_LOAD_STATUS_DONE:
            return "done"
        return "active"

    @staticmethod
    def _semantic_kind(message: AgentLoadMessage) -> str:
        if isinstance(message, MessageRef):
            return _REFERENCE_MESSAGE_KINDS.get(message.definition.key, message.definition.key)
        return _PROTOCOL_MESSAGE_KINDS.get(message, _KIND_RAW)

    def _render_message(self, message: AgentLoadMessage) -> str:
        if isinstance(message, str):
            return message
        controller = self._locale_controller
        return format_message(message) if controller is None else render_str(controller.localizer, message)

    def _render_entry(self, entry: _ProgressEntry) -> str:
        label = self._render_message(entry.message)
        if not entry.has_count:
            return label
        count_text = f"{entry.count_current}/{entry.count_total}" if entry.count_total else "-"
        if entry.include_failed:
            return self._render_message(
                _PROGRESS_WITH_COUNT_FAILED.bind(label=label, count_text=count_text, failed=entry.failed)
            )
        return self._render_message(_PROGRESS_WITH_COUNT.bind(label=label, count_text=count_text))

    def _sync_messages(self) -> None:
        self._messages = [self._render_entry(entry) for entry in self._progress_entries]

    def _in_progress_style(self) -> Style:
        """Marker style for active steps, matching the todo checklist."""
        warning = "yellow"
        with suppress(Exception):
            warning = self.app.theme_variables.get("warning", "yellow")
        return rich_style_from_textual_color(warning, bold=True)

    def _active_line(self, message: str) -> Text:
        """One in-progress line: todo-style ``▸`` marker + plain message."""
        text = Text()
        text.append("▸ ", style=self._in_progress_style())
        text.append(message)
        return text

    def _apply_progress(self) -> None:
        """Render the latest buffered progress state."""
        self.query_one("#agent-load-result-title", Static).display = False
        if not self._progress_entries:
            preparing = self._render_message(_STAGE_PREPARING_AGENT.bind())
            self.query_one("#agent-load-message", Static).update(self._active_line(preparing))
            return
        text = Text()
        for index, entry in enumerate(self._progress_entries):
            message = self._render_entry(entry)
            if index:
                text.append("\n")
            if entry.status == "done":
                text.append("✓ ", style="green")
                text.append(message, style="dim")
            elif entry.status == "error":
                text.append(f"⚠️ {message}", style="bold red")
            else:
                text.append_text(self._active_line(message))
        self.query_one("#agent-load-message", Static).update(text)
        if self._subtitle:
            self.query_one("#agent-load-container", VerticalGroup).border_subtitle = Text(self._subtitle)

    def finish(self, message: AgentLoadMessage = "") -> None:
        """Dismiss the loading dialog after a successful build."""
        if self._resolved:
            return
        self._resolved = True
        if message:
            self._record_finish_message(message, status="done")
        else:
            self._mark_finish_message_done()
        if not self._live:
            self._dismiss_pending = True
            return
        self._apply_finish_state()
        self._schedule_finish_after_refresh()

    def _record_finish_message(self, message: AgentLoadMessage, *, status: str) -> None:
        if not message:
            return
        for index, entry in enumerate(self._progress_entries):
            if entry.key == self.FINISH_ENTRY_KEY:
                entry.message = message
                entry.semantic_kind = message.definition.key if isinstance(message, MessageRef) else _KIND_RAW
                entry.status = status
                entry.has_count = False
                entry.include_failed = False
                self._progress_entries.pop(index)
                self._progress_entries.append(entry)
                self._progress_entries = self._progress_entries[-self.MAX_PROGRESS_LINES :]
                self._sync_messages()
                return
        semantic_kind = message.definition.key if isinstance(message, MessageRef) else _KIND_RAW
        self._progress_entries.append(_ProgressEntry(message, self.FINISH_ENTRY_KEY, semantic_kind, status))
        self._progress_entries = self._progress_entries[-self.MAX_PROGRESS_LINES :]
        self._sync_messages()

    def _mark_finish_message_done(self) -> None:
        for entry in self._progress_entries:
            if entry.key == self.FINISH_ENTRY_KEY:
                entry.status = "done"
                self._sync_messages()
                return

    def _apply_finish_state(self) -> None:
        self._apply_progress()
        self.add_class("-success")
        self._remove_loading_indicator()

    def _remove_loading_indicator(self) -> None:
        loading = self.query_one("#agent-load-loading", ChrysLoadingIndicator)
        loading.display = False
        loading.remove()

    def _schedule_finish(self) -> None:
        self.set_timer(self.FINISH_HOLD_SECONDS, self._dismiss_after_finish)

    def _schedule_finish_after_refresh(self) -> None:
        if not self.call_after_refresh(self._schedule_finish):
            self._schedule_finish()

    def _dismiss_after_finish(self) -> None:
        self.request_dismiss()

    def request_dismiss(self) -> None:
        self.dismiss_when_topmost()

    def _on_deferred_dismiss(self) -> None:
        with suppress(Exception):
            self.query_one("#agent-load-container", VerticalGroup).display = False

    def set_result(self, success: bool, message: AgentLoadMessage, allow_esc: bool = False) -> None:
        """Swap the loading state for a final result + Ok button."""
        if self._resolved:
            return
        self._resolved = True

        if not self._live:
            self._pending_result = (success, message, allow_esc)
            return

        self._apply_result(success, message, allow_esc)

    def _apply_result(self, success: bool, message: AgentLoadMessage, allow_esc: bool) -> None:
        self._esc_allowed = allow_esc

        self._remove_loading_indicator()

        title_widget = self.query_one("#agent-load-result-title", Static)
        title_widget.display = not success
        unable = self._render_message(_RESULT_UNABLE_TO_LOAD.bind())
        title_widget.update(Text(unable, style="bold") if not success else "")

        message_widget = self.query_one("#agent-load-message", Static)
        message_widget.update(Text(self._render_message(message)))
        message_widget.display = True

        buttons = self.query_one("#agent-load-buttons", HorizontalGroup)
        buttons.display = True

        container = self.query_one("#agent-load-container", VerticalGroup)
        title = (
            self._render_message(_RESULT_AGENT_LOADED.bind()) if success else self._render_message(self._title_message)
        )
        container.border_title = Text(title)

        self.add_class("-success" if success else "-error")
        self.query_one("#agent-load-ok", Button).focus()

    @on(Button.Pressed, "#agent-load-ok")
    def _on_ok(self, event: Button.Pressed) -> None:
        event.stop()
        self.dismiss(None)

    def action_dismiss_if_allowed(self) -> None:
        if self._resolved and self._esc_allowed:
            self.dismiss(None)

    def _allow_click_outside_dismiss(self) -> bool:
        return self._resolved and self._esc_allowed
