# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""A closed tool group's title counts its calls by kind, and keeps its failed and interrupted counts in view."""

from __future__ import annotations

from typing import TYPE_CHECKING

from rich.style import Style
from rich.text import Text
from textual.containers import Vertical

from chrys.app.tui.widgets.chat.fold_rows import (
    FoldRow,
    FoldRowStyles,
    ToolTally,
    duration_words,
    fit_fold_row,
    tool_category,
)
from chrys.app.tui.widgets.chat.panel import ChatPanel
from chrys.app.tui.widgets.chat.tool_call import ToolGroup, ToolGroupTitle
from chrys.foundation.hosted_tools import HostedToolFamily
from chrys.foundation.i18n import Localizer
from chrys.foundation.tool_kinds import (
    KIND_FILESYSTEM_READ,
    KIND_FILESYSTEM_WRITE,
    KIND_SEARCH,
    KIND_SHELL,
    KIND_SLEEP,
)
from tests.support.tui_helpers import ChatPanelApp, LocalizedWidgetApp, open_process_for, resize_when_settled
from tests.support.waiting import wait_for

if TYPE_CHECKING:
    import pytest

_STYLES = FoldRowStyles(detail=Style(dim=True), failed=Style(color="red"), interrupted=Style(color="yellow"))
_COUNTS = ("2 reads", "5 commands")


def _styled(text: Text) -> list[tuple[str, Style | str]]:
    return [(text.plain[span.start : span.end], span.style) for span in text.spans]


def test_a_call_counts_under_its_hosted_family_first_then_its_kind() -> None:
    assert tool_category(KIND_FILESYSTEM_READ) == "read"
    assert tool_category(KIND_SHELL) == "command"
    # A hosted fetch carries the search kind; its family says what it did.
    assert tool_category(KIND_SEARCH, HostedToolFamily.FETCH) == "fetch"
    assert tool_category(KIND_SEARCH, HostedToolFamily.SEARCH) == "web_search"
    assert tool_category("", HostedToolFamily.CODE) == "other"
    assert tool_category(KIND_SLEEP) == "sleep"
    assert tool_category("") == "other"


def test_a_tally_lists_its_categories_in_row_order_in_the_words_of_each_language() -> None:
    tally = ToolTally.of(
        [("command", "")] * 5
        + [("read", "failed"), ("agent", "interrupted"), ("read", ""), ("edit", "failed"), ("other", "")]
    )
    english = Localizer("en")
    assert tally.total == 10
    assert tally.count_words(english) == ("2 reads", "1 edit", "5 commands", "1 agent", "1 other call")
    assert tally.outcomes(english) == ("2 failed", "1 interrupted")
    assert tally.count_words(Localizer("zh-Hans")) == (
        "2 次读取",
        "1 次编辑",
        "5 条命令",
        "1 个子智能体",
        "1 次其他调用",
    )
    assert ToolTally.of([]).count_words(english) == ()
    assert ToolTally.of([]).outcomes(english) == ("", "")


def test_a_span_of_time_reads_in_its_largest_units_with_the_number_it_starts_with() -> None:
    english, chinese = Localizer("en"), Localizer("zh-Hans")
    spans = (0, 59, 60, 65, 3600, 3660, 3661, 90061)
    assert [duration_words(english, seconds) for seconds in spans] == [
        "0s",
        "59s",
        "1m",
        "1m 5s",
        "1h",
        "1h 1m",
        "1h 1m",
        "25h 1m",
    ]
    # Minutes alone, or last, read as minutes: "分" alone reads as a score or a cent.
    assert duration_words(chinese, 60) == "1 分钟"
    assert duration_words(chinese, 65) == "1 分 5 秒"
    assert duration_words(chinese, 3660) == "1 小时 1 分钟"


def test_a_group_s_sleeps_say_how_long_they_slept_in_all_and_a_process_counts_them() -> None:
    sleeps = ToolTally.of([("sleep", "")], slept_ms=23_400) + ToolTally.of([("sleep", "")], slept_ms=42_600)
    tally = ToolTally.of([("read", "")]) + sleeps
    english, chinese = Localizer("en"), Localizer("zh-Hans")
    assert tally.action_words(english) == ("Read 1 file", "slept 1m 6s")
    assert sleeps.action_words(english) == ("Slept 1m 6s",)
    assert tally.count_words(english) == ("1 read", "2 sleeps")
    assert tally.action_words(chinese) == ("读取 1 个文件", "休眠了 1 分 6 秒")
    assert tally.count_words(chinese) == ("1 次读取", "2 次休眠")


def test_a_row_keeps_its_outcomes_in_view_and_cuts_the_counts_before_them() -> None:
    row = FoldRow("▸ Tools     ", _COUNTS, "1 failed", "1 interrupted")
    wide = fit_fold_row(row, 80, _STYLES)
    assert wide.plain == "▸ Tools     2 reads · 5 commands · 1 failed · 1 interrupted"
    assert ("1 failed", Style(color="red")) in _styled(wide)
    assert ("1 interrupted", Style(color="yellow")) in _styled(wide)

    narrow = fit_fold_row(row, 40, _STYLES)
    assert narrow.plain == "▸ Tools     … · 1 failed · 1 interrupted"
    assert narrow.cell_len == 40
    assert ("1 failed", Style(color="red")) in _styled(narrow)

    # Narrower than the outcomes themselves, the row is simply cut.
    tiny = fit_fold_row(row, 10, _STYLES)
    assert tiny.cell_len == 10 and tiny.plain.endswith("…")

    plain = fit_fold_row(FoldRow("▸ Tools     ", _COUNTS), 16, _STYLES)
    assert plain.plain == "▸ Tools     2 r…"


def test_a_count_reads_in_one_style_its_number_included() -> None:
    row = FoldRow("▸ 工具  ", ("2 次读取", "共 12 条命令"))
    assert _styled(fit_fold_row(row, 80, _STYLES)) == [
        ("2 次读取", Style(dim=True)),
        (" · ", Style(dim=True)),
        ("共 12 条命令", Style(dim=True)),
    ]


async def test_a_closed_group_counts_its_calls_and_a_narrow_title_still_shows_what_failed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with ChatPanelApp().run_test(size=(100, 24)) as pilot:
        cp = pilot.app.query_one(ChatPanel)
        await cp.add_user_message("prompt")
        await cp.add_tool_start("r1", "read_file", KIND_FILESYSTEM_READ, args={"path": "a.py"})
        await cp.add_tool_start("r2", "read_file", KIND_FILESYSTEM_READ, args={"path": "b.py"})
        await cp.add_tool_start("w1", "write_file", KIND_FILESYSTEM_WRITE, args={"path": "c.py"})
        await cp.add_tool_start("s1", "zsh", KIND_SHELL, args={"command": "ls"})
        await cp.add_tool_start("s2", "zsh", KIND_SHELL, args={"command": "sleep 60"})
        group = cp.query_one(ToolGroup)
        title = group.query_one(ToolGroupTitle)
        await cp.add_tool_result("r1", "read_file", "ok")
        await cp.add_tool_result("r2", "read_file", "Error: no such file")
        await cp.add_tool_result("w1", "write_file", "Rejected by user", approval="user_rejected")
        await cp.add_tool_result("s1", "zsh", "done")
        # Still running: the title is the latest activity, after the running dot.
        assert title.render().plain[:2] in {"• ", "◦ "}
        assert title.render().plain[2:] == "Running shell command"

        await cp.add_interrupted()
        # The stopped turn folds; open it to read the group.
        open_process_for(group)
        # The rejected write failed with the read; the cut-short command was interrupted, not failed.
        counts = "Read 2 files · edited 1 file · ran 2 commands · 2 failed · 1 interrupted"
        await wait_for(
            lambda: title.render().plain == f"{'▸' if group.collapsed else '▾'} {counts}",
            pilot=pilot,
            description="the group's counts",
        )

        refreshes: list[dict[str, object]] = []
        repaint = title.refresh

        def recording_refresh(*args: object, **kwargs: object) -> object:
            refreshes.append(kwargs)
            return repaint(*args, **kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr(title, "refresh", recording_refresh)
        group.collapsed = not group.collapsed
        await wait_for(
            lambda: title.render().plain == f"{'▸' if group.collapsed else '▾'} {counts}",
            pilot=pilot,
            description="the arrow follows the fold",
        )
        # The title stays one line: new text repaints it without laying out the transcript.
        assert refreshes == [{"layout": False}]

        styled = dict(_styled(title.render()))
        failed = title.get_component_rich_style("toolgrouptitle--failed").color
        interrupted = title.get_component_rich_style("toolgrouptitle--detail").color
        assert isinstance(styled["2 failed"], Style) and styled["2 failed"].color == failed
        assert isinstance(styled["1 interrupted"], Style) and styled["1 interrupted"].color == interrupted
        assert failed != interrupted
        # A count reads in the row's colour, its number included: not brighter.
        for count in ("Read 2 files", "edited 1 file", "ran 2 commands"):
            assert isinstance(styled[count], Style) and styled[count].color == interrupted

        await resize_when_settled(pilot, 50, 24)
        narrow = title.render().plain
        assert narrow.endswith("… · 2 failed · 1 interrupted")
        assert 0 < Text(narrow).cell_len <= title.content_size.width


async def test_a_group_built_before_it_composes_works_out_its_title_when_it_does(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Replay adds a session's calls to groups not yet composed: none of them works out a title it cannot show."""
    rows = 0
    title_row = ToolGroup._title_row

    def counting_title_row(group: ToolGroup) -> FoldRow:
        nonlocal rows
        rows += 1
        return title_row(group)

    monkeypatch.setattr(ToolGroup, "_title_row", counting_title_row)
    async with LocalizedWidgetApp(lambda: Vertical(id="host")).run_test() as pilot:
        group = ToolGroup(live=False)
        for index in range(50):
            await group.add_collapsed_replay_tool(f"c{index}", "read_file", KIND_FILESYSTEM_READ, result="ok")
        assert rows == 0

        await pilot.app.query_one("#host").mount(group)
        title = group.query_one(ToolGroupTitle)
        await wait_for(lambda: title.render().plain == "▸ Read 50 files", pilot=pilot, description="the title")
        assert rows >= 1


async def test_a_group_s_sleeps_slept_the_time_they_took_or_with_none_known_what_they_asked_for() -> None:
    async with LocalizedWidgetApp(lambda: Vertical(id="host")).run_test() as pilot:
        group = ToolGroup(live=False)
        await group.add_collapsed_replay_tool("r1", "read_file", KIND_FILESYSTEM_READ, result="ok")
        await group.add_collapsed_replay_tool(
            "s1", "sleep", KIND_SLEEP, args={"seconds": 30}, result="Slept 23 seconds.", duration_ms=23_400
        )
        await group.add_collapsed_replay_tool("s2", "sleep", KIND_SLEEP, args={"seconds": 40}, result="Slept 40s.")
        # Skipped, with no time known, it slept for none the row can tell.
        await group.add_collapsed_replay_tool(
            "s3", "sleep", KIND_SLEEP, args={"seconds": 50}, result="Skipped.", metadata={"sleep_skipped": True}
        )
        # No result answered it (the session stopped while it slept): nor did this one.
        await group.add_collapsed_replay_tool("s4", "sleep", KIND_SLEEP, args={"seconds": 1800}, result="")
        # A time counted from before an approval it waited for: it slept no longer than it asked.
        await group.add_collapsed_replay_tool(
            "s5", "sleep", KIND_SLEEP, args={"seconds": 10}, result="Slept 10 seconds.", duration_ms=70_000
        )
        await pilot.app.query_one("#host").mount(group)
        title = group.query_one(ToolGroupTitle)
        await wait_for(
            lambda: title.render().plain == "▸ Read 1 file · slept 1m 13s", pilot=pilot, description="the title"
        )
