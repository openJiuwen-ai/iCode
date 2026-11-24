# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tests for ``DiffPeriodPane``'s checkable mode (used by ``RollbackModal``).

These cover the parts of the checkbox UX that can be exercised without
real mouse-coordinate targeting:

- Checkbox labels carry the click-toggle meta so :class:`CheckableTree`
  can intercept clicks on them.
- Dispatching :class:`CheckableTree.CheckToggled` — the message
  ``CheckableTree`` posts when it detects a checkbox click — flips the
  pane's internal selection state and re-renders the labels.
- Folder cascade via ``toggle_check_node`` respects tri-state rules.
- ``selected_paths()`` reflects the current state.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from textual.app import App, ComposeResult
from textual.containers import VerticalGroup
from textual.screen import ModalScreen
from textual.widgets import Static, Tab, TabbedContent, Tree

from chrys.app.tui.screens.diff.screen import (
    _CHECK_TOGGLE_META_KEY,
    CheckableTree,
    DiffFileEntry,
    DiffPeriodPane,
    DiffScreen,
    _build_period_entries,
    _checkbox_text,
    _text_eol_label,
)
from chrys.app.tui.util.diff_entries import DiffLoadResult
from chrys.app.tui.widgets import ChrysLoadingIndicator
from chrys.service.mutations.store import SnapshotStore
from chrys.service.mutations.tracker import MutationTracker
from chrys.service.mutations.types import MutationOp, MutationSource
from tests.support.waiting import wait_for, wait_until


def _entry(
    path: str,
    rel: str,
    op: MutationOp = MutationOp.MODIFY,
    *,
    before_text: str = "before",
    after_text: str = "after",
    encoding: str = "utf-8",
    bytes_changed: bool | None = None,
    before_hash: str | None = None,
    after_hash: str | None = None,
    source: str = "",
    old_path: str | None = None,
) -> DiffFileEntry:
    if bytes_changed is None:
        bytes_changed = before_text != after_text
    return DiffFileEntry(
        path=path,
        rel_path=rel,
        operation=op,
        old_path=old_path,
        before_text=before_text,
        after_text=after_text,
        is_binary=False,
        encoding=encoding,
        bytes_changed=bytes_changed,
        before_hash=before_hash,
        after_hash=after_hash,
        source=source,
    )


class _PaneApp(App):
    def __init__(self, entries: list[DiffFileEntry]) -> None:
        super().__init__()
        self._entries = entries

    def compose(self) -> ComposeResult:
        yield DiffPeriodPane(self._entries, checkable=True)


class _PlainPaneApp(App):
    def __init__(self, entries: list[DiffFileEntry]) -> None:
        super().__init__()
        self._entries = entries

    def compose(self) -> ComposeResult:
        yield DiffPeriodPane(self._entries)


class _DiffScreenApp(App):
    def on_mount(self) -> None:
        self.push_screen(
            DiffScreen(
                {1: [_entry("/repo/a.py", "a.py")]},
                cwd="/repo",
                session_id="session-1234567890",
            )
        )


class _PrefixedTurnTabsDiffScreenApp(App):
    def on_mount(self) -> None:
        self.push_screen(
            DiffScreen(
                {
                    1: [_entry("/repo/one.py", "one.py")],
                    100: [_entry("/repo/hundred.py", "hundred.py")],
                },
                cwd="/repo",
                session_id="session-1234567890",
            )
        )


class _AllOnlyDiffScreenApp(App):
    def on_mount(self) -> None:
        self.push_screen(
            DiffScreen(
                {},
                cwd="/repo",
                session_id="session-1234567890",
                all_entries=[_entry("/repo/Power.yaml", "Power.yaml")],
            )
        )


class _AllAndSingleTurnDiffScreenApp(App):
    def __init__(self, *, all_matches_turn: bool, hash_metadata_differs: bool = False) -> None:
        super().__init__()
        self._all_matches_turn = all_matches_turn
        self._hash_metadata_differs = hash_metadata_differs

    def on_mount(self) -> None:
        all_entry = _entry(
            "/repo/Power.yaml",
            "Power.yaml",
            before_hash="all-before" if self._hash_metadata_differs else None,
            after_hash="all-after" if self._hash_metadata_differs else None,
        )
        turn_entry = (
            _entry(
                "/repo/Power.yaml",
                "Power.yaml",
                before_hash="turn-before" if self._hash_metadata_differs else None,
                after_hash="turn-after" if self._hash_metadata_differs else None,
            )
            if self._all_matches_turn
            else _entry("/repo/live.py", "live.py")
        )
        self.push_screen(
            DiffScreen(
                {2: [turn_entry]},
                cwd="/repo",
                session_id="session-1234567890",
                all_entries=[all_entry],
            )
        )


async def _wait_for_diff_screen(
    pilot,
    predicate,
    *,
    message: str,
) -> None:
    """Wait for a deferred DiffScreen state without single-pause timing assumptions."""
    await wait_for(
        predicate,
        pilot=pilot,
        description=message,
    )


def test_checkbox_text_carries_toggle_meta() -> None:
    """The ``[x]`` glyph Text carries the meta key that ``CheckableTree``
    looks for on click — this is the whole contract that makes the
    checkbox mouse-clickable."""
    for state in ("on", "off", "mixed"):
        text = _checkbox_text(state)
        assert text.style.meta.get(_CHECK_TOGGLE_META_KEY) is True, (
            f"checkbox glyph for state={state!r} missing toggle meta"
        )


def test_leaf_label_marks_implicit_source() -> None:
    pane = DiffPeriodPane([])
    explicit = _entry("/repo/a.py", "a.py")
    implicit = _entry("/repo/b.py", "b.py", source=MutationSource.IMPLICIT.value)

    assert not pane._leaf_label(explicit).plain.endswith(" ?")
    assert pane._leaf_label(implicit).plain.endswith("b.py ?")


def test_text_eol_label() -> None:
    assert _text_eol_label("a\nb\n") == "LF"
    assert _text_eol_label("a\r\nb\r\n") == "CRLF"
    assert _text_eol_label("a\rb\r") == "CR"
    assert _text_eol_label("a\r\nb\n") == "Mixed"
    assert _text_eol_label("single line") == "No EOL"


@pytest.mark.asyncio
async def test_diff_screen_hides_approval_badge() -> None:
    async with _DiffScreenApp().run_test() as pilot:
        await pilot.pause()
        assert isinstance(pilot.app.screen, DiffScreen)
        assert list(pilot.app.screen.query("#approval-badge")) == []


@pytest.mark.asyncio
async def test_diff_screen_footer_puts_back_in_the_right_corner() -> None:
    """Esc Back takes the corner the main screen gives its quit key, and leaves the left row."""
    async with _DiffScreenApp().run_test() as pilot:
        await _wait_for_diff_screen(
            pilot,
            lambda: (
                isinstance(pilot.app.screen, DiffScreen)
                and {"toggle_view", "toggle_change_list"} <= {key.action for key in pilot.app.screen.query("FooterKey")}
            ),
            message="diff footer keys never rendered",
        )
        keys = list(pilot.app.screen.query("FooterKey"))

        assert [key.action for key in keys if key.has_class("-corner")] == ["go_back"]
        assert [key.action for key in keys if not key.has_class("-corner")] == [
            "toggle_view",
            "toggle_change_list",
        ]


@pytest.mark.asyncio
async def test_diff_screen_paints_centered_loading_state_before_mounting_content() -> None:
    load_started = asyncio.Event()
    release_load = asyncio.Event()
    entry = _entry("/repo/async.py", "async.py")

    async def load_data() -> DiffLoadResult:
        load_started.set()
        await release_load.wait()
        return DiffLoadResult(all_entries=[entry], per_period_entries={1: [entry]}, total_periods=1)

    class _AsyncDiffScreenApp(App):
        def on_mount(self) -> None:
            self.push_screen(
                DiffScreen(
                    {},
                    cwd="/repo",
                    session_id="session-1234567890",
                    load_data=load_data,
                )
            )

    async with _AsyncDiffScreenApp().run_test() as pilot:
        await asyncio.wait_for(load_started.wait(), timeout=20)
        await pilot.pause()

        screen = pilot.app.screen
        assert isinstance(screen, DiffScreen)
        assert screen.query_one("#diff-container")
        assert screen.query_one("#diff-loading", ChrysLoadingIndicator).display is True
        assert list(screen.query(TabbedContent)) == []
        assert list(screen.query(DiffPeriodPane)) == []
        assert screen.check_action("toggle_view", ()) is False
        assert screen.check_action("toggle_change_list", ()) is False
        assert screen.check_action("go_back", ()) is True
        visible_actions = {active.binding.action for active in screen.active_bindings.values()}
        assert "go_back" in visible_actions
        assert "toggle_view" not in visible_actions
        assert "toggle_change_list" not in visible_actions
        # Footer keys mount via an async recompose after the screen push;
        # under load they can land later than one pause.
        await _wait_for_diff_screen(
            pilot,
            lambda: bool(screen.query("FooterKey")),
            message="footer keys never rendered for the loading screen",
        )
        footer_actions = {key.action for key in screen.query("FooterKey")}
        assert "go_back" in footer_actions
        assert "toggle_view" not in footer_actions
        assert "toggle_change_list" not in footer_actions

        await pilot.press("space", "ctrl+g")
        assert screen._change_list_visible is True

        release_load.set()
        await _wait_for_diff_screen(
            pilot,
            lambda: screen._content_ready,
            message="deferred diff content never became ready",
        )

        tabs = screen.query_one(TabbedContent)
        assert tabs.active == "turn-1"
        assert list(screen.query(ChrysLoadingIndicator)) == []
        assert screen.query_one(DiffPeriodPane)._active_path == "/repo/async.py"
        assert screen.check_action("toggle_view", ()) is True
        assert screen.check_action("toggle_change_list", ()) is True
        visible_actions = {active.binding.action for active in screen.active_bindings.values()}
        assert "toggle_view" in visible_actions
        assert "toggle_change_list" in visible_actions
        await _wait_for_diff_screen(
            pilot,
            lambda: {"toggle_view", "toggle_change_list"} <= {key.action for key in screen.query("FooterKey")},
            message="deferred diff footer actions never became visible",
        )
        footer_actions = {key.action for key in screen.query("FooterKey")}
        assert "toggle_view" in footer_actions
        assert "toggle_change_list" in footer_actions


@pytest.mark.asyncio
async def test_diff_screen_escape_cancels_loading_before_pop() -> None:
    load_started = asyncio.Event()
    load_cancelled = asyncio.Event()
    never_finish = asyncio.Event()

    async def load_data() -> DiffLoadResult:
        load_started.set()
        try:
            await never_finish.wait()
        except asyncio.CancelledError:
            load_cancelled.set()
            raise
        raise AssertionError("unreachable")

    class _EscapeDuringLoadingApp(App):
        def on_mount(self) -> None:
            self.push_screen(DiffScreen({}, cwd="/repo", load_data=load_data))

    async with _EscapeDuringLoadingApp().run_test() as pilot:
        await asyncio.wait_for(load_started.wait(), timeout=20)
        diff_screen = pilot.app.screen
        assert isinstance(diff_screen, DiffScreen)

        await pilot.press("escape")
        await asyncio.wait_for(load_cancelled.wait(), timeout=20)
        await pilot.pause()

        assert not isinstance(pilot.app.screen, DiffScreen)
        assert diff_screen.is_attached is False
        assert diff_screen._content_ready is False
        assert list(diff_screen.query(TabbedContent)) == []
        assert list(diff_screen.query("#diff-load-error")) == []


@pytest.mark.asyncio
async def test_empty_diff_waits_to_close_until_transient_modal_is_gone() -> None:
    load_started = asyncio.Event()
    release_load = asyncio.Event()

    async def load_data() -> DiffLoadResult:
        load_started.set()
        await release_load.wait()
        return DiffLoadResult.empty()

    class _TransientModal(ModalScreen[None]):
        def compose(self) -> ComposeResult:
            yield Static("Approval still pending", id="transient-modal-body")

    class _EmptyDiffApp(App):
        def __init__(self) -> None:
            super().__init__()
            self.diff_screen = DiffScreen({}, cwd="/repo", load_data=load_data)

        def on_mount(self) -> None:
            self.push_screen(self.diff_screen)

    async with _EmptyDiffApp().run_test() as pilot:
        await asyncio.wait_for(load_started.wait(), timeout=20)
        app = pilot.app
        transient = _TransientModal()
        await app.push_screen(transient)

        release_load.set()
        await _wait_for_diff_screen(
            pilot,
            lambda: app.diff_screen._close_when_current,
            message="empty diff did not defer its close behind the transient modal",
        )

        assert app.screen is transient
        assert transient.is_attached is True

        transient.dismiss(None)
        await _wait_for_diff_screen(
            pilot,
            lambda: app.screen is not transient and app.screen is not app.diff_screen,
            message="empty diff did not close after the transient modal",
        )
        assert app.diff_screen.is_attached is False


@pytest.mark.asyncio
async def test_diff_load_error_survives_removed_loading_shell(monkeypatch: pytest.MonkeyPatch) -> None:
    load_started = asyncio.Event()
    release_load = asyncio.Event()
    entry = _entry("/repo/error.py", "error.py")

    async def load_data() -> DiffLoadResult:
        load_started.set()
        await release_load.wait()
        return DiffLoadResult(all_entries=[entry], per_period_entries={1: [entry]}, total_periods=1)

    class _ErrorDiffApp(App):
        def __init__(self) -> None:
            super().__init__()
            self.diff_screen = DiffScreen({}, cwd="/repo", load_data=load_data)

        def on_mount(self) -> None:
            self.push_screen(self.diff_screen)

    async with _ErrorDiffApp().run_test() as pilot:
        await asyncio.wait_for(load_started.wait(), timeout=20)
        screen = pilot.app.diff_screen

        def fail_refresh_bindings() -> None:
            raise RuntimeError("footer refresh failed")

        monkeypatch.setattr(screen, "refresh_bindings", fail_refresh_bindings)
        release_load.set()
        await _wait_for_diff_screen(
            pilot,
            lambda: bool(list(screen.query("#diff-load-error"))),
            message="diff load failure did not render its fallback error",
        )

        assert pilot.app.screen is screen
        assert screen._content_ready is False
        assert list(screen.query("#diff-loading-state")) == []
        assert (
            screen.query_one("#diff-load-error", Static).render().plain.startswith("Error: unable to load diff viewer")
        )


@pytest.mark.asyncio
async def test_diff_screen_can_render_all_only_entries() -> None:
    async with _AllOnlyDiffScreenApp().run_test() as pilot:
        await pilot.pause()
        tabs = pilot.app.screen.query_one(TabbedContent)
        assert tabs.active == "turn-all"
        assert len(list(pilot.app.screen.query("#turn-all"))) == 1
        assert list(pilot.app.screen.query("#turn-1")) == []


@pytest.mark.asyncio
async def test_diff_screen_hides_all_when_single_turn_matches_all_entries() -> None:
    async with _AllAndSingleTurnDiffScreenApp(all_matches_turn=True).run_test() as pilot:
        await pilot.pause()
        tabs = pilot.app.screen.query_one(TabbedContent)
        assert tabs.active == "turn-2"
        assert list(pilot.app.screen.query("#turn-all")) == []
        assert len(list(pilot.app.screen.query("#turn-2"))) == 1


@pytest.mark.asyncio
async def test_diff_screen_hides_all_when_single_turn_only_differs_by_hash_metadata() -> None:
    async with _AllAndSingleTurnDiffScreenApp(all_matches_turn=True, hash_metadata_differs=True).run_test() as pilot:
        await pilot.pause()
        tabs = pilot.app.screen.query_one(TabbedContent)
        assert tabs.active == "turn-2"
        assert list(pilot.app.screen.query("#turn-all")) == []
        assert len(list(pilot.app.screen.query("#turn-2"))) == 1


@pytest.mark.asyncio
async def test_diff_screen_keeps_all_when_single_turn_differs_from_all_entries() -> None:
    async with _AllAndSingleTurnDiffScreenApp(all_matches_turn=False).run_test() as pilot:
        await pilot.pause()
        tabs = pilot.app.screen.query_one(TabbedContent)
        assert tabs.active == "turn-2"
        assert len(list(pilot.app.screen.query("#turn-all"))) == 1
        assert len(list(pilot.app.screen.query("#turn-2"))) == 1
        assert [tab.label.plain for tab in tabs.query(Tab)] == ["T2", "All"]


@pytest.mark.asyncio
async def test_diff_screen_uses_prefixed_turn_tabs_with_expected_widths() -> None:
    async with _PrefixedTurnTabsDiffScreenApp().run_test() as pilot:
        await pilot.pause()
        screen = pilot.app.screen
        first_tab = screen.query_one("#--content-tab-turn-1", Tab)
        hundredth_tab = screen.query_one("#--content-tab-turn-100", Tab)

        assert first_tab.label.plain == "T1"
        assert hundredth_tab.label.plain == "T100"
        await wait_for(
            lambda: first_tab.region.width > 0 and hundredth_tab.region.width > 0,
            pilot=pilot,
            description="turn tabs to be laid out",
        )
        assert first_tab.region.width == 4
        assert hundredth_tab.region.width == 6


@pytest.mark.asyncio
async def test_diff_screen_ctrl_g_toggles_change_list_across_tabs() -> None:
    async with _AllAndSingleTurnDiffScreenApp(all_matches_turn=False).run_test() as pilot:
        screen = pilot.app.screen
        assert isinstance(screen, DiffScreen)
        await wait_for(
            lambda: screen._content_ready,
            pilot=pilot,
            description="diff screen content to become ready",
        )
        change_lists = list(screen.query(".diff-changelist"))
        assert len(change_lists) == 2
        assert all(change_list.display for change_list in change_lists)

        await pilot.press("ctrl+g")
        await pilot.pause()
        assert all(not change_list.display for change_list in change_lists)

        tabs = screen.query_one(TabbedContent)
        tabs.active = "turn-all"
        await pilot.pause()
        assert all(not change_list.display for change_list in change_lists)

        await pilot.press("ctrl+g")
        await pilot.pause()
        assert all(change_list.display for change_list in change_lists)


@pytest.mark.asyncio
async def test_diff_screen_initializes_only_active_turn_until_tab_switch(monkeypatch: pytest.MonkeyPatch) -> None:
    from chrys.app.tui.widgets.diff_view import DiffView

    prepared_paths: list[str] = []
    original_prepare = DiffView.prepare

    async def counted_prepare(self: DiffView) -> None:
        prepared_paths.append(self.path_before)
        await original_prepare(self)

    monkeypatch.setattr(DiffView, "prepare", counted_prepare)

    class _ManyTurnsDiffScreenApp(App):
        def on_mount(self) -> None:
            turns = {turn: [_entry(f"/repo/turn_{turn}.py", f"turn_{turn}.py")] for turn in range(1, 6)}
            self.push_screen(
                DiffScreen(
                    turns,
                    cwd="/repo",
                    session_id="session-1234567890",
                )
            )

    async with _ManyTurnsDiffScreenApp().run_test() as pilot:
        await pilot.pause()

        screen = pilot.app.screen
        assert isinstance(screen, DiffScreen)
        tabs = screen.query_one(TabbedContent)
        assert tabs.active == "turn-5"
        assert await wait_until(lambda: "/repo/turn_5.py" in prepared_paths, pilot=pilot, timeout=10.0)
        # The lazy-tab contract is WHICH turns prepared, not how many times:
        # concurrent _show_file calls for the same path can double-prepare on
        # contended workers, and exact list equality flakes on that.
        assert set(prepared_paths) == {"/repo/turn_5.py"}

        # TabActivated is dropped while _content_loading is True (the reveal
        # sequence initializes the active pane itself), and turn_5's prepare
        # only STARTS before the reveal completes — switching during that
        # window orphans the new tab. A real user can't switch until the
        # reveal either (content is hidden and actions are gated), so wait
        # for it before switching.
        assert await wait_until(lambda: screen._content_ready, pilot=pilot, timeout=10.0)
        # The switch-triggered prepare chain is asynchronous; poll for it
        # instead of relying on a single pause on contended CI workers.
        tabs.active = "turn-1"
        assert await wait_until(lambda: "/repo/turn_1.py" in prepared_paths, pilot=pilot, timeout=10.0)
        assert set(prepared_paths) == {"/repo/turn_5.py", "/repo/turn_1.py"}


@pytest.mark.asyncio
async def test_initial_diff_selects_rendered_file_leaf_in_workspace_tree() -> None:
    entries = [
        _entry(
            "/w/src/chrys/app/tui/screens/dialogs/tool_view.py",
            "src/chrys/app/tui/screens/dialogs/tool_view.py",
        )
    ]

    async with _PlainPaneApp(entries).run_test() as pilot:
        await pilot.pause()
        pane = pilot.app.query_one(DiffPeriodPane)
        tree = pilot.app.query_one(".workspace-tree", CheckableTree)

        assert pane._active_path == "/w/src/chrys/app/tui/screens/dialogs/tool_view.py"
        cursor_node = tree.cursor_node
        assert cursor_node is not None
        assert isinstance(cursor_node.data, DiffFileEntry)
        assert cursor_node.data.path == pane._active_path


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("before_text", "after_text", "expected_split"),
    [
        ("alpha\ngamma\n", "alpha\nbeta\ngamma\n", False),
        ("alpha\nbeta\ngamma\n", "alpha\ngamma\n", False),
        ("alpha\nold\ngamma\n", "alpha\nnew\ngamma\n", True),
    ],
)
async def test_default_diff_view_mode_uses_actual_add_delete_counts(
    before_text: str,
    after_text: str,
    expected_split: bool,
) -> None:
    from chrys.app.tui.widgets.diff_view import DiffView

    entries = [
        _entry(
            "/w/changed.py",
            "changed.py",
            before_text=before_text,
            after_text=after_text,
        )
    ]

    async with _PlainPaneApp(entries).run_test() as pilot:
        await pilot.pause()
        diff_view = pilot.app.query_one(DiffView)

        assert diff_view.split is expected_split


@pytest.mark.asyncio
async def test_real_pure_move_shows_placeholder_without_added_file_diff(tmp_path: Path) -> None:
    from chrys.app.tui.widgets.diff_view import DiffView

    src = tmp_path / "old_name.py"
    dst = tmp_path / "new_name.py"
    # Write bytes directly: Path.write_text translates "\n" -> "\r\n" on
    # Windows, which would make the after_text assertion platform-dependent.
    src.write_bytes(b"same\n")
    tracker = MutationTracker(SnapshotStore(tmp_path / "session"))

    tracker.start_turn(1)
    tracker.pre_snapshot([str(src), str(dst)])
    src.rename(dst)
    mutation = tracker.record(str(dst), MutationOp.MOVE, MutationSource.SHELL, "c1", old_path=str(src))
    assert mutation is not None

    turn = tracker.get_turn_mutations(1)
    assert turn is not None
    entries = _build_period_entries(tracker, turn, str(tmp_path))
    assert len(entries) == 1
    assert entries[0].before_text == ""
    assert entries[0].after_text == "same\n"
    assert entries[0].bytes_changed is True

    async with _PlainPaneApp(entries).run_test() as pilot:
        await pilot.pause()
        message = pilot.app.query_one(".diff-message", Static)

        assert message.display is True
        assert message.render().plain == f"File moved: {src} \u2192 new_name.py"
        assert len(pilot.app.query(DiffView)) == 0


@pytest.mark.asyncio
async def test_external_tree_rebuild_does_not_duplicate_leaves() -> None:
    """Rebuilding the external tree must clear first.

    ``ensure_initialized`` is retryable — if ``_initialize`` raises mid-build
    the pane stays un-initialized and a later tab revisit re-runs it — so
    ``_build_external_tree`` (like the workspace builder) must not stack rows.
    """
    entries = [
        _entry("/repo/in.py", "in.py"),
        _entry("/elsewhere/out.py", "../out.py"),
    ]

    async with _PaneApp(entries).run_test() as pilot:
        await pilot.pause()
        pane = pilot.app.query_one(DiffPeriodPane)

        ext_tree = pane.query_one(".external-tree", Tree)
        assert len(ext_tree.root.children) == 1, "expected one external leaf after initial build"

        pane._build_external_tree()
        assert len(ext_tree.root.children) == 1, "external rebuild must not stack leaves"


@pytest.mark.asyncio
async def test_leaf_toggle_via_check_toggled_message() -> None:
    """Simulating ``CheckToggled`` on a leaf flips its state.

    This is the exact code path that a real mouse click ends up
    exercising — the tree subclass posts ``CheckToggled``, the pane's
    handler mutates ``_unchecked``, and the labels re-render.
    """
    entries = [
        _entry("/w/a.txt", "a.txt"),
        _entry("/w/b.txt", "b.txt"),
    ]
    async with _PaneApp(entries).run_test() as pilot:
        pane = pilot.app.query_one(DiffPeriodPane)
        tree = pilot.app.query_one(".workspace-tree", CheckableTree)

        # Find the leaf node for a.txt.  Workspace tree has no folders
        # here (flat), so leaves are direct children of root.
        leaf_a = next(
            child
            for child in tree.root.children
            if isinstance(child.data, DiffFileEntry) and child.data.rel_path == "a.txt"
        )

        # Defaults: all checked.
        assert pane.selected_paths() == ["/w/a.txt", "/w/b.txt"]

        # Post the message the Tree subclass would post on a checkbox click.
        pane.post_message(CheckableTree.CheckToggled(leaf_a))
        await pilot.pause()

        assert "/w/a.txt" not in pane.selected_paths()
        assert "/w/b.txt" in pane.selected_paths()

        # Label should now show the unchecked glyph.
        assert "[ ]" in leaf_a.label.plain
        assert "[x]" not in leaf_a.label.plain

        # Toggle back.
        pane.post_message(CheckableTree.CheckToggled(leaf_a))
        await pilot.pause()
        assert pane.selected_paths() == ["/w/a.txt", "/w/b.txt"]
        assert "[x]" in leaf_a.label.plain


@pytest.mark.asyncio
async def test_duplicate_zero_diff_entries_are_filtered_before_tree_render() -> None:
    """Duplicate same-file entries should not render a stale (+0, -0) leaf."""
    entries = [
        _entry("/w/Power.yaml", "Power.yaml", before_text="same\n", after_text="same\n"),
        _entry("/w/Power.yaml", "Power.yaml", before_text="old\n", after_text="new\n"),
    ]
    async with _PaneApp(entries).run_test() as pilot:
        tree = pilot.app.query_one(".workspace-tree", CheckableTree)

        leaves = [child for child in tree.root.children if isinstance(child.data, DiffFileEntry)]
        assert len(leaves) == 1
        assert leaves[0].label.plain == "[x] [+-] Power.yaml"
        assert leaves[0].data.before_text == "old\n"
        assert leaves[0].data.after_text == "new\n"


@pytest.mark.asyncio
async def test_later_duplicate_zero_diff_entry_clears_existing_row() -> None:
    """A later no-op duplicate should clear an earlier stale row for that file."""
    entries = [
        _entry("/w/Power.yaml", "Power.yaml", before_text="old\n", after_text="new\n"),
        _entry("/w/Power.yaml", "Power.yaml", before_text="same\n", after_text="same\n"),
    ]
    async with _PaneApp(entries).run_test() as pilot:
        tree = pilot.app.query_one(".workspace-tree", CheckableTree)

        leaves = [child for child in tree.root.children if isinstance(child.data, DiffFileEntry)]
        assert leaves == []


@pytest.mark.asyncio
async def test_eol_only_entry_shows_centered_explanation() -> None:
    """EOL-only entries stay in the tree and show a clear empty-diff message."""
    entries = [
        _entry(
            "/w/Power.yaml",
            "Power.yaml",
            before_text="name: Power\n",
            after_text="name: Power\r\n",
        )
    ]
    async with _PaneApp(entries).run_test() as pilot:
        await pilot.pause()
        tree = pilot.app.query_one(".workspace-tree", CheckableTree)
        leaves = [child for child in tree.root.children if isinstance(child.data, DiffFileEntry)]
        assert len(leaves) == 1

        message = pilot.app.query_one(".diff-message", Static)
        assert message.display is True
        assert message.render().plain == "Only line endings changed. No line content changes to display."

        content_box = pilot.app.query_one(".diff-content", VerticalGroup)
        assert str(content_box.border_title).endswith("Power.yaml (+0, -0)")
        assert str(content_box.border_subtitle) == "CRLF - UTF-8"


@pytest.mark.asyncio
async def test_metadata_only_entry_shows_centered_explanation() -> None:
    """Byte-only text changes stay visible even when decoded text is identical."""
    entries = [
        _entry(
            "/w/Power.yaml",
            "Power.yaml",
            before_text="name: Power\n",
            after_text="name: Power\n",
            bytes_changed=True,
        )
    ]
    async with _PaneApp(entries).run_test() as pilot:
        await pilot.pause()
        message = pilot.app.query_one(".diff-message", Static)
        assert message.display is True
        assert message.render().plain == (
            "Only file encoding or byte-level representation changed. No line content changes to display."
        )

        content_box = pilot.app.query_one(".diff-content", VerticalGroup)
        assert str(content_box.border_title).endswith("Power.yaml (+0, -0)")
        assert str(content_box.border_subtitle) == "LF - UTF-8"


@pytest.mark.asyncio
async def test_diff_content_subtitle_shows_eol_and_encoding() -> None:
    entries = [
        _entry(
            "/w/notes.txt",
            "notes.txt",
            before_text="old\n",
            after_text="new\r\n",
            encoding="gbk",
        )
    ]
    async with _PaneApp(entries).run_test() as pilot:
        await pilot.pause()
        content_box = pilot.app.query_one(".diff-content", VerticalGroup)
        assert str(content_box.border_subtitle) == "CRLF - GBK"


@pytest.mark.asyncio
async def test_folder_toggle_cascades_to_descendants() -> None:
    """Toggling a folder in tri-state mode propagates to every descendant.

    ``src/`` has two files and starts fully checked.  First toggle on
    the folder → every descendant becomes unchecked (``[x]`` → ``[ ]``).
    Second toggle → everything checks back on (``[ ]`` → ``[x]``).
    """
    entries = [
        _entry("/w/src/a.py", "src/a.py"),
        _entry("/w/src/b.py", "src/b.py"),
        _entry("/w/other.txt", "other.txt"),
    ]
    async with _PaneApp(entries).run_test() as pilot:
        pane = pilot.app.query_one(DiffPeriodPane)
        tree = pilot.app.query_one(".workspace-tree", CheckableTree)

        src_folder = next(child for child in tree.root.children if child.label.plain.endswith("src"))

        # Initial: everything checked.
        assert set(pane.selected_paths()) == {"/w/src/a.py", "/w/src/b.py", "/w/other.txt"}

        # Toggle folder — cascades to uncheck both src leaves.
        pane.post_message(CheckableTree.CheckToggled(src_folder))
        await pilot.pause()
        assert set(pane.selected_paths()) == {"/w/other.txt"}
        assert "[ ]" in src_folder.label.plain

        # Toggle again — cascade re-checks both leaves.
        pane.post_message(CheckableTree.CheckToggled(src_folder))
        await pilot.pause()
        assert set(pane.selected_paths()) == {"/w/src/a.py", "/w/src/b.py", "/w/other.txt"}
        assert "[x]" in src_folder.label.plain


@pytest.mark.asyncio
async def test_partial_selection_shows_mixed_state_on_folder() -> None:
    """Unchecking one of a folder's descendants flips the folder to ``[~]``."""
    entries = [
        _entry("/w/src/a.py", "src/a.py"),
        _entry("/w/src/b.py", "src/b.py"),
    ]
    async with _PaneApp(entries).run_test() as pilot:
        pane = pilot.app.query_one(DiffPeriodPane)
        tree = pilot.app.query_one(".workspace-tree", CheckableTree)

        src_folder = next(child for child in tree.root.children if child.label.plain.endswith("src"))
        leaf_a = next(
            child
            for child in src_folder.children
            if isinstance(child.data, DiffFileEntry) and child.data.rel_path.endswith("a.py")
        )

        pane.post_message(CheckableTree.CheckToggled(leaf_a))
        await pilot.pause()

        # Folder should now render the mixed (tri-state) glyph.
        assert "[~]" in src_folder.label.plain


@pytest.mark.asyncio
async def test_selection_changed_message_fires_with_counts() -> None:
    """``DiffPeriodPane.SelectionChanged`` fires on every toggle with the
    right counts.  The rollback modal relies on this message to drive
    the "Revert N file changes" label.
    """
    entries = [
        _entry("/w/a.txt", "a.txt"),
        _entry("/w/b.txt", "b.txt"),
        _entry("/w/c.txt", "c.txt"),
    ]
    captured: list[tuple[int, int]] = []

    class _ListenerApp(App):
        def compose(self) -> ComposeResult:
            yield DiffPeriodPane(entries, checkable=True)

        def on_diff_period_pane_selection_changed(self, event: DiffPeriodPane.SelectionChanged) -> None:
            captured.append((event.selected_count, event.total_count))

    async with _ListenerApp().run_test() as pilot:
        pane = pilot.app.query_one(DiffPeriodPane)
        tree = pilot.app.query_one(".workspace-tree", CheckableTree)
        leaves = [child for child in tree.root.children if isinstance(child.data, DiffFileEntry)]

        # Uncheck a; then uncheck b.
        pane.post_message(CheckableTree.CheckToggled(leaves[0]))
        await pilot.pause()
        pane.post_message(CheckableTree.CheckToggled(leaves[1]))
        await pilot.pause()

        # Counts track the selection: 3 -> 2 -> 1, total always 3.
        assert captured == [(2, 3), (1, 3)]


async def test_badge_alignment_after_pane_removed_is_noop() -> None:
    """A pending after-refresh callback can fire after the pane left the
    DOM — ``RollbackModal`` swaps panes on turn change while an
    init/resize ``call_after_refresh`` is still queued.  The alignment
    refresh must treat the unmounted trees as "nothing to re-anchor"
    instead of crashing on ``query_one(".workspace-tree")``.
    """
    app = _PaneApp([_entry("/repo/a.py", "a.py", source=MutationSource.SHELL.value)])
    async with app.run_test() as pilot:
        pane = app.query_one(DiffPeriodPane)
        await pilot.pause()
        assert pane._initialized
        await pane.remove()
        pane._refresh_badge_alignment()  # must not raise


@pytest.mark.asyncio
async def test_narrow_diff_footer_leaves_a_cell_before_back() -> None:
    async with _DiffScreenApp().run_test(size=(44, 20)) as pilot:
        await _wait_for_diff_screen(
            pilot,
            lambda: (
                {"go_back", "toggle_view", "toggle_change_list"}
                <= {key.action for key in pilot.app.screen.query("FooterKey")}
            ),
            message="back shortcut rendered",
        )
        await pilot.pause()
        line = pilot.app.screen._compositor.render_strips()[-1].text
        back = line.index("esc Back")
        assert line[back - 1] == " ", line
