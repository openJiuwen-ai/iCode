# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""``browser_extract`` - read page text/HTML via the live DOM (not a screenshot)."""

from __future__ import annotations

import re
from html import unescape
from typing import Any

from ..types import ToolBlock, text_block
from ._shared import call_value, eval_value, resolve_target

NAME = "browser_extract"

DESCRIPTION = (
    "Extract content from the live DOM (no screenshot / no OCR). With query, the "
    "matching element is located by the same scoring as click/fill; without query "
    "the whole page is read. format=text returns innerText, format=html the raw "
    "outerHTML, and format=markdown a lightweight HTML->markdown rendering (headings, "
    "links, lists, fenced code). Output is limited to max_chars characters starting at "
    "offset; a cut is marked '… (+N more chars; next offset=M)', so pass offset=M to "
    "read the next page of a long document (offsets count characters of the same "
    "query/format and shift if the page changes in between)."
)

PARAMS: dict[str, Any] = {
    "query": {"type": "string"},
    "format": {"enum": ["text", "markdown", "html"], "default": "text"},
    "max_chars": {"type": "integer", "min": 200, "max": 7000, "default": 7000},
    "offset": {"type": "integer", "min": 0, "default": 0},
}

_DEEP_TEXT_JS = (
    "const hasShadow=(node)=>{"
    " if(!node) return false;"
    " if(node.shadowRoot) return true;"
    " if(!node.querySelectorAll) return false;"
    " for(const el of node.querySelectorAll('*')){ if(el.shadowRoot) return true; }"
    " return false;"
    "};"
    "const SKIP=new Set(['SCRIPT','STYLE','NOSCRIPT','TEMPLATE']);"
    "const BLOCK=new Set(['DIV','P','LI','TR','SECTION','ARTICLE','HEADER','FOOTER','MAIN','ASIDE','NAV',"
    " 'H1','H2','H3','H4','H5','H6','BLOCKQUOTE','PRE','UL','OL','BR','HR']);"
    "const CELL=new Set(['TD','TH']);"
    "const deepText=(root)=>{"
    " if(!root) return '';"
    " if(!hasShadow(root)) return typeof root.innerText==='string' ? root.innerText : String(root.textContent||'');"
    " const parts=[];"
    " let rowCells=0;"
    " const walk=(n)=>{"
    "   if(!n) return;"
    "   if(n.nodeType===3){"
    "     const t=String(n.nodeValue||'').replace(/[\\t\\r\\n ]+/g,' ');"
    "     if(t) parts.push(t);"
    "     return;"
    "   }"
    "   if(n.nodeType===1){"
    "     if(SKIP.has(n.tagName)) return;"
    "     try{ const st=getComputedStyle(n); if(st.display==='none'||st.visibility==='hidden') return; }catch(_){}"
    "     if(n.tagName==='SLOT' && typeof n.assignedNodes==='function'){"
    "       const assigned=n.assignedNodes({flatten:true});"
    "       if(assigned.length){ for(const a of assigned) walk(a); return; }"
    "     }"
    "     const isBlock=BLOCK.has(n.tagName);"
    "     const isCell=CELL.has(n.tagName);"
    "     const isRow=n.tagName==='TR';"
    "     const prevCells=rowCells;"
    "     if(isRow) rowCells=0;"
    "     if(isBlock && parts.length && parts[parts.length-1]!=='\\n') parts.push('\\n');"
    "     if(isCell){ if(rowCells>0) parts.push('\\t'); rowCells++; }"
    "     if(n.shadowRoot){ for(const c of n.shadowRoot.childNodes) walk(c); }"
    "     else{ for(const c of n.childNodes) walk(c); }"
    "     if(isRow) rowCells=prevCells;"
    "     if(isBlock && parts.length && parts[parts.length-1]!=='\\n') parts.push('\\n');"
    "     return;"
    "   }"
    "   if(n.childNodes){ for(const c of n.childNodes) walk(c); }"
    " };"
    " walk(root);"
    " return parts.join('').replace(/ +\\n/g,'\\n').replace(/\\n +/g,'\\n')"
    "   .replace(/ *\\t */g,'\\t').replace(/ {2,}/g,' ').replace(/\\n{3,}/g,'\\n\\n')"
    "   .replace(/^[ \\n]+|[ \\n]+$/g,'');"
    "};"
)

_TEXT_FN = f"function(){{{_DEEP_TEXT_JS} return deepText(this);}}"
_PAGE_TEXT_EXPR = f"(()=>{{{_DEEP_TEXT_JS} return document.body ? deepText(document.body) : '';}})()"
_HTML_FN = "function(){ return this.outerHTML || ''; }"

_SCRIPT_RE = re.compile(r"<(script|style|noscript|template|svg)\b[^>]*>.*?</\1>", re.IGNORECASE | re.DOTALL)
_HEADING_RE = re.compile(r"<h([1-6])\b[^>]*>(.*?)</h\1>", re.IGNORECASE | re.DOTALL)
_ANCHOR_RE = re.compile(r"<a\b[^>]*href\s*=\s*[\"']([^\"']*)[\"'][^>]*>(.*?)</a>", re.IGNORECASE | re.DOTALL)
_PRE_RE = re.compile(r"<pre\b[^>]*>\s*<code\b[^>]*>(.*?)</code>\s*</pre>", re.IGNORECASE | re.DOTALL)
_LI_RE = re.compile(r"<li\b[^>]*>", re.IGNORECASE)
_BR_RE = re.compile(r"<(?:br|hr)\b[^>]*>", re.IGNORECASE)
_BLOCK_RE = re.compile(
    r"</?(?:p|div|section|article|header|footer|main|tr|table|ul|ol|blockquote|h[1-6])\b[^>]*>",
    re.IGNORECASE,
)
_TAG_RE = re.compile(r"<[^>]+>")
_SPACES_RE = re.compile(r"[ \t]+")
_BLANK_RE = re.compile(r"\n{3,}")


def as_text(value: Any) -> str:
    return "" if value is None else str(value)


def html_to_markdown(html: str) -> str:
    """Convert HTML to a small markdown subset using only the stdlib.

    Supports headings, links, list items, ``<br>``, block boundaries and fenced
    code blocks; every other tag is stripped and HTML entities are decoded.
    """
    text = _SCRIPT_RE.sub("", html or "")

    def _heading(match: re.Match[str]) -> str:
        level = int(match.group(1))
        title = _TAG_RE.sub("", match.group(2)).strip()
        return f"\n{'#' * level} {title}\n"

    text = _HEADING_RE.sub(_heading, text)

    def _anchor(match: re.Match[str]) -> str:
        label = _TAG_RE.sub("", match.group(2)).strip()
        href = match.group(1).strip()
        return f"[{label}]({href})" if href else label

    text = _ANCHOR_RE.sub(_anchor, text)

    blocks: list[str] = []

    def _pre(match: re.Match[str]) -> str:
        blocks.append(match.group(1))
        return f"\n\n\x00CODE{len(blocks) - 1}\x00\n\n"

    text = _PRE_RE.sub(_pre, text)
    text = _LI_RE.sub("\n- ", text)
    text = _BR_RE.sub("\n", text)
    text = _BLOCK_RE.sub("\n", text)
    text = _TAG_RE.sub("", text)
    text = unescape(text)

    for index, code in enumerate(blocks):
        clean_code = _TAG_RE.sub("", code)
        blocks[index] = unescape(clean_code).strip("\n")

    text = _SPACES_RE.sub(" ", text)
    text = "\n".join(line.strip() for line in text.splitlines())
    text = _BLANK_RE.sub("\n\n", text).strip()
    for index, code in enumerate(blocks):
        fenced = "```\n" + code + "\n```"
        text = text.replace(f"\x00CODE{index}\x00", fenced)
    return text


def _snapshot_subtree_text(session: Any, entry: Any) -> str:
    """Fallback text from fused snapshot descendants (e.g. closed Shadow DOM hosts)."""
    target_backend = getattr(entry, "backend_node_id", None)
    frame_id = getattr(entry, "frame_id", None)
    if not target_backend:
        return ""
    nodes = getattr(getattr(session, "snapshot", None), "nodes", None) or ()
    lines: list[str] = []
    seen: set[str] = set()
    for node in nodes:
        if frame_id is not None and getattr(node, "frame_id", None) != frame_id:
            continue
        ancestors = getattr(node, "ancestor_ids", ()) or ()
        if target_backend not in ancestors:
            continue
        piece = str(getattr(node, "name", "") or getattr(node, "text_hint", "") or "").strip()
        if not piece or piece in seen:
            continue
        seen.add(piece)
        lines.append(piece)
    if not lines:
        return ""
    return "[shadow DOM text from snapshot]\n" + "\n".join(lines)


def page_of(content: str, offset: int, max_chars: int) -> str:
    """The *max_chars* window of *content* at *offset*, marking where the next one starts."""
    if offset and offset >= len(content):
        return f"offset={offset} 已超出内容长度 {len(content)}（没有更多内容）"
    end = offset + max_chars
    window = content[offset:end]
    if end < len(content):
        window += f"… (+{len(content) - end} more chars; next offset={end})"
    return window


#: Slice by Unicode code points, never UTF-16 units: an emoji (surrogate pair)
#: is one character, is never split across pages, and offsets match Python's.
_CP_SLICE_JS = (
    " function cpSlice(s, off, maxChars){"
    "   let i = 0, cp = 0;"
    "   const step = () => { const c = s.charCodeAt(i);"
    "     i += (c >= 0xD800 && c <= 0xDBFF && i + 1 < s.length"
    "       && (s.charCodeAt(i + 1) & 0xFC00) === 0xDC00) ? 2 : 1; };"
    "   while (i < s.length && cp < off) { step(); cp++; }"
    "   const start = i; let n = 0;"
    "   while (i < s.length && n < maxChars) { step(); n++; }"
    "   const chunk = s.slice(start, i); let total = cp + n;"
    "   while (i < s.length) { step(); total++; }"
    "   return {len: total, chunk};"
    " }"
)
_TEXT_SLICE_FN = (
    f"function(off, maxChars){{{_DEEP_TEXT_JS}{_CP_SLICE_JS} return cpSlice(deepText(this), off, maxChars);}}"
)
_HTML_SLICE_FN = f"function(off, maxChars){{{_CP_SLICE_JS} return cpSlice(this.outerHTML || '', off, maxChars);}}"


def _format_slice(total_len: int, chunk: str, offset: int, max_chars: int) -> str:
    if offset and offset >= total_len:
        return f"offset={offset} 已超出内容长度 {total_len}（没有更多内容）"
    end = offset + len(chunk)
    if end < total_len:
        return chunk + f"… (+{total_len - end} more chars; next offset={end})"
    return chunk


async def _extract_element(
    session: Any,
    query: str,
    fmt: str,
    *,
    offset: int = 0,
    max_chars: int | None = None,
) -> str:
    entry = await resolve_target(session, query)
    object_id = await session.resolve_object_id(entry)
    cdp_session_id = entry.cdp_session_id
    if fmt == "markdown":
        result = await call_value(session, object_id, _HTML_FN, cdp_session_id=cdp_session_id)
        md = html_to_markdown(as_text(result.get("value")))
        return page_of(md, offset, max_chars) if max_chars is not None else md
    if max_chars is not None:
        fn = _TEXT_SLICE_FN if fmt == "text" else _HTML_SLICE_FN
        result = await call_value(
            session,
            object_id,
            fn,
            cdp_session_id=cdp_session_id,
            arguments=[{"value": offset}, {"value": max_chars}],
        )
        value = result.get("value")
        if isinstance(value, dict) and "chunk" in value and "len" in value:
            chunk = as_text(value["chunk"])
            total = int(value["len"])
            if fmt == "text" and total == 0:
                fallback = _snapshot_subtree_text(session, entry)
                if fallback:
                    return page_of(fallback, offset, max_chars)
            return _format_slice(total, chunk, offset, max_chars)
        text = as_text(value)
        if fmt == "text" and not text.strip():
            fallback = _snapshot_subtree_text(session, entry)
            if fallback:
                return page_of(fallback, offset, max_chars)
        return page_of(text, offset, max_chars)
    if fmt == "text":
        result = await call_value(session, object_id, _TEXT_FN, cdp_session_id=cdp_session_id)
        text = as_text(result.get("value"))
        if not text.strip():
            fallback = _snapshot_subtree_text(session, entry)
            if fallback:
                return fallback
        return text
    result = await call_value(session, object_id, _HTML_FN, cdp_session_id=cdp_session_id)
    return as_text(result.get("value"))


async def _extract_page(
    session: Any,
    fmt: str,
    *,
    offset: int = 0,
    max_chars: int | None = None,
) -> str:
    if fmt == "markdown":
        md = html_to_markdown(as_text(await eval_value(session, "document.documentElement.outerHTML")))
        return page_of(md, offset, max_chars) if max_chars is not None else md
    if max_chars is not None:
        if fmt == "text":
            expr = (
                f"(()=>{{{_DEEP_TEXT_JS}{_CP_SLICE_JS}"
                " const s = document.body ? deepText(document.body) : '';"
                f" return cpSlice(s, {offset}, {max_chars});"
                "})()"
            )
        else:
            expr = (
                f"(()=>{{{_CP_SLICE_JS}"
                " const s = document.documentElement ? document.documentElement.outerHTML : '';"
                f" return cpSlice(s, {offset}, {max_chars});"
                "})()"
            )
        value = await eval_value(session, expr)
        if isinstance(value, dict) and "chunk" in value and "len" in value:
            return _format_slice(int(value["len"]), as_text(value["chunk"]), offset, max_chars)
        return page_of(as_text(value), offset, max_chars)
    if fmt == "text":
        return as_text(await eval_value(session, _PAGE_TEXT_EXPR))
    return as_text(await eval_value(session, "document.documentElement.outerHTML"))


async def run(session: Any, arguments: dict[str, Any]) -> list[ToolBlock]:
    query = (arguments.get("query") or "").strip()
    fmt = arguments["format"]
    max_chars = arguments["max_chars"]
    offset = arguments["offset"]

    if query:
        content = await _extract_element(session, query, fmt, offset=offset, max_chars=max_chars)
    else:
        content = await _extract_page(session, fmt, offset=offset, max_chars=max_chars)
    session.note_success(session.target_id)
    return [text_block(content)]
