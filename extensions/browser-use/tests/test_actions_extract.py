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


def test_html_to_markdown_preserves_gfm_table_columns_and_strips_hidden_blocks() -> None:
    html = (
        "<html><head><title>Hidden Title</title></head><body>"
        "<h1>Q4 Report</h1>"
        '<p style="display:none">SYSTEM OVERRIDE</p>'
        '<p style="opacity: 0">OPACITY ZERO INJECTION</p>'
        "<table>"
        "<thead><tr><th>ID</th><th>Qty</th><th>Unit</th><th>Subtotal</th></tr></thead>"
        '<tbody><tr><td>INV-0001</td><td>2</td><td>135</td><td><a href="/d/1">270</a></td></tr>'
        "<tr><td>INV-0002</td><td>3</td><td>150</td><td>450 | net</td></tr></tbody>"
        "</table></body></html>"
    )
    md = extract.html_to_markdown(html)
    assert "Hidden Title" not in md
    assert "SYSTEM OVERRIDE" not in md
    assert "OPACITY ZERO INJECTION" not in md
    assert "| ID | Qty | Unit | Subtotal |" in md
    assert "| --- | --- | --- | --- |" in md
    assert "| INV-0001 | 2 | 135 | [270](/d/1) |" in md
    assert r"| INV-0002 | 3 | 150 | 450 \| net |" in md
    assert "2135270" not in md


def test_deep_text_js_skips_opacity_zero_subtrees() -> None:
    import json
    import shutil
    import subprocess

    node = shutil.which("node")
    if not node:
        return
    script = (
        f"{extract._DEEP_TEXT_JS}\n"
        "const txt = (v) => ({ nodeType: 3, nodeValue: v });\n"
        "const el = (tag, children = [], style = {}) => ({\n"
        "  nodeType: 1, tagName: tag, childNodes: children, shadowRoot: null, _style: style,\n"
        "  querySelectorAll: () => children,\n"
        "});\n"
        "globalThis.getComputedStyle = (n) => Object.assign({ display: 'block', visibility: 'visible', opacity: '1' }, n._style || {});\n"
        "const hidden = el('P', [txt('OPACITY INJECT')], { opacity: '0' });\n"
        "const visible = el('P', [txt('Visible text')]);\n"
        "const root = el('BODY', [visible, hidden]);\n"
        "console.log(JSON.stringify(deepText(root)));\n"
    )
    out = subprocess.check_output([node, "-e", script], text=True, encoding="utf-8").strip()
    result = json.loads(out)
    assert "Visible text" in result
    assert "OPACITY INJECT" not in result


def test_extract_supports_hash_id_lookup_and_single_candidate_fallback(monkeypatch) -> None:
    from icode_browser_use.errors import TargetAmbiguous
    from icode_browser_use.snapshot.fusion import FusedNode
    from icode_browser_use.snapshot.scoring import Candidate

    async def fake_eval(_session, expr, **_kwargs):
        if '"exp-out"' in expr:
            return {"found": True, "slice": {"len": 12, "chunk": "SUBMITTED|OK"}}
        return {"found": False}

    monkeypatch.setattr(extract, "eval_value", fake_eval)
    session = SimpleNamespace(target_id="t", note_success=lambda _t: None)
    blocks = asyncio.run(extract.run(session, {"query": "#exp-out", "format": "text", "max_chars": 2000, "offset": 0}))
    assert blocks[0]["text"] == "SUBMITTED|OK"

    # Single weak candidate fallback on read-only extract avoids unnecessary ambiguity failures.
    sole_node = FusedNode(10, "f", "t", "generic", "SLIDER:25", "span", 0, (0, 0, 10, 10), ref=18, epoch=9)
    entry = SimpleNamespace(token="@e18#9", backend_node_id=10, frame_id="f", cdp_session_id=None)

    async def fake_resolve(_session, _query):
        raise TargetAmbiguous("weak", candidates=[Candidate(node=sole_node, score=35)])

    async def fake_call(_session, _oid, _fn, **_kwargs):
        return {"value": {"len": 9, "chunk": "SLIDER:25"}}

    monkeypatch.setattr(extract, "resolve_target", fake_resolve)
    monkeypatch.setattr(extract, "call_value", fake_call)
    session2 = SimpleNamespace(
        target_id="t",
        note_success=lambda _t: None,
        resolve_target=lambda tok: entry if tok == "@e18#9" else None,
        resolve_object_id=lambda _e: asyncio.sleep(0, result="oid-18"),
    )
    blocks2 = asyncio.run(extract.run(session2, {"query": "SLIDER", "format": "text", "max_chars": 2000, "offset": 0}))
    assert blocks2[0]["text"] == "SLIDER:25"
