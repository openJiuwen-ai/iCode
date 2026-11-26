# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""``browser_find`` - locate elements without re-rendering the whole page."""

from __future__ import annotations

from typing import Any

from ..snapshot.scoring import build_node_index, describe_region
from ..snapshot.serializer import escape_label, is_hidden, sanitize_text
from ..types import ToolBlock, text_block
from ._shared import snapshot_needs_refresh

NAME = "browser_find"

DESCRIPTION = (
    "Find elements in the current snapshot by text, role, tag or href; returns local "
    "matches with full @eN#epoch refs, visibility, and region context. Use role to narrow broad "
    "queries (or pass query='' with a non-empty role to list elements of that role). Refreshes "
    "after an action dirties the snapshot; pass fresh=true to read the live page "
    "again after an asynchronous update."
)

PARAMS: dict[str, Any] = {
    "query": {
        "type": "string",
        "required": True,
        "description": "Case-insensitive text/name/tag/href query",
    },
    "role": {"type": "string", "default": "", "description": "Optional exact ARIA role"},
    "limit": {"type": "integer", "default": 20, "min": 1, "max": 50},
    "fresh": {"type": "boolean", "default": False, "description": "Refresh before searching"},
}


def _token(node: Any) -> str:
    if getattr(node, "ref", None) is None or getattr(node, "epoch", None) is None:
        return "(no ref)"
    return f"@e{node.ref}#{node.epoch}"


def _searchable(node: Any) -> str:
    return " ".join(
        str(part or "")
        for part in (
            getattr(node, "name", ""),
            getattr(node, "text_hint", ""),
            getattr(node, "placeholder", ""),
            getattr(node, "tag", ""),
            getattr(node, "role", ""),
            getattr(node, "href", ""),
        )
    ).casefold()


def _role_matches(node: Any, role: str) -> bool:
    if not role:
        return True
    node_role = str(getattr(node, "role", "")).casefold()
    node_tag = str(getattr(node, "tag", "")).casefold()
    if node_role == role:
        return True
    if role in {"img", "image"} and (node_role in {"img", "image"} or node_tag == "img"):
        return True
    return role == "textbox" and bool(getattr(node, "editable", False))


def _actionability_flags(node: Any) -> str:
    flags: list[str] = []
    if getattr(node, "editable", False):
        flags.append("editable")
    if getattr(node, "readonly", False):
        flags.append("readonly")
    if getattr(node, "disabled", False):
        flags.append("disabled")
    if getattr(node, "focusable", False):
        flags.append("focusable")
    if getattr(node, "in_viewport", False):
        flags.append("in-view")
    bbox = getattr(node, "bbox", None)
    has_box = isinstance(bbox, (tuple, list)) and len(bbox) == 4 and float(bbox[2]) > 0 and float(bbox[3]) > 0
    if not getattr(node, "has_layout", False) and not has_box:
        flags.append("no-layout")
    return f" [{' '.join(flags)}]" if flags else ""


def matching_nodes(nodes: list[Any], query: str, role: str = "") -> list[Any]:
    """Nodes whose name, text hint, placeholder, tag, role or href contains *query* (case-insensitive)."""
    needle = query.casefold()
    role = role.casefold()
    return [
        node
        for node in nodes
        if not is_hidden(node) and (not needle or needle in _searchable(node)) and _role_matches(node, role)
    ]


def _is_label_node(node: Any) -> bool:
    tag = str(getattr(node, "tag", "") or "").lower()
    role = str(getattr(node, "role", "") or "").lower()
    return tag == "label" or role in {"labeltext", "label"}


def _box_size_suffix(node: Any) -> str:
    tag = str(getattr(node, "tag", "") or "").lower()
    if tag not in {"canvas", "svg"}:
        return ""
    bbox = getattr(node, "bbox", None)
    if isinstance(bbox, (tuple, list)) and len(bbox) == 4 and float(bbox[2]) > 0 and float(bbox[3]) > 0:
        return f" size={round(float(bbox[2]))}x{round(float(bbox[3]))}"
    return ""


async def run(session: Any, arguments: dict[str, Any]) -> list[ToolBlock]:
    query = str(arguments.get("query", "")).strip()
    role = str(arguments.get("role", "")).strip().casefold()
    limit = max(1, min(50, int(arguments.get("limit", 20))))
    if not query and not role:
        from ..errors import BadRequest

        raise BadRequest("browser_find requires a non-empty query or role")
    if arguments.get("fresh") or snapshot_needs_refresh(session):
        from . import snapshot

        await snapshot.run(session, {"scope": "viewport", "interactive_only": False})
    nodes = list(session.snapshot.nodes)
    nodes_by_key = build_node_index(nodes)
    matches = matching_nodes(nodes, query, role)
    query_label = query or f"role={role}"
    if not matches:
        return [text_block(f'browser_find: no matches for "{query_label}"')]
    matches.sort(
        key=lambda node: (
            not getattr(node, "visible", False),
            not getattr(node, "in_viewport", False),
            _is_label_node(node),
            not getattr(node, "editable", False),
            not getattr(node, "focusable", False),
            not getattr(node, "interactive", False),
        )
    )
    lines = [f'browser_find: {len(matches)} match(es) for "{query_label}"']
    for node in matches[:limit]:
        raw_name = str(getattr(node, "name", "") or getattr(node, "text_hint", "") or "")
        name = escape_label(raw_name, limit=120)
        role_text = sanitize_text(str(getattr(node, "role", "") or "-"))
        tag_text = sanitize_text(str(getattr(node, "tag", "") or "-"))
        line = f"- {_token(node)} role={role_text} name={name or '-'} tag={tag_text}"
        raw_href = str(getattr(node, "href", "") or "")
        href = escape_label(raw_href, limit=120)
        if href and href != name:
            line += f" href={href}"
        line += _box_size_suffix(node)
        flags = _actionability_flags(node)
        if flags:
            line += flags
        region = describe_region(node, nodes_by_key)
        if region:
            line += f" in {region}"
        raw_placeholder = str(getattr(node, "placeholder", "") or "")
        placeholder = escape_label(raw_placeholder, limit=80)
        if placeholder and placeholder not in name:
            line += f' placeholder="{placeholder}"'
        lines.append(line)
    if len(matches) > limit:
        lines.append(f"(truncated: {len(matches) - limit} more; increase limit)")
    return [text_block("\n".join(lines))]
