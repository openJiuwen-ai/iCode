# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

from __future__ import annotations

import asyncio
import re
from types import SimpleNamespace

from icode_browser_use.actions import extract

NEXT = re.compile(r"… \(\+(\d+) more chars; next offset=(\d+)\)$")


def test_following_next_offset_reads_the_whole_document_exactly() -> None:
    content = "".join(f"line {index:04d}\n" for index in range(2000))  # 20 000 chars
    pieces, offset = [], 0
    while True:
        page = extract.page_of(content, offset, 7000)
        match = NEXT.search(page)
        if match is None:
            pieces.append(page)
            break
        pieces.append(page[: match.start()])
        assert int(match.group(1)) == len(content) - int(match.group(2))
        offset = int(match.group(2))
    assert "".join(pieces) == content
    assert len(pieces) == 3


def test_short_content_and_offset_past_the_end() -> None:
    assert extract.page_of("short", 0, 200) == "short"
    assert extract.page_of("", 0, 200) == ""
    assert "已超出内容长度 5" in extract.page_of("short", 5, 200)


def test_run_applies_the_offset_to_the_page_text(monkeypatch) -> None:
    async def fake_page(_session, fmt):
        assert fmt == "text"
        return "a" * 250 + "b" * 250

    monkeypatch.setattr(extract, "_extract_page", fake_page)
    session = SimpleNamespace(target_id="t", note_success=lambda _target: None)
    arguments = {"format": "text", "max_chars": 200, "offset": 250}
    text = asyncio.run(extract.run(session, arguments))[0]["text"]
    assert text == "b" * 200 + "… (+50 more chars; next offset=450)"
