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
    async def fake_page(_session, fmt, *, offset=0, max_chars=None):
        assert fmt == "text"
        content = "a" * 250 + "b" * 250
        return extract.page_of(content, offset, max_chars) if max_chars is not None else content

    monkeypatch.setattr(extract, "_extract_page", fake_page)
    session = SimpleNamespace(target_id="t", note_success=lambda _target: None)
    arguments = {"format": "text", "max_chars": 200, "offset": 250}
    text = asyncio.run(extract.run(session, arguments))[0]["text"]
    assert text == "b" * 200 + "… (+50 more chars; next offset=450)"


def test_deep_text_js_preserves_empty_leading_middle_and_trailing_table_cells_and_inline_spans() -> None:
    import json
    import shutil
    import subprocess

    from icode_browser_use.actions.extract import _DEEP_TEXT_JS

    assert r"\t+\n" not in _DEEP_TEXT_JS
    assert r"\n\t+" not in _DEEP_TEXT_JS
    assert ".trim()" not in _DEEP_TEXT_JS

    node = shutil.which("node")
    if not node:
        return
    script = (
        f"{_DEEP_TEXT_JS}\n"
        "const txt = (v) => ({ nodeType: 3, nodeValue: v });\n"
        "const el = (tag, children = [], shadow = null) => ({\n"
        "  nodeType: 1, tagName: tag, childNodes: children, shadowRoot: shadow,\n"
        "  querySelectorAll: () => (shadow ? [{ shadowRoot: shadow }] : []),\n"
        "});\n"
        "const tr = (...cells) => el('TR', cells);\n"
        "const th = (t) => el('TH', t ? [txt(t)] : []);\n"
        "const td = (t) => el('TD', t ? [txt(t)] : []);\n"
        "const root = el('BODY', [\n"
        "  el('P', [txt('Order: '), el('SPAN', [txt('INV-')]), el('SPAN', [txt('0042')])]),\n"
        "  el('TABLE', [\n"
        "    tr(th('Q1'), th('Q2'), th('Q3')),\n"
        "    tr(td(''), td('20'), td('30')),\n"
        "    tr(td('10'), td(''), td('30')),\n"
        "    tr(td('10'), td('20'), td('')),\n"
        "  ]),\n"
        "  el('DIV', [], { childNodes: [el('SPAN', [txt('Help')])] }),\n"
        "]);\n"
        "root.querySelectorAll = () => [root.childNodes[2]];\n"
        "console.log(JSON.stringify(deepText(root)));\n"
    )
    out = subprocess.check_output([node, "-e", script], text=True, encoding="utf-8").strip()
    result = json.loads(out)
    assert "Order: INV-0042" in result
    assert "Q1\tQ2\tQ3\n\t20\t30\n10\t\t30\n10\t20\t\nHelp" in result
