# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Fusion naming: pseudo-elements are dropped and icon-only controls get a fallback name."""

from __future__ import annotations

from typing import Any

import pytest

from icode_browser_use.errors import TargetAmbiguous
from icode_browser_use.snapshot.fusion import COMPUTED_STYLES, FrameInfo, FusedNode, fuse
from icode_browser_use.snapshot.scoring import rank, resolve_ambiguity

FRAME = FrameInfo(frame_id="f", viewport=(800.0, 600.0))


class _Doc:
    """Builds a ``DOMSnapshot.captureSnapshot`` payload one node at a time."""

    def __init__(self) -> None:
        self.strings: list[str] = []
        self.node_type: list[int] = []
        self.node_name: list[int] = []
        self.node_value: list[int] = []
        self.parent: list[int] = []
        self.backend: list[int] = []
        self.attributes: list[list[int]] = []
        self.pseudo_index: list[int] = []
        self.pseudo_value: list[int] = []
        self.layout_nodes: list[int] = []
        self.layout_text: list[int] = []
        self.bounds: list[list[float]] = []
        self.styles: list[list[str]] = []

    def _s(self, value: str) -> int:
        if value not in self.strings:
            self.strings.append(value)
        return self.strings.index(value)

    def element(
        self,
        tag: str,
        parent: int = -1,
        *,
        attrs: dict[str, str] | None = None,
        text: str = "",
        pseudo: str = "",
        bounds: tuple[float, float, float, float] = (10, 10, 50, 20),
        cursor: str = "",
    ) -> int:
        position = len(self.node_type)
        self.node_type.append(1)
        self.node_name.append(self._s(tag))
        self.node_value.append(-1)
        self.parent.append(parent)
        self.backend.append(100 + position)
        flat: list[int] = []
        for key, value in (attrs or {}).items():
            flat.extend((self._s(key), self._s(value)))
        self.attributes.append(flat)
        if pseudo:
            self.pseudo_index.append(position)
            self.pseudo_value.append(self._s(pseudo))
        self.layout_nodes.append(position)
        self.layout_text.append(self._s(text) if text else -1)
        self.bounds.append(list(bounds))
        style = [""] * len(COMPUTED_STYLES)
        style[COMPUTED_STYLES.index("cursor")] = cursor
        self.styles.append(style)
        return position

    def payload(self) -> dict[str, Any]:
        nodes: dict[str, Any] = {
            "nodeType": self.node_type,
            "nodeName": self.node_name,
            "nodeValue": self.node_value,
            "parentIndex": self.parent,
            "backendNodeId": self.backend,
            "attributes": self.attributes,
        }
        if self.pseudo_index:
            nodes["pseudoType"] = {"index": self.pseudo_index, "value": self.pseudo_value}
        layout = {
            "nodeIndex": self.layout_nodes,
            "text": self.layout_text,
            "bounds": self.bounds,
            "styles": self.styles,
        }
        return {"strings": self.strings, "documents": [{"nodes": nodes, "layout": layout}]}


def _ax(doc: _Doc, position: int, role: str, name: str = "") -> dict[str, Any]:
    return {"backendDOMNodeId": doc.backend[position], "role": {"value": role}, "name": {"value": name}}


def _by_tag(nodes: list[FusedNode], tag: str) -> FusedNode:
    return next(node for node in nodes if node.tag == tag)


def test_pseudo_element_boxes_are_not_nodes() -> None:
    doc = _Doc()
    body = doc.element("body")
    composer = doc.element("div", body, attrs={"role": "textbox", "aria-label": "Enter a prompt here"})
    doc.element("::before", composer, text="Enter a prompt here", pseudo="before")
    doc.element("::after", composer, pseudo="after")
    ax = {"nodes": [_ax(doc, composer, "textbox", "Enter a prompt here")]}

    nodes = fuse(doc.payload(), ax, frame=FRAME, target_id="t")

    assert [node.tag for node in nodes] == ["body", "div"]
    textbox = _by_tag(nodes, "div")
    assert (textbox.role, textbox.name) == ("textbox", "Enter a prompt here")


def test_fusion_captures_contenteditable_placeholder_and_layout_flags() -> None:
    doc = _Doc()
    body = doc.element("body")
    editor = doc.element(
        "div",
        body,
        attrs={"contenteditable": "true", "data-placeholder": "Write a comment"},
        bounds=(10, 10, 200, 40),
    )
    hidden = doc.element("div", body, attrs={"readonly": ""}, bounds=(0, 0, 0, 0))
    nodes = fuse(doc.payload(), None, frame=FRAME, target_id="t")
    ed_node = next(n for n in nodes if n.backend_node_id == doc.backend[editor])
    hid_node = next(n for n in nodes if n.backend_node_id == doc.backend[hidden])
    assert ed_node.editable is True
    assert ed_node.interactive is True
    assert ed_node.has_layout is True
    assert ed_node.placeholder == "Write a comment"
    assert hid_node.editable is False
    assert hid_node.readonly is True
    assert hid_node.has_layout is False


def test_pseudo_element_is_dropped_by_name_without_pseudo_type() -> None:
    doc = _Doc()
    owner = doc.element("button", attrs={"aria-label": "Send"})
    doc.element("::before", owner, text="➤")  # older CDP shapes without pseudoType

    nodes = fuse(doc.payload(), {"nodes": [_ax(doc, owner, "button", "Send")]}, frame=FRAME, target_id="t")

    assert [node.tag for node in nodes] == ["button"]


def test_placeholder_pseudo_no_longer_makes_the_textbox_ambiguous() -> None:
    doc = _Doc()
    body = doc.element("body")
    composer = doc.element("div", body, attrs={"role": "textbox", "aria-label": "Enter a prompt here"})
    doc.element("::before", composer, text="Enter a prompt here", pseudo="before")
    nodes = fuse(
        doc.payload(), {"nodes": [_ax(doc, composer, "textbox", "Enter a prompt here")]}, frame=FRAME, target_id="t"
    )

    best = resolve_ambiguity(rank(nodes, "Enter a prompt here"))

    assert best.node.tag == "div"
    assert best.node.role == "textbox"


def test_icon_only_link_falls_back_to_its_href() -> None:
    doc = _Doc()
    body = doc.element("body")
    link = doc.element("a", body, attrs={"href": "/chat/c1"})
    doc.element("img", link, attrs={"src": "icon.gif"})
    nodes = fuse(doc.payload(), {"nodes": [_ax(doc, link, "link")]}, frame=FRAME, target_id="t")

    anchor = _by_tag(nodes, "a")
    assert anchor.role == "link"
    assert anchor.name == "/chat/c1"
    assert anchor.href == "/chat/c1"
    assert anchor.text_hint == "/chat/c1"
    assert anchor.name_from_href is True
    assert anchor.fingerprint()["name_source"] == "href"


def test_wrappers_inheriting_cursor_pointer_do_not_borrow_the_image_label() -> None:
    doc = _Doc()
    body = doc.element("body")
    item = doc.element("li", body, cursor="pointer")
    link = doc.element("a", item, attrs={"href": "/home"}, cursor="pointer")
    box = doc.element("span", link, cursor="pointer")
    doc.element("img", box, attrs={"alt": "Home"}, cursor="pointer")
    nodes = fuse(doc.payload(), {"nodes": [_ax(doc, link, "link")]}, frame=FRAME, target_id="t")

    assert nodes[link].name == "Home"
    assert nodes[link].name_from_href is False
    assert nodes[box].name == ""
    assert nodes[item].name == ""
    assert nodes[box].interactive and nodes[item].interactive  # the cursor still marks them clickable


def test_href_names_do_not_compete_with_real_labels() -> None:
    doc = _Doc()
    body = doc.element("body")
    links = [doc.element("a", body, attrs={"href": f"/chat/c{n}"}) for n in (1, 2, 3)]
    button = doc.element("button", body, text="Chat", bounds=(10, 1000, 50, 20))
    ax = {"nodes": [_ax(doc, link, "link") for link in links] + [_ax(doc, button, "button", "Chat")]}
    nodes = fuse(doc.payload(), ax, frame=FRAME, target_id="t")
    assert [nodes[link].name for link in links] == ["/chat/c1", "/chat/c2", "/chat/c3"]
    assert nodes[button].in_viewport is False

    assert resolve_ambiguity(rank(nodes, "Chat")).node.tag == "button"
    assert [candidate.node.tag for candidate in rank(nodes, "chat")] == ["button"]
    assert resolve_ambiguity(rank(nodes, "/chat/c2")).node.href == "/chat/c2"
    with pytest.raises(TargetAmbiguous):
        resolve_ambiguity(rank(nodes, "/chat/c"))  # a path prefix still matches every link


def test_hrefs_without_a_word_are_not_names() -> None:
    doc = _Doc()
    body = doc.element("body")
    root = doc.element("a", body, attrs={"href": "/"})
    anchor = doc.element("a", body, attrs={"href": "#"})
    nodes = fuse(doc.payload(), None, frame=FRAME, target_id="t")

    assert nodes[root].name == "" and nodes[root].href == "/"
    assert nodes[anchor].name == "" and nodes[anchor].href == "#"
    assert "name_source" not in nodes[root].fingerprint()


def test_named_link_keeps_its_accessible_name_and_exposes_href() -> None:
    doc = _Doc()
    link = doc.element("a", attrs={"href": "/chat/c2"}, text="Trip planning")
    nodes = fuse(doc.payload(), {"nodes": [_ax(doc, link, "link", "Trip planning")]}, frame=FRAME, target_id="t")

    anchor = _by_tag(nodes, "a")
    assert anchor.name == "Trip planning"
    assert anchor.href == "/chat/c2"


def test_fallback_order_prefers_labels_over_href() -> None:
    doc = _Doc()
    body = doc.element("body")
    titled = doc.element("a", body, attrs={"href": "/x", "title": "Open settings"})
    labelled = doc.element("a", body, attrs={"href": "/y", "aria-label": "Profile", "title": "ignored"})
    wrapped = doc.element("a", body, attrs={"href": "/z"})
    box = doc.element("span", wrapped)
    doc.element("img", box, attrs={"alt": "Weather in Shanghai"})
    nodes = fuse(doc.payload(), None, frame=FRAME, target_id="t")

    assert nodes[titled].name == "Open settings"
    assert nodes[labelled].name == "Profile"
    assert nodes[wrapped].name == "Weather in Shanghai"
    assert nodes[box].name == ""  # a plain container never borrows the image label
    assert nodes[body].name == ""


def test_image_label_does_not_reach_distant_ancestors() -> None:
    doc = _Doc()
    root = doc.element("nav", attrs={"role": "button"})
    level1 = doc.element("div", root, attrs={"role": "button"})
    level2 = doc.element("div", level1, attrs={"role": "button"})
    level3 = doc.element("div", level2, attrs={"role": "button"})
    doc.element("img", level3, attrs={"alt": "deep"})
    nodes = fuse(doc.payload(), None, frame=FRAME, target_id="t")

    assert [node.name for node in nodes] == ["", "deep", "deep", "deep", "deep"]


def test_opaque_hrefs_are_not_used_as_names() -> None:
    doc = _Doc()
    body = doc.element("body")
    script = doc.element("a", body, attrs={"href": "javascript:void(0)"})
    data = doc.element("a", body, attrs={"href": "data:text/plain,hello"})
    long_href = "/path/" + "x" * 300
    long = doc.element("a", body, attrs={"href": long_href})
    nodes = fuse(doc.payload(), None, frame=FRAME, target_id="t")

    assert nodes[script].name == ""
    assert nodes[script].href == "javascript:void(0)"
    assert nodes[data].name == ""
    assert nodes[long].name == long_href[:120]
    assert nodes[long].href == long_href


def test_href_is_only_read_from_anchors() -> None:
    doc = _Doc()
    link = doc.element("link", attrs={"href": "style.css", "rel": "stylesheet"})
    nodes = fuse(doc.payload(), None, frame=FRAME, target_id="t")

    assert nodes[link].href == ""
    assert nodes[link].name == ""


def test_fusion_records_the_dom_parent_and_ancestors() -> None:
    doc = _Doc()
    form = doc.element("form")
    label = doc.element("label", form)
    field = doc.element("input", label, attrs={"type": "text"})
    nodes = {node.backend_node_id: node for node in fuse(doc.payload(), None, frame=FRAME, target_id="t")}

    field_backend = 100 + field
    assert nodes[field_backend].parent_backend_id == 100 + label
    assert nodes[field_backend].ancestor_ids == (100 + form, 100 + label)
    assert nodes[100 + form].parent_backend_id == 0
    assert nodes[100 + form].ancestor_ids == ()


def test_interactive_child_inside_same_name_cell_resolves_without_ambiguity() -> None:
    doc = _Doc()
    table = doc.element("table")
    row = doc.element("tr", table)
    cell = doc.element("td", row, text="Resolve INC-0117")
    button = doc.element("button", cell, text="Resolve INC-0117")
    ax = {
        "nodes": [
            _ax(doc, cell, "cell", "Resolve INC-0117"),
            _ax(doc, button, "button", "Resolve INC-0117"),
        ]
    }
    nodes = fuse(doc.payload(), ax, frame=FRAME, target_id="t")

    best = resolve_ambiguity(rank(nodes, "Resolve INC-0117"))
    assert best.node.tag == "button"
    assert best.node.interactive is True

    # Two distinct buttons with the same label must still raise TargetAmbiguous.
    row2 = doc.element("tr", table)
    cell2 = doc.element("td", row2, text="Resolve INC-0117")
    button2 = doc.element("button", cell2, text="Resolve INC-0117")
    ax2 = {
        "nodes": [
            _ax(doc, cell, "cell", "Resolve INC-0117"),
            _ax(doc, button, "button", "Resolve INC-0117"),
            _ax(doc, cell2, "cell", "Resolve INC-0117"),
            _ax(doc, button2, "button", "Resolve INC-0117"),
        ]
    }
    nodes2 = fuse(doc.payload(), ax2, frame=FRAME, target_id="t")
    with pytest.raises(TargetAmbiguous):
        resolve_ambiguity(rank(nodes2, "Resolve INC-0117"))


def test_unnamed_form_control_falls_back_to_readable_name_or_id_only() -> None:
    doc = _Doc()
    body = doc.element("body")
    heading = doc.element("h3", body, text="Dropdown List")
    sel = doc.element("select", body, attrs={"id": "dropdown"})
    noisy = doc.element("select", body, attrs={"id": ":r0:"})
    hashed = doc.element("input", body, attrs={"id": "field-8f92a01b", "type": "text"})
    labelled = doc.element("select", body, attrs={"id": "ignored_id", "aria-label": "Country"})
    ax = {
        "nodes": [
            _ax(doc, heading, "heading", "Dropdown List"),
            _ax(doc, sel, "combobox", ""),
            _ax(doc, noisy, "combobox", ""),
            _ax(doc, hashed, "textbox", ""),
            _ax(doc, labelled, "combobox", "Country"),
        ]
    }
    nodes = fuse(doc.payload(), ax, frame=FRAME, target_id="t")

    assert nodes[sel].name == "dropdown"
    assert nodes[sel].name_from_attr is True
    assert nodes[sel].fingerprint()["name_source"] == "attr"
    assert nodes[noisy].name == ""
    assert nodes[hashed].name == ""
    assert nodes[labelled].name == "Country"
    assert nodes[labelled].name_from_attr is False

    best = resolve_ambiguity(rank(nodes, "dropdown"))
    assert best.node.tag == "select"


def test_single_hidden_exact_match_resolves_and_single_weak_match_does_not_claim_ambiguity() -> None:
    # A hidden exact-name button ("Stop generating") scores 50 (< MIN_TOP_SCORE=55),
    # yet is the sole exact candidate: wait_for(hidden, "Stop generating") must resolve it.
    hidden_stop = FusedNode(
        backend_node_id=10,
        frame_id="f",
        target_id="t",
        role="button",
        name="Stop generating",
        tag="button",
        nth=1,
        bbox=(0.0, 0.0, 0.0, 0.0),
        in_viewport=False,
        visible=False,
        interactive=True,
        ref=1,
        epoch=1,
    )
    ranked = rank([hidden_stop], "Stop generating")
    assert len(ranked) == 1 and ranked[0].score < 55 and ranked[0].exact_match is True
    assert resolve_ambiguity(ranked).node is hidden_stop

    # A single weak non-exact candidate still refuses blind selection, without claiming "目标不唯一".
    weak = FusedNode(
        backend_node_id=11,
        frame_id="f",
        target_id="t",
        role="generic",
        name="Long unrelated report footer",
        tag="div",
        nth=1,
        bbox=(0.0, 1200.0, 100.0, 20.0),
        in_viewport=False,
        visible=True,
        interactive=False,
        ref=2,
        epoch=1,
    )
    weak_ranked = rank([weak], "report")
    assert len(weak_ranked) == 1 and weak_ranked[0].score < 55 and weak_ranked[0].exact_match is False
    with pytest.raises(TargetAmbiguous) as exc_info:
        resolve_ambiguity(weak_ranked)
    assert "目标不唯一" not in str(exc_info.value)
    assert "目标匹配度过低" in str(exc_info.value)


def test_exact_text_generic_node_outranks_fuzzy_matches() -> None:
    """An unnamed generic status div whose text_hint matches spec exactly must beat fuzzy matches."""
    nodes = [
        FusedNode(
            backend_node_id=7,
            frame_id="f",
            target_id="t",
            role="textbox",
            name="Release Code",
            tag="input",
            nth=1,
            bbox=(10.0, 40.0, 120.0, 24.0),
            in_viewport=True,
            visible=True,
            interactive=True,
            ref=7,
            epoch=1,
        ),
        FusedNode(
            backend_node_id=6,
            frame_id="f",
            target_id="t",
            role="heading",
            name="Release Gate",
            tag="h1",
            nth=1,
            bbox=(10.0, 10.0, 120.0, 24.0),
            in_viewport=True,
            visible=True,
            interactive=False,
            ref=6,
            epoch=1,
        ),
        FusedNode(
            backend_node_id=9,
            frame_id="f",
            target_id="t",
            role="generic",
            name="",
            tag="div",
            nth=1,
            bbox=(10.0, 80.0, 160.0, 20.0),
            text_hint="RELEASE_APPROVED_OK",
            in_viewport=True,
            visible=True,
            interactive=False,
            ref=9,
            epoch=1,
        ),
    ]
    ranked = rank(nodes, "RELEASE_APPROVED_OK")
    assert ranked[0].node.ref == 9
    assert ranked[0].exact_match is True
    assert resolve_ambiguity(ranked).node.ref == 9


def test_fusion_marks_css_hidden_and_head_meta_nodes_and_propagates_opacity_zero() -> None:
    doc = _Doc()
    head = doc.element("head", bounds=(0, 0, 0, 0))
    title = doc.element("title", head, text="Secret Title", bounds=(0, 0, 0, 0))
    body = doc.element("body")
    vis = doc.element("p", body, text="Normal visible text")
    disp_none = doc.element("p", body, attrs={"style": "display:none"}, text="DISPLAY NONE INJECT", bounds=(0, 0, 0, 0))
    vis_hidden = doc.element("p", body, text="VISIBILITY HIDDEN INJECT")
    doc.styles[vis_hidden][COMPUTED_STYLES.index("visibility")] = "hidden"
    op_parent = doc.element("div", body)
    doc.styles[op_parent][COMPUTED_STYLES.index("opacity")] = "0"
    op_child = doc.element("p", op_parent, text="INHERITED OPACITY ZERO INJECT")

    nodes = fuse(doc.payload(), None, frame=FRAME, target_id="t")
    by_id = {n.backend_node_id: n for n in nodes}
    assert by_id[doc.backend[vis]].css_hidden is False
    assert by_id[doc.backend[vis]].visible is True
    assert by_id[doc.backend[title]].css_hidden is True
    assert by_id[doc.backend[disp_none]].css_hidden is True
    assert by_id[doc.backend[vis_hidden]].css_hidden is True
    assert by_id[doc.backend[op_parent]].css_hidden is True
    assert by_id[doc.backend[op_child]].css_hidden is True
    assert by_id[doc.backend[op_child]].visible is False


def test_scoring_resolves_same_name_label_and_slider_to_slider() -> None:
    label = FusedNode(
        1,
        "f",
        "t",
        "LabelText",
        "Priority Level",
        "label",
        0,
        (10, 10, 80, 20),
        ref=1,
        epoch=1,
        visible=True,
        in_viewport=True,
        interactive=True,
    )
    slider = FusedNode(
        2,
        "f",
        "t",
        "slider",
        "Priority Level",
        "input",
        0,
        (100, 10, 120, 20),
        ref=2,
        epoch=1,
        visible=True,
        in_viewport=True,
        interactive=True,
        focusable=True,
    )
    best = resolve_ambiguity(rank([label, slider], "Priority Level"))
    assert best.node.ref == 2
    assert best.node.role == "slider"
