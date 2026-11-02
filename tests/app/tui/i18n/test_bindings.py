# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Contracts for localized binding metadata and stock Textual footers."""

from __future__ import annotations

import importlib
import inspect
import logging
import pkgutil
from dataclasses import FrozenInstanceError
from types import ModuleType
from typing import ClassVar

import pytest
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.pilot import Pilot
from textual.widget import Widget
from textual.widgets import Footer
from textual.widgets._footer import FooterKey, FooterLabel

from chrys.app import tui
from chrys.app.tui import binding_display
from chrys.app.tui.app import ChrysApp
from chrys.app.tui.binding_display import (
    BindingDisplaySpec,
    get_binding_display_provenance,
    get_binding_display_spec,
    iter_binding_display_specs,
    localized_binding,
    resolve_binding_display,
)
from chrys.app.tui.i18n import LocaleController, render_str
from chrys.app.tui.screens.diff import screen as diff_screen
from chrys.app.tui.screens.main import screen as main_screen
from chrys.app.tui.screens.sessions import screen as sessions_screen
from chrys.app.tui.widgets.chrome.footer import ChrysFooter
from chrys.foundation.config.settings import Settings
from chrys.foundation.i18n import Localizer, MessageRef, msg
from tests.support.waiting import wait_for, wait_until_quiet


async def _wait_for_footer_keys(pilot: Pilot[None], *nodes: App[None] | Widget) -> None:
    """Footer keys mount via a deferred recompose; wait until every node has them.

    Presence alone is not stable: a bindings publish landing while a recompose
    is in flight forces a second, by-design recompose pass, and on a loaded
    worker its remove/mount gap can open right after a presence check. Require
    the keys to survive one settled (idle) pass — which also gives freshly
    mounted keys their layout geometry — before returning.
    """

    def _keys_present() -> bool:
        return all(node.query(FooterKey) for node in nodes)

    await wait_for(_keys_present, pilot=pilot, description="footer keys mounted")

    def _key_identities() -> tuple[tuple[int, ...], ...]:
        return tuple(tuple(id(key) for key in node.query(FooterKey)) for node in nodes)

    await wait_until_quiet(
        _key_identities,
        description="footer key identities",
        pumps=5,
        pilot=pilot,
    )
    if not _keys_present():
        raise AssertionError("footer keys kept recomposing without settling")


_DESCRIPTION = msg("tests.binding.description", fallback="Description")
_EQUAL_DESCRIPTION = msg("tests.binding.description", fallback="Description")
_OTHER_DESCRIPTION = msg("tests.binding.description", fallback="Other description")
_TOOLTIP = msg("tests.binding.description.tooltip", fallback="More information")
_SECOND_DESCRIPTION = msg("tests.binding.second", fallback="Second")


@pytest.fixture
def isolated_binding_registry(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(binding_display, "_BINDING_DISPLAY_REGISTRY", {})


def test_textual_binding_display_fields_and_group_contract() -> None:
    group = Binding.Group(description="Navigation", compact=True)
    binding = Binding(
        "x",
        "do_thing",
        "Do thing",
        tooltip="More information",
        id="contract.binding.do_thing",
        group=group,
    )

    assert binding.id == "contract.binding.do_thing"
    assert binding.description == "Do thing"
    assert binding.tooltip == "More information"
    assert binding.group is group
    assert group.description == "Navigation"
    assert group.compact is True
    with pytest.raises(FrozenInstanceError):
        group.description = "Changed"  # type: ignore[misc]


@pytest.mark.asyncio
async def test_stock_footer_consumes_binding_descriptions_tooltips_and_group_description() -> None:
    group = Binding.Group(description="Navigation")

    class FooterContractApp(App[None]):
        ENABLE_COMMAND_PALETTE = False
        BINDINGS: ClassVar[list[Binding]] = [
            Binding(
                "a",
                "plain",
                "Plain description",
                tooltip="Plain tooltip",
                id="contract.binding.plain",
            ),
            Binding("b", "grouped_one", "Grouped one", tooltip="Grouped tooltip", group=group),
            Binding("c", "grouped_two", "Grouped two", group=group),
        ]

        def compose(self) -> ComposeResult:
            yield Footer()

    app = FooterContractApp()
    async with app.run_test() as pilot:
        await _wait_for_footer_keys(pilot, app)
        keys = {key.action: key for key in app.query(FooterKey)}
        labels = list(app.query(FooterLabel))

        assert app.active_bindings["a"].binding.id == "contract.binding.plain"
        assert keys["plain"].description == "Plain description"
        assert keys["plain"].tooltip == "Plain tooltip"
        assert keys["grouped_one"].description == ""
        assert keys["grouped_one"].tooltip == "Grouped tooltip"
        assert keys["grouped_two"].description == ""
        assert keys["grouped_two"].tooltip == "Grouped two"
        assert [str(label.render()) for label in labels] == ["Navigation"]


@pytest.mark.asyncio
async def test_chrys_footer_without_controller_is_stock_identical_and_unregistered_is_literal() -> None:
    binding = Binding(
        "x",
        "literal_action",
        "Literal description",
        tooltip="Literal tooltip",
        id="tests.binding.unregistered",
    )

    class FooterContractApp(App[None]):
        ENABLE_COMMAND_PALETTE = False
        BINDINGS: ClassVar[list[Binding]] = [binding]

        def __init__(self) -> None:
            super().__init__()
            self.stock = Footer()
            self.chrys = ChrysFooter()

        def compose(self) -> ComposeResult:
            yield self.stock
            yield self.chrys

    app = FooterContractApp()
    async with app.run_test() as pilot:
        await _wait_for_footer_keys(pilot, app.stock, app.chrys)
        stock_key = app.stock.query_one(FooterKey)
        chrys_key = app.chrys.query_one(FooterKey)

        assert chrys_key.description == stock_key.description == "Literal description"
        assert chrys_key.tooltip == stock_key.tooltip == "Literal tooltip"
        assert chrys_key.render() == stock_key.render()
        assert chrys_key.styles.width == stock_key.styles.width
        assert str(chrys_key.styles.width) == "auto"


@pytest.mark.asyncio
async def test_controlled_chrys_footer_keeps_unregistered_binding_literal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from chrys.app.tui import i18n as tui_i18n
    from chrys.app.tui.i18n import LocaleController

    binding = Binding(
        "x",
        "literal_action",
        "Literal description",
        tooltip="Literal tooltip",
        id="tests.binding.unregistered",
    )
    controller = LocaleController(Settings(locale="en"))

    class FooterContractApp(App[None]):
        ENABLE_COMMAND_PALETTE = False
        BINDINGS: ClassVar[list[Binding]] = [binding]

        def compose(self) -> ComposeResult:
            yield ChrysFooter(locale_controller=controller)

    monkeypatch.setattr(tui_i18n, "persist_locale", lambda _locale: None)
    app = FooterContractApp()
    async with app.run_test() as pilot:
        await _wait_for_footer_keys(pilot, app)
        key = app.query_one(FooterKey)
        identity = id(key)

        controller.switch_locale("zh-Hans")
        await pilot.pause()

        assert id(app.query_one(FooterKey)) == identity
        assert key.description == "Literal description"
        assert key.tooltip == "Literal tooltip"


@pytest.mark.asyncio
async def test_chrys_footer_resolves_tooltip_and_description_from_live_bundle(
    isolated_binding_registry: None,
) -> None:
    class _ExpandedLocalizer(Localizer):
        def render(self, reference: MessageRef) -> str:
            if reference.definition.key == _DESCRIPTION.key:
                return "Expanded active-bundle description"
            if reference.definition.key == _TOOLTIP.key:
                return "Expanded active-bundle tooltip"
            return super().render(reference)

    binding = localized_binding("x", "expanded", _DESCRIPTION, tooltip=_TOOLTIP)
    controller = LocaleController(Settings(locale="en"), localizer=_ExpandedLocalizer("en"))

    class FooterContractApp(App[None]):
        ENABLE_COMMAND_PALETTE = False
        BINDINGS: ClassVar[list[Binding]] = [binding]

        def compose(self) -> ComposeResult:
            yield ChrysFooter(locale_controller=controller)

    app = FooterContractApp()
    async with app.run_test() as pilot:
        await _wait_for_footer_keys(pilot, app)
        key = app.query_one(FooterKey)

        assert key.description == "Expanded active-bundle description"
        assert key.tooltip == "Expanded active-bundle tooltip"
        assert str(key.styles.width) == "auto"
        assert key.content_region.width == key.render().cell_len


@pytest.mark.asyncio
async def test_chrys_footer_unregistered_group_label_falls_through_as_literal() -> None:
    group = Binding.Group(description="[not-a-rich-tag]")
    controller = LocaleController(Settings(locale="en"))

    class FooterContractApp(App[None]):
        ENABLE_COMMAND_PALETTE = False
        BINDINGS: ClassVar[list[Binding]] = [
            Binding("x", "first", "First", group=group),
            Binding("y", "second", "Second", group=group),
        ]

        def compose(self) -> ComposeResult:
            yield ChrysFooter(locale_controller=controller)

    app = FooterContractApp()
    async with app.run_test() as pilot:
        await _wait_for_footer_keys(pilot, app)

        assert str(app.query_one(FooterLabel).render()) == "[not-a-rich-tag]"


def test_equal_binding_display_registration_is_idempotent(isolated_binding_registry: None) -> None:
    first = localized_binding("a", "do_thing", _DESCRIPTION)
    second = localized_binding("b", "do_thing", _EQUAL_DESCRIPTION)

    assert _DESCRIPTION is not _EQUAL_DESCRIPTION
    assert _DESCRIPTION == _EQUAL_DESCRIPTION
    assert first.id == second.id == _DESCRIPTION.key
    assert get_binding_display_spec(_DESCRIPTION.key) == BindingDisplaySpec(_DESCRIPTION)
    assert get_binding_display_provenance(_DESCRIPTION.key) == "do_thing"
    assert list(iter_binding_display_specs()) == [BindingDisplaySpec(_DESCRIPTION)]


def test_conflicting_binding_display_spec_raises(isolated_binding_registry: None) -> None:
    localized_binding("a", "do_thing", _DESCRIPTION)

    with pytest.raises(ValueError, match="Conflicting binding display registration"):
        localized_binding("b", "do_thing", _OTHER_DESCRIPTION)


def test_conflicting_binding_display_provenance_raises(isolated_binding_registry: None) -> None:
    localized_binding("a", "do_thing", _DESCRIPTION)

    with pytest.raises(ValueError, match="Conflicting binding display registration"):
        localized_binding("b", "do_something_else", _DESCRIPTION)


def test_unregistered_binding_id_fails_closed(isolated_binding_registry: None) -> None:
    binding = Binding("a", "do_thing", "Literal", id="tests.binding.missing")

    assert get_binding_display_spec(binding.id or "") is None
    assert resolve_binding_display(binding) is None


def test_binding_provenance_mismatch_fails_closed_with_content_free_diagnostic(
    isolated_binding_registry: None,
    caplog: pytest.LogCaptureFixture,
) -> None:
    localized_binding("a", "do_thing", _DESCRIPTION)
    mismatched = Binding("b", "other_action", "Literal", id=_DESCRIPTION.key)

    with caplog.at_level(logging.WARNING, logger=binding_display.__name__):
        assert resolve_binding_display(mismatched) is None

    assert [record.getMessage() for record in caplog.records] == [
        "Binding display provenance mismatch; using literal display text."
    ]
    assert _DESCRIPTION.key not in caplog.text
    assert "do_thing" not in caplog.text
    assert "other_action" not in caplog.text


def test_binding_tooltip_spec_is_stored_and_resolved(isolated_binding_registry: None) -> None:
    binding = localized_binding("a", "do_thing", _DESCRIPTION, tooltip=_TOOLTIP)

    assert binding.description == _DESCRIPTION.fallback
    assert binding.tooltip == _TOOLTIP.fallback
    assert resolve_binding_display(binding) == BindingDisplaySpec(_DESCRIPTION, _TOOLTIP)


def test_binding_display_enumeration_returns_a_snapshot(isolated_binding_registry: None) -> None:
    localized_binding("a", "do_thing", _DESCRIPTION)
    snapshot = iter_binding_display_specs()
    localized_binding("b", "second", _SECOND_DESCRIPTION)

    assert list(snapshot) == [BindingDisplaySpec(_DESCRIPTION)]
    assert set(iter_binding_display_specs()) == {
        BindingDisplaySpec(_DESCRIPTION),
        BindingDisplaySpec(_SECOND_DESCRIPTION),
    }


def _import_all_tui_modules() -> tuple[ModuleType, ...]:
    names = [module.name for module in pkgutil.walk_packages(tui.__path__, prefix=f"{tui.__name__}.")]
    return tuple(importlib.import_module(name) for name in names)


def test_importing_every_tui_module_populates_conflict_free_registry() -> None:
    modules = _import_all_tui_modules()

    assert modules
    assert get_binding_display_spec("tui.binding.sessions") is not None
    assert get_binding_display_spec("tui.binding.toggle_change_list") is not None


def test_every_declared_binding_id_has_matching_registered_provenance() -> None:
    declared: list[Binding] = []
    for module in _import_all_tui_modules():
        for _name, cls in inspect.getmembers(module, inspect.isclass):
            if cls.__module__ != module.__name__ or not issubclass(cls, Widget):
                continue
            for candidate in cls.__dict__.get("BINDINGS", ()):
                if isinstance(candidate, Binding) and candidate.id is not None:
                    declared.append(candidate)

    assert {binding.id for binding in declared} == {
        "tui.binding.agents",
        "tui.binding.back",
        "tui.binding.break_agent",
        "tui.binding.cancel",
        "tui.binding.chat_page_down",
        "tui.binding.chat_page_up",
        "tui.binding.chat_scroll_bottom",
        "tui.binding.close",
        "tui.binding.copy",
        "tui.binding.cycle_level",
        "tui.binding.delete",
        "tui.binding.help",
        "tui.binding.logs",
        "tui.binding.paste",
        "tui.binding.quit",
        "tui.binding.trajectory",
        "tui.binding.sessions",
        "tui.binding.settings",
        "tui.binding.shell_to_background",
        "tui.binding.sidebar",
        "tui.binding.themes",
        "tui.binding.toggle_change_list",
        "tui.binding.toggle_check",
        "tui.binding.toggle_graph",
        "tui.binding.toggle_split_view",
        "tui.binding.toggle_view",
        "tui.guides.cycle_language",
        "tui.workflow.binding.next_node",
        "tui.workflow.binding.previous_node",
        "tui.workflow.binding.open_node",
        "tui.workflow.binding.next_workflow",
        "tui.workflow.binding.previous_workflow",
        "tui.workflow.delete",
        "tui.workflow.node.retry",
    }
    for binding in declared:
        assert get_binding_display_provenance(binding.id or "", binding.action) == binding.action
        assert resolve_binding_display(binding) is not None


def test_session_delete_binding_preserves_english_and_renders_simplified_chinese() -> None:
    binding = next(binding for binding in sessions_screen.SessionsScreen.BINDINGS if binding.action == "delete_session")
    spec = resolve_binding_display(binding)

    assert binding.description == "Delete"
    assert spec is not None
    assert render_str(Localizer("en"), spec.description.bind()) == "Delete"
    assert render_str(Localizer("zh-Hans"), spec.description.bind()) == "删除"


def test_migrated_bindings_preserve_english_and_keymap_contracts() -> None:
    main_bindings = [binding for binding in main_screen.MainScreen.BINDINGS if binding.id is not None]
    diff_bindings = [binding for binding in diff_screen.DiffScreen.BINDINGS if binding.id is not None]

    # AIxCoding catalog: f4 binding retired — the provenance set and the
    # keymap contract table drop the models_config entry.
    assert [
        (
            binding.key,
            binding.action,
            binding.description,
            binding.show,
            binding.key_display,
            binding.priority,
            binding.tooltip,
            binding.system,
            binding.group,
        )
        for binding in (*main_bindings, *diff_bindings)
    ] == [
        ("ctrl+b", "interrupt", "Break", False, None, False, "", False, None),
        ("ctrl+t", "shell_to_background", "Move to background", False, None, False, "", False, None),
        ("f1", "sessions", "Sessions", True, None, True, "", False, None),
        ("f2", "agents_config", "Agents", True, None, True, "", False, None),
        ("f6", "show_log_viewer", "Logs", True, None, True, "", False, None),
        ("f8", "open_guide", "Help", True, None, True, "", False, None),
        ("f9", "pick_theme", "Themes", True, None, True, "", False, None),
        ("f10", "settings", "Settings", True, None, True, "", False, None),
        ("f12", "toggle_trajectory_dashboard", "Trajectory", True, None, True, "", False, None),
        ("ctrl+g", "toggle_sidebar", "Sidebar", True, None, True, "", False, None),
        ("pageup", "chat_page_up", "Chat Page Up", False, None, True, "", False, None),
        ("pagedown", "chat_page_down", "Chat Page Down", False, None, True, "", False, None),
        ("ctrl+end", "chat_scroll_bottom", "Chat Scroll Bottom", False, None, True, "", False, None),
        ("ctrl+q", "quit", "Quit", False, None, True, "", False, None),
        ("escape", "go_back", "Back", True, None, True, "", False, None),
        ("space", "toggle_view", "Toggle Split View", True, None, True, "", False, None),
        ("ctrl+g", "toggle_change_list", "Toggle Change List", True, None, True, "", False, None),
    ]
    for binding in (*main_bindings, *diff_bindings):
        spec = resolve_binding_display(binding)
        assert spec is not None
        assert binding.description == spec.description.fallback

    assert main_screen.MainScreen.BINDINGS[-1] is main_bindings[-1]
    assert ChrysApp.COMMAND_PALETTE_BINDING == "ctrl+q"
    assert main_bindings[-1].key == "ctrl+q"
