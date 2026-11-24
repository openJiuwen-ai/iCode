# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Clipboard helpers for TUI copy actions."""

from __future__ import annotations

import logging
import os
from contextlib import suppress
from typing import TYPE_CHECKING, Literal, Protocol
from weakref import WeakKeyDictionary, WeakSet, ref

from textual.message import Message

from chrys.app.tui.copy_messages import COPY_FAILED
from chrys.app.tui.i18n import render_str, widget_localizer
from chrys.app.tui.support.clipboard_process import copy_windows_text
from chrys.app.tui.support.clipboard_writer import ClipboardWriter
from chrys.app.tui.terminal.launcher import is_remote_ssh_session
from chrys.foundation import platform as platform_helpers

if TYPE_CHECKING:
    from textual.widget import Widget

logger = logging.getLogger(__name__)

BROWSER_CLIPBOARD_META_TYPE = "chrys_browser_clipboard_copy"
_TEXTUAL_WEB_DRIVER = "textual.drivers.web_driver:WebDriver"

OSC52_COPY_MAX_BYTES = 64 * 1024
"""Terminal-clipboard payload cap; larger copies skip OSC52 to avoid truncation."""


class ClipboardReadApp(Protocol):
    """Textual's clipboard value and thread-safe result message port."""

    @property
    def clipboard(self) -> str: ...

    def post_message(self, message: Message) -> bool: ...


class ClipboardApp(ClipboardReadApp, Protocol):
    """Textual's clipboard cache and terminal writer, kept separately."""

    _clipboard: str

    def copy_to_clipboard(self, text: str) -> None: ...


_native_writers: WeakKeyDictionary[ClipboardReadApp, ClipboardWriter] = WeakKeyDictionary()
_closing_apps: WeakSet[ClipboardReadApp] = WeakSet()


class ClipboardWriteFailed(Message):
    """A native write failed after its bounded retries."""

    def __init__(self, writer: ClipboardWriter, sequence: int) -> None:
        super().__init__()
        self.writer = writer
        self.sequence = sequence


def clipboard_failure_is_current(app: ClipboardReadApp, event: ClipboardWriteFailed) -> bool:
    """Ignore results superseded by another copy, app run, or teardown."""
    with suppress(TypeError):
        return (
            app not in _closing_apps
            and _native_writer(app) is event.writer
            and not event.writer.closed
            and event.writer.is_current(event.sequence)
        )
    return False


def _new_native_writer(app: ClipboardReadApp) -> ClipboardWriter:
    app_ref = ref(app)

    def failed(sequence: int) -> None:
        if owner := app_ref():
            owner.post_message(ClipboardWriteFailed(writer, sequence))

    writer = ClipboardWriter(clipboard_copy, on_failure=failed)
    return writer


def open_clipboard(app: ClipboardReadApp) -> None:
    """Allow copies for an explicitly started app run."""
    # Protocol hosts need not support weak references or hashing.
    with suppress(TypeError):
        _closing_apps.discard(app)


def stop_clipboard(app: ClipboardReadApp) -> None:
    """Reject new native copies as soon as teardown begins, even before the first copy."""
    # These hosts cannot dispatch native copies either.
    with suppress(TypeError):
        _closing_apps.add(app)


def _clipboard_frontend() -> Literal["browser", "native-windows", "terminal"]:
    if os.environ.get("TEXTUAL_DRIVER") == _TEXTUAL_WEB_DRIVER:
        return "browser"
    # WSL processes share the terminal's clipboard route, but WSL is not SSH
    # and does not imply that launching a local desktop window is unavailable.
    if (
        platform_helpers.get_platform().is_windows
        and not is_remote_ssh_session()
        and not any(os.environ.get(name) for name in ("WSL_DISTRO_NAME", "WSL_INTEROP"))
    ):
        return "native-windows"
    return "terminal"


def uses_native_windows_clipboard() -> bool:
    """Whether this frontend's system clipboard belongs to the Windows host."""
    return _clipboard_frontend() == "native-windows"


def uses_host_clipboard() -> bool:
    """Whether this frontend can use the host's text and bitmap clipboard."""
    frontend = _clipboard_frontend()
    return frontend == "native-windows" or (frontend == "terminal" and not platform_helpers.get_platform().is_windows)


def _native_writer(app: ClipboardReadApp, *, create: bool = False) -> ClipboardWriter | None:
    """Find a queue without requiring protocol hosts to support weak references."""
    try:
        if create and app in _closing_apps:
            return None
        writer = _native_writers.get(app)
        if create and (writer is None or writer.closed):
            # An explicit reopen may have an older native call still running.
            # Its worker owns it; the process batch chain orders this queue after it.
            writer = _native_writers[app] = _new_native_writer(app)
        return writer
    except TypeError:
        # Plain protocol hosts may be unhashable or lack weak-reference slots.
        # Reject native dispatch rather than block the UI with a sync fallback.
        return None


async def close_clipboard(app: ClipboardReadApp) -> None:
    """Drain this app's native clipboard queue within the exit budget."""
    stop_clipboard(app)
    if writer := _native_writer(app):
        try:
            await writer.close()
        finally:
            # Retain this app's unfinished queue for wait/paste and an explicit
            # reopen. ClipboardWriter serializes native batches across apps.
            if not writer.busy and _native_writer(app) is writer:
                del _native_writers[app]


async def wait_for_clipboard(app: ClipboardReadApp) -> None:
    """Settle native writes already submitted by this app."""
    if writer := _native_writer(app):
        await writer.wait()


def clipboard_copy(text: str) -> None:
    """Let native failures reach the writer; other routes guard their fallback."""
    # Frontend routing happens at dispatch. An accepted Windows batch must
    # keep its isolated backend even if terminal environment markers change.
    if platform_helpers.get_platform().is_windows:
        copy_windows_text(text)
    else:
        platform_helpers.clipboard_copy(text)


def notify_after_copy(
    widget: Widget,
    message: str,
    *,
    dispatched: bool,
    title: str = "",
    timeout: float | None = None,
    markup: bool = True,
) -> None:
    """Report client dispatch failures and wait for native write success."""
    if not uses_native_windows_clipboard():
        if not dispatched and not uses_host_clipboard():
            widget.notify(render_str(widget_localizer(widget), COPY_FAILED.bind()), severity="error", markup=False)
        else:
            widget.notify(message, title=title, timeout=timeout, markup=markup)
        return
    app = widget.app
    writer = _native_writer(app)
    if writer is None or app in _closing_apps:
        return
    sequence = writer.sequence
    widget_ref, app_ref = ref(widget), ref(app)

    def show() -> None:
        target, owner = widget_ref(), app_ref()
        if (
            owner is not None
            and owner.is_running
            and owner not in _closing_apps
            and _native_writer(owner) is writer
            and not writer.closed
            and writer.is_current(sequence)
        ):
            # A dismissed modal may have been collected before the copy landed.
            recipient = target if target is not None else owner
            recipient.notify(message, title=title, timeout=timeout, markup=markup)

    def schedule() -> None:
        if owner := app_ref():
            # Textual call_later posts a Callback message; post_message uses
            # call_soon_threadsafe when invoked from this writer thread.
            owner.call_later(show)

    if writer.when_copied(schedule):
        show()


def _clipboard_safe_text(text: str) -> str:
    """Return text that can be encoded for terminal and OS clipboard paths."""
    return text.encode("utf-8", errors="backslashreplace").decode("utf-8")


def terminal_clipboard_size(text: str) -> int:
    """Return the UTF-8 byte size of text after clipboard sanitization."""
    return len(_clipboard_safe_text(text).encode("utf-8"))


def native_clipboard_write_pending(app: ClipboardReadApp) -> bool:
    """Whether reading the OS clipboard could return content before our latest copy."""
    writer = _native_writer(app)
    return writer is not None and writer.busy


def paste_text_from_clipboards(app: ClipboardReadApp) -> str:
    """Read the freshest paste text without crossing browser sessions.

    Native TUI sessions prefer the host OS clipboard because Textual's local
    cache does not observe copies made outside Chrys. Browser sessions and
    remote Windows terminals must not read the server's clipboard: it belongs
    to the host process, not necessarily the client or even the same session.
    """
    if not uses_host_clipboard():
        return app.clipboard
    if native_clipboard_write_pending(app):
        # Copy/cut followed immediately by paste must not read the previous
        # OS value, or contend with our own in-flight clipboard write.
        return app.clipboard
    try:
        text = platform_helpers.clipboard_paste()
    except Exception:
        logger.debug("Host OS clipboard paste failed", exc_info=True)
        text = ""
    return text or app.clipboard


def _copy_text_to_browser_clipboard(app: ClipboardApp, text: str) -> bool:
    """Ask the textual-serve browser frontend to copy *text* to its clipboard."""
    driver = getattr(app, "_driver", None)
    write_meta = getattr(driver, "write_meta", None)
    if not callable(write_meta):
        return False
    try:
        write_meta({"type": BROWSER_CLIPBOARD_META_TYPE, "text": text})
    except Exception:
        logger.debug("Browser clipboard copy dispatch failed", exc_info=True)
        return False
    return True


def copy_text_to_clipboards(app: ClipboardApp, text: str, *, max_terminal_bytes: int | None = None) -> bool:
    """Dispatch a best-effort copy to the clipboard belonging to this frontend.

    Native Windows writes only through Win32: sending OSC52 as well lets
    Windows Terminal compete for the same clipboard and deadlock against
    EmptyClipboard's notification to its previous owner. Browser sessions and
    remote Windows terminals write only to their client's clipboard. Other
    terminal frontends retain their host OS fallback.
    True means dispatched to the native Windows or terminal copy path; it
    is not an acknowledgement from the OS or terminal.
    """
    text = _clipboard_safe_text(text)
    frontend = _clipboard_frontend()
    if frontend == "native-windows":
        writer = _native_writer(app, create=True)
        if writer is None or not writer.submit(text):
            return False
        app._clipboard = text
        return True
    terminal_copied = max_terminal_bytes is None or len(text.encode("utf-8")) <= max_terminal_bytes
    if terminal_copied:
        try:
            app.copy_to_clipboard(text)
        except Exception:
            logger.debug("Terminal clipboard copy failed", exc_info=True)
            terminal_copied = False
    if frontend == "browser":
        browser_copied = _copy_text_to_browser_clipboard(app, text)
        if browser_copied:
            app._clipboard = text
        return terminal_copied or browser_copied
    if platform_helpers.get_platform().is_windows:
        # This is a remote terminal: the server's Win32 clipboard is not the
        # client's, and touching it here would block the UI outside our queue.
        return terminal_copied
    try:
        clipboard_copy(text)
    except Exception:
        logger.debug("Host OS clipboard copy failed", exc_info=True)
    return terminal_copied
