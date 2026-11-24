# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""The chat transcript panel as the widgets in it see it, without importing it."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from textual.widget import Widget


class ChatTranscriptPanelMarker:
    """Nominal marker base identifying the chat transcript panel.

    Chat helper modules must not import :mod:`chrys.app.tui.widgets.chat.panel`
    (see ``tests/architecture/test_tui_structure.py``). Helpers that need to
    recognize the transcript panel among a widget's ancestors — e.g. the
    selection controller's hot per-mouse-move filter — check this base with a
    plain ``isinstance`` instead of importing the concrete ``ChatPanel``.
    """

    __slots__ = ()

    def entry_folded(self) -> None:
        """An entry folded, or gave rows up: show the transcript at its new height in the layout that places it."""
        raise NotImplementedError

    def process_guide_for(self, member: Widget) -> Widget | None:
        """Return the open process whose guide can fold from beside this member."""
        raise NotImplementedError


def note_fold(widget: Widget) -> None:
    """Tell the chat transcript *widget* is in that it folded, or gave rows up, before a layout shows it.

    The rows it gives up go in that layout, not a frame after it.
    """
    for ancestor in widget.ancestors:
        if isinstance(ancestor, ChatTranscriptPanelMarker):
            ancestor.entry_folded()
            return
