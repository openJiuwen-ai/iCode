# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""``browser_snapshot`` - fuse the perception sources and re-mint refs."""

from __future__ import annotations

from dataclasses import replace
from typing import Any

from ..snapshot.serializer import is_structural
from ..types import ToolBlock, text_block
from ._shared import (
    capture_and_register,
    detect_login,
    eval_value,
    gather_in_order,
    login_hint,
    render_snapshot,
    resolve_target,
    stats_line,
)

NAME = "browser_snapshot"

DESCRIPTION = (
    "Return the current page as a compact text tree of interactive elements, each "
    "with a fresh @eN#epoch ref you must use for click/fill/scroll. Re-snapshot "
    "after any change; stale refs are rejected. scope=viewport (default) stages "
    "only what is on screen plus nearby nodes; scope=full marks every node as "
    "in-viewport so nothing is dropped for being offscreen. interactive_only=true "
    "keeps only interactive nodes. target=<ref|text> serializes only that element's "
    "subtree, which is how you read a dense region without the rest of the page. "
    "since=<header version> returns only changed nodes and keeps refs for unchanged "
    "nodes; after context loss use since=0. During a login wait this call doubles as "
    "the keepalive probe (its error reports waited/remaining seconds)."
)

PARAMS: dict[str, Any] = {
    "scope": {"enum": ["viewport", "full"], "default": "viewport"},
    "interactive_only": {"type": "boolean", "default": False},
    "target": {"type": "string", "default": "", "description": "Serialize only this element's subtree"},
    "since": {
        "type": "integer",
        "default": 0,
        "min": 0,
        "description": "Return only changes since this snapshot version; 0 returns full",
    },
}


def _subtree(nodes: list[Any], entry: Any) -> list[Any]:
    """*entry*'s node and its fused descendants in the same document.

    Cross-document (OOPIF) children of an iframe node stay out: they belong to
    another CDP document and their refs are listed separately.
    """
    backend = entry.backend_node_id
    return [
        node
        for node in nodes
        if node.frame_id == entry.frame_id and (node.backend_node_id == backend or backend in node.ancestor_ids)
    ]


async def _read_mutation_count(session: Any) -> int:
    from ..snapshot.fusion import MUTATION_COUNT_EXPRESSION

    value = await eval_value(session, MUTATION_COUNT_EXPRESSION)
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


async def _page_title(session: Any) -> str:
    value = await eval_value(session, "document.title")
    return "" if value is None else str(value)


def _node_delta_state(node: Any) -> tuple[Any, ...]:
    """Displayed node state for ``since`` deltas without changing ref fingerprints."""
    bbox = getattr(node, "bbox", (0, 0, 0, 0))
    no_layout = not getattr(node, "has_layout", True) and (bbox[2] <= 0 or bbox[3] <= 0)
    return (
        node.fingerprint(),
        bool(getattr(node, "editable", False)),
        bool(getattr(node, "readonly", False)),
        bool(getattr(node, "disabled", False)),
        no_layout,
        str(getattr(node, "placeholder", "") or ""),
        bool(getattr(node, "scrollable", False)),
        str(getattr(node, "text_hint", "") or "")[:120] if not getattr(node, "name", "") else "",
    )


async def run(session: Any, arguments: dict[str, Any]) -> list[ToolBlock]:
    scope = arguments["scope"]
    interactive_only = arguments["interactive_only"]
    since = int(arguments.get("since", 0) or 0)
    old_version = session.snapshot.version
    old_epoch = session.page_epoch

    count, title = await gather_in_order(_read_mutation_count(session), _page_title(session))
    changed = session.observe_mutations(count)

    nodes, _frame = await capture_and_register(session, title=title, dom_changed=changed)

    login_error = detect_login(session, nodes)
    if login_error is not None:
        raise login_error

    target = str(arguments.get("target") or "").strip()
    entry = None
    view = list(nodes)
    frame_note = ""
    if target:
        entry = await resolve_target(session, target)
        view = _subtree(view, entry)
        own = next(
            (
                node
                for node in nodes
                if node.frame_id == entry.frame_id and node.backend_node_id == entry.backend_node_id
            ),
            None,
        )
        if own is not None and own.tag in {"iframe", "frame"}:
            frame_note = "target is a frame: its content is a separate document; use scope=full to list it"
    focus = entry.token if entry is not None else ""
    if interactive_only:
        view = [node for node in view if getattr(node, "interactive", False)]
    if scope == "full":
        # scope=full means "do not drop offscreen nodes": the serializer stages by
        # viewport first, so we render copies marked in-viewport (the cached
        # nodes keep their real viewport facts for later target ranking).
        view = [replace(node, in_viewport=True) for node in view]

    url = session.snapshot.url
    if since and since == old_version and old_epoch == session.page_epoch:
        old_nodes = [
            node
            for node in session.snapshot.previous_nodes
            if not interactive_only or getattr(node, "interactive", False)
        ]
        if entry is not None:
            old_nodes = _subtree(old_nodes, entry)
        old_visible = [node for node in old_nodes if not is_structural(node)]
        current_visible = [node for node in view if not is_structural(node)]
        old_by_id = {(node.frame_id, node.backend_node_id): node for node in old_visible}
        current_by_id = {(node.frame_id, node.backend_node_id): node for node in current_visible}
        changed_nodes = [
            node
            for node in current_visible
            if (node.frame_id, node.backend_node_id) not in old_by_id
            or _node_delta_state(node) != _node_delta_state(old_by_id[(node.frame_id, node.backend_node_id)])
        ]
        removed = len(set(old_by_id) - set(current_by_id))
        if not changed_nodes and not removed:
            if changed:
                text = (
                    f"{render_snapshot(session, [], url=url, title=title, focus=focus)}\n"
                    f"delta: mutated (no node-level changes) since version {since}"
                )
            else:
                text = (
                    f"{render_snapshot(session, [], url=url, title=title, focus=focus)}\n"
                    f"delta: no changes since version {since}"
                )
        else:
            text = (
                f"{render_snapshot(session, changed_nodes, url=url, title=title, focus=focus)}\n"
                f"delta: +{len(changed_nodes)} -{removed} since version {since}"
            )
    else:
        text = render_snapshot(session, view, url=url, title=title, focus=focus)
    text = f"{text}\n{stats_line(session, view)}"
    if frame_note:
        text = f"{text}\n{frame_note}"
    hint = login_hint(url)
    if hint:
        text = f"{text}\n{hint}"
    session.note_success(session.target_id)
    return [text_block(text)]
