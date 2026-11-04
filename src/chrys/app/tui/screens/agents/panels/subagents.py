# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Sub-Agents configuration panel — composable widget for the Sub-Agents tab."""

from __future__ import annotations

import contextlib
import re
from typing import TYPE_CHECKING

from rich.markup import escape
from rich.text import Text
from textual import on
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.css.query import NoMatches
from textual.widgets import Button, Label

from chrys.app.tui.i18n import render_str, widget_localizer
from chrys.app.tui.screens.agents.validation_messages import (
    CONTEXT_ERROR,
    DUPLICATE_SUB_AGENT_PROFILE,
    DUPLICATE_SUB_AGENT_TOOL,
    FIELD_POSITIVE_INTEGER,
    FIELD_VALID_INTEGER,
    MAX_CONCURRENCY_FIELD,
    MAX_TOTAL_CONCURRENCY_FIELD,
    PROFILE_SELECTION_REQUIRED,
    SUB_AGENT_CONTEXT,
    TOOL_NAME_IDENTIFIER,
)
from chrys.app.tui.screens.agents.validation_messages import (
    MAX_TOTAL_CONCURRENCY as _MAX_TOTAL_CONCURRENCY,
)
from chrys.app.tui.widgets import ConfigAddButton, EnhancedTextArea, HatchedEmptyState, Select
from chrys.app.tui.widgets import EnhancedInput as Input
from chrys.app.tui.widgets.config_card import ConfigCard
from chrys.foundation.i18n import DisplayBlock, MessageRef, msg
from chrys.service.profiles.agents.schema import DEFAULT_SUB_AGENT_CONCURRENCY

if TYPE_CHECKING:
    from textual.app import ComposeResult

    from chrys.service.profiles.agents.registry import AgentProfileRegistry
    from chrys.service.profiles.agents.schema import AgentProfile, SubAgentRef, SubAgentsConfig


_DEFAULT_PROFILE_NAME = msg(
    "tui.agent_config.subagents.default_profile_name",
    fallback="Defaults to profile name",
)
_DEFAULT_PROFILE_DESCRIPTION = msg(
    "tui.agent_config.subagents.default_profile_description",
    fallback="Defaults to profile description",
)
_DEFAULTS_TO = msg("tui.agent_config.subagents.defaults_to", fallback="Defaults to '{name}'")
_DEFAULTS_TO_DESCRIPTION = msg(
    "tui.agent_config.subagents.defaults_to_description",
    fallback="Defaults to '{value}'",
    multiline=True,
)
_NO_DESCRIPTION = msg("tui.agent_config.subagents.no_description", fallback="No description")
_SUB_AGENT = msg("tui.agent_config.subagents.sub_agent", fallback="Sub-Agent")
_PROFILE = msg("tui.agent_config.subagents.profile", fallback="Profile")
_TOOL_NAME = msg("tui.agent_config.subagents.tool_name", fallback="Tool Name")
_TOOL_DESCRIPTION = msg("tui.agent_config.subagents.tool_description", fallback="Tool Description")
_MAX_CONCURRENCY = msg("tui.agent_config.subagents.max_concurrency", fallback="Max Concurrency")
_SUB_AGENTS = msg("tui.agent_config.subagents.title", fallback="Sub-Agents")
_DESCRIPTION = msg(
    "tui.agent_config.subagents.description",
    fallback="Configure sub-agents available to this profile",
)
_ADD = msg("tui.agent_config.subagents.add", fallback="+ Add")
_EMPTY = msg("tui.agent_config.subagents.empty", fallback="No sub-agents configured")


def _render_context_error(widget: object, context: str, reference: MessageRef) -> str:
    localizer = widget_localizer(widget)
    return render_str(
        localizer,
        CONTEXT_ERROR.bind(
            context=DisplayBlock(context),
            message=DisplayBlock(render_str(localizer, reference)),
        ),
    )


class SubAgentCard(ConfigCard):
    """A single sub-agent reference entry."""

    DEFAULT_CSS = """
    SubAgentCard .sa-header-row {
        height: auto;
        margin: 0 0 1 0;
    }
    SubAgentCard .sa-label {
        height: 1;
        color: $text-muted;
        margin: 1 0 0 0;
    }
    SubAgentCard .sa-field {
        width: 1fr;
        height: auto;
        margin: 0;
    }
    SubAgentCard EnhancedTextArea {
        height: 5;
        border: none;
        background: $foreground 8%;
        padding: 0 0;
        margin: 0;
        scrollbar-size-vertical: 1;
    }
    SubAgentCard EnhancedTextArea:focus {
        border: none;
        background: $foreground 12%;
    }
    SubAgentCard EnhancedTextArea .text-area--cursor-line {
        background: $primary 15%;
    }
    SubAgentCard Input {
        height: 1;
        border: none;
        background: $foreground 8%;
        padding: 0 1;
        margin: 0;
    }
    SubAgentCard Input:focus {
        border: none;
        background: $foreground 12%;
    }
    SubAgentCard Select {
        height: auto;
        margin: 0;
    }
    SubAgentCard SelectCurrent {
        height: 1;
        border: none;
        background: $foreground 8%;
        padding: 0 1;
    }
    SubAgentCard SelectOverlay {
        border: round $tui-border-primary $border-opacity;
        background: $surface;
    }
    """

    _delete_button_prefix = "sa-delete-btn"

    def __init__(
        self,
        profile_name: str,
        tool_name: str,
        tool_description: str,
        max_concurrency: int,
        index: int,
        available_profiles: list[tuple[Text, str]],
        registry: AgentProfileRegistry | None = None,
        profile_lookup: dict[str, AgentProfile] | None = None,
        max_concurrency_text: str | None = None,
        read_only: bool = False,
    ) -> None:
        self._profile_name = profile_name
        self._tool_name = tool_name
        self._tool_description = tool_description
        self._max_concurrency = max_concurrency
        self._max_concurrency_text = max_concurrency_text
        self._index = index
        self._available_profiles = available_profiles
        self._registry = registry
        self._profile_lookup = profile_lookup or {}
        self._profile_user_modified = False
        super().__init__(index=index, read_only=read_only)

    def _profile_hints(self, profile_name: str) -> tuple[str, str]:
        """Return (name_placeholder, description_placeholder) for a profile."""
        localizer = widget_localizer(self)
        if not profile_name:
            return (
                render_str(localizer, _DEFAULT_PROFILE_NAME.bind()),
                render_str(localizer, _DEFAULT_PROFILE_DESCRIPTION.bind()),
            )
        p = self._profile_lookup.get(profile_name)
        if p is None and self._registry is not None:
            p = self._registry.get(profile_name)
        if p is None:
            return (
                render_str(localizer, _DEFAULT_PROFILE_NAME.bind()),
                render_str(localizer, _DEFAULT_PROFILE_DESCRIPTION.bind()),
            )
        return (
            render_str(localizer, _DEFAULTS_TO.bind(name=p.name)),
            render_str(localizer, _DEFAULTS_TO_DESCRIPTION.bind(value=DisplayBlock(p.description)))
            if p.description
            else render_str(localizer, _NO_DESCRIPTION.bind()),
        )

    def compose(self) -> ComposeResult:
        name_hint, desc_hint = self._profile_hints(self._profile_name)
        localizer = widget_localizer(self)

        yield from self.compose_header(
            render_str(localizer, _SUB_AGENT.bind()),
            row_class="sa-header-row",
            title_class="sa-title",
        )

        with Vertical(classes="sa-field"):
            yield Label(f"[red]*[/red] {escape(render_str(localizer, _PROFILE.bind()))}", classes="sa-label")
            # Set the initial value synchronously on construction so the value
            # is readable as soon as the card is mounted.  Deferring via
            # ``call_after_refresh`` raced with the synchronous Save handler on
            # Windows CI, dropping refs whose Select hadn't been populated yet.
            has_initial = bool(self._profile_name) and any(v == self._profile_name for _, v in self._available_profiles)
            profile_select = Select(
                self._available_profiles,
                id=f"sa-profile-{self._index}",
                allow_blank=True,
                value=self._profile_name if has_initial else Select.NULL,
            )
            profile_select.disabled = self._read_only
            yield profile_select
            yield Label(render_str(localizer, _TOOL_NAME.bind()), classes="sa-label")
            tool_name = Input(
                value=self._tool_name,
                placeholder=name_hint,
                id=f"sa-tool-name-{self._index}",
            )
            tool_name.disabled = self._read_only
            yield tool_name
            yield Label(render_str(localizer, _TOOL_DESCRIPTION.bind()), classes="sa-label")
            tool_description = EnhancedTextArea(
                self._tool_description,
                placeholder=desc_hint,
                id=f"sa-tool-desc-{self._index}",
            )
            tool_description.read_only = self._read_only
            yield tool_description
            yield Label(render_str(localizer, _MAX_CONCURRENCY.bind()), classes="sa-label")
            max_concurrency_value = (
                self._max_concurrency_text if self._max_concurrency_text is not None else str(self._max_concurrency)
            )
            max_concurrency = Input(
                value=max_concurrency_value,
                placeholder=str(DEFAULT_SUB_AGENT_CONCURRENCY),
                id=f"sa-max-conc-{self._index}",
            )
            max_concurrency.disabled = self._read_only
            yield max_concurrency

    @on(Select.Changed)
    def _on_profile_changed(self, event: Select.Changed) -> None:
        """Update tool name and description placeholders when profile changes."""
        if event.select.id != f"sa-profile-{self._index}":
            return
        if self._read_only:
            return
        if event.value != self._profile_name:
            self._profile_user_modified = True
        profile_name = ""
        if isinstance(event.value, str) and event.value:
            profile_name = event.value
        name_hint, desc_hint = self._profile_hints(profile_name)
        with contextlib.suppress(Exception):
            self.query_one(f"#sa-tool-name-{self._index}", Input).placeholder = name_hint
        with contextlib.suppress(Exception):
            self.query_one(f"#sa-tool-desc-{self._index}", EnhancedTextArea).placeholder = desc_hint

    def get_config(self) -> dict:
        """Read current widget state into a dict."""
        profile = self._profile_name
        try:
            val = self.query_one(f"#sa-profile-{self._index}", Select).value
            # Only accept real string values — reject no-selection sentinels.
        except NoMatches:
            pass
        else:
            if isinstance(val, str) and val:
                profile = val
            elif self._profile_user_modified:
                profile = ""

        max_conc = self._max_concurrency
        with contextlib.suppress(ValueError, NoMatches):
            raw_max_conc = self.query_one(f"#sa-max-conc-{self._index}", Input).value.strip()
            max_conc = int(raw_max_conc)

        tool_name = self._tool_name
        with contextlib.suppress(NoMatches):
            tool_name = self.query_one(f"#sa-tool-name-{self._index}", Input).value.strip()

        tool_description = self._tool_description
        with contextlib.suppress(NoMatches):
            tool_description = self.query_one(f"#sa-tool-desc-{self._index}", EnhancedTextArea).text.strip()

        return {
            "profile": profile,
            "tool_name": tool_name,
            "tool_description": tool_description,
            "max_concurrency": max_conc,
        }

    def _snapshot_max_concurrency_text(self) -> str | None:
        """Return raw max concurrency text for UI-only rebuild preservation."""
        with contextlib.suppress(NoMatches):
            return self.query_one(f"#sa-max-conc-{self._index}", Input).value
        return self._max_concurrency_text

    @property
    def has_profile(self) -> bool:
        """Whether this card has a valid profile selected."""
        try:
            val = self.query_one(f"#sa-profile-{self._index}", Select).value
            return isinstance(val, str) and bool(val)
        except Exception:
            return False

    def validate(self) -> list[str]:
        """Validate this sub-agent entry."""
        errors: list[str] = []
        localizer = widget_localizer(self)
        display = render_str(localizer, SUB_AGENT_CONTEXT.bind(index=self._index + 1))

        if not self.has_profile:
            errors.append(_render_context_error(self, display, PROFILE_SELECTION_REQUIRED.bind()))
            return errors

        try:
            tool_name = self.query_one(f"#sa-tool-name-{self._index}", Input).value.strip()
            if tool_name and not re.match(r"^[a-zA-Z_][a-zA-Z0-9_]*$", tool_name):
                errors.append(_render_context_error(self, display, TOOL_NAME_IDENTIFIER.bind()))
        except Exception:
            pass

        try:
            mc_text = self.query_one(f"#sa-max-conc-{self._index}", Input).value.strip()
            if mc_text:
                try:
                    mc = int(mc_text)
                    if mc <= 0:
                        errors.append(
                            _render_context_error(
                                self,
                                display,
                                FIELD_POSITIVE_INTEGER.bind(field=render_str(localizer, MAX_CONCURRENCY_FIELD.bind())),
                            )
                        )
                except ValueError:
                    errors.append(
                        _render_context_error(
                            self,
                            display,
                            FIELD_VALID_INTEGER.bind(field=render_str(localizer, MAX_CONCURRENCY_FIELD.bind())),
                        )
                    )
        except Exception:
            pass

        return errors


class SubAgentsConfigPanel(VerticalScroll):
    """Composable widget for the Sub-Agents configuration tab."""

    DEFAULT_CSS = """
    SubAgentsConfigPanel {
        height: 1fr;
        padding: 0 0 0 2;
        scrollbar-size-vertical: 1;
    }
    SubAgentsConfigPanel .sa-section-title {
        color: $secondary;
        text-style: bold;
        height: 1;
    }
    SubAgentsConfigPanel .sa-section-desc {
        color: $text-muted;
        height: 1;
        margin: 0 0 1 0;
    }
    SubAgentsConfigPanel .sa-header-bar {
        height: auto;
        margin: 0 0 1 0;
    }
    SubAgentsConfigPanel .sa-label {
        height: 1;
        color: $text-muted;
        margin: 1 0 0 0;
    }
    SubAgentsConfigPanel .sa-conc-row {
        height: auto;
        margin: 0;
    }
    SubAgentsConfigPanel #sa-max-total {
        width: 16;
        height: 1;
        border: none;
        background: $foreground 8%;
        padding: 0 1;
    }
    SubAgentsConfigPanel #sa-max-total:focus {
        border: none;
        background: $foreground 12%;
    }
    SubAgentsConfigPanel #sa-add-btn {
        min-width: 10;
        height: 1;
        margin: 0 0 1 0;
    }
    SubAgentsConfigPanel .sa-label:first-of-type {
        margin: 0;
    }
    SubAgentsConfigPanel #sa-cards {
        height: auto;
    }
    SubAgentsConfigPanel .sa-empty {
        margin: 0 2 1 0;
    }
    """

    def __init__(
        self,
        sub_agents_config: SubAgentsConfig | None = None,
        registry: AgentProfileRegistry | None = None,
        current_profile_name: str = "",
        available_profiles: list[AgentProfile] | None = None,
        read_only: bool = False,
    ) -> None:
        from chrys.service.profiles.agents.schema import SubAgentsConfig as SAC

        self._config = sub_agents_config or SAC()
        self._registry = registry
        self._current_profile_name = current_profile_name
        self._available_profile_overrides = available_profiles
        self._read_only = read_only
        self._max_concurrency_overrides: list[str | None] = [None for _ref in self._config.agents]
        super().__init__()

    def _available_profile_objects(self) -> list[AgentProfile]:
        if self._available_profile_overrides is not None:
            return self._available_profile_overrides
        if self._registry is None:
            return []
        return self._registry.list_profiles(include_sub_agent_only=True)

    def _profile_lookup(self) -> dict[str, AgentProfile]:
        return {profile.name: profile for profile in self._available_profile_objects()}

    def _available_profiles(self) -> list[tuple[Text, str]]:
        """Build list of (display, value) tuples for the profile selector."""
        profiles = self._available_profile_objects()
        return [
            (Text(f"{p.display_name or p.name}{' (ACP)' if p.acp is not None else ''}"), p.name)
            for p in profiles
            if p.name != self._current_profile_name
        ]

    def compose(self) -> ComposeResult:
        localizer = widget_localizer(self)
        with Vertical(classes="sa-header-bar"):
            yield Label(render_str(localizer, _SUB_AGENTS.bind()), classes="sa-section-title")
            yield Label(render_str(localizer, _DESCRIPTION.bind()), classes="sa-section-desc")
            yield Label(render_str(localizer, _MAX_TOTAL_CONCURRENCY.bind()), classes="sa-label")
            with Horizontal(classes="sa-conc-row"):
                max_total = Input(
                    value=str(self._config.max_total_concurrency),
                    placeholder=str(DEFAULT_SUB_AGENT_CONCURRENCY),
                    id="sa-max-total",
                )
                max_total.disabled = self._read_only
                yield max_total

        add_button = ConfigAddButton(render_str(localizer, _ADD.bind()), id="sa-add-btn")
        add_button.disabled = self._read_only
        add_button.display = not self._read_only
        yield add_button
        yield Vertical(id="sa-cards")

    async def on_mount(self) -> None:
        await self._rebuild_cards()

    async def _rebuild_cards(self) -> None:
        container = self.query_one("#sa-cards", Vertical)
        await container.remove_children()
        agents = self._config.agents
        if not agents:
            await container.mount(
                HatchedEmptyState(render_str(widget_localizer(self), _EMPTY.bind()), classes="sa-empty")
            )
            return
        available = self._available_profiles()
        profile_lookup = self._profile_lookup()
        await container.mount(
            *(
                SubAgentCard(
                    profile_name=ref.profile,
                    tool_name=ref.tool_name,
                    tool_description=ref.tool_description,
                    max_concurrency=ref.max_concurrency,
                    index=i,
                    available_profiles=available,
                    registry=self._registry,
                    profile_lookup=profile_lookup,
                    max_concurrency_text=self._max_concurrency_override_for_index(i),
                    read_only=self._read_only,
                )
                for i, ref in enumerate(agents)
            )
        )

    def _max_concurrency_override_for_index(self, index: int) -> str | None:
        if index < len(self._max_concurrency_overrides):
            return self._max_concurrency_overrides[index]
        return None

    @on(Button.Pressed, "#sa-add-btn")
    async def _on_add(self, _event: Button.Pressed) -> None:
        if self._read_only:
            return
        from chrys.service.profiles.agents.schema import SubAgentRef

        self._config.agents, self._max_concurrency_overrides = self._collect_agent_ref_snapshots()
        self._config.agents.insert(0, SubAgentRef(profile=""))
        self._max_concurrency_overrides.insert(0, None)
        await self._rebuild_cards()

    def _collect_agent_ref_snapshots(self) -> tuple[list[SubAgentRef], list[str | None]]:
        """Snapshot current sub-agent rows plus UI-only max concurrency text."""
        from chrys.service.profiles.agents.schema import SubAgentRef

        cards = list(self.query(SubAgentCard))
        if len(cards) != len(self._config.agents):
            return (
                [
                    SubAgentRef(
                        profile=ref.profile,
                        tool_name=ref.tool_name,
                        tool_description=ref.tool_description,
                        max_concurrency=ref.max_concurrency,
                    )
                    for ref in self._config.agents
                ],
                [self._max_concurrency_override_for_index(index) for index in range(len(self._config.agents))],
            )

        refs: list[SubAgentRef] = []
        max_concurrency_overrides: list[str | None] = []
        for card in cards:
            cfg = card.get_config()
            refs.append(
                SubAgentRef(
                    profile=cfg["profile"],
                    tool_name=cfg["tool_name"],
                    tool_description=cfg["tool_description"],
                    max_concurrency=cfg["max_concurrency"],
                )
            )
            max_concurrency_overrides.append(card._snapshot_max_concurrency_text())
        return refs, max_concurrency_overrides

    def _collect_agent_refs(self) -> list[SubAgentRef]:
        """Snapshot current sub-agent rows, preserving blanks for row indices."""
        refs, _max_concurrency_overrides = self._collect_agent_ref_snapshots()
        return refs

    @on(SubAgentCard.RemoveRequested)
    async def _on_remove(self, event: SubAgentCard.RemoveRequested) -> None:
        if self._read_only:
            return
        self._config.agents, self._max_concurrency_overrides = self._collect_agent_ref_snapshots()
        if 0 <= event.index < len(self._config.agents):
            self._config.agents.pop(event.index)
            if event.index < len(self._max_concurrency_overrides):
                self._max_concurrency_overrides.pop(event.index)
            await self._rebuild_cards()

    def get_config(self) -> SubAgentsConfig:
        """Read current widget state into a SubAgentsConfig.

        Cards without a profile selected are silently dropped.
        """
        from chrys.service.profiles.agents.schema import SubAgentsConfig as SAC

        max_total = self._config.max_total_concurrency
        with contextlib.suppress(ValueError, Exception):
            max_total = int(self.query_one("#sa-max-total", Input).value.strip())

        # Mount-race fallback: the panel is freshly (re)mounted and not
        # every SubAgentCard has reached the DOM yet — Windows CI hits
        # this when Save fires immediately after Clone. _config.agents is
        # the synchronously-maintained structural source of truth (seeded
        # at __init__, kept in sync by _on_add / _on_remove), so use it
        # whenever the card view is incomplete. The user cannot have
        # edited what is not visible, so falling back loses no edits.
        # Blank profiles are still silently dropped to match the
        # cards-mounted path.
        agents = [ref for ref in self._collect_agent_refs() if ref.profile]
        return SAC(max_total_concurrency=max_total, agents=agents)

    def validate(self) -> list[str]:
        """Validate all sub-agent entries."""
        localizer = widget_localizer(self)
        errors: list[str] = []
        try:
            val = self.query_one("#sa-max-total", Input).value.strip()
            if val:
                try:
                    mt = int(val)
                    if mt <= 0:
                        errors.append(
                            render_str(
                                localizer,
                                FIELD_POSITIVE_INTEGER.bind(
                                    field=render_str(localizer, MAX_TOTAL_CONCURRENCY_FIELD.bind())
                                ),
                            )
                        )
                except ValueError:
                    errors.append(
                        render_str(
                            localizer,
                            FIELD_VALID_INTEGER.bind(field=render_str(localizer, MAX_TOTAL_CONCURRENCY_FIELD.bind())),
                        )
                    )
        except Exception:
            pass

        seen_profiles: set[str] = set()
        seen_tool_names: set[str] = set()
        for card in self.query(SubAgentCard):
            errors.extend(card.validate())
            with contextlib.suppress(Exception):
                cfg = card.get_config()
                profile = cfg["profile"]
                if profile:
                    if profile in seen_profiles:
                        errors.append(
                            render_str(
                                localizer,
                                DUPLICATE_SUB_AGENT_PROFILE.bind(name=DisplayBlock(profile)),
                            )
                        )
                    seen_profiles.add(profile)
                    effective_name = cfg["tool_name"] or profile
                    if effective_name in seen_tool_names:
                        errors.append(
                            render_str(
                                localizer,
                                DUPLICATE_SUB_AGENT_TOOL.bind(name=DisplayBlock(effective_name)),
                            )
                        )
                    seen_tool_names.add(effective_name)
        return errors
