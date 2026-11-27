# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""``browser_query`` - default-enabled safe read-only structured DOM query.

Extracts structured JSON records (cards, product grids, tables, links, exact
strings) via declarative CSS selectors and an allowlisted field spec — with
zero arbitrary JavaScript execution, zero DOM mutation, WASP hidden-CSS
filtering, password redaction, and secret scrubbing.
"""

from __future__ import annotations

import json
import re
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from ..errors import BadRequest
from ..page_log import (
    _AUTH_HEADER_RE,
    _AUTH_SCHEME_RE,
    _BARE_JWT_RE,
    _COOKIE_HEADER_RE,
    _DIGEST_AUTH_RE,
    _SECRET_RE,
    _USERINFO_RE,
    _mask_auth_scheme,
)
from ..types import ToolBlock, text_block
from ._shared import call_value, eval_value, is_oopif_visible

NAME = "browser_query"

DESCRIPTION = (
    "Safe, read-only structured DOM query (default-enabled, zero JS execution). "
    "Queries elements matching a CSS selector (piercing open Shadow DOM) and returns "
    "bounded JSON items with deterministic total_matched / returned / next_offset counts. "
    "Pass fields as {output_key: spec} to extract structured records per item in one call: "
    "'h3' (visible text), '.price@exact_text' (exact whitespace/NBSP), 'a@href', 'img@src', "
    "'@data-asin' (attribute on the item itself), '.badge@count' (descendant count), or "
    "'td[]' / 'a[]@href' (array of up to 20 matches). Omit fields for a default summary. "
    "Optionally scope with root (CSS selector or @eN#epoch ref, including OOPIFs). "
    "CSS-hidden elements are excluded by default (visible_only=true), password inputs and "
    "sensitive URL credentials/tokens are redacted, and output is always valid JSON."
)

PARAMS: dict[str, Any] = {
    "selector": {
        "type": "string",
        "required": True,
        "maxLength": 500,
        "description": "CSS selector matching each item/row/element to query (pierces open Shadow DOM)",
    },
    "fields": {
        "type": "object",
        "maxProperties": 16,
        "additionalProperties": {"type": "string", "maxLength": 300},
        "description": (
            "Optional mapping of output key -> relative extraction spec, e.g. "
            "{'title': 'h3', 'price': '.price@exact_text', 'url': 'a@href', 'cells': 'td[]'}"
        ),
    },
    "root": {
        "type": "string",
        "maxLength": 500,
        "description": "Optional container scope: CSS selector (e.g. 'main', '#results') or @eN#epoch ref",
    },
    "visible_only": {
        "type": "boolean",
        "default": True,
        "description": "Exclude CSS-hidden elements (display:none / visibility:hidden / opacity:0 / hidden)",
    },
    "limit": {"type": "integer", "min": 1, "max": 50, "default": 20},
    "offset": {"type": "integer", "min": 0, "default": 0},
    "max_chars": {"type": "integer", "min": 100, "max": 7000, "default": 7000},
}

_REF_SPEC_RE = re.compile(r"^@e\d+#\d+$")
_FIELD_KEY_RE = re.compile(r"^[A-Za-z0-9_.-]{1,48}$")
_RESERVED_FIELD_KEYS = frozenset({"__proto__", "constructor", "prototype", "index"})
_ATTR_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,47}$")
_DATA_ATTR_RE = re.compile(r"^data-[a-z0-9_-]{1,40}$")
_SENSITIVE_DATA_FRAGMENTS = (
    "token",
    "secret",
    "password",
    "passwd",
    "auth",
    "cookie",
    "session",
    "csrf",
    "xsrf",
    "apikey",
    "api-key",
    "credential",
    "private",
    "jwt",
    "bearer",
)
_ALLOWED_MODES = frozenset({"text", "exact_text", "tag", "count"})
_ALLOWED_ATTRS = frozenset(
    {
        "href",
        "src",
        "alt",
        "title",
        "id",
        "class",
        "name",
        "role",
        "type",
        "lang",
        "dir",
        "datetime",
        "placeholder",
        "value",
        "checked",
        "selected",
        "disabled",
        "readonly",
        "colspan",
        "rowspan",
        "aria-label",
        "aria-expanded",
        "aria-selected",
        "aria-checked",
        "aria-disabled",
        "aria-current",
        "aria-valuenow",
        "aria-valuetext",
        "aria-valuemin",
        "aria-valuemax",
        "itemprop",
        "itemtype",
        "content",
        "rel",
        "target",
    }
)
_URL_ATTRS = frozenset({"href", "src"})
_MAX_PER_FIELD_ARRAY = 20
_MAX_SCAN_ELEMENTS = 2000
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f\ud800-\udfff]")


def _split_field_spec(raw: str) -> tuple[str, str]:
    """Split *raw* into ``(selector_part, attr_or_mode)`` on the last ``@`` outside ``[...]``."""
    depth = 0
    in_quote = ""
    split_at = -1
    for idx, ch in enumerate(raw):
        if in_quote:
            if ch == in_quote:
                in_quote = ""
            continue
        if ch in ("'", '"'):
            in_quote = ch
        elif ch == "[":
            depth += 1
        elif ch == "]" and depth > 0:
            depth -= 1
        elif ch == "@" and depth == 0:
            split_at = idx
    if split_at < 0:
        return raw.strip(), "text"
    return raw[:split_at].strip(), raw[split_at + 1 :].strip().lower()


def _validate_attr_or_mode(key: str, target: str) -> tuple[str, str]:
    """Return ``(mode, attr)`` or raise :class:`BadRequest` for disallowed attributes."""
    if not target or target == "text":
        return "text", ""
    if target in _ALLOWED_MODES:
        return target, ""
    if not _ATTR_NAME_RE.fullmatch(target):
        raise BadRequest(f"字段 {key!r} 的属性/模式 @{target} 格式非法")
    if target in _ALLOWED_ATTRS:
        return "attr", target
    if _DATA_ATTR_RE.fullmatch(target) and not any(bad in target for bad in _SENSITIVE_DATA_FRAGMENTS):
        return "attr", target
    raise BadRequest(
        f"字段 {key!r} 的属性 @{target} 不在安全只读白名单内（允许 text/exact_text/tag/count、"
        "href/src/alt/title/id/class/role/aria-*/datetime/value 及非敏感 data-*）"
    )


def parse_fields(fields: dict[str, str] | None) -> list[dict[str, Any]]:
    """Compile and security-validate declarative field specs before touching the page."""
    if not fields:
        return []
    parsed: list[dict[str, Any]] = []
    for key, raw_spec in fields.items():
        if not _FIELD_KEY_RE.fullmatch(key):
            raise BadRequest(f"字段名 {key!r} 非法（仅允许 1-48 位字母/数字/._-）")
        if key in _RESERVED_FIELD_KEYS:
            raise BadRequest(f"字段名 {key!r} 为保留键，不能作为提取字段名")
        spec = (raw_spec or "").strip()
        if not spec:
            raise BadRequest(f"字段 {key!r} 的提取规则不能为空")
        sel_part, attr_or_mode = _split_field_spec(spec)
        multi = False
        if sel_part.endswith("[]"):
            multi = True
            sel_part = sel_part[:-2].strip()
            if not sel_part:
                raise BadRequest(f"字段 {key!r} 使用 [] 数组模式时必须提供子选择器（如 'td[]' 或 'a[]@href'）")
        mode, attr = _validate_attr_or_mode(key, attr_or_mode)
        if mode == "count" and multi:
            raise BadRequest(f"字段 {key!r} 的 @count 与 [] 不能同时使用（@count 已返回匹配总数）")
        if mode == "count" and not sel_part:
            raise BadRequest(f"字段 {key!r} 使用 @count 时必须提供子选择器")
        parsed.append(
            {
                "key": key,
                "selector": sel_part,
                "mode": mode,
                "attr": attr,
                "multi": multi,
            }
        )
    return parsed


#: Hardened read-only page-side extractor. Never invokes eval/Function, never
#: mutates DOM, never accesses cookies/storage/network, skips script/style/head,
#: filters CSS-hidden text (WASP defense), and redacts password input values.
_QUERY_ENGINE_JS = (
    "const SKIP=new Set(['SCRIPT','STYLE','NOSCRIPT','TEMPLATE','HEAD','TITLE','META','LINK','BASE']);"
    "const BLOCK=new Set(['DIV','P','LI','TR','SECTION','ARTICLE','HEADER','FOOTER','MAIN','ASIDE','NAV',"
    "'H1','H2','H3','H4','H5','H6','BLOCKQUOTE','PRE','UL','OL','BR','HR']);"
    "const CELL=new Set(['TD','TH']);"
    "const MEDIA=new Set(['IMG','SVG','CANVAS','VIDEO','AUDIO','PICTURE']);"
    "const BOOL_ATTRS=new Set(['checked','selected','disabled','readonly']);"
    "const SENSITIVE_INPUT_RE=/password|passwd|token|secret|csrf|xsrf|auth|cookie|session|apikey|api-key|credential|private|jwt|bearer|cc-number|cc-csc/i;"
    "const tagOf=(el)=>String(el&&el.tagName||'').toUpperCase();"
    "const isSkippedTag=(el)=>!el||el.nodeType!==1||SKIP.has(tagOf(el));"
    "const isSensitiveInput=(el)=>{"
    " if(tagOf(el)!=='INPUT') return false;"
    " const tp=String(el.getAttribute('type')||el.type||'').toLowerCase();"
    " if(tp==='password'||tp==='hidden') return true;"
    " const meta=[el.getAttribute('name')||'',el.getAttribute('id')||'',el.getAttribute('autocomplete')||''].join(' ');"
    " return SENSITIVE_INPUT_RE.test(meta);"
    "};"
    "const styleOf=(el)=>{"
    " const win=el&&el.ownerDocument&&el.ownerDocument.defaultView;"
    " return (win&&typeof win.getComputedStyle==='function')?win.getComputedStyle(el):getComputedStyle(el);"
    "};"
    "const isZeroClip=(st)=>{"
    " const clip=String(st.clip||'').replace(/\\s+/g,'').toLowerCase();"
    " if(clip&&clip!=='auto'){"
    "  const m=clip.match(/^rect\\(([^,]+),([^,]+),([^,]+),([^)]+)\\)$/);"
    "  if(m){"
    "   const top=parseFloat(m[1]),right=parseFloat(m[2]),bottom=parseFloat(m[3]),left=parseFloat(m[4]);"
    "   if(!Number.isNaN(top)&&!Number.isNaN(right)&&!Number.isNaN(bottom)&&!Number.isNaN(left)&&(right<=left||bottom<=top)) return true;"
    "  }"
    " }"
    " const cp=String(st.clipPath||'').replace(/\\s+/g,'').toLowerCase();"
    " if(cp&&cp!=='none'){"
    "  if(/^inset\\((5[0-9]|[6-9][0-9]|100)%/.test(cp)||/^circle\\(0(%|px|em|rem)?(\\)|at)/.test(cp)||/^ellipse\\(0/.test(cp)||cp==='polygon(0px0px,0px0px,0px0px)') return true;"
    " }"
    " return false;"
    "};"
    "const isStyleHidden=(el)=>{"
    " if(!el||el.nodeType!==1) return false;"
    " const tag=tagOf(el);"
    " if(SKIP.has(tag)||el.hidden===true) return true;"
    " try{"
    "  const st=styleOf(el);"
    "  const op=parseFloat(st.opacity);"
    "  if(st.display==='none'||st.visibility==='hidden'||st.visibility==='collapse'||st.contentVisibility==='hidden'||(!Number.isNaN(op)&&op<=0)) return true;"
    "  if(isZeroClip(st)) return true;"
    "  if(st.display!=='contents'&&tag!=='OPTION'&&tag!=='AREA'){"
    "   const r=el.getBoundingClientRect();"
    "   const ov=String(st.overflow||'').toLowerCase();"
    "   const ovx=String(st.overflowX||'').toLowerCase();"
    "   const ovy=String(st.overflowY||'').toLowerCase();"
    "   const clips=(ov&&ov!=='visible')||(ovx&&ovx!=='visible')||(ovy&&ovy!=='visible');"
    "   if(r.width<=0&&r.height<=0) return true;"
    "   if((r.width<=0||r.height<=0)&&(clips||el.children.length===0)) return true;"
    "   const fs=parseFloat(st.fontSize);"
    "   if(!Number.isNaN(fs)&&fs<=0&&!MEDIA.has(tag)&&(!el.querySelector||!el.querySelector('img,svg,canvas,video'))) return true;"
    "  }"
    " }catch(_){}"
    " return false;"
    "};"
    "const isHidden=(el)=>{"
    " let cur=el;"
    " while(cur){"
    "  if(cur.nodeType===1){"
    "   const tag=tagOf(cur);"
    "   if(SKIP.has(tag)||cur.hidden===true) return true;"
    "   try{"
    "    const st=styleOf(cur);"
    "    const op=parseFloat(st.opacity);"
    "    if(st.display==='none'||st.visibility==='hidden'||st.visibility==='collapse'||st.contentVisibility==='hidden'||(!Number.isNaN(op)&&op<=0)) return true;"
    "    if(isZeroClip(st)) return true;"
    "    if(cur!==el&&st.display!=='contents'){"
    "     const ov=String(st.overflow||'').toLowerCase();"
    "     const ovx=String(st.overflowX||'').toLowerCase();"
    "     const ovy=String(st.overflowY||'').toLowerCase();"
    "     const clips=(ov&&ov!=='visible')||(ovx&&ovx!=='visible')||(ovy&&ovy!=='visible');"
    "     if(clips){ const r=cur.getBoundingClientRect(); if(r.width<=0||r.height<=0) return true; }"
    "    }"
    "   }catch(_){}"
    "  }"
    "  if(cur.parentElement){ cur=cur.parentElement; }"
    "  else if(cur.getRootNode){"
    "   const rn=cur.getRootNode();"
    "   if(rn&&rn.host&&rn.host!==cur){ cur=rn.host; }"
    "   else{"
    "    try{ const win=cur.ownerDocument&&cur.ownerDocument.defaultView; cur=(win&&win.frameElement)||null; }"
    "    catch(_){ cur=null; }"
    "   }"
    "  }else{ cur=null; }"
    " }"
    " return isStyleHidden(el);"
    "};"
    "const deepQueryAll=(root,sel,maxScan,filterHidden)=>{"
    " const out=[]; const seen=new Set(); const seenRoots=new Set();"
    " const collect=(container)=>{"
    "  if(!container||out.length>=maxScan) return;"
    "  if(seenRoots.has(container)) return;"
    "  seenRoots.add(container);"
    "  if(typeof container.querySelectorAll==='function'){"
    "   const direct=container.querySelectorAll(sel);"
    "   for(const el of direct){"
    "    if(out.length>=maxScan) return;"
    "    if(!seen.has(el)&&!isSkippedTag(el)&&(!filterHidden||!isHidden(el))){ seen.add(el); out.push(el); }"
    "   }"
    "  }"
    "  if(container.shadowRoot) collect(container.shadowRoot);"
    "  if(typeof container.querySelectorAll==='function'){"
    "   const all=container.querySelectorAll('*');"
    "   for(const el of all){"
    "    if(out.length>=maxScan) return;"
    "    if(el.shadowRoot) collect(el.shadowRoot);"
    "   }"
    "  }"
    " };"
    " collect(root);"
    " return out;"
    "};"
    "const readText=(root,exact)=>{"
    " if(!root||isSkippedTag(root)) return '';"
    " if(isSensitiveInput(root)) return '[REDACTED]';"
    " const parts=[]; let rowCells=0;"
    " const walk=(n)=>{"
    "  if(!n) return;"
    "  if(n.nodeType===3){"
    "   const raw=String(n.nodeValue||'');"
    "   const t=exact?raw.replace(/[\\r\\n\\t]+/g,' '):raw.replace(/[\\t\\r\\n ]+/g,' ');"
    "   if(t) parts.push(t);"
    "   return;"
    "  }"
    "  if(n.nodeType===1){"
    "   if(isStyleHidden(n)) return;"
    "   if(isSensitiveInput(n)){ parts.push('[REDACTED]'); return; }"
    "   const tag=tagOf(n);"
    "   if(tag==='SLOT'&&typeof n.assignedNodes==='function'){"
    "    const assigned=n.assignedNodes({flatten:true});"
    "    if(assigned.length){ for(const a of assigned) walk(a); return; }"
    "   }"
    "   const isBlock=BLOCK.has(tag); const isCell=CELL.has(tag); const isRow=tag==='TR';"
    "   const prevCells=rowCells; if(isRow) rowCells=0;"
    "   if(isBlock&&parts.length&&parts[parts.length-1]!=='\\n') parts.push('\\n');"
    "   if(isCell){ if(rowCells>0) parts.push('\\t'); rowCells++; }"
    "   if(n.shadowRoot){ for(const c of n.shadowRoot.childNodes) walk(c); }"
    "   else{ for(const c of n.childNodes) walk(c); }"
    "   if(isRow) rowCells=prevCells;"
    "   if(isBlock&&parts.length&&parts[parts.length-1]!=='\\n') parts.push('\\n');"
    "   return;"
    "  }"
    "  if(n.childNodes){ for(const c of n.childNodes) walk(c); }"
    " };"
    " walk(root);"
    " const joined=parts.join('').replace(/\\n{3,}/g,'\\n\\n');"
    " return exact?joined.replace(/^[\\r\\n]+|[\\r\\n]+$/g,''):joined.replace(/^\\s+|\\s+$/g,'');"
    "};"
    "const readAttr=(el,attr)=>{"
    " if(!el||isSkippedTag(el)) return null;"
    " if(attr==='value'&&isSensitiveInput(el)) return '[REDACTED]';"
    " if(BOOL_ATTRS.has(attr)){"
    "  if(attr in el) return Boolean(el[attr]);"
    "  return el.hasAttribute(attr);"
    " }"
    " if(attr==='href'||attr==='src'){"
    "  const raw=el.getAttribute(attr);"
    "  if(raw==null) return null;"
    "  const low=String(raw).replace(/[\\x00-\\x20]+/g,'').toLowerCase();"
    "  if(low.startsWith('javascript:')||low.startsWith('vbscript:')) return '';"
    "  const resolved=(typeof el[attr]==='string'&&el[attr])?el[attr]:raw;"
    "  return String(resolved);"
    " }"
    " if(attr==='value'){"
    "  if('value' in el && el.value!==undefined && el.value!==null) return String(el.value);"
    "  return el.getAttribute('value');"
    " }"
    " const v=el.getAttribute(attr);"
    " return v===null?null:String(v);"
    "};"
    "const readOne=(el,f)=>{"
    " if(!el) return null;"
    " if(f.mode==='text') return readText(el,false);"
    " if(f.mode==='exact_text') return readText(el,true);"
    " if(f.mode==='tag') return tagOf(el).toLowerCase();"
    " if(f.mode==='attr') return readAttr(el,f.attr);"
    " return null;"
    "};"
    "const runQuery=(scope,cfg)=>{"
    " let root=scope;"
    " if(!root){"
    "  if(cfg.rootSelector){"
    "   try{ const found=deepQueryAll(document,cfg.rootSelector,1,cfg.visibleOnly); root=found[0]||null; }"
    "   catch(e){ return {error:'invalid_root',message:String(e&&e.message||e)}; }"
    "   if(!root) return {error:'root_not_found',url:location.href};"
    "  }else{ root=document; }"
    " }"
    " let filtered;"
    " try{ filtered=deepQueryAll(root,cfg.selector,cfg.maxScan,cfg.visibleOnly); }"
    " catch(e){ return {error:'invalid_selector',message:String(e&&e.message||e)}; }"
    " const total=filtered.length;"
    " const slice=filtered.slice(cfg.offset,cfg.offset+cfg.limit);"
    " const items=[];"
    " for(let i=0;i<slice.length;i++){"
    "  const el=slice[i];"
    "  const row=Object.create(null);"
    "  row.index=cfg.offset+i;"
    "  if(!cfg.fields||!cfg.fields.length){"
    "   const tag=tagOf(el);"
    "   row.tag=tag.toLowerCase();"
    "   const txt=readText(el,false);"
    "   if(txt) row.text=txt;"
    "   const id=el.getAttribute('id'); if(id) row.id=id;"
    "   if(tag==='A'||tag==='AREA'){ const h=readAttr(el,'href'); if(h) row.href=h; }"
    "   if(tag==='IMG'){ const s=readAttr(el,'src'); if(s) row.src=s; const a=readAttr(el,'alt'); if(a) row.alt=a; }"
    "  }else{"
    "   for(const f of cfg.fields){"
    "    if(!f.selector){"
    "     row[f.key]=readOne(el,f);"
    "    }else{"
    "     let sub;"
    "     const subCap=f.mode==='count'?cfg.maxScan:(f.multi?cfg.maxPerField:1);"
    "     try{ sub=deepQueryAll(el,f.selector,subCap,cfg.visibleOnly); }"
    "     catch(e){ return {error:'invalid_field_selector',field:f.key,message:String(e&&e.message||e)}; }"
    "     if(f.mode==='count'){ row[f.key]=sub.length; }"
    "     else if(f.multi){ row[f.key]=sub.slice(0,cfg.maxPerField).map((n)=>readOne(n,f)); }"
    "     else{ row[f.key]=sub.length?readOne(sub[0],f):null; }"
    "    }"
    "   }"
    "  }"
    "  items.push(row);"
    " }"
    " return {url:location.href,total_matched:total,scan_capped:filtered.length>=cfg.maxScan,items};"
    "};"
)

_QUERY_ON_NODE_FN = f"function(cfg){{{_QUERY_ENGINE_JS} return runQuery(this, cfg);}}"


_SENSITIVE_QUERY_KEYS = frozenset(
    {
        "token",
        "access_token",
        "id_token",
        "refresh_token",
        "auth",
        "authorization",
        "api_key",
        "apikey",
        "key",
        "secret",
        "password",
        "passwd",
        "pwd",
        "session",
        "session_id",
        "sid",
        "csrf",
        "xsrf",
        "jwt",
        "signature",
        "sig",
        "cookie",
    }
)


def _redact_text_preserving_spaces(text: str) -> str:
    """Redact credentials/tokens without collapsing NBSP or truncating normal content."""
    out = _USERINFO_RE.sub(r"\1", text)
    out = _DIGEST_AUTH_RE.sub(lambda match: f"{match.group(1)} ***", out)
    out = _COOKIE_HEADER_RE.sub(
        lambda match: f"{match.group(1)}: ***" if match.group(2) == ":" else f"{match.group(1)}=***",
        out,
    )
    out = _AUTH_HEADER_RE.sub(lambda match: f"{match.group(1)}=***", out)
    out = _SECRET_RE.sub(lambda match: f"{match.group(1)}=***", out)
    out = _AUTH_SCHEME_RE.sub(_mask_auth_scheme, out)
    out = _BARE_JWT_RE.sub("***", out)
    return _CONTROL_RE.sub("", out)


def _clean_query_string(query: str) -> str:
    if not query:
        return ""
    pairs = parse_qsl(query, keep_blank_values=True)
    cleaned_pairs = [
        (
            k,
            "***"
            if (
                k.lower() in _SENSITIVE_QUERY_KEYS
                or any(s in k.lower() for s in ("token", "secret", "passwd", "password", "csrf", "xsrf", "apikey"))
            )
            else v,
        )
        for k, v in pairs
    ]
    return urlencode(cleaned_pairs, doseq=True)


def _clean_url(raw: str) -> str:
    """Strip userinfo and sensitive query params while preserving normal navigation/product URLs."""
    text = _CONTROL_RE.sub("", raw or "").strip()
    if not text:
        return ""
    scheme_probe = re.sub(r"[\t\r\n ]+", "", text).lower()
    if scheme_probe.startswith(("javascript:", "vbscript:")):
        return ""
    if scheme_probe.startswith("data:"):
        return "data:"
    if scheme_probe.startswith("blob:"):
        return "blob:"
    try:
        parts = urlsplit(text)
    except ValueError:
        return _redact_text_preserving_spaces(text)
    scheme_low = parts.scheme.lower()
    if scheme_low in {"http", "https"} or (not scheme_low and (parts.hostname or parts.query or parts.fragment)):
        netloc = parts.netloc
        if parts.hostname:
            host = f"[{parts.hostname}]" if ":" in parts.hostname else parts.hostname
            try:
                port = parts.port
            except ValueError:
                port = None
            netloc = f"{host}:{port}" if port is not None else host
        query = _clean_query_string(parts.query)
        frag = "" if (parts.fragment and len(parts.fragment) >= 16 and "=" in parts.fragment) else parts.fragment
        text = urlunsplit((parts.scheme, netloc, parts.path, query, frag))
    return _redact_text_preserving_spaces(text)


def _sanitize_value(value: Any, *, is_url: bool = False) -> Any:
    if isinstance(value, str):
        return _clean_url(value) if is_url else _redact_text_preserving_spaces(value)
    if isinstance(value, list):
        return [_sanitize_value(item, is_url=is_url) for item in value]
    if isinstance(value, dict):
        return {str(k): _sanitize_value(v, is_url=(k in _URL_ATTRS)) for k, v in value.items()}
    return value


def _shrink_single_item(item: dict[str, Any], cap: int) -> dict[str, Any]:
    """Trim overly long string values inside one item so even a single huge item fits *cap*."""
    shrunk: dict[str, Any] = {}
    str_cap = max(32, cap // max(2, len(item) + 1))
    elem_cap = max(24, str_cap // 2)
    for k, v in item.items():
        if isinstance(v, str) and len(v) > str_cap:
            shrunk[k] = v[:str_cap] + "…"
        elif isinstance(v, list):
            shrunk[k] = [x[:elem_cap] + "…" if isinstance(x, str) and len(x) > elem_cap else x for x in v[:5]]
        else:
            shrunk[k] = v
    return shrunk


def _pack_json_payload(
    *,
    url: str,
    selector: str,
    total_matched: int,
    offset: int,
    items: list[dict[str, Any]],
    max_chars: int,
    scan_capped: bool = False,
) -> str:
    """Serialize query results as strictly valid JSON bounded by *max_chars*."""
    kept = list(items)
    safe_url = url
    safe_selector = selector

    def _build(
        current: list[dict[str, Any]],
        *,
        item_trimmed: bool = False,
        item_too_large: bool = False,
    ) -> str:
        returned = len(current)
        has_more = offset + returned < total_matched or scan_capped or item_trimmed or item_too_large
        payload: dict[str, Any] = {
            "url": safe_url,
            "selector": safe_selector,
            "total_matched": total_matched,
            "offset": offset,
            "returned": returned,
            "truncated": has_more,
            "items": current,
        }
        if returned > 0 and offset + returned < total_matched:
            payload["next_offset"] = offset + returned
        if item_too_large:
            payload["item_too_large"] = True
        if scan_capped:
            payload["scan_capped"] = True
        return json.dumps(payload, ensure_ascii=False)

    def _fit_envelope(*, item_trimmed: bool = False, item_too_large: bool = False) -> None:
        nonlocal safe_url, safe_selector
        meta_cap = max(24, min(200, max_chars // 4))
        if len(_build([], item_trimmed=item_trimmed, item_too_large=item_too_large)) > max_chars:
            if len(safe_url) > meta_cap:
                safe_url = safe_url[: max(16, meta_cap - 1)] + "…"
            if (
                len(_build([], item_trimmed=item_trimmed, item_too_large=item_too_large)) > max_chars
                and len(safe_selector) > meta_cap
            ):
                safe_selector = safe_selector[: max(16, meta_cap - 1)] + "…"
            if len(_build([], item_trimmed=item_trimmed, item_too_large=item_too_large)) > max_chars:
                safe_url = ""
            if len(_build([], item_trimmed=item_trimmed, item_too_large=item_too_large)) > max_chars:
                safe_selector = safe_selector[:16]

    # Ensure envelope metadata never crowds out the JSON budget.
    _fit_envelope()

    rendered = _build(kept)
    if len(rendered) <= max_chars:
        return rendered
    while len(kept) > 1 and len(rendered) > max_chars:
        kept.pop()
        rendered = _build(kept)
    if len(rendered) <= max_chars:
        return rendered
    if kept:
        kept = [_shrink_single_item(kept[0], max(60, max_chars - len(_build([])) - 32))]
        rendered = _build(kept, item_trimmed=True)
        if len(rendered) <= max_chars:
            return rendered
        kept = []
    _fit_envelope(item_trimmed=bool(items), item_too_large=bool(items))
    return _build(kept, item_trimmed=bool(items), item_too_large=bool(items))


async def run(session: Any, arguments: dict[str, Any]) -> list[ToolBlock]:
    selector = (arguments.get("selector") or "").strip()
    if not selector:
        raise BadRequest("selector 不能为空")
    fields_raw = arguments.get("fields")
    compiled_fields = parse_fields(fields_raw if isinstance(fields_raw, dict) else None)
    root_spec = (arguments.get("root") or "").strip()
    visible_only = bool(arguments.get("visible_only", True))
    limit = int(arguments.get("limit", 20))
    offset = int(arguments.get("offset", 0))
    max_chars = int(arguments.get("max_chars", 7000))

    cfg_payload = {
        "selector": selector,
        "rootSelector": "" if _REF_SPEC_RE.match(root_spec) else root_spec,
        "fields": compiled_fields,
        "visibleOnly": visible_only,
        "limit": limit,
        "offset": offset,
        "maxPerField": _MAX_PER_FIELD_ARRAY,
        "maxScan": _MAX_SCAN_ELEMENTS,
    }

    if _REF_SPEC_RE.match(root_spec):
        entry = session.resolve_target(root_spec)
        if visible_only and entry.cdp_session_id and callable(getattr(session, "require_client", None)):
            oopif_vis = await is_oopif_visible(session, entry.cdp_session_id, fail_open=False)
            if oopif_vis is not True:
                payload = _pack_json_payload(
                    url=_clean_url(str(getattr(getattr(session, "snapshot", None), "url", "") or "")),
                    selector=selector,
                    total_matched=0,
                    offset=offset,
                    items=[],
                    max_chars=max_chars,
                )
                session.note_success(session.target_id)
                return [text_block(payload)]
        object_id = await session.resolve_object_id(entry)
        res = await call_value(
            session,
            object_id,
            _QUERY_ON_NODE_FN,
            cdp_session_id=entry.cdp_session_id,
            arguments=[{"value": cfg_payload}],
        )
        raw_result = res.get("value")
    else:
        expr = f"(()=>{{{_QUERY_ENGINE_JS} return runQuery(null, {json.dumps(cfg_payload, ensure_ascii=False)});}})()"
        raw_result = await eval_value(session, expr)

    if not isinstance(raw_result, dict):
        raise BadRequest("结构化查询未返回有效结果，请确认页面已加载后重试")

    err = raw_result.get("error")
    if err == "invalid_selector":
        raise BadRequest(f"CSS 选择器语法无效（{selector!r}）：{raw_result.get('message', '')}")
    if err == "invalid_root":
        raise BadRequest(f"root CSS 选择器语法无效（{root_spec!r}）：{raw_result.get('message', '')}")
    if err == "invalid_field_selector":
        raise BadRequest(f"字段 {raw_result.get('field')!r} 的 CSS 选择器语法无效：{raw_result.get('message', '')}")
    if err == "root_not_found":
        payload = _pack_json_payload(
            url=_clean_url(str(raw_result.get("url") or getattr(getattr(session, "snapshot", None), "url", "") or "")),
            selector=selector,
            total_matched=0,
            offset=offset,
            items=[],
            max_chars=max_chars,
        )
        session.note_success(session.target_id)
        return [text_block(payload)]

    url_field_keys = {f["key"] for f in compiled_fields if f["mode"] == "attr" and f["attr"] in _URL_ATTRS}
    raw_items = raw_result.get("items") or []
    sanitized_items: list[dict[str, Any]] = []
    for item in raw_items:
        if not isinstance(item, dict):
            continue
        clean_row: dict[str, Any] = {}
        for k, v in item.items():
            clean_row[str(k)] = _sanitize_value(v, is_url=(k in url_field_keys or k in _URL_ATTRS))
        sanitized_items.append(clean_row)

    page_url = _clean_url(str(raw_result.get("url") or getattr(getattr(session, "snapshot", None), "url", "") or ""))
    text = _pack_json_payload(
        url=page_url,
        selector=selector,
        total_matched=int(raw_result.get("total_matched") or 0),
        offset=offset,
        items=sanitized_items,
        max_chars=max_chars,
        scan_capped=bool(raw_result.get("scan_capped", False)),
    )
    session.note_success(session.target_id)
    return [text_block(text)]
