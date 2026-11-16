# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""ReferencePreviewDialog — what a clicked draft label stands for.

An image shows its path and the picture, read from disk after the dialog has
painted; a quote shows its whole text. A quote of Diff lines shows what it
names and those lines as a Diff block, highlighted after the dialog has painted.
"""

from __future__ import annotations

import asyncio
import binascii
from pathlib import Path
from typing import TYPE_CHECKING, ClassVar

from PIL import Image, UnidentifiedImageError
from rich.text import Text
from textual import work
from textual.containers import HorizontalScroll, VerticalGroup, VerticalScroll
from textual.content import Content
from textual.widgets import Static

from chrys.app.tui.binding_display import CLOSE_BINDING, localized_binding
from chrys.app.tui.i18n import render_str
from chrys.app.tui.screens.dialogs.base import BaseDialog
from chrys.app.tui.util.diff_comments import QUOTED_AFTER, QUOTED_BEFORE, QUOTED_IN_SESSION, QUOTED_IN_TURN
from chrys.app.tui.widgets.chat.image_preview import ChatImagePreview, image_preview_from_bytes
from chrys.app.tui.widgets.chat.image_view import TerminalImages
from chrys.app.tui.widgets.diff_view.compute import highlight_snippet, shown_code
from chrys.app.tui.widgets.diff_view.quote import ParsedDiffQuote, parse_diff_quote
from chrys.app.tui.widgets.diff_view.rows import DiffRow, Side
from chrys.app.tui.widgets.diff_view.unified import UnifiedDiffLines
from chrys.foundation.i18n import MessageRef, msg
from chrys.foundation.i18n.formatting import format_message, sanitize_legacy_block, sanitize_legacy_scalar
from chrys.foundation.text.images import MAX_IMAGE_SOURCE_BYTES, detect_image_media_type, read_image_source_bytes

if TYPE_CHECKING:
    from textual.app import ComposeResult

    from chrys.app.tui.i18n import LocaleController
    from chrys.foundation.text.draft_references import DraftReference

_PREVIEW_PATH = msg("tui.reference_preview.path", fallback="Path: {path}")
_PREVIEW_LOADING = msg("tui.reference_preview.loading", fallback="Loading image…")
_PREVIEW_MISSING = msg("tui.reference_preview.missing", fallback="The image file can no longer be read.")
_PREVIEW_UNREADABLE = msg("tui.reference_preview.unreadable", fallback="This image cannot be shown.")
_PREVIEW_IMAGE_ROWS = 20


class ReferencePreviewDialog(BaseDialog[None]):
    """Show the image or quote one draft label stands for."""

    # A viewer with nothing to decide closes by key, like the other viewers.
    BINDINGS: ClassVar[list] = [
        localized_binding("escape", "close", CLOSE_BINDING, show=False, priority=True),
        localized_binding("q", "close", CLOSE_BINDING, show=False),
    ]

    CSS_PATH = "reference_preview.tcss"

    def __init__(
        self, reference: DraftReference, *, cwd: Path, locale_controller: LocaleController | None = None
    ) -> None:
        self._reference = reference
        self._cwd = cwd
        self._locale_controller = locale_controller
        self._diff_quote = parse_diff_quote(reference.content) if reference.kind == "quote" else None
        super().__init__()

    def compose(self) -> ComposeResult:
        reference = self._reference
        with VerticalGroup(id="reference-preview-container") as container:
            # The border names the label; the body is what it stands for.
            container.border_title = Text(reference.label)
            with VerticalScroll(id="reference-preview-body"):
                if reference.kind == "image":
                    yield Static(
                        Text(self._text_of(_PREVIEW_PATH.bind(path=sanitize_legacy_scalar(reference.content)))),
                        id="reference-preview-path",
                    )
                    yield Static(Text(self._text_of(_PREVIEW_LOADING.bind())), id="reference-preview-status")
                elif self._diff_quote is not None:
                    yield Static(Text(self._diff_quote_title(self._diff_quote)), id="reference-preview-title")
                    # Code lines do not wrap, as in any Diff: the block scrolls sideways to show a long one whole.
                    with HorizontalScroll(id="reference-preview-diff"):
                        yield _diff_block(self._diff_quote)
                else:
                    # The whole dialog is the quote: plain text, no quote bar to break on wrapped lines.
                    yield Static(Text(sanitize_legacy_block(reference.content)), id="reference-preview-text")

    def on_mount(self) -> None:
        if not self._composed_live:
            return
        if self._reference.kind == "image":
            self._load_image(Path(self._reference.content))
        elif self._diff_quote is not None:
            self._highlight(self._diff_quote)

    @work(exclusive=True, exit_on_error=False)
    async def _highlight(self, quote: ParsedDiffQuote) -> None:
        """Highlight the quoted lines off the event loop, then show them in place of the plain ones."""
        shown = [shown_code(line.text) for line in quote.lines]
        try:
            highlighted = await asyncio.to_thread(highlight_snippet, "\n".join(shown), quote.path)
        except Exception:
            return  # The plain lines stay.
        if not self._composed_live or len(highlighted) != len(shown):
            return
        block = self.query_one("#reference-preview-diff-lines", UnifiedDiffLines)
        block.set_rows(
            _quoted_rows(quote, highlighted),
            number_digits=len(str(quote.lines[-1].number)),
            code_width=max(line.cell_length for line in highlighted),
        )

    def _diff_quote_title(self, quote: ParsedDiffQuote) -> str:
        """What the quote names, as the comment bar does, and which changes its version is relative to."""
        line = next(line.number for line in quote.lines if line.quoted)
        version = (QUOTED_BEFORE if quote.version == "before" else QUOTED_AFTER).bind(line=line)
        source = QUOTED_IN_SESSION.bind() if quote.turn is None else QUOTED_IN_TURN.bind(turn=quote.turn)
        return " · ".join((sanitize_legacy_scalar(quote.path), self._text_of(version), self._text_of(source)))

    @work(exclusive=True, exit_on_error=False)
    async def _load_image(self, path: Path) -> None:
        """Read and decode the image off the event loop, then show it in place of the loading line."""
        problem: MessageRef | None = None
        preview: ChatImagePreview | None = None
        try:
            # A mention recalled from history may be relative: read it as the engine does.
            preview = await asyncio.to_thread(_read_preview, self._cwd / path.expanduser())
        except FileNotFoundError, IsADirectoryError, PermissionError:
            problem = _PREVIEW_MISSING.bind()
        except binascii.Error, Image.DecompressionBombError, OSError, RuntimeError, UnidentifiedImageError, ValueError:
            problem = _PREVIEW_UNREADABLE.bind()
        if not self.is_attached or not self.app.is_running:
            return
        status = self.query_one("#reference-preview-status", Static)
        if preview is None:
            status.update(Text(self._text_of(problem or _PREVIEW_UNREADABLE.bind())))
            return
        status.display = False
        await self.query_one("#reference-preview-body", VerticalScroll).mount(
            TerminalImages([preview], max_rows=_PREVIEW_IMAGE_ROWS, id="reference-preview-image")
        )

    def action_close(self) -> None:
        self.dismiss(None)

    def _text_of(self, message: MessageRef) -> str:
        if self._locale_controller is None:
            return format_message(message)
        return render_str(self._locale_controller.localizer, message)


def _quoted_rows(quote: ParsedDiffQuote, code: list[Content]) -> list[DiffRow]:
    before = quote.version == "before"
    return [
        DiffRow(line.kind, text, before=line.number if before else None, after=None if before else line.number)
        for line, text in zip(quote.lines, code, strict=True)
    ]


def _diff_block(quote: ParsedDiffQuote) -> UnifiedDiffLines:
    """The quoted lines as a Diff block: the quoted version's numbers only, the quoted line marked."""
    code = [Content(shown_code(line.text)) for line in quote.lines]
    return UnifiedDiffLines(
        _quoted_rows(quote, code),
        number_digits=len(str(quote.lines[-1].number)),
        code_width=max(line.cell_length for line in code),
        number_side=Side.BEFORE if quote.version == "before" else Side.AFTER,
        marked_row=next(index for index, line in enumerate(quote.lines) if line.quoted),
        id="reference-preview-diff-lines",
    )


def _read_preview(path: Path) -> ChatImagePreview:
    data = read_image_source_bytes(path)
    if len(data) > MAX_IMAGE_SOURCE_BYTES:
        msg_text = "image file exceeds the preview size limit"
        raise ValueError(msg_text)
    # The bytes say what the image is, whatever its name says.
    media_type = detect_image_media_type(path, data)
    if media_type is None:
        msg_text = "not a supported image"
        raise ValueError(msg_text)
    return image_preview_from_bytes(data, media_type)
