# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tests for the GuideDialog modal."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from textual.app import App, ComposeResult
from textual.pilot import Pilot
from textual.widgets import Static, Tree

from chrys.app.tui.screens.guides.screen import GuideDialog
from chrys.app.tui.widgets import HatchedEmptyState
from chrys.app.tui.widgets.markdown.widget import VirtualizedMarkdown
from tests.support.waiting import wait_for, wait_until

_INDEX = """\
default: intro
locales:
  - zh-Hans
  - en
topics:
  - id: intro
    path: start/what-is-chrys.md
  - id: getting_started
    path: start/getting-started.md
  - id: configuration
    children:
      - id: mcp
        path: guides/configuration/mcp.md
"""

_DEFAULT_TITLE = "什么是 Chrys"


@pytest.fixture
def guide_docs(tmp_path: Path) -> Path:
    (tmp_path / "index.yaml").write_text(_INDEX, encoding="utf-8")
    (tmp_path / "zh-Hans" / "start").mkdir(parents=True)
    (tmp_path / "zh-Hans" / "start" / "what-is-chrys.md").write_text(
        f"# {_DEFAULT_TITLE}\n\nChrys 是一个智能体平台。\n\n[MCP 配置](../guides/configuration/mcp.md)",
        encoding="utf-8",
    )
    (tmp_path / "zh-Hans" / "start" / "getting-started.md").write_text(
        "# 开始使用\n\n安装并运行 Chrys。\n\n[MCP 锚点](../guides/configuration/mcp.md#连接-mcp-服务器)",
        encoding="utf-8",
    )
    (tmp_path / "zh-Hans" / "guides" / "configuration").mkdir(parents=True)
    (tmp_path / "zh-Hans" / "guides" / "configuration" / "mcp.md").write_text(
        "# 连接 MCP 服务器\n\n## 会话\n\nMCP 配置说明。", encoding="utf-8"
    )
    return tmp_path


class _DialogHost(App):
    def compose(self) -> ComposeResult:
        yield Static("placeholder")


class _StubLocaleController:
    """Records surface registration; reports a fixed effective locale."""

    def __init__(self, effective_locale: str = "") -> None:
        self.registered: list[object] = []
        self.unregistered: list[object] = []
        self.effective_locale = effective_locale

    @property
    def localizer(self) -> _StubLocaleController:
        return self

    def register_surface(self, surface: object) -> None:
        self.registered.append(surface)

    def unregister_surface(self, surface: object) -> None:
        self.unregistered.append(surface)


def _zh_dialog(docs_root: Path) -> GuideDialog:
    """A dialog whose app locale is zh-Hans, the only language the fixture docs have."""
    return GuideDialog(locale_controller=_StubLocaleController("zh-Hans"), docs_root=docs_root)


def _markdown_text(dialog: GuideDialog) -> str:
    return dialog.query_one("#gd-doc", VirtualizedMarkdown)._markdown


async def _wait_for_document(dialog: GuideDialog, pilot: Pilot, text: str, description: str) -> None:
    await wait_for(
        lambda: dialog.query_one("#gd-doc").display and text in _markdown_text(dialog),
        pilot=pilot,
        description=description,
    )


async def _wait_for_default_document(dialog: GuideDialog, pilot: Pilot) -> None:
    await _wait_for_document(dialog, pilot, _DEFAULT_TITLE, "default document to finish loading")


def _cursor_on_active_topic(dialog: GuideDialog) -> bool:
    tree = dialog.query_one("#gd-tree", Tree)
    return tree.cursor_node is not None and tree.cursor_node.data is dialog._active_topic


@pytest.mark.asyncio
async def test_dialog_shows_default_topic_from_h1(guide_docs: Path) -> None:
    dialog = _zh_dialog(guide_docs)
    app = _DialogHost()

    async with app.run_test(size=(120, 40)) as pilot:
        await app.push_screen(dialog)
        await _wait_for_default_document(dialog, pilot)

        assert dialog.query_one("#gd-empty").display is False
        tree = dialog.query_one("#gd-tree", Tree)
        assert tree.root.children[0].label.plain == _DEFAULT_TITLE
        assert dialog._active_topic is not None and dialog._active_topic.id == "intro"


@pytest.mark.asyncio
async def test_default_topic_is_highlighted_when_it_is_not_the_first_row(guide_docs: Path) -> None:
    """The document on screen and the tree cursor agree from the start,
    even when a branch row comes before the default topic."""
    (guide_docs / "index.yaml").write_text(
        _INDEX.replace("default: intro", "default: mcp").replace(
            "  - id: intro\n    path: start/what-is-chrys.md\n",
            "  - id: start\n    children:\n      - id: intro\n        path: start/what-is-chrys.md\n",
        ),
        encoding="utf-8",
    )
    dialog = _zh_dialog(guide_docs)
    app = _DialogHost()

    async with app.run_test(size=(120, 40)) as pilot:
        await app.push_screen(dialog)
        await _wait_for_document(dialog, pilot, "MCP 配置说明", "default document to finish loading")

        assert dialog._active_topic is not None and dialog._active_topic.id == "mcp"
        await wait_for(
            lambda: _cursor_on_active_topic(dialog),
            pilot=pilot,
            description="tree cursor on the default topic",
        )


@pytest.mark.asyncio
async def test_space_cycles_language_and_missing_language_shows_no_content(guide_docs: Path) -> None:
    dialog = _zh_dialog(guide_docs)
    app = _DialogHost()

    async with app.run_test(size=(120, 40)) as pilot:
        await app.push_screen(dialog)
        await _wait_for_default_document(dialog, pilot)

        # zh-Hans -> en: no en files exist -> "no content" empty state; the
        # tree labels must stay in the UI language (zh-Hans).
        await pilot.press("space")
        await wait_for(
            lambda: dialog.query_one("#gd-empty").display,
            pilot=pilot,
            description="empty state after switching to a missing language",
        )
        assert dialog.query_one("#gd-empty", HatchedEmptyState).label == "No content"
        assert dialog.query_one("#gd-doc").display is False
        assert dialog._manual_language is True
        tree = dialog.query_one("#gd-tree", Tree)
        assert tree.root.children[0].label.plain == _DEFAULT_TITLE
        assert tree.root.children[2].label.plain == "configuration"

        # en -> zh-Hans: document returns
        await pilot.press("space")
        await _wait_for_default_document(dialog, pilot)
        assert dialog.query_one("#gd-empty").display is False


@pytest.mark.asyncio
async def test_space_cycle_preserves_tree_selection(guide_docs: Path) -> None:
    """Select a topic with the keyboard and keep it across a language change."""
    dialog = _zh_dialog(guide_docs)
    app = _DialogHost()

    async with app.run_test(size=(120, 40)) as pilot:
        await app.push_screen(dialog)
        await _wait_for_default_document(dialog, pilot)
        await wait_for(
            lambda: _cursor_on_active_topic(dialog),
            pilot=pilot,
            description="tree cursor on the default topic",
        )

        await pilot.press("down")
        await pilot.press("enter")
        await _wait_for_document(dialog, pilot, "开始使用", "second topic document to render")
        assert dialog._active_topic is not None and dialog._active_topic.id == "getting_started"

        # Cycle away (en missing -> empty) and back; the selection must
        # survive (the tree is not rebuilt on Space) and the same topic
        # reloads.
        await pilot.press("space")
        await wait_for(
            lambda: dialog.query_one("#gd-empty").display,
            pilot=pilot,
            description="empty state after switching to a missing language",
        )
        await pilot.press("space")
        await _wait_for_document(dialog, pilot, "开始使用", "document to return after cycling back")

        assert dialog._active_topic is not None and dialog._active_topic.id == "getting_started"
        await wait_for(
            lambda: _cursor_on_active_topic(dialog),
            pilot=pilot,
            description="tree selection to be restored after cycling",
        )


@pytest.mark.asyncio
async def test_manual_language_never_persists_and_stops_following_global(guide_docs: Path) -> None:
    """Space only changes this dialog: the controller sees nothing but the
    surface registration (the stub has no locale setter to call)."""
    controller = _StubLocaleController("zh-Hans")
    dialog = GuideDialog(locale_controller=controller, docs_root=guide_docs)
    app = _DialogHost()

    async with app.run_test(size=(120, 40)) as pilot:
        await app.push_screen(dialog)
        await _wait_for_default_document(dialog, pilot)
        await pilot.press("space")
        await wait_for(
            lambda: dialog.query_one("#gd-empty").display,
            pilot=pilot,
            description="document language to switch to en",
        )
        assert dialog._current_language() == "en"
        assert controller.effective_locale == "zh-Hans"
        # Dismiss so on_unmount runs; then the whole register/unregister pair
        # must be visible and nothing else touched the controller.
        await pilot.press("escape")
        await wait_for(
            lambda: controller.unregistered == [dialog],
            pilot=pilot,
            description="dialog to unregister on dismiss",
        )
        assert controller.registered == [dialog]
        assert dialog._manual_language is True


@pytest.mark.asyncio
@pytest.mark.parametrize("controller", [None, _StubLocaleController("fr")], ids=["no-controller", "undocumented"])
async def test_document_language_falls_back_to_english(
    guide_docs: Path, controller: _StubLocaleController | None
) -> None:
    """An app locale the docs don't have reads English, not the first listed locale."""
    dialog = GuideDialog(locale_controller=controller, docs_root=guide_docs)
    app = _DialogHost()

    async with app.run_test(size=(120, 40)) as pilot:
        await app.push_screen(dialog)
        await wait_for(
            lambda: dialog.query_one("#gd-empty").display,
            pilot=pilot,
            description="missing en document to show the empty state",
        )
        assert dialog._current_language() == "en"


@pytest.mark.asyncio
async def test_docs_root_missing_shows_unavailable_state(tmp_path: Path) -> None:
    dialog = GuideDialog(docs_root=tmp_path)
    app = _DialogHost()

    async with app.run_test(size=(120, 40)) as pilot:
        await app.push_screen(dialog)
        empty = dialog.query_one("#gd-empty", HatchedEmptyState)
        await wait_for(lambda: empty.display, pilot=pilot, description="unavailable state")

        assert empty.label == "Guide documents not found"
        assert dialog.query_one("#gd-tree").display is False
        assert dialog.query_one("#gd-doc").display is False


@pytest.mark.asyncio
async def test_malformed_index_shows_index_error_state(tmp_path: Path) -> None:
    (tmp_path / "index.yaml").write_text(": : :\n", encoding="utf-8")
    dialog = GuideDialog(docs_root=tmp_path)
    app = _DialogHost()

    async with app.run_test(size=(120, 40)) as pilot:
        await app.push_screen(dialog)
        empty = dialog.query_one("#gd-empty", HatchedEmptyState)
        await wait_for(lambda: empty.display, pilot=pilot, description="index error state")

        assert empty.label == "Guide index could not be loaded"


@pytest.mark.asyncio
async def test_unreadable_document_shows_no_content(guide_docs: Path) -> None:
    """A document that is not UTF-8 is unavailable, not a crash."""
    (guide_docs / "zh-Hans" / "start" / "what-is-chrys.md").write_bytes(b"# \xff\xfe broken\n")
    dialog = _zh_dialog(guide_docs)
    app = _DialogHost()

    async with app.run_test(size=(120, 40)) as pilot:
        await app.push_screen(dialog)
        empty = dialog.query_one("#gd-empty", HatchedEmptyState)
        await wait_for(lambda: empty.display, pilot=pilot, description="empty state for the unreadable document")

        assert empty.label == "No content"
        tree = dialog.query_one("#gd-tree", Tree)
        assert tree.root.children[0].label.plain == "what-is-chrys"


@pytest.mark.asyncio
async def test_ui_language_en_degrades_tree_labels_and_space_keeps_them(guide_docs: Path) -> None:
    """With the global locale set to en (no en docs), tree labels degrade to
    file names; cycling the document language must not touch the tree."""
    controller = _StubLocaleController(effective_locale="en")
    dialog = GuideDialog(locale_controller=controller, docs_root=guide_docs)
    app = _DialogHost()

    async with app.run_test(size=(120, 40)) as pilot:
        await app.push_screen(dialog)
        # en has no files at all: the document shows the empty state.
        await wait_for(
            lambda: dialog.query_one("#gd-empty").display,
            pilot=pilot,
            description="default document to resolve to the empty state",
        )

        tree = dialog.query_one("#gd-tree", Tree)
        assert tree.root.children[0].label.plain == "what-is-chrys"
        assert tree.root.children[1].label.plain == "getting-started"
        assert tree.root.children[2].label.plain == "configuration"
        assert tree.root.children[2].children[0].label.plain == "mcp"

        # Space cycles to zh-Hans: the document returns, the tree labels
        # stay in the UI language (en).
        await pilot.press("space")
        await _wait_for_default_document(dialog, pilot)
        assert tree.root.children[0].label.plain == "what-is-chrys"
        assert tree.root.children[2].label.plain == "configuration"


@pytest.mark.asyncio
async def test_global_locale_switch_relabels_tree_but_keeps_manual_document_language(guide_docs: Path) -> None:
    controller = _StubLocaleController(effective_locale="zh-Hans")
    dialog = GuideDialog(locale_controller=controller, docs_root=guide_docs)
    app = _DialogHost()

    async with app.run_test(size=(120, 40)) as pilot:
        await app.push_screen(dialog)
        await _wait_for_default_document(dialog, pilot)
        tree = dialog.query_one("#gd-tree", Tree)
        assert tree.root.children[0].label.plain == _DEFAULT_TITLE
        assert dialog._ui_language == "zh-Hans"

        # Global locale switch: tree labels re-resolve and the document
        # (not yet manually cycled) follows the global locale too.
        controller.effective_locale = "en"
        dialog.refresh_localization()
        await wait_for(
            lambda: dialog.query_one("#gd-empty").display,
            pilot=pilot,
            description="document to follow the global locale to en",
        )
        assert dialog._ui_language == "en"
        assert dialog._current_language() == "en"
        assert tree.root.children[0].label.plain == "what-is-chrys"

        # Manual Space pick pins the document language; a later global
        # switch still relabels the tree but leaves the document alone.
        await pilot.press("space")
        await _wait_for_default_document(dialog, pilot)

        # Global back to zh-Hans: the UI language relabels the tree to
        # Chinese; the manual document language is already zh-Hans.  A
        # manual pick starts no reload, so the rebuild is synchronous.
        controller.effective_locale = "zh-Hans"
        dialog.refresh_localization()
        assert dialog._pending_load is None
        assert dialog._ui_language == "zh-Hans"
        assert dialog._current_language() == "zh-Hans"
        assert tree.root.children[0].label.plain == _DEFAULT_TITLE

        # Global to en: the UI follows (tree labels degrade), but the manual
        # document language stays pinned to zh-Hans — a regression that
        # reset _lang_index here would flip the document to en (empty).
        controller.effective_locale = "en"
        dialog.refresh_localization()
        assert dialog._pending_load is None
        assert dialog._ui_language == "en"
        assert dialog._current_language() == "zh-Hans"
        assert _DEFAULT_TITLE in _markdown_text(dialog)
        assert tree.root.children[0].label.plain == "what-is-chrys"


@pytest.mark.asyncio
async def test_refresh_localization_cancels_inflight_load(guide_docs: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A second global-locale refresh must cancel the first background load
    instead of letting it clobber the freshly localized state."""
    controller = _StubLocaleController(effective_locale="zh-Hans")
    dialog = GuideDialog(locale_controller=controller, docs_root=guide_docs)
    app = _DialogHost()
    started = asyncio.Event()
    release = asyncio.Event()
    real_load = dialog._load_document

    async def slow_load() -> None:
        started.set()
        await release.wait()
        await real_load()

    async with app.run_test(size=(120, 40)) as pilot:
        await app.push_screen(dialog)
        await _wait_for_default_document(dialog, pilot)
        # Patch only after the initial mount load so the slow wrapper guards
        # just the refresh path.
        monkeypatch.setattr(dialog, "_load_document", slow_load)

        controller.effective_locale = "en"
        dialog.refresh_localization()
        first = dialog._pending_load
        assert first is not None
        await wait_for(
            lambda: started.is_set() or first.done(),
            pilot=pilot,
            description="refresh load to start",
        )
        assert not first.done(), first

        dialog.refresh_localization()
        second = dialog._pending_load
        await wait_for(first.done, pilot=pilot, description="first refresh load to finish cancelling")
        assert first.cancelled()
        assert second is not None and second is not first
        release.set()
        await wait_for(second.done, pilot=pilot, description="second refresh load to finish")
        second.result()
        assert dialog.query_one("#gd-empty").display is True


@pytest.mark.asyncio
async def test_superseded_load_does_not_reclaim_the_panes(guide_docs: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A slow load that finishes after a newer one leaves the newer state on screen.

    Selecting a topic loads it inside the screen's message pump while Space
    runs on the App's, so the two loads overlap.  Here the older load renders
    last; the language it read is no longer current, so the empty state the
    newer load chose must stay.
    """
    dialog = _zh_dialog(guide_docs)
    app = _DialogHost()
    started = asyncio.Event()
    release = asyncio.Event()
    completed: list[None] = []

    async with app.run_test(size=(120, 40)) as pilot:
        await app.push_screen(dialog)
        await _wait_for_default_document(dialog, pilot)
        markdown = dialog.query_one("#gd-doc", VirtualizedMarkdown)
        real_update = markdown.update
        real_load = dialog._load_document

        async def slow_update(text: str) -> None:
            if "开始使用" in text:
                started.set()
                await release.wait()
            await real_update(text)

        async def counted_load() -> None:
            await real_load()
            completed.append(None)

        monkeypatch.setattr(markdown, "update", slow_update)
        monkeypatch.setattr(dialog, "_load_document", counted_load)
        tree = dialog.query_one("#gd-tree", Tree)
        tree.select_node(tree.root.children[1])
        # The screen's pump is parked in the selection handler until
        # ``release``, so this wait cannot use Pilot's settle barrier.
        await wait_for(started.is_set, description="selected topic to start rendering")

        await dialog.action_cycle_language()
        assert completed == [None]
        assert dialog.query_one("#gd-empty").display is True

        release.set()
        await wait_for(lambda: len(completed) == 2, pilot=pilot, description="superseded load to finish")
        assert dialog.query_one("#gd-empty").display is True
        assert dialog.query_one("#gd-doc").display is False


@pytest.mark.asyncio
async def test_click_relative_link_navigates_and_highlights_tree(guide_docs: Path) -> None:
    """A relative markdown link loads the target document and moves the tree
    cursor onto the matching node (highlight + scroll)."""
    dialog = _zh_dialog(guide_docs)
    app = _DialogHost()

    async with app.run_test(size=(120, 40)) as pilot:
        await app.push_screen(dialog)
        await _wait_for_default_document(dialog, pilot)
        markdown = dialog.query_one("#gd-doc", VirtualizedMarkdown)
        markdown.post_message(VirtualizedMarkdown.LinkClicked(markdown, "../guides/configuration/mcp.md"))
        await _wait_for_document(dialog, pilot, "MCP 配置说明", "linked document to render")

        assert dialog._active_topic is not None and dialog._active_topic.id == "mcp"
        assert _cursor_on_active_topic(dialog)


@pytest.mark.asyncio
async def test_link_click_before_the_rebuilt_tree_renders_highlights_the_target(guide_docs: Path) -> None:
    """A link outside any collapsed branch moves the cursor at once — the
    hidden root is never expanded but hides nothing, so it must not defer the
    move to the next refresh — and lands it on the target even though a
    rebuilt tree has no row numbers until it renders or idles."""
    dialog = _zh_dialog(guide_docs)
    app = _DialogHost()

    async with app.run_test(size=(120, 40)) as pilot:
        await app.push_screen(dialog)
        await _wait_for_default_document(dialog, pilot)
        markdown = dialog.query_one("#gd-doc", VirtualizedMarkdown)

        # A global locale switch rebuilds the tree; nothing yields before the
        # click, so no frame can have numbered the new rows.
        dialog.refresh_localization()
        await dialog._on_doc_link_clicked(VirtualizedMarkdown.LinkClicked(markdown, "../guides/configuration/mcp.md"))

        assert dialog._active_topic is not None and dialog._active_topic.id == "mcp"
        assert _cursor_on_active_topic(dialog)


@pytest.mark.asyncio
async def test_click_link_into_collapsed_branch_expands_and_highlights_it(guide_docs: Path) -> None:
    dialog = _zh_dialog(guide_docs)
    app = _DialogHost()

    async with app.run_test(size=(120, 40)) as pilot:
        await app.push_screen(dialog)
        await _wait_for_default_document(dialog, pilot)
        tree = dialog.query_one("#gd-tree", Tree)
        configuration = tree.root.children[2]
        configuration.collapse()
        # The hidden leaf keeps its old row number, which now lies past the
        # last row: moving the cursor there without expanding highlights nothing.
        assert tree.last_line == 2 and configuration.children[0]._line == 3

        markdown = dialog.query_one("#gd-doc", VirtualizedMarkdown)
        markdown.post_message(VirtualizedMarkdown.LinkClicked(markdown, "../guides/configuration/mcp.md"))
        await _wait_for_document(dialog, pilot, "MCP 配置说明", "linked document to render")

        assert configuration.is_expanded
        await wait_for(
            lambda: _cursor_on_active_topic(dialog),
            pilot=pilot,
            description="tree cursor on the linked topic inside the reopened branch",
        )


async def _spy_anchor_navigation(
    dialog: GuideDialog, monkeypatch: pytest.MonkeyPatch
) -> tuple[list[tuple[str, bool]], list[int]]:
    """Record every goto_anchor (fragment, found) pair and every scroll target."""
    markdown = dialog.query_one("#gd-doc", VirtualizedMarkdown)
    calls: list[tuple[str, bool]] = []
    scroll_calls: list[int] = []
    real_goto = markdown.goto_anchor
    real_scroll = markdown.scroll_to

    def spy(anchor: str) -> bool:
        result = real_goto(anchor)
        calls.append((anchor, result))
        return result

    def scroll_spy(*args: object, **kwargs: object) -> None:
        scroll_calls.append(kwargs.get("y"))  # type: ignore[arg-type]
        real_scroll(*args, **kwargs)

    monkeypatch.setattr(markdown, "goto_anchor", spy)
    monkeypatch.setattr(markdown, "scroll_to", scroll_spy)
    return calls, scroll_calls


@pytest.mark.asyncio
async def test_click_anchor_link_renders_then_goto_anchor(guide_docs: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A link with a #fragment loads the target then scrolls to the anchor.

    The fragment is GitHub's heading id, handed to goto_anchor unchanged;
    the spy records the (anchor, success) pair so a silent no-scroll cannot
    pass.
    """
    dialog = _zh_dialog(guide_docs)
    app = _DialogHost()

    async with app.run_test(size=(120, 40)) as pilot:
        await app.push_screen(dialog)
        await _wait_for_default_document(dialog, pilot)
        calls, scroll_calls = await _spy_anchor_navigation(dialog, monkeypatch)
        markdown = dialog.query_one("#gd-doc", VirtualizedMarkdown)
        markdown.post_message(
            VirtualizedMarkdown.LinkClicked(markdown, "../guides/configuration/mcp.md#连接-mcp-服务器")
        )
        await wait_for(
            lambda: calls == [("连接-mcp-服务器", True)],
            pilot=pilot,
            description="anchor scroll to succeed after the target renders",
        )
        assert "MCP 配置说明" in _markdown_text(dialog)
        assert dialog._pending_anchor is None
        # The anchor scroll must be the final scroll: a second load would
        # append a trailing scroll_to(0) that clobbers it.  Content shorter
        # than the viewport clamps the offset to 0, so assert the call
        # sequence (exactly one load: [top, anchor]) not the offset.
        assert len(scroll_calls) == 2 and scroll_calls[0] == 0


@pytest.mark.asyncio
async def test_click_pure_cjk_anchor_scrolls_to_title(guide_docs: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A pure-CJK fragment is GitHub's id for a CJK heading (Textual's own
    slug would be empty) and scrolls that heading to the top."""
    dialog = _zh_dialog(guide_docs)
    app = _DialogHost()

    async with app.run_test(size=(120, 40)) as pilot:
        await app.push_screen(dialog)
        await _wait_for_default_document(dialog, pilot)
        calls, scroll_calls = await _spy_anchor_navigation(dialog, monkeypatch)
        markdown = dialog.query_one("#gd-doc", VirtualizedMarkdown)
        markdown.post_message(VirtualizedMarkdown.LinkClicked(markdown, "../guides/configuration/mcp.md#会话"))
        await wait_for(
            lambda: calls == [("会话", True)],
            pilot=pilot,
            description="CJK anchor scroll to succeed after the target renders",
        )
        assert "MCP 配置说明" in _markdown_text(dialog)
        assert dialog._pending_anchor is None
        # Exactly one load's scroll sequence, ending on the anchor.
        assert len(scroll_calls) == 2 and scroll_calls[0] == 0 and scroll_calls[-1] > 0
        assert _cursor_on_active_topic(dialog)


@pytest.mark.asyncio
async def test_link_click_cancels_inflight_default_load(guide_docs: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A link click during the on-mount default load must cancel the
    background task so its continuation cannot clobber the freshly
    navigated state."""
    dialog = _zh_dialog(guide_docs)
    app = _DialogHost()
    started = asyncio.Event()
    release = asyncio.Event()
    real_load = dialog._load_document
    load_calls = 0

    async def slow_first_load() -> None:
        # Only the on-mount default load blocks. The link handler's own load
        # runs inside the screen's message pump, and a pump parked on
        # ``release`` would keep every Pilot wait from ever settling.
        nonlocal load_calls
        load_calls += 1
        if load_calls == 1:
            started.set()
            await release.wait()
        await real_load()

    # Patch before mounting so the default-load task itself is the slow one.
    monkeypatch.setattr(dialog, "_load_document", slow_first_load)

    async with app.run_test(size=(120, 40)) as pilot:
        await app.push_screen(dialog)
        first = dialog._pending_load
        assert first is not None
        await wait_for(
            lambda: started.is_set() or first.done(),
            pilot=pilot,
            description="default load to start",
        )
        assert not first.done(), first
        # Until the first load decides, neither the document nor the empty
        # state takes the pane: showing both split it in half.
        assert dialog.query_one("#gd-doc").display is False
        assert dialog.query_one("#gd-empty").display is False

        markdown = dialog.query_one("#gd-doc", VirtualizedMarkdown)
        markdown.post_message(VirtualizedMarkdown.LinkClicked(markdown, "../guides/configuration/mcp.md"))
        await _wait_for_document(
            dialog, pilot, "MCP 配置说明", "linked document to render while the default load is still blocked"
        )
        await wait_for(first.done, pilot=pilot, description="blocked default load to finish cancelling")
        assert first.cancelled()
        # The cancelled default load can no longer continue, so releasing it
        # leaves the linked document in place.
        release.set()
        assert _cursor_on_active_topic(dialog)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "href",
    [
        "../acp/chrys-acp.md",  # a markdown file outside the topic tree
        "#为智能体配置子智能体",  # page-internal anchor, already handled by the widget
    ],
)
async def test_click_unresolvable_link_leaves_state_unchanged(guide_docs: Path, href: str) -> None:
    dialog = _zh_dialog(guide_docs)
    app = _DialogHost()

    async with app.run_test(size=(120, 40)) as pilot:
        await app.push_screen(dialog)
        await _wait_for_default_document(dialog, pilot)
        await wait_for(
            lambda: _cursor_on_active_topic(dialog),
            pilot=pilot,
            description="tree cursor on the default topic",
        )
        markdown = dialog.query_one("#gd-doc", VirtualizedMarkdown)
        markdown.post_message(VirtualizedMarkdown.LinkClicked(markdown, href))

        assert not await wait_until(
            lambda: (
                _DEFAULT_TITLE not in _markdown_text(dialog)
                or dialog._active_topic is None
                or dialog._active_topic.id != "intro"
                or not _cursor_on_active_topic(dialog)
            ),
            timeout=0.5,
            pilot=pilot,
        )
        # Positive control: the same dispatch path does navigate for a link
        # into the topic tree.
        markdown.post_message(VirtualizedMarkdown.LinkClicked(markdown, "../guides/configuration/mcp.md"))
        await _wait_for_document(dialog, pilot, "MCP 配置说明", "resolvable link to navigate")


@pytest.mark.asyncio
async def test_click_external_link_opens_it_and_keeps_the_document(
    guide_docs: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    dialog = _zh_dialog(guide_docs)
    app = _DialogHost()
    opened: list[str] = []

    def open_url(url: str, *, new_tab: bool = True) -> None:
        opened.append(url)

    monkeypatch.setattr(app, "open_url", open_url)

    async with app.run_test(size=(120, 40)) as pilot:
        await app.push_screen(dialog)
        await _wait_for_default_document(dialog, pilot)
        markdown = dialog.query_one("#gd-doc", VirtualizedMarkdown)
        markdown.post_message(VirtualizedMarkdown.LinkClicked(markdown, "https://example.com/guide.md"))
        await wait_for(
            lambda: opened == ["https://example.com/guide.md"],
            pilot=pilot,
            description="external link to open in the browser",
        )

        assert _DEFAULT_TITLE in _markdown_text(dialog)
        assert dialog._active_topic is not None and dialog._active_topic.id == "intro"


@pytest.mark.asyncio
async def test_click_link_in_missing_language_shows_empty_and_clears_anchor(guide_docs: Path) -> None:
    """In a language without files the link still moves the tree highlight,
    the document shows the empty state, and a stale anchor is dropped."""
    controller = _StubLocaleController(effective_locale="en")
    dialog = GuideDialog(locale_controller=controller, docs_root=guide_docs)
    app = _DialogHost()

    async with app.run_test(size=(120, 40)) as pilot:
        await app.push_screen(dialog)
        await wait_for(
            lambda: dialog.query_one("#gd-empty").display,
            pilot=pilot,
            description="empty state for the missing en documents",
        )
        markdown = dialog.query_one("#gd-doc", VirtualizedMarkdown)
        markdown.post_message(VirtualizedMarkdown.LinkClicked(markdown, "getting-started.md#开始"))
        await wait_for(
            lambda: dialog._active_topic is not None and dialog._active_topic.id == "getting_started",
            pilot=pilot,
            description="link handler to move the active topic",
        )

        assert dialog._pending_anchor is None
        assert dialog.query_one("#gd-empty").display is True
        await wait_for(
            lambda: _cursor_on_active_topic(dialog),
            pilot=pilot,
            description="tree cursor on the linked topic",
        )
