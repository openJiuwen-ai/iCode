# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""ModelsScreen — modal for browsing and selecting model profiles."""

from __future__ import annotations

from enum import StrEnum
from typing import TYPE_CHECKING, ClassVar

from textual import on
from textual.containers import VerticalGroup
from textual.content import Content
from textual.widgets import OptionList
from textual.widgets.option_list import Option

from chrys.app.tui.binding_display import CLOSE_BINDING, localized_binding
from chrys.app.tui.i18n import render_str, widget_localizer
from chrys.app.tui.screens.dialogs.base import BaseDialog
from chrys.foundation.i18n import msg
from chrys.foundation.text.reading_order import reading_order
from chrys.service.profiles.models.schema import is_model_profile_selectable

if TYPE_CHECKING:
    from textual.app import ComposeResult

    from chrys.service.profiles.models.registry import ModelProfileRegistry

_PROFILE_OPTION_PREFIX = "profile:"
_MANAGE_OPTION_ID = "action:manage"

_MODELS = msg("tui.model_picker.title", fallback="Models")
_MANAGE_MODELS = msg("tui.model_picker.manage", fallback="Manage models…")
_HIDDEN_PROFILES = msg(
    "tui.model_picker.hidden_profiles",
    fallback="{count} incomplete profile hidden",
    plural_fallback="{count} incomplete profiles hidden",
)


class ModelPickerAction(StrEnum):
    """Non-profile actions returned by the model picker."""

    MANAGE = "manage"


type ModelPickerResult = str | ModelPickerAction | None


class ModelsScreen(BaseDialog[ModelPickerResult]):
    """Modal for selecting model profiles or opening model settings.

    Single click selects and dismisses. Current profile is dimmed and disabled.
    Escape cancels.
    """

    DEFAULT_CSS = """
    ModelsScreen {
        align: center middle;
    }
    ModelsScreen > #container {
        width: 60;
        max-width: 90%;
        max-height: 90%;
        height: auto;
        background: $surface;
        border: round $tui-border-primary $border-opacity;
        border-title-align: left;
        border-title-color: $primary;
        padding: 0;
        overflow-x: hidden;
    }
    ModelsScreen > #container > OptionList {
        height: auto;
        max-height: 100%;
        border: none;
        padding: 0 0 0 1;
        scrollbar-size: 1 1;
    }
    """

    BINDINGS: ClassVar[list] = [
        localized_binding("escape", "dismiss", CLOSE_BINDING),
    ]

    def __init__(self, registry: ModelProfileRegistry, current_profile_id: str) -> None:
        self._registry = registry
        self._current_profile_id = current_profile_id
        super().__init__()

    def compose(self) -> ComposeResult:
        with VerticalGroup(id="container") as container:
            container.border_title = render_str(widget_localizer(self), _MODELS.bind())
            yield OptionList()

    def on_mount(self) -> None:
        ol = self.query_one(OptionList)
        # AIxCoding catalog: "Manage models…" entry disabled — the picker
        # lists profiles only; model configuration is centrally managed and
        # read-only (aixcoding/docs/batches/catalog-sync/design.md).
        profiles = self._registry.list_profiles()
        selectable_profiles = sorted(
            (profile for profile in profiles if is_model_profile_selectable(profile)),
            key=lambda profile: reading_order(profile.name),
        )
        for profile in selectable_profiles:
            is_current = profile.id == self._current_profile_id
            prefix = "◦ " if is_current else ""
            if is_current:
                content = Content.assemble((f"{prefix}{profile.name}", "bold #888888"))
            else:
                content = Content.assemble((profile.name, "bold"))
            ol.add_option(
                Option(
                    content,
                    id=f"{_PROFILE_OPTION_PREFIX}{profile.id}",
                    disabled=is_current,
                )
            )

    @on(OptionList.OptionSelected)
    def _on_selected(self, event: OptionList.OptionSelected) -> None:
        """Single click selects and dismisses."""
        option_id = event.option.id
        if not option_id:
            return
        namespace_and_value = option_id.split(":", maxsplit=1)
        if len(namespace_and_value) != 2:
            return
        namespace, value = namespace_and_value
        if namespace == "profile":
            self.dismiss(value)
        elif namespace == "action" and value == "manage":
            self.dismiss(ModelPickerAction.MANAGE)
