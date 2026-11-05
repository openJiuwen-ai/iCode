# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tests for TUI surfaces participating in GC freeze coordination."""

from __future__ import annotations

import asyncio
import gc
import inspect
import logging
import weakref
from concurrent.futures import ThreadPoolExecutor
from contextlib import suppress
from threading import Event
from types import SimpleNamespace
from typing import Any

import pytest
from rich.text import Text
from textual._node_list import NodeList
from textual.app import App, ComposeResult
from textual.pilot import Pilot
from textual.strip import Strip
from textual.widgets import Static

from chrys.app.tui.screens.main import screen as screen_module
from chrys.app.tui.screens.main.screen import MainScreen
from chrys.app.tui.screens.main.state import MainScreenState
from chrys.app.tui.screens.main.suggestions import SuggestionHandler
from chrys.app.tui.support import gc_freeze
from chrys.app.tui.support.gc_freeze import (
    DetachedFifoCache,
    DetachedLruCache,
    GcAbsorbReason,
    GcAbsorbRequested,
    GcFreezeBlockReason,
    GcFreezeCoordinator,
    GcReclaimReason,
    GcReclaimRequested,
    abort_textual_screen_gc_freeze,
    after_textual_screen_gc_freeze,
    prepare_textual_screen_for_gc,
)
from chrys.app.tui.terminal.panel import ShellPanel
from chrys.app.tui.terminal.widget import Terminal
from chrys.app.tui.widgets.chat.panel import ChatPanel
from chrys.app.tui.widgets.chat.session_json import SessionJsonPanel
from chrys.app.tui.widgets.chat.tool_call import ToolCall, ToolGroup
from chrys.app.tui.widgets.chrome.file_index import ProjectPathIndex, WorkspacePathIndex
from chrys.app.tui.widgets.chrome.file_scanner import ProjectPathScanResult, ProjectPathSuggestion
from tests.support.pilot_barrier import screen_is_settled
from tests.support.waiting import wait_for


class _Participant:
    def __init__(self, reason: GcFreezeBlockReason | None = None) -> None:
        self.reason = reason
        self.calls: list[str] = []

    def gc_freeze_block_reason(self) -> GcFreezeBlockReason | None:
        self.calls.append("block")
        return self.reason

    def prepare_for_gc_freeze(self) -> None:
        self.calls.append("prepare")

    def after_gc_freeze(self) -> None:
        self.calls.append("after")

    def abort_gc_freeze(self) -> None:
        self.calls.append("abort")


class _FailingParticipant(_Participant):
    def __init__(self, label: str) -> None:
        super().__init__()
        self.label = label

    def after_gc_freeze(self) -> None:
        self.calls.append("after")
        raise RuntimeError(self.label)


def _weakref_is_alive(reference: weakref.ReferenceType[object]) -> bool:
    """Inspect a weakref without retaining its target in the async test frame."""
    return reference() is not None


def _tool_call_weakref(group: ToolGroup, call_id: str) -> weakref.ReferenceType[ToolCall]:
    """Build a weakref without retaining the widget in an async test frame."""
    descendant = group.get_tool(call_id)
    assert isinstance(descendant, ToolCall)
    return weakref.ref(descendant)


@pytest.mark.parametrize(
    ("loading", "running", "scrolling", "expected"),
    [
        (True, False, False, GcFreezeBlockReason.AGENT_LOADING),
        (False, True, False, GcFreezeBlockReason.AGENT_RUNNING),
        (False, False, True, GcFreezeBlockReason.SCROLL_GC_PAUSED),
    ],
)
def test_main_screen_own_freeze_gates(
    monkeypatch: pytest.MonkeyPatch,
    loading: bool,
    running: bool,
    scrolling: bool,
    expected: GcFreezeBlockReason,
) -> None:
    participant = _Participant(GcFreezeBlockReason.PARTICIPANT)
    state = MainScreenState()
    state.run.agent_loading = loading
    state.run.agent_running = running
    screen = SimpleNamespace(_state=state, _gc_freeze_participants=(participant,))
    monkeypatch.setattr(screen_module, "scroll_gc_paused", lambda: scrolling)

    assert MainScreen.gc_freeze_block_reason(screen) is expected
    assert participant.calls == []


def test_main_screen_delegates_gate_and_hooks_in_registration_order(monkeypatch: pytest.MonkeyPatch) -> None:
    first = _Participant()
    second = _Participant(GcFreezeBlockReason.PARTICIPANT)
    third = _Participant(GcFreezeBlockReason.SHELL_VISIBLE)
    screen = SimpleNamespace(_state=MainScreenState(), _gc_freeze_participants=(first, second, third))
    monkeypatch.setattr(screen_module, "scroll_gc_paused", lambda: False)
    screen_cache_calls: list[str] = []
    monkeypatch.setattr(
        screen_module,
        "prepare_textual_screen_for_gc",
        lambda _screen: screen_cache_calls.append("prepare"),
    )
    monkeypatch.setattr(
        screen_module,
        "after_textual_screen_gc_freeze",
        lambda _screen: screen_cache_calls.append("after"),
    )

    assert MainScreen.gc_freeze_block_reason(screen) is GcFreezeBlockReason.PARTICIPANT
    MainScreen.prepare_for_gc_freeze(screen)
    MainScreen.after_gc_freeze(screen)

    assert first.calls == ["block", "prepare", "after"]
    assert second.calls == ["block", "prepare", "after"]
    assert third.calls == ["prepare", "after"]
    assert screen_cache_calls == ["prepare", "after"]


def test_main_screen_reports_every_gc_renewal_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    first = _FailingParticipant("first participant")
    second = _FailingParticipant("second participant")
    screen = SimpleNamespace(_gc_freeze_participants=(first, second))

    def _fail_screen_cache(_screen: object) -> None:
        raise RuntimeError("screen cache")

    monkeypatch.setattr(screen_module, "after_textual_screen_gc_freeze", _fail_screen_cache)

    with pytest.raises(ExceptionGroup) as raised:
        MainScreen.after_gc_freeze(screen)

    assert [str(error) for error in raised.value.exceptions] == [
        "screen cache",
        "first participant",
        "second participant",
    ]


class _ScreenCacheApp(App):
    def compose(self) -> ComposeResult:
        yield ChatPanel()


class _LinkedRingLruCache:
    """Upstream Textual's LRU layout: entries linked into a ring that ``clear()`` abandons."""

    def __init__(self, maxsize: int) -> None:
        self._maxsize = maxsize
        self._links: dict[object, list[Any]] = {}
        self._head: list[Any] = []

    def __setitem__(self, key: object, value: object) -> None:
        head = self._head
        if not head:
            head[:] = [head, head, key, value]
        else:
            self._head = [head[0], head, key, value]
            head[0][1] = self._head
            head[0] = self._head
        self._links[key] = self._head
        if len(self._links) > self._maxsize:
            head = self._head
            oldest = head[0]
            oldest[0][1] = head
            head[0] = oldest[0]
            del self._links[oldest[2]]

    def get(self, key: object, default: object = None) -> object:
        link = self._links.get(key)
        return default if link is None else link[3]

    def clear(self) -> None:
        self._links.clear()
        self._head = []


def test_textual_cache_probe_accepts_the_installed_acyclic_caches() -> None:
    assert gc_freeze.textual_screen_caches_acyclic.__wrapped__() is True
    assert gc_freeze.textual_screen_caches_acyclic() is True
    assert gc.isenabled() is True


def test_textual_cache_probe_rejects_a_linked_ring_lru(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(gc_freeze, "LRUCache", _LinkedRingLruCache)

    assert gc_freeze.textual_screen_caches_acyclic.__wrapped__() is False
    assert gc.isenabled() is True


def test_textual_cache_probe_failure_keeps_the_detach_path(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    class _ChangedFifoCache:
        def __init__(self, maxsize: int) -> None:
            raise TypeError(f"unexpected FIFOCache signature for maxsize={maxsize}")

    monkeypatch.setattr(gc_freeze, "FIFOCache", _ChangedFifoCache)

    with caplog.at_level(logging.WARNING, logger=gc_freeze.__name__):
        assert gc_freeze.textual_screen_caches_acyclic.__wrapped__() is False
    assert gc.isenabled() is True
    assert "keeps detaching screen caches" in caplog.text


@pytest.mark.asyncio
async def test_textual_screen_cache_hooks_leave_acyclic_caches_and_layout_in_place() -> None:
    app = _ScreenCacheApp()
    async with app.run_test() as pilot:
        screen = app.screen
        await wait_for(lambda: screen_is_settled(app, screen), pilot=pilot, description="settled screen layout")
        installed = [
            (widget, widget._box_model_cache, widget._query_one_cache, widget._arrangement_cache)
            for widget in screen.walk_children(with_self=True)
        ]
        placed = set(screen._compositor.widgets)
        assert placed

        prepare_textual_screen_for_gc(screen)
        after_textual_screen_gc_freeze(screen)
        abort_textual_screen_gc_freeze(screen)

        assert set(screen._compositor.widgets) == placed
        for widget, box_cache, query_cache, arrangement_cache in installed:
            assert widget._box_model_cache is box_cache
            assert widget._query_one_cache is query_cache
            assert widget._arrangement_cache is arrangement_cache
        assert screen._layout_required is False


def test_screen_caches_freeze_in_place_only_while_a_removal_clears_them(monkeypatch: pytest.MonkeyPatch) -> None:
    assert gc_freeze.textual_screen_caches_freeze_in_place() is True

    monkeypatch.setattr(NodeList, "_remove", inspect.unwrap(NodeList._remove))

    assert gc_freeze.textual_screen_caches_freeze_in_place() is False


@pytest.mark.asyncio
@pytest.mark.parametrize("fallback", ["cyclic-caches", "stock-removal"])
async def test_textual_screen_cache_hooks_clear_compositor_and_renew_cyclic_lrus(
    monkeypatch: pytest.MonkeyPatch,
    fallback: str,
) -> None:
    if fallback == "cyclic-caches":
        monkeypatch.setattr(gc_freeze, "textual_screen_caches_acyclic", lambda: False)
    else:
        # A removal that leaves stale lookups behind needs the detach path to drop them.
        monkeypatch.setattr(NodeList, "_remove", inspect.unwrap(NodeList._remove))
    app = _ScreenCacheApp()
    async with app.run_test() as pilot:
        screen = app.screen
        await wait_for(lambda: screen_is_settled(app, screen), pilot=pilot, description="settled screen layout")
        assert screen._layout_required is False
        old_box_cache = screen._box_model_cache
        old_query_cache = screen._query_one_cache
        old_arrangement_cache = screen._arrangement_cache
        old_box_cache["box"] = object()  # type: ignore[index]
        old_query_cache[("query",)] = screen  # type: ignore[index]
        assert screen._compositor.widgets

        prepare_textual_screen_for_gc(screen)
        assert not screen._compositor.widgets
        assert isinstance(screen._box_model_cache, DetachedLruCache)
        assert isinstance(screen._query_one_cache, DetachedLruCache)
        assert isinstance(screen._arrangement_cache, DetachedFifoCache)

        after_textual_screen_gc_freeze(screen)
        assert screen._box_model_cache is not old_box_cache
        assert screen._query_one_cache is not old_query_cache
        assert screen._arrangement_cache is not old_arrangement_cache
        assert screen._layout_required is True


def test_chat_panel_delegates_gc_hooks_to_dynamic_render_cache_surfaces(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = _Participant()
    second = _Participant()
    panel = ChatPanel()
    monkeypatch.setattr(panel, "_gc_freeze_render_cache_surfaces", lambda: [first, second])

    assert panel.gc_freeze_block_reason() is None
    panel.prepare_for_gc_freeze()
    panel.after_gc_freeze()

    assert first.calls == ["prepare", "after"]
    assert second.calls == ["prepare", "after"]


def test_chat_panel_reports_every_gc_renewal_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    first = _FailingParticipant("first surface")
    second = _FailingParticipant("second surface")
    panel = ChatPanel()
    monkeypatch.setattr(panel, "_gc_freeze_render_cache_surfaces", lambda: [first, second])

    with pytest.raises(ExceptionGroup) as raised:
        panel.after_gc_freeze()

    assert [str(error) for error in raised.value.exceptions] == ["first surface", "second surface"]


def test_session_json_hide_releases_content_and_after_hook_renews_lru() -> None:
    panel = SessionJsonPanel()
    panel.display = True
    panel._formatted_json = "x" * 1_000
    panel._file_mtime = 10.0
    panel._last_theme_key = (True, "#ffffff")
    panel._text_lines = [Text("value")]
    panel._plain_lines = ["value"]
    panel._line_widths = [5]
    panel._max_width = 5
    panel._strip_cache[0] = Strip.blank(5)
    panel._cache_scroll_x = 2
    panel._cache_width = 5
    old_cache = panel._strip_cache
    old_generation = panel._content_generation

    assert panel.gc_freeze_block_reason() is GcFreezeBlockReason.SESSION_JSON_VISIBLE
    panel.hide_session_json()

    assert panel.gc_freeze_block_reason() is None
    assert panel._formatted_json == ""
    assert panel._file_mtime == 0.0
    assert panel._last_theme_key is None
    assert panel._text_lines == []
    assert panel._plain_lines == []
    assert panel._line_widths == []
    assert panel._max_width == 0
    assert len(panel._strip_cache) == 0
    assert panel._cache_scroll_x == -1
    assert panel._cache_width == -1
    assert panel.virtual_size.height == 0
    assert panel._content_generation == old_generation + 1

    panel.prepare_for_gc_freeze()
    assert isinstance(panel._strip_cache, DetachedLruCache)
    assert panel._content_generation == old_generation + 2
    panel.after_gc_freeze()
    assert panel._strip_cache is not old_cache
    assert panel._strip_cache.maxsize == old_cache.maxsize


def test_session_json_highlight_descendants_are_acyclic_while_frozen() -> None:
    class _Sentinel:
        pass

    payload = SessionJsonPanel._highlight('{"value": [1, 2, 3]}', True, None)
    sentinel = _Sentinel()
    sentinel_ref = weakref.ref(sentinel)
    descendant = payload[0][0]
    descendant._text.append(sentinel)  # type: ignore[arg-type]

    gc.collect()
    gc.freeze()
    try:
        del sentinel
        del descendant
        del payload
        assert sentinel_ref() is None
    finally:
        gc.unfreeze()
        gc.collect()


def _pool_runs_calls_in_turn() -> None:
    """Give the running loop one pool thread, which runs its calls in turn; the loop shuts it down as it closes."""
    asyncio.get_running_loop().set_default_executor(ThreadPoolExecutor(max_workers=1))


async def _after_pool_calls_let_go() -> None:
    """Return once the calls queued on the pool before now have let go of what they returned.

    A cancelled call's thread holds what it returns until it returns it, and its future holds that until the loop
    runs the callback the thread queued to hand it over. A call queued after it on the one pool thread ends once the
    thread has moved on; then a loop turn runs that callback, which the thread queued before this call ended.
    """
    await asyncio.to_thread(lambda: None)
    # A call that ends before its future is wrapped returns without a loop turn.
    await asyncio.sleep(0)


@pytest.mark.asyncio
@pytest.mark.parametrize("cancelled", [False, True], ids=["completed", "cancelled"])
async def test_real_session_json_to_thread_payload_drops_while_frozen(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
    *,
    cancelled: bool,
) -> None:
    class _Sentinel:
        pass

    path = tmp_path / "session.json"
    path.write_text('{"value": [1, 2, 3]}', encoding="utf-8")
    original_highlight = SessionJsonPanel._highlight
    loop = asyncio.get_running_loop()
    _pool_runs_calls_in_turn()
    started = asyncio.Event()
    release = Event()
    sentinel_refs: list[weakref.ReferenceType[_Sentinel]] = []

    def _blocked_highlight(json_text: str, dark: bool, gutter_color: str | None):
        payload = original_highlight(json_text, dark, gutter_color)
        sentinel = _Sentinel()
        sentinel_refs.append(weakref.ref(sentinel))
        payload[0][0]._text.append(sentinel)  # type: ignore[arg-type]
        loop.call_soon_threadsafe(started.set)
        release.wait()
        return payload

    monkeypatch.setattr(SessionJsonPanel, "_highlight", staticmethod(_blocked_highlight))
    panel = SessionJsonPanel()
    panel.display = True
    panel._content_generation = 1
    task = asyncio.create_task(panel._load_worker(path, True, None, 1))
    await started.wait()

    gc.collect()
    gc.freeze()
    try:
        panel.hide_session_json()
        if cancelled:
            task.cancel()
        release.set()
        with suppress(asyncio.CancelledError):
            await task
        del task
        del panel
        del _blocked_highlight
        # A cancelled worker does not wait for its highlight to let go of the payload.
        await _after_pool_calls_let_go()

        assert len(sentinel_refs) == 1
        assert sentinel_refs[0]() is None
    finally:
        release.set()
        gc.unfreeze()
        gc.collect()


@pytest.mark.asyncio
async def test_session_json_shell_suspension_allows_in_flight_load_commit(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    from chrys.app.tui.widgets.chat import session_json as session_json_module

    panel = SessionJsonPanel()
    panel.display = True
    panel._content_generation = 7
    monkeypatch.setattr(panel, "scroll_home", lambda *, animate: None)
    started = asyncio.Event()
    release = asyncio.Event()
    call_count = 0

    async def _to_thread(_function, *_args, **_kwargs):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            started.set()
            await release.wait()
            return '{"value": 1}'
        return [Text("new")], ["new"], [3], 3

    monkeypatch.setattr(session_json_module.asyncio, "to_thread", _to_thread)
    task = asyncio.create_task(panel._load_worker(tmp_path / "session.json", True, None, 7))
    await started.wait()

    panel.suspend_for_shell_mode()
    panel.prepare_for_gc_freeze()
    assert panel.display is False
    assert panel.gc_freeze_block_reason() is GcFreezeBlockReason.SESSION_JSON_VISIBLE
    assert panel._content_generation == 7

    release.set()
    await task

    assert panel._formatted_json == '{\n  "value": 1\n}'
    assert panel._status == ""
    assert panel._plain_lines == ["new"]
    assert panel._content_generation == 7

    panel.finish_shell_mode(restore=True)
    assert panel.display is True
    assert panel._shell_mode_suspended is False
    assert panel._plain_lines == ["new"]


def test_session_json_shell_finish_without_restore_cancels_retained_content_once() -> None:
    panel = SessionJsonPanel()
    panel.display = True
    panel._formatted_json = '{"value": 1}'
    panel._plain_lines = ["value"]
    panel._content_generation = 7

    panel.suspend_for_shell_mode()
    panel.finish_shell_mode(restore=False)

    assert panel.display is False
    assert panel._shell_mode_suspended is False
    assert panel.gc_freeze_block_reason() is None
    assert panel._formatted_json == ""
    assert panel._plain_lines == []
    assert panel._content_generation == 8

    panel.finish_shell_mode(restore=False)
    assert panel._content_generation == 8


@pytest.mark.asyncio
async def test_session_json_hide_during_load_prevents_stale_commit(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    from chrys.app.tui.widgets.chat import session_json as session_json_module

    panel = SessionJsonPanel()
    panel.display = True
    panel._content_generation = 7
    started = asyncio.Event()
    release = asyncio.Event()
    call_count = 0

    async def _to_thread(_function, *_args, **_kwargs):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            started.set()
            await release.wait()
            return '{"value": 1}'
        return [Text("new")], ["new"], [3], 3

    monkeypatch.setattr(session_json_module.asyncio, "to_thread", _to_thread)
    task = asyncio.create_task(panel._load_worker(tmp_path / "session.json", True, None, 7))
    await started.wait()

    panel.hide_session_json()
    release.set()
    await task

    assert panel._formatted_json == ""
    assert panel._text_lines == []
    assert panel._plain_lines == []
    assert panel.virtual_size.height == 0


@pytest.mark.asyncio
async def test_session_json_hide_during_rehighlight_prevents_stale_commit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from chrys.app.tui.widgets.chat import session_json as session_json_module

    panel = SessionJsonPanel()
    panel.display = True
    panel._content_generation = 4
    panel._formatted_json = "{}"
    panel._text_lines = [Text("old")]
    started = asyncio.Event()
    release = asyncio.Event()

    async def _to_thread(_function, *_args, **_kwargs):
        started.set()
        await release.wait()
        return [Text("new")], ["new"], [3], 3

    monkeypatch.setattr(session_json_module.asyncio, "to_thread", _to_thread)
    task = asyncio.create_task(panel._rehighlight_worker(False, None, 4))
    await started.wait()

    panel.hide_session_json()
    release.set()
    await task

    assert panel._formatted_json == ""
    assert panel._text_lines == []
    assert panel._plain_lines == []


def test_shell_participant_blocks_visible_mode_and_renews_render_lru() -> None:
    panel = ShellPanel()
    terminal = Terminal(size=(20, 4))
    panel._terminal = terminal
    terminal._strip_cache[0] = Strip.blank(20)
    old_cache = terminal._strips
    old_capacity = old_cache.maxsize

    assert panel.gc_freeze_block_reason() is None
    panel.prepare_for_gc_freeze()
    assert isinstance(terminal._strips, DetachedLruCache)

    panel.after_gc_freeze()
    assert terminal._strips is not old_cache
    assert terminal._strips.maxsize == old_capacity

    panel.display = True
    assert panel.gc_freeze_block_reason() is GcFreezeBlockReason.SHELL_VISIBLE


@pytest.mark.parametrize("surface", ["json", "terminal"])
def test_post_freeze_lru_renewal_keeps_refilled_links_collectible(surface: str) -> None:
    class _Sentinel:
        pass

    panel: SessionJsonPanel | ShellPanel
    if surface == "json":
        json_panel = SessionJsonPanel()
        json_panel.display = False
        json_panel.prepare_for_gc_freeze()
        panel = json_panel
    else:
        shell_panel = ShellPanel()
        shell_panel._terminal = Terminal(size=(20, 4))
        shell_panel.prepare_for_gc_freeze()
        panel = shell_panel

    gc.collect()
    gc.freeze()
    try:
        panel.after_gc_freeze()
        cache = (
            panel._strip_cache if isinstance(panel, SessionJsonPanel) else panel._terminal._strips  # type: ignore[union-attr]
        )
        sentinel = _Sentinel()
        sentinel_ref = weakref.ref(sentinel)
        cache["key"] = sentinel  # type: ignore[index]
        del sentinel
        cache.clear()
        gc.collect()

        assert sentinel_ref() is None
    finally:
        gc.unfreeze()
        gc.collect()


def test_suggestion_participant_blocks_popup_without_releasing_warm_cache() -> None:
    handler = object.__new__(SuggestionHandler)
    warm_index = WorkspacePathIndex("/repo")
    handler._view = SimpleNamespace(suggestions_active=True)
    handler._file_index = warm_index

    assert handler.gc_freeze_block_reason() is GcFreezeBlockReason.SUGGESTIONS_VISIBLE
    handler.prepare_for_gc_freeze()
    handler.after_gc_freeze()
    assert handler._file_index is warm_index

    handler._view.suggestions_active = False
    assert handler.gc_freeze_block_reason() is None


def test_frozen_project_path_index_dies_by_refcount_without_unfreeze() -> None:
    scan = ProjectPathScanResult.from_suggestions(
        root="/repo",
        paths=[ProjectPathSuggestion("src/chrys/app.py", "file")],
    )
    index = ProjectPathIndex.build(scan)
    index_ref = weakref.ref(index)

    gc.collect()
    gc.freeze()
    try:
        del index
        assert index_ref() is None
    finally:
        gc.unfreeze()
        gc.collect()


@pytest.mark.asyncio
async def test_cancelled_inflight_index_to_thread_drops_payload_while_frozen() -> None:
    scan = ProjectPathScanResult.from_suggestions(
        root="/repo",
        paths=[ProjectPathSuggestion(f"src/file_{path_number}.py", "file") for path_number in range(1_000)],
    )
    index = ProjectPathIndex.build(scan)
    index_ref = weakref.ref(index)
    payload_box = [index]
    del index
    loop = asyncio.get_running_loop()
    _pool_runs_calls_in_turn()
    started = asyncio.Event()
    release = Event()

    def _return_index() -> ProjectPathIndex:
        # Read before it is released: the box is cleared once the call is cancelled, and it may resume after that.
        value = payload_box[0]
        loop.call_soon_threadsafe(started.set)
        release.wait()
        return value

    task = asyncio.create_task(asyncio.to_thread(_return_index))
    await started.wait()
    gc.collect()
    gc.freeze()
    try:
        task.cancel()
        release.set()
        with suppress(asyncio.CancelledError):
            await task
        del task
        del _return_index
        payload_box.clear()
        # The cancelled call does not wait for its thread to let go of the index.
        await _after_pool_calls_let_go()

        assert index_ref() is None
    finally:
        release.set()
        gc.unfreeze()
        gc.collect()


class _ManualClock:
    def __init__(self) -> None:
        self.value = 0.0

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


class _ToolGroupGcApp(App):
    """Chat panel under a real coordinator whose own freeze hooks are inert."""

    def __init__(self) -> None:
        self.clock = _ManualClock()
        self.coordinator = GcFreezeCoordinator(self, enabled=True, clock=self.clock)
        self.reclaim_requests: list[GcReclaimRequested] = []
        super().__init__()

    def compose(self) -> ComposeResult:
        yield ChatPanel()

    def freeze_block_reason(self) -> GcFreezeBlockReason | None:
        return None

    def prepare_for_gc_freeze(self) -> None:
        return

    def after_gc_freeze(self) -> None:
        return

    def on_gc_absorb_requested(self, event: GcAbsorbRequested) -> None:
        self.coordinator.request_absorb(
            reason=event.reason,
            terminal_boundary=event.terminal_boundary,
            requested_at=event.time,
        )

    def on_gc_reclaim_requested(self, event: GcReclaimRequested) -> None:
        self.reclaim_requests.append(event)
        self.coordinator.request_reclaim(
            reason=event.reason,
            prompt=event.prompt,
            requested_at=event.time,
            removed=event.removed,
        )

    def on_unmount(self) -> None:
        self.coordinator.close()


def _unreachable(reference: weakref.ReferenceType[ToolCall]) -> bool:
    """Collect young garbage, then report whether *reference*'s target is gone."""
    gc.collect()
    return reference() is None


async def _freeze_then_build_a_tool_turn(app: _ToolGroupGcApp, pilot: Pilot[None]) -> ToolGroup:
    """Freeze the empty transcript, then build one completed tool turn under the new epoch."""
    panel = app.query_one(ChatPanel)
    app.coordinator.start()
    await wait_for(
        lambda: app.coordinator.frozen,
        pilot=pilot,
        description="GC coordinator freezes the empty transcript",
    )
    await panel.add_user_message("tools")
    await panel.add_tool_start("tool1", "plain_tool", "", args={"value": 1})
    await panel.add_tool_result("tool1", "plain_tool", "done", 10)
    return panel.query_one(ToolGroup)


async def _collapse_and_drain_prune(group: ToolGroup, pilot: Pilot[None]) -> None:
    group.collapsed = True
    await wait_for(lambda: not group._content_mounted, pilot=pilot, description="collapsed tool subtree is removed")
    removal_drained = asyncio.Event()
    assert group.call_later(removal_drained.set)
    await wait_for(removal_drained.is_set, pilot=pilot, description="tool group removal callback drains")


async def _terminal_absorb(app: _ToolGroupGcApp, group: ToolGroup, pilot: Pilot[None]) -> str:
    previous_metrics = app.coordinator.last_action_metrics
    group.post_message(GcAbsorbRequested(GcAbsorbReason.TURN_TERMINAL, terminal_boundary=True))
    await wait_for(
        lambda: app.coordinator.last_action_metrics is not previous_metrics,
        pilot=pilot,
        description="turn-end absorb completes",
    )
    metrics = app.coordinator.last_action_metrics
    assert metrics is not None
    return metrics.action


@pytest.mark.asyncio
async def test_young_completed_tool_subtree_needs_no_reclaim_once_unreachable() -> None:
    """A subtree built and pruned between two freezes is young garbage, not frozen state."""
    app = _ToolGroupGcApp()
    async with app.run_test() as pilot:
        group = await _freeze_then_build_a_tool_turn(app, pilot)
        absorbs_before_turn = app.coordinator._absorbs_since_reclaim
        descendant_ref = _tool_call_weakref(group, "tool1")
        await _collapse_and_drain_prune(group, pilot)

        assert [request.reason for request in app.reclaim_requests] == [GcReclaimReason.STABLE_CONTENT_REMOVED]
        assert app.reclaim_requests[0].removed is not None
        assert app.coordinator._idle_reclaim_pending is False
        # The spinner's cancelled interval handle keeps the card reachable from the
        # event loop until its deadline passes; production turns end long after that.
        await wait_for(
            lambda: _unreachable(descendant_ref),
            pilot=pilot,
            description="pruned tool card becomes unreachable",
        )

        assert await _terminal_absorb(app, group, pilot) == "absorb"
        assert app.coordinator._absorbs_since_reclaim == absorbs_before_turn + 1
        assert app.coordinator._idle_reclaim_pending is False
        assert app.coordinator._idle_reclaim_reasons == set()


def _tool_body_weakref(group: ToolGroup, call_id: str) -> weakref.ReferenceType[Static]:
    """Build a weakref to a tool card's body without retaining it in an async test frame."""
    descendant = group.get_tool(call_id)
    assert isinstance(descendant, ToolCall)
    return weakref.ref(descendant.query_one("#tc-body", Static))


@pytest.mark.asyncio
async def test_young_tool_subtree_still_held_at_the_next_absorb_dies_on_its_idle_full_reclaim() -> None:
    """A young removed node something still holds when the next freeze runs is frozen, so it earns the reclaim."""
    app = _ToolGroupGcApp()
    async with app.run_test() as pilot:
        group = await _freeze_then_build_a_tool_turn(app, pilot)
        card_ref = _tool_call_weakref(group, "tool1")
        body_ref = _tool_body_weakref(group, "tool1")
        holder = [body_ref()]
        await _collapse_and_drain_prune(group, pilot)
        assert app.coordinator._idle_reclaim_pending is False
        await wait_for(lambda: _unreachable(card_ref), pilot=pilot, description="pruned tool card becomes unreachable")
        assert _weakref_is_alive(body_ref)

        assert await _terminal_absorb(app, group, pilot) == "absorb"
        assert app.coordinator._idle_reclaim_pending is True
        assert app.coordinator._idle_reclaim_reasons == {GcReclaimReason.STABLE_CONTENT_REMOVED}

        holder.clear()
        gc.collect()
        assert _weakref_is_alive(body_ref)
        app.clock.advance(4.0)
        app.coordinator.on_tick()
        await wait_for(
            lambda: not app.coordinator._idle_reclaim_pending,
            pilot=pilot,
            description="idle GC reclaim completes",
        )

        assert app.coordinator.last_action_metrics is not None
        assert app.coordinator.last_action_metrics.action == "full"
        assert not _weakref_is_alive(body_ref)


@pytest.mark.asyncio
async def test_frozen_completed_tool_subtree_dies_on_its_idle_full_reclaim() -> None:
    app = _ToolGroupGcApp()
    async with app.run_test() as pilot:
        panel = app.query_one(ChatPanel)
        await panel.add_user_message("tools")
        await panel.add_tool_start("tool1", "plain_tool", "", args={"value": 1})
        await panel.add_tool_result("tool1", "plain_tool", "done", 10)
        group = panel.query_one(ToolGroup)
        descendant_ref = _tool_call_weakref(group, "tool1")

        app.coordinator.start()
        await wait_for(
            lambda: app.coordinator.frozen,
            pilot=pilot,
            description="GC coordinator freezes mounted content",
        )
        assert app.coordinator.frozen is True

        group.collapsed = True
        await wait_for(
            lambda: not group._content_mounted and app.coordinator._idle_reclaim_pending,
            pilot=pilot,
            description="collapsed tool subtree is removed and reclaim requested",
        )
        assert group._content_mounted is False
        assert app.coordinator._idle_reclaim_pending is True
        assert [(request.reason, request.prompt, request.removed) for request in app.reclaim_requests] == [
            (GcReclaimReason.STABLE_CONTENT_REMOVED, False, None)
        ]
        gc.collect()
        assert _weakref_is_alive(descendant_ref)

        # The real idle window spans many event-loop turns. Drain removal and
        # InvokeLater bookkeeping before advancing the deterministic clock so
        # the full reclaim measures the detached subtree, not the test's await.
        removal_drained = asyncio.Event()
        assert group.call_later(removal_drained.set)
        await wait_for(removal_drained.is_set, pilot=pilot, description="tool group removal callback drains")

        app.clock.advance(4.0)
        app.coordinator.on_tick()
        await wait_for(
            lambda: not app.coordinator._idle_reclaim_pending,
            pilot=pilot,
            description="idle GC reclaim completes",
        )

        assert app.coordinator._idle_reclaim_pending is False
        assert app.coordinator.frozen is True
        assert not _weakref_is_alive(descendant_ref)
