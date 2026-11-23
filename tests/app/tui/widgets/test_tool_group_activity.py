# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Live tool-group activity header and default-collapse behavior."""

from __future__ import annotations

from types import ModuleType

import pytest
from textual.app import App, ComposeResult
from textual.pilot import Pilot
from textual.screen import Screen
from textual.widgets import Static

from chrys.app.tui.i18n import LocaleController, LocaleSwitchStatus
from chrys.app.tui.widgets.chat import tool_call as tool_call_module
from chrys.app.tui.widgets.chat.live_process_row import LiveProcessRow
from chrys.app.tui.widgets.chat.panel import ChatPanel
from chrys.app.tui.widgets.chat.renderers import sleep as sleep_module
from chrys.app.tui.widgets.chat.renderers.sleep import SleepToolCall
from chrys.app.tui.widgets.chat.renderers.sub_agent import SubAgentToolCall
from chrys.app.tui.widgets.chat.tool_call import ToolGroup, ToolGroupTitle, _tool_activity_reference
from chrys.app.tui.widgets.loading import ChrysLoadingIndicator
from chrys.foundation.config.settings import Settings
from chrys.foundation.i18n.formatting import format_message
from chrys.foundation.tool_kinds import (
    KIND_ASK_USER,
    KIND_FILESYSTEM_READ,
    KIND_FILESYSTEM_WRITE,
    KIND_SEARCH,
    KIND_SHELL,
    KIND_SKILL,
    KIND_SLEEP,
    KIND_SUB_AGENT,
)
from tests.support.pilot_barrier import screen_is_settled
from tests.support.tui_helpers import ChatPanelApp, click_when_settled, title_runs, title_text
from tests.support.waiting import wait_for, wait_until


def _painted(app: App) -> str:
    """What the screen paints, as the compositor composes it from each widget's rendered lines."""
    return "\n".join(strip.text for strip in app.screen._compositor.render_strips())


def _title(group: ToolGroup) -> str:
    return title_text(group.query_one(ToolGroupTitle))


def _show_process(panel: ChatPanel) -> None:
    row = panel.query_one(LiveProcessRow)
    if row.collapsed:
        panel._toggle_live_process_row(row)


async def _running(pilot: Pilot[None], group: ToolGroup) -> bool:
    """Whether the title shows the group running: the dot before it, and its refresh timer ticking.

    The timer ticks once the title's Show is handled: a tick before then, the title not yet composited, parks it.
    """
    title = group.query_one(ToolGroupTitle)
    await wait_for(lambda: screen_is_settled(pilot.app, title.screen), pilot=pilot, description="the title shown")
    shown = title_runs(title)
    assert shown is group.shows_running
    timer = title._auto_refresh_timer
    assert timer is not None and timer._active.is_set() is shown
    return shown


@pytest.mark.asyncio
async def test_a_new_tool_group_starts_folded_even_in_open_activity() -> None:
    async with ChatPanelApp(thinking_expanded=lambda: True).run_test(size=(100, 30)) as pilot:
        panel = pilot.app.query_one(ChatPanel)
        await panel.add_user_message("Prompt")
        await panel.add_thinking_delta("r1", "reasoning", "Find the entry point.")
        panel.finish_thinking_segment("r1")
        await click_when_settled(pilot, panel.query_one(LiveProcessRow))
        await panel.add_tool_start("read", "read_file", KIND_FILESYSTEM_READ, args={"path": "notes.py"})

        group = panel.query_one(ToolGroup)
        await wait_for(
            lambda: "Reading notes.py" in _painted(pilot.app),
            pilot=pilot,
            description="the new tool group's title is painted",
        )
        cards = group.query_one("#tg-content")
        assert not cards.display
        await click_when_settled(pilot, group.query_one(ToolGroupTitle))
        assert cards.display


@pytest.mark.asyncio
async def test_activity_title_renders_a_real_ellipsis_at_narrow_width() -> None:
    async with ChatPanelApp().run_test(size=(28, 12)) as pilot:
        panel = pilot.app.query_one(ChatPanel)
        await panel.add_tool_start(
            "shell",
            "zsh",
            KIND_SHELL,
            args={"reason": "Running an intentionally long focused validation command"},
        )
        await pilot.pause()

        title = panel.query_one(ToolGroupTitle)
        _show_process(panel)
        await pilot.pause()
        rendered = title.render()
        assert rendered.plain.endswith("…")
        assert rendered.cell_len <= title.content_size.width


@pytest.mark.asyncio
async def test_live_group_activity_survives_manual_expand_and_falls_back_by_recency() -> None:
    async with ChatPanelApp().run_test(size=(100, 30)) as pilot:
        panel = pilot.app.query_one(ChatPanel)
        await panel.add_tool_start(
            "shell",
            "zsh",
            KIND_SHELL,
            args={"command": "pytest", "reason": "Running focused tests"},
        )
        await pilot.pause()

        group = panel.query_one(ToolGroup)
        _show_process(panel)
        assert group.collapsed is True
        assert _title(group) == "Running focused tests"
        assert await _running(pilot, group) is True
        # The dot takes the fold arrow's place in the title itself.
        assert list(group.query_one("#tg-header").children) == [group.query_one(ToolGroupTitle)]

        await pilot.click(ToolGroupTitle)
        await pilot.pause()
        assert group.collapsed is False
        assert _title(group) == "Running focused tests"
        assert await _running(pilot, group) is True

        await panel.add_tool_start(
            "read",
            "read_file",
            KIND_FILESYSTEM_READ,
            args={"path": "src/chrys/app.py"},
        )
        assert _title(group) == "Reading src/chrys/app.py"
        panel.update_tool_args("shell", {"command": "pytest", "reason": "Updated shell work"})
        assert _title(group) == "Reading src/chrys/app.py"

        await panel.add_tool_result("read", "read_file", "done")
        assert _title(group) == "Updated shell work"
        assert await _running(pilot, group) is True

        await panel.add_tool_result("shell", "zsh", "done")
        assert _title(group) == "Updated shell work"
        assert await _running(pilot, group) is True

        await panel.add_tool_start("write", "write_file", KIND_FILESYSTEM_WRITE, args={"path": "report.md"})
        assert _title(group) == "Writing report.md"
        assert await _running(pilot, group) is True

        await panel.add_tool_result("write", "write_file", "done")
        assert _title(group) == "Writing report.md"
        assert await _running(pilot, group) is True

        await panel.add_answer_delta("s1", "Moving on")
        assert _title(group) == "▸ Read 1 file · edited 1 file · ran 1 command"
        assert await _running(pilot, group) is False

        group.on_tool_group_title_clicked()
        await pilot.pause()
        assert group.collapsed is False
        assert _title(group) == "▾ Read 1 file · edited 1 file · ran 1 command"


@pytest.mark.asyncio
async def test_bare_chat_panel_folds_its_live_process() -> None:
    class _BarePanelApp(App):
        def compose(self) -> ComposeResult:
            yield ChatPanel()

    async with _BarePanelApp().run_test() as pilot:
        panel = pilot.app.query_one(ChatPanel)
        await panel.add_tool_start("read", "read_file", KIND_FILESYSTEM_READ, args={"path": "README.md"})
        assert panel.query_one(ToolGroup).collapsed is True
        assert panel.query_one(ToolGroup).process_hidden
        assert panel.query_one(LiveProcessRow).collapsed


@pytest.mark.asyncio
async def test_shell_without_reason_hides_command_and_skill_activity_uses_safe_fields() -> None:
    async with ChatPanelApp().run_test() as pilot:
        panel = pilot.app.query_one(ChatPanel)
        await panel.add_tool_start("shell", "zsh", KIND_SHELL, args={"command": "secret --token value"})
        group = panel.query_one(ToolGroup)

        assert _title(group) == "Running shell command"
        assert "secret" not in _title(group)

        await panel.add_tool_result("shell", "zsh", "done")
        await panel.add_tool_start(
            "skill",
            "run_skill_script",
            KIND_SKILL,
            args={"skill_name": "documents", "script_name": "render_docx.py"},
        )
        assert _title(group) == "Running skill script documents/render_docx.py"


@pytest.mark.asyncio
async def test_fold_target_ignores_locked_groups() -> None:
    class _FoldApp(App):
        def compose(self) -> ComposeResult:
            yield ChatPanel()

    async with _FoldApp().run_test() as pilot:
        panel = pilot.app.query_one(ChatPanel)
        locked = ToolGroup()
        other = ToolGroup()
        other.collapsed = True
        await panel.mount(locked, other)
        locked.lock_collapse_for("ask")

        assert panel.toggle_fold_all() is False
        assert locked.collapsed is False
        assert other.collapsed is False


@pytest.mark.asyncio
async def test_paused_sub_agent_expands_and_locks_group_until_resume() -> None:
    async with ChatPanelApp().run_test() as pilot:
        panel = pilot.app.query_one(ChatPanel)
        await panel.add_tool_start("parent", "Explore", KIND_SUB_AGENT, args={"task": "inspect"})
        panel.link_sub_agent_invocation("parent", "invocation", "Explore Agent")
        group = panel.query_one(ToolGroup)

        assert group.collapsed is True
        assert group.collapse_locked is False

        _show_process(panel)
        panel.sub_agent_paused("invocation", "acp_transport", "connection closed", 2)
        await pilot.pause()

        assert group.collapsed is False
        assert group.collapse_locked is True
        group.collapsed = True
        assert group.collapsed is False

        panel.sub_agent_resumed_after_pause("invocation")
        assert group.collapsed is False
        assert group.collapse_locked is False
        group.collapsed = True
        assert group.collapsed is True


@pytest.mark.asyncio
async def test_sub_agent_inner_activity_updates_parent_header_and_retains_latest_state() -> None:
    async with ChatPanelApp().run_test(size=(100, 30)) as pilot:
        panel = pilot.app.query_one(ChatPanel)
        await panel.add_tool_start("parent", "Explore", KIND_SUB_AGENT, args={"task": "inspect"})
        panel.link_sub_agent_invocation("parent", "invocation", "Explore Agent")

        group = panel.query_one(ToolGroup)
        _show_process(panel)
        assert _title(group) == "Running Explore Agent"

        panel.add_sub_agent_answer("Explore Agent", "invocation", "segment", "Let me inspect the notes.")

        assert _title(group) == "Running Explore Agent: Let me inspect the notes."

        await panel.add_sub_agent_tool_start(
            "Explore",
            "invocation",
            "read_file",
            {"path": "notes.txt", "content": "do not retain"},
            "inner",
            tool_kind=KIND_FILESYSTEM_READ,
        )

        assert _title(group) == "Running Explore Agent: Reading notes.txt"
        assert group._tool_records["parent"].nested_activities["inner"].args == {"path": "notes.txt"}

        panel._end_tool_group()
        assert _title(group) == "Running Explore Agent: Reading notes.txt"
        assert await _running(pilot, group) is True

        panel.complete_sub_agent_tool("Explore", "invocation", "inner", "done", 1)
        assert _title(group) == "Running Explore Agent: Reading notes.txt"
        assert group._tool_records["parent"].nested_activities == {}

        await panel.add_tool_result("parent", "Explore", "done")
        # Its one call succeeded: the row names the agent it ran.
        assert _title(group) == "▸ Ran agent Explore Agent"
        assert await _running(pilot, group) is False


def _sub_agent_line(panel: ChatPanel) -> str:
    """The card's activity line, without the running dot before it while the agent runs."""
    return title_text(panel.query_one(SubAgentToolCall).query_one("#sa-activity-text", Static))


@pytest.mark.asyncio
async def test_a_sub_agent_retry_takes_the_answer_it_withdraws_out_of_the_latest_activity() -> None:
    """The header and the card's line show the agent again; withdrawing an answer they no longer show changes nothing."""
    async with ChatPanelApp().run_test(size=(100, 30)) as pilot:
        panel = pilot.app.query_one(ChatPanel)
        await panel.add_tool_start("parent", "Explore", KIND_SUB_AGENT, args={"task": "inspect"})
        panel.link_sub_agent_invocation("parent", "invocation", "Explore Agent")
        group = panel.query_one(ToolGroup)

        panel.add_sub_agent_answer("Explore Agent", "invocation", "segment-1", "Done.")
        # A later answer shows the same line: the line is that answer's now.
        panel.add_sub_agent_answer("Explore Agent", "invocation", "segment-2", "Done.")
        panel.sub_agent_answer_withdrawn("invocation", ("segment-1",))
        assert (_title(group), _sub_agent_line(panel)) == ("Running Explore Agent: Done.", "Done.")

        panel.sub_agent_answer_withdrawn("invocation", ("segment-2",))
        assert (_title(group), _sub_agent_line(panel)) == ("Running Explore Agent", "Running Explore Agent")


@pytest.mark.asyncio
async def test_a_sub_agent_call_started_again_unchanged_is_no_new_activity() -> None:
    """An ACP agent sends a running call's start again before its result: only new input makes it the activity again."""
    async with ChatPanelApp().run_test(size=(100, 30)) as pilot:
        panel = pilot.app.query_one(ChatPanel)
        await panel.add_tool_start("parent", "Explore", KIND_SUB_AGENT, args={"task": "inspect"})
        panel.link_sub_agent_invocation("parent", "invocation", "Explore Agent")
        group = panel.query_one(ToolGroup)

        async def start(path: str) -> None:
            await panel.add_sub_agent_tool_start(
                "Explore", "invocation", "read_file", {"path": path}, "inner", tool_kind=KIND_FILESYSTEM_READ
            )

        await start("a.txt")
        panel.add_sub_agent_answer("Explore Agent", "invocation", "segment", "Now the next file.")
        await start("a.txt")
        assert (_title(group), _sub_agent_line(panel)) == (
            "Running Explore Agent: Now the next file.",
            "Now the next file.",
        )
        await start("b.txt")
        assert (_title(group), _sub_agent_line(panel)) == ("Running Explore Agent: Reading b.txt", "Reading b.txt")


@pytest.mark.asyncio
async def test_system_retry_and_interrupt_are_tool_group_presentation_boundaries() -> None:
    async with ChatPanelApp().run_test(size=(100, 30)) as pilot:
        panel = pilot.app.query_one(ChatPanel)

        await panel.add_tool_start("system", "read_file", KIND_FILESYSTEM_READ, args={"path": "system.txt"})
        first = panel.query_one(ToolGroup)
        await panel.add_tool_result("system", "read_file", "done")
        assert _title(first) == "Reading system.txt"
        await panel.add_system("Environment changed")
        assert _title(first) == "▸ Read system.txt"

        await panel.add_tool_start("retry", "read_file", KIND_FILESYSTEM_READ, args={"path": "retry.txt"})
        second = panel._tool_groups_by_call_id["retry"]
        await panel.add_tool_result("retry", "read_file", "done")
        assert _title(second) == "Reading retry.txt"
        await panel.add_retry("Trying again", 1, 2, 0)
        assert _title(second) == "▸ Read retry.txt"

        await panel.add_tool_start("interrupt", "read_file", KIND_FILESYSTEM_READ, args={"path": "active.txt"})
        third = panel._tool_groups_by_call_id["interrupt"]
        assert _title(third) == "Reading active.txt"
        await panel.add_interrupted()
        # The read was cut short: the row counts it, and says it was interrupted.
        assert _title(third) == "▸ Read 1 file · 1 interrupted"
        assert await _running(pilot, third) is False


@pytest.mark.asyncio
async def test_a_replayed_group_s_title_never_runs_and_keeps_no_refresh_timer() -> None:
    class _ReplayGroupApp(App):
        def compose(self) -> ComposeResult:
            yield ToolGroup(live=False)

    async with _ReplayGroupApp().run_test() as pilot:
        title = pilot.app.query_one(ToolGroup).query_one(ToolGroupTitle)
        assert title._work_running is False
        assert title._auto_refresh_timer is None


def test_loading_indicator_pause_and_resume_are_safe_before_mount() -> None:
    indicator = ChrysLoadingIndicator()
    indicator.pause_animation()
    indicator.resume_animation()


@pytest.mark.parametrize(
    ("tool_name", "tool_kind", "args", "expected"),
    [
        ("edit_file", KIND_FILESYSTEM_WRITE, {"path": "notes.md"}, "Editing notes.md"),
        ("grep", KIND_SEARCH, {"pattern": "TODO"}, "Searching for TODO"),
        ("glob", KIND_SEARCH, {"pattern": "*.py"}, "Finding *.py"),
        ("load_skill", KIND_SKILL, {"skill_name": "documents"}, "Loading skill documents"),
        (
            "read_skill_resource",
            KIND_SKILL,
            {"skill_name": "documents", "resource_name": "guide.md"},
            "Reading skill resource documents/guide.md",
        ),
        ("ask_user", KIND_ASK_USER, {"question": "Continue?"}, "Waiting for your answer"),
        ("custom_tool", "mcp", {}, "Running custom_tool"),
    ],
)
def test_activity_copy_uses_kind_specific_safe_arguments(
    tool_name: str,
    tool_kind: str,
    args: dict[str, str],
    expected: str,
) -> None:
    activity = _tool_activity_reference(tool_name, tool_kind, args)

    assert (activity if isinstance(activity, str) else format_message(activity)) == expected


class _Clock:
    """The tool group's and the sleep card's own ``time``, read as the test sets it."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.now = 1000.0
        shadow = ModuleType("time")
        shadow.monotonic = lambda: self.now  # type: ignore[attr-defined]
        monkeypatch.setattr(tool_call_module, "time", shadow)
        monkeypatch.setattr(sleep_module, "time", shadow)


def _card_time_frames(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """The time a sleep card shows, as each frame paints it."""
    frames: list[str] = []
    paint = Screen._compositor_refresh

    def record(screen: Screen) -> None:
        paint(screen)
        painted = [strip.text for strip in screen._compositor.render_strips()]
        if shown := " ".join(line.strip(" │") for line in painted if "seconds/" in line or "Remaining" in line):
            frames.append(shown)

    monkeypatch.setattr(Screen, "_compositor_refresh", record)
    return frames


@pytest.mark.asyncio
async def test_a_running_sleep_counts_down_in_its_group_title_after_why_it_sleeps(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = _Clock(monkeypatch)
    async with ChatPanelApp().run_test(size=(100, 30)) as pilot:
        panel = pilot.app.query_one(ChatPanel)
        await panel.add_tool_start("s1", "sleep", KIND_SLEEP, args={"seconds": 30, "reason": "Waiting for CI"})
        group = panel.query_one(ToolGroup)
        await wait_for(
            lambda: "Sleeping: Waiting for CI · 30s left" in _painted(pilot.app), pilot=pilot, description="30s"
        )
        # Its card counts from the same start as the title, though it was built after it.
        assert panel.query_one(SleepToolCall)._start_time == clock.now

        clock.now += 1.0
        await wait_for(
            lambda: "Sleeping: Waiting for CI · 29s left" in _painted(pilot.app),
            pilot=pilot,
            description="the title paints the next second left as it comes",
        )
        clock.now += 65.0
        await wait_for(
            lambda: "Sleeping: Waiting for CI · 0s left" in _painted(pilot.app), pilot=pilot, description="0s"
        )

        await panel.add_tool_result("s1", "sleep", "Slept 30 seconds.", 30_000)
        # Its activity stays until what follows starts, with no time left to show.
        assert _title(group) == "Sleeping: Waiting for CI"
        # The running title's own frames paint the time left: the group keeps no timer for it.
        assert not group._timers


@pytest.mark.asyncio
async def test_a_sleep_title_counts_down_only_while_shown(monkeypatch: pytest.MonkeyPatch) -> None:
    clock = _Clock(monkeypatch)
    async with ChatPanelApp().run_test(size=(100, 30)) as pilot:
        panel = pilot.app.query_one(ChatPanel)
        await panel.add_tool_start("s1", "sleep", KIND_SLEEP, args={"seconds": 30})
        title = panel.query_one(ToolGroupTitle)
        await wait_for(lambda: "Sleeping · 30s left" in _painted(pilot.app), pilot=pilot, description="30s")
        main = pilot.app.screen
        await pilot.app.push_screen(Screen())
        await wait_for(lambda: pilot.app.screen is not main, pilot=pilot, description="covered")

        refreshes: list[object] = []
        refresh = title.refresh
        monkeypatch.setattr(
            title, "refresh", lambda *args, **kwargs: (refreshes.append(args), refresh(*args, **kwargs))
        )
        clock.now += 3.0
        # Longer than a second: a countdown ticking each second under the covering screen would repaint by then.
        assert not await wait_until(lambda: refreshes, timeout=1.5, pilot=pilot)

        pilot.app.pop_screen()
        await wait_for(
            lambda: "Sleeping · 27s left" in _painted(pilot.app),
            pilot=pilot,
            description="the time left as it is now, once the title shows again",
        )


@pytest.mark.asyncio
async def test_a_narrow_sleep_title_cuts_why_it_sleeps_and_keeps_its_time_left(monkeypatch: pytest.MonkeyPatch) -> None:
    _Clock(monkeypatch)
    async with ChatPanelApp().run_test(size=(40, 12)) as pilot:
        panel = pilot.app.query_one(ChatPanel)
        await panel.add_tool_start(
            "s1", "sleep", KIND_SLEEP, args={"seconds": 3600, "reason": "Waiting for the nightly build to finish"}
        )
        title = panel.query_one(ToolGroupTitle)
        _show_process(panel)
        await pilot.pause()
        rendered = title.render()
        assert rendered.plain.endswith("… · 1h left")
        assert rendered.cell_len <= title.content_size.width

        await panel.add_tool_start("s2", "sleep", KIND_SLEEP, args={"seconds": 90})
        assert title_text(title) == "Sleeping · 1m 30s left"


@pytest.mark.asyncio
async def test_a_repeated_sleep_start_keeps_the_countdown_until_completion(monkeypatch: pytest.MonkeyPatch) -> None:
    clock = _Clock(monkeypatch)
    async with ChatPanelApp().run_test(size=(100, 30)) as pilot:
        panel = pilot.app.query_one(ChatPanel)
        await panel.add_tool_start("s1", "sleep", KIND_SLEEP, args={"seconds": 30})
        group = panel.query_one(ToolGroup)
        clock.now += 10.0
        # Started again while it runs, as an ACP agent does when what it shows changes: it goes on counting.
        await panel.add_tool_start("s1", "sleep", KIND_SLEEP, args={"seconds": 30})
        assert _title(group) == "Sleeping · 20s left"

        await panel.add_tool_result("s1", "sleep", "Slept 30 seconds.", 30_000)
        finished = _title(group)
        clock.now += 60.0
        await panel.add_tool_start("s1", "sleep", KIND_SLEEP, args={"seconds": 60, "reason": "Repeated delivery"})
        assert _title(group) == finished == "Sleeping"

        await panel.add_tool_start("s2", "sleep", KIND_SLEEP, args={"seconds": 30})
        assert _title(group) == "Sleeping · 30s left"


@pytest.mark.asyncio
async def test_a_sleep_card_built_after_its_call_started_counts_from_the_start_in_its_first_frame(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = _Clock(monkeypatch)
    frames = _card_time_frames(monkeypatch)
    composed: list[str] = []
    compose = SleepToolCall.compose

    def record_compose(card: SleepToolCall) -> ComposeResult:
        composed.append(card._running_label_text().plain)
        return compose(card)

    monkeypatch.setattr(SleepToolCall, "compose", record_compose)
    async with ChatPanelApp().run_test(size=(100, 30)) as pilot:
        panel = pilot.app.query_one(ChatPanel)
        # As a nested transcript built later hands it the start its journal recorded.
        await panel._tool_registry.add_tool_start(
            "s1", "sleep", KIND_SLEEP, args={"seconds": 60}, started_at=clock.now - 40.0
        )
        _show_process(panel)
        panel.query_one(ToolGroup).collapsed = False
        await wait_for(lambda: frames, pilot=pilot, description="the card paints")
        assert set(frames) == {"- sleep (40 seconds/60 seconds) Skip    Remaining: 20 seconds"}
        # It composes with it: no frame, however soon after its mount, shows another.
        assert composed == ["- sleep (40 seconds/60 seconds)"]


@pytest.mark.asyncio
async def test_a_sleep_card_its_folded_group_opens_shows_the_time_left_now_in_its_first_frame(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = _Clock(monkeypatch)
    frames = _card_time_frames(monkeypatch)
    async with ChatPanelApp().run_test(size=(100, 30)) as pilot:
        panel = pilot.app.query_one(ChatPanel)
        await panel.add_tool_start("s1", "sleep", KIND_SLEEP, args={"seconds": 60})
        group = panel.query_one(ToolGroup)
        clock.now += 40.0
        # Folded, the card did not count.
        _show_process(panel)
        group.collapsed = False
        await wait_for(lambda: frames, pilot=pilot, description="the card paints")
        assert set(frames) == {"- sleep (40 seconds/60 seconds) Skip    Remaining: 20 seconds"}


@pytest.mark.asyncio
async def test_a_sleep_title_reads_in_one_language_after_a_locale_switch(monkeypatch: pytest.MonkeyPatch) -> None:
    _Clock(monkeypatch)
    app = ChatPanelApp()
    controller = LocaleController(Settings(locale="en"))
    app.locale_controller = controller  # type: ignore[attr-defined]
    async with app.run_test(size=(100, 30)) as pilot:
        panel = pilot.app.query_one(ChatPanel)
        await panel.add_tool_start("s1", "sleep", KIND_SLEEP, args={"seconds": 30, "reason": "CI"})
        await wait_for(lambda: "Sleeping: CI · 30s left" in _painted(pilot.app), pilot=pilot, description="English")

        assert controller.switch_locale("zh-Hans").status is LocaleSwitchStatus.EFFECTIVE_CHANGED
        # It repaints every frame while it runs: what it does reads in the language its time left does.
        chinese = "休眠中：CI · 还剩 30 秒"  # noqa: RUF001
        await wait_for(lambda: chinese in _painted(pilot.app), pilot=pilot, description="Chinese")


@pytest.mark.asyncio
async def test_an_interrupted_sleep_counts_what_it_slept_until_then(monkeypatch: pytest.MonkeyPatch) -> None:
    clock = _Clock(monkeypatch)
    async with ChatPanelApp().run_test(size=(100, 30)) as pilot:
        panel = pilot.app.query_one(ChatPanel)
        await panel.add_tool_start("s1", "sleep", KIND_SLEEP, args={"seconds": 30})
        group = panel.query_one(ToolGroup)
        clock.now += 20.0
        await panel.add_interrupted()
        # As a replay of the turn reads it, from the timing the interrupted call recorded.
        assert _title(group) == "▸ Slept 20s · 1 interrupted"
