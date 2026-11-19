# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Exercise native Windows copy routing through real TUI keyboard events."""

from __future__ import annotations

import asyncio
import gc
from concurrent.futures import Future
from dataclasses import replace
from pathlib import Path
from threading import Event, get_ident
from types import ModuleType, SimpleNamespace
from weakref import ref

import pytest
from PIL import Image
from textual.app import App
from textual.selection import SELECT_ALL
from textual.widget import Widget
from textual.widgets import Input

from chrys.app.tui import clipboard
from chrys.app.tui.screens.dialogs import tool_view
from chrys.app.tui.screens.main.screen import MainScreen
from chrys.app.tui.support import clipboard_writer
from chrys.app.tui.widgets.chat.messages import UserMessage
from chrys.app.tui.widgets.chat.panel import ChatPanel
from chrys.app.tui.widgets.chrome import image_paste
from chrys.app.tui.widgets.chrome.draft_editor import DraftEditor
from chrys.app.tui.widgets.chrome.input_bar import _ChatTextArea
from chrys.app.tui.widgets.chrome.prompt_draft import ChatDraft
from chrys.app.tui.widgets.editor import EditorMode
from chrys.app.tui.widgets.input import EnhancedInput
from chrys.app.tui.widgets.markdown import VirtualizedMarkdown
from chrys.app.tui.widgets.text_area import EnhancedTextArea
from tests.support.tui_app_harness import make_chrys_app
from tests.support.waiting import ENGINE_TURN_TIMEOUT, MAIN_SCREEN_BUILD_TIMEOUT, wait_for


@pytest.fixture(autouse=True)
def _windows_clipboard(monkeypatch: pytest.MonkeyPatch) -> None:
    # Only the clipboard adapter sees Windows; real UI/platform services
    # must keep using the host OS when this test runs on Linux or macOS.
    platform = clipboard.platform_helpers
    shadow = ModuleType(platform.__name__)
    shadow.__dict__.update(platform.__dict__)
    info = replace(platform.get_platform(), os_name="windows")
    shadow.get_platform = lambda: info
    monkeypatch.setattr(clipboard, "platform_helpers", shadow)


async def test_repeated_chat_ctrl_c_keeps_ui_live_without_osc52_and_pastes_latest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    entered, release = Event(), Event()
    ui_thread = get_ident()
    native_threads: list[int] = []
    native_copies: list[str] = []
    terminal_copies: list[str] = []

    def write(text: str) -> None:
        native_threads.append(get_ident())
        entered.set()
        # Fail deterministically on the old synchronous path without hanging its event loop.
        if get_ident() != ui_thread:
            assert release.wait(10)
        native_copies.append(text)

    monkeypatch.setattr(clipboard, "clipboard_copy", write)
    monkeypatch.setattr(clipboard.platform_helpers, "clipboard_paste", lambda: "external text")
    app = make_chrys_app(tmp_path)
    monkeypatch.setattr(app, "copy_to_clipboard", terminal_copies.append)
    try:
        async with app.run_test(size=(110, 35)) as pilot:
            await wait_for(
                lambda: isinstance(app.screen, MainScreen) and app.screen.is_mounted,
                timeout=MAIN_SCREEN_BUILD_TIMEOUT,
                pilot=pilot,
            )
            screen = app.screen
            panel = screen.query_one(ChatPanel)
            await panel.add_user_message("first selection")
            await panel.add_user_message("newest selection")
            bodies = [message.query_one(VirtualizedMarkdown) for message in panel.query(UserMessage)]
            await wait_for(lambda: all(body.get_selection(SELECT_ALL) is not None for body in bodies), pilot=pilot)

            screen.selections = {bodies[0]: SELECT_ALL}
            await pilot.press("ctrl+c")
            await wait_for(entered.is_set)
            assert native_threads and native_threads[0] != ui_thread
            assert not native_copies
            assert not terminal_copies

            screen.selections = {bodies[1]: SELECT_ALL}
            await pilot.press("ctrl+c", "ctrl+c")
            assert app.clipboard == "newest selection"
            assert clipboard.paste_text_from_clipboards(app) == "newest selection"
            assert len(native_threads) == 1
            assert not native_copies
            assert not terminal_copies

            input_area = screen.query_one("#chat-input", EnhancedTextArea)
            input_area.focus()
            await pilot.press("ctrl+v")
            assert input_area.text == "newest selection"
            assert not native_copies

            release.set()
            await clipboard._native_writers[app].wait()
            assert native_copies == ["first selection", "newest selection"]
            assert clipboard.paste_text_from_clipboards(app) == "external text"
    finally:
        release.set()
        await clipboard.close_clipboard(app)


async def test_windows_native_copy_preserves_large_unicode_payload_without_osc52(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    copied: list[str] = []
    terminal_copies: list[str] = []
    monkeypatch.setattr(clipboard, "clipboard_copy", copied.append)
    app = App()
    monkeypatch.setattr(app, "copy_to_clipboard", terminal_copies.append)
    text = "中文😀\n" * 20000
    try:
        assert clipboard.copy_text_to_clipboards(app, text, max_terminal_bytes=clipboard.OSC52_COPY_MAX_BYTES)
        await clipboard._native_writers[app].wait()
        assert app.clipboard == text
        assert copied == [text]
        assert terminal_copies == []
    finally:
        await clipboard.close_clipboard(app)


@pytest.mark.parametrize("editor_kind", ["chat", "expanded"])
async def test_pending_text_copy_takes_precedence_over_old_clipboard_image(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, editor_kind: str
) -> None:
    entered, release = Event(), Event()
    image_reads: list[str] = []
    bitmap = Image.new("RGB", (2, 2), "red")

    def write(text: str) -> None:
        entered.set()
        assert release.wait(10)

    def grab_image():
        image_reads.append("image")
        return bitmap

    monkeypatch.setattr(clipboard, "clipboard_copy", write)
    monkeypatch.setattr(image_paste, "_grab_clipboard", grab_image)
    monkeypatch.setattr(image_paste, "get_platform", clipboard.platform_helpers.get_platform)
    app = make_chrys_app(tmp_path)
    image_dir = tmp_path / "clipboard-images"
    try:
        async with app.run_test(size=(110, 35)) as pilot:
            await wait_for(
                lambda: isinstance(app.screen, MainScreen) and app.screen.is_mounted,
                timeout=MAIN_SCREEN_BUILD_TIMEOUT,
                pilot=pilot,
            )
            if editor_kind == "chat":
                editor = app.screen.query_one("#chat-input", _ChatTextArea)
                editor.set_clipboard_image_dir(image_dir)
            else:
                editor = DraftEditor(
                    ChatDraft(""),
                    paste_cwd=str(tmp_path),
                    clipboard_image_dir=image_dir,
                    mode=EditorMode.STANDARD,
                )
                await app.screen.mount(editor)
            editor.focus()
            assert clipboard.copy_text_to_clipboards(app, "latest copied text")
            await wait_for(entered.is_set)
            await pilot.press("ctrl+v")
            assert editor.text == "latest copied text"
            assert image_reads == []
            assert not image_dir.exists()

            release.set()
            await clipboard.wait_for_clipboard(app)
            # A subsequent external image copy must still use the image path.
            editor.clear()
            await pilot.press("ctrl+v")
            assert image_reads == ["image"]
            assert list(image_dir.glob("*.png"))
            if editor_kind == "chat":
                assert editor.text == "[Image #1] "
            else:
                assert ".png" in editor.text
    finally:
        release.set()
        await clipboard.close_clipboard(app)
        bitmap.close()


@pytest.mark.parametrize("variable", ["SSH_CONNECTION", "SSH_CLIENT", "SSH_TTY", "WSL_DISTRO_NAME", "WSL_INTEROP"])
@pytest.mark.parametrize("delivery", ["terminal", "too-large", "terminal-error"])
async def test_remote_windows_preserves_terminal_copy_without_touching_host_clipboard(
    monkeypatch: pytest.MonkeyPatch, variable: str, delivery: str
) -> None:
    monkeypatch.setenv(variable, "remote")
    copied: list[str] = []
    terminal_copies: list[str] = []

    def terminal_copy(text: str) -> None:
        terminal_copies.append(text)
        if delivery == "terminal-error":
            raise RuntimeError("terminal unavailable")

    # Keep the real adapter: patching clipboard_copy would hide its Win32 fallback.
    monkeypatch.setattr(clipboard.platform_helpers, "clipboard_copy", copied.append)
    app = App()
    monkeypatch.setattr(app, "copy_to_clipboard", terminal_copy)
    assert clipboard.copy_text_to_clipboards(
        app, "remote text", max_terminal_bytes=1 if delivery == "too-large" else None
    ) is (delivery == "terminal")
    assert terminal_copies == ([] if delivery == "too-large" else ["remote text"])
    assert copied == []
    assert app not in clipboard._native_writers


def test_windows_backend_stays_isolated_if_terminal_markers_change(monkeypatch: pytest.MonkeyPatch) -> None:
    isolated: list[str] = []
    synchronous: list[str] = []
    monkeypatch.setattr(clipboard, "copy_windows_text", isolated.append)
    monkeypatch.setattr(clipboard.platform_helpers, "clipboard_copy", synchronous.append)
    monkeypatch.setenv("SSH_CONNECTION", "changed after dispatch")

    # This is the backend already captured by an accepted native writer.
    clipboard.clipboard_copy("accepted selection")

    assert isolated == ["accepted selection"]
    assert synchronous == []


@pytest.mark.parametrize("editor_kind", ["input", "chat", "expanded"])
@pytest.mark.parametrize("paste_key", ["ctrl+v", "shift+insert"])
async def test_remote_windows_copy_paste_never_reads_host_text_or_bitmap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, editor_kind: str, paste_key: str
) -> None:
    monkeypatch.setenv("SSH_CONNECTION", "remote")
    reads: list[str] = []

    def read_text() -> str:
        reads.append("text")
        return "unrelated host text"

    def read_image():
        reads.append("image")
        return

    monkeypatch.setattr(clipboard.platform_helpers, "clipboard_paste", read_text)
    monkeypatch.setattr(image_paste, "_grab_clipboard", read_image)
    monkeypatch.setattr(image_paste, "get_platform", clipboard.platform_helpers.get_platform)
    image_dir = tmp_path / "clipboard-images"
    app = App()
    async with app.run_test() as pilot:
        if editor_kind == "input":
            editor = EnhancedInput()
        elif editor_kind == "chat":
            editor = _ChatTextArea(capture_submit=lambda: pytest.fail("paste must not submit"))
            editor.set_clipboard_image_dir(image_dir)
        else:
            editor = DraftEditor(
                ChatDraft(""), paste_cwd=str(tmp_path), clipboard_image_dir=image_dir, mode=EditorMode.STANDARD
            )
        await app.screen.mount(editor)
        editor.focus()
        await wait_for(lambda: editor.has_focus, pilot=pilot)
        assert clipboard.copy_text_to_clipboards(app, "fresh client selection")
        await pilot.press(paste_key)
        assert (editor.value if isinstance(editor, EnhancedInput) else editor.text) == "fresh client selection"
        assert reads == []
        assert not image_dir.exists()


@pytest.mark.parametrize("failure", ["too-large", "terminal-error"])
async def test_remote_windows_copy_failure_does_not_report_success(
    monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    monkeypatch.setenv("SSH_CONNECTION", "remote")
    notices: list[tuple[str, str, str]] = []

    def notify(message, *, title="", severity="information", timeout=None, markup=True) -> None:
        notices.append((str(message), title, severity))

    def reject(text: str) -> None:
        raise RuntimeError("terminal unavailable")

    app = App()
    monkeypatch.setattr(app, "notify", notify)
    async with app.run_test():
        app._clipboard = "previous selection"
        if failure == "terminal-error":
            monkeypatch.setattr(app, "copy_to_clipboard", reject)
        tool_view.copy_and_notify(
            app.screen,
            "x" * (clipboard.OSC52_COPY_MAX_BYTES + 1) if failure == "too-large" else "selection",
            copied=tool_view.COPIED_OUTPUT,
            too_large=tool_view.COPIED_OUTPUT_TOO_LARGE,
            unavailable=tool_view.COPIED_OUTPUT_UNAVAILABLE,
        )
        assert app.clipboard == "previous selection"
        assert notices == [("Could not copy to the system clipboard. Please try again.", "", "error")]


async def test_stock_textual_copy_uses_the_same_native_queue(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    copied: list[str] = []
    terminal_copies: list[str] = []
    monkeypatch.setattr(clipboard, "clipboard_copy", copied.append)

    def terminal_copy(self: App, text: str) -> None:
        terminal_copies.append(text)

    monkeypatch.setattr(App, "copy_to_clipboard", terminal_copy)
    app = make_chrys_app(tmp_path)
    clipboard.stop_clipboard(app)
    async with app.run_test() as pilot:
        await wait_for(
            lambda: isinstance(app.screen, MainScreen) and app.screen.is_mounted,
            timeout=MAIN_SCREEN_BUILD_TIMEOUT,
            pilot=pilot,
        )
        stock_input = Input("stock input")
        await app.screen.mount(stock_input)
        stock_input.focus()
        stock_input.select_all()
        await pilot.press("ctrl+c")
        await clipboard._native_writers[app].wait()
        assert copied == ["stock input"]
        assert terminal_copies == []
        assert app.clipboard == "stock input"


async def test_failed_thread_start_does_not_replace_clipboard_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    app = App()
    app._clipboard = "previous"

    def fail_start(self) -> None:
        raise RuntimeError("thread unavailable")

    monkeypatch.setattr(clipboard_writer.Thread, "start", fail_start)
    monkeypatch.setattr(clipboard.platform_helpers, "clipboard_paste", lambda: "")
    try:
        assert not clipboard.copy_text_to_clipboards(app, "rejected")
        assert app.clipboard == "previous"
        assert clipboard.paste_text_from_clipboards(app) == "previous"
    finally:
        await clipboard.close_clipboard(app)


@pytest.mark.parametrize("editor_type", [EnhancedInput, EnhancedTextArea])
@pytest.mark.parametrize("rejection", ["closed", "thread-start"])
async def test_rejected_native_cut_preserves_the_editor_selection(
    monkeypatch: pytest.MonkeyPatch, editor_type: type[EnhancedInput | EnhancedTextArea], rejection: str
) -> None:
    app = App()
    async with app.run_test():
        editor = editor_type("keep selected text")
        await app.screen.mount(editor)
        editor.select_all()
        app._clipboard = "previous"
        if rejection == "closed":
            clipboard.stop_clipboard(app)
        else:

            def fail_start(self) -> None:
                raise RuntimeError("thread unavailable")

            monkeypatch.setattr(clipboard_writer.Thread, "start", fail_start)
        editor.action_cut()
        assert editor.selected_text == "keep selected text"
        assert app.clipboard == "previous"
    await clipboard.close_clipboard(app)


@pytest.mark.parametrize("outcome", ["retry", "failure", "superseded"])
async def test_chat_copy_feedback_waits_for_the_current_native_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, outcome: str
) -> None:
    entered, release = Event(), Event()
    attempts: list[str] = []
    copied: list[str] = []
    notices: list[tuple[str, str, str]] = []

    def write(text: str) -> None:
        attempts.append(text)
        if len(attempts) == 1:
            entered.set()
            assert release.wait(10)
            raise PermissionError("clipboard busy")
        copied.append(text)

    def notify(message, *, title="", severity="information", timeout=None, markup=True) -> None:
        if title == "Copied" or "system clipboard" in str(message):
            notices.append((str(message), title, severity))

    monkeypatch.setattr(clipboard, "clipboard_copy", write)
    if outcome == "failure":
        monkeypatch.setattr(clipboard_writer, "_COPY_RETRY_SECONDS", 0)
    app = make_chrys_app(tmp_path)
    monkeypatch.setattr(app, "notify", notify)
    try:
        async with app.run_test(size=(110, 35)) as pilot:
            await wait_for(
                lambda: isinstance(app.screen, MainScreen) and app.screen.is_mounted,
                timeout=MAIN_SCREEN_BUILD_TIMEOUT,
                pilot=pilot,
            )
            panel = app.screen.query_one(ChatPanel)
            await panel.add_user_message("first")
            await panel.add_user_message("latest")
            messages = list(panel.query(UserMessage))
            messages[0].copy_user_message()
            await wait_for(entered.is_set)
            assert notices == []
            if outcome == "superseded":
                messages[1].copy_user_message()
            release.set()
            await clipboard.wait_for_clipboard(app)
            await wait_for(lambda: bool(notices), pilot=pilot)
            if outcome == "failure":
                assert copied == []
                assert notices == [("Could not copy to the system clipboard. Please try again.", "", "error")]
            else:
                assert copied == ["latest" if outcome == "superseded" else "first"]
                assert len(notices) == 1
                assert notices[0][1:] == ("Copied", "information")
    finally:
        release.set()
        await clipboard.close_clipboard(app)


async def test_copy_success_feedback_survives_collection_of_its_source_widget(monkeypatch: pytest.MonkeyPatch) -> None:
    entered, release = Event(), Event()
    notified = asyncio.Event()
    ui_thread = get_ident()
    notices: list[tuple[str, str]] = []

    def write(text: str) -> None:
        assert get_ident() != ui_thread
        entered.set()
        assert release.wait(5)

    def notify(message: str, *, title="", timeout=None, markup=True) -> None:
        assert get_ident() == ui_thread
        notices.append((message, title))
        notified.set()

    def register_feedback():
        # A released modal/widget must not be retained just to deliver its toast.
        source = Widget()
        clipboard.notify_after_copy(source, "Selection copied", dispatched=True, title="Copied", markup=False)
        return ref(source)

    monkeypatch.setattr(clipboard, "clipboard_copy", write)
    app = App()
    monkeypatch.setattr(app, "notify", notify)
    try:
        async with app.run_test():
            assert clipboard.copy_text_to_clipboards(app, "selection")
            await wait_for(entered.is_set)
            source_ref = register_feedback()
            gc.collect()
            assert source_ref() is None
            assert notices == []
            release.set()
            # No pilot input or writer future wakes the loop: Textual's
            # thread-safe Callback message must deliver the notice itself.
            await asyncio.wait_for(notified.wait(), timeout=ENGINE_TURN_TIMEOUT)
            await clipboard.wait_for_clipboard(app)
            assert notices == [("Selection copied", "Copied")]
    finally:
        release.set()
        await clipboard.close_clipboard(app)


async def test_explicit_reopen_accepts_copy_while_previous_run_is_still_draining(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entered, release = Event(), Event()
    copied: list[str] = []

    def write(text: str) -> None:
        if text == "old active":
            entered.set()
            assert release.wait(5)
        copied.append(text)

    monkeypatch.setattr(clipboard, "clipboard_copy", write)
    monkeypatch.setattr(clipboard_writer, "_CLOSE_TIMEOUT_SECONDS", 0)
    app = App()
    old_writer = None
    try:
        assert clipboard.copy_text_to_clipboards(app, "old active")
        await wait_for(entered.is_set)
        old_writer = clipboard._native_writers[app]
        assert clipboard.copy_text_to_clipboards(app, "old latest")
        old_failure = clipboard.ClipboardWriteFailed(old_writer, old_writer.sequence)
        await clipboard.close_clipboard(app)
        clipboard.open_clipboard(app)
        assert not clipboard.clipboard_failure_is_current(app, old_failure)
        assert clipboard.copy_text_to_clipboards(app, "reopened")
        assert clipboard._native_writers[app] is not old_writer
        assert not clipboard.clipboard_failure_is_current(app, old_failure)
        assert clipboard.paste_text_from_clipboards(app) == "reopened"
        assert copied == []
        release.set()
        await clipboard.wait_for_clipboard(app)
        assert copied == ["old active", "old latest", "reopened"]
    finally:
        release.set()
        if old_writer is not None:
            await old_writer.wait()
        await clipboard.wait_for_clipboard(app)
        await clipboard.close_clipboard(app)


@pytest.mark.parametrize("copied_before_close", [False, True])
async def test_copy_after_close_requires_an_explicit_new_run(
    monkeypatch: pytest.MonkeyPatch, copied_before_close: bool
) -> None:
    copied: list[str] = []
    monkeypatch.setattr(clipboard, "clipboard_copy", copied.append)
    app = App()
    try:
        if copied_before_close:
            assert clipboard.copy_text_to_clipboards(app, "first run")
        await clipboard.close_clipboard(app)
        assert app not in clipboard._native_writers
        previous = app.clipboard
        assert not clipboard.copy_text_to_clipboards(app, "late copy")
        assert app.clipboard == previous
        assert app not in clipboard._native_writers
        clipboard.open_clipboard(app)
        assert clipboard.copy_text_to_clipboards(app, "next run")
        await clipboard.close_clipboard(app)
        assert copied == (["first run"] if copied_before_close else []) + ["next run"]
    finally:
        await clipboard.close_clipboard(app)


async def test_timed_out_close_retains_one_writer_until_native_copy_finishes(monkeypatch: pytest.MonkeyPatch) -> None:
    entered, release = Event(), Event()
    copied: list[str] = []

    def write(text: str) -> None:
        entered.set()
        assert release.wait(5)
        copied.append(text)

    monkeypatch.setattr(clipboard, "clipboard_copy", write)
    monkeypatch.setattr(clipboard_writer, "_CLOSE_TIMEOUT_SECONDS", 0)
    app = App()
    try:
        assert clipboard.copy_text_to_clipboards(app, "active")
        await wait_for(entered.is_set)
        assert clipboard.copy_text_to_clipboards(app, "latest")
        writer = clipboard._native_writers[app]
        await clipboard.close_clipboard(app)
        assert clipboard._native_writers[app] is writer
        assert not clipboard.copy_text_to_clipboards(app, "rejected while closing")
        assert app.clipboard == "latest"
        assert clipboard.paste_text_from_clipboards(app) == "latest"
        release.set()
        await clipboard.wait_for_clipboard(app)
        assert copied == ["active", "latest"]
        assert not clipboard.copy_text_to_clipboards(app, "late copy after drain")
        clipboard.open_clipboard(app)
        assert clipboard.copy_text_to_clipboards(app, "after drain")
        await clipboard.wait_for_clipboard(app)
        assert copied == ["active", "latest", "after drain"]
    finally:
        release.set()
        await clipboard.wait_for_clipboard(app)
        await clipboard.close_clipboard(app)


async def test_new_app_cannot_overlap_a_timed_out_copy_after_old_app_is_collected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entered, release, attempting, second_entered = Event(), Event(), Event(), Event()
    copied: list[str] = []

    class _ObservedFuture(Future):
        def result(self, timeout=None):
            if not self.done():
                attempting.set()
            return super().result(timeout)

    class _Host:
        _clipboard = ""

        @property
        def clipboard(self) -> str:
            return self._clipboard

        def copy_to_clipboard(self, text: str) -> None:
            raise AssertionError("native Windows must not dispatch OSC52")

    def write(text: str) -> None:
        if text == "old active":
            entered.set()
            assert release.wait(5)
        if text == "new app":
            second_entered.set()
        copied.append(text)

    monkeypatch.setattr(clipboard_writer, "Future", _ObservedFuture)
    monkeypatch.setattr(clipboard_writer, "_CLOSE_TIMEOUT_SECONDS", 0)
    monkeypatch.setattr(clipboard, "clipboard_copy", write)
    old_app, new_app = _Host(), _Host()
    old_writer = None
    try:
        assert clipboard.copy_text_to_clipboards(old_app, "old active")
        old_writer = clipboard._native_writers[old_app]
        await wait_for(entered.is_set)
        assert clipboard.copy_text_to_clipboards(old_app, "old latest")
        await clipboard.close_clipboard(old_app)
        old_ref = ref(old_app)
        del old_app
        assert old_ref() is None
        assert old_writer.busy

        attempting.clear()
        assert clipboard.copy_text_to_clipboards(new_app, "new app")
        # Both the ordered batch path and an incorrectly overlapping write have
        # a readiness signal, so an old implementation fails without timing out.
        await wait_for(lambda: attempting.is_set() or second_entered.is_set())
        assert not second_entered.is_set()
        release.set()
        await old_writer.wait()
        await clipboard.wait_for_clipboard(new_app)
        assert copied == ["old active", "old latest", "new app"]
    finally:
        release.set()
        if old_writer is not None:
            await old_writer.wait()
            await old_writer.close()
        await clipboard.wait_for_clipboard(new_app)
        await clipboard.close_clipboard(new_app)


async def test_real_apps_are_collected_while_native_workers_are_blocked_or_queued(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entered, release = Event(), Event()
    copied: list[str] = []
    writers: list[clipboard_writer.ClipboardWriter] = []

    def write(text: str) -> None:
        if text == "blocked":
            entered.set()
            assert release.wait(10)
        copied.append(text)

    async def run_app(text: str | None):
        app = App()
        async with app.run_test():
            if text is not None:
                assert clipboard.copy_text_to_clipboards(app, text)
                writers.append(clipboard._native_writers[app])
                clipboard.notify_after_copy(app.screen, "Copied", dispatched=True)
                if text == "blocked":
                    await wait_for(entered.is_set)
            await clipboard.close_clipboard(app)
        return ref(app)

    monkeypatch.setattr(clipboard, "clipboard_copy", write)
    monkeypatch.setattr(clipboard_writer, "_CLOSE_TIMEOUT_SECONDS", 0)
    try:
        control = await run_app(None)
        blocked = await run_app("blocked")
        queued = await run_app("queued")
        gc.collect()
        assert control() is None
        assert all(writer.closed and writer.busy for writer in writers)
        assert blocked() is None
        assert queued() is None
        assert copied == []
        release.set()
        await writers[-1].wait()
        assert copied == ["blocked", "queued"]
    finally:
        release.set()
        for writer in writers:
            await writer.wait()
            await writer.close()


class _SlottedClipboardHost:
    __slots__ = ("_clipboard",)

    def __init__(self) -> None:
        self._clipboard = "previous"

    @property
    def clipboard(self) -> str:
        return self._clipboard

    def copy_to_clipboard(self, text: str) -> None:
        raise AssertionError("native Windows must not fall back to OSC52")


class _UnhashableClipboardHost(_SlottedClipboardHost):
    __hash__ = None


@pytest.mark.parametrize("host_type", [_SlottedClipboardHost, _UnhashableClipboardHost, SimpleNamespace])
async def test_protocol_hosts_without_weak_keys_degrade_without_crashing(
    host_type: type, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = host_type()
    if isinstance(app, SimpleNamespace):
        app._clipboard = app.clipboard = "previous"
    copied: list[str] = []
    monkeypatch.setattr(clipboard, "clipboard_copy", copied.append)
    monkeypatch.setattr(clipboard.platform_helpers, "clipboard_paste", lambda: "external")
    assert not clipboard.copy_text_to_clipboards(app, "new")
    assert app.clipboard == "previous"
    assert clipboard.paste_text_from_clipboards(app) == "external"
    await clipboard.wait_for_clipboard(app)
    await clipboard.close_clipboard(app)
    assert copied == []
