# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tests for ConversationToc and summarize_prompt: summary length, scrollbar geometry, and tab expansion before render."""

from __future__ import annotations

from unittest.mock import Mock

import pytest
from rich.text import Text
from textual.geometry import Region
from textual.widgets import Tree

from chrys.app.tui.widgets.sidebar.toc import ConversationToc, TocItem, summarize_prompt
from tests.support.tui_helpers import (
    WidgetApp,
)
from tests.support.waiting import wait_for


def test_conversation_toc_default_summary_keeps_longer_prompt() -> None:
    prompt = "x" * 100
    assert summarize_prompt(prompt) == prompt

    long_prompt = "x" * 121
    assert summarize_prompt(long_prompt) == ("x" * 117) + "..."


async def test_conversation_toc_scrollbar_is_one_cell_and_flush_right() -> None:
    async with WidgetApp(ConversationToc).run_test(size=(50, 10)) as pilot:
        toc = pilot.app.query_one(ConversationToc)
        toc.update_items([TocItem(turn_id=f"turn-{i}", summary=f"Turn {i}") for i in range(20)])
        tree = pilot.app.query_one("#toc-tree", Tree)
        box = toc.query_one("#toc-box")

        await wait_for(
            lambda: tree.vertical_scrollbar.region.width == 1,
            pilot=pilot,
            description="TOC scrollbar laid out",
        )

        assert tree.styles.scrollbar_size_vertical == 1
        assert tree.region.x == box.content_region.x
        assert tree.vertical_scrollbar.region.right == box.region.right - 1


@pytest.mark.parametrize("entries", [20, 2], ids=["scrolling", "fitting"])
async def test_conversation_toc_keeps_a_clear_column_after_a_long_entry(entries: int) -> None:
    """Between the entry and the scrollbar when there is one, and the panel's edge when there is not."""
    async with WidgetApp(ConversationToc).run_test(size=(50, 10)) as pilot:
        toc = pilot.app.query_one(ConversationToc)
        toc.update_items([TocItem(turn_id=f"turn-{i}", summary="x" * 100) for i in range(entries)])
        tree = pilot.app.query_one("#toc-tree", Tree)
        scrolling = entries > 10
        await wait_for(
            lambda: tree.size.width and tree.show_vertical_scrollbar == scrolling,
            pilot=pilot,
            description="TOC laid out",
        )
        tree.cursor_line = 0
        await wait_for(lambda: tree.cursor_line == 0, pilot=pilot, description="TOC cursor on the first entry")

        # What shows of a line is what the scrollbar leaves of it.
        width = tree.scrollable_content_region.width
        assert width == tree.content_size.width - scrolling
        left = tree.styles.padding.left
        highlighted, plain = tree.render_lines(Region(left, 0, width, 2))

        for row, line in ((highlighted, 0), (plain, 1)):
            assert row.text.endswith("x ") and not row.text.endswith("  ")
            # The clear cell is still the row: its background, and the line a click on it selects.
            entry, clear = list(row.crop(width - 2, width))[-2:]
            assert clear.style is not None and entry.style is not None
            assert clear.style.bgcolor == entry.style.bgcolor
            assert clear.style.meta["line"] == line
        assert list(highlighted)[-1].style.bgcolor != list(plain)[-1].style.bgcolor


async def test_conversation_toc_label_keeps_longer_summary() -> None:
    summary = "x" * 100
    async with WidgetApp(ConversationToc).run_test() as pilot:
        toc = pilot.app.query_one(ConversationToc)
        toc.update_items([TocItem(turn_id="turn-1", summary=summary)])
        await pilot.pause()

        tree = pilot.app.query_one("#toc-tree", Tree)
        label = tree.root.children[0].label
        assert isinstance(label, Text)
        assert label.plain == f"1. {summary}"


async def test_conversation_toc_expands_tabs_before_tree_render() -> None:
    summary = "样例\t文本 mock\t数据"
    async with WidgetApp(ConversationToc).run_test(size=(70, 10)) as pilot:
        toc = pilot.app.query_one(ConversationToc)
        toc.update_items([TocItem(turn_id="turn-1", summary=summary)])
        await pilot.pause()

        tree = pilot.app.query_one("#toc-tree", Tree)
        label = tree.root.children[0].label
        assert isinstance(label, Text)
        assert label.plain == f"1. {summary}".expandtabs(8)

        rendered = "".join(segment.text for segment in tree.render_line(0)._segments).rstrip()
        assert "\t" not in rendered
        assert rendered == f"1. {summary}".expandtabs(8)


async def test_live_cleanup_removes_only_retired_nodes_and_keeps_expansion_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    kept = TocItem(
        turn_id="turn-kept",
        summary="Earlier prompt",
        turn_index=4,
        children=[TocItem(turn_id="inj-kept", summary="Earlier note", turn_index=4)],
    )
    retired = TocItem(turn_id="turn-retired", summary="Empty interrupted prompt", turn_index=5)
    pending = TocItem(turn_id="turn-new", summary="New prompt")
    async with WidgetApp(ConversationToc).run_test() as pilot:
        toc = pilot.app.query_one(ConversationToc)
        toc.update_live_items([kept, retired, pending])
        tree = toc.query_one("#toc-tree", Tree)
        first, removed, newest = tree.root.children
        child = first.children[0]
        first.collapse()
        tree.select_node(first)
        await pilot.pause()
        clear = Mock(side_effect=AssertionError("live changes must preserve the tree"))
        unchanged_label = Mock(wraps=first.set_label)
        monkeypatch.setattr(tree, "clear", clear)
        monkeypatch.setattr(first, "set_label", unchanged_label)

        pending.turn_index = 5
        toc.update_live_items([kept, pending])

        assert list(tree.root.children) == [first, newest]
        assert removed not in tree.root.children
        assert first.children[0] is child
        assert first.is_expanded is False
        assert tree.cursor_node is first
        assert newest.label == Text("5. New prompt")
        clear.assert_not_called()
        unchanged_label.assert_not_called()


async def test_live_toc_waits_for_authoritative_index_and_updates_only_its_label(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    item = TocItem(turn_id="turn-pending", summary="Pending opener")
    async with WidgetApp(ConversationToc).run_test() as pilot:
        toc = pilot.app.query_one(ConversationToc)
        toc.update_live_items([item])
        tree = toc.query_one("#toc-tree", Tree)
        node = tree.root.children[0]
        assert node.label == Text("Pending opener")
        update_label = Mock(wraps=node.set_label)
        monkeypatch.setattr(node, "set_label", update_label)

        item.turn_index = 42
        toc.update_live_items([item])
        toc.update_live_items([item])

        assert tree.root.children[0] is node
        assert node.label == Text("42. Pending opener")
        update_label.assert_called_once_with(Text("42. Pending opener"))
