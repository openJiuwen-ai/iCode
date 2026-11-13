# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""MCP editors display external values safely while retaining raw edits and copies."""

from __future__ import annotations

from typing import overload

from rich.cells import cell_len
from rich.text import Text
from textual._cells import cell_width_to_column_index
from textual.document._document import Document
from textual.document._document_navigator import DocumentNavigator
from textual.document._wrapped_document import WrappedDocument
from textual.geometry import Size

from chrys.app.tui.widgets import EnhancedInput, EnhancedTextArea
from chrys.foundation.i18n.formatting import sanitize_legacy_scalar, sanitize_terminal_block


class _MCPDocument(Document):
    """Expose safe lines for geometry; inherited edits, text and selections retain raw storage.

    Replacing controls one for one keeps document columns valid for both views.
    Textual's Document edits and copies its private raw lines, while wrapping,
    navigation and sizing consume the display lines exposed here.
    """

    @property
    def lines(self) -> list[str]:
        return [sanitize_terminal_block(line) for line in super().lines]

    @overload
    def __getitem__(self, line_index: int) -> str: ...

    @overload
    def __getitem__(self, line_index: slice) -> list[str]: ...

    def __getitem__(self, line_index: int | slice) -> str | list[str]:
        lines = super().__getitem__(line_index)
        if isinstance(lines, str):
            return sanitize_terminal_block(lines)
        return [sanitize_terminal_block(line) for line in lines]

    def get_size(self, tab_width: int) -> Size:
        width = max((cell_len(line.expandtabs(tab_width)) for line in self.lines), default=0)
        return Size(width, self.line_count)


class MCPInput(EnhancedInput):
    """Single-line MCP field whose terminal representation cannot execute controls."""

    @property
    def _value(self) -> Text:
        if self.password:
            return super()._value
        text = Text(sanitize_legacy_scalar(self.value), no_wrap=True, overflow="ignore", end="")
        return self.highlighter(text) if self.highlighter is not None else text

    def _position_to_cell(self, position: int) -> int:
        return self._value[:position].cell_len

    def _cell_offset_to_index(self, offset: int) -> int:
        return cell_width_to_column_index(self._value.plain, offset + self.scroll_offset.x, 4)


class MCPTextArea(EnhancedTextArea):
    """Plain-text MCP field with matching safe rendering, geometry and raw editing."""

    def _set_document(self, text: str, language: str | None) -> None:
        if language:
            raise ValueError("MCP configuration fields require plain text.")
        self._highlight_query = None
        self.document = _MCPDocument(text)
        self.wrapped_document = WrappedDocument(self.document, tab_width=self.indent_width)
        self.navigator = DocumentNavigator(self.wrapped_document)
        self._build_highlight_map()
        self.move_cursor((0, 0))
        self._rewrap_and_refresh_virtual_size()
