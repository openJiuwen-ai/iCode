# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""``browser_extract`` - read page text/HTML via the live DOM (not a screenshot)."""

from __future__ import annotations

import json
import re
from html.parser import HTMLParser
from typing import Any
from urllib.parse import urlsplit

from ..errors import StaleRef, TargetAmbiguous, TargetNotFound
from ..policy import sanitize_display_url
from ..snapshot.fusion import FusedNode
from ..snapshot.scoring import build_node_index, describe_region, rank
from ..snapshot.serializer import escape_label, sanitize_role
from ..types import ToolBlock, text_block
from ._shared import call_value, eval_value, resolve_target, snapshot_needs_refresh

NAME = "browser_extract"

DESCRIPTION = (
    "Extract content from the live DOM (no screenshot / no OCR). Without query "
    "the whole page is read (preferred for reading page text); with query, pass "
    "an @eN#epoch ref, a #element-id, or visible target text (complex CSS selectors "
    "are not supported). Pass max_matches (2-5) with a text query to read multiple "
    "matching elements in one call within the shared max_chars budget. format=text "
    "returns visible text, format=html the raw outerHTML, and format=markdown a "
    "lightweight HTML->markdown rendering (headings, links, lists, tables, fenced code). "
    "Output is limited to max_chars characters starting at offset; a cut is marked "
    "'… (+N more chars; next offset=M)', so pass offset=M to read the next page of a "
    "long document (offsets count characters of the same query/format and shift if the "
    "page changes in between)."
)

PARAMS: dict[str, Any] = {
    "query": {"type": "string"},
    "format": {"enum": ["text", "markdown", "html"], "default": "text"},
    "max_chars": {"type": "integer", "min": 50, "max": 7000, "default": 7000},
    "offset": {"type": "integer", "min": 0, "default": 0},
    "max_matches": {"type": "integer", "min": 1, "max": 5, "default": 1},
}

_DEEP_TEXT_JS = (
    "const SKIP=new Set(['SCRIPT','STYLE','NOSCRIPT','TEMPLATE']);"
    "const BLOCK=new Set(['DIV','P','LI','TR','SECTION','ARTICLE','HEADER','FOOTER','MAIN','ASIDE','NAV',"
    " 'H1','H2','H3','H4','H5','H6','BLOCKQUOTE','PRE','UL','OL','BR','HR']);"
    "const CELL=new Set(['TD','TH']);"
    "const styleOf=(el)=>{"
    " if(!el || el.nodeType!==1) return null;"
    " try{"
    "  const win=(el.ownerDocument && el.ownerDocument.defaultView) || (typeof window!=='undefined' ? window : null);"
    "  const fn=(win && typeof win.getComputedStyle==='function')"
    "   ? win.getComputedStyle : (typeof getComputedStyle==='function' ? getComputedStyle : null);"
    "  return fn ? fn(el) : null;"
    " }catch(_){ return null; }"
    "};"
    "const isHardHiddenStyle=(st)=>{"
    " if(!st) return false;"
    " if(st.display==='none' || st.contentVisibility==='hidden') return true;"
    " const op=parseFloat(st.opacity);"
    " return !Number.isNaN(op) && op<=0;"
    "};"
    "const isVisHiddenStyle=(st)=>!!st && (st.visibility==='hidden' || st.visibility==='collapse');"
    "const isAncestorHardHidden=(node)=>{"
    " if(!node) return true;"
    " let cur=node.nodeType===1 ? node : (node.parentElement || null);"
    " let crossedFrame=false;"
    " let hops=0;"
    " while(cur && cur.nodeType===1 && hops<256){"
    "  hops++;"
    "  const tag=String(cur.tagName||'').toUpperCase();"
    "  if(SKIP.has(tag) || cur.hidden===true) return true;"
    "  const st=styleOf(cur);"
    "  if(isHardHiddenStyle(st)) return true;"
    "  if(crossedFrame && isVisHiddenStyle(st)) return true;"
    "  if(cur.parentElement){ cur=cur.parentElement; continue; }"
    "  const root=typeof cur.getRootNode==='function' ? cur.getRootNode() : null;"
    "  if(root && root.host){ cur=root.host; continue; }"
    "  try{"
    "   const fe=cur.ownerDocument && cur.ownerDocument.defaultView ? cur.ownerDocument.defaultView.frameElement : null;"
    "   if(fe){"
    "    if(typeof fe.getBoundingClientRect==='function'){"
    "     const fr=fe.getBoundingClientRect();"
    "     if(fr.width<=0 || fr.height<=0) return true;"
    "    }"
    "    crossedFrame=true; cur=fe; continue;"
    "   }"
    "  }catch(_){}"
    "  break;"
    " }"
    " return false;"
    "};"
    "const isAncestorHidden=(node)=>{"
    " if(!node) return true;"
    " let cur=node.nodeType===1 ? node : (node.parentElement || null);"
    " let visChecked=false;"
    " let hops=0;"
    " while(cur && cur.nodeType===1 && hops<256){"
    "  hops++;"
    "  const tag=String(cur.tagName||'').toUpperCase();"
    "  if(SKIP.has(tag) || cur.hidden===true) return true;"
    "  const st=styleOf(cur);"
    "  if(isHardHiddenStyle(st)) return true;"
    "  if(!visChecked && st && st.visibility){"
    "   visChecked=true;"
    "   if(isVisHiddenStyle(st)) return true;"
    "  }"
    "  if(cur.parentElement){ cur=cur.parentElement; continue; }"
    "  const root=typeof cur.getRootNode==='function' ? cur.getRootNode() : null;"
    "  if(root && root.host){ cur=root.host; continue; }"
    "  try{"
    "   const fe=cur.ownerDocument && cur.ownerDocument.defaultView ? cur.ownerDocument.defaultView.frameElement : null;"
    "   if(fe){"
    "    if(typeof fe.getBoundingClientRect==='function'){"
    "     const fr=fe.getBoundingClientRect();"
    "     if(fr.width<=0 || fr.height<=0) return true;"
    "    }"
    "    visChecked=false; cur=fe; continue;"
    "   }"
    "  }catch(_){}"
    "  break;"
    " }"
    " return false;"
    "};"
    "const hasShadow=(node)=>{"
    " if(!node) return false;"
    " if(node.shadowRoot) return true;"
    " if(!node.querySelectorAll) return false;"
    " for(const el of node.querySelectorAll('*')){ if(el.shadowRoot) return true; }"
    " return false;"
    "};"
    "const isComplexHidden=(el)=>{"
    " const st=styleOf(el);"
    " if(!st) return false;"
    " const op=parseFloat(st.opacity);"
    " return (!Number.isNaN(op) && op<=0) || st.contentVisibility==='hidden' || st.visibility==='collapse';"
    "};"
    "const hasComplexHidden=(node)=>{"
    " if(!node) return false;"
    " if(isComplexHidden(node)) return true;"
    " if(!node.querySelectorAll) return false;"
    " for(const el of node.querySelectorAll('*')){ if(isComplexHidden(el)) return true; }"
    " return false;"
    "};"
    "const deepText=(root)=>{"
    " if(!root || isAncestorHardHidden(root)) return '';"
    " if(root.isConnected!==false && !hasShadow(root) && !hasComplexHidden(root)){"
    "  return typeof root.innerText==='string' ? root.innerText : String(root.textContent||'');"
    " }"
    " const parts=[];"
    " let rowCells=0;"
    " const rootSt=styleOf(root);"
    " const initVisHidden=isVisHiddenStyle(rootSt);"
    " const walk=(n, visHidden)=>{"
    "   if(!n) return;"
    "   if(n.nodeType===3){"
    "     if(visHidden) return;"
    "     const t=String(n.nodeValue||'').replace(/[\\t\\r\\n ]+/g,' ');"
    "     if(t) parts.push(t);"
    "     return;"
    "   }"
    "   if(n.nodeType===1){"
    "     const tag=String(n.tagName||'').toUpperCase();"
    "     if(SKIP.has(tag) || n.hidden===true) return;"
    "     const st=styleOf(n);"
    "     if(isHardHiddenStyle(st)) return;"
    "     const curVisHidden=(st && st.visibility) ? isVisHiddenStyle(st) : visHidden;"
    "     if(tag==='SLOT' && typeof n.assignedNodes==='function'){"
    "       const assigned=n.assignedNodes({flatten:true});"
    "       if(assigned.length){ for(const a of assigned) walk(a, curVisHidden); return; }"
    "     }"
    "     const isBlock=!curVisHidden && BLOCK.has(tag);"
    "     const isCell=!curVisHidden && CELL.has(tag);"
    "     const isRow=!curVisHidden && tag==='TR';"
    "     const prevCells=rowCells;"
    "     if(isRow) rowCells=0;"
    "     if(isBlock && parts.length && parts[parts.length-1]!=='\\n') parts.push('\\n');"
    "     if(isCell){ if(rowCells>0) parts.push('\\t'); rowCells++; }"
    "     if(n.shadowRoot){ for(const c of n.shadowRoot.childNodes) walk(c, curVisHidden); }"
    "     else if(n.childNodes){ for(const c of n.childNodes) walk(c, curVisHidden); }"
    "     if(isRow) rowCells=prevCells;"
    "     if(isBlock && parts.length && parts[parts.length-1]!=='\\n') parts.push('\\n');"
    "     return;"
    "   }"
    "   if(n.childNodes){ for(const c of n.childNodes) walk(c, visHidden); }"
    " };"
    " walk(root, initVisHidden);"
    " return parts.join('').replace(/ +\\n/g,'\\n').replace(/\\n +/g,'\\n')"
    "   .replace(/ *\\t */g,'\\t').replace(/ {2,}/g,' ').replace(/\\n{3,}/g,'\\n\\n')"
    "   .replace(/^[ \\n]+|[ \\n]+$/g,'');"
    "};"
)

_MARKDOWN_JS = (
    _DEEP_TEXT_JS + "const MD_SKIP=new Set(['SCRIPT','STYLE','NOSCRIPT','TEMPLATE','SVG','HEAD']);"
    "const MD_BLOCK=new Set(['P','DIV','SECTION','ARTICLE','HEADER','FOOTER','MAIN','ASIDE','NAV',"
    " 'UL','OL','BLOCKQUOTE','FIGURE','FIGCAPTION','DL','DT','DD','FORM','ADDRESS']);"
    "const cleanHref=(raw)=>{"
    " const cleaned=String(raw||'').replace(/[\\u0000-\\u001f\\u007f]+/g,'').trim();"
    " if(!cleaned) return '';"
    " const low=cleaned.toLowerCase();"
    " if(low.startsWith('javascript:')||low.startsWith('vbscript:')||low.startsWith('file:')"
    "  ||low.startsWith('data:')||low.startsWith('blob:')||low.startsWith('about:')) return '';"
    " if(low.startsWith('http://')||low.startsWith('https://')||cleaned.startsWith('//')){"
    "  try{"
    "   const u=new URL(cleaned.startsWith('//')?('https:'+cleaned):cleaned);"
    "   if(u.protocol!=='http:' && u.protocol!=='https:') return '';"
    "   const hash=(u.hash && u.hash.length<16 && !u.hash.includes('=')) ? u.hash : '';"
    "   return u.origin + u.pathname + hash;"
    "  }catch(_){ return ''; }"
    " }"
    " const qIdx=cleaned.indexOf('?');"
    " const hIdx=cleaned.indexOf('#');"
    " let path=cleaned;"
    " let hash='';"
    " if(hIdx>=0){"
    "  const rawHash=cleaned.slice(hIdx);"
    "  path=cleaned.slice(0,hIdx);"
    "  if(rawHash.length<16 && !rawHash.includes('=')) hash=rawHash;"
    " }"
    " if(qIdx>=0 && (hIdx<0 || qIdx<hIdx)) path=path.slice(0,qIdx);"
    " return path + hash;"
    "};"
    "const deepMarkdown=(root)=>{"
    " if(!root || isAncestorHardHidden(root)) return '';"
    " const target=(root.tagName && String(root.tagName).toUpperCase()==='HTML' && root.ownerDocument && root.ownerDocument.body)"
    "  ? root.ownerDocument.body : root;"
    " if(!target || isAncestorHardHidden(target)) return '';"
    " const tokens=[];"
    " const codeBlocks=[];"
    " const tables=[];"
    " const collectInline=(n, visHidden, buf)=>{"
    "  if(!n) return;"
    "  if(n.nodeType===3){ if(!visHidden) buf.push(String(n.nodeValue||'')); return; }"
    "  if(n.nodeType!==1){ if(n.childNodes){ for(const c of n.childNodes) collectInline(c, visHidden, buf); } return; }"
    "  const tag=String(n.tagName||'').toUpperCase();"
    "  if(MD_SKIP.has(tag) || n.hidden===true) return;"
    "  const st=styleOf(n);"
    "  if(isHardHiddenStyle(st)) return;"
    "  const curVis=(st && st.visibility) ? isVisHiddenStyle(st) : visHidden;"
    "  if(tag==='BR'){ if(!curVis) buf.push(' '); return; }"
    "  if(tag==='A'){"
    "   const inner=[];"
    "   if(n.shadowRoot){ for(const c of n.shadowRoot.childNodes) collectInline(c, curVis, inner); }"
    "   else if(n.childNodes){ for(const c of n.childNodes) collectInline(c, curVis, inner); }"
    "   const label=inner.join('').replace(/\\s+/g,' ').trim();"
    "   if(label){"
    "    const href=cleanHref(n.getAttribute ? n.getAttribute('href') : '');"
    "    buf.push(href ? ('['+label+']('+href+')') : label);"
    "   }"
    "   return;"
    "  }"
    "  if(tag==='SLOT' && typeof n.assignedNodes==='function'){"
    "   const assigned=n.assignedNodes({flatten:true});"
    "   if(assigned.length){ for(const a of assigned) collectInline(a, curVis, buf); return; }"
    "  }"
    "  const pad=!curVis && (MD_BLOCK.has(tag) || tag==='TR' || tag==='TD' || tag==='TH' || tag==='LI');"
    "  if(pad && buf.length) buf.push(' ');"
    "  if(n.shadowRoot){ for(const c of n.shadowRoot.childNodes) collectInline(c, curVis, buf); }"
    "  else if(n.childNodes){ for(const c of n.childNodes) collectInline(c, curVis, buf); }"
    "  if(pad && buf.length) buf.push(' ');"
    " };"
    " const renderTable=(tbl, visHidden)=>{"
    "  const rows=[];"
    "  const scanRows=(container, vh)=>{"
    "   const kids=container.shadowRoot ? container.shadowRoot.childNodes : container.childNodes;"
    "   if(!kids) return;"
    "   for(const ch of kids){"
    "    if(!ch || ch.nodeType!==1) continue;"
    "    const tag=String(ch.tagName||'').toUpperCase();"
    "    if(MD_SKIP.has(tag) || ch.hidden===true) continue;"
    "    const st=styleOf(ch);"
    "    if(isHardHiddenStyle(st)) continue;"
    "    const cv=(st && st.visibility) ? isVisHiddenStyle(st) : vh;"
    "    if(tag==='TABLE') continue;"
    "    if(tag==='THEAD' || tag==='TBODY' || tag==='TFOOT'){ scanRows(ch, cv); continue; }"
    "    if(tag==='TR'){"
    "     const cells=[];"
    "     const rKids=ch.shadowRoot ? ch.shadowRoot.childNodes : ch.childNodes;"
    "     if(rKids){"
    "      for(const cell of rKids){"
    "       if(!cell || cell.nodeType!==1) continue;"
    "       const ctag=String(cell.tagName||'').toUpperCase();"
    "       if(ctag!=='TD' && ctag!=='TH') continue;"
    "       if(cell.hidden===true) continue;"
    "       const cst=styleOf(cell);"
    "       if(isHardHiddenStyle(cst)) continue;"
    "       const ccv=(cst && cst.visibility) ? isVisHiddenStyle(cst) : cv;"
    "       const buf=[];"
    "       collectInline(cell, ccv, buf);"
    "       cells.push(buf.join('').replace(/\\s+/g,' ').trim().replace(/\\|/g,'\\\\|'));"
    "      }"
    "     }"
    "     if(cells.length) rows.push(cells);"
    "    }"
    "   }"
    "  };"
    "  scanRows(tbl, visHidden);"
    "  if(!rows.length) return '';"
    "  let cols=0; for(const r of rows){ if(r.length>cols) cols=r.length; }"
    "  if(cols<=0) return '';"
    "  const lines=[];"
    "  for(let i=0;i<rows.length;i++){"
    "   const padded=rows[i].slice();"
    "   while(padded.length<cols) padded.push('');"
    "   lines.push('| '+padded.join(' | ')+' |');"
    "   if(i===0){ const sep=[]; for(let c=0;c<cols;c++) sep.push('---'); lines.push('| '+sep.join(' | ')+' |'); }"
    "  }"
    "  return lines.join('\\n');"
    " };"
    " const collectRawCode=(n, visHidden, buf)=>{"
    "  if(!n) return;"
    "  if(n.nodeType===3){ if(!visHidden) buf.push(String(n.nodeValue||'')); return; }"
    "  if(n.nodeType!==1){ if(n.childNodes){ for(const c of n.childNodes) collectRawCode(c, visHidden, buf); } return; }"
    "  const tag=String(n.tagName||'').toUpperCase();"
    "  if(MD_SKIP.has(tag) || n.hidden===true) return;"
    "  const st=styleOf(n);"
    "  if(isHardHiddenStyle(st)) return;"
    "  const cv=(st && st.visibility) ? isVisHiddenStyle(st) : visHidden;"
    "  if(tag==='BR'){ if(!cv) buf.push('\\n'); return; }"
    "  if(n.shadowRoot){ for(const c of n.shadowRoot.childNodes) collectRawCode(c, cv, buf); }"
    "  else if(n.childNodes){ for(const c of n.childNodes) collectRawCode(c, cv, buf); }"
    " };"
    " const walk=(n, visHidden)=>{"
    "  if(!n) return;"
    "  if(n.nodeType===3){"
    "   if(!visHidden) tokens.push(String(n.nodeValue||'').replace(/[\\t\\r\\n ]+/g,' '));"
    "   return;"
    "  }"
    "  if(n.nodeType!==1){ if(n.childNodes){ for(const c of n.childNodes) walk(c, visHidden); } return; }"
    "  const tag=String(n.tagName||'').toUpperCase();"
    "  if(MD_SKIP.has(tag) || n.hidden===true) return;"
    "  const st=styleOf(n);"
    "  if(isHardHiddenStyle(st)) return;"
    "  const cv=(st && st.visibility) ? isVisHiddenStyle(st) : visHidden;"
    "  if(tag==='SLOT' && typeof n.assignedNodes==='function'){"
    "   const assigned=n.assignedNodes({flatten:true});"
    "   if(assigned.length){ for(const a of assigned) walk(a, cv); return; }"
    "  }"
    "  if(tag==='TABLE'){"
    "   const md=renderTable(n, cv);"
    "   if(md){ const idx=tables.length; tables.push(md); tokens.push('\\n\\n\\u0000TABLE'+idx+'\\u0000\\n\\n'); }"
    "   return;"
    "  }"
    "  if(tag==='PRE'){"
    "   const buf=[];"
    "   collectRawCode(n, cv, buf);"
    "   const code=buf.join('').replace(/^\\n+|\\n+$/g,'');"
    "   if(code){ const idx=codeBlocks.length; codeBlocks.push(code); tokens.push('\\n\\n\\u0000CODE'+idx+'\\u0000\\n\\n'); }"
    "   return;"
    "  }"
    "  if(/^H[1-6]$/.test(tag)){"
    "   const buf=[];"
    "   collectInline(n, cv, buf);"
    "   const heading=buf.join('').replace(/\\s+/g,' ').trim();"
    "   if(heading) tokens.push('\\n'+'#'.repeat(Number(tag[1]))+' '+heading+'\\n');"
    "   return;"
    "  }"
    "  if(tag==='A'){"
    "   const buf=[];"
    "   collectInline(n, cv, buf);"
    "   const label=buf.join('').replace(/\\s+/g,' ').trim();"
    "   if(label){"
    "    const href=cleanHref(n.getAttribute ? n.getAttribute('href') : '');"
    "    tokens.push(href ? ('['+label+']('+href+')') : label);"
    "   }"
    "   return;"
    "  }"
    "  if(tag==='BR' || tag==='HR'){ if(!cv) tokens.push('\\n'); return; }"
    "  if(tag==='LI'){"
    "   if(!cv) tokens.push('\\n- ');"
    "   if(n.shadowRoot){ for(const c of n.shadowRoot.childNodes) walk(c, cv); }"
    "   else if(n.childNodes){ for(const c of n.childNodes) walk(c, cv); }"
    "   if(!cv) tokens.push('\\n');"
    "   return;"
    "  }"
    "  const isBlk=!cv && MD_BLOCK.has(tag);"
    "  if(isBlk) tokens.push('\\n');"
    "  if(n.shadowRoot){ for(const c of n.shadowRoot.childNodes) walk(c, cv); }"
    "  else if(n.childNodes){ for(const c of n.childNodes) walk(c, cv); }"
    "  if(isBlk) tokens.push('\\n');"
    " };"
    " walk(target, isVisHiddenStyle(styleOf(target)));"
    " let out=tokens.join('').replace(/[ \\t]+/g,' ').split('\\n').map((l)=>l.trim()).join('\\n')"
    "  .replace(/\\n{3,}/g,'\\n\\n').trim();"
    " for(let i=0;i<tables.length;i++) out=out.replace('\\u0000TABLE'+i+'\\u0000', tables[i]);"
    " for(let i=0;i<codeBlocks.length;i++) out=out.replace('\\u0000CODE'+i+'\\u0000', '```\\n'+codeBlocks[i]+'\\n```');"
    " return out;"
    "};"
)

_TEXT_FN = f"function(){{{_DEEP_TEXT_JS} return {{text: deepText(this), hidden: isAncestorHidden(this)}};}}"
_PAGE_TEXT_EXPR = f"(()=>{{{_DEEP_TEXT_JS} return document.body ? deepText(document.body) : '';}})()"
_HTML_FN = "function(){ return this.outerHTML || ''; }"
_MARKDOWN_FN = f"function(){{{_MARKDOWN_JS} return deepMarkdown(this);}}"
_PAGE_MARKDOWN_EXPR = f"(()=>{{{_MARKDOWN_JS} return document.body ? deepMarkdown(document.body) : deepMarkdown(document.documentElement);}})()"

_SPACES_RE = re.compile(r"[ \t]+")
_BLANK_RE = re.compile(r"\n{3,}")
_SIMPLE_ID_RE = re.compile(r"^#([A-Za-z_][\w:-]*)$")
_CSS_LIKE_RE = re.compile(r"^(?:[#.][A-Za-z_][\w-]*|.*(?:\s*>\s*|\[[^\]]+\]))")
_CTRL_CHARS_RE = re.compile(r"[\x00-\x1f\x7f]+")
_HTML_VOID_TAGS = frozenset(
    {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}
)
_HTML_SKIP_TAGS = frozenset({"script", "style", "noscript", "template", "svg", "head"})
_HTML_BLOCK_TAGS = frozenset(
    {
        "p",
        "div",
        "section",
        "article",
        "header",
        "footer",
        "main",
        "aside",
        "nav",
        "ul",
        "ol",
        "blockquote",
        "figure",
        "figcaption",
        "dl",
        "dt",
        "dd",
        "form",
        "address",
    }
)
_HTML_HEADING_TAGS = frozenset({"h1", "h2", "h3", "h4", "h5", "h6"})
_DISALLOWED_HREF_SCHEMES = ("javascript:", "vbscript:", "file:", "data:", "blob:", "about:")
_INNER_NEXT_OFFSET_RE = re.compile(r"… \(\+(\d+) more chars; next offset=\d+\)$")


def _sanitize_markdown_href(raw: str) -> str:
    """Scrub unsafe schemes, credentials, and query params from Markdown link targets."""
    cleaned = _CTRL_CHARS_RE.sub("", raw or "").strip()
    if not cleaned:
        return ""
    low = cleaned.lower()
    if low.startswith(_DISALLOWED_HREF_SCHEMES):
        return ""
    if low.startswith(("http://", "https://")) or cleaned.startswith("//"):
        candidate = f"https:{cleaned}" if cleaned.startswith("//") else cleaned
        base = sanitize_display_url(candidate)
        if not base:
            return ""
        try:
            frag = urlsplit(candidate).fragment
        except ValueError:
            frag = ""
        if frag and "=" not in frag and len(frag) < 16 and not base.endswith(":"):
            return f"{base}#{frag}"
        return base
    try:
        parts = urlsplit(cleaned)
    except ValueError:
        return ""
    if parts.scheme:
        return ""
    frag = f"#{parts.fragment}" if parts.fragment and "=" not in parts.fragment and len(parts.fragment) < 16 else ""
    return f"{parts.path}{frag}"


def _inline_style_visibility(attrs_dict: dict[str, str | None]) -> tuple[bool, bool | None]:
    """Return ``(hard_hidden, visibility_override)`` from HTML ``hidden`` and inline ``style``."""
    if "hidden" in attrs_dict:
        return True, None
    style = attrs_dict.get("style") or ""
    if not style:
        return False, None
    hard_hidden = False
    vis_override: bool | None = None
    for decl in style.split(";"):
        if ":" not in decl:
            continue
        prop, raw_val = decl.split(":", 1)
        prop_name = prop.strip().lower()
        val = re.sub(r"!\s*important\s*$", "", raw_val, flags=re.IGNORECASE).strip().lower()
        if prop_name == "display" and val == "none":
            hard_hidden = True
        elif prop_name == "content-visibility" and val == "hidden":
            hard_hidden = True
        elif prop_name == "opacity":
            num_str = val[:-1].strip() if val.endswith("%") else val
            try:
                if float(num_str) <= 0.0:
                    hard_hidden = True
            except ValueError:
                pass
        elif prop_name == "visibility":
            if val in {"hidden", "collapse"}:
                vis_override = True
            elif val == "visible":
                vis_override = False
    return hard_hidden, vis_override


class _MarkdownHTMLParser(HTMLParser):
    """Stdlib HTML->Markdown converter that tracks full nesting for hidden subtrees and nested tables."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.tokens: list[str] = []
        self.tables: list[str] = []
        self.code_blocks: list[str] = []
        # Stack entries: (tag, hard_hidden, vis_hidden)
        self._stack: list[tuple[str, bool, bool]] = []
        self._skip_depth = 0
        self._pre_depth = 0
        self._pre_parts: list[str] = []
        self._heading_level = 0
        self._heading_parts: list[str] = []
        self._anchor_href: str | None = None
        self._anchor_parts: list[str] = []
        # Table state (nested tables collapse cleanly inside the enclosing cell).
        self._table_depth = 0
        self._table_rows: list[list[str]] = []
        self._in_row = False
        self._row_cells: list[str] = []
        self._in_cell = False
        self._cell_parts: list[str] = []

    def _cur_hidden(self) -> tuple[bool, bool]:
        if self._skip_depth > 0:
            return True, True
        if not self._stack:
            return False, False
        _, hard, vis = self._stack[-1]
        return hard, vis

    def _emit_text(self, text: str) -> None:
        if self._pre_depth > 0:
            self._pre_parts.append(text)
            return
        if self._anchor_href is not None:
            self._anchor_parts.append(text)
            return
        if self._heading_level > 0:
            self._heading_parts.append(text)
            return
        if self._table_depth > 0:
            if self._in_cell:
                self._cell_parts.append(text)
            return
        self.tokens.append(text)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        low = tag.lower()
        is_void = low in _HTML_VOID_TAGS
        parent_hard, parent_vis = self._cur_hidden()
        if self._skip_depth > 0 or low in _HTML_SKIP_TAGS:
            if not is_void:
                self._skip_depth += 1
                self._stack.append((low, True, True))
            return

        attrs_dict = {k.lower(): v for k, v in attrs}
        self_hard, vis_override = _inline_style_visibility(attrs_dict)
        hard = parent_hard or self_hard
        vis = parent_vis if vis_override is None else vis_override
        if not is_void:
            self._stack.append((low, hard, vis))

        if hard:
            return

        if self._pre_depth > 0:
            if low == "br" and not vis:
                self._pre_parts.append("\n")
            return

        if low == "pre":
            self._pre_depth = 1
            self._pre_parts = []
            return

        if low == "table":
            self._table_depth += 1
            if self._table_depth == 1:
                self._table_rows = []
                self._in_row = False
                self._row_cells = []
                self._in_cell = False
                self._cell_parts = []
            elif self._in_cell and not vis:
                self._cell_parts.append(" ")
            return

        if self._table_depth == 1:
            if low == "tr":
                self._in_row = True
                self._row_cells = []
                return
            if low in {"td", "th"} and self._in_row:
                self._in_cell = True
                self._cell_parts = []
                return

        if low == "a" and self._anchor_href is None:
            href = _sanitize_markdown_href(attrs_dict.get("href") or "")
            self._anchor_href = href
            self._anchor_parts = []
            return

        if self._table_depth > 0:
            if low in {"br", "tr", "td", "th", "p", "div", "li"} and self._in_cell and not vis:
                self._cell_parts.append(" ")
            return

        if low in _HTML_HEADING_TAGS and self._heading_level == 0:
            self._heading_level = int(low[1])
            self._heading_parts = []
            return
        if low in {"br", "hr"} and not vis:
            self._emit_text("\n")
            return
        if low == "li" and not vis:
            self.tokens.append("\n- ")
            return
        if low in _HTML_BLOCK_TAGS and not vis:
            self.tokens.append("\n")

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        low = tag.lower()
        if low in _HTML_VOID_TAGS:
            self.handle_starttag(tag, attrs)
            return
        self.handle_starttag(tag, attrs)
        self.handle_endtag(tag)

    def handle_endtag(self, tag: str) -> None:
        low = tag.lower()
        if low in _HTML_VOID_TAGS:
            return
        # Pop matching tag (tolerant of mildly unbalanced HTML).
        match_idx = -1
        for i in range(len(self._stack) - 1, -1, -1):
            if self._stack[i][0] == low:
                match_idx = i
                break
        if match_idx < 0:
            return
        while len(self._stack) > match_idx:
            popped_tag, popped_hard, popped_vis = self._stack.pop()
            if self._skip_depth > 0:
                self._skip_depth -= 1
                continue
            if popped_hard:
                continue
            if popped_tag == "pre" and self._pre_depth > 0:
                self._pre_depth = 0
                code = "".join(self._pre_parts).strip("\n")
                self._pre_parts = []
                if code:
                    self.code_blocks.append(code)
                    self.tokens.append(f"\n\n\x00CODE{len(self.code_blocks) - 1}\x00\n\n")
                continue
            if self._pre_depth > 0:
                continue
            if popped_tag == "a" and self._anchor_href is not None:
                label = " ".join("".join(self._anchor_parts).split())
                href = self._anchor_href
                self._anchor_href = None
                self._anchor_parts = []
                if label:
                    rendered_link = f"[{label}]({href})" if href else label
                    self._emit_text(rendered_link)
                continue
            if popped_tag == "table" and self._table_depth > 0:
                self._table_depth -= 1
                if self._table_depth == 0:
                    rendered = _format_markdown_table(self._table_rows)
                    self._table_rows = []
                    self._in_row = False
                    self._row_cells = []
                    self._in_cell = False
                    self._cell_parts = []
                    if rendered:
                        self.tables.append(rendered)
                        self.tokens.append(f"\n\n\x00TABLE{len(self.tables) - 1}\x00\n\n")
                continue
            if self._table_depth == 1:
                if popped_tag in {"td", "th"} and self._in_cell:
                    raw_cell = " ".join("".join(self._cell_parts).split()).replace("|", r"\|")
                    self._row_cells.append(raw_cell)
                    self._in_cell = False
                    self._cell_parts = []
                elif popped_tag == "tr" and self._in_row:
                    if self._row_cells:
                        self._table_rows.append(self._row_cells)
                    self._in_row = False
                    self._row_cells = []
                continue
            if popped_tag in _HTML_HEADING_TAGS and self._heading_level > 0:
                title = " ".join("".join(self._heading_parts).split())
                level = self._heading_level
                self._heading_level = 0
                self._heading_parts = []
                if title:
                    self.tokens.append(f"\n{'#' * level} {title}\n")
                continue
            if (popped_tag in _HTML_BLOCK_TAGS or popped_tag == "li") and not popped_vis:
                self.tokens.append("\n")

    def handle_data(self, data: str) -> None:
        hard, vis = self._cur_hidden()
        if hard or vis:
            return
        self._emit_text(data)


def _format_markdown_table(rows: list[list[str]]) -> str:
    if not rows:
        return ""
    cols = max(len(cells) for cells in rows)
    if cols <= 0:
        return ""
    lines: list[str] = []
    for idx, cells in enumerate(rows):
        padded = cells + [""] * (cols - len(cells))
        lines.append("| " + " | ".join(padded) + " |")
        if idx == 0:
            lines.append("| " + " | ".join("---" for _ in range(cols)) + " |")
    return "\n".join(lines)


def as_text(value: Any) -> str:
    return "" if value is None else str(value)


def html_to_markdown(html: str) -> str:
    """Convert HTML to a small markdown subset using only the stdlib.

    Supports headings, links, list items, GFM tables (including nested tables),
    ``<br>``, block boundaries, and fenced code blocks while stripping hidden
    subtrees (``[hidden]``, ``display:none``, ``visibility:hidden|collapse``,
    ``opacity:0``, ``content-visibility:hidden``) and scrubbing link URLs.
    """
    parser = _MarkdownHTMLParser()
    parser.feed(html or "")
    parser.close()

    text = "".join(parser.tokens)
    text = _SPACES_RE.sub(" ", text)
    text = "\n".join(line.strip() for line in text.splitlines())
    text = _BLANK_RE.sub("\n\n", text).strip()
    for index, table_md in enumerate(parser.tables):
        text = text.replace(f"\x00TABLE{index}\x00", table_md)
    for index, code in enumerate(parser.code_blocks):
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
        if getattr(node, "css_hidden", False):
            continue
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
    f"function(off, maxChars){{{_DEEP_TEXT_JS}{_CP_SLICE_JS}"
    " const sl = cpSlice(deepText(this), off, maxChars);"
    " sl.hidden = isAncestorHidden(this);"
    " return sl;}"
)
_MARKDOWN_SLICE_FN = (
    f"function(off, maxChars){{{_MARKDOWN_JS}{_CP_SLICE_JS} return cpSlice(deepMarkdown(this), off, maxChars);}}"
)
_HTML_SLICE_FN = f"function(off, maxChars){{{_CP_SLICE_JS} return cpSlice(this.outerHTML || '', off, maxChars);}}"


def _format_slice(total_len: int, chunk: str, offset: int, max_chars: int) -> str:
    if offset and offset >= total_len:
        return f"offset={offset} 已超出内容长度 {total_len}（没有更多内容）"
    end = offset + len(chunk)
    if end < total_len:
        return chunk + f"… (+{total_len - end} more chars; next offset={end})"
    return chunk


async def _extract_by_element_id(
    session: Any,
    element_id: str,
    fmt: str,
    *,
    offset: int = 0,
    max_chars: int | None = None,
) -> str | None:
    """Read-only fast path for ``#element-id`` queries that LLMs commonly pass to extract."""
    id_js = json.dumps(element_id)
    if fmt == "markdown":
        if max_chars is not None:
            expr = (
                f"(()=>{{{_MARKDOWN_JS}{_CP_SLICE_JS} const el=document.getElementById({id_js});"
                f" return el ? {{found:true, slice:cpSlice(deepMarkdown(el), {offset}, {max_chars})}} : {{found:false}}; }})()"
            )
            res = await eval_value(session, expr)
            if not isinstance(res, dict) or not res.get("found"):
                return None
            sl = res.get("slice")
            if isinstance(sl, dict) and "chunk" in sl and "len" in sl:
                return _format_slice(int(sl["len"]), as_text(sl["chunk"]), offset, max_chars)
            md = html_to_markdown(as_text(res.get("html") if "html" in res else sl))
            return page_of(md, offset, max_chars)
        expr = (
            f"(()=>{{{_MARKDOWN_JS} const el=document.getElementById({id_js});"
            " return el ? {found:true, md:deepMarkdown(el)} : {found:false}; })()"
        )
        res = await eval_value(session, expr)
        if not isinstance(res, dict) or not res.get("found"):
            return None
        if "md" in res:
            return as_text(res.get("md"))
        return html_to_markdown(as_text(res.get("html")))
    if fmt == "html":
        if max_chars is not None:
            expr = (
                f"(()=>{{{_CP_SLICE_JS} const el=document.getElementById({id_js});"
                f" return el ? {{found:true, slice:cpSlice(el.outerHTML||'', {offset}, {max_chars})}} : {{found:false}}; }})()"
            )
            res = await eval_value(session, expr)
            if not isinstance(res, dict) or not res.get("found"):
                return None
            sl = res.get("slice")
            if isinstance(sl, dict) and "chunk" in sl and "len" in sl:
                return _format_slice(int(sl["len"]), as_text(sl["chunk"]), offset, max_chars)
            return page_of(as_text(sl), offset, max_chars)
        expr = (
            f"(()=>{{ const el=document.getElementById({id_js});"
            " return el ? {found:true, html:el.outerHTML||''} : {found:false}; })()"
        )
        res = await eval_value(session, expr)
        if not isinstance(res, dict) or not res.get("found"):
            return None
        return as_text(res.get("html"))
    if max_chars is not None:
        expr = (
            f"(()=>{{{_DEEP_TEXT_JS}{_CP_SLICE_JS} const el=document.getElementById({id_js});"
            f" return el ? {{found:true, slice:cpSlice(deepText(el), {offset}, {max_chars})}} : {{found:false}}; }})()"
        )
        res = await eval_value(session, expr)
        if not isinstance(res, dict) or not res.get("found"):
            return None
        sl = res.get("slice")
        if isinstance(sl, dict) and "chunk" in sl and "len" in sl:
            return _format_slice(int(sl["len"]), as_text(sl["chunk"]), offset, max_chars)
        return page_of(as_text(sl), offset, max_chars)
    expr = (
        f"(()=>{{{_DEEP_TEXT_JS} const el=document.getElementById({id_js});"
        " return el ? {found:true, text:deepText(el)} : {found:false}; })()"
    )
    res = await eval_value(session, expr)
    if not isinstance(res, dict) or not res.get("found"):
        return None
    return as_text(res.get("text"))


_EXTRACT_LANDMARKS = frozenset({"main", "article", "dialog"})
_REF_SPEC_RE = re.compile(r"^@e\d+#\d+$")


async def _refresh_snapshot_preserving_sig(session: Any) -> None:
    from . import snapshot

    prev_sig = getattr(session, "_last_snapshot_sig", None)
    try:
        await snapshot.run(session, {"scope": "viewport", "interactive_only": False})
    finally:
        try:
            session._last_snapshot_sig = prev_sig
        except (AttributeError, TypeError):
            pass


async def _extract_entry(
    session: Any,
    entry: Any,
    fmt: str,
    *,
    offset: int = 0,
    max_chars: int | None = None,
) -> str:
    object_id = await session.resolve_object_id(entry)
    cdp_session_id = entry.cdp_session_id
    if fmt == "markdown":
        if max_chars is not None:
            result = await call_value(
                session,
                object_id,
                _MARKDOWN_SLICE_FN,
                cdp_session_id=cdp_session_id,
                arguments=[{"value": offset}, {"value": max_chars}],
            )
            value = result.get("value")
            if isinstance(value, dict) and "chunk" in value and "len" in value:
                return _format_slice(int(value["len"]), as_text(value["chunk"]), offset, max_chars)
            md = html_to_markdown(as_text(value))
            return page_of(md, offset, max_chars)
        result = await call_value(session, object_id, _MARKDOWN_FN, cdp_session_id=cdp_session_id)
        value = result.get("value")
        text = as_text(value)
        if "<" in text and ">" in text:
            return html_to_markdown(text)
        return text
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
            is_hidden = bool(value.get("hidden"))
            if fmt == "text" and total == 0 and not is_hidden:
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
        value = result.get("value")
        if isinstance(value, dict) and "text" in value:
            text = as_text(value.get("text"))
            is_hidden = bool(value.get("hidden"))
        else:
            text = as_text(value)
            is_hidden = False
        if not text.strip() and not is_hidden:
            fallback = _snapshot_subtree_text(session, entry)
            if fallback:
                return fallback
        return text
    result = await call_value(session, object_id, _HTML_FN, cdp_session_id=cdp_session_id)
    return as_text(result.get("value"))


async def _try_unique_landmark(session: Any, query: str) -> Any | None:
    """Resolve a bare landmark name (``main`` / ``article`` / ``dialog``) to its unique visible landmark."""
    wanted = query.strip().lower()
    if wanted not in _EXTRACT_LANDMARKS:
        return None
    if snapshot_needs_refresh(session):
        await _refresh_snapshot_preserving_sig(session)
    nodes: list[FusedNode] = list(getattr(getattr(session, "snapshot", None), "nodes", ()) or ())
    matches = [
        node
        for node in nodes
        if getattr(node, "visible", False)
        and not getattr(node, "css_hidden", False)
        and getattr(node, "ref", None) is not None
        and getattr(node, "epoch", None) is not None
        and wanted in {str(getattr(node, "role", "")).lower(), str(getattr(node, "tag", "")).lower()}
    ]
    if len(matches) == 1:
        sole = matches[0]
        return session.resolve_target(f"@e{sole.ref}#{sole.epoch}")
    return None


async def _extract_multiple(
    session: Any,
    query: str,
    fmt: str,
    *,
    max_matches: int,
    offset: int = 0,
    max_chars: int = 7000,
) -> str:
    """Read up to *max_matches* ranked candidates for *query* in one call within *max_chars*."""
    if snapshot_needs_refresh(session):
        await _refresh_snapshot_preserving_sig(session)
    nodes: list[FusedNode] = list(getattr(getattr(session, "snapshot", None), "nodes", ()) or ())
    candidates = [
        cand
        for cand in rank(nodes, query)
        if getattr(cand.node, "ref", None) is not None and getattr(cand.node, "epoch", None) is not None
    ]
    if not candidates:
        raise TargetNotFound("未找到匹配的元素，请重新 snapshot 后重试")
    picked = candidates[: max(1, min(5, int(max_matches)))]
    nodes_by_key = build_node_index(nodes)
    # Keep per-item cap independent of *offset* so outer pagination offsets stay deterministic.
    per_item_limit = max(400, min(3500, 7000 // max(1, len(picked))))
    sections: list[str] = []
    for idx, cand in enumerate(picked, 1):
        node = cand.node
        token = f"@e{node.ref}#{node.epoch}"
        try:
            entry = session.resolve_target(token)
            body = await _extract_entry(session, entry, fmt, offset=0, max_chars=per_item_limit)
        except StaleRef:
            if len(picked) == 1:
                raise
            continue
        body = _INNER_NEXT_OFFSET_RE.sub(
            lambda m, tok=token: f"… (+{m.group(1)} more chars; use query={tok} for full item)",
            body,
        )
        label = escape_label(node.name or node.text_hint, limit=80)
        role = sanitize_role(node.role)
        vis = "in-view" if node.in_viewport else "off-screen"
        region = describe_region(node, nodes_by_key)
        where = f" in {region}" if region else ""
        sections.append(f'[match {idx}/{len(picked)}] {token} {role} "{label}" [{vis}]{where}\n{body}')
    if not sections:
        raise StaleRef("所选元素均已失效，请重新 browser_snapshot", reason="node_detached")
    combined = "\n\n".join(sections)
    return page_of(combined, offset, max_chars)


async def _extract_element(
    session: Any,
    query: str,
    fmt: str,
    *,
    offset: int = 0,
    max_chars: int | None = None,
    max_matches: int = 1,
) -> str:
    id_match = _SIMPLE_ID_RE.match(query)
    if id_match is not None:
        direct = await _extract_by_element_id(session, id_match.group(1), fmt, offset=offset, max_chars=max_chars)
        if direct is not None:
            return direct
    if max_matches > 1 and not _REF_SPEC_RE.match(query):
        return await _extract_multiple(
            session,
            query,
            fmt,
            max_matches=max_matches,
            offset=offset,
            max_chars=max_chars if max_chars is not None else 7000,
        )
    landmark = await _try_unique_landmark(session, query)
    if landmark is not None:
        return await _extract_entry(session, landmark, fmt, offset=offset, max_chars=max_chars)
    try:
        entry = await resolve_target(session, query)
    except TargetAmbiguous as exc:
        # Read-only extract is safe to proceed when scoring found a single unambiguous element.
        if len(exc.candidates) == 1:
            sole = exc.candidates[0].node
            if sole.ref is not None and sole.epoch is not None:
                entry = session.resolve_target(f"@e{sole.ref}#{sole.epoch}")
            else:
                raise
        elif _CSS_LIKE_RE.match(query):
            hint = "提示：browser_extract 不支持复杂 CSS 选择器；请省略 query 读整页，或传入 @eN#epoch / #element-id"
            raise TargetAmbiguous(
                f"{exc.message}；{hint}",
                candidates=exc.candidates,
                details=f"{exc.details}\n{hint}" if exc.details else hint,
            ) from exc
        else:
            batch_hint = "提示：若需一次读取前多个匹配项，可传入 max_matches=3（上限 5）"
            raise TargetAmbiguous(
                exc.message,
                candidates=exc.candidates,
                details=f"{exc.details}\n{batch_hint}" if exc.details else batch_hint,
            ) from exc
    except TargetNotFound as exc:
        if _CSS_LIKE_RE.match(query):
            raise TargetNotFound(
                f"{exc}（提示：browser_extract 不支持复杂 CSS 选择器；请省略 query 读整页，或传入 @eN#epoch / #element-id）"
            ) from exc
        raise
    return await _extract_entry(session, entry, fmt, offset=offset, max_chars=max_chars)


async def _extract_page(
    session: Any,
    fmt: str,
    *,
    offset: int = 0,
    max_chars: int | None = None,
) -> str:
    if fmt == "markdown":
        if max_chars is not None:
            expr = (
                f"(()=>{{{_MARKDOWN_JS}{_CP_SLICE_JS}"
                " const s = document.body ? deepMarkdown(document.body) : deepMarkdown(document.documentElement);"
                f" return cpSlice(s, {offset}, {max_chars});"
                "})()"
            )
            value = await eval_value(session, expr)
            if isinstance(value, dict) and "chunk" in value and "len" in value:
                return _format_slice(int(value["len"]), as_text(value["chunk"]), offset, max_chars)
            md = html_to_markdown(as_text(value))
            return page_of(md, offset, max_chars)
        value = await eval_value(session, _PAGE_MARKDOWN_EXPR)
        text = as_text(value)
        if "<" in text and ">" in text:
            return html_to_markdown(text)
        return text
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
    max_matches = int(arguments.get("max_matches", 1))

    if query:
        content = await _extract_element(
            session, query, fmt, offset=offset, max_chars=max_chars, max_matches=max_matches
        )
    else:
        content = await _extract_page(session, fmt, offset=offset, max_chars=max_chars)
    session.note_success(session.target_id)
    return [text_block(content)]
