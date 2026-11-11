# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Leaf-label badges: ``?``/``~``/``!`` markers and right-alignment."""

from __future__ import annotations

from textual.widgets import Tree

from chrys.app.tui.screens.diff.screen import (
    _ALL_TAB,
    _BADGE_LEGENDS,
    _CHANGE_LIST,
    _DIFF_VIEWER_SESSION_TITLE,
    _EXTERNAL_TREE,
    _RUN_TAB,
    _SKIP_REASON_LABELS,
    _TURN_TAB,
    DiffPeriodPane,
    _entry_content_subtitle,
)
from chrys.app.tui.util.diff_entries import DiffFileEntry
from chrys.app.tui.util.diff_labels import entry_source_suffix
from chrys.foundation.i18n import Localizer
from chrys.foundation.i18n.formatting import format_message
from chrys.service.mutations.types import MutationOp
from tests.support.tui_helpers import WidgetApp
from tests.support.waiting import wait_for


def test_diff_chrome_catalog_keeps_english_and_localizes_chinese() -> None:
    title = _DIFF_VIEWER_SESSION_TITLE.bind(session_id="abc12345")

    assert format_message(title) == "Diff Viewer - Session: abc12345"
    assert format_message(_CHANGE_LIST.bind()) == "Change List"
    assert {marker: format_message(definition.bind()) for marker, definition in _BADGE_LEGENDS.items()} == {
        "!": "modified internally and externally",
        "?": "implicitly detected",
        "~": "authorship unverified",
    }
    assert {reason: format_message(definition.bind()) for reason, definition in _SKIP_REASON_LABELS.items()} == {
        "too_large": "file too large",
        "binary": "binary file",
        "unreadable": "file unreadable",
    }

    assert format_message(_EXTERNAL_TREE.bind()) == "External"
    assert format_message(_TURN_TAB.bind(turn_id=2)) == "T2"
    assert format_message(_RUN_TAB.bind(number=2)) == "Run 2"
    assert format_message(_ALL_TAB.bind()) == "All"

    chinese = Localizer("zh-Hans")
    assert chinese.render(_SKIP_REASON_LABELS["unreadable"].bind()) == "无法读取文件"
    assert chinese.render(title) == "文件变更查看器 - 会话：abc12345"  # noqa: RUF001
    assert chinese.render(_CHANGE_LIST.bind()) == "变更列表"
    assert chinese.render(_EXTERNAL_TREE.bind()) == "外部"
    assert chinese.render(_TURN_TAB.bind(turn_id=2)) == "第 2 轮"
    assert chinese.render(_RUN_TAB.bind(number=2)) == "第 2 次运行"
    assert chinese.render(_ALL_TAB.bind()) == "全部"


def _entry(
    rel_path: str = "src/a.py",
    *,
    source: str = "shell",
    contested: bool = False,
    inferred: bool = False,
    content_omitted: str = "",
    operation: MutationOp = MutationOp.MODIFY,
) -> DiffFileEntry:
    return DiffFileEntry(
        path=f"/ws/{rel_path}",
        rel_path=rel_path,
        operation=operation,
        old_path=None,
        before_text="a",
        after_text="b",
        is_binary=False,
        encoding="utf-8",
        bytes_changed=True,
        source=source,
        contested=contested,
        inferred=inferred,
        content_omitted=content_omitted,
    )


def test_badge_markers_and_styles() -> None:
    assert entry_source_suffix(_entry()) == []
    assert entry_source_suffix(_entry(inferred=True)) == [("~", "dim")]
    assert entry_source_suffix(_entry(source="implicit")) == [("?", "bold yellow")]
    # Contested is dim while the row's own attribution is inference…
    assert entry_source_suffix(_entry(inferred=True, contested=True)) == [("~", "dim"), ("!", "dim")]
    assert entry_source_suffix(_entry(source="implicit", contested=True)) == [
        ("?", "bold yellow"),
        ("!", "dim"),
    ]
    # …and bold yellow on proven rows: a hard both-sessions-wrote conflict.
    assert entry_source_suffix(_entry(source="edit_file", contested=True)) == [("!", "bold yellow")]


async def test_full_diff_tree_keeps_all_provenance_markers() -> None:
    entries = [
        _entry("implicit.py", source="implicit"),
        _entry("inferred.py", inferred=True),
        _entry("contested.py", source="edit_file", contested=True),
        _entry("implicit_contested.py", source="implicit", contested=True),
        _entry("inferred_contested.py", inferred=True, contested=True),
    ]
    async with WidgetApp(lambda: DiffPeriodPane(entries)).run_test(size=(100, 24)) as pilot:
        tree = pilot.app.query_one(".workspace-tree", Tree)
        await wait_for(
            lambda: len(tree.root.children) == 5,
            pilot=pilot,
            description="full diff file leaves built",
        )
        labels = {
            node.data.rel_path: node.label.plain for node in tree.root.children if isinstance(node.data, DiffFileEntry)
        }
        for name, markers in {
            "implicit.py": "?",
            "inferred.py": "~",
            "contested.py": "!",
            "implicit_contested.py": "? !",
            "inferred_contested.py": "~ !",
        }.items():
            assert labels[name].endswith(f" {markers}")


def test_no_backup_note_stays_in_body_before_badges() -> None:
    pane = DiffPeriodPane([])
    label = pane._leaf_label(_entry(inferred=True, content_omitted="too_large")).plain
    assert "(no backup)" in label
    assert label.index("(no backup)") < label.index("~")


class _FakeTree:
    """Just enough Tree surface for pad computation."""

    def __init__(self, width: int) -> None:
        from types import SimpleNamespace

        self.scrollable_content_region = SimpleNamespace(width=width)


def test_badge_blocks_end_align_across_depths() -> None:
    entries = [
        _entry("a.py", inferred=True),
        _entry("src/deep/longer_name.py", inferred=True),
        _entry("src/x.py", source="edit_file", contested=True, inferred=True),  # 2 markers
    ]
    pane = DiffPeriodPane(entries)
    guide_depth = 2
    rows = [(e, e.rel_path.count("/") * guide_depth) for e in entries]
    pane._assign_badge_pads(rows, _FakeTree(40))

    for entry, indent in rows:
        label = pane._leaf_label(entry).plain
        # Badge block ends flush at the tree's right edge.
        assert indent + len(label) == 40
        # Full display name always present, never truncated.
        assert entry.rel_path.rsplit("/", 1)[-1] in label
        if entry.inferred and entry.contested:
            assert label.endswith("~ !")


def test_row_wider_than_tree_keeps_trailing_badge() -> None:
    wide = _entry("a_name_longer_than_the_whole_tree_region.py", inferred=True)
    pane = DiffPeriodPane([wide])
    pane._assign_badge_pads([(wide, 0)], _FakeTree(20))
    assert pane._leaf_label(wide).plain.endswith("a_name_longer_than_the_whole_tree_region.py ~")


def test_subtitle_plain_when_no_badges() -> None:
    entry = _entry(source="edit_file")
    entry.after_text = "x\n"
    assert _entry_content_subtitle(entry).plain == "LF - UTF-8"


def test_subtitle_carries_badge_legends_in_fixed_order() -> None:
    entry = _entry(source="implicit", contested=True)
    entry.after_text = "x\n"
    assert _entry_content_subtitle(entry).plain == (
        "!: modified internally and externally - ?: implicitly detected - LF - UTF-8"
    )

    inferred = _entry(inferred=True)
    inferred.after_text = "x"
    assert _entry_content_subtitle(inferred).plain == "~: authorship unverified - No EOL - UTF-8"

    proven_contested = _entry(source="edit_file", contested=True)
    proven_contested.after_text = "x\n"
    assert _entry_content_subtitle(proven_contested).plain == ("!: modified internally and externally - LF - UTF-8")


def test_unlaid_out_tree_falls_back_to_widest_body_column() -> None:
    short = _entry("a.py", inferred=True)
    long = _entry("much_longer_filename.py", inferred=True)
    pane = DiffPeriodPane([short, long])
    pane._assign_badge_pads([(short, 0), (long, 0)], _FakeTree(0))
    assert pane._leaf_label(long).plain.endswith("much_longer_filename.py ~")
    assert pane._badge_pad[short.path] > pane._badge_pad[long.path] == 1
