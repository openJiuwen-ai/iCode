# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Toasts under the header, the newest on top.

Textual appends each new toast under the older ones and scrolls its rack to
the end, which stacks a bottom-docked rack upward from the newest toast.
Chrys docks the rack under the header instead (``chrys.tcss``), so the stack
grows downward: new toasts mount ahead of the older ones and the rack keeps
its start in view.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from textual.app import ScreenStackError
from textual.css.query import NoMatches
from textual.widgets._toast import Toast, ToastHolder, ToastRack

if TYPE_CHECKING:
    from textual.app import App
    from textual.notifications import Notification, Notifications

TEXTUAL_TOAST_RACK_FORK_VERSION = "8.2.7"
"""The Textual release whose private ``App._refresh_notifications`` and ``ToastRack.show`` this mirrors."""


def refresh_newest_first(app: App[object]) -> None:
    """``App._refresh_notifications`` showing the current screen's rack newest first."""
    try:
        screen = app.screen
    except ScreenStackError:
        return
    try:
        rack = screen.get_child_by_type(ToastRack)
    except NoMatches:
        return
    app.call_later(show_newest_first, rack, app._notifications)


def show_newest_first(rack: ToastRack, notifications: Notifications) -> None:
    """``ToastRack.show`` with the newest toast on top.

    The only intentional delta from Textual's version: new toasts mount ahead
    of the toasts already racked, and the rack scrolls to its start.
    """
    rack.display = bool(notifications)
    for toast in rack.query(Toast):
        if toast._notification not in notifications:
            toast.remove()

    new_toasts: list[Notification] = []
    for notification in notifications:
        try:
            rack.get_child_by_id(rack._toast_id(notification))
        except NoMatches:
            if not notification.has_expired:
                new_toasts.append(notification)

    if new_toasts:
        # Notifications iterate oldest first; the newest goes in first so it lands on top.
        rack.mount_all(
            [
                ToastHolder(Toast(notification), id=rack._toast_id(notification))
                for notification in reversed(new_toasts)
            ],
            before=0,
        )
        rack.call_later(rack.scroll_home, animate=False, force=True)


def dismiss_toast(app: App[object], notification: Notification) -> None:
    """Take *notification* down before its timeout; a no-op once it is gone.

    Uses ``App._unnotify``, private in Textual :data:`TEXTUAL_TOAST_RACK_FORK_VERSION`.
    """
    app._unnotify(notification)
