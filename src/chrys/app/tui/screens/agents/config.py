# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""AgentsConfigScreen — unified agent configuration modal with sidebar and tabbed config."""

from __future__ import annotations

import contextlib
import copy
import logging
import re
import uuid
from collections.abc import Callable, Collection
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar

from rich.text import Text
from textual import on
from textual.containers import Horizontal, Vertical
from textual.content import Content
from textual.css.query import NoMatches
from textual.widgets import Button, OptionList, Select, Static, TabbedContent, TabPane, TextArea
from textual.widgets.option_list import Option, OptionDoesNotExist

from chrys.app.tui.binding_display import CANCEL_BINDING, localized_binding
from chrys.app.tui.i18n import render_str, widget_locale_controller, widget_localizer
from chrys.app.tui.screens.agents.agent_draft_store import AgentDraftStore, mcp_draft_errors_by_server
from chrys.app.tui.screens.agents.validation_messages import FIX_VALIDATION_ERRORS
from chrys.app.tui.screens.dialogs.base import BaseDialog
from chrys.app.tui.widgets import Checkbox
from chrys.app.tui.widgets import EnhancedInput as Input
from chrys.app.tui.widgets.config_card import CardListChanged
from chrys.foundation.i18n import DisplayBlock, MessageDef, MessageRef, msg

if TYPE_CHECKING:
    from textual.app import ComposeResult
    from textual.widget import Widget

    from chrys.app.tui.screens.agents.mcp_choices import MCPChoices
    from chrys.service.profiles.agents.registry import AgentProfileRegistry, AgentProfileRegistrySnapshot
    from chrys.service.profiles.agents.schema import AgentProfile
    from chrys.service.profiles.models.registry import ModelProfileRegistry

logger = logging.getLogger(__name__)

_READ_ONLY_NOTICE = msg(
    "tui.agent_config.read_only_notice",
    fallback="• Agent is running. This page is read-only.",
)

_CONFIGURATION_TITLE = msg("tui.agent_config.title.configuration", fallback="Agent Configuration")
_TAB_BASIC = msg("tui.agent_config.tab.basic", fallback="Basic")
_TAB_INSTRUCTIONS = msg("tui.agent_config.tab.instructions", fallback="Instructions")
_TAB_TOOLS = msg("tui.agent_config.tab.tools", fallback="Tools")
_TAB_SUB_AGENTS = msg("tui.agent_config.tab.sub_agents", fallback="Sub-Agents")
_TAB_SKILLS = msg("tui.agent_config.tab.skills", fallback="Skills")
_TAB_MCP = msg("tui.agent_config.tab.mcp", fallback="MCP")
_TAB_MEMORY = msg("tui.agent_config.tab.memory", fallback="Memory")
_TAB_COMPACTION = msg("tui.agent_config.tab.compaction", fallback="Compaction")
_TAB_ACP_SETTINGS = msg("tui.agent_config.tab.acp_settings", fallback="ACP Settings")
_TAB_ACP_SHORT = msg("tui.agent_config.tab.acp_short", fallback="ACP")
_NEW = msg("tui.agent_config.button.new", fallback="New")
_CLONE = msg("tui.agent_config.button.clone", fallback="Clone")
_DELETE = msg("tui.agent_config.button.delete", fallback="Delete")
_RESET = msg("tui.agent_config.button.reset", fallback="Reset")
_SAVE = msg("tui.agent_config.button.save", fallback="Save")
_CLOSE = msg("tui.agent_config.button.close", fallback="Close")
_MODIFIED_LABEL = msg("tui.agent_config.profile.modified", fallback="{name} (modified)")
_RESET_AGENT_TITLE = msg("tui.agent_config.confirm.reset.title", fallback="Reset Agent")
_DELETE_AGENT_TITLE = msg("tui.agent_config.confirm.delete.title", fallback="Delete Agent")
_RESET_AGENT_MESSAGE = msg(
    "tui.agent_config.confirm.reset.message",
    fallback=(
        'Reset built-in agent\n"{display}" to system defaults?\n\n'
        "Skills, MCP, and Memory settings will be preserved. All other changes will be discarded."
    ),
    multiline=True,
)
_DELETE_AGENT_MESSAGE = msg(
    "tui.agent_config.confirm.delete.message",
    fallback='Delete agent profile\n"{display}"?',
    multiline=True,
)
_TAB_ERROR = msg("tui.agent_config.panel_error.detail", fallback="{tab}: {error}", multiline=True)
_PANELS_STILL_MOUNTING = msg(
    "tui.agent_config.panel_error.mounting",
    fallback="Agent config panels are still mounting:\n{details}",
    multiline=True,
)
_PANELS_READ_ERROR = msg(
    "tui.agent_config.panel_error.read",
    fallback="Could not read tab configs:\n{details}",
    multiline=True,
)

_AGENT_TITLE = msg("tui.agent_config.title.agent", fallback="Agent")
_AGENT_SETTINGS_TITLE = msg("tui.agent_config.title.settings", fallback="Agent Settings")
_VALIDATION_ERROR_TITLE = msg("tui.agent_config.title.validation_error", fallback="Validation Error")
_SAVE_ERROR_TITLE = msg("tui.agent_config.title.save_error", fallback="Save Error")
_LAST_AGENT = msg(
    "tui.agent_config.last_agent",
    fallback="Cannot remove the last agent profile — at least one must remain.",
)
_LAST_MAIN_AGENT = msg(
    "tui.agent_config.last_main_agent",
    fallback="Cannot remove the last main agent profile — add another first.",
)
_AGENT_STILL_REFERENCED = msg(
    "tui.agent_config.still_referenced",
    fallback=(
        "Cannot remove '{name}' — still used as a sub-agent by: {preview}{suffix}. Unlink it from those agents first."
    ),
)
_AGENT_MORE_SUFFIX = msg("tui.agent_config.more_suffix", fallback=" (+{extra_count} more)")
_AGENT_REMOVED = msg("tui.agent_config.removed", fallback="Agent removed")
_AGENT_DELETED = msg("tui.agent_config.deleted", fallback="Agent deleted")
_AGENT_RESET_PENDING = msg(
    "tui.agent_config.reset_pending",
    fallback="Agent reset prepared — save to apply",
)
_AGENT_RESET_COMPLETE = msg(
    "tui.agent_config.reset_complete",
    fallback="Agent reset to system defaults",
)
_AGENT_ALREADY_DEFAULT = msg(
    "tui.agent_config.already_default",
    fallback="Agent already uses the system defaults",
)
_AGENT_PROFILE_SAVED = msg("tui.agent_config.saved", fallback="Agent profile saved")


class _DismissIntent(StrEnum):
    """Modal result to return when the user closes the config screen."""

    NONE = ""
    UPDATED = "updated"
    SWITCHED = "switched"


@dataclass
class _AgentDraft:
    """Modal-local staged state for one agent profile."""

    key: str
    original_name: str | None
    profile: AgentProfile
    original_profile: AgentProfile | None
    is_builtin: bool
    dirty: bool = False
    reset_to_builtin: bool = False


@dataclass
class _AgentDraftSnapshot:
    """Restorable staged draft state for failed save attempts."""

    profile: AgentProfile
    dirty: bool
    reset_to_builtin: bool


@dataclass
class _AgentSaveSnapshot:
    """State needed to roll back a multi-profile save."""

    files: dict[Path, bytes | None]
    registry: AgentProfileRegistrySnapshot
    current_profile: str
    active_profile_name: str


class _AgentConfigPanelsNotReady(RuntimeError):
    """Raised when a panel exists but its child widgets have not mounted yet."""


class AgentsConfigScreen(BaseDialog[str]):
    """Unified agent configuration modal.

    Left sidebar lists all agent profiles; selecting one loads its
    configuration in the tabbed right panel.

    Tabs: Basic, Instructions, Tools, Sub-Agents, Skills, MCP, Compaction.

    Dismiss result:
      - ``""`` — user cancelled, nothing to reload.
      - ``"updated"`` — user saved; caller should reload the registry.
      - ``"switched"`` — active agent profile changed (e.g. deleted), caller
        should reload and switch the engine to the new active profile.
    """

    CSS_PATH = "config.tcss"
    _HYDRATION_RETRY_SECONDS: ClassVar[float] = 0.02
    _HYDRATION_MAX_ATTEMPTS: ClassVar[int] = 25
    _TAB_IDS: ClassVar[tuple[str, ...]] = (
        "basic",
        "instructions",
        "tools",
        "sub-agents",
        "skills",
        "mcp",
        "memory",
        "compaction",
        "acp",
    )

    BINDINGS: ClassVar[list] = [
        localized_binding("escape", "cancel", CANCEL_BINDING, show=False, priority=True),
    ]

    def __init__(
        self,
        registry: AgentProfileRegistry,
        current_profile: str,
        initial_profile: str = "",
        initial_tab: str = "basic",
        *,
        active_profile_name: str = "",
        model_registry: ModelProfileRegistry | None = None,
        active_model_profile_id: str = "",
        workspace_cwd: str | None = None,
        workspace_roots: list[str] | None = None,
        on_saved: Callable[[str | None, str | None], None] | None = None,
        read_only: bool = False,
    ) -> None:
        self._registry = registry
        self._model_registry = model_registry
        self._active_model_profile_id = active_model_profile_id
        self._workspace_cwd = workspace_cwd
        self._workspace_roots = [root for root in workspace_roots or [] if root]
        if not self._workspace_roots and workspace_cwd:
            self._workspace_roots = [workspace_cwd]
        self._read_only = read_only
        self._current_profile = current_profile
        self._initial_profile = initial_profile or current_profile
        self._initial_tab = initial_tab
        self._selected_profile_name: str = ""
        self._selected_draft_key: str = ""
        self._drafts: dict[str, _AgentDraft] = {}
        self._mounted_tabs: set[str] = set()
        # Per draft key: its MCP server cards' folds when it was last shown. The panel is
        # rebuilt from the draft, which carries no UI state, each time the agent loads.
        self._mcp_card_folds: dict[str, list[bool]] = {}
        self._hydrating: bool = False
        self._hydration_preserve_dirty: bool = False
        self._hydrating_generation: int = 0
        self._pending_agent_type_change: tuple[str, str] | None = None
        self._dismiss_intent = _DismissIntent.NONE
        # Resolve the session-active profile's *registry name* (stable
        # identifier — ``display_name`` is freely editable and ``name``
        # is editable for user profiles).  Prefer the caller-supplied
        # ``active_profile_name`` when provided (unambiguous); otherwise
        # fall back to matching ``current_profile`` against ``.name``
        # first and ``display_name`` second so a profile whose display
        # label collides with another profile's registry name still
        # resolves correctly.
        self._original_active_profile_name: str = ""
        if active_profile_name and registry.get(active_profile_name) is not None:
            self._original_active_profile_name = active_profile_name
        else:
            profiles = registry.list_profiles(include_sub_agent_only=True)
            for p in profiles:
                if p.name == current_profile:
                    self._original_active_profile_name = p.name
                    break
            else:
                for p in profiles:
                    if (p.display_name or p.name) == current_profile:
                        self._original_active_profile_name = p.name
                        break
        self._active_profile_name = self._original_active_profile_name
        # Sync callback fired after each successful Save (no dismiss).
        # Args: (new_display_label, new_registry_name).
        # ``new_display_label`` is non-None when the active profile's
        # display label changed (caller refreshes main-screen widgets).
        # ``new_registry_name`` is non-None when the active profile's
        # registry name changed (caller queues an engine switch for
        # modal Close).
        self._on_saved = on_saved
        super().__init__()

    # ── Compose ──────────────────────────────────────────────────────

    def compose(self) -> ComposeResult:
        localizer = widget_localizer(self)
        with Vertical(id="ac-container") as container:
            container.border_title = Text(render_str(localizer, _CONFIGURATION_TITLE.bind()))

            with Horizontal(id="ac-main"):
                # Left sidebar
                with Vertical(id="ac-sidebar"):
                    yield OptionList(id="ac-list")

                # Right panel
                with Vertical(id="ac-right"), TabbedContent(initial=self._initial_tab, id="ac-tabs"):
                    with TabPane(render_str(localizer, _TAB_BASIC.bind()), id="basic"):
                        pass
                    with TabPane(render_str(localizer, _TAB_INSTRUCTIONS.bind()), id="instructions"):
                        pass
                    with TabPane(render_str(localizer, _TAB_TOOLS.bind()), id="tools"):
                        pass
                    with TabPane(render_str(localizer, _TAB_SUB_AGENTS.bind()), id="sub-agents"):
                        pass
                    with TabPane(render_str(localizer, _TAB_SKILLS.bind()), id="skills"):
                        pass
                    with TabPane(render_str(localizer, _TAB_MCP.bind()), id="mcp"):
                        pass
                    with TabPane(render_str(localizer, _TAB_MEMORY.bind()), id="memory"):
                        pass
                    with TabPane(render_str(localizer, _TAB_COMPACTION.bind()), id="compaction"):
                        pass
                    with TabPane(render_str(localizer, _TAB_ACP_SETTINGS.bind()), id="acp"):
                        pass

            # Footer buttons
            with Vertical(id="ac-footer") as footer:
                if self._read_only:
                    footer.add_class("-read-only")
                with Horizontal(id="ac-buttons"):
                    yield Button(render_str(localizer, _NEW.bind()), variant="primary", id="ac-new", flat=True)
                    yield Button(render_str(localizer, _CLONE.bind()), variant="primary", id="ac-clone", flat=True)
                    yield Button(render_str(localizer, _RESET.bind()), variant="warning", id="ac-reset", flat=True)
                    yield Button(render_str(localizer, _DELETE.bind()), variant="error", id="ac-delete", flat=True)
                    yield Static(id="ac-buttons-spacer")
                    save_button = Button(
                        render_str(localizer, _SAVE.bind()), variant="primary", id="ac-save", flat=True
                    )
                    save_button.disabled = True
                    yield save_button
                    yield Button(render_str(localizer, _CLOSE.bind()), variant="warning", id="ac-cancel", flat=True)
                notice = Static(Text(render_str(localizer, _READ_ONLY_NOTICE.bind())), id="ac-read-only-notice")
                notice.display = self._read_only
                yield notice

    # ── Mount ────────────────────────────────────────────────────────

    def on_mount(self) -> None:
        self._initialize_drafts()
        self._populate_sidebar()
        self._select_initial_profile()
        self._update_save_button_state()
        self._apply_read_only_state()

    def on_unmount(self) -> None:
        self._hydrating_generation += 1
        self._hydrating = False
        self._hydration_preserve_dirty = False
        self._pending_agent_type_change = None

    @staticmethod
    def _existing_draft_key(profile_name: str) -> str:
        return f"profile:{profile_name}"

    @staticmethod
    def _new_draft_key() -> str:
        return f"draft:{uuid.uuid4().hex}"

    def _initialize_drafts(self) -> None:
        """Seed modal-local drafts from the current registry state."""
        self._drafts.clear()
        self._mcp_card_folds.clear()
        profiles = self._registry.list_profiles(include_sub_agent_only=True)
        for profile in profiles:
            key = self._existing_draft_key(profile.name)
            original = copy.deepcopy(profile)
            self._canonicalize_profile_for_ui(original)
            draft_profile = copy.deepcopy(profile)
            self._canonicalize_profile_for_ui(draft_profile)
            self._drafts[key] = _AgentDraft(
                key=key,
                original_name=profile.name,
                profile=draft_profile,
                original_profile=original,
                is_builtin=self._registry.is_builtin(profile.name),
            )

    @staticmethod
    def _canonicalize_profile_for_ui(profile) -> None:
        """Match harmless normalizations done by tab panels (ordering)."""
        from chrys.app.tui.screens.agents.panels.tools import _ALL_CATEGORIES

        enabled = set(profile.tools.builtins)
        known = {cat for cat, _label, _desc, _enabled in _ALL_CATEGORIES}
        ordered = [cat for cat, _label, _desc, _enabled in _ALL_CATEGORIES if cat in enabled]
        ordered.extend(cat for cat in profile.tools.builtins if cat not in known)
        profile.tools.builtins = ordered
        profile.skills.script_extensions = sorted(profile.skills.script_extensions)
        profile.name = profile.name.strip()
        profile.display_name = profile.display_name.strip()
        profile.description = profile.description.strip()
        for ref in profile.sub_agents.agents:
            ref.profile = ref.profile.strip()
            ref.tool_name = ref.tool_name.strip()
            ref.tool_description = ref.tool_description.strip()
        profile.memory.files = [path.strip() for path in profile.memory.files if path.strip()]
        profile.memory.folders = [path.strip() for path in profile.memory.folders if path.strip()]
        profile.skills.paths = [path.strip() for path in profile.skills.paths if path.strip()]
        for server in profile.tools.mcp:
            server.name = server.name.strip()
            server.description = server.description.strip()
            server.tool_name_prefix = server.tool_name_prefix.strip()
            server.allowed_tools = (
                [tool.strip() for tool in server.allowed_tools if tool.strip()]
                if server.allowed_tools is not None
                else None
            )
            server.always_load = [tool.strip() for tool in server.always_load if tool.strip()]
            if not server.use_progressive_disclosure or server.allowed_tools == []:
                server.use_progressive_disclosure = False
                server.always_load = []
        # Mirror AcpConfigPanel.get_config exactly (command/cwd/mode/model
        # stripped, mapping keys stripped with empty keys dropped, argument
        # values untouched) so hydration settles and dirty stays honest.
        if profile.acp is not None:
            acp = profile.acp
            acp.command = acp.command.strip()
            acp.cwd = acp.cwd.strip()
            acp.session_mode = acp.session_mode.strip()
            acp.model_id = acp.model_id.strip()
            acp.env = {key.strip(): value for key, value in acp.env.items() if key.strip()}
            acp.config_options = {key.strip(): value for key, value in acp.config_options.items() if key.strip()}

    def _visible_drafts(self) -> list[_AgentDraft]:
        return list(self._drafts.values())

    def _find_visible_draft_key(self, profile_name: str) -> str | None:
        for draft in self._visible_drafts():
            if draft.profile.name == profile_name:
                return draft.key
        return None

    def _populate_sidebar(self) -> None:
        ol = self.query_one("#ac-list", OptionList)
        ol.clear_options()
        drafts = self._visible_drafts()
        for i, draft in enumerate(drafts):
            if i > 0:
                ol.add_option(None)
            profile = draft.profile
            label = profile.display_name or profile.name
            if draft.dirty:
                label = self._render_toast(_MODIFIED_LABEL.bind(name=label))
            is_current = draft.original_name == self._active_profile_name
            prefix = "◦ " if is_current else ""
            content = Content.assemble(
                (f"{prefix}{label}", "bold"),
                (f"\n{profile.description}", "dim") if profile.description else "",
            )
            ol.add_option(Option(content, id=draft.key))

    def _select_initial_profile(self) -> None:
        """Highlight and load the initial profile."""
        drafts = self._visible_drafts()
        if not drafts:
            return

        # Resolve initial profile name from display name first.  If caller
        # supplied a display label that happens to match another profile's
        # registry name, the display-label match is the intended target.
        target = ""
        for draft in drafts:
            profile = draft.profile
            if (profile.display_name or profile.name) == self._initial_profile:
                target = draft.key
                break
        if not target:
            visible_by_name = {draft.profile.name: draft.key for draft in drafts}
            target = visible_by_name.get(self._initial_profile, drafts[0].key)

        ol = self.query_one("#ac-list", OptionList)
        with contextlib.suppress(OptionDoesNotExist):
            ol.highlighted = ol.get_option_index(target)

        self._load_profile(target)

    # ── Profile loading ──────────────────────────────────────────────

    def _load_profile(self, draft_key: str, *, mcp_card_folds_carried: bool = False) -> None:
        """Clear tab panes and mount fresh config panels for the given draft.

        ``mcp_card_folds_carried``: the caller has already set every draft's
        MCP card folds (Save, which rebuilt the drafts), so leave them as set.
        """
        draft = self._drafts.get(draft_key)
        if draft is None:
            return
        pending_agent_type = self._pending_agent_type_change
        if pending_agent_type is not None and pending_agent_type[0] != draft_key:
            self._pending_agent_type_change = None
        if not mcp_card_folds_carried:
            # Also on a reload in place: Reset keeps the MCP servers, and a panel
            # ignores folds that no longer match its server count.
            self._remember_mcp_card_folds()
        profile = draft.profile
        self._selected_draft_key = draft_key
        self._selected_profile_name = profile.name
        is_builtin = draft.is_builtin

        self.query_one("#ac-reset", Button).display = is_builtin
        self.query_one("#ac-delete", Button).display = not is_builtin

        # Clear all panes, then mount only the active tab. Additional tabs are
        # mounted on first activation so the modal can paint without building
        # every editor panel up front.
        self._hydrating = True
        self._hydration_preserve_dirty = False
        self._mounted_tabs.clear()
        self._clear_tab_panes()
        self._configure_tabs_for_profile(profile)
        active_tab = self._active_tab_id()
        if (profile.acp is not None and active_tab not in {"basic", "acp"}) or (
            profile.acp is None and active_tab == "acp"
        ):
            active_tab = "basic"
            self.query_one("#ac-tabs", TabbedContent).active = active_tab
        self._ensure_tab_mounted(active_tab, profile, is_builtin)
        self._schedule_hydration_finish(preserve_dirty=False)
        self._apply_read_only_state()

    def _active_tab_id(self) -> str:
        with contextlib.suppress(NoMatches):
            active = self.query_one("#ac-tabs", TabbedContent).active
            if active in self._TAB_IDS:
                return active
        return self._initial_tab if self._initial_tab in self._TAB_IDS else "basic"

    def _clear_tab_panes(self) -> None:
        for tab_id in self._TAB_IDS:
            self.query_one(f"#{tab_id}", TabPane).remove_children()

    def _configure_tabs_for_profile(self, profile: AgentProfile) -> None:
        tabs = self.query_one("#ac-tabs", TabbedContent)
        builtin_tabs = ("instructions", "tools", "sub-agents", "skills", "mcp", "memory", "compaction")
        if profile.acp is not None:
            for tab_id in builtin_tabs:
                tabs.hide_tab(tab_id)
            tabs.show_tab("acp")
        else:
            for tab_id in builtin_tabs:
                tabs.show_tab(tab_id)
            tabs.hide_tab("acp")

    def _ensure_tab_mounted(self, tab_id: str, profile, is_builtin: bool) -> None:
        if tab_id in self._mounted_tabs:
            return
        if tab_id == "basic":
            self._mount_basic_tab(profile, is_builtin)
        elif tab_id == "instructions":
            self._mount_instructions_tab(profile)
        elif tab_id == "tools":
            self._mount_tools_tab(profile)
        elif tab_id == "sub-agents":
            self._mount_subagents_tab(profile)
        elif tab_id == "skills":
            self._mount_skills_tab(profile)
        elif tab_id == "mcp":
            self._mount_mcp_tab(profile)
        elif tab_id == "memory":
            self._mount_memory_tab(profile)
        elif tab_id == "compaction":
            self._mount_compaction_tab(profile)
        elif tab_id == "acp" and profile.acp is not None:
            self._mount_acp_tab(profile)
        else:
            return
        self._mounted_tabs.add(tab_id)
        self._apply_read_only_state()
        self.call_after_refresh(self._apply_read_only_state)

    def _schedule_hydration_finish(self, *, preserve_dirty: bool) -> None:
        self._hydrating = True
        self._hydration_preserve_dirty = self._hydration_preserve_dirty or preserve_dirty
        self._hydrating_generation += 1
        generation = self._hydrating_generation
        self.set_timer(self._HYDRATION_RETRY_SECONDS, lambda: self._finish_hydrating(generation, 0))

    def _dirty_after_hydration(self, draft: _AgentDraft) -> bool:
        if self._read_only:
            return False
        return True if self._hydration_preserve_dirty else self._is_draft_dirty(draft)

    def _finish_hydrating(self, generation: int, attempts: int) -> None:
        if generation != self._hydrating_generation or not self.is_attached:
            return
        pending_agent_type = self._pending_agent_type_change
        if pending_agent_type is not None and pending_agent_type[0] == self._selected_draft_key:
            self.call_after_refresh(lambda: self._complete_hydration(generation))
            return
        draft = self._drafts.get(self._selected_draft_key)
        if draft is not None:
            next_attempt = attempts + 1
            try:
                mounted_profile = self._build_profile_from_mounted_panels(draft)
                if mounted_profile != draft.profile:
                    if next_attempt < self._HYDRATION_MAX_ATTEMPTS:
                        self.set_timer(
                            self._HYDRATION_RETRY_SECONDS,
                            lambda: self._finish_hydrating(generation, next_attempt),
                        )
                        return
                    logger.warning(
                        "Agent config hydration did not settle for profile %r after %s attempts",
                        draft.profile.name,
                        next_attempt,
                    )
                else:
                    self._set_draft_dirty(draft, self._dirty_after_hydration(draft))
            except _AgentConfigPanelsNotReady as exc:
                if next_attempt < self._HYDRATION_MAX_ATTEMPTS:
                    self.set_timer(
                        self._HYDRATION_RETRY_SECONDS,
                        lambda: self._finish_hydrating(generation, next_attempt),
                    )
                    return
                logger.warning(
                    "Agent config hydration timed out for profile %r after %s attempts: %s",
                    draft.profile.name,
                    next_attempt,
                    exc,
                )
            except Exception:
                if next_attempt < self._HYDRATION_MAX_ATTEMPTS:
                    self.set_timer(
                        self._HYDRATION_RETRY_SECONDS,
                        lambda: self._finish_hydrating(generation, next_attempt),
                    )
                    return
                logger.warning(
                    "Agent config hydration failed for profile %r after %s attempts",
                    draft.profile.name,
                    next_attempt,
                    exc_info=True,
                )
        # Clear the hydration guard only after the next refresh. Field-change
        # events (e.g. a sub-agent Select.Changed) queued while the panels were
        # mounting are still in the message queue; draining them before the guard
        # drops keeps _mark_selected_dirty gated for that settle traffic. Clearing
        # synchronously let those queued events land after the guard cleared and
        # force a spurious, sticky dirty that wrongly enabled Save -- an
        # intermittent CI failure on the dirty-tracking tests.
        self.call_after_refresh(lambda: self._complete_hydration(generation))

    def _complete_hydration(self, generation: int) -> None:
        if generation != self._hydrating_generation or not self.is_attached:
            return
        preserve_dirty = self._hydration_preserve_dirty
        self._hydrating = False
        self._hydration_preserve_dirty = False
        draft = self._drafts.get(self._selected_draft_key)
        if draft is not None and not self._read_only:
            self._set_draft_dirty(draft, True if preserve_dirty else self._is_draft_dirty(draft))
        self._update_save_button_state()
        self._apply_read_only_state()
        pending_agent_type = self._pending_agent_type_change
        self._pending_agent_type_change = None
        if pending_agent_type is not None and pending_agent_type[0] == self._selected_draft_key:
            self._apply_agent_type_change(*pending_agent_type)

    def _mount_basic_tab(self, profile, is_builtin: bool) -> None:
        from chrys.app.tui.screens.agents.panels.basic import BasicConfigPanel

        pane = self.query_one("#basic", TabPane)
        pane.remove_children()
        pane.mount(
            BasicConfigPanel(
                profile,
                is_builtin=is_builtin,
                model_registry=self._model_registry,
                active_profile_id=self._active_model_profile_id,
                read_only=self._read_only,
            )
        )

    def _mount_instructions_tab(self, profile) -> None:
        from chrys.app.tui.screens.agents.panels.instructions import InstructionsConfigPanel

        pane = self.query_one("#instructions", TabPane)
        pane.remove_children()
        pane.mount(InstructionsConfigPanel(profile.instructions))

    def _mount_tools_tab(self, profile) -> None:
        from chrys.app.tui.screens.agents.panels.tools import ToolsConfigPanel

        pane = self.query_one("#tools", TabPane)
        pane.remove_children()
        pane.mount(ToolsConfigPanel(profile.tools, read_only=self._read_only))

    def _mount_subagents_tab(self, profile) -> None:
        from chrys.app.tui.screens.agents.panels.subagents import SubAgentsConfigPanel

        pane = self.query_one("#sub-agents", TabPane)
        pane.remove_children()
        pane.mount(
            SubAgentsConfigPanel(
                profile.sub_agents,
                registry=self._registry,
                current_profile_name=profile.name,
                available_profiles=self._sub_agent_available_profiles(),
                read_only=self._read_only,
            )
        )

    def _sub_agent_available_profiles(self) -> list[AgentProfile]:
        profiles: list[AgentProfile] = []
        seen_names: set[str] = set()

        def add(profile: AgentProfile) -> None:
            if not profile.name or profile.name in seen_names:
                return
            seen_names.add(profile.name)
            profiles.append(profile)

        visible_drafts = self._visible_drafts()
        represented_originals = {draft.original_name for draft in visible_drafts if draft.original_name is not None}
        for draft in visible_drafts:
            if draft.key == self._selected_draft_key:
                continue
            if draft.original_profile is not None and draft.original_name != draft.profile.name:
                add(draft.original_profile)
        for draft in visible_drafts:
            add(draft.profile)
        for profile in self._registry.list_profiles(include_sub_agent_only=True):
            if profile.name in represented_originals:
                continue
            add(profile)
        return profiles

    def _mount_skills_tab(self, profile) -> None:
        from chrys.app.tui.screens.agents.panels.skills import SkillsConfigPanel

        pane = self.query_one("#skills", TabPane)
        pane.remove_children()
        pane.mount(SkillsConfigPanel(profile.skills, workspace_cwd=self._workspace_cwd, read_only=self._read_only))

    def _remember_mcp_card_folds(self) -> None:
        """Keep the selected draft's MCP card folds for the next time it loads."""
        from chrys.app.tui.screens.agents.panels.mcp import MCPConfigPanel

        key = self._selected_draft_key
        if key not in self._drafts or "mcp" not in self._mounted_tabs:
            return
        with contextlib.suppress(NoMatches):
            self._mcp_card_folds[key] = self._live_panel(MCPConfigPanel).card_folds()

    def _mount_mcp_tab(self, profile) -> None:
        from chrys.app.tui.screens.agents.panels.mcp import MCPConfigPanel

        pane = self.query_one("#mcp", TabPane)
        pane.remove_children()
        pane.mount(
            MCPConfigPanel(
                profile.tools.mcp,
                card_folds=self._mcp_card_folds.get(self._selected_draft_key),
                workspace_cwd=self._workspace_cwd,
                read_only=self._read_only,
                additional_reserved_tool_names=self._selected_sub_agent_tool_names,
                reuse_choices=self._mcp_reuse_choices,
            )
        )

    def _mcp_reuse_choices(self) -> MCPChoices:
        """Offer the latest other drafts, including unsaved and sub-agent-only profiles."""
        from chrys.app.tui.screens.agents.mcp_choices import collect_mcp_choices

        return collect_mcp_choices(
            draft.profile for draft in self._visible_drafts() if draft.key != self._selected_draft_key
        )

    def _selected_sub_agent_tool_names(self) -> set[str]:
        """Return live sub-agent names reserved by the selected draft."""
        draft = self._drafts.get(self._selected_draft_key)
        if draft is None:
            return set()
        return {tool_name for ref in draft.profile.sub_agents.agents if (tool_name := ref.tool_name or ref.profile)}

    def _mount_memory_tab(self, profile) -> None:
        from chrys.app.tui.screens.agents.panels.memory import MemoryConfigPanel

        pane = self.query_one("#memory", TabPane)
        pane.remove_children()
        pane.mount(MemoryConfigPanel(profile.memory, workspace_cwd=self._workspace_cwd, read_only=self._read_only))

    def _mount_compaction_tab(self, profile) -> None:
        from chrys.app.tui.screens.agents.panels.compaction import CompactionConfigPanel

        pane = self.query_one("#compaction", TabPane)
        pane.remove_children()
        pane.mount(CompactionConfigPanel(profile.compaction))

    def _mount_acp_tab(self, profile) -> None:
        from chrys.app.tui.screens.agents.panels.acp import AcpConfigPanel

        if profile.acp is None:
            return
        pane = self.query_one("#acp", TabPane)
        pane.remove_children()
        pane.mount(
            AcpConfigPanel(
                profile.acp,
                workspace_cwd=self._workspace_cwd,
                workspace_roots=self._workspace_roots,
                read_only=self._read_only,
            )
        )

    # ── Sidebar selection ────────────────────────────────────────────

    @on(OptionList.OptionSelected, "#ac-list")
    def _on_profile_selected(self, event: OptionList.OptionSelected) -> None:
        target_key = event.option.id
        if target_key and target_key != self._selected_draft_key:
            if not self._read_only:
                try:
                    self._sync_selected_draft_from_panels()
                except Exception as exc:
                    self.notify(
                        str(exc),
                        title=self._render_toast(_AGENT_TITLE.bind()),
                        severity="error",
                        timeout=5,
                        markup=False,
                    )
                    self._restore_sidebar_highlight()
                    return
                self._refresh_sidebar_preserving_selection()
            ol = self.query_one("#ac-list", OptionList)
            with contextlib.suppress(OptionDoesNotExist):
                ol.highlighted = ol.get_option_index(target_key)
            self._load_profile(target_key)

    def _restore_sidebar_highlight(self) -> None:
        if not self._selected_draft_key:
            return
        ol = self.query_one("#ac-list", OptionList)
        with contextlib.suppress(OptionDoesNotExist):
            ol.highlighted = ol.get_option_index(self._selected_draft_key)

    def _refresh_sidebar_preserving_selection(self) -> None:
        self._populate_sidebar()
        self._restore_sidebar_highlight()

    @on(TabbedContent.TabActivated, "#ac-tabs")
    def _on_tab_activated(self, event: TabbedContent.TabActivated) -> None:
        tab_id = event.pane.id or ""
        if tab_id not in self._TAB_IDS or not self._selected_draft_key:
            return
        draft = self._drafts.get(self._selected_draft_key)
        if draft is None:
            return
        tab_mounted = tab_id in self._mounted_tabs
        if not self._hydrating and not self._read_only:
            self._mark_selected_dirty(force=True)
        if not tab_mounted:
            preserve_dirty = draft.dirty
            self._hydrating = True
            self._ensure_tab_mounted(tab_id, draft.profile, draft.is_builtin)
            self._schedule_hydration_finish(preserve_dirty=preserve_dirty)
        self._apply_read_only_state()

    def _update_save_button_state(self) -> None:
        if not self.is_attached:
            return
        try:
            save = self.query_one("#ac-save", Button)
        except NoMatches:
            return
        if self._read_only:
            save.disabled = True
            save.display = False
            return
        dirty = any(draft.dirty for draft in self._drafts.values())
        save.disabled = not dirty

    @staticmethod
    def _is_read_only_hidden_button(button_id: str) -> bool:
        if button_id in {
            "ac-new",
            "ac-clone",
            "ac-reset",
            "ac-delete",
            "ac-save",
            "sa-add-btn",
            "mem-add-file",
            "mem-add-folder",
            "sk-add-btn",
            "mcp-add-btn",
            "acp-add-arg",
            "acp-add-env",
            "acp-add-option",
            "acp-test",
        }:
            return True
        return button_id.startswith(
            (
                "mcp-hadd-",
                "mcp-eadd-",
                "mcp-hdel-",
                "mcp-edel-",
                "mcp-delete-btn-",
                "mcp-test-btn-",
                "acp-",
                "sa-delete-btn-",
                "mem-delete-btn-",
                "mem-file-browse-",
                "mem-folder-browse-",
                "sk-browse-",
                "sk-delete-btn-",
            )
        )

    def _apply_read_only_state(self) -> None:
        """Make mounted profile controls view-only while preserving navigation."""
        if not self._read_only or not self.is_attached:
            return
        try:
            self.query_one("#ac-footer", Vertical).add_class("-read-only")
            self.query_one("#ac-read-only-notice", Static).display = True
        except NoMatches:
            return
        for input_widget in self.query(Input):
            input_widget.disabled = True
        for select in self.query(Select):
            select.disabled = True
        for checkbox in self.query(Checkbox):
            checkbox.disabled = True
        for text_area in self.query(TextArea):
            text_area.read_only = True
        for button in self.query(Button):
            button_id = button.id or ""
            if button_id == "ac-cancel":
                button.disabled = False
                button.display = True
                continue
            button.disabled = True
            if self._is_read_only_hidden_button(button_id):
                button.display = False

    def _set_dismiss_intent(self, intent: _DismissIntent) -> None:
        if self._dismiss_intent == _DismissIntent.SWITCHED or intent == _DismissIntent.NONE:
            return
        self._dismiss_intent = intent

    def _is_draft_dirty(self, draft: _AgentDraft) -> bool:
        if draft.original_profile is None:
            return True
        return draft.reset_to_builtin or draft.profile != draft.original_profile

    def _set_draft_dirty(self, draft: _AgentDraft, dirty: bool) -> None:
        if draft.dirty != dirty:
            draft.dirty = dirty
            self._refresh_sidebar_preserving_selection()
        self._update_save_button_state()

    def _snapshot_drafts(self) -> dict[str, _AgentDraftSnapshot]:
        return {
            key: _AgentDraftSnapshot(
                profile=copy.deepcopy(draft.profile),
                dirty=draft.dirty,
                reset_to_builtin=draft.reset_to_builtin,
            )
            for key, draft in self._drafts.items()
        }

    def _restore_drafts(self, snapshot: dict[str, _AgentDraftSnapshot]) -> None:
        for key, state in snapshot.items():
            draft = self._drafts.get(key)
            if draft is None:
                continue
            draft.profile = copy.deepcopy(state.profile)
            draft.dirty = state.dirty
            draft.reset_to_builtin = state.reset_to_builtin
        selected = self._drafts.get(self._selected_draft_key)
        if selected is not None:
            self._selected_profile_name = selected.profile.name
        self._refresh_sidebar_preserving_selection()
        self._update_save_button_state()

    def _mounted_panel_validation_errors(self) -> list[str]:
        return self._validate_all()

    def _mounted_panels_have_validation_errors(self) -> bool:
        return bool(self._mounted_panel_validation_errors())

    def _mark_selected_dirty(self, *, force: bool = False, optimistic: bool = False) -> None:
        if self._read_only:
            return
        if self._hydrating or not self._selected_draft_key:
            return
        draft = self._drafts.get(self._selected_draft_key)
        if draft is None:
            return
        if optimistic:
            self._set_draft_dirty(draft, True)
            return
        try:
            draft.profile = self._build_profile_from_mounted_panels(draft)
        except _AgentConfigPanelsNotReady:
            # Panels are still mounting/settling — a transient read must not flip
            # the dirty flag. A field-change event (e.g. a sub-agent Select.Changed)
            # firing in the post-hydration settle window, after _hydrating clears
            # but before the panels are queryable, would otherwise force a spurious
            # dirty that sticks and wrongly enables Save. Leave dirty untouched; the
            # next settled comparison (or a real edit) sets it correctly.
            self._update_save_button_state()
            return
        except Exception:
            if force:
                self._set_draft_dirty(draft, True)
            else:
                logger.debug("Could not compare mounted agent config to draft", exc_info=True)
                self._update_save_button_state()
            return
        dirty = self._is_draft_dirty(draft)
        if not dirty and (force or draft.dirty):
            dirty = self._mounted_panels_have_validation_errors()
        self._set_draft_dirty(draft, dirty)

    @on(Input.Changed)
    @on(Checkbox.Changed)
    @on(Select.Changed)
    @on(TextArea.Changed)
    def _on_config_field_changed(self, event: object) -> None:
        if isinstance(event, Select.Changed) and event.select.id == "bc-agent-type":
            # The discriminator handler must sanitize hidden sections before
            # any generic harvest can install ``acp`` and make the switch
            # appear already complete.
            return
        self._mark_selected_dirty(force=True)

    @on(Select.Changed, "#bc-agent-type")
    def _on_agent_type_changed(self, event: Select.Changed) -> None:
        """Apply the destructive ACP discriminator switch after its inline warning."""
        if self._read_only or event.value not in {"builtin", "acp"}:
            return
        draft = self._drafts.get(self._selected_draft_key)
        if draft is None or draft.is_builtin:
            return
        agent_type = str(event.value)
        wants_acp = agent_type == "acp"
        if wants_acp == (draft.profile.acp is not None):
            pending_agent_type = self._pending_agent_type_change
            if self._hydrating and pending_agent_type is not None and pending_agent_type[0] == draft.key:
                self._pending_agent_type_change = None
            return
        if self._hydrating:
            self._pending_agent_type_change = (draft.key, agent_type)
            return
        self._apply_agent_type_change(draft.key, agent_type)

    def _apply_agent_type_change(self, draft_key: str, agent_type: str) -> None:
        if self._read_only or draft_key != self._selected_draft_key:
            return
        draft = self._drafts.get(draft_key)
        if draft is None or draft.is_builtin:
            return
        wants_acp = agent_type == "acp"
        if wants_acp == (draft.profile.acp is not None):
            return
        from chrys.app.tui.screens.agents.panels.basic import BasicConfigPanel

        basic = self._live_panel(BasicConfigPanel).get_config()
        self._apply_basic_to_profile(draft.profile, basic, is_builtin=False)
        if wants_acp:
            self._sanitize_profile_for_acp(draft.profile)
        else:
            draft.profile.acp = None
        self._canonicalize_profile_for_ui(draft.profile)
        self._set_draft_dirty(draft, True)
        self._selected_profile_name = draft.profile.name
        self._load_profile(draft.key)

    @staticmethod
    def _sanitize_profile_for_acp(profile: AgentProfile) -> None:
        from chrys.service.profiles.agents.schema import (
            AcpAgentConfig,
            ApprovalConfig,
            CompactionConfig,
            MemoryConfig,
            ModelConfig,
            SkillsConfig,
            SubAgentsConfig,
            ToolsConfig,
        )

        profile.acp = profile.acp or AcpAgentConfig()
        profile.sub_agent_only = True
        profile.instructions = ""
        profile.tools = ToolsConfig()
        profile.skills = SkillsConfig()
        profile.sub_agents = SubAgentsConfig()
        profile.memory = MemoryConfig()
        profile.compaction = CompactionConfig()
        profile.approval = ApprovalConfig()
        profile.model = ModelConfig()

    @on(Button.Pressed)
    def _on_config_structure_button_pressed(self, event: Button.Pressed) -> None:
        if self._read_only:
            return
        button_id = event.button.id or ""
        if not self._is_config_mutation_button(button_id):
            return
        self._mark_structure_changed()

    @on(CardListChanged)
    def _on_card_list_changed(self, _event: CardListChanged) -> None:
        if self._read_only:
            return
        self._mark_structure_changed()

    def _mark_structure_changed(self) -> None:
        # The panel rebuilds its rows asynchronously: mark dirty now, then re-read once it has settled.
        self._mark_selected_dirty(force=True, optimistic=True)
        self.call_after_refresh(lambda: self._mark_selected_dirty(force=True))

    @staticmethod
    def _is_config_mutation_button(button_id: str) -> bool:
        # MCP add/delete presses may only open a dialog; CardListChanged marks
        # the draft dirty once an actual insertion or confirmed deletion lands.
        if button_id in {
            "sa-add-btn",
            "mem-add-file",
            "mem-add-folder",
            "sk-add-btn",
            "acp-add-arg",
            "acp-add-env",
            "acp-add-option",
        }:
            return True
        return button_id.startswith(
            (
                "sk-ext-",
                "sk-browse-",
                "mcp-hadd-",
                "mcp-eadd-",
                "mcp-hdel-",
                "mcp-edel-",
                "acp-row-delete-",
                "sa-delete-btn-",
                "mem-delete-btn-",
                "sk-delete-btn-",
            )
        )

    # ── Buttons ──────────────────────────────────────────────────────

    @on(Button.Pressed, "#ac-new")
    def _on_new(self, _event: Button.Pressed) -> None:
        """Stage a new blank profile and select it."""
        if self._read_only:
            return
        from chrys.service.profiles.agents.schema import AgentProfile

        try:
            self._sync_selected_draft_from_panels()
        except Exception as exc:
            self.notify(
                str(exc),
                title=self._render_toast(_AGENT_TITLE.bind()),
                severity="error",
                timeout=5,
                markup=False,
            )
            return

        # Generate a unique name
        base = "new-agent"
        name = base
        counter = 1
        while self._draft_name_exists(name):
            counter += 1
            name = f"{base}-{counter}"

        profile = AgentProfile(
            name=name,
            id=self._next_profile_id(),
            display_name=name,
            description="A custom agent profile",
            instructions="You are a helpful AI assistant. Follow the user's instructions carefully and provide clear, concise responses.",
        )
        self._canonicalize_profile_for_ui(profile)
        key = self._new_draft_key()
        self._drafts[key] = _AgentDraft(
            key=key,
            original_name=None,
            profile=profile,
            original_profile=None,
            is_builtin=False,
            dirty=True,
        )

        self._refresh_sidebar_preserving_selection()
        ol = self.query_one("#ac-list", OptionList)
        with contextlib.suppress(OptionDoesNotExist):
            ol.highlighted = ol.get_option_index(key)
        self._load_profile(key)
        self._update_save_button_state()

    def _draft_name_exists(self, name: str) -> bool:
        lowered = name.casefold()
        return lowered in {reserved.casefold() for reserved in self._reserved_profile_names()}

    def _reserved_profile_names(self) -> set[str]:
        """Names unavailable for generated profiles across drafts and registry entries."""
        names = set(self._registry.list_names())
        names.update(draft.profile.name for draft in self._drafts.values() if draft.profile.name)
        return names

    def _next_profile_id(self) -> str:
        """Generate a fresh agent profile id."""
        existing_ids = {
            profile.id for profile in self._registry.list_profiles(include_sub_agent_only=True) if profile.id
        }
        existing_ids.update(draft.profile.id for draft in self._drafts.values() if draft.profile.id)
        while True:
            profile_id = uuid.uuid4().hex[:12]
            if profile_id not in existing_ids:
                return profile_id

    @staticmethod
    def _clone_name_parts(name: str) -> tuple[str, int] | None:
        """Return ``(root, suffix_index)`` for generated clone names."""
        match = re.fullmatch(r"(.+?)-copy(?:-(\d+))?", name, flags=re.IGNORECASE)
        if match is None:
            return None
        suffix = match.group(2)
        return match.group(1), int(suffix) if suffix is not None else 1

    def _clone_name_indices(self, root_name: str) -> list[int]:
        """Return existing generated clone suffixes for the given root."""
        indices: list[int] = []
        for name in self._reserved_profile_names():
            parts = self._clone_name_parts(name)
            if parts is not None and parts[0].casefold() == root_name.casefold():
                indices.append(parts[1])
        return indices

    @staticmethod
    def _clone_display_root(source_display_name: str) -> str:
        """Return the original display root for a generated clone label."""
        match = re.fullmatch(r"(.+?) copy(?: \d+)?", source_display_name, flags=re.IGNORECASE)
        return source_display_name if match is None else match.group(1)

    def _next_clone_names(self, source_name: str, source_display_name: str) -> tuple[str, str]:
        """Generate unique registry and display names for a cloned agent profile."""
        existing_names = {name.casefold() for name in self._reserved_profile_names()}
        source_parts = self._clone_name_parts(source_name)
        if source_parts is None:
            root_name = source_name
            counter = 1
        else:
            root_name, source_index = source_parts
            source_display_name = self._clone_display_root(source_display_name)
            counter = max([source_index, *self._clone_name_indices(root_name)]) + 1
        base_name = f"{root_name}-copy"
        base_display_name = f"{source_display_name} Copy"
        name = base_name if counter == 1 else f"{base_name}-{counter}"
        display_name = base_display_name if counter == 1 else f"{base_display_name} {counter}"
        while name.casefold() in existing_names:
            counter += 1
            name = f"{base_name}-{counter}"
            display_name = f"{base_display_name} {counter}"
        return name, display_name

    @on(Button.Pressed, "#ac-clone")
    def _on_clone(self, _event: Button.Pressed) -> None:
        """Stage a copy of the selected profile and select it."""
        if self._read_only:
            return
        try:
            self._sync_selected_draft_from_panels()
        except Exception as exc:
            self.notify(
                str(exc),
                title=self._render_toast(_AGENT_TITLE.bind()),
                severity="error",
                timeout=5,
                markup=False,
            )
            return

        source_draft = self._drafts.get(self._selected_draft_key)
        if source_draft is None:
            return

        source = source_draft.profile
        duplicate = copy.deepcopy(source)
        duplicate.name, duplicate.display_name = self._next_clone_names(source.name, source.display_name or source.name)
        duplicate.id = self._next_profile_id()
        self._canonicalize_profile_for_ui(duplicate)
        key = self._new_draft_key()
        self._drafts[key] = _AgentDraft(
            key=key,
            original_name=None,
            profile=duplicate,
            original_profile=None,
            is_builtin=False,
            dirty=True,
        )
        # The copy has the same MCP servers: open their cards as the original shows them.
        self._remember_mcp_card_folds()
        if (folds := self._mcp_card_folds.get(source_draft.key)) is not None:
            self._mcp_card_folds[key] = list(folds)

        self._refresh_sidebar_preserving_selection()
        ol = self.query_one("#ac-list", OptionList)
        with contextlib.suppress(OptionDoesNotExist):
            ol.highlighted = ol.get_option_index(key)
        self._load_profile(key)
        self._update_save_button_state()

    @on(Button.Pressed, "#ac-reset")
    def _on_reset(self, _event: Button.Pressed) -> None:
        """Stage a built-in profile reset after confirmation."""
        if self._read_only:
            return
        try:
            self._sync_selected_draft_from_panels()
        except Exception as exc:
            self.notify(
                str(exc),
                title=self._render_toast(_AGENT_TITLE.bind()),
                severity="error",
                timeout=5,
                markup=False,
            )
            return
        draft_key = self._selected_draft_key
        draft = self._drafts.get(draft_key)
        if draft is None or not draft.is_builtin or draft.original_name is None:
            return
        display = draft.profile.display_name or draft.profile.name
        from chrys.app.tui.screens.dialogs.confirm import ConfirmDialog

        dialog = ConfirmDialog(
            title=_RESET_AGENT_TITLE.bind(),
            message=_RESET_AGENT_MESSAGE.bind(display=display),
            confirm_label=_RESET.bind(),
            confirm_variant="primary",
            locale_controller=widget_locale_controller(self),
        )
        self.app.push_screen(
            dialog,
            callback=lambda confirmed: self._do_reset(draft_key) if confirmed else None,
        )

    def _do_reset(self, draft_key: str) -> None:
        """Replace one built-in draft with its reset candidate."""
        if self._read_only:
            return
        draft = self._drafts.get(draft_key)
        if draft is None or not draft.is_builtin or draft.original_name is None:
            return
        reset_profile = self._registry.build_builtin_reset(
            draft.original_name,
            preserve_from=draft.profile,
        )
        self._canonicalize_profile_for_ui(reset_profile)
        draft_changed = reset_profile != draft.profile
        shadow_exists = self._profile_yaml_path(draft.original_name).is_file()
        original = draft.original_profile
        reset_matches_template = self._profile_matches_builtin_template(reset_profile, draft.original_name)
        needs_save = reset_profile != original or (shadow_exists and reset_matches_template)
        draft.profile = reset_profile
        draft.reset_to_builtin = needs_save
        self._selected_profile_name = reset_profile.name
        self._set_draft_dirty(draft, needs_save)
        self._load_profile(draft_key)
        message = (
            _AGENT_RESET_PENDING if needs_save else _AGENT_RESET_COMPLETE if draft_changed else _AGENT_ALREADY_DEFAULT
        )
        self.notify(
            self._render_toast(message.bind()),
            title=self._render_toast(_AGENT_SETTINGS_TITLE.bind()),
            severity="information",
            timeout=3,
            markup=False,
        )

    @on(Button.Pressed, "#ac-delete")
    def _on_delete(self, _event: Button.Pressed) -> None:
        """Delete the selected custom profile after confirmation."""
        if self._read_only:
            return
        draft_key = self._selected_draft_key
        draft = self._drafts.get(draft_key)
        if draft is None or draft.is_builtin:
            return
        name = draft.profile.name
        # Block deleting the last profile overall.  An
        # empty registry would leave the main screen's input-bar tag
        # bound to a missing profile (no label, no description) and
        # give the engine nothing to fall back to on ``SettingsReload``.
        remaining_visible = self._remaining_visible_after_delete(draft)
        if not remaining_visible:
            self.notify(
                self._render_toast(_LAST_AGENT.bind()),
                title=self._render_toast(_AGENT_TITLE.bind()),
                severity="warning",
                timeout=5,
                markup=False,
            )
            return
        # Block deleting the last main-eligible profile (i.e.
        # one that can serve as the active main agent — not
        # ``sub_agent_only``).  The engine must always be bound to a
        # real main-agent profile; sub-agent-only profiles cannot fill
        # that slot.  Sub-agent-only profiles themselves may be freely
        # deleted (having zero sub-agents is a valid state) — as long
        # as the general guard above still permits it.
        target = draft.original_profile or draft.profile
        if not target.sub_agent_only:
            # ``list_profiles()`` excludes ``sub_agent_only`` by default.
            remaining_main = [p for p in remaining_visible if not p.sub_agent_only]
            if not remaining_main:
                self.notify(
                    self._render_toast(_LAST_MAIN_AGENT.bind()),
                    title=self._render_toast(_AGENT_TITLE.bind()),
                    severity="warning",
                    timeout=5,
                    markup=False,
                )
                return

        # Block deleting an agent that is still referenced as a
        # sub-agent by other profiles.  Without this guard the registry
        # would silently strip the reference from every parent profile
        # and re-save them — a dangerous action to take without the
        # user's explicit consent.  Make the user unlink manually first.
        target_names = {target.name}
        if draft.original_name is not None:
            target_names.add(draft.original_name)
        referencing = self._referencing_profiles_for_delete(draft, target_names)
        if referencing:
            preview = ", ".join(referencing[:3])
            suffix = (
                self._render_toast(_AGENT_MORE_SUFFIX.bind(extra_count=len(referencing) - 3))
                if len(referencing) > 3
                else ""
            )
            self.notify(
                self._render_toast(_AGENT_STILL_REFERENCED.bind(name=name, preview=preview, suffix=suffix)),
                title=self._render_toast(_AGENT_TITLE.bind()),
                severity="warning",
                timeout=6,
                markup=False,
            )
            return
        display = draft.profile.display_name or name
        from chrys.app.tui.screens.dialogs.confirm import ConfirmDialog

        dialog = ConfirmDialog(
            title=_DELETE_AGENT_TITLE.bind(),
            message=_DELETE_AGENT_MESSAGE.bind(display=display),
            confirm_label=_DELETE.bind(),
            confirm_variant="error",
            locale_controller=widget_locale_controller(self),
        )
        self.app.push_screen(
            dialog,
            callback=lambda confirmed: self._do_delete(draft_key) if confirmed else None,
        )

    def _referencing_profiles_for_delete(self, draft: _AgentDraft, target_names: set[str]) -> list[str]:
        referencing: list[str] = []
        seen_names: set[str] = set()

        def add_if_referencing(profile_name: str, display_name: str, refs) -> None:
            if profile_name in target_names or profile_name in seen_names:
                return
            if any(ref.profile in target_names for ref in refs):
                seen_names.add(profile_name)
                referencing.append(display_name or profile_name)

        for other in self._visible_drafts():
            if other.key == draft.key:
                continue
            add_if_referencing(other.profile.name, other.profile.display_name, other.profile.sub_agents.agents)

        represented_originals = {
            other.original_name for other in self._visible_drafts() if other.original_name is not None
        }
        for profile in self._registry.list_profiles(include_sub_agent_only=True):
            if profile.name in represented_originals:
                continue
            add_if_referencing(profile.name, profile.display_name, profile.sub_agents.agents)

        return referencing

    def _remaining_visible_after_delete(self, draft: _AgentDraft) -> list[AgentProfile]:
        if draft.original_name is None:
            return [d.profile for d in self._visible_drafts() if d.key != draft.key]
        return [
            profile
            for profile in self._registry.list_profiles(include_sub_agent_only=True)
            if profile.name != draft.original_name
        ]

    def _do_delete(self, draft_key: str) -> None:
        """Delete the profile immediately after user confirmation."""
        if self._read_only:
            return
        from chrys.service.profiles.agents.serializer import delete_profile

        draft = self._drafts.get(draft_key)
        if draft is None or draft.is_builtin:
            return
        old_active_name = self._active_profile_name
        promoted_profile: AgentProfile | None = None

        if draft.original_name is not None:
            if draft.original_name == old_active_name:
                remaining_main = [p for p in self._registry.list_profiles() if p.name != draft.original_name]
                if not remaining_main:
                    self.notify(
                        self._render_toast(_LAST_MAIN_AGENT.bind()),
                        title=self._render_toast(_AGENT_TITLE.bind()),
                        severity="warning",
                        timeout=5,
                        markup=False,
                    )
                    return
                promoted_profile = remaining_main[0]
            delete_profile(draft.original_name)
            self._registry.remove(draft.original_name)
            self._set_dismiss_intent(_DismissIntent.UPDATED)
            if promoted_profile is not None:
                self._active_profile_name = promoted_profile.name
                self._current_profile = promoted_profile.display_name or promoted_profile.name
                self._set_dismiss_intent(_DismissIntent.SWITCHED)
                if self._on_saved is not None:
                    self._on_saved(None, promoted_profile.name)

        self._drafts.pop(draft_key, None)

        self._populate_sidebar()
        remaining = self._visible_drafts()
        if remaining:
            next_key = remaining[0].key
            ol = self.query_one("#ac-list", OptionList)
            with contextlib.suppress(OptionDoesNotExist):
                ol.highlighted = ol.get_option_index(next_key)
            self._load_profile(next_key)
        self.notify(
            self._render_toast((_AGENT_DELETED if draft.original_name is not None else _AGENT_REMOVED).bind()),
            title=self._render_toast(_AGENT_SETTINGS_TITLE.bind()),
            severity="information",
            timeout=3,
            markup=False,
        )
        self._update_save_button_state()

    @on(Button.Pressed, "#ac-save")
    def _on_save(self, _event: Button.Pressed) -> None:
        if self._read_only:
            return
        if not self._hydrating:
            errors = self._validate_all()
            if errors:
                self._unfold_invalid_mcp_cards()
                self.notify(
                    "\n".join(errors),
                    title=self._render_toast(_VALIDATION_ERROR_TITLE.bind()),
                    severity="error",
                    timeout=5,
                    markup=False,
                )
                return
            try:
                self._sync_selected_draft_from_panels()
            except Exception as exc:
                self.notify(
                    str(exc),
                    title=self._render_toast(_SAVE_ERROR_TITLE.bind()),
                    severity="error",
                    timeout=5,
                    markup=False,
                )
                return
        draft_snapshot = self._snapshot_drafts()
        retargeted_profiles = self._retarget_renamed_sub_agent_refs()
        errors = self._validate_drafts(retargeted_profiles)
        if errors:
            # Read before the restore: it can clear a dirty flag the retargeting set for this pass.
            invalid_mcp_servers = self._selected_draft_invalid_mcp_servers()
            self._restore_drafts(draft_snapshot)
            self._unfold_invalid_mcp_cards(invalid_mcp_servers)
            self.notify(
                "\n".join(errors),
                title=self._render_toast(_VALIDATION_ERROR_TITLE.bind()),
                severity="error",
                timeout=5,
                markup=False,
            )
            return
        try:
            self._apply_all(retargeted_profiles)
        except Exception as exc:
            self._restore_drafts(draft_snapshot)
            self.notify(
                str(exc),
                title=self._render_toast(_SAVE_ERROR_TITLE.bind()),
                severity="error",
                timeout=5,
                markup=False,
            )
            return
        self.notify(
            self._render_toast(_AGENT_PROFILE_SAVED.bind()),
            title=self._render_toast(_AGENT_SETTINGS_TITLE.bind()),
            severity="information",
            timeout=3,
            markup=False,
        )

    def _render_toast(self, reference: MessageRef) -> str:
        return render_str(widget_localizer(self), reference)

    @on(Button.Pressed, "#ac-cancel")
    def _on_cancel_btn(self, _event: Button.Pressed) -> None:
        self.dismiss(self._cancel_result())

    def action_cancel(self) -> None:
        self.dismiss(self._cancel_result())

    def _cancel_result(self) -> str:
        """Return the dismiss result when the user closes the modal.

        ``"switched"`` when a delete promoted a different profile to
        active — the caller rebuilds the engine against that profile,
        even if a later Save updated the promoted profile in-place.
        ``"updated"`` when any Save happened during the session (or the
        active profile was edited mid-modal and already synced).
        ``""`` otherwise.
        """
        if self._read_only:
            return ""
        return self._dismiss_intent.value

    def _default_dismiss_result(self) -> str:  # type: ignore[override]
        return self._cancel_result()

    # ── Validation ───────────────────────────────────────────────────

    def _validate_all(self) -> list[str]:
        from chrys.app.tui.screens.agents.panels.acp import AcpConfigPanel
        from chrys.app.tui.screens.agents.panels.basic import BasicConfigPanel
        from chrys.app.tui.screens.agents.panels.compaction import CompactionConfigPanel
        from chrys.app.tui.screens.agents.panels.instructions import InstructionsConfigPanel
        from chrys.app.tui.screens.agents.panels.mcp import MCPConfigPanel
        from chrys.app.tui.screens.agents.panels.memory import MemoryConfigPanel
        from chrys.app.tui.screens.agents.panels.skills import SkillsConfigPanel
        from chrys.app.tui.screens.agents.panels.subagents import SubAgentsConfigPanel
        from chrys.app.tui.screens.agents.panels.tools import ToolsConfigPanel

        draft = self._drafts.get(self._selected_draft_key)
        is_acp = draft is not None and draft.profile.acp is not None
        panels = (
            (("basic", BasicConfigPanel), ("acp", AcpConfigPanel))
            if is_acp
            else (
                ("basic", BasicConfigPanel),
                ("instructions", InstructionsConfigPanel),
                ("tools", ToolsConfigPanel),
                ("sub-agents", SubAgentsConfigPanel),
                ("skills", SkillsConfigPanel),
                ("mcp", MCPConfigPanel),
                ("memory", MemoryConfigPanel),
                ("compaction", CompactionConfigPanel),
            )
        )
        errors: list[str] = []
        for tab_id, panel_cls in panels:
            if tab_id not in self._mounted_tabs:
                continue
            try:
                errors.extend(self._live_panel(panel_cls).validate())
            except Exception:
                logger.debug("Validation skipped for %s", panel_cls.__name__, exc_info=True)

        return errors

    def _selected_draft_invalid_mcp_servers(self) -> set[int]:
        """Positions of the selected draft's MCP servers that the saved-draft validation rejects.

        That validation checks only dirty drafts: a clean selected draft reported
        no error, so none of its cards is named by the Save error.
        """
        draft = self._drafts.get(self._selected_draft_key)
        if draft is None or not draft.dirty:
            return set()
        return {index for index, errors in enumerate(mcp_draft_errors_by_server("", draft.profile)) if errors}

    def _unfold_invalid_mcp_cards(self, invalid_servers: Collection[int] = ()) -> None:
        """Unfold the MCP server cards that failed validation: a folded card hides the fields its error names.

        The panel checks its own cards; *invalid_servers* adds the positions
        Save's saved-draft validation rejected, which checks some fields differently.
        """
        from chrys.app.tui.screens.agents.panels.mcp import MCPConfigPanel

        draft = self._drafts.get(self._selected_draft_key)
        if "mcp" not in self._mounted_tabs or (draft is not None and draft.profile.acp is not None):
            return
        try:
            self._live_panel(MCPConfigPanel).unfold_invalid_cards(invalid_servers)
        except Exception:
            # As in _validate_all: a panel that can't validate is skipped, never fails the Save handler.
            logger.debug("Could not unfold the invalid MCP server cards", exc_info=True)

    def _validate_drafts(self, retargeted_profiles: list[AgentProfile] | None = None) -> list[str]:
        """Validate the staged draft graph before writing anything to disk."""
        model_profile_exists: Callable[[str], bool] | None = None
        model_registry = self._model_registry
        if model_registry is not None:

            def model_profile_exists(profile_id: str) -> bool:
                return model_registry.get(profile_id) is not None

        store = AgentDraftStore(
            self._visible_drafts(),
            self._registry.list_names(),
            model_profile_exists=model_profile_exists,
            workspace_cwd=self._workspace_cwd,
            workspace_roots=self._workspace_roots,
        )
        return store.validate(retargeted_profiles, render_message=self._render_toast)

    # ── Apply ────────────────────────────────────────────────────────

    def _apply_basic(self, updated, basic: dict) -> None:
        draft = self._drafts.get(self._selected_draft_key)
        self._apply_basic_to_profile(updated, basic, is_builtin=bool(draft and draft.is_builtin))

    @staticmethod
    def _apply_basic_to_profile(updated, basic: dict, *, is_builtin: bool) -> None:
        from chrys.service.profiles.agents.schema import AcpAgentConfig, ModelConfig

        if not is_builtin:
            updated.name = basic["name"]
        updated.display_name = basic["display_name"]
        updated.description = basic["description"]
        if basic.get("agent_type") == "acp":
            updated.acp = updated.acp or AcpAgentConfig()
            updated.sub_agent_only = True
            updated.model = ModelConfig()
        else:
            updated.acp = None
            updated.sub_agent_only = basic["sub_agent_only"]
            updated.model = ModelConfig(profile_id=basic.get("model_profile_id", ""))

    def _sync_selected_draft_from_panels(self) -> None:
        """Harvest the mounted panels into the selected draft profile."""
        if self._read_only:
            return
        if self._hydrating:
            # During tab/profile hydration the draft is already the source of truth.
            # Reading panels before their children settle can harvest stale widgets
            # from the previously mounted profile, especially on slower Windows CI.
            return
        draft = self._drafts.get(self._selected_draft_key)
        if draft is None:
            return
        try:
            draft.profile = self._build_profile_from_mounted_panels(draft)
        except Exception:
            # A field that can't be read (an MCP command with an unclosed quote) fails before validation.
            self._unfold_invalid_mcp_cards()
            raise
        self._selected_profile_name = draft.profile.name
        validation_errors = self._mounted_panel_validation_errors()
        self._set_draft_dirty(draft, self._is_draft_dirty(draft) or bool(validation_errors))
        if validation_errors:
            # Switching still validates the mounted draft strictly because leaving
            # invalid widget state would make the in-memory transaction ambiguous.
            # The user stays on this agent: show the cards whose fields the error names.
            self._unfold_invalid_mcp_cards()
            error_message = "\n".join(
                [
                    *validation_errors,
                    self._render_toast(FIX_VALIDATION_ERRORS.bind()),
                ]
            )
            raise RuntimeError(error_message)

    def _retarget_renamed_sub_agent_refs(self) -> list[AgentProfile]:
        """Keep staged sub-agent references pointed at profiles renamed in this transaction."""
        name_counts: dict[str, int] = {}
        for draft in self._visible_drafts():
            name = draft.profile.name.strip()
            if name:
                lowered = name.casefold()
                name_counts[lowered] = name_counts.get(lowered, 0) + 1

        rename_map = {
            draft.original_name: draft.profile.name
            for draft in self._visible_drafts()
            if draft.original_name
            and draft.profile.name
            and draft.original_name != draft.profile.name
            and re.match(r"^[a-zA-Z0-9][a-zA-Z0-9_-]*$", draft.profile.name)
            and name_counts.get(draft.profile.name.casefold()) == 1
        }
        if not rename_map:
            return []

        for draft in self._visible_drafts():
            changed = False
            for ref in draft.profile.sub_agents.agents:
                new_name = rename_map.get(ref.profile)
                if new_name is None or new_name == ref.profile:
                    continue
                ref.profile = new_name
                changed = True
            if changed:
                draft.dirty = True

        represented_originals = {
            draft.original_name for draft in self._visible_drafts() if draft.original_name is not None
        }
        retargeted_profiles: list[AgentProfile] = []
        for profile in self._registry.list_profiles(include_sub_agent_only=True):
            if profile.name in represented_originals:
                continue
            updated = copy.deepcopy(profile)
            changed = False
            for ref in updated.sub_agents.agents:
                new_name = rename_map.get(ref.profile)
                if new_name is None or new_name == ref.profile:
                    continue
                ref.profile = new_name
                changed = True
            if changed:
                retargeted_profiles.append(updated)
        return retargeted_profiles

    def _live_panel[P: Widget](self, panel_cls: type[P]) -> P:
        """Return the mounted ``panel_cls`` panel, skipping one that a profile load is replacing.

        ``_load_profile`` removes the old panels without waiting, and a removed
        panel stays in the DOM, ahead of its replacement, until all its children
        have exited. Reading it then yields its previous profile's values for the
        widgets already gone, which a harvest would write into the new draft.
        """
        for panel in self.query(panel_cls).results(panel_cls):
            if not panel._pruning:
                return panel
        raise NoMatches(f"No {panel_cls.__name__} is mounted")

    def _build_profile_from_mounted_panels(self, draft: _AgentDraft):
        """Return a profile copy built from the currently mounted tab panels."""
        from chrys.app.tui.screens.agents.panels.acp import AcpConfigPanel
        from chrys.app.tui.screens.agents.panels.basic import BasicConfigPanel
        from chrys.app.tui.screens.agents.panels.compaction import CompactionConfigPanel
        from chrys.app.tui.screens.agents.panels.instructions import InstructionsConfigPanel
        from chrys.app.tui.screens.agents.panels.mcp import MCPConfigPanel
        from chrys.app.tui.screens.agents.panels.memory import MemoryConfigPanel
        from chrys.app.tui.screens.agents.panels.skills import SkillsConfigPanel
        from chrys.app.tui.screens.agents.panels.subagents import SubAgentsConfigPanel
        from chrys.app.tui.screens.agents.panels.tools import ToolsConfigPanel

        builtin_panels: list[tuple[str, MessageDef, type[Any], Callable[[AgentProfile, Any], None]]] = [
            (
                "basic",
                _TAB_BASIC,
                BasicConfigPanel,
                lambda p, cfg: self._apply_basic_to_profile(p, cfg, is_builtin=draft.is_builtin),
            ),
            (
                "instructions",
                _TAB_INSTRUCTIONS,
                InstructionsConfigPanel,
                lambda p, cfg: setattr(p, "instructions", cfg),
            ),
            ("tools", _TAB_TOOLS, ToolsConfigPanel, lambda p, cfg: setattr(p.tools, "builtins", cfg)),
            ("sub-agents", _TAB_SUB_AGENTS, SubAgentsConfigPanel, lambda p, cfg: setattr(p, "sub_agents", cfg)),
            ("skills", _TAB_SKILLS, SkillsConfigPanel, lambda p, cfg: setattr(p, "skills", cfg)),
            ("mcp", _TAB_MCP, MCPConfigPanel, lambda p, cfg: setattr(p.tools, "mcp", cfg)),
            ("memory", _TAB_MEMORY, MemoryConfigPanel, lambda p, cfg: setattr(p, "memory", cfg)),
            ("compaction", _TAB_COMPACTION, CompactionConfigPanel, lambda p, cfg: setattr(p, "compaction", cfg)),
        ]
        panels: list[tuple[str, MessageDef, type[Any], Callable[[AgentProfile, Any], None]]] = (
            [
                (
                    "basic",
                    _TAB_BASIC,
                    BasicConfigPanel,
                    lambda p, cfg: self._apply_basic_to_profile(p, cfg, is_builtin=draft.is_builtin),
                ),
                ("acp", _TAB_ACP_SHORT, AcpConfigPanel, lambda p, cfg: setattr(p, "acp", cfg)),
            ]
            if draft.profile.acp is not None
            else builtin_panels
        )
        updated = copy.deepcopy(draft.profile)
        collected: list[tuple[Callable[[AgentProfile, Any], None], Any]] = []
        not_ready: list[str] = []
        failures: list[str] = []
        for tab_id, tab_title, panel_cls, applier in panels:
            if tab_id not in self._mounted_tabs:
                continue
            try:
                cfg = self._live_panel(panel_cls).get_config()
            except NoMatches as exc:
                not_ready.append(
                    self._render_toast(
                        _TAB_ERROR.bind(
                            tab=self._render_toast(tab_title.bind()),
                            error=DisplayBlock(str(exc)),
                        )
                    )
                )
                continue
            except Exception as exc:
                logger.debug("Failed to read %s tab config", tab_title.fallback, exc_info=True)
                failures.append(
                    self._render_toast(
                        _TAB_ERROR.bind(
                            tab=self._render_toast(tab_title.bind()),
                            error=DisplayBlock(str(exc)),
                        )
                    )
                )
                continue
            collected.append((applier, cfg))
        if not_ready:
            raise _AgentConfigPanelsNotReady(
                self._render_toast(_PANELS_STILL_MOUNTING.bind(details=DisplayBlock("\n".join(not_ready))))
            )
        if failures:
            raise RuntimeError(self._render_toast(_PANELS_READ_ERROR.bind(details=DisplayBlock("\n".join(failures)))))
        for applier, cfg in collected:
            applier(updated, cfg)
        self._canonicalize_profile_for_ui(updated)
        return updated

    @staticmethod
    def _profile_yaml_path(name: str) -> Path:
        from chrys.foundation.platform import get_platform

        return get_platform().config_dir / "agents" / f"{name}.yaml"

    def _snapshot_save_state(self, affected_names: set[str]) -> _AgentSaveSnapshot:
        affected_paths = {self._profile_yaml_path(name) for name in affected_names}
        return _AgentSaveSnapshot(
            files={path: path.read_bytes() if path.is_file() else None for path in affected_paths},
            registry=self._registry.snapshot(),
            current_profile=self._current_profile,
            active_profile_name=self._active_profile_name,
        )

    def _restore_save_state(self, snapshot: _AgentSaveSnapshot) -> None:
        self._current_profile = snapshot.current_profile
        self._active_profile_name = snapshot.active_profile_name
        self._registry.restore(snapshot.registry)
        from chrys.foundation.platform.files import atomic_write_owner_only_bytes

        for path, content in snapshot.files.items():
            if content is None:
                with contextlib.suppress(FileNotFoundError):
                    path.unlink()
            else:
                # Same writer as save_profile: a rolled-back file must keep the
                # owner-only mode the serializer gave it, not the umask default.
                atomic_write_owner_only_bytes(path, content)

    def _old_profile_path_is_still_targeted(self, old_name: str, target_names: set[str]) -> bool:
        old_path = self._profile_yaml_path(old_name)
        for target_name in target_names:
            target_path = self._profile_yaml_path(target_name)
            if old_path == target_path:
                return True
            with contextlib.suppress(OSError):
                if old_path.exists() and target_path.exists() and old_path.samefile(target_path):
                    return True
        return False

    def _should_persist_retargeted_profile(self, profile: AgentProfile) -> bool:
        return not self._registry.is_builtin(profile.name) or self._profile_yaml_path(profile.name).is_file()

    def _profile_matches_builtin_template(self, profile: AgentProfile, builtin_name: str) -> bool:
        template = self._registry.get_builtin_template(builtin_name)
        if template is None:
            return False
        from chrys.service.profiles.agents.serializer import profile_to_dict

        candidate = copy.deepcopy(profile)
        self._canonicalize_profile_for_ui(candidate)
        self._canonicalize_profile_for_ui(template)
        return profile_to_dict(candidate) == profile_to_dict(template)

    def _reset_matches_builtin_template(self, draft: _AgentDraft) -> bool:
        return (
            draft.reset_to_builtin
            and draft.original_name is not None
            and self._profile_matches_builtin_template(draft.profile, draft.original_name)
        )

    def _perform_save(
        self,
        dirty_drafts: list[_AgentDraft],
        retargeted_profiles: list[AgentProfile],
        *,
        old_names: set[str],
        target_names: set[str],
        old_active_name: str,
    ) -> tuple[str | None, str | None]:
        from chrys.service.profiles.agents.serializer import delete_profile, save_profile

        # Write everything before deleting anything: a save that fails after an
        # exact-template reset already removed its shadow would leave the
        # rollback to recreate that file instead of never touching it.
        reset_drafts: list[_AgentDraft] = []
        for draft in dirty_drafts:
            if self._reset_matches_builtin_template(draft):
                reset_drafts.append(draft)
            else:
                save_profile(draft.profile)
        for profile in retargeted_profiles:
            if self._should_persist_retargeted_profile(profile):
                save_profile(profile)
        for draft in reset_drafts:
            delete_profile(draft.profile.name)

        for old_name in old_names:
            old_path = self._profile_yaml_path(old_name)
            if not self._old_profile_path_is_still_targeted(old_name, target_names) and old_path.is_file():
                old_path.unlink()

        dirty_target_names = {draft.profile.name for draft in dirty_drafts}
        for old_name in old_names:
            # Registry keys are intentionally case-sensitive here: case-only renames
            # need only the old exact key removed, without matching the new key.
            # cascade=False is required because refs were already retargeted; cascading
            # here would strip valid parent refs to the renamed profile.
            if old_name not in dirty_target_names:
                self._registry.remove(old_name, cascade=False)

        active_new_display: str | None = None
        active_new_name: str | None = None
        for draft in dirty_drafts:
            updated = draft.profile
            old_name = draft.original_name
            self._registry.register(updated)

            # Detect edits to the session-active profile so the main
            # screen can refresh its input-bar tag / subtitle on Save.
            if old_name == old_active_name:
                new_display = updated.display_name or updated.name
                if new_display != self._current_profile:
                    active_new_display = new_display
                if updated.name != self._active_profile_name:
                    active_new_name = updated.name

        for profile in retargeted_profiles:
            self._registry.register(profile)
        return active_new_display, active_new_name

    def _apply_all(self, retargeted_profiles: list[AgentProfile] | None = None) -> None:
        if self._read_only:
            return
        dirty_drafts = [draft for draft in self._drafts.values() if draft.dirty]
        retargeted_profiles = retargeted_profiles or []
        if not dirty_drafts and not retargeted_profiles:
            return

        old_active_name = self._active_profile_name
        active_new_display: str | None = None
        active_new_name: str | None = None

        renamed_drafts = [
            draft for draft in dirty_drafts if draft.original_name and draft.profile.name != draft.original_name
        ]

        old_names = {draft.original_name for draft in renamed_drafts if draft.original_name is not None}
        target_names = {draft.profile.name for draft in dirty_drafts}
        target_names.update(
            profile.name for profile in retargeted_profiles if self._should_persist_retargeted_profile(profile)
        )
        snapshot = self._snapshot_save_state(old_names | target_names)

        try:
            active_new_display, active_new_name = self._perform_save(
                dirty_drafts,
                retargeted_profiles,
                old_names=old_names,
                target_names=target_names,
                old_active_name=old_active_name,
            )
        except Exception:
            self._restore_save_state(snapshot)
            raise

        if active_new_display is not None:
            self._current_profile = active_new_display
        if active_new_name is not None:
            self._active_profile_name = active_new_name

        self._set_dismiss_intent(_DismissIntent.UPDATED)
        selected_name = self._selected_profile_name

        # Rebuilding the drafts drops their keys: carry each agent's MCP card
        # folds over by the name it was saved under.
        self._remember_mcp_card_folds()
        mcp_card_folds = {
            self._drafts[key].profile.name: folds for key, folds in self._mcp_card_folds.items() if key in self._drafts
        }

        # Refresh sidebar — a rename or description change should show
        # up immediately, and the user may keep editing.
        self._initialize_drafts()
        for name, folds in mcp_card_folds.items():
            if (key := self._find_visible_draft_key(name)) is not None:
                self._mcp_card_folds[key] = folds
        self._populate_sidebar()
        selected_key = self._find_visible_draft_key(selected_name)
        if selected_key is None:
            visible = self._visible_drafts()
            selected_key = visible[0].key if visible else ""
        ol = self.query_one("#ac-list", OptionList)
        with contextlib.suppress(OptionDoesNotExist):
            ol.highlighted = ol.get_option_index(selected_key)
        if selected_key:
            # Re-mount all tab panels against the freshly-saved profile.
            self._load_profile(selected_key, mcp_card_folds_carried=True)
        self._update_save_button_state()

        # Notify the caller of any active-profile change so main-screen widgets
        # refresh immediately and registry-name renames queue a switch for Close.
        if (active_new_display is not None or active_new_name is not None) and self._on_saved is not None:
            self._on_saved(active_new_display, active_new_name)
        elif active_new_name is not None:
            self._set_dismiss_intent(_DismissIntent.SWITCHED)

        # A save can retire the session-active profile from main duty (the
        # External ACP conversion forces ``sub_agent_only``). The engine must
        # never be pointed at a sub-agent-only profile — reload rejects an ACP
        # main and the next startup would refuse the persisted config — so
        # promote a remaining main-eligible profile, exactly like delete does.
        # This runs after the rename notification above so its queued switch
        # overrides a rename that would otherwise re-target the retired name.
        active = self._registry.get(self._active_profile_name)
        if active is not None and active.sub_agent_only:
            remaining_main = [p for p in self._registry.list_profiles() if p.name != active.name]
            if remaining_main:
                promoted = remaining_main[0]
                self._active_profile_name = promoted.name
                self._current_profile = promoted.display_name or promoted.name
                self._set_dismiss_intent(_DismissIntent.SWITCHED)
                if self._on_saved is not None:
                    self._on_saved(None, promoted.name)
