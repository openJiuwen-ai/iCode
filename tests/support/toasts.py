# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Every toast an App has shown, including those that have timed out since.

``App._notifications`` forgets a toast once its timeout has run out on the wall clock, and a
slash-command warning lasts only 3 s. A loaded runner stalls longer than that (a gen2 collection,
the whole process descheduled), so a test polling that set can miss a toast the App did show.
The TUI tests' autouse fixture calls ``record_shown_toasts``, which logs each toast as the App
takes it in; ``shown_toasts`` reads that log.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from weakref import WeakKeyDictionary

from textual.notifications import Notifications

if TYPE_CHECKING:
    import pytest
    from textual.app import App
    from textual.notifications import Notification

_SHOWN: WeakKeyDictionary[Notifications, dict[str, Notification]] = WeakKeyDictionary()
"""Each App's toasts by identity, keyed by the App's toast collection."""


def record_shown_toasts(monkeypatch: pytest.MonkeyPatch) -> None:
    """Log every toast any App takes in, for as long as *monkeypatch* is active."""
    add = Notifications.add

    def _add(self: Notifications, notification: Notification) -> Notifications:
        _SHOWN.setdefault(self, {})[notification.identity] = notification
        return add(self, notification)

    monkeypatch.setattr(Notifications, "add", _add)


def shown_toasts(app: App) -> list[Notification]:
    """Every toast *app* has shown while recording, oldest first, timed out or cleared since or not."""
    return list(_SHOWN.get(app._notifications, {}).values())
