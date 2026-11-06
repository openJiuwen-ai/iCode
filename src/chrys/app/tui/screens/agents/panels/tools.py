# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tools configuration panel — composable widget for the Tools tab."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

from rich.text import Text
from textual.containers import Vertical, VerticalScroll
from textual.content import Content
from textual.css.query import NoMatches
from textual.widgets import Label

from chrys.app.tui.i18n import render_str, widget_localizer
from chrys.app.tui.widgets import Checkbox
from chrys.foundation.branding import APP_DISPLAY_NAME
from chrys.foundation.i18n import MessageDef, msg
from chrys.foundation.i18n.formatting import sanitize_legacy_scalar
from chrys.foundation.platform.files import surrogate_safe_text
from chrys.service.tools.packs import BUILTIN_PACKS

if TYPE_CHECKING:
    from collections.abc import Sequence

    from textual.app import ComposeResult
    from textual.widget import Widget

    from chrys.service.extensions import ExtensionCatalog
    from chrys.service.profiles.agents.schema import ToolsConfig

logger = logging.getLogger(__name__)

_BUILTIN_TOOL_CATEGORIES = msg("tui.agent_config.tools.categories", fallback="Builtin Tool Categories")
_CATEGORIES_DESCRIPTION = msg(
    "tui.agent_config.tools.categories_description",
    fallback="Toggle categories to enable or disable",
)

_EXTENSIONS = msg("tui.agent_config.tools.extensions", fallback="Extensions")
_EXTENSIONS_DESCRIPTION = msg(
    "tui.agent_config.tools.extensions_description",
    fallback=(
        "Python extensions installed for {app}. They run inside {app} with your permissions: "
        "install only extensions you trust"
    ),
)
_FINDING_EXTENSIONS = msg("tui.agent_config.tools.finding_extensions", fallback="Looking for installed extensions…")
_NO_EXTENSIONS = msg(
    "tui.agent_config.tools.no_extensions",
    fallback="No extensions installed. The Python extensions guide (F8) shows how to write or install one",
)
_EXTENSIONS_UNREADABLE = msg(
    "tui.agent_config.tools.extensions_unreadable", fallback="Installed extensions cannot be listed"
)
_EXTENSION_NOT_INSTALLED = msg(
    "tui.agent_config.tools.extension_not_installed",
    fallback="Not installed: agents start without it",
)
_EXTENSION_CONFLICT = msg(
    "tui.agent_config.tools.extension_conflict",
    fallback="Installed more than once, so neither copy loads:",
)
_EXTENSION_SKIPPED = msg("tui.agent_config.tools.extension_skipped", fallback="Skipped {problem}")

# The packs a profile can switch, in catalog order, as (id, label, description); every
# agent loads the untitled (``ALWAYS_LOADED_TOOL_CATEGORIES``) ones, so they have no switch.
SWITCHABLE_PACKS: tuple[tuple[str, MessageDef, MessageDef], ...] = tuple(
    (pack.id, pack.title, pack.description)
    for pack in BUILTIN_PACKS
    if pack.title is not None and pack.description is not None
)


def _cat_id(cat: str) -> str:
    """Sanitise a category name for use as a CSS-safe widget ID."""
    return cat.replace("_", "-").replace(".", "-")


@dataclass(frozen=True, slots=True)
class ToolSelection:
    """What the Tools tab enables: ``tools.builtins`` and ``extensions``."""

    builtins: list[str]
    extensions: list[str]


class ToolsConfigPanel(VerticalScroll):
    """Composable widget for the Tools configuration tab.

    Displays builtin tool categories, then the installed extensions, as
    checkboxes. Listing the extensions reads only their metadata (none of their
    code runs), in a worker so the tab paints first.
    """

    DEFAULT_CSS = """
    ToolsConfigPanel {
        height: 1fr;
        /* Left inset only: the scrollbar is carved out inside the padding,
           so right padding would sit right of the scrollbar. The right inset
           rides child margins below instead. */
        padding: 0 0 0 2;
        scrollbar-size-vertical: 1;
        scrollbar-gutter: stable;
    }
    ToolsConfigPanel > * {
        margin-right: 1;
    }
    ToolsConfigPanel .tc-section-title {
        color: $secondary;
        text-style: bold;
        height: 1;
    }
    ToolsConfigPanel .tc-section-desc {
        color: $text-muted;
        height: auto;
        width: 1fr;
        margin: 0 1 1 0;
        text-wrap: wrap;
        text-overflow: fold;
    }
    ToolsConfigPanel .tc-categories {
        height: auto;
    }
    ToolsConfigPanel Checkbox {
        height: 1;
        padding: 0;
        border: none;
        background: transparent;
        margin: 0;
    }
    ToolsConfigPanel Checkbox > .toggle--button {
        color: $foreground 35%;
        background: $foreground 6%;
    }
    ToolsConfigPanel Checkbox.-on > .toggle--button {
        color: $success;
        background: $foreground 6%;
    }
    ToolsConfigPanel .tc-cat-desc {
        color: $text-muted;
        height: auto;
        width: 1fr;
        margin: 0 0 1 4;
        text-wrap: wrap;
        text-overflow: fold;
    }
    """

    def __init__(
        self,
        tools_config: ToolsConfig | None = None,
        *,
        extensions: Sequence[str] = (),
        read_only: bool = False,
    ) -> None:
        from chrys.service.profiles.agents.schema import ToolsConfig as TC

        self._tools = tools_config or TC()
        self._active_categories: set[str] = set(self._tools.builtins)
        self._extensions = list(extensions)
        self._read_only = read_only
        self._extension_switches: dict[str, Checkbox] = {}
        super().__init__()

    def compose(self) -> ComposeResult:
        localizer = widget_localizer(self)
        yield Label(render_str(localizer, _BUILTIN_TOOL_CATEGORIES.bind()), classes="tc-section-title")
        yield Label(render_str(localizer, _CATEGORIES_DESCRIPTION.bind()), classes="tc-section-desc")
        with Vertical(classes="tc-categories"):
            for cat, label, desc in SWITCHABLE_PACKS:
                cb = Checkbox(
                    render_str(localizer, label.bind()),
                    value=cat in self._active_categories,
                    id=f"tc-cat-{_cat_id(cat)}",
                )
                cb.disabled = self._read_only
                yield cb
                yield Label(render_str(localizer, desc.bind()), classes="tc-cat-desc")
        yield Label(render_str(localizer, _EXTENSIONS.bind()), classes="tc-section-title")
        yield Label(
            render_str(localizer, _EXTENSIONS_DESCRIPTION.bind(app=APP_DISPLAY_NAME)), classes="tc-section-desc"
        )
        with Vertical(id="tc-extensions", classes="tc-categories"):
            yield Label(
                render_str(localizer, _FINDING_EXTENSIONS.bind()), id="tc-extensions-status", classes="tc-cat-desc"
            )

    def on_mount(self) -> None:
        self.run_worker(self._list_extensions(), group="extensions", exit_on_error=False)

    async def _list_extensions(self) -> None:
        from chrys.service.extensions import discover_extensions

        status = self.query_one("#tc-extensions-status", Label)
        localizer = widget_localizer(self)
        try:
            catalog = await asyncio.to_thread(discover_extensions)
        except Exception:
            logger.warning("Listing the installed extensions failed", exc_info=True)
            status.update(render_str(localizer, _EXTENSIONS_UNREADABLE.bind()))
            return
        rows = self._extension_rows(catalog)
        if not rows:
            status.update(render_str(localizer, _NO_EXTENSIONS.bind()))
            return
        status.display = False
        await self.query_one("#tc-extensions", Vertical).mount(*rows)

    def _extension_rows(self, catalog: ExtensionCatalog) -> list[Widget]:
        localizer = widget_localizer(self)
        enabled = self._extensions
        rows: list[Widget] = []

        def add(extension_id: str, *lines: str, available: bool) -> None:
            # Ids, summaries and paths come from packages, files and profiles: shown as plain text.
            label = Content.from_text(sanitize_legacy_scalar(extension_id), markup=False)
            switch = Checkbox(label, value=extension_id in enabled)
            switch.disabled = self._read_only or not available
            if available or extension_id in enabled:
                self._extension_switches[extension_id] = switch
            detail = "\n".join(sanitize_legacy_scalar(surrogate_safe_text(line)) for line in lines if line)
            rows.extend((switch, Label(Text(detail), classes="tc-cat-desc")))

        # Where an extension is installed goes on a line of its own, after its summary.
        for extension_id in sorted(catalog.sources):
            source = catalog.sources[extension_id]
            add(extension_id, source.summary, source.location, available=True)
        for extension_id in sorted(catalog.conflicts):
            locations = (copy.location for copy in catalog.conflicts[extension_id])
            add(extension_id, render_str(localizer, _EXTENSION_CONFLICT.bind()), *locations, available=False)
        for extension_id in enabled:
            if extension_id not in catalog.sources and extension_id not in catalog.conflicts:
                # Kept so a save keeps it; unchecking it removes it from the profile.
                add(extension_id, render_str(localizer, _EXTENSION_NOT_INSTALLED.bind()), available=True)
        rows.extend(
            Label(
                Text(sanitize_legacy_scalar(render_str(localizer, _EXTENSION_SKIPPED.bind(problem=problem)))),
                classes="tc-cat-desc",
            )
            for problem in catalog.skipped
        )
        return rows

    def get_config(self) -> ToolSelection:
        """Return the enabled tool categories and extensions."""
        return ToolSelection(builtins=self._builtin_config(), extensions=self._extension_config())

    def _extension_config(self) -> list[str]:
        # Until the extensions are listed (or for one listed as a conflict) the profile's choice stands.
        kept = [
            extension_id
            for extension_id in self._extensions
            if extension_id not in self._extension_switches or self._extension_switches[extension_id].value
        ]
        added = [
            extension_id
            for extension_id, switch in self._extension_switches.items()
            if switch.value and extension_id not in kept
        ]
        return [*kept, *added]

    def _builtin_config(self) -> list[str]:
        result: list[str] = []
        for cat, _label, _desc in SWITCHABLE_PACKS:
            try:
                cb = self.query_one(f"#tc-cat-{_cat_id(cat)}", Checkbox)
            except NoMatches:
                if cat in self._active_categories:
                    result.append(cat)
            else:
                if cb.value:
                    result.append(cat)
        # Categories without a switch here (another version's tools) pass through a save untouched.
        known = {cat for cat, *_ in SWITCHABLE_PACKS}
        result.extend(dict.fromkeys(cat for cat in self._tools.builtins if cat not in known))
        return result

    def validate(self) -> list[str]:
        """Validate tools configuration."""
        return []
