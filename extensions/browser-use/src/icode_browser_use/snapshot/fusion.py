# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Perception: fuse DOMSnapshot, the AX tree and the DOM into ``FusedNode``s.

The raw ``DOMSnapshot.captureSnapshot`` result is joined with the
``Accessibility.getFullAXTree`` result on ``backendNodeId``. Geometry (bounds,
visibility, scrollability) comes from the snapshot; semantics (role, name,
focusability) come from the AX tree. Nodes the snapshot never laid out are still
emitted (``visible=False``) because scoring needs them (e.g. shadow-DOM
controls).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

# --- injected, read-only page probe -----------------------------------------

#: Installed once per document via ``Page.addScriptToEvaluateOnNewDocument``.
#: It counts DOM mutations into ``window.__chrys_mutations`` and *never touches
#: the DOM* (writing it would trigger React re-renders and break the page).
MUTATION_PROBE_SCRIPT: str = """
(() => {
  if (window.__chrys_probe_installed) { return; }
  window.__chrys_probe_installed = true;
  if (typeof window.__chrys_mutations !== "number") { window.__chrys_mutations = 0; }
  try {
    const observer = new MutationObserver((records) => {
      window.__chrys_mutations += records.length;
    });
    observer.observe(document, {
      childList: true,
      subtree: true,
      attributes: true,
      characterData: true,
    });
  } catch (error) {
    /* read-only probe: swallow observation errors, never touch the DOM */
  }
})();
"""

#: ``Runtime.evaluate`` expression that reads the probe counter.
MUTATION_COUNT_EXPRESSION: str = "window.__chrys_mutations || 0"

#: Computed-style minimal set (design section 4.1) - protects fragile sites.
COMPUTED_STYLES: tuple[str, ...] = (
    "display",
    "visibility",
    "opacity",
    "overflow",
    "overflow-x",
    "overflow-y",
    "cursor",
    "pointer-events",
    "position",
    "background-color",
)

#: ``DOMSnapshot.captureSnapshot`` parameters built from :data:`COMPUTED_STYLES`.
SNAPSHOT_CAPTURE_PARAMS: dict[str, Any] = {
    "computedStyles": list(COMPUTED_STYLES),
    "includePaintOrder": True,
    "includeDOMRects": True,
}

_IDX_DISPLAY = COMPUTED_STYLES.index("display")
_IDX_VISIBILITY = COMPUTED_STYLES.index("visibility")
_IDX_OPACITY = COMPUTED_STYLES.index("opacity")
_IDX_OVERFLOW = COMPUTED_STYLES.index("overflow")
_IDX_OVERFLOW_X = COMPUTED_STYLES.index("overflow-x")
_IDX_OVERFLOW_Y = COMPUTED_STYLES.index("overflow-y")
_IDX_CURSOR = COMPUTED_STYLES.index("cursor")
_IDX_POINTER_EVENTS = COMPUTED_STYLES.index("pointer-events")

_INTERACTIVE_ROLES = frozenset(
    {"button", "link", "textbox", "checkbox", "radio", "combobox", "slider", "switch", "tab", "menuitem"}
)
_SCROLL_OVERFLOW = frozenset({"auto", "scroll"})
_CLICKABLE_TAGS = frozenset({"button", "input", "select", "textarea"})


@dataclass(frozen=True)
class FrameInfo:
    """Geometry of the frame a snapshot was taken in."""

    frame_id: str
    url: str = ""
    title: str = ""
    viewport: tuple[float, float] = (0.0, 0.0)
    scroll_x: float = 0.0
    scroll_y: float = 0.0
    offset: tuple[float, float] = (0.0, 0.0)
    dpr: float = 1.0


@dataclass
class FusedNode:
    """One element after AX + DOMSnapshot fusion."""

    backend_node_id: int
    frame_id: str
    target_id: str
    role: str
    name: str
    tag: str
    nth: int
    bbox: tuple[float, float, float, float]
    text_hint: str = ""
    in_viewport: bool = False
    visible: bool = False
    interactive: bool = False
    scrollable: bool = False
    focusable: bool = False
    disabled: bool = False
    ref: int | None = None
    epoch: int | None = None
    cdp_session_id: str | None = None
    """Child CDP session for an OOPIF node; None for the main frame."""
    href: str = ""
    """The ``href`` attribute of an ``<a>``/``<area>``, as written in the DOM."""
    name_from_href: bool = False
    """``name`` is the link's ``href`` (no accessible name): it is a path, not page text."""
    parent_backend_id: int = 0
    """``backend_node_id`` of the direct DOM parent element (0 for the document root)."""
    ancestor_ids: tuple[int, ...] = ()
    """Element ancestors, root first (subtree selection and folding)."""
    name_from_ax: bool = False
    editable: bool = False
    readonly: bool = False
    placeholder: str = ""
    has_layout: bool = False

    def fingerprint(self) -> dict[str, str]:
        """Coarse identity used to detect a stale reference within an epoch."""
        fingerprint = {"role": self.role, "name": self.name, "tag": self.tag, "nth": str(self.nth)}
        if self.name_from_href:
            fingerprint["name_source"] = "href"
        elif self.name_from_ax:
            fingerprint["name_source"] = "ax"
        return fingerprint


# --- string table / attribute helpers ---------------------------------------


def _resolve(strings: list[Any], index: Any) -> str:
    """Resolve a string-table index; tolerate pre-resolved strings."""
    if index is None:
        return ""
    if isinstance(index, str):
        return index
    try:
        position = int(index)
    except (TypeError, ValueError):
        return ""
    if 0 <= position < len(strings):
        value = strings[position]
        return value if isinstance(value, str) else str(value)
    return ""


def _parse_attributes(strings: list[Any], flat: Any) -> dict[str, str]:
    """Parse a flat ``[nameIdx, valueIdx, ...]`` attribute list into a mapping."""
    if not isinstance(flat, list):
        return {}
    result: dict[str, str] = {}
    position = 0
    while position + 1 < len(flat):
        name = _resolve(strings, flat[position])
        value = _resolve(strings, flat[position + 1])
        if name:
            result[name.lower()] = value
        position += 2
    return result


def _style(row: Any, index: int, strings: list[Any]) -> str:
    """Read one computed-style value from a per-node style row."""
    if not isinstance(row, list) or index >= len(row):
        return ""
    value = row[index]
    if isinstance(value, str):
        return value
    return _resolve(strings, value)


def _client_rect_count(client_rects: Any, layout_index: int | None) -> int | None:
    """Number of client rects for a layout entry, tolerant of both CDP shapes."""
    if not isinstance(client_rects, list) or layout_index is None or layout_index >= len(client_rects):
        return None
    entry = client_rects[layout_index]
    if not isinstance(entry, list) or not entry:
        return 0 if isinstance(entry, list) else None
    first = entry[0]
    if isinstance(first, list):
        return len(entry)  # nested: one rectangle per client rect (inline elements)
    return 1  # flat: a single rectangle for this node


def _clickable_node_indices(nodes_section: dict[str, Any], layout_section: dict[str, Any]) -> set[int]:
    """Collect node indices marked ``isClickable``, tolerating every CDP shape."""
    result: set[int] = set()
    raw = nodes_section.get("isClickable")
    if isinstance(raw, dict):
        listed = raw.get("index")
        if isinstance(listed, list):  # RareBooleanData
            for item in listed:
                if isinstance(item, (int, float)):
                    result.add(int(item))
        else:  # a ``{nodeIndex: bool}`` mapping
            for key, value in raw.items():
                if value and str(key).isdigit():
                    result.add(int(key))
    elif isinstance(raw, list):
        for position, value in enumerate(raw):
            if value is True:
                result.add(position)
    raw_layout = layout_section.get("isClickable")
    if isinstance(raw_layout, dict) and "index" not in raw_layout:
        node_index = layout_section.get("nodeIndex") or []
        for key, value in raw_layout.items():
            if not value or not str(key).isdigit():
                continue
            layout_position = int(key)
            if 0 <= layout_position < len(node_index):
                result.add(int(node_index[layout_position]))
    return result


def _pseudo_node_indices(nodes_section: dict[str, Any]) -> set[int]:
    """Node indices DOMSnapshot reports as CSS pseudo-elements (``pseudoType`` RareStringData)."""
    raw = nodes_section.get("pseudoType")
    if not isinstance(raw, dict):
        return set()
    listed = raw.get("index")
    if not isinstance(listed, list):
        return set()
    return {int(item) for item in listed if isinstance(item, (int, float))}


_IMAGE_LABEL_DEPTH = 3
#: Only a real control (link/button by role or tag) borrows a wrapped image's
#: label: ``cursor: pointer`` is inherited, so plain wrappers inside a link would
#: otherwise be named too.
_IMAGE_LABEL_ROLES = frozenset({"link", "button"})
_HREF_NAME_LIMIT = 120
_UNNAMEABLE_HREF_SCHEMES = ("javascript:", "data:", "blob:")


def _image_labels(nodes_section: dict[str, Any], strings: list[Any], parent_index: list[Any]) -> dict[int, str]:
    """``alt``/``title`` of an ``<img>`` offered to its nearest ancestors (bounded depth).

    An icon-only link or button has no accessible name when the image has no
    ``alt`` Chromium could use; the map lets the fusion fall back to whatever
    label the image does carry before giving up.
    """
    node_type = nodes_section.get("nodeType") or []
    node_name = nodes_section.get("nodeName") or []
    attributes = nodes_section.get("attributes") or []
    labels: dict[int, str] = {}
    for position in range(len(node_type)):
        if int(node_type[position] or 0) != 1:
            continue
        if _resolve(strings, node_name[position] if position < len(node_name) else None).lower() != "img":
            continue
        attrs = _parse_attributes(strings, attributes[position] if position < len(attributes) else None)
        label = attrs.get("alt", "").strip() or attrs.get("title", "").strip()
        if not label:
            continue
        ancestor = int(parent_index[position]) if position < len(parent_index) else -1
        for _ in range(_IMAGE_LABEL_DEPTH):
            if ancestor < 0:
                break
            labels.setdefault(ancestor, label)
            ancestor = int(parent_index[ancestor]) if ancestor < len(parent_index) else -1
    return labels


def _href_name(href: str) -> str:
    """A link's ``href`` as a last-resort name; opaque schemes stay unnamed."""
    if not href or href.lower().startswith(_UNNAMEABLE_HREF_SCHEMES):
        return ""
    if not any(char.isalnum() for char in href):
        return ""  # "/" or "#" would match every spec containing that character
    return href[:_HREF_NAME_LIMIT]


# --- AX tree -----------------------------------------------------------------


def _as_true(value: Any) -> bool:
    if value is True:
        return True
    return isinstance(value, str) and value.strip().lower() == "true"


_TEXT_ONLY_AX_ROLES = frozenset({"StaticText", "InlineTextBox", "LineBreak"})


def _build_ax_index(ax: dict[str, Any] | None) -> dict[int, dict[str, Any]]:
    """Index usable AX nodes by ``backendDOMNodeId`` (ignored nodes skipped)."""
    index: dict[int, dict[str, Any]] = {}
    if not isinstance(ax, dict):
        return index
    for node in ax.get("nodes") or []:
        if not isinstance(node, dict) or node.get("ignored"):
            continue
        backend = node.get("backendDOMNodeId")
        if backend is None:
            continue
        focusable = False
        disabled = False
        readonly = False
        for prop in node.get("properties") or []:
            if not isinstance(prop, dict):
                continue
            prop_name = prop.get("name")
            prop_value = (prop.get("value") or {}).get("value")
            if prop_name == "focusable" and _as_true(prop_value):
                focusable = True
            elif prop_name == "disabled" and _as_true(prop_value):
                disabled = True
            elif prop_name == "readonly" and _as_true(prop_value):
                readonly = True
        role = str((node.get("role") or {}).get("value") or "")
        name = str((node.get("name") or {}).get("value") or "")
        key = int(backend)
        existing = index.get(key)
        if existing is not None and role in _TEXT_ONLY_AX_ROLES and existing["role"] not in _TEXT_ONLY_AX_ROLES:
            if not existing["name"] and name:
                existing["name"] = name
            continue
        index[key] = {
            "role": role,
            "name": name,
            "focusable": focusable,
            "disabled": disabled,
            "readonly": readonly,
        }
    return index


# --- fusion ------------------------------------------------------------------


_TEXT_INPUT_TYPES = frozenset({"", "text", "search", "email", "url", "tel", "password", "number"})


def _is_contenteditable(attrs: dict[str, str]) -> bool:
    if "contenteditable" not in attrs:
        return False
    return attrs["contenteditable"].strip().lower() in {"", "true", "plaintext-only"}


def _tag_interactive(tag: str, attrs: dict[str, str]) -> bool:
    if tag in _CLICKABLE_TAGS:
        return True
    if _is_contenteditable(attrs):
        return True
    if tag == "a" and "href" in attrs:
        return True
    if attrs.get("role", "").lower() == "button":
        return True
    if "onclick" in attrs:
        return True
    tabindex = attrs.get("tabindex")
    if tabindex is not None:
        try:
            if int(tabindex) >= 0:
                return True
        except ValueError:
            pass
    return False


def _backend_at(backend_ids: list[Any], index: int) -> int:
    """The backend id at *index* (0 when absent or malformed)."""
    if index < 0 or index >= len(backend_ids):
        return 0
    try:
        return int(backend_ids[index])
    except (TypeError, ValueError):
        return 0


def _ancestor_backend_ids(parent_index: list[Any], backend_ids: list[Any], position: int) -> tuple[int, ...]:
    """Element ancestors of *position*, root first (document nodes carry no backend id)."""
    chain: list[int] = []
    seen: set[int] = set()
    hops = 0
    parent = int(parent_index[position]) if position < len(parent_index) else -1
    while 0 <= parent < len(parent_index) and parent not in seen and hops < 512:
        seen.add(parent)
        hops += 1
        backend = _backend_at(backend_ids, parent)
        if backend:
            chain.append(backend)
        parent = int(parent_index[parent]) if parent < len(parent_index) else -1
    return tuple(reversed(chain))


def _is_interactive(
    *,
    tag: str,
    attrs: dict[str, str],
    role: str,
    focusable: bool,
    clickable: bool,
    style_row: Any,
    strings: list[Any],
) -> bool:
    """Interaction when any of the three sources is positive (design 4.1)."""
    if focusable or clickable:
        return True
    if role.strip().lower() in _INTERACTIVE_ROLES:
        return True
    if _tag_interactive(tag, attrs):
        return True
    cursor = _style(style_row, _IDX_CURSOR, strings).strip().lower()
    pointer_events = _style(style_row, _IDX_POINTER_EVENTS, strings).strip().lower()
    return cursor == "pointer" and pointer_events != "none"


_TEXT_SKIP_PARENTS = frozenset({"script", "style", "noscript", "template"})


def _direct_child_text(
    nodes_section: dict[str, Any],
    layout_of: dict[int, int],
    texts: list[Any],
    strings: list[Any],
    parent_index: list[Any],
) -> dict[int, str]:
    """Aggregate direct ``#text`` children per parent element index (for closed Shadow DOM text)."""
    node_type = nodes_section.get("nodeType") or []
    node_name = nodes_section.get("nodeName") or []
    node_value = nodes_section.get("nodeValue") or []
    buckets: dict[int, list[str]] = {}
    for position in range(len(node_type)):
        if int(node_type[position] or 0) != 3:
            continue
        parent = int(parent_index[position]) if position < len(parent_index) else -1
        if parent < 0 or parent >= len(node_name):
            continue
        # Direct #text under a ShadowRoot (nodeType == 11) would be dropped when
        # _fuse_document skips non-elements; attribute it to the shadow host.
        if parent < len(node_type) and int(node_type[parent] or 0) == 11:
            host = int(parent_index[parent]) if parent < len(parent_index) else -1
            if 0 <= host < len(node_name):
                parent = host
        parent_tag = _resolve(strings, node_name[parent]).lower()
        if parent_tag in _TEXT_SKIP_PARENTS:
            continue
        raw = _resolve(strings, node_value[position] if position < len(node_value) else None)
        if not raw:
            lp = layout_of.get(position)
            if lp is not None and lp < len(texts):
                raw = _resolve(strings, texts[lp])
        cleaned = " ".join(raw.split())
        if cleaned:
            buckets.setdefault(parent, []).append(cleaned)
    return {parent: " ".join(parts)[:200] for parent, parts in buckets.items()}


def _fuse_document(
    nodes_section: dict[str, Any],
    layout_section: dict[str, Any],
    strings: list[Any],
    ax_index: dict[int, dict[str, Any]],
    frame: FrameInfo,
    target_id: str,
    cdp_session_id: str | None = None,
) -> list[FusedNode]:
    parent_index = nodes_section.get("parentIndex") or []
    node_type = nodes_section.get("nodeType") or []
    node_name = nodes_section.get("nodeName") or []
    node_value = nodes_section.get("nodeValue") or []
    backend_ids = nodes_section.get("backendNodeId") or []
    attributes = nodes_section.get("attributes") or []

    node_index = layout_section.get("nodeIndex") or []
    layout_of: dict[int, int] = {}
    for layout_position, node_position in enumerate(node_index):
        layout_of[int(node_position)] = layout_position
    bounds = layout_section.get("bounds") or []
    style_rows = layout_section.get("styles") or []
    texts = layout_section.get("text") or []
    client_rects = layout_section.get("clientRects")

    clickable = _clickable_node_indices(nodes_section, layout_section)
    pseudo = _pseudo_node_indices(nodes_section)
    image_labels = _image_labels(nodes_section, strings, parent_index)
    child_text = _direct_child_text(nodes_section, layout_of, texts, strings, parent_index)
    dpr = frame.dpr or 1.0
    viewport_w, viewport_h = frame.viewport
    offset_x, offset_y = frame.offset

    counters: dict[tuple[int, str], int] = {}
    out: list[FusedNode] = []
    for position in range(len(node_type)):
        if int(node_type[position] or 0) != 1:
            continue  # only elements: ``#text`` and friends are skipped
        tag = _resolve(strings, node_name[position] if position < len(node_name) else None).lower()
        if position in pseudo or tag.startswith("::"):
            # A ``::before``/``::after`` box has no element of its own: its text duplicates
            # the owner's name and clicking it lands on the owner, so it is never a target.
            continue
        backend_id = int(backend_ids[position]) if position < len(backend_ids) else 0
        attrs = _parse_attributes(strings, attributes[position] if position < len(attributes) else None)
        parent = int(parent_index[position]) if position < len(parent_index) else -1
        parent_backend_id = _backend_at(backend_ids, parent) if parent >= 0 else 0
        ancestor_ids = _ancestor_backend_ids(parent_index, backend_ids, position)
        key = (parent, tag)
        counters[key] = counters.get(key, 0) + 1
        nth = counters[key]

        layout_position = layout_of.get(position)
        raw_bounds = bounds[layout_position] if layout_position is not None and layout_position < len(bounds) else None
        has_bounds = isinstance(raw_bounds, list) and len(raw_bounds) == 4 and raw_bounds[2] > 0 and raw_bounds[3] > 0

        if has_bounds:
            css_x = float(raw_bounds[0]) / dpr - frame.scroll_x + offset_x
            css_y = float(raw_bounds[1]) / dpr - frame.scroll_y + offset_y
            width = float(raw_bounds[2]) / dpr
            height = float(raw_bounds[3]) / dpr
        else:
            css_x = css_y = width = height = 0.0

        has_style = layout_position is not None and layout_position < len(style_rows)
        style_row = style_rows[layout_position] if has_style else None
        has_text = layout_position is not None and layout_position < len(texts)
        layout_text = _resolve(strings, texts[layout_position]) if has_text else ""

        display = _style(style_row, _IDX_DISPLAY, strings).strip().lower()
        visibility = _style(style_row, _IDX_VISIBILITY, strings).strip().lower()
        opacity_raw = _style(style_row, _IDX_OPACITY, strings).strip()
        try:
            opacity = float(opacity_raw) if opacity_raw else 1.0
        except ValueError:
            opacity = 1.0

        visible = has_bounds and display != "none" and visibility != "hidden" and opacity > 0.0
        in_viewport = (
            visible
            and viewport_w > 0
            and viewport_h > 0
            and css_x < viewport_w
            and css_x + width > 0
            and css_y < viewport_h
            and css_y + height > 0
        )

        overflow = _style(style_row, _IDX_OVERFLOW, strings).strip().lower()
        overflow_x = _style(style_row, _IDX_OVERFLOW_X, strings).strip().lower() or overflow
        overflow_y = _style(style_row, _IDX_OVERFLOW_Y, strings).strip().lower() or overflow
        rect_count = _client_rect_count(client_rects, layout_position)
        bigger_than_viewport = width > viewport_w or height > viewport_h
        scrollable = (overflow_x in _SCROLL_OVERFLOW or overflow_y in _SCROLL_OVERFLOW) and (
            (rect_count is not None and rect_count > 1) or bigger_than_viewport
        )

        ax_node = ax_index.get(backend_id) or {}
        focusable = bool(ax_node.get("focusable"))
        disabled = bool(ax_node.get("disabled")) or "disabled" in attrs
        readonly = (
            bool(ax_node.get("readonly"))
            or "readonly" in attrs
            or attrs.get("aria-readonly", "").strip().lower() == "true"
        )
        role = str(ax_node.get("role") or "") or attrs.get("role", "") or tag
        is_text_input = tag == "input" and attrs.get("type", "").strip().lower() in _TEXT_INPUT_TYPES
        editable = (not disabled and not readonly) and (
            _is_contenteditable(attrs) or tag == "textarea" or is_text_input or role.strip().lower() == "textbox"
        )
        placeholder = (
            attrs.get("placeholder", "").strip()
            or attrs.get("data-placeholder", "").strip()
            or attrs.get("aria-placeholder", "").strip()
        )[:120]
        href = attrs.get("href", "").strip() if tag in ("a", "area") else ""
        ax_name = str(ax_node.get("name") or "")
        name = ax_name or layout_text
        if not name:
            name = _resolve(strings, node_value[position] if position < len(node_value) else None)
        own_text = child_text.get(position, "")
        text_hint = (layout_text or name or own_text).strip()

        interactive = _is_interactive(
            tag=tag,
            attrs=attrs,
            role=role,
            focusable=focusable,
            clickable=position in clickable,
            style_row=style_row,
            strings=strings,
        )
        name_from_href = False
        if not name:
            # Icon-only controls: the AX name is empty, so fall back to the labels the
            # DOM still carries (a wrapped image's only for a link/button, so plain
            # wrappers stay unnamed), and for a link to where it leads.
            control = role in _IMAGE_LABEL_ROLES or tag == "button" or (tag == "a" and bool(href))
            name = (
                attrs.get("aria-label", "").strip()
                or attrs.get("alt", "").strip()
                or attrs.get("title", "").strip()
                or (image_labels.get(position, "") if control else "")
            )
            if not name:
                name = _href_name(href)
                name_from_href = bool(name)
            if name:
                text_hint = name

        out.append(
            FusedNode(
                backend_node_id=backend_id,
                frame_id=frame.frame_id,
                target_id=target_id,
                role=role,
                name=name,
                tag=tag,
                nth=nth,
                bbox=(css_x, css_y, width, height),
                text_hint=text_hint,
                in_viewport=in_viewport,
                visible=visible,
                interactive=interactive,
                scrollable=scrollable,
                focusable=focusable,
                disabled=disabled,
                cdp_session_id=cdp_session_id,
                href=href,
                name_from_href=name_from_href,
                parent_backend_id=parent_backend_id,
                ancestor_ids=ancestor_ids,
                name_from_ax=bool(ax_name),
                editable=editable,
                readonly=readonly,
                placeholder=placeholder,
                has_layout=has_bounds,
            )
        )
    return out


def fuse(
    snapshot: dict[str, Any],
    ax: dict[str, Any] | None,
    *,
    frame: FrameInfo,
    target_id: str,
    cdp_session_id: str | None = None,
) -> list[FusedNode]:
    """Fuse a raw ``captureSnapshot`` result with an AX tree into ``FusedNode``s.

    Output order is the DOM traversal order (parent before child) of the first
    document, then any further documents (same-process iframes). Nodes without
    layout bounds are still emitted with ``visible=False``. ``cdp_session_id``
    marks nodes captured from an OOPIF child session.
    """
    if not isinstance(snapshot, dict):
        return []
    strings = snapshot.get("strings") or []
    documents = snapshot.get("documents") or []
    ax_index = _build_ax_index(ax)
    result: list[FusedNode] = []
    for doc_index, document in enumerate(documents):
        if not isinstance(document, dict):
            continue
        nodes_section = document.get("nodes") or {}
        layout_section = document.get("layout") or {}
        doc_frame = frame
        if doc_index > 0:
            doc_frame_id = _resolve(strings, document.get("frameId")) or frame.frame_id
            doc_frame = FrameInfo(
                frame_id=doc_frame_id,
                viewport=(
                    float(document.get("contentWidth") or frame.viewport[0]),
                    float(document.get("contentHeight") or frame.viewport[1]),
                ),
                scroll_x=float(document.get("scrollOffsetX") or 0.0),
                scroll_y=float(document.get("scrollOffsetY") or 0.0),
                offset=frame.offset,
                dpr=frame.dpr,
            )
        result.extend(
            _fuse_document(
                nodes_section,
                layout_section,
                strings,
                ax_index,
                doc_frame,
                target_id,
                cdp_session_id,
            )
        )
    return result
