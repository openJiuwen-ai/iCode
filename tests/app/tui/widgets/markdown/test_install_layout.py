# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Markdown installed before its first arrange is laid out once, at the real width.

A freshly mounted (or hidden) ``VirtualizedMarkdown`` finishes its parse before
Textual has given it a width. Laying out then used a placeholder width that the
first arrange threw away and redid — every replayed answer paid for two layouts
and its parent chat panel saw a height change between them.
"""

from __future__ import annotations

import pytest
from textual.app import App, ComposeResult
from textual.containers import Container
from textual.content import Content, Span
from textual.geometry import Region

from chrys.app.tui.widgets.chat.panel import ChatPanel
from chrys.app.tui.widgets.markdown.widget import VirtualizedMarkdown, _text_height
from chrys.foundation.models.history_markers import HistoryMarkerKind
from tests.support.pilot_barrier import screen_is_settled
from tests.support.tui_helpers import ChatPanelApp, resize_when_settled
from tests.support.waiting import wait_for

_DOCUMENT = "# Title\n\n" + "\n\n".join(
    f"Paragraph {index} with enough words to wrap at a narrow width, twice over." for index in range(6)
)


def _record_layout_widths(monkeypatch: pytest.MonkeyPatch) -> list[tuple[VirtualizedMarkdown, int]]:
    """Record the width every layout pass of any markdown widget actually used."""
    layouts: list[tuple[VirtualizedMarkdown, int]] = []
    original = VirtualizedMarkdown._layout_blocks

    def layout_spy(self: VirtualizedMarkdown, width: int | None = None, *, unchanged_prefix: int = 0) -> None:
        layouts.append((self, self.scrollable_content_region.width if width is None else width))
        original(self, width, unchanged_prefix=unchanged_prefix)

    monkeypatch.setattr(VirtualizedMarkdown, "_layout_blocks", layout_spy)
    return layouts


def _laid_out_at_current_width(widget: VirtualizedMarkdown) -> bool:
    width = widget.scrollable_content_region.width
    return width > 0 and widget._width_at_last_layout == width and bool(widget._block_line_info)


class _HiddenMarkdownApp(App[None]):
    def compose(self) -> ComposeResult:
        with Container(id="host"):
            yield VirtualizedMarkdown(_DOCUMENT)

    def on_mount(self) -> None:
        self.query_one("#host").display = False


async def test_markdown_parsed_while_unarranged_defers_layout_to_the_real_width(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    layouts = _record_layout_widths(monkeypatch)
    app = _HiddenMarkdownApp()
    async with app.run_test(size=(60, 20)) as pilot:
        markdown = app.query_one(VirtualizedMarkdown)
        await wait_for(lambda: bool(markdown._blocks), pilot=pilot, description="hidden markdown parsed")

        # Parsed but never arranged: no layout at a stand-in width, and the
        # constructor's line estimate still sizes the widget.
        assert markdown.scrollable_content_region.width == 0
        assert layouts == []
        assert markdown._width_at_last_layout == 0
        assert markdown._block_line_info == []
        assert markdown.virtual_size.height == _DOCUMENT.count("\n") + 1

        app.query_one("#host").display = True
        await wait_for(lambda: _laid_out_at_current_width(markdown), pilot=pilot, description="shown markdown laid out")

        real_width = markdown.scrollable_content_region.width
        assert [width for _widget, width in layouts] == [real_width]
        reference = VirtualizedMarkdown()
        reference._blocks = reference._build_blocks(_DOCUMENT)
        reference._layout_blocks(real_width)
        assert markdown.virtual_size.height == reference.virtual_size.height


class _HiddenRowMarkdownApp(_HiddenMarkdownApp):
    """Sized by its content, as a transcript row is."""

    CSS = "VirtualizedMarkdown { height: auto; }"


async def test_an_empty_document_installed_while_unarranged_shows_no_rows() -> None:
    """The arrange-time layout skips a widget without blocks, so emptying must not wait for it."""
    app = _HiddenRowMarkdownApp()
    async with app.run_test(size=(60, 20)) as pilot:
        markdown = app.query_one(VirtualizedMarkdown)
        await wait_for(lambda: bool(markdown._blocks), pilot=pilot, description="hidden markdown parsed")
        assert markdown.virtual_size.height == _DOCUMENT.count("\n") + 1

        await markdown.update("")
        assert markdown._blocks == []
        assert markdown.scrollable_content_region.width == 0
        assert markdown.virtual_size.height == 0

        app.query_one("#host").display = True
        await wait_for(lambda: screen_is_settled(app, app.screen), pilot=pilot, description="shown markdown settled")
        assert markdown.virtual_size.height == 0
        assert markdown.size.height == 0
        assert markdown.max_scroll_y == 0


async def test_replacing_blocks_while_unarranged_drops_the_old_block_layout(monkeypatch: pytest.MonkeyPatch) -> None:
    app = App[None]()
    async with app.run_test(size=(60, 20)) as pilot:
        markdown = VirtualizedMarkdown(_DOCUMENT)
        await app.mount(markdown)
        await wait_for(lambda: _laid_out_at_current_width(markdown), pilot=pilot, description="markdown laid out")
        assert len(markdown._block_line_info) > 1

        monkeypatch.setattr(type(markdown), "scrollable_content_region", property(lambda _self: Region(0, 0, 0, 0)))
        markdown._install_blocks(markdown._build_blocks("# Only"))

        # Old line info indexed the replaced blocks; nothing may keep it.
        assert markdown._block_line_info == []
        assert markdown._total_lines == 0
        assert markdown._width_at_last_layout == 0
        assert [title for _level, title, _block_id in markdown.table_of_contents] == ["Only"]
        assert markdown.goto_anchor("only")


async def test_replayed_transcript_lays_markdown_out_only_at_real_widths(monkeypatch: pytest.MonkeyPatch) -> None:
    """A populated restore never pays for a placeholder-width markdown layout."""
    layouts = _record_layout_widths(monkeypatch)
    messages: list[dict[str, object]] = []
    for turn in range(12):
        messages.append({"role": "user", "contents": [{"type": "text", "text": f"question {turn}"}]})
        fence = "\n".join(f"value_{line} = {line} * {turn}" for line in range(8))
        answer = f"## Answer {turn}\n\n{_DOCUMENT}\n\n```python\n{fence}\n```\n"
        messages.append({"role": "assistant", "contents": [{"type": "text", "text": answer}]})
        messages.append(
            {"role": "system", "contents": [], "additional_properties": {HistoryMarkerKind.KEY: HistoryMarkerKind.TURN}}
        )

    async with ChatPanelApp().run_test(size=(100, 30)) as pilot:
        panel = pilot.app.query_one(ChatPanel)
        # Replaying behind another foreground surface: nothing is arranged
        # until the panel is shown again, so every parse finishes unarranged.
        panel.display = False
        await panel.replay_history(messages)
        await panel.wait_replay_complete()
        markdowns = list(panel.query(VirtualizedMarkdown))
        # Every turn renders its question and its answer as markdown.
        assert len(markdowns) == 24
        assert all(markdown._blocks for markdown in markdowns)
        assert layouts == []

        panel.display = True
        await wait_for(
            lambda: all(_laid_out_at_current_width(markdown) for markdown in markdowns),
            pilot=pilot,
            description="replayed markdown laid out at the panel width",
        )

        assert all(width > 0 for _widget, width in layouts)
        # Each widget's layouts ran at real widths only; the last one is the
        # width it shows at.
        for markdown in markdowns:
            widths = [width for widget, width in layouts if widget is markdown]
            assert widths
            assert widths[-1] == markdown.scrollable_content_region.width


@pytest.mark.parametrize(
    "text",
    ["", " ", " " * 101, "two words " * 9, "a" * 80, "中文文字", "👍🏽e\u0301👩‍💻🇯🇵" * 5, "a\nb\n\n", "中\t文"],
)
def test_text_height_matches_textual_on_repeated_rewrap(text: str) -> None:
    measured = Content(text, [Span(0, len(text), "bold")])
    reference = Content(text, [Span(0, len(text), "bold")])
    for width in (1, 2, 3, 5, 12, 40, 80, 80, 1):
        assert _text_height(measured, width) == reference.get_height({}, width)


async def test_streamed_layout_keeps_the_identical_prefix_and_remeasures_on_resize() -> None:
    app = App[None]()
    async with app.run_test(size=(70, 26)) as pilot:
        streamed = VirtualizedMarkdown()
        reference = VirtualizedMarkdown()
        streamed.styles.overflow_y = reference.styles.overflow_y = "hidden"
        await app.mount(streamed, reference)
        await wait_for(lambda: streamed.scrollable_content_region.width > 0, pilot=pilot)
        streamed.set_streaming(True)
        first = """# Layout

| Field | Value |
| --- | --- |
| First | very wide payload next to 中文和日本語 |

```python
print("stable fence")
```

```mermaid
graph TD
  A --> B
```

After the diagram.
"""
        second = first + "\nThe last paragraph changes and wraps. " * 8
        await streamed.update(first)
        initial_layout = tuple(streamed._block_line_info)
        table_layout = next(iter(streamed._table_layouts.values()))

        await streamed.update(second)
        for before, after in zip(initial_layout, streamed._block_line_info, strict=False):
            assert after is before
        assert next(iter(streamed._table_layouts.values())) is table_layout

        await reference.update(second)

        def assert_matches_full_render() -> None:
            assert streamed.virtual_size == reference.virtual_size
            assert streamed._block_line_info == reference._block_line_info
            assert streamed._diagram_action_lines == reference._diagram_action_lines
            for line in range(streamed.virtual_size.height):
                assert list(streamed.render_line(line)) == list(reference.render_line(line))

        assert_matches_full_render()
        old_width = streamed._width_at_last_layout
        await resize_when_settled(pilot, 42, 26)
        await wait_for(
            lambda: all(
                _laid_out_at_current_width(widget) and widget._width_at_last_layout != old_width
                for widget in (streamed, reference)
            ),
            pilot=pilot,
            description="both documents have wrapped at the new width",
        )
        assert_matches_full_render()
        assert next(iter(streamed._table_layouts.values())) is not table_layout

        # A replacement removes the cached action line and measured offset.
        await streamed.update("Unrelated text")
        assert streamed._diagram_action_lines == {}
        assert len(streamed._block_line_info) == 1
