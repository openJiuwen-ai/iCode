# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""One display writer for entries with independent content and process visibility."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from textual.message import Message
from textual.widget import Widget

if TYPE_CHECKING:
    from collections.abc import Callable

    from rich.text import Text

    from chrys.app.tui.widgets.loading import RunningTitle

type LiveStatus = Callable[[RunningTitle, int, bool], Text]


@dataclass
class LiveStatusChanged(Message):
    """A member's latest activity may have changed independently of its process tally."""

    member: ProcessMember

    def can_replace(self, message: Message) -> bool:
        return isinstance(message, LiveStatusChanged) and self.member is message.member


class ProcessMember(Widget):
    """A process may hide an entry without overwriting its own visibility."""

    _process_hidden: bool = False
    _content_visible: bool = True
    _reports_live_status: bool = False

    @property
    def process_hidden(self) -> bool:
        return self._process_hidden

    def set_process_hidden(self, hidden: bool) -> None:
        self._process_hidden = hidden
        self._sync_process_display()

    def set_content_visible(self, visible: bool) -> None:
        self._content_visible = visible
        self._sync_process_display()

    def set_live_status_reporting(self, enabled: bool) -> None:
        """Report activity to the temporary row while it tracks this member."""
        self._reports_live_status = enabled

    def _notify_live_status(self) -> None:
        if self._reports_live_status and self.is_attached:
            self.post_message(LiveStatusChanged(self))

    def _sync_process_display(self) -> None:
        visible = self._content_visible and not self._process_hidden
        if self.display != visible:
            self.display = visible
