# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tests for chat message widgets: user messages and image previews, message headers and timestamps, agent message streaming/copy/cache, and error/system/status-action rows."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import patch

import pytest
from PIL import Image
from rich.color import ColorSystem
from rich.text import Text
from textual import on
from textual.app import App, ComposeResult
from textual.color import Color as TextualColor
from textual.geometry import Offset, Region
from textual.pilot import Pilot
from textual.selection import SELECT_ALL, Selection
from textual.widgets import Button, Static

from chrys.app.tui.i18n import LocaleController
from chrys.app.tui.theme import CHRYS_THEME, TuiVariableDefaultsMixin
from chrys.app.tui.widgets.chat import image_preview as image_preview_module
from chrys.app.tui.widgets.chat.fold_rows import AGENT_LABEL, OPEN_ARROW, THOUGHT_LABEL
from chrys.app.tui.widgets.chat.image_preview import extract_image_previews
from chrys.app.tui.widgets.chat.messages import (
    AgentMessage,
    ConversationStatusAction,
    ErrorMessage,
    InterruptedMessage,
    MessageCopyButton,
    MessageHeaderRow,
    RetryMessage,
    StreamedMarkdownMessage,
    SystemMessage,
    ThinkingMessage,
    UserMessage,
    _AgentHeader,
    _AgentReplyHeader,
    _FoldRowHeader,
    _user_header_text,
    _UserHeader,
    _UserImagePreview,
    format_message_created_at,
)
from chrys.app.tui.widgets.chat.panel import ChatPanel
from chrys.app.tui.widgets.chat.tool_call import (
    ToolCall,
    ToolGroup,
)
from chrys.app.tui.widgets.markdown import VirtualizedMarkdown
from chrys.foundation.config.settings import Settings
from chrys.kernel import Content
from tests.support.images import image_bytes
from tests.support.pilot_barrier import screen_is_settled
from tests.support.tui_helpers import (
    ChatPanelApp,
    WidgetApp,
    click_when_settled,
    png_bytes,
)
from tests.support.waiting import wait_for


async def _rendered_body(pilot: Pilot[None], message: UserMessage) -> str:
    body = message.query_one(VirtualizedMarkdown)
    await wait_for(
        lambda: body.get_selection(SELECT_ALL) is not None,
        pilot=pilot,
        description="the user message body has rendered",
    )
    selected = body.get_selection(SELECT_ALL)
    assert selected is not None
    return selected[0]


async def test_user_message_renders_markdown_below_its_header() -> None:
    text = "Fix `List<String>` in <file>\nsecond **line**\n```py\nprint(1)\n```"

    async with WidgetApp(lambda: UserMessage(text, timestamp="- 4:32 AM")).run_test() as pilot:
        msg = pilot.app.query_one(UserMessage)
        body = await _rendered_body(pilot, msg)

        assert str(msg.query_one(_UserHeader).content) == "\u276f You - 4:32 AM"
        assert msg.query_one(VirtualizedMarkdown).source == text
        # Newlines stay line breaks and HTML-looking text stays as typed.
        assert body.splitlines()[:2] == ["Fix List<String> in <file>", "second line"]
        assert "print(1)" in body
        assert "```" not in body


async def test_user_message_shows_backslashes_in_a_path_as_typed() -> None:
    text = "open D:\\project\\.code and C:\\Users\\me\\_cache"

    async with WidgetApp(lambda: UserMessage(text)).run_test(size=(80, 20)) as pilot:
        msg = pilot.app.query_one(UserMessage)

        assert await _rendered_body(pilot, msg) == text


async def test_user_message_body_shows_control_characters_as_text() -> None:
    text = "red \x1b[31malert\x1b[0m\r\nnext"

    async with WidgetApp(lambda: UserMessage(text)).run_test() as pilot:
        msg = pilot.app.query_one(UserMessage)
        body = await _rendered_body(pilot, msg)

        assert "\x1b" not in body
        assert body.splitlines()[1] == "next"
        assert msg.text == text


async def test_user_message_renders_embedded_image_preview_below_a_blank_row() -> None:
    contents = [
        "look at @shot.png",
        Content.from_data(data=png_bytes((240, 80, 80)), media_type="image/png"),
    ]

    async with WidgetApp(lambda: UserMessage("look at @shot.png", contents=contents)).run_test(size=(80, 20)) as pilot:
        msg = pilot.app.query_one(UserMessage)
        preview = msg.query_one(_UserImagePreview)
        await wait_for(lambda: preview.content_size.height == 10, pilot=pilot, description="the image is laid out")

        assert len(msg._image_previews) == 1
        assert preview.region.height == 11
        assert preview.styles.padding.top == 1
        assert all(preview.render_line(y).text == "\u2580" * 32 for y in range(10))


async def test_user_message_with_embedded_image_keeps_text_selectable() -> None:
    text = "look at @shot.png"
    contents = [
        text,
        Content.from_data(data=png_bytes((240, 80, 80)), media_type="image/png"),
    ]

    async with WidgetApp(lambda: UserMessage(text, contents=contents)).run_test(size=(80, 20)) as pilot:
        msg = pilot.app.query_one(UserMessage)
        assert await _rendered_body(pilot, msg) == text
        text_widget = msg.query_one(VirtualizedMarkdown)
        image_widget = msg.query_one(_UserImagePreview)
        widget, offset = pilot.app.screen.get_widget_and_offset_at(text_widget.region.x + 6, text_widget.region.y)
        image_hit, image_offset = pilot.app.screen.get_widget_and_offset_at(
            image_widget.content_region.x, image_widget.content_region.y
        )

        assert widget is text_widget
        assert offset == Offset(6, 0)
        assert text_widget.get_selection(Selection(Offset(0, 0), Offset(len(text), 0))) == (text, "\n")
        assert image_hit is image_widget
        assert image_offset is None
        assert image_widget.allow_select is False
        assert image_widget.get_selection(Selection(None, None)) is None


async def test_user_message_copies_through_its_header_and_body() -> None:
    text = "selected **text**"

    async with WidgetApp(lambda: UserMessage(text)).run_test() as pilot:
        msg = pilot.app.query_one(UserMessage)
        await _rendered_body(pilot, msg)
        header = msg.query_one(_UserHeader)
        body = msg.query_one(VirtualizedMarkdown)
        pilot.app.screen.selections = {
            msg: SELECT_ALL,
            header: SELECT_ALL,
            body: SELECT_ALL,
        }

        assert msg.get_selection(SELECT_ALL) is None
        assert header.get_selection(SELECT_ALL) == ("[You]", "\n")
        assert pilot.app.screen.get_selected_text() == "[You]\nselected text"


async def test_an_injected_note_looks_like_a_user_message_indented_to_the_calls_around_it() -> None:
    """Between thinking or tool rows, which have no vertical margin, it keeps a blank row above and below."""
    typed = UserMessage("typed")
    above = ThinkingMessage("before", collapsed=True)
    note = UserMessage("note", is_injection=True)
    below = ThinkingMessage("after", collapsed=True)
    async with WidgetApp(lambda: [typed, above, note, below]).run_test() as pilot:
        await _rendered_body(pilot, note)
        typed_body = typed.query_one(VirtualizedMarkdown)
        note_body = note.query_one(VirtualizedMarkdown)

        assert str(note.query_one(_UserHeader).content) == "\u276f You"
        assert note.styles.border_left == typed.styles.border_left
        assert note_body.content_region.x - note.region.x == typed_body.content_region.x - typed.region.x
        assert note.region.x == above.query_one(MessageHeaderRow).content_region.x
        assert note.region.y == above.region.bottom + 1
        assert below.region.y == note.region.bottom + 1


async def test_a_user_message_ends_a_column_short_of_the_scrollbar() -> None:
    async with ChatPanelApp().run_test(size=(60, 20)) as pilot:
        panel = pilot.app.query_one(ChatPanel)
        await panel.add_user_message("\n\n".join(f"Line {number}." for number in range(20)))
        await panel.add_user_message("A note.", is_injection=True)
        typed, note = panel.query(UserMessage)
        # The scrollbar can show before the note is laid out: its region stays empty until then.
        await wait_for(
            lambda: panel.show_vertical_scrollbar and note.region and screen_is_settled(pilot.app, pilot.app.screen),
            pilot=pilot,
            description="the transcript scrolls with both messages laid out",
        )
        scrollbar = panel.vertical_scrollbar.region
        assert scrollbar.width == 1
        assert typed.region.right == note.region.right == scrollbar.x - 1


async def test_user_copy_button_shows_only_for_text_outside_compressed_history(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    contents = [Content.from_data(data=png_bytes((240, 80, 80)), media_type="image/png")]
    typed = UserMessage("typed")
    injected = UserMessage("injected", is_injection=True)
    compressed = UserMessage("archived", compressed=True)
    image_only = UserMessage("", contents=contents)

    async with WidgetApp(lambda: [typed, injected, compressed, image_only]).run_test() as pilot:
        copied: list[str] = []
        monkeypatch.setattr("chrys.app.tui.clipboard.clipboard_copy", copied.append)
        pilot.app.copy_to_clipboard("existing")
        await pilot.pause()

        assert typed.query_one(MessageCopyButton).display is True
        assert injected.query_one(MessageCopyButton).display is True
        assert compressed.query_one(MessageCopyButton).display is False
        assert image_only.query_one(MessageCopyButton).display is False

        image_only.copy_user_message()
        assert pilot.app.clipboard == "existing"
        assert copied == []


def test_image_preview_extracts_bounded_display_copy() -> None:
    previews = extract_image_previews(
        [Content.from_data(data=png_bytes((240, 80, 80), size=(1200, 800)), media_type="image/png")]
    )

    assert len(previews) == 1
    assert previews[0].image.width <= 512
    assert previews[0].image.height <= 512


def test_image_preview_extract_ignores_decompression_bomb(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    def raise_decompression_bomb(_data: object, *, formats: tuple[str, ...]) -> None:
        raise Image.DecompressionBombError("too large")

    monkeypatch.setattr(image_preview_module.Image, "open", raise_decompression_bomb)

    with caplog.at_level("DEBUG", logger=image_preview_module.__name__):
        assert extract_image_previews([Content.from_data(data=b"not decoded", media_type="image/png")]) == []
    assert "Skipping invalid chat image preview" in caplog.text


def test_image_preview_decodes_only_whitelisted_formats() -> None:
    previews = extract_image_previews(
        [
            Content.from_data(data=image_bytes("TIFF"), media_type="image/tiff"),
            Content.from_data(data=image_bytes("BMP"), media_type="image/bmp"),
        ]
    )

    assert [preview.media_type for preview in previews] == ["image/bmp"]


async def test_replay_history_decodes_image_previews_from_base64_contents() -> None:
    raw_messages = [
        {
            "role": "user",
            "contents": [
                {"type": "text", "text": "look at @a.png and @b.png"},
                Content.from_data(data=png_bytes((240, 80, 80)), media_type="image/png").to_dict(),
                Content.from_data(data=png_bytes((80, 180, 120)), media_type="image/png").to_dict(),
            ],
        }
    ]

    async with ChatPanelApp().run_test(size=(80, 20)) as pilot:
        panel = pilot.app.query_one(ChatPanel)
        await panel.replay_history(raw_messages)
        await pilot.pause()

        msg = panel.query_one(UserMessage)
        assert msg._text == "look at @a.png and @b.png"
        assert len(msg._image_previews) == 2
        preview = msg.query_one(_UserImagePreview)
        await wait_for(lambda: preview.content_size.height > 0, pilot=pilot, description="the images are laid out")
        assert "\u2580" in preview.render_line(0).text


async def test_replay_history_decodes_tool_result_images_from_base64_items() -> None:
    image = Content.from_data(
        data=png_bytes((240, 80, 80), size=(4, 4)),
        media_type="image/png",
        additional_properties={"width": 1977, "height": 1125, "media_type": "image/png"},
    )
    image_dict = image.to_dict()
    image_dict.pop("media_type")
    raw_messages = [
        {"role": "user", "contents": [{"type": "text", "text": "show image"}]},
        {
            "role": "assistant",
            "contents": [
                Content.from_function_call(
                    "tool-image",
                    "view_image",
                    arguments={"path": "plot.png"},
                ).to_dict()
            ],
        },
        {
            "role": "tool",
            "contents": [
                Content.from_function_result(
                    "tool-image",
                    result=[image],
                ).to_dict()
            ],
        },
    ]
    raw_messages[2]["contents"][0]["items"][0] = image_dict

    async with ChatPanelApp().run_test(size=(80, 20)) as pilot:
        panel = pilot.app.query_one(ChatPanel)
        await panel.replay_history(raw_messages)
        await pilot.pause()

        group = panel.query_one(ToolGroup)
        assert group.collapsed is True
        assert group._content_mounted is False
        record = next(iter(group._tool_records.values()))
        assert len(record.image_contents) == 1

        group.collapsed = False
        await wait_for(
            lambda: list(panel.query("#tc-images")), pilot=pilot, description='list(panel.query("#tc-images"))'
        )

        tool = panel.query_one(ToolCall)
        body = tool.query_one("#tc-body", Static)
        assert body.render().plain == ""
        image_panel = tool.query_one("#tc-panel")
        assert str(image_panel.border_subtitle) == "Resolution: 1977x1125 · Type: image/png"
        assert len(list(tool.query("#tc-images"))) == 1


def test_format_message_created_at_uses_local_time() -> None:
    created_at = datetime.now(UTC).replace(second=0, microsecond=0)
    local = created_at.astimezone()
    hour = local.hour % 12 or 12
    suffix = "AM" if local.hour < 12 else "PM"
    expected = f"- {hour}:{local.minute:02d} {suffix}"

    assert format_message_created_at(created_at) == expected
    assert format_message_created_at(created_at.isoformat()) == expected
    assert format_message_created_at("") == ""
    assert format_message_created_at("not-a-time") == ""
    assert format_message_created_at(123) == ""
    assert format_message_created_at({}) == ""


def test_message_headers_include_dim_timestamp_when_present() -> None:
    user = _user_header_text(timestamp="- 10:16 PM")
    agent = AgentMessage("hi", profile_name="Code Agent", timestamp="- 10:17 PM", duration_ms=2345)
    instant_agent = AgentMessage("hi", profile_name="Code Agent", duration_ms=0)

    assert user.plain == "\u276f You - 10:16 PM"
    assert agent._header_text().plain == "▾ Code Agent - 10:17 PM (2s)"
    assert instant_agent._header_text().plain == "▾ Code Agent (0ms)"


@pytest.mark.parametrize(
    ("locale", "agent_label", "button_text", "tooltip", "user_tooltip", "retry_text", "error_text"),
    [
        (
            "en",
            "Agent",
            "copy",
            "Copy raw agent response",
            "Copy your raw message",
            "✗ Error\ntemporary failure Retrying in 7s (2/4)...",
            "✗ Error\nsomething broke",
        ),
        (
            "zh-Hans",
            "智能体",
            "复制",
            "复制原始智能体回复",
            "复制你的原始消息",
            "✗ 错误\ntemporary failure 正在重试，等待 7 秒（2/4）...",  # noqa: RUF001
            "✗ 错误\nsomething broke",
        ),
    ],
)
async def test_agent_and_retry_chrome_render_at_mount_locale(
    locale: str,
    agent_label: str,
    button_text: str,
    tooltip: str,
    user_tooltip: str,
    retry_text: str,
    error_text: str,
) -> None:
    user = UserMessage("hello")
    agent = AgentMessage("<think>one\n\ntwo</think>", is_intermediate=True)
    retry = RetryMessage("temporary failure", attempt=2, max_attempts=4, delay_seconds=7)
    error = ErrorMessage("something broke")

    class MessageApp(App):
        def __init__(self) -> None:
            self.locale_controller = LocaleController(Settings(locale=locale))
            super().__init__()

        def compose(self) -> ComposeResult:
            yield user
            yield agent
            yield retry
            yield error

    async with MessageApp().run_test() as pilot:
        await pilot.pause()

        assert agent._copy_label() == agent_label
        assert agent._header_text().plain == f"▾ {agent_label}"
        # Think tags are answer text, shown as written in every locale.
        assert agent.text == "<think>one\n\ntwo</think>"
        copy_button = agent.query_one(MessageCopyButton)
        assert copy_button.render().plain == button_text
        assert copy_button.tooltip == tooltip
        user_copy_button = user.query_one(MessageCopyButton)
        assert user_copy_button.render().plain == button_text
        assert user_copy_button.tooltip == user_tooltip
        assert retry.render().plain == retry_text
        assert error._render_text().plain == error_text


def test_streaming_agent_header_timestamp_updates_on_final() -> None:
    agent = AgentMessage("partial", is_final=False, profile_name="Code Agent", timestamp="- 10:16 PM")

    agent.stream_update("done", is_final=True, timestamp="- 10:17 PM")

    assert agent._header_text().plain == "▾ Code Agent - 10:17 PM"


def test_agent_message_render_lines_unattached_returns_blank() -> None:
    rendered = AgentMessage("hello").render_lines(Region(0, 0, 20, 2))

    assert [strip.cell_length for strip in rendered] == [20, 20]


async def test_agent_message_streaming() -> None:
    async with WidgetApp(lambda: AgentMessage("partial", is_final=False)).run_test() as pilot:
        msg = pilot.app.query_one(AgentMessage)
        assert msg.text == "partial"
        assert msg._is_final is False
        assert msg.has_class("--streaming")

        msg.stream_update("full response", is_final=True)
        assert msg.text == "full response"
        assert msg._is_final is True


@pytest.mark.parametrize("answer", [True, False], ids=["answer", "text_before_tools"])
async def test_streamed_text_stays_muted_until_it_is_known_to_be_the_answer(answer: bool) -> None:
    async with WidgetApp(lambda: AgentMessage("partial", is_final=False)).run_test() as pilot:
        msg = pilot.app.query_one(AgentMessage)
        streaming_border = msg.styles.border_left

        if answer:
            msg.stream_update("partial", is_final=True)
        else:
            msg.settle_intermediate()
        await pilot.pause()

        assert not msg.has_class("--streaming")
        if answer:
            assert msg.styles.border_left != streaming_border
        else:
            # Text between tool calls draws the thin guide thinking and tool groups draw, in the colour it streamed in.
            assert msg.styles.border_left == ("solid", streaming_border[1])


async def test_a_growing_streamed_answer_never_shows_a_scrollbar_of_its_own(monkeypatch: pytest.MonkeyPatch) -> None:
    """The body is laid out before the message grows to fit it: a scrollbar then would narrow it for a frame."""
    scrollbar_after_layout: list[bool] = []
    layout_blocks = VirtualizedMarkdown._layout_blocks

    def layout_spy(self: VirtualizedMarkdown, width: int | None = None) -> None:
        layout_blocks(self, width)
        scrollbar_after_layout.append(self.show_vertical_scrollbar)

    monkeypatch.setattr(VirtualizedMarkdown, "_layout_blocks", layout_spy)
    async with WidgetApp(lambda: AgentMessage("```", is_final=False)).run_test(size=(60, 20)) as pilot:
        msg = pilot.app.query_one(AgentMessage)
        body = msg.query_one(VirtualizedMarkdown)
        text = "```"
        for line in range(8):
            layouts = len(scrollbar_after_layout)
            text += f"\nline {line}"
            msg.stream_update(text)
            await wait_for(
                lambda layouts=layouts: len(scrollbar_after_layout) > layouts,
                pilot=pilot,
                description="the grown answer laid out",
            )
        await wait_for(lambda: body.size.height > 8, pilot=pilot, description="the message grew to fit its answer")

        assert True not in scrollbar_after_layout


async def test_agent_message_shows_html_looking_text_as_written() -> None:
    text = "Fix List<String> in <file>.\n\n<Widget>\nbody\n</Widget>\n\n| a |\n|---|\n| one<br>two |"

    async with WidgetApp(lambda: AgentMessage(text)).run_test(size=(80, 20)) as pilot:
        body = pilot.app.query_one(AgentMessage).query_one(VirtualizedMarkdown)
        await wait_for(
            lambda: body.get_selection(SELECT_ALL) is not None,
            pilot=pilot,
            description="the agent message body has rendered",
        )
        selected = body.get_selection(SELECT_ALL)
        assert selected is not None
        rendered = selected[0]

        assert "Fix List<String> in <file>." in rendered
        assert "<Widget> body </Widget>" in rendered
        assert "│ one │" in rendered
        assert "│ two │" in rendered


async def test_clicking_an_agent_reply_header_folds_and_unfolds_its_body() -> None:
    async with WidgetApp(lambda: AgentMessage("hello", profile_name="Code Agent")).run_test() as pilot:
        msg = pilot.app.query_one(AgentMessage)
        header = msg.query_one(_AgentHeader)

        await click_when_settled(pilot, header)
        await wait_for(lambda: msg.collapsed, pilot=pilot, description="reply folded")
        await click_when_settled(pilot, header)
        await wait_for(lambda: not msg.collapsed, pilot=pilot, description="reply unfolded")


async def test_agent_copy_button_sits_next_to_header_timestamp() -> None:
    async with WidgetApp(
        lambda: AgentMessage("hello", profile_name="Code Agent", timestamp="- 1:23 PM")
    ).run_test() as pilot:
        await pilot.pause()
        msg = pilot.app.query_one(AgentMessage)
        header = msg.query_one(".agent-header", Static)
        copy_button = msg.query_one(MessageCopyButton)

        assert copy_button.display is True
        assert copy_button.region.x == header.region.right


async def test_empty_agent_message_hides_copy_button(monkeypatch: pytest.MonkeyPatch) -> None:
    async with WidgetApp(
        lambda: AgentMessage("", profile_name="Code Agent", timestamp="- 1:23 PM")
    ).run_test() as pilot:
        copied: list[str] = []
        monkeypatch.setattr("chrys.app.tui.clipboard.clipboard_copy", copied.append)
        pilot.app.copy_to_clipboard("existing")
        await pilot.pause()

        msg = pilot.app.query_one(AgentMessage)
        copy_button = msg.query_one(MessageCopyButton)

        assert copy_button.display is False

        msg.copy_agent_response()
        assert pilot.app.clipboard == "existing"
        assert copied == []


async def test_structured_completion_checkmark_is_styled_and_not_copyable() -> None:
    async with WidgetApp(
        lambda: AgentMessage("✓", profile_name="Code Agent", is_structured_completion=True)
    ).run_test() as pilot:
        await pilot.pause()
        msg = pilot.app.query_one(AgentMessage)

        assert msg.has_class("--structured-completion")
        assert msg.query_one(MessageCopyButton).display is False


@pytest.mark.parametrize(
    ("make", "indent"),
    [
        (lambda: AgentMessage("hello", profile_name="Code Agent"), 0),
        (lambda: AgentMessage("hello", is_intermediate=True), len(f"{OPEN_ARROW} ") + 2),
        (lambda: ThinkingMessage("hello"), len(f"{OPEN_ARROW} ") + 2),
    ],
    ids=["answer", "between_tool_calls", "thinking"],
)
async def test_text_inside_a_turn_starts_two_columns_in_from_its_header_label_and_an_answer_under_its_arrow(
    make, indent
) -> None:
    """An answer's body has no gutter, so it stays on the direct markdown path; text inside a turn draws its guide."""
    async with WidgetApp(make).run_test() as pilot:
        await pilot.pause()
        msg = pilot.app.query_one(StreamedMarkdownMessage)
        header = msg.query_one(MessageHeaderRow)
        body = msg.query_one(VirtualizedMarkdown)

        assert body.content_region.x == header.content_region.x + indent
        assert body._can_render_lines_direct(Region(0, 0, body.size.width, body.size.height)) is (indent == 0)


@pytest.mark.parametrize(
    "make",
    [lambda: AgentMessage("hello\nworld", is_intermediate=True), lambda: ThinkingMessage("hello\nworld")],
    ids=["between_tool_calls", "thinking"],
)
async def test_a_guide_body_draws_its_guide_and_blank_row_itself_so_the_message_chrome_stays_one_strip(make) -> None:
    """The guide under the fold arrow and the blank row under the text are the body's: a vertical gutter on the
    message would end the shared chrome strip its rows render from."""
    async with WidgetApp(make).run_test() as pilot:
        await pilot.pause()
        msg = pilot.app.query_one(StreamedMarkdownMessage)
        header = msg.query_one(MessageHeaderRow)
        body = msg.query_one(VirtualizedMarkdown)
        rows = msg.render_lines(Region(0, 0, msg.size.width, 3))

        assert msg.styles.gutter.height == 0
        assert msg._background_strip_cache is not None
        assert all(strip is msg._background_strip_cache for strip in rows)
        assert body.styles.border_left[0] == "solid"
        assert body.region.x == header.content_region.x
        assert msg.region.bottom == body.region.bottom == body.content_region.bottom + 1


async def test_agent_message_chrome_render_cache_reuses_rows_and_tracks_theme() -> None:
    async with WidgetApp(lambda: AgentMessage("hello\nworld")).run_test() as pilot:
        await pilot.pause()
        msg = pilot.app.query_one(AgentMessage)
        crop = Region(0, 0, msg.size.width, 3)

        first = msg.render_lines(crop)
        cached = msg._background_strip_cache
        cached_key = msg._background_strip_cache_key

        assert cached is not None
        assert cached_key is not None
        assert all(strip is cached for strip in first)

        taller = msg.render_lines(Region(0, 0, msg.size.width, 5))
        assert msg._background_strip_cache is cached
        assert all(strip is cached for strip in taller)

        next_color_system = (
            ColorSystem.STANDARD if str(pilot.app.console.color_system) == "256" else ColorSystem.EIGHT_BIT
        )
        pilot.app.console._color_system = next_color_system
        recolored = msg.render_lines(crop)

        assert msg._background_strip_cache_key != cached_key
        assert all(strip is msg._background_strip_cache for strip in recolored)

        theme_key = msg._background_strip_cache_key
        pilot.app.theme = "textual-light"
        await pilot.pause()
        themed = msg.render_lines(crop)

        assert msg._background_strip_cache_key != theme_key
        assert all(strip is msg._background_strip_cache for strip in themed)


async def test_error_message_renders() -> None:
    async with WidgetApp(lambda: ErrorMessage("something broke")).run_test() as pilot:
        msg = pilot.app.query_one(ErrorMessage)
        rendered = msg._render_text()
        assert "something broke" in rendered.plain
        assert "Error" in rendered.plain


async def test_chrys_conversation_status_rails_keep_semantic_colors() -> None:
    class MsgApp(TuiVariableDefaultsMixin, App):
        def __init__(self) -> None:
            super().__init__()
            self.register_theme(CHRYS_THEME)
            self.theme = CHRYS_THEME.name

        def compose(self) -> ComposeResult:
            yield ErrorMessage("something broke")
            yield RetryMessage("temporary failure", attempt=1, max_attempts=3, delay_seconds=1)
            yield InterruptedMessage("Execution interrupted", "user")
            yield InterruptedMessage("Execution interrupted", "error")

    async with MsgApp().run_test() as pilot:
        variables = pilot.app.get_css_variables()
        assert variables["tui-border-status-error"] == variables["error"]
        assert variables["tui-border-status-warning"] == variables["warning"]
        widgets = [
            (pilot.app.query_one(ErrorMessage), "error"),
            (pilot.app.query_one(RetryMessage), "error"),
            *zip(pilot.app.query(InterruptedMessage), ("warning", "error"), strict=True),
        ]
        for widget, semantic_color in widgets:
            border_color = widget.styles.border_left[1]
            expected_color = TextualColor.parse(variables[semantic_color])
            assert border_color.r == pytest.approx(expected_color.r, abs=1)
            assert border_color.g == pytest.approx(expected_color.g, abs=1)
            assert border_color.b == pytest.approx(expected_color.b, abs=1)
            assert border_color.a == 0.8
            body = widget if isinstance(widget, RetryMessage) else widget.query_one(".status-body", Static)
            assert body.styles.color == expected_color


@pytest.mark.parametrize("widget_type", [ErrorMessage, InterruptedMessage])
async def test_conversation_status_action_waits_for_admission_before_hiding(widget_type) -> None:
    pressed: list[bool] = []

    class MsgApp(App):
        def compose(self) -> ComposeResult:
            yield widget_type("something broke", action_label="Retry")

        @on(ConversationStatusAction.Pressed)
        def on_status_action_pressed(self, event: ConversationStatusAction.Pressed) -> None:
            event.stop()
            pressed.append(True)

    async with MsgApp().run_test() as pilot:
        action = pilot.app.query_one(widget_type)
        button = action.query_one(Button)
        action.on_button_pressed(Button.Pressed(button))
        await pilot.pause()

        assert pressed == [True]
        assert button.parent is not None
        assert button.parent.display is True
        action.set_action_disabled(True)
        assert button.disabled
        action.on_button_pressed(Button.Pressed(button))
        await pilot.pause()
        assert pressed == [True]
        assert button.parent.display is True
        action.set_action_disabled(False)
        action.on_button_pressed(Button.Pressed(button))
        await pilot.pause()
        assert pressed == [True, True]
        action.hide_action()
        assert button.parent.display is False


async def test_system_message_renders() -> None:
    async with WidgetApp(lambda: SystemMessage("session started")).run_test() as pilot:
        msg = pilot.app.query_one(SystemMessage)
        # SystemMessage passes text to Static constructor
        assert msg is not None


async def test_fold_row_headers_built_under_a_running_app_restyle_nothing_until_they_mount() -> None:
    """A transcript's replay builds a header per message: mounting styles each, so building one styles none."""
    async with ChatPanelApp().run_test() as pilot:
        with patch.object(pilot.app.stylesheet, "update_nodes", autospec=True) as update_nodes:
            headers = [
                _FoldRowHeader(THOUGHT_LABEL.bind(), collapsed=True, text="Look."),
                _AgentReplyHeader(AGENT_LABEL.fallback, None, collapsed=True, text="Next."),
                _AgentReplyHeader(AGENT_LABEL.fallback, Text("Agent"), collapsed=False, text="Done."),
            ]
        assert update_nodes.call_count == 0
        assert [header.has_class("fold-row-header") for header in headers] == [True, True, False]
