# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Simplified-Chinese and legacy-payload coverage for transcript tool cards."""

from __future__ import annotations

from typing import cast

import pytest
from rich.text import Text
from textual.app import App, ComposeResult
from textual.widget import Widget
from textual.widgets import Static

from chrys.app.tui.i18n import LocaleController
from chrys.app.tui.widgets.chat.context_fold import ContextFoldWidget
from chrys.app.tui.widgets.chat.renderers.ask_user import AskUserToolCall
from chrys.app.tui.widgets.chat.renderers.file_edit import EditFileToolCall
from chrys.app.tui.widgets.chat.renderers.hosted_generic import HostedToolCall
from chrys.app.tui.widgets.chat.renderers.hosted_shell import HostedShellToolCall
from chrys.app.tui.widgets.chat.renderers.skill import SkillToolCall
from chrys.app.tui.widgets.chat.renderers.sub_agent import SubAgentToolCall
from chrys.app.tui.widgets.chat.tool_call import ToolCall
from chrys.app.tui.widgets.chat.tool_renderers import create_tool_widget, hosted_family_display_title
from chrys.app.tui.widgets.chat.tool_view_builders import ToolViewDiff, ToolViewImage, build_code_view
from chrys.foundation.config.settings import Settings
from chrys.foundation.hosted_tools import HostedToolFamily
from chrys.foundation.i18n import Localizer
from chrys.foundation.tool_kinds import KIND_SHELL
from tests.support.waiting import wait_for


class _LocalizedHost(App[None]):
    def __init__(self, *widgets: Widget, locale: str = "zh-Hans") -> None:
        self.locale_controller = LocaleController(Settings(locale=locale))
        self._widgets = widgets
        super().__init__()

    def compose(self) -> ComposeResult:
        yield from self._widgets


def _plain(value: object) -> str:
    return value.plain if isinstance(value, Text) else str(value)


def test_tool_view_builder_placeholders_keep_english_and_accept_chinese_renderer() -> None:
    english_code = build_code_view("text", "")
    chinese_code = build_code_view("text", "", render_message=Localizer("zh-Hans").render)
    english_diff = next(iter(ToolViewDiff("a.py", "old", "new").compose()))
    chinese_diff = next(iter(ToolViewDiff("a.py", "old", "new", render_message=Localizer("zh-Hans").render).compose()))

    assert _plain(english_code.content) == "(empty)"
    assert _plain(chinese_code.content) == "（空）"  # noqa: RUF001
    assert _plain(english_diff.content) == "Preparing diff..."
    assert _plain(chinese_diff.content) == "正在加载差异视图..."


@pytest.mark.parametrize(
    ("locale", "output", "empty", "answer", "diff"),
    [
        ("en", "Output", "(empty)", "Answer", "Diff"),
        ("zh-Hans", "输出", "（空）", "回答", "差异"),  # noqa: RUF001
    ],
)
@pytest.mark.asyncio
async def test_tool_copy_sections_render_shared_titles_and_empty_placeholders(
    locale: str,
    output: str,
    empty: str,
    answer: str,
    diff: str,
) -> None:
    generic = ToolCall("generic", "remote")
    ask = AskUserToolCall("ask", "ask_user", args={})
    sub_agent = SubAgentToolCall("sub-agent", "sub_agent", args={})
    skill = SkillToolCall("skill", "load_skill", args={})
    file_edit = EditFileToolCall("file", "edit_file", args={"path": "a.py"})
    app = _LocalizedHost(generic, ask, sub_agent, skill, file_edit, locale=locale)

    async with app.run_test(size=(100, 40)) as pilot:
        await pilot.pause()
        file_edit.result_text = "Edited a.py"
        file_edit._before_content = "old\n"
        file_edit._after_content = "new\n"

        assert generic._tool_copy_sections() == [(output, "text", empty)]
        assert ask._tool_copy_input() == ("markdown", empty)
        assert ask._tool_copy_sections() == [(answer, "text", empty)]
        assert sub_agent._tool_copy_input() == ("markdown", empty)
        assert sub_agent._tool_copy_sections() == [(output, "markdown", empty)]
        assert skill._tool_copy_sections() == [(output, "markdown", empty)]
        assert file_edit._tool_copy_full_diff_section()[0] == diff


@pytest.mark.parametrize(
    ("locale", "title", "omitted", "unchanged"),
    [
        (
            "en",
            "Diff Preview",
            "@@ Full diff omitted: snapshot is 140002 chars (limit 65536); showing bounded diff around first change @@",
            "@@ Bounded preview contains no changed lines @@",
        ),
        (
            "zh-Hans",
            "差异预览",
            "@@ 已省略完整差异：快照共 140002 个字符（限制 65536 个）；显示首个更改附近的有界差异 @@",  # noqa: RUF001
            "@@ 有界预览不包含更改的行 @@",
        ),
    ],
)
@pytest.mark.asyncio
async def test_bounded_diff_notices_render_at_tool_locale(
    locale: str,
    title: str,
    omitted: str,
    unchanged: str,
) -> None:
    file_edit = EditFileToolCall("file", "edit_file", args={"path": "a.py"})
    app = _LocalizedHost(file_edit, locale=locale)

    async with app.run_test() as pilot:
        await pilot.pause()
        file_edit._before_content = "\x00" + ("x" * 70_000)
        file_edit._after_content = "\x01" + ("x" * 70_000)

        section = file_edit._tool_copy_preview_diff_section()
        assert section is not None
        assert section[0] == title
        assert omitted in section[2]
        assert unchanged in section[2]


@pytest.mark.asyncio
async def test_rejected_and_hosted_shell_chrome_render_chinese() -> None:
    generic = ToolCall("call-generic", "remote", args={"value": "x"})
    hosted = cast(
        HostedShellToolCall,
        create_tool_widget(
            "call-shell",
            "shell",
            KIND_SHELL,
            args={"commands": ["printf hi", "pwd"]},
            provider_hosted=True,
            hosted_family=HostedToolFamily.SHELL,
            provider="openai",
            canonical_status="completed",
        ),
    )
    app = _LocalizedHost(generic, hosted)

    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        assert "运行中" in _plain(hosted.query_one("#tc-body", Static).content)

        generic.set_complete("denied", approval="user_rejected")
        hosted.set_complete("", metadata={"stdout": "hi"})
        await pilot.pause()

        assert _plain(generic.query_one("#tc-panel").border_subtitle) == "已拒绝"
        assert _plain(hosted.query_one("#tc-panel").border_subtitle) == "已完成"
        body = _plain(hosted.query_one("#tc-body", Static).content)
        assert "命令：\nprintf hi\npwd" in body  # noqa: RUF001
        assert "stdout：\nhi" in body  # noqa: RUF001


def test_context_fold_resolves_chinese_once_and_keeps_english_fallback() -> None:
    resolve_chinese = Localizer("zh-Hans").render
    ranged = ContextFoldWidget(
        "ctx-range",
        "摘要",
        5_000,
        (3, 5),
        resolve_message=resolve_chinese,
    )
    counted = ContextFoldWidget(
        "ctx-count",
        "摘要",
        5_000,
        resolve_message=resolve_chinese,
    )
    english = ContextFoldWidget("ctx-en", "summary", 5_000, (3, 5))

    assert ranged.title_text == "▸ 已压缩 (第 3-5 轮)"
    assert counted.title_text == "▸ 已压缩 (5,000 条消息)"
    assert english.title_text == "▸ Compressed (Turn 3-5)"


@pytest.mark.asyncio
async def test_provider_and_generic_payload_sinks_replace_legacy_controls() -> None:
    status_card = cast(
        HostedToolCall,
        create_tool_widget(
            "call-status",
            "remote_tool",
            "",
            provider_hosted=True,
            hosted_family=HostedToolFamily.GENERIC,
            provider_status="\x1b[31mfailed",
        ),
    )
    hosted_body = cast(
        HostedToolCall,
        create_tool_widget(
            "call-body",
            "remote_tool",
            "",
            provider_hosted=True,
            hosted_family=HostedToolFamily.GENERIC,
            canonical_status="completed",
        ),
    )
    generic_body = ToolCall("call-generic-body", "remote")
    app = _LocalizedHost(status_card, hosted_body, generic_body)

    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        status_card.update_hosted_status("running", "\x1b[31mfailed")
        hosted_body.set_complete("first\rsecond\nthird\x1b[31m")
        generic_body.set_complete("value\x1b[2J")
        await pilot.pause()

        subtitle = _plain(status_card.query_one("#tc-panel").border_subtitle)
        assert "\x1b" not in subtitle
        assert "�" in subtitle

        hosted_text = _plain(hosted_body.query_one("#tc-body", Static).content)
        assert "\x1b" not in hosted_text
        assert "first�second\nthird�[31m" in hosted_text

        generic_text = _plain(generic_body.query_one("#tc-body", Static).content)
        assert "\x1b" not in generic_text
        assert generic_text == "value�[2J"


def test_hosted_family_title_accepts_a_chinese_renderer() -> None:
    localizer = Localizer("zh-Hans")

    assert hosted_family_display_title(HostedToolFamily.SHELL, render_message=localizer.render) == "Hosted Shell"


@pytest.mark.asyncio
async def test_edit_spinner_spend_and_discovery_details_render_chinese() -> None:
    from chrys.app.tui.widgets.chat.renderers.execute import ExecuteToolCall
    from chrys.app.tui.widgets.chat.renderers.hosted_generic import HostedToolDiscoveryCall

    edit = EditFileToolCall("call-edit", "edit_file", args={"file_path": "a.txt"})
    discovery = HostedToolDiscoveryCall("call-disc", "list_tools")
    agent = SubAgentToolCall("call-agent", "sub_agent", args={"prompt": "do things"})
    execute = ExecuteToolCall("call-exec", "bash", args={"command": "sleep 5", "timeout": 90})
    app = _LocalizedHost(edit, discovery, agent, execute)

    async with app.run_test(size=(100, 40)) as pilot:
        await pilot.pause()

        assert "编辑中" in edit._render_spinner().plain

        discovery._discovered_count = 4
        assert "已发现 4 个" in discovery._label_details()

        agent._progress_total_usage_tokens = 1024
        assert "消耗：" in agent._render_subtitle()  # noqa: RUF001

        assert "限时 1m 30s" in execute._running_label_text().plain


@pytest.mark.parametrize(
    ("locale", "no_command", "image_unavailable"),
    [
        ("en", "(no command)", "(image unavailable)"),
        ("zh-Hans", "（无命令）", "（图像不可用）"),  # noqa: RUF001
    ],
)
@pytest.mark.asyncio
async def test_execute_and_image_fallback_placeholders_render_at_mount_locale(
    locale: str,
    no_command: str,
    image_unavailable: str,
) -> None:
    from rich.syntax import Syntax

    from chrys.app.tui.widgets.chat.renderers.execute import ExecuteToolCall

    execute = ExecuteToolCall("call-exec", "bash", args={})
    image = ToolViewImage([])
    app = _LocalizedHost(execute, image, locale=locale)

    async with app.run_test(size=(100, 40)) as pilot:
        await pilot.pause()

        code_view = execute._tool_view_input_widgets()[0]
        assert cast(Syntax, code_view.content).code == no_command
        assert image.render().plain == image_unavailable


@pytest.mark.parametrize(
    ("locale", "expected"),
    [
        ("en", "Error: failed to prepare diff — [boom] detail\nsecond line"),
        ("zh-Hans", "错误：加载差异视图失败 — [boom] detail\nsecond line"),  # noqa: RUF001
    ],
)
@pytest.mark.asyncio
async def test_diff_prepare_failure_renders_at_mount_locale_with_literal_detail(
    locale: str,
    expected: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from chrys.app.tui.widgets.diff_view import DiffView

    async def _fail(self: DiffView) -> None:
        raise RuntimeError("[boom] detail\nsecond line")

    monkeypatch.setattr(DiffView, "prepare", _fail)
    diff = ToolViewDiff("a.py", "old", "new")
    app = _LocalizedHost(diff, locale=locale)

    async with app.run_test(size=(100, 30)) as pilot:
        placeholder = diff.query_one(".tool-view-diff-placeholder", Static)
        await wait_for(
            lambda: _plain(placeholder.content) == expected,
            pilot=pilot,
            description="diff failure placeholder",
        )


@pytest.mark.asyncio
async def test_compaction_retry_notice_renders_chinese_with_literal_message() -> None:
    from chrys.app.tui.widgets.chat.compaction_card import CompactionCard

    card = CompactionCard("compaction-1")
    app = _LocalizedHost(card)

    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        for attempt in range(1, 5):
            card.show_retry_notice("[502] upstream error\nRetry-After: 30", attempt, 5, 3)
        await pilot.pause()

        notice = card.query_one("#compaction-retry-notice", Static)
        assert notice.display
        assert notice.render().plain == "⚠ [502] upstream error\nRetry-After: 30 — 3 秒后重试（4/5）"  # noqa: RUF001


@pytest.mark.asyncio
async def test_compaction_state_labels_render_chinese_with_literal_reason() -> None:
    from chrys.app.tui.widgets.chat.compaction_card import CompactionCard

    running = CompactionCard("compaction-running")
    summarized = CompactionCard("compaction-ok")
    interrupted = CompactionCard("compaction-canceled")
    failed = CompactionCard("compaction-failed")
    reasoned = CompactionCard("compaction-failed-reason")
    app = _LocalizedHost(running, summarized, interrupted, failed, reasoned)

    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        summarized.set_complete(outcome="ok", duration_ms=68_000, last_words="总结")
        interrupted.set_complete(outcome="canceled", duration_ms=1500)
        failed.set_complete(outcome="failed", duration_ms=1500)
        reasoned.set_complete(outcome="failed", failure_reason="[quota] limit exceeded\nRetry-After: 30")
        await pilot.pause()

        def _label(card: CompactionCard) -> str:
            return card.query_one("#compaction-label", Static).render().plain

        assert "正在压缩对话..." in _label(running)
        assert _label(summarized) == "▸ 对话已总结 ✓ (1m 8s)"
        assert _label(interrupted) == "✗ 压缩已中断 (1s)"
        assert _label(failed) == "✗ 压缩失败 (1s)"
        assert _label(reasoned) == "✗ 压缩失败（[quota] limit exceeded\nRetry-After: 30）"  # noqa: RUF001


@pytest.mark.asyncio
async def test_tool_group_title_summarizes_its_calls_per_locale() -> None:
    from chrys.app.tui.widgets.chat.tool_call import ToolGroup, ToolGroupTitle

    class _EnglishHost(App[None]):
        def __init__(self, widget: Widget) -> None:
            self._widget = widget
            super().__init__()

        def compose(self) -> ComposeResult:
            yield self._widget

    localized_group = ToolGroup()
    app = _LocalizedHost(localized_group)
    async with app.run_test(size=(100, 30)) as pilot:
        await localized_group.add_tool("c1", "bash", KIND_SHELL, "", args={})
        localized_group.complete_tool("c1", "done")
        localized_group.finalize_activity_header()
        await pilot.pause()

        # The row says what the calls did, in the words of the language; a call with no command to name keeps its
        # count.
        assert localized_group.query_one(ToolGroupTitle).render().plain == "▾ 运行 1 条命令"

    english_group = ToolGroup()
    english_app = _EnglishHost(english_group)
    async with english_app.run_test(size=(100, 30)) as pilot:
        await english_group.add_tool("c1", "bash", KIND_SHELL, "", args={})
        english_group.complete_tool("c1", "done")
        english_group.finalize_activity_header()
        await pilot.pause()

        assert english_group.query_one(ToolGroupTitle).render().plain == "▾ Ran 1 command"
