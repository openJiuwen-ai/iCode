# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tests for StatusBar: localization of status/trail/tooltip, layout and flash trails, snapshot restore, and details-click messages."""

from __future__ import annotations

from collections.abc import Callable

import pytest
from textual import on
from textual.app import App, ComposeResult
from textual.timer import Timer, TimerCallback
from textual.widgets import Static

from chrys.app.tui import i18n as tui_i18n
from chrys.app.tui.i18n import LocaleController, LocaleSwitchStatus
from chrys.app.tui.widgets import ChrysLoadingIndicator
from chrys.app.tui.widgets.chrome.status_bar import (
    STATUS_INTERRUPTED,
    STATUS_SESSION_RESTORED,
    STATUS_THINKING,
    StatusBar,
    TrailCount,
)
from chrys.foundation.config.settings import Settings
from chrys.foundation.events.types import (
    AgentRuntimeDetails,
    RuntimeHookDetails,
    RuntimeHookSourceDetails,
)
from tests.support.tui_helpers import (
    WidgetApp,
    make_click,
)
from tests.support.waiting import wait_for


class _DetailsClickApp(App):
    """StatusBar host that records the DetailsClicked messages it receives."""

    def __init__(self) -> None:
        self.details_clicks: list[StatusBar.DetailsClicked] = []
        super().__init__()

    def compose(self) -> ComposeResult:
        yield StatusBar()

    @on(StatusBar.DetailsClicked)
    def _record_details_click(self, event: StatusBar.DetailsClicked) -> None:
        self.details_clicks.append(event)


async def test_status_bar_status_reactive_refreshes_text() -> None:
    async with WidgetApp(StatusBar).run_test() as pilot:
        status_bar = pilot.app.query_one(StatusBar)
        status_bar.status = "Thinking"
        await pilot.pause()

        assert pilot.app.query_one("#status-text", Static).render().plain == "Thinking"


@pytest.mark.parametrize(
    ("elapsed", "english", "chinese"),
    [
        (61, "1m 1s", "1分 1秒"),
        (3600, "1h 0m 0s", "1小时 0分 0秒"),
        (30030, "8h 20m 30s", "8小时 20分 30秒"),
        (86399, "23h 59m 59s", "23小时 59分 59秒"),
        (86400, "1d 0h 0m 0s", "1天 0小时 0分 0秒"),
        (102050, "1d 4h 20m 50s", "1天 4小时 20分 50秒"),
    ],
)
async def test_status_bar_relocalizes_status_tool_trail_tooltip_and_literal_payload(
    monkeypatch: pytest.MonkeyPatch,
    elapsed: int,
    english: str,
    chinese: str,
) -> None:
    from types import SimpleNamespace

    from chrys.app.tui.screens.main.model_indicator import ModelIndicatorState
    from chrys.app.tui.screens.main.runtime_info import RegistryRuntimeInfoProvider
    from chrys.app.tui.widgets.chrome import status_bar as status_bar_module

    controller = LocaleController(Settings(locale="en"))
    monkeypatch.setattr(tui_i18n, "persist_locale", lambda _locale: None)
    now = [100.0]
    monkeypatch.setattr(status_bar_module, "time", SimpleNamespace(monotonic=lambda: now[0]))
    status_bar: StatusBar | None = None

    async with WidgetApp(lambda: StatusBar(locale_controller=controller)).run_test() as pilot:
        status_bar = pilot.app.query_one(StatusBar)
        assert status_bar in controller._surfaces
        status_bar.set_profile("Code Agent")
        status_bar.set_model(
            ModelIndicatorState(
                label="Test Model",
                tooltip="",
                mode="select",
                profile_id="test-model",
                visible=True,
            )
        )
        status_bar.start_run()
        now[0] += elapsed
        status_bar.add_tool_call()
        runtime_info = RegistryRuntimeInfoProvider(  # type: ignore[arg-type]
            SimpleNamespace(agent_registry=None)
        )
        status_bar.set_tool_info(
            runtime_info.format_tool_info(
                ["read_file", "write_file"],
                ["review"],
                memory_files=["AGENTS.md"],
                runtime_details=AgentRuntimeDetails(
                    hook_sources=[
                        RuntimeHookSourceDetails(
                            scope="project",
                            hooks=[
                                RuntimeHookDetails(id="guard", enabled=True),
                                RuntimeHookDetails(id="notify", enabled=True),
                                RuntimeHookDetails(id="disabled", enabled=False),
                            ],
                        )
                    ]
                ),
            )
        )
        status_bar.show(STATUS_THINKING.bind())

        assert status_bar.query_one("#agent-label", Static).render().plain == "Agent"
        assert status_bar.query_one("#model-label", Static).render().plain == "Model"
        assert status_bar.query_one("#status-text", Static).render().plain == "Thinking"
        assert status_bar.query_one("#status-trail", Static).render().plain == f"  ({english} · 1 tool call)"
        assert (
            status_bar.query_one("#status-tool-info", Static).render().plain == "2 tools · 1 skill · 2 hooks · 1 file"
        )

        assert controller.switch_locale("zh-Hans").status is LocaleSwitchStatus.EFFECTIVE_CHANGED
        assert status_bar.query_one("#agent-label", Static).render().plain == "智能体"
        assert status_bar.query_one("#model-label", Static).render().plain == "模型"
        assert status_bar.query_one("#status-text", Static).render().plain == "正在思考"
        assert status_bar.query_one("#status-trail", Static).render().plain == f"  ({chinese} · 1 次工具调用)"
        tool_info = status_bar.query_one("#status-tool-info", Static)
        assert tool_info.render().plain == "2 个工具 · 1 个 Skill · 2 个钩子 · 1 个文件"
        assert tool_info.tooltip is not None
        assert tool_info.tooltip.plain == "点击查看详情"

        now[0] += 1
        status_bar.show("provider payload")
        assert controller.switch_locale("en").status is LocaleSwitchStatus.EFFECTIVE_CHANGED
        assert status_bar.query_one("#status-text", Static).render().plain == "provider payload"

    assert status_bar is not None
    assert status_bar not in controller._surfaces


async def test_status_bar_relocalizes_active_flash_and_snapshot_restore(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    controller = LocaleController(Settings(locale="en"))
    monkeypatch.setattr(tui_i18n, "persist_locale", lambda _locale: None)

    async with WidgetApp(lambda: StatusBar(locale_controller=controller)).run_test() as pilot:
        status_bar = pilot.app.query_one(StatusBar)
        status_bar.set_tool_info((STATUS_SESSION_RESTORED.bind(session_id="abc123"),))
        status_bar.show(STATUS_THINKING.bind())
        snapshot = status_bar.snapshot()

        status_bar.flash(STATUS_INTERRUPTED.bind(), caution=True)
        assert status_bar.query_one("#status-flash", Static).render().plain == "Interrupted by user"

        assert controller.switch_locale("zh-Hans").status is LocaleSwitchStatus.EFFECTIVE_CHANGED
        assert status_bar.query_one("#status-flash", Static).render().plain == "已由用户中断"

        status_bar.restore(snapshot)
        await pilot.pause()
        assert status_bar.query_one("#status-text", Static).render().plain == "正在思考"
        assert status_bar.query_one("#status-tool-info", Static).render().plain == "会话已恢复：abc123"  # noqa: RUF001


@pytest.mark.parametrize(
    ("elapsed", "english", "chinese"),
    [(61, "1m 1s", "1分 1秒"), (102050, "1d 4h 20m 50s", "1天 4小时 20分 50秒")],
)
async def test_status_bar_completed_flash_restores_localized_elapsed_from_snapshot(
    monkeypatch: pytest.MonkeyPatch,
    elapsed: int,
    english: str,
    chinese: str,
) -> None:
    from types import SimpleNamespace

    from chrys.app.tui.widgets.chrome import status_bar as status_bar_module

    controller = LocaleController(Settings(locale="en"))
    monkeypatch.setattr(tui_i18n, "persist_locale", lambda _locale: None)
    now = [100.0]
    monkeypatch.setattr(status_bar_module, "time", SimpleNamespace(monotonic=lambda: now[0]))

    async with WidgetApp(lambda: StatusBar(locale_controller=controller)).run_test() as pilot:
        status_bar = pilot.app.query_one(StatusBar)
        status_bar.start_run()
        now[0] += elapsed
        status_bar.flash_completed()
        snapshot = status_bar.snapshot()
        assert status_bar.query_one("#status-flash", Static).render().plain == f"Completed in {english}"

        status_bar.flash("shell payload", warn=True)
        assert controller.switch_locale("zh-Hans").status is LocaleSwitchStatus.EFFECTIVE_CHANGED
        status_bar.restore(snapshot)

        assert status_bar.query_one("#status-flash", Static).render().plain == f"已完成，用时 {chinese}"  # noqa: RUF001


async def test_status_bar_localizes_tool_count_choices_and_elapsed_formats(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from chrys.app.tui.widgets.chrome import status_bar as status_bar_module

    controller = LocaleController(Settings(locale="zh-Hans"))
    now = [117.0]
    monkeypatch.setattr(status_bar_module.time, "monotonic", lambda: now[0])

    async with WidgetApp(lambda: StatusBar(locale_controller=controller)).run_test() as pilot:
        status_bar = pilot.app.query_one(StatusBar)
        status_bar._start_time = 100.0
        status_bar._tool_count = 1
        status_bar.show(STATUS_THINKING.bind())
        assert status_bar.query_one("#status-trail", Static).render().plain == "  (17秒 · 1 次工具调用)"

        now[0] = 221.0
        status_bar._tool_count = 2
        status_bar.refresh_localization()
        assert status_bar.query_one("#status-trail", Static).render().plain == "  (2分 1秒 · 2 次工具调用)"


async def test_status_bar_show_hide() -> None:
    async with WidgetApp(StatusBar).run_test() as pilot:
        sb = pilot.app.query_one(StatusBar)
        assert sb.styles.padding.left == 0
        assert sb.query_one(".status-selectors").styles.padding.right == 1
        assert sb.query_one(".status-selectors").display is False
        assert sb.query_one(".status-body").styles.padding.left == 0
        assert sb.shown is False
        assert sb.visible is False

        sb.show("thinking...")
        assert sb.shown is True
        assert sb.visible is True
        assert sb.status == "thinking..."
        assert sb.query_one(".status-run").visible is True
        assert sb.query_one(".status-flash-bar").visible is False

        sb.flash("done")
        assert sb.query_one(".status-run").visible is False
        assert sb.query_one(".status-flash-bar").visible is True

        sb.hide()
        assert sb.shown is False
        assert sb.visible is False
        assert sb.query_one(".status-run").visible is False
        assert sb.query_one(".status-flash-bar").visible is False


async def test_the_run_status_changes_at_most_once_per_interval_and_a_repeat_repaints_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from types import SimpleNamespace

    from chrys.app.tui.widgets.chrome import status_bar as status_bar_module

    now = [100.0]
    monkeypatch.setattr(status_bar_module, "time", SimpleNamespace(monotonic=lambda: now[0]))

    async with WidgetApp(StatusBar).run_test() as pilot:
        sb = pilot.app.query_one(StatusBar)
        text = sb.query_one("#status-text", Static)
        trail = sb.query_one("#status-trail", Static)
        # The test fires each paced change itself, so nothing but the timer can deliver it.
        timers: list[tuple[float, TimerCallback]] = []

        def recording_set_timer(
            delay: float, callback: TimerCallback | None = None, *, name: str | None = None, pause: bool = False
        ) -> Timer:
            assert callback is not None
            timers.append((delay, callback))
            return Timer(sb, delay, name=name, callback=callback, pause=pause)

        monkeypatch.setattr(sb, "set_timer", recording_set_timer)
        sb.start_run()
        sb.show("Thinking")
        assert text.render().plain == "Thinking"

        painted: list[object] = []
        real_update = text.update

        def recording_update(*args: object, **kwargs: object) -> None:
            painted.append(args[0])
            real_update(*args, **kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr(text, "update", recording_update)
        for _ in range(3):
            sb.show("Thinking")
        assert painted == []
        assert timers == []

        # A new count shows at once though the status it comes with repeats.
        sb.add_tool_call()
        sb.show("Thinking")
        assert trail.render().plain == "  (0s · 1 tool call)"
        assert painted == []

        # Within the interval: the newest change waits for the one timer.
        now[0] += 0.05
        sb.show("Running: read_file")
        sb.show("Running: grep")
        assert sb.status == "Thinking"
        assert sb.snapshot()["status"] == "Running: grep"
        assert [delay for delay, _ in timers] == [pytest.approx(0.1)]
        timers[0][1]()
        assert text.render().plain == "Running: grep"

        now[0] += 1
        sb.show("Thinking")
        assert text.render().plain == "Thinking"
        assert len(timers) == 1

        # Showing the current status again drops a change still waiting: it went stale.
        now[0] += 0.05
        sb.show("Running: read_file")
        sb.show("Thinking")
        timers[1][1]()
        assert text.render().plain == "Thinking"
        assert sb.snapshot()["status"] == "Thinking"

        # A flash replaces a change still waiting for its interval.
        sb.show("Running: read_file")
        sb.flash("Done")
        timers[2][1]()
        assert sb.status == ""
        assert sb.query_one(".status-flash-bar").visible is True


async def test_status_bar_flash_text_gets_full_row_when_trail_is_empty() -> None:
    """The auto-width trail must leave the whole row to the flash message."""

    async with WidgetApp(StatusBar).run_test(size=(80, 6)) as pilot:
        sb = pilot.app.query_one(StatusBar)
        sb.flash("M" * 55 + "TAIL-END-MARKER")
        await pilot.pause()
        await pilot.pause()
        strips = pilot.app.screen._compositor.render_strips()
        frame = "\n".join(strip.text for strip in strips)
        # A half-row flash box (the old 1fr/1fr split) clips this tail.
        assert "TAIL-END-MARKER" in frame


async def test_status_bar_trail_shows_the_recent_output_speed_only_while_output_arrives(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from types import SimpleNamespace

    from chrys.app.tui.widgets.chrome import status_bar as status_bar_module

    now = [100.0]
    monkeypatch.setattr(status_bar_module, "time", SimpleNamespace(monotonic=lambda: now[0]))

    def _trail(status_bar: StatusBar) -> str:
        status_bar._refresh_trail()
        return status_bar.query_one("#status-trail", Static).render().plain

    async with WidgetApp(StatusBar).run_test() as pilot:
        status_bar = pilot.app.query_one(StatusBar)
        status_bar.start_run()
        assert "tok/s" not in _trail(status_bar)

        # 20 words a second, at one token each, over 2 s.
        for _ in range(3):
            status_bar.note_output_text("word " * 20)
            now[0] += 1.0
        now[0] -= 1.0
        units = 2 * status_bar_module._OUTPUT_TOKENIZER.count_token_units("word " * 20)
        expected = round(units / status_bar_module.TOKEN_UNITS_PER_TOKEN / 2.0)
        assert f" {expected} tok/s" in _trail(status_bar)

        # A pause in the stream (a tool running) hides it rather than showing a stale rate.
        now[0] += status_bar_module._OUTPUT_SPEED_IDLE_SECONDS + 0.1
        assert "tok/s" not in _trail(status_bar)


async def test_status_bar_output_speed_needs_a_span_and_forgets_old_samples(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from types import SimpleNamespace

    from chrys.app.tui.widgets.chrome import status_bar as status_bar_module

    now = [100.0]
    monkeypatch.setattr(status_bar_module, "time", SimpleNamespace(monotonic=lambda: now[0]))

    async with WidgetApp(StatusBar).run_test() as pilot:
        status_bar = pilot.app.query_one(StatusBar)
        status_bar.start_run()

        # Text that arrives all at once, as a response that was not streamed does, has no rate.
        status_bar.note_output_text("word " * 500)
        now[0] += status_bar_module._OUTPUT_SPEED_MIN_SPAN_SECONDS / 2
        status_bar.note_output_text("word " * 500)
        assert status_bar._output_speed() is None
        now[0] += status_bar_module._OUTPUT_SPEED_MIN_SPAN_SECONDS / 2
        status_bar.note_output_text("word")
        assert status_bar._output_speed() is not None

        # What arrived before the window no longer counts.
        now[0] += status_bar_module._OUTPUT_SPEED_WINDOW_SECONDS
        status_bar.note_output_text("word")
        now[0] += 2.0
        status_bar.note_output_text("word")
        slow = status_bar._output_speed()
        assert slow is not None
        assert slow < 5

        # A new run starts from nothing.
        status_bar.start_run()
        assert status_bar._output_speed() is None


async def test_status_bar_output_speed_leaves_out_what_arrived_before_its_span(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from types import SimpleNamespace

    from chrys.app.tui.widgets.chrome import status_bar as status_bar_module

    now = [100.0]
    monkeypatch.setattr(status_bar_module, "time", SimpleNamespace(monotonic=lambda: now[0]))

    async with WidgetApp(StatusBar).run_test() as pilot:
        status_bar = pilot.app.query_one(StatusBar)
        status_bar.start_run()

        # Two whole responses, as ones that were not streamed land: the first took its time before the span began.
        status_bar.note_output_text("word " * 3000)
        now[0] += 2.0
        status_bar.note_output_text("word " * 100)

        units = status_bar_module._OUTPUT_TOKENIZER.count_token_units("word " * 100)
        assert status_bar._output_speed() == round(units / status_bar_module.TOKEN_UNITS_PER_TOKEN / 2.0)


async def test_status_bar_noting_output_paints_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    """A delta is per-token work; the trail picks the rate up on its one-second tick."""

    async with WidgetApp(StatusBar).run_test() as pilot:
        status_bar = pilot.app.query_one(StatusBar)
        status_bar.start_run()
        await pilot.pause()
        trail = status_bar.query_one("#status-trail", Static)
        before = trail.render().plain
        updates: list[object] = []
        monkeypatch.setattr(trail, "update", lambda *args, **kwargs: updates.append(args))

        status_bar.note_output_text("word " * 50)

        assert updates == []
        assert trail.render().plain == before


async def test_status_bar_run_trail_hugs_status_text() -> None:
    """The elapsed/tool-call trail sits right after the status label.

    A flexible status-text width strands the trail mid-row, far from the
    label it annotates; the tool info keeps the right edge of the row.
    """

    async with WidgetApp(StatusBar).run_test(size=(100, 6)) as pilot:
        sb = pilot.app.query_one(StatusBar)
        sb.start_run()
        sb.add_tool_call()
        sb.set_tool_info("MODEL-MARKER")
        sb.show("Running: explore_agent")
        await pilot.pause()
        await pilot.pause()

        text_widget = sb.query_one("#status-text", Static)
        trail_widget = sb.query_one("#status-trail", Static)
        assert text_widget.region.width == len("Running: explore_agent")
        assert trail_widget.region.x == text_widget.region.right

        strips = pilot.app.screen._compositor.render_strips()
        row = next(strip.text for strip in strips if "Running:" in strip.text)
        # Trail text is adjacent (its two leading spaces are part of it) and
        # the persistent tool info stays right-aligned.
        assert "Running: explore_agent  (" in row
        assert "tool call)" in row
        assert row.rstrip().endswith("MODEL-MARKER")


async def test_status_bar_warn_flash_suppresses_tool_trail() -> None:
    """Warn flashes (shell mode instructions) get the row to themselves."""

    async with WidgetApp(StatusBar).run_test(size=(80, 6)) as pilot:
        sb = pilot.app.query_one(StatusBar)
        sb.set_tool_info("TOOLTRAIL-MARKER")

        sb.flash("regular note")
        await pilot.pause()
        await pilot.pause()
        strips = pilot.app.screen._compositor.render_strips()
        frame = "\n".join(strip.text for strip in strips)
        assert "TOOLTRAIL-MARKER" in frame

        sb.flash("shell mode notice", warn=True)
        await pilot.pause()
        await pilot.pause()
        assert sb.query_one("#status-flash").region.width == sb.query_one(".status-body").content_region.width
        strips = pilot.app.screen._compositor.render_strips()
        frame = "\n".join(strip.text for strip in strips)
        assert "TOOLTRAIL-MARKER" not in frame


async def test_status_bar_snapshot_restore_preserves_run_state() -> None:
    async with WidgetApp(StatusBar).run_test() as pilot:
        sb = pilot.app.query_one(StatusBar)
        sb.set_tool_info("3 tools")
        sb.start_run()
        sb.add_tool_call()
        sb.show("Thinking")
        snapshot = sb.snapshot()

        sb.set_tool_info("")
        sb.start_run()
        sb.show("Restoring Session")
        sb.restore(snapshot)
        await pilot.pause()

        assert sb.status == "Thinking"
        assert sb._start_time == snapshot["start_time"]
        assert sb._tool_count == snapshot["tool_count"]
        assert sb._tool_trail == "3 tools"


async def test_status_bar_idle_restore_clears_shell_flash_before_tool_info_updates() -> None:
    async with WidgetApp(StatusBar).run_test() as pilot:
        sb = pilot.app.query_one(StatusBar)
        sb.set_profile("Code Agent")
        sb.set_tool_info("Original runtime")
        sb.clear_status()
        snapshot = sb.snapshot()

        sb.flash("Shell mode", warn=True)
        sb.restore(snapshot)
        sb.set_tool_info("Updated runtime")
        await pilot.pause()

        assert sb._flash is None
        assert not sb.has_class("-warn")
        tool_info = sb.query_one("#status-tool-info", Static)
        assert tool_info.visible is True
        assert tool_info.render().plain == "Updated runtime"


async def test_status_bar_selectors_only_restore_does_not_poison_next_run_visibility() -> None:
    async with WidgetApp(StatusBar).run_test() as pilot:
        sb = pilot.app.query_one(StatusBar)
        sb.hide()
        sb.set_profile("Code Agent")
        snapshot = sb.snapshot()
        assert snapshot["content_shown"] is False
        assert snapshot["idle_shown"] is False

        sb.flash("Shell mode", warn=True)
        sb.restore(snapshot)
        sb.show("Thinking")
        await pilot.pause()

        assert sb.query_one("ChrysLoadingIndicator").visible is True
        assert sb.query_one("#status-text", Static).visible is True
        assert sb.query_one("#status-text", Static).render().plain == "Thinking"


async def test_status_bar_hidden_restore_recovers_configured_idle_chrome() -> None:
    async with WidgetApp(StatusBar).run_test() as pilot:
        sb = pilot.app.query_one(StatusBar)
        sb.set_profile("Code Agent")
        sb.hide()
        snapshot = sb.snapshot()

        sb.flash("Shell mode", warn=True)
        sb.restore(snapshot)
        await pilot.pause()

        assert sb.visible is True
        assert sb._flash is None
        assert sb.query_one("#profile-tag", Static).render().plain == "Code Agent"
        assert sb.query_one("#status-text", Static).visible is False


async def test_status_bar_tooltip_is_click_hint_only() -> None:
    async with WidgetApp(StatusBar).run_test() as pilot:
        sb = pilot.app.query_one(StatusBar)
        sb.set_tool_info("3 tools")

        sb.show("Loading")

        tool_info = sb.query_one("#status-tool-info", Static)
        assert tool_info.tooltip is not None
        assert tool_info.tooltip.plain == "Click for details"


async def test_status_bar_empty_runtime_trail_removes_mouse_details_entry() -> None:
    from types import SimpleNamespace

    from chrys.app.tui.screens.main.runtime_info import RegistryRuntimeInfoProvider

    runtime_info = RegistryRuntimeInfoProvider(  # type: ignore[arg-type]
        SimpleNamespace(agent_registry=None)
    )
    trail = runtime_info.format_tool_info(
        [],
        [],
        runtime_details=AgentRuntimeDetails(
            hook_sources=[
                RuntimeHookSourceDetails(
                    scope="global",
                    hooks=[RuntimeHookDetails(id="disabled", enabled=False)],
                )
            ]
        ),
    )
    assert trail == ()

    app = _DetailsClickApp()
    async with app.run_test() as pilot:
        sb = pilot.app.query_one(StatusBar)
        sb.set_tool_info(trail)
        sb.show("Loading")
        await pilot.pause()

        tool_info = sb.query_one("#status-tool-info", Static)
        assert tool_info.render().plain == ""
        assert tool_info.styles.pointer == "default"
        assert tool_info.tooltip is None

        event = make_click(sb, screen_x=tool_info.region.x, screen_y=tool_info.region.y)
        sb.on_click(event)
        await pilot.pause()

    assert event._no_default_action is False
    assert event._stop_propagation is False
    assert app.details_clicks == []


def test_runtime_tool_count_takes_in_context_tools() -> None:
    """The trail counts every tool the Tools tab lists, the context management ones included."""
    from types import SimpleNamespace

    from chrys.app.tui.screens.main.runtime_info import RegistryRuntimeInfoProvider
    from chrys.foundation.i18n.formatting import format_message

    runtime_info = RegistryRuntimeInfoProvider(  # type: ignore[arg-type]
        SimpleNamespace(agent_registry=None)
    )
    context_tools = AgentRuntimeDetails(
        context_tools=["compress_context", "recall_context", "list_compressed_contexts"]
    )

    trail = runtime_info.format_tool_info(["read_file", "sleep"], [], runtime_details=context_tools)
    alone = runtime_info.format_tool_info([], [], runtime_details=context_tools)

    assert [format_message(part) for part in trail] == ["5 tools"]
    assert [format_message(part) for part in alone] == ["3 tools"]


async def test_status_bar_set_tool_info_refreshes_visible_flash_trail() -> None:
    async with WidgetApp(StatusBar).run_test() as pilot:
        sb = pilot.app.query_one(StatusBar)
        sb.flash("Completed in 1s")
        sb.set_tool_info("11 tools · 1 skill")
        await pilot.pause()

        flash_trail = sb.query_one("#status-flash-trail", Static)
        assert flash_trail.render().plain == "11 tools · 1 skill"


@pytest.mark.parametrize(
    ("reveal_trail", "target_id"),
    [
        pytest.param(lambda sb: sb.show("Loading"), "#status-tool-info", id="tool-info"),
        pytest.param(lambda sb: sb.flash("Profile: Code", trail="3 tools"), "#status-flash-trail", id="flash-trail"),
    ],
)
async def test_status_bar_posts_details_clicked(reveal_trail: Callable[[StatusBar], None], target_id: str) -> None:
    """Clicking either trail rendering of the tool info posts DetailsClicked and consumes the click."""
    app = _DetailsClickApp()
    async with app.run_test() as pilot:
        sb = pilot.app.query_one(StatusBar)
        sb.set_tool_info("3 tools")
        reveal_trail(sb)
        await pilot.pause()

        target = sb.query_one(target_id, Static)
        event = make_click(sb, screen_x=target.region.x, screen_y=target.region.y)
        sb.on_click(event)
        await pilot.pause()

    assert event._no_default_action is True
    assert event._stop_propagation is True
    assert len(app.details_clicks) == 1


async def test_status_bar_spinner_ticks_only_while_it_is_painted() -> None:
    """The bar swaps faces through layout-free visibility flips; their Show/Hide park and restart the spinner's timer."""
    async with WidgetApp(StatusBar).run_test() as pilot:
        sb = pilot.app.query_one(StatusBar)
        indicator = sb.query_one(ChrysLoadingIndicator)
        timer = indicator._auto_refresh_timer
        assert timer is not None
        assert indicator.visible is False
        await wait_for(lambda: not timer._active.is_set(), pilot=pilot, description="the hidden spinner is parked")

        sb.show("Thinking")
        assert indicator.visible is True
        await wait_for(timer._active.is_set, pilot=pilot, description="the running face animates the spinner")

        sb.flash("Done")
        assert indicator.visible is False
        await wait_for(lambda: not timer._active.is_set(), pilot=pilot, description="the flash face parks the spinner")

        sb.show("Thinking again")
        await wait_for(timer._active.is_set, pilot=pilot, description="the spinner animates again")
        sb.hide()
        await wait_for(lambda: not timer._active.is_set(), pilot=pilot, description="the hidden bar parks the spinner")


@pytest.mark.parametrize(
    ("reveal", "target_id", "room"),
    [
        # Status text after the 8-cell spinner; the trail's 2 cells of padding.
        pytest.param(StatusBar.show, "#status-tool-info", 70, id="running"),
        # The flash with its 2 cells of padding; the trail's 2.
        pytest.param(StatusBar.flash, "#status-flash-trail", 76, id="flash"),
    ],
)
async def test_a_trail_too_wide_keeps_its_count_short_at_the_end(
    reveal: Callable[[StatusBar, str], None], target_id: str, room: int
) -> None:
    async with WidgetApp(StatusBar).run_test(size=(80, 3)) as pilot:
        sb = pilot.app.query_one(StatusBar)
        sb.set_tool_info(("13 tools", "1 skill", TrailCount("1 background task", "1 task")))
        trail = sb.query_one(target_id, Static)

        async def shown_at(width: int) -> str:
            reveal(sb, "x" * (room - width))
            await wait_for(lambda: trail.size.width == width, pilot=pilot, description=f"the trail {width} wide")
            return trail.render().plain

        assert await shown_at(40) == "13 tools · 1 skill · 1 background task"
        assert await shown_at(27) == "13 tools · 1 skill · 1 task"
        assert await shown_at(24) == "13 tools · 1 s… · 1 task"
        assert await shown_at(10) == "… · 1 task"
        assert await shown_at(9) == "1 task"
        assert await shown_at(40) == "13 tools · 1 skill · 1 background task"
        assert str(trail.content) == "13 tools · 1 skill · 1 background task"

        sb.set_tool_info((TrailCount("2 background tasks", "2 tasks"),))
        assert await shown_at(10) == "2 tasks"
