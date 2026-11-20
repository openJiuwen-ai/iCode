# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

from __future__ import annotations

from browser_use.snapshot.fusion import FusedNode
from browser_use.snapshot.serializer import render, sanitize_text, weight


def node(
    *,
    ref: int | None = 1,
    role: str = "button",
    name: str = "OK",
    y: float = 0.0,
    in_viewport: bool = True,
    interactive: bool = True,
    scrollable: bool = False,
    cdp_session_id: str | None = None,
    parent: int = 0,
    ancestors: tuple[int, ...] = (),
    text_hint: str = "",
) -> FusedNode:
    return FusedNode(
        backend_node_id=ref or 0,
        frame_id="frame",
        target_id="target",
        role=role,
        name=name,
        tag="button",
        nth=0,
        bbox=(0.0, y, 10.0, 10.0),
        in_viewport=in_viewport,
        visible=True,
        interactive=interactive,
        scrollable=scrollable,
        ref=ref,
        epoch=7,
        cdp_session_id=cdp_session_id,
        parent_backend_id=parent,
        ancestor_ids=ancestors,
        text_hint=text_hint,
    )


def test_weight_counts_cjk_as_two() -> None:
    assert weight("abc") == 3
    assert weight("中文") == 4


def test_sanitize_removes_controls_and_lone_surrogates() -> None:
    assert sanitize_text("a\x00b\x7f") == "ab"
    assert sanitize_text("a\u200bb\ufeff") == "ab"
    assert sanitize_text("a\tb\nc") == "a\tb\nc"
    assert sanitize_text("a\ud800b") == "ab"
    assert sanitize_text("a\ud83d\ude00b") == "a😀b"


def test_render_empty_is_header_only() -> None:
    assert render([], page_epoch=7, url="https://example.com", title="Example") == (
        "[https://example.com] Example  (epoch 7)"
    )


def test_render_orders_and_marks_nodes() -> None:
    nodes = [
        node(ref=1, y=30.0, in_viewport=False),
        node(ref=2, role="link", name="Later", y=20.0, in_viewport=True),
        node(ref=3, role="textbox", name="First", y=10.0, in_viewport=True, scrollable=True),
    ]
    text = render(nodes, page_epoch=7, url="https://example.com", title="Example")
    lines = text.splitlines()
    assert lines[0] == "[https://example.com] Example  (epoch 7)"
    assert lines[1].startswith("  @e3#7 textbox")
    assert lines[1].endswith("*")
    assert lines[2].startswith("  @e2#7 link")
    assert lines[3].startswith("  @e1#7 button")


def test_render_respects_budget_and_reports_more() -> None:
    nodes = [node(ref=index, name="x" * 40, y=float(index)) for index in range(1, 6)]
    text = render(nodes, page_epoch=7, url="https://example.com", title="Example", budget=120)
    assert "… (+" in text
    assert "more interactive" in text
    assert "offscreen" in text


def test_render_respects_max_nodes() -> None:
    nodes = [node(ref=index, name=f"N{index}", y=float(index)) for index in range(1, 5)]
    text = render(nodes, page_epoch=7, url="https://example.com", title="Example", max_nodes=2)
    assert text.count("@e") == 2


def test_render_uses_dash_for_unreferenced_nodes() -> None:
    text = render([node(ref=None)], page_epoch=7, url="https://example.com", title="Example")
    assert "  - button" in text


def test_render_marks_oopif_content() -> None:
    text = render(
        [node(ref=None, name="Inside", cdp_session_id="child-session")],
        page_epoch=7,
        url="https://example.com",
        title="Example",
    )
    assert '  - [iframe] button "Inside"' in text


def test_render_skips_unnamed_non_interactive_structure() -> None:
    nodes = [
        node(ref=1, name="Save"),
        node(ref=2, role="generic", name="", interactive=False),
        node(ref=3, role="generic", name="", text_hint="Sidebar", interactive=False),
    ]
    text = render(nodes, page_epoch=7, url="https://example.com", title="Example")
    assert "Save" in text
    assert "Sidebar" in text
    assert text.count("@e") == 2


def test_render_keeps_an_unnamed_scrollable_container() -> None:
    """A nameless scroller has no other affordance: its ref is how browser_scroll targets it."""
    nodes = [node(ref=4, role="generic", name="", interactive=False, scrollable=True)]
    text = render(nodes, page_epoch=7, url="https://example.com", title="Example")
    assert '  @e4#7 generic ""*' in text


def test_render_folds_consecutive_same_name_text_siblings() -> None:
    nodes = [
        node(ref=1, role="listitem", name="Row", parent=9, y=0.0, interactive=False),
        node(ref=2, role="listitem", name="Row", parent=9, y=1.0, interactive=False),
        node(ref=3, role="listitem", name="Row", parent=9, y=2.0, interactive=False),
        node(ref=4, role="listitem", name="Row", parent=8, y=3.0, interactive=False),
    ]
    text = render(nodes, page_epoch=7, url="https://example.com", title="Example")
    assert text.count('listitem "Row"') == 2
    assert '  @e1#7 listitem "Row" ×3' in text
    assert "×2" not in text


def test_render_does_not_fold_interactive_duplicates() -> None:
    """Each interactive duplicate is its own target; folding would hide two of its refs."""
    nodes = [
        node(ref=1, name="Delete", parent=9, y=0.0),
        node(ref=2, name="Delete", parent=9, y=1.0),
    ]
    text = render(nodes, page_epoch=7, url="https://example.com", title="Example")
    assert text.count("@e") == 2
    assert "×" not in text


def test_render_does_not_fold_unnamed_siblings() -> None:
    nodes = [
        node(ref=1, role="button", name="", y=0.0),
        node(ref=2, role="button", name="", y=1.0),
    ]
    text = render(nodes, page_epoch=7, url="https://example.com", title="Example")
    assert text.count("@e") == 2


def test_render_reports_nodes_dropped_by_max_nodes() -> None:
    nodes = [
        node(ref=index, role="listitem", name="Row", parent=9, y=float(index), interactive=False)
        for index in range(1, 131)
    ]
    text = render(nodes, page_epoch=7, url="https://example.com", title="Example", max_nodes=120)
    assert "×120" in text
    assert "… (+10 more interactive" in text


def test_render_names_a_nameless_interactive_node_from_its_text() -> None:
    """A clickable div/span often has no accessible name; its text is what the model needs."""
    nodes = [node(ref=5, role="div", name="", text_hint="Some text", interactive=True, scrollable=False)]
    text = render(nodes, page_epoch=7, url="https://example.com", title="Example")
    assert '  @e5#7 div "Some text"' in text


def test_render_names_the_subtree_in_the_header() -> None:
    text = render([node(ref=1)], page_epoch=7, url="https://example.com", title="Example", focus="@e1#7")
    assert text.splitlines()[0] == "[https://example.com] Example  (epoch 7, subtree @e1#7)"
