# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tests for the status-bar profile and model selectors."""

from __future__ import annotations

import pytest
from rich.cells import cell_len
from textual.app import App, ComposeResult
from textual.content import Content
from textual.widgets import Static

from chrys.app.tui.screens.main.model_indicator import ModelIndicatorState, compute_model_indicator_state
from chrys.app.tui.widgets.chrome.status_bar import StatusBar
from chrys.app.tui.workspace_text import WorkspaceIndicatorState
from chrys.foundation.events.types import RuntimeModelDetails
from chrys.foundation.i18n import Localizer
from tests.support.tui_helpers import click_when_settled, delay_resize_dispatch
from tests.support.waiting import wait_for


class _TagApp(App[None]):
    def __init__(self) -> None:
        super().__init__()
        self.profile_clicks = 0
        self.model_click_modes: list[str] = []
        self.busy_selectors: list[str] = []

    def compose(self) -> ComposeResult:
        yield StatusBar()

    def on_status_bar_profile_tag_clicked(self, _event: StatusBar.ProfileTagClicked) -> None:
        self.profile_clicks += 1

    def on_status_bar_model_tag_clicked(self, event: StatusBar.ModelTagClicked) -> None:
        self.model_click_modes.append(event.mode)

    def on_status_bar_selector_busy(self, event: StatusBar.SelectorBusy) -> None:
        self.busy_selectors.append(event.selector)


def _selectable_model_state(label: str = "Selectable Model") -> ModelIndicatorState:
    return ModelIndicatorState(
        label=label,
        tooltip=f"{label} · openai · responses · model-id · 200k context",
        mode="select",
        profile_id="model-profile",
        visible=True,
    )


@pytest.mark.asyncio
async def test_startup_loading_collapses_empty_selector_padding() -> None:
    async with _TagApp().run_test(size=(80, 10)) as pilot:
        bar = pilot.app.query_one(StatusBar)
        bar.show("Loading")
        await pilot.pause()

        selectors = bar.query_one(".status-selectors")
        body = bar.query_one(".status-body")
        indicator = bar.query_one("ChrysLoadingIndicator")
        assert selectors.display is False
        assert body.region.x == bar.content_region.x
        assert indicator.region.x == bar.content_region.x + 1

        bar.set_profile("Code Agent")
        bar.set_model(_selectable_model_state())
        bar.set_workspace(WorkspaceIndicatorState("Unspecified", Content("Unspecified"), "Workspace"))
        await pilot.pause()

        model_tag = bar.query_one("#model-tag", Static)
        assert selectors.display is True
        assert bar.query_one("#workspace-tag", Static).display is True
        assert body.region.x == model_tag.region.right + 1


@pytest.mark.asyncio
@pytest.mark.parametrize("width", [40, 50, 63])
async def test_compact_layout_prioritizes_buttons_and_run_status(width: int) -> None:
    async with _TagApp().run_test(size=(width, 10)) as pilot:
        bar = pilot.app.query_one(StatusBar)
        bar.set_profile("Code Agent")
        bar.set_model(_selectable_model_state("DSV4-Flash [Responses]"))
        bar.set_tool_info("RUNTIME")
        bar.show("Thinking")
        await pilot.pause()

        assert bar.has_class("-compact")
        assert bar.query_one("#agent-label").display is False
        assert bar.query_one("#model-label").display is False
        profile_tag = bar.query_one("#profile-tag", Static)
        model_tag = bar.query_one("#model-tag", Static)
        assert profile_tag.display is True
        assert model_tag.display is True
        assert cell_len(profile_tag.render().plain) <= 7
        assert cell_len(model_tag.render().plain) <= 10
        assert model_tag.region.x == profile_tag.region.right + 1
        assert bar.query_one("#status-tool-info").display is False

        strips = pilot.app.screen._compositor.render_strips()
        frame = "\n".join(strip.text for strip in strips)
        assert "Thinking" in frame
        assert "RUNTIME" not in frame


@pytest.mark.asyncio
async def test_compact_layout_hides_flash_runtime_trail_and_recovers_at_64_columns() -> None:
    async with _TagApp().run_test(size=(63, 10)) as pilot:
        bar = pilot.app.query_one(StatusBar)
        bar.set_profile("Code Agent")
        bar.set_model(_selectable_model_state())
        bar.flash("Completed", trail="RUNTIME")
        await pilot.pause()

        assert bar.query_one("#status-flash-trail").display is False
        frame = "\n".join(strip.text for strip in pilot.app.screen._compositor.render_strips())
        assert "Completed" in frame
        assert "RUNTIME" not in frame

        await pilot.resize_terminal(64, 10)
        # The Resize event that drops the compact tier lands asynchronously.
        await wait_for(
            lambda: not bar.has_class("-compact"),
            pilot=pilot,
            description="status bar leaves compact tier at 64 columns",
        )

        assert bar.query_one("#agent-label").display is True
        assert bar.query_one("#model-label").display is True
        assert bar.query_one("#status-flash-trail").display is True


@pytest.mark.asyncio
async def test_normal_layout_prioritizes_flash_status_over_runtime_details() -> None:
    async with _TagApp().run_test(size=(70, 10)) as pilot:
        bar = pilot.app.query_one(StatusBar)
        bar.set_profile("Q&A Agent")
        bar.set_model(_selectable_model_state("DSV4-Flash [Responses]"))
        primary = "Session restored: e4f9"
        bar.flash(primary, trail="8 tools · 1 skill · 9 hooks · 1 file")
        await pilot.pause()

        assert not bar.has_class("-compact")
        flash = bar.query_one("#status-flash", Static)
        flash_trail = bar.query_one("#status-flash-trail", Static)
        assert flash.region.width == len(primary) + 2
        assert flash_trail.region.x == flash.region.right
        frame = "\n".join(strip.text for strip in pilot.app.screen._compositor.render_strips())
        assert primary in frame


@pytest.mark.asyncio
async def test_agent_model_tag_is_literal_locked_and_reports_its_lock_even_while_busy() -> None:
    state = compute_model_indicator_state(
        RuntimeModelDetails(
            profile_id="bound",
            name="Bound [模型]",
            provider="openai",
            api_style="responses",
            model_id="bound-model",
            max_context_tokens=200_000,
            selection_source="agent",
        ),
        True,
        "Code [Agent]",
        Localizer("en"),
    )

    async with _TagApp().run_test(size=(80, 10)) as pilot:
        bar = pilot.app.query_one(StatusBar)
        bar.show("Thinking")
        bar.set_model(state)
        await pilot.pause()

        tag = bar.query_one("#model-tag", Static)
        assert tag.render().plain == "Bound [模型]"
        assert tag.has_class("-locked")
        assert tag.styles.pointer == "default"

        await click_when_settled(pilot, "#model-tag")
        await pilot.pause()

        assert pilot.app.model_click_modes == ["locked"]

        # The lock outlasts the run, so it wins over the busy notice.
        bar.agent_running = True
        await pilot.pause()
        await click_when_settled(pilot, "#model-tag")
        await pilot.pause()

        assert pilot.app.model_click_modes == ["locked", "locked"]
        assert pilot.app.busy_selectors == []


@pytest.mark.asyncio
async def test_profile_and_model_tags_share_all_transient_interaction_guards() -> None:
    async with _TagApp().run_test(size=(130, 10)) as pilot:
        bar = pilot.app.query_one(StatusBar)
        bar.show("Thinking")
        bar.set_profile("Code [Agent]")
        bar.set_model(_selectable_model_state())
        await pilot.pause()

        profile_tag = bar.query_one("#profile-tag", Static)
        model_tag = bar.query_one("#model-tag", Static)

        async def click_both(*, shown: bool = True) -> None:
            for selector in ("#profile-tag", "#model-tag"):
                if shown:
                    await click_when_settled(pilot, selector)
                else:
                    # Shell mode hides the selectors, so no settled click can land on them.
                    await pilot.click(selector)
            await pilot.pause()

        assert profile_tag.styles.pointer == "pointer"
        assert model_tag.styles.pointer == "pointer"
        await click_both()
        assert pilot.app.profile_clicks == 1
        assert pilot.app.model_click_modes == ["select"]

        guards = {
            "agent_running": lambda value: setattr(bar, "agent_running", value),
            "agent_loading": lambda value: setattr(bar, "agent_loading", value),
            "input_locked": lambda value: setattr(bar, "input_locked", value),
            "execution_busy": bar.set_execution_busy,
            "shell_mode": lambda value: setattr(bar, "shell_mode", value),
        }
        for guard, set_guard in guards.items():
            set_guard(True)
            await pilot.pause()
            assert profile_tag.styles.pointer == "default", guard
            assert model_tag.styles.pointer == "default", guard
            assert not profile_tag.has_class("-locked"), guard
            assert not model_tag.has_class("-locked"), guard
            await click_both(shown=guard != "shell_mode")
            assert pilot.app.profile_clicks == 1
            assert pilot.app.model_click_modes == ["select"]
            # A run explains the ignored click; shell mode hides the selectors instead.
            expected_busy = [] if guard == "shell_mode" else ["profile", "model"]
            assert pilot.app.busy_selectors == expected_busy, guard
            pilot.app.busy_selectors.clear()

            set_guard(False)
            await pilot.pause()
            assert profile_tag.styles.pointer == "pointer", guard
            assert model_tag.styles.pointer == "pointer", guard

        assert profile_tag.render().plain == "Code [Agent]"
        assert model_tag.render().plain == "Selectable Model"


@pytest.mark.asyncio
@pytest.mark.parametrize("late_resize", [False, True])
async def test_tag_truncation_is_cell_aware_and_recovers_after_resize(
    monkeypatch: pytest.MonkeyPatch, late_resize: bool
) -> None:
    profile_name = "配置[测试]Agent"
    model_name = "超长模型[中文]Alpha"

    async with _TagApp().run_test(size=(120, 10)) as pilot:
        if late_resize:
            delay_resize_dispatch(pilot.app, monkeypatch, 0.25)
        bar = pilot.app.query_one(StatusBar)
        bar.show("Thinking")
        bar.set_profile(profile_name, description="Agent description")
        bar.set_model(_selectable_model_state(model_name))
        bar.set_tool_info("RUNTIME")
        await pilot.pause()

        profile_tag = bar.query_one("#profile-tag", Static)
        model_tag = bar.query_one("#model-tag", Static)
        assert profile_tag.render().plain == profile_name
        assert model_tag.render().plain == model_name
        assert profile_name in profile_tag.tooltip.plain
        assert model_name in model_tag.tooltip.plain

        def frame() -> str:
            return "\n".join(strip.text for strip in pilot.app.screen._compositor.render_strips())

        # The App dispatches a resize from a debounce timer, which Pilot's
        # message barrier does not wait for: poll for the re-truncated tags and
        # the relaid-out frame instead.
        await pilot.resize_terminal(72, 10)
        await wait_for(
            lambda: (
                profile_tag.render().plain.endswith("…")
                and model_tag.render().plain.endswith("…")
                and "Thinking" in frame()
                and "RUNTIME" in frame()
            ),
            pilot=pilot,
            description="selector tags truncate and the status body fits at 72 columns",
        )
        assert cell_len(profile_tag.render().plain) <= 10
        assert cell_len(model_tag.render().plain) <= 12

        await pilot.resize_terminal(120, 10)
        await wait_for(
            lambda: profile_tag.render().plain == profile_name and model_tag.render().plain == model_name,
            pilot=pilot,
            description="selector tags show their full names again at 120 columns",
        )


@pytest.mark.asyncio
async def test_selectors_have_left_padding_gap_and_no_separator() -> None:
    async with _TagApp().run_test(size=(130, 10)) as pilot:
        bar = pilot.app.query_one(StatusBar)
        bar.show("Thinking")
        bar.set_workspace(WorkspaceIndicatorState("Unspecified", Content("Unspecified"), "Workspace"))
        bar.set_profile("Code Agent", description="Agent description")
        bar.set_model(_selectable_model_state())
        await pilot.pause()
        agent_label = bar.query_one("#agent-label", Static)
        profile_tag = bar.query_one("#profile-tag", Static)
        model_label = bar.query_one("#model-label", Static)
        model_tag = bar.query_one("#model-tag", Static)
        status_text = bar.query_one("#status-text", Static)
        assert agent_label.allow_select is False
        assert profile_tag.allow_select is False
        assert model_label.allow_select is False
        assert model_tag.allow_select is False
        assert agent_label.render().plain == "Agent"
        assert model_label.render().plain == "Model"
        assert agent_label.styles.padding.left == 1
        assert agent_label.styles.padding.right == 1
        assert model_label.styles.padding.left == 1
        assert model_label.styles.padding.right == 1
        assert agent_label.styles.background.a == pytest.approx(0.15)
        assert agent_label.styles.color.a == pytest.approx(0.9)
        assert agent_label.rich_style.bgcolor == model_label.rich_style.bgcolor
        assert profile_tag.rich_style.bgcolor == model_tag.rich_style.bgcolor
        assert profile_tag.rich_style.color == model_tag.rich_style.color
        assert agent_label.rich_style.bgcolor != profile_tag.rich_style.bgcolor
        workspace_label = bar.query_one("#workspace-label", Static)
        workspace_tag = bar.query_one("#workspace-tag", Static)
        assert workspace_label.region.x == bar.content_region.x + 1
        assert workspace_tag.region.x == workspace_label.region.right
        assert agent_label.region.x == workspace_tag.region.right + 1
        assert profile_tag.region.x == agent_label.region.right
        assert model_label.region.x == profile_tag.region.right + 1
        assert model_tag.region.x == model_label.region.right
        assert bar.query_one(".status-selectors").styles.padding.right == 1
        assert bar.query_one(".status-body").region.x == model_tag.region.right + 1
        assert status_text.region.x >= model_tag.region.right


@pytest.mark.asyncio
async def test_shell_mode_hides_both_selectors() -> None:
    async with _TagApp().run_test(size=(80, 10)) as pilot:
        bar = pilot.app.query_one(StatusBar)
        bar.set_profile("Code Agent")
        bar.set_model(_selectable_model_state())
        bar.flash("Shell mode")
        await pilot.pause()

        selectors = bar.query_one(".status-selectors")
        assert selectors.display is True

        bar.shell_mode = True
        await pilot.pause()
        assert selectors.display is False

        strips = pilot.app.screen._compositor.render_strips()
        frame = "\n".join(strip.text for strip in strips)
        assert "Shell mode" in frame
        assert "Code Agent" not in frame
        assert "Selectable Model" not in frame

        bar.shell_mode = False
        await pilot.pause()
        assert selectors.display is True


@pytest.mark.asyncio
async def test_clear_status_keeps_idle_selectors_without_status_content() -> None:
    async with _TagApp().run_test(size=(104, 10)) as pilot:
        bar = pilot.app.query_one(StatusBar)
        bar.set_profile("Code Agent")
        bar.set_model(_selectable_model_state())
        bar.set_tool_info("3 tools · 1 skill")
        bar.flash("Redundant idle status")
        bar.clear_status()
        await pilot.pause()

        assert bar.visible is True
        assert bar.query_one(".status-run").visible is True
        assert bar.query_one(".status-flash-bar").visible is False
        assert bar.query_one("ChrysLoadingIndicator").visible is False
        assert bar.query_one("#status-text").visible is False
        assert bar.query_one("#status-trail").visible is False
        assert bar.query_one(".status-selectors").display is True
        tool_info = bar.query_one("#status-tool-info", Static)
        assert tool_info.visible is True
        assert tool_info.render().plain == "3 tools · 1 skill"
        assert tool_info.region.right == bar.content_region.right
        assert tool_info.styles.pointer == "pointer"
        assert tool_info.tooltip is not None
        assert tool_info.tooltip.plain == "Click for details"
        strips = pilot.app.screen._compositor.render_strips()
        frame = "\n".join(strip.text for strip in strips)
        assert "Code Agent" in frame
        assert "3 tools · 1 skill" in frame
        assert "Redundant idle status" not in frame


@pytest.mark.asyncio
async def test_flash_after_idle_does_not_leave_spinner_overlapping_text() -> None:
    async with _TagApp().run_test(size=(80, 10)) as pilot:
        bar = pilot.app.query_one(StatusBar)
        bar.set_profile("Code Agent")
        bar.set_tool_info("3 tools")
        bar.clear_status()

        restored = "会话已恢复：abc123"  # noqa: RUF001
        bar.flash(restored)
        await pilot.pause()

        assert bar.query_one("ChrysLoadingIndicator").visible is False
        strips = pilot.app.screen._compositor.render_strips()
        frame = "\n".join(strip.text for strip in strips)
        assert restored in frame
