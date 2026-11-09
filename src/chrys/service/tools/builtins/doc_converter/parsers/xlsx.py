# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""XLSX parser — converts Excel spreadsheets to Markdown using openpyxl."""

from __future__ import annotations

from typing import Any

from chrys.service.tools.builtins.doc_converter.parsers._table import mark_struck, rows_to_markdown_table
from chrys.service.tools.builtins.doc_converter.parsers.base import DocumentImageSink, ParsedDocument


def _load_worksheets(path: str) -> Any:
    """Open *path* read-only with its worksheets and without its chart sheets.

    A chart sheet holds no cells, and openpyxl loads its drawings even
    read-only: every picture on one goes through Pillow with every decoder.
    Shared strings keep their formatted runs, so a cell struck through only
    in part stays visible to ``_sheet_rows``.
    """
    from openpyxl.reader.excel import ExcelReader

    class _WorksheetReader(ExcelReader):
        def read_chartsheet(self, sheet: object, rel: object) -> None:
            """Leave the chart sheet, and the drawings it would load, unread."""

    reader = _WorksheetReader(path, read_only=True, data_only=True, rich_text=True)
    reader.read()
    return reader.wb


def _sheet_rows(worksheet: Any) -> list[tuple[object, ...]]:
    """Return the cell values of *worksheet*, with struck-through text marked by ``mark_struck``.

    A cell's font strikes out its whole value; a cell without a style of its
    own has the workbook's default font, which can strike too. A formatted
    run of a shared string is struck by its own run font alone, and a run
    without one takes the cell's font. Read-only openpyxl flattens inline
    strings to plain text, so only their cell font counts: Excel, WPS and
    LibreOffice write cell text as shared strings, while openpyxl itself
    writes inline strings.
    """
    from openpyxl.cell.rich_text import CellRichText, TextBlock

    def cell_value(cell: Any) -> object:
        value = cell.value
        if value is None:
            return None
        try:
            cell_struck = bool(cell.font.strike)
        except IndexError:
            # openpyxl does not bound-check a cell's style or font index, and some
            # exporters write broken ones: such a value converts unmarked, as it did
            # before fonts were read.
            cell_struck = False
        if isinstance(value, CellRichText):
            return mark_struck(
                (part.text, bool(part.font.strike)) if isinstance(part, TextBlock) else (part, cell_struck)
                for part in value
            )
        if cell_struck:
            return mark_struck([(str(value), True)])
        return value

    return [tuple(cell_value(cell) for cell in row) for row in worksheet.iter_rows()]


class XlsxParser:
    """Convert XLSX files to Markdown tables via ``openpyxl``."""

    @property
    def supported_extensions(self) -> frozenset[str]:
        return frozenset({".xlsx"})

    def parse(self, path: str, *, image_sink: DocumentImageSink | None = None) -> ParsedDocument:
        wb = _load_worksheets(path)
        parts: list[str] = []
        try:
            for sheet_name in wb.sheetnames:
                ws = wb[sheet_name]
                rows = _sheet_rows(ws)
                parts.append(f"# Sheet: {sheet_name}")
                if not rows:
                    parts.append("(empty sheet)")
                    continue
                parts.append(rows_to_markdown_table(rows))
        finally:
            wb.close()
        return ParsedDocument(markdown="\n\n".join(parts))
