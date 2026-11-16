# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tests for the sessions browser presentation logic (sort / tree / filter)."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

from rich.cells import cell_len

from chrys.app.tui.screens.sessions.presenter import (
    COLUMNS,
    DEFAULT_SORT_COLUMN,
    DEFAULT_SORT_REVERSE,
    WORKFLOW_COLUMNS,
    build_session_rows,
    build_workflow_rows,
    column_by_key,
    fork_anchors,
    format_tokens,
    rank_search_matches,
    row_cells,
    time_ago,
    workflow_status_labels,
)
from chrys.app.tui.util.formatting import elide_middle
from chrys.foundation.models.session_surface import SessionSurface
from chrys.service.state.session_listing import SessionListingEntry
from chrys.service.state.store import ChatSessionMeta, SessionMeta, WorkflowSessionMeta
from chrys.service.workflows.history import WorkflowRunMeta
from chrys.service.workflows.outcomes import RunStatus

_BASE = datetime(2026, 6, 1, tzinfo=UTC)


def _meta(
    session_id: str,
    *,
    minutes_old: int = 0,
    parent: str = "",
    title: str = "",
    generated_title: str = "",
    prompts: str = "",
    turns: int = 0,
    size: int = 0,
    cwd: str = "",
    profile: str = "Code",
) -> SessionMeta:
    return ChatSessionMeta(
        session_id=session_id,
        agent_profile=profile,
        agent_display_name=profile,
        created_at=_BASE - timedelta(minutes=minutes_old),
        updated_at=_BASE - timedelta(minutes=minutes_old),
        message_count=1,
        turn_count=turns,
        title=title or session_id,
        generated_title=generated_title,
        user_prompt_search_text=prompts,
        size_bytes=size,
        primary_cwd=cwd,
        parent_session_id=parent,
    )


def _ids(rows) -> list[str]:
    return [row.meta.session_id for row in rows]


def _workflow(
    session_id: str,
    *,
    title: str = "Review",
    status: RunStatus = "completed",
    input_text: str = "",
    runs: int = 1,
    minutes_old: int = 0,
    size: int = 0,
) -> WorkflowSessionMeta:
    updated = _BASE - timedelta(minutes=minutes_old)
    return WorkflowSessionMeta(
        session_id=session_id,
        created_at=updated,
        updated_at=updated,
        run_count=runs,
        size_bytes=size,
        latest_run_id="run",
        latest_run=WorkflowRunMeta("run", "review", title, status, updated, input_text),
    )


def _entries(*metas: SessionMeta) -> list[SessionListingEntry]:
    return [
        SessionListingEntry(meta.session_id, meta.updated_at, SessionSurface.TUI, Path(meta.session_id), meta=meta)
        for meta in metas
    ]


def _ranked(metas: Sequence[SessionMeta], query: str) -> list[str]:
    ranked = rank_search_matches(_entries(*metas), query, status_labels=workflow_status_labels())
    return [entry.session_id for entry in ranked]


class TestColumns:
    def test_every_column_key_resolves(self) -> None:
        for column in COLUMNS:
            assert column_by_key(column.key) is column

    def test_unknown_key_falls_back_to_default(self) -> None:
        assert column_by_key("bogus").key == DEFAULT_SORT_COLUMN

    def test_row_cells_cover_every_column(self) -> None:
        cells = row_cells(_meta("abc", turns=7, size=2048), now=_BASE)
        assert set(cells) == {column.key for column in COLUMNS}
        assert cells["turns"] == "7"
        assert cells["size"] == "2.0 KB"


class TestElideMiddle:
    def test_short_text_unchanged(self) -> None:
        assert elide_middle("chrys", 24) == "chrys"

    def test_exact_width_unchanged(self) -> None:
        assert elide_middle("x" * 24, 24) == "x" * 24

    def test_long_name_keeps_head_and_tail(self) -> None:
        name = "Lazy-Theta-with-optimization-any-angle-pathfinding-master"
        assert elide_middle(name, 24) == "Lazy-Theta-w…ding-master"

    def test_cjk_measured_in_cells(self) -> None:
        elided = elide_middle("目" * 20, 24)
        assert cell_len(elided) <= 24
        assert "…" in elided

    def test_directory_cell_is_elided(self) -> None:
        long_dir = "Lazy-Theta-with-optimization-any-angle-pathfinding-master"
        cells = row_cells(_meta("abc", cwd=f"/repos/{long_dir}"), now=_BASE)
        assert cells["directory"] == "Lazy-Theta-w…ding-master"


class TestFormatTokens:
    def test_below_thousand_is_verbatim(self) -> None:
        assert format_tokens(0) == "0"
        assert format_tokens(500) == "500"

    def test_thousands(self) -> None:
        assert format_tokens(3_000) == "3k"
        assert format_tokens(1_500) == "1.5k"
        assert format_tokens(12_345) == "12.3k"

    def test_millions_and_billions(self) -> None:
        assert format_tokens(10_000_000) == "10m"
        assert format_tokens(2_500_000) == "2.5m"
        assert format_tokens(5_000_000_000) == "5b"

    def test_unit_changes_before_a_count_reads_1000k(self) -> None:
        assert format_tokens(999_949) == "999.9k"
        assert format_tokens(999_950) == "1m"
        assert format_tokens(999_999) == "1m"
        assert format_tokens(999_949_999) == "999.9m"
        assert format_tokens(999_950_000) == "1b"
        assert format_tokens(999_999_999) == "1b"


class TestTimeAgo:
    def test_just_now(self) -> None:
        assert time_ago(_BASE, now=_BASE) == "just now"

    def test_days(self) -> None:
        assert time_ago(_BASE - timedelta(days=2), now=_BASE) == "2 days ago"

    def test_twelve_months_read_as_one_year(self) -> None:
        assert time_ago(_BASE - timedelta(days=359), now=_BASE) == "11 months ago"
        assert time_ago(_BASE - timedelta(days=360), now=_BASE) == "1 year ago"
        assert time_ago(_BASE - timedelta(days=364), now=_BASE) == "1 year ago"
        assert time_ago(_BASE - timedelta(days=365), now=_BASE) == "1 year ago"
        assert time_ago(_BASE - timedelta(days=719), now=_BASE) == "1 year ago"
        assert time_ago(_BASE - timedelta(days=720), now=_BASE) == "2 years ago"
        assert time_ago(_BASE - timedelta(days=730), now=_BASE) == "2 years ago"
        assert time_ago(_BASE - timedelta(days=1079), now=_BASE) == "2 years ago"
        assert time_ago(_BASE - timedelta(days=1080), now=_BASE) == "3 years ago"
        assert time_ago(_BASE - timedelta(days=1094), now=_BASE) == "3 years ago"
        assert time_ago(_BASE - timedelta(days=1095), now=_BASE) == "3 years ago"


class TestSorting:
    def test_default_sort_is_last_active_descending(self) -> None:
        rows = build_session_rows(
            [_meta("old", minutes_old=10), _meta("new"), _meta("mid", minutes_old=5)],
            sort_column=DEFAULT_SORT_COLUMN,
            sort_reverse=DEFAULT_SORT_REVERSE,
        )
        assert _ids(rows) == ["new", "mid", "old"]

    def test_sort_by_id_ascending(self) -> None:
        rows = build_session_rows(
            [_meta("bbb"), _meta("aaa"), _meta("ccc")],
            sort_column="id",
            sort_reverse=False,
        )
        assert _ids(rows) == ["aaa", "bbb", "ccc"]

    def test_sort_by_turns_descending(self) -> None:
        rows = build_session_rows(
            [_meta("a", turns=1), _meta("b", turns=30), _meta("c", turns=4)],
            sort_column="turns",
            sort_reverse=True,
        )
        assert _ids(rows) == ["b", "c", "a"]

    def test_sort_by_size_ascending(self) -> None:
        rows = build_session_rows(
            [_meta("a", size=500), _meta("b", size=5), _meta("c", size=50)],
            sort_column="size",
            sort_reverse=False,
        )
        assert _ids(rows) == ["b", "c", "a"]

    def test_sort_by_title_is_case_insensitive(self) -> None:
        rows = build_session_rows(
            [_meta("a", title="zebra"), _meta("b", title="Apple"), _meta("c", title="mango")],
            sort_column="title",
            sort_reverse=False,
        )
        assert _ids(rows) == ["b", "c", "a"]


class TestForkTree:
    def test_children_nest_under_parent_regardless_of_sort(self) -> None:
        sessions = [
            _meta("parent", minutes_old=10),
            _meta("standalone", minutes_old=1),
            _meta("fork", minutes_old=0, parent="parent"),
        ]
        rows = build_session_rows(sessions, sort_column="last_active", sort_reverse=True)
        # fork is newest overall but must render under its (older) parent.
        assert _ids(rows) == ["standalone", "parent", "fork"]
        by_id = {row.meta.session_id: row for row in rows}
        assert by_id["fork"].depth == 1
        assert by_id["fork"].tree_prefix == "└ "
        assert by_id["parent"].depth == 0
        assert by_id["parent"].tree_prefix == ""

    def test_two_fork_levels_use_nested_guides(self) -> None:
        sessions = [
            _meta("root", minutes_old=10),
            _meta("kid-a", minutes_old=8, parent="root"),
            _meta("kid-b", minutes_old=6, parent="root"),
            _meta("grand", minutes_old=4, parent="kid-b"),
        ]
        rows = build_session_rows(sessions, sort_column="last_active", sort_reverse=False)
        assert _ids(rows) == ["root", "kid-a", "kid-b", "grand"]
        by_id = {row.meta.session_id: row for row in rows}
        assert by_id["kid-a"].tree_prefix == "├ "
        assert by_id["kid-b"].tree_prefix == "└ "
        assert by_id["grand"].tree_prefix == "  └ "
        assert by_id["grand"].depth == 2

    def test_siblings_sorted_within_parent(self) -> None:
        sessions = [
            _meta("root", turns=0),
            _meta("kid-small", turns=2, parent="root"),
            _meta("kid-big", turns=9, parent="root"),
        ]
        rows = build_session_rows(sessions, sort_column="turns", sort_reverse=True)
        assert _ids(rows) == ["root", "kid-big", "kid-small"]

    def test_parent_ids_match_across_uuid_and_short_forms(self) -> None:
        # fork_session records the parent's canonical UUID while listings
        # surface short ids — the tree must join both shapes.
        parent_uuid = "4201eebc-ca45-4328-8882-272f3d7c41cb"
        sessions = [
            _meta(parent_uuid, minutes_old=5),
            _meta("child-1", parent=parent_uuid[:14].replace("-", "")[:12]),
            _meta("child-2", parent=parent_uuid, minutes_old=1),
        ]
        rows = build_session_rows(sessions, sort_column="last_active", sort_reverse=True)
        by_id = {row.meta.session_id: row for row in rows}
        assert by_id["child-1"].depth == 1
        assert by_id["child-2"].depth == 1

    def test_missing_parent_lists_child_as_root(self) -> None:
        rows = build_session_rows([_meta("orphan", parent="gone")])
        assert _ids(rows) == ["orphan"]
        assert rows[0].depth == 0
        assert rows[0].tree_prefix == ""

    def test_parent_cycle_is_promoted_not_dropped(self) -> None:
        sessions = [
            _meta("a", parent="b"),
            _meta("b", parent="a"),
            _meta("solo"),
        ]
        rows = build_session_rows(sessions)
        assert sorted(_ids(rows)) == ["a", "b", "solo"]

    def test_self_parent_is_treated_as_root(self) -> None:
        rows = build_session_rows([_meta("weird", parent="weird")])
        assert _ids(rows) == ["weird"]

    def test_deep_fork_chain_does_not_recurse(self) -> None:
        """Traversal is iterative — a 1500-deep fork chain must not blow the stack."""
        sessions = [_meta(f"s{index:05d}", parent=f"s{index - 1:05d}" if index else "") for index in range(1500)]
        rows = build_session_rows(sessions, sort_column="id", sort_reverse=False)
        assert len(rows) == 1500
        assert rows[-1].depth == 1499

        filtered = build_session_rows(sessions, query="s01499")
        assert len(filtered) == 1500  # every ancestor anchors the match
        assert filtered[-1].matched and not filtered[0].matched


class TestFilter:
    def test_query_matches_the_searched_columns(self) -> None:
        sessions = [
            _meta("aaa", title="fix the bug", cwd="/repo/alpha", turns=3),
            _meta("bbb", title="write docs", cwd="/repo/beta", turns=12),
        ]
        assert _ids(build_session_rows(sessions, query="docs")) == ["bbb"]
        assert _ids(build_session_rows(sessions, query="alpha")) == ["aaa"]
        assert _ids(build_session_rows(sessions, query="12")) == ["bbb"]

    def test_size_and_last_active_are_not_searched(self) -> None:
        """Last Active is localized relative time ("2 days ago" matches nearly every row for "days"); Size needs a
        folder walk per session, which a search across every session cannot afford."""
        chat = _meta("aaa", minutes_old=3 * 24 * 60, size=3 * 1024 * 1024)
        workflow = _workflow("wf-1", minutes_old=3 * 24 * 60, size=3 * 1024 * 1024)
        workflow_cells = build_workflow_rows([workflow], now=_BASE)[0].cells
        for cells in (row_cells(chat, now=_BASE), workflow_cells):
            assert (cells["last_active"], cells["size"]) == ("3 days ago", "3.0 MB")
        for query in ("days ago", "ago", "MB"):
            assert build_session_rows([chat], query=query, now=_BASE) == [], query
            assert build_workflow_rows([workflow], query=query, now=_BASE) == [], query
            # The ranking reads no clock, so it is probed with what every relative time shows.
            assert _ranked([chat, workflow], query) == [], query
        for columns in (COLUMNS, WORKFLOW_COLUMNS):
            # Nor highlighted: the table marks the query only in the columns the search reads.
            assert [column.key for column in columns if not column.searched] == ["last_active", "size"]

    def test_query_is_case_insensitive(self) -> None:
        sessions = [_meta("aaa", title="Fix The Bug")]
        assert _ids(build_session_rows(sessions, query="fix the")) == ["aaa"]

    def test_query_matches_only_visible_text(self) -> None:
        """Invisible text (UUID tail, full path) must NOT match — a row
        included without a visible highlight reads as a false positive."""
        full_id = "4201eebc-ca45-4328-8882-272f3d7c41cb"
        sessions = [_meta(full_id, title="some title", cwd="/home/user/deep/project")]
        # Visible: short id prefix and leaf directory name.
        assert _ids(build_session_rows(sessions, query="4201eebcca45")) == [full_id]
        assert _ids(build_session_rows(sessions, query="project")) == [full_id]
        # Invisible: UUID tail beyond the short id, path above the leaf.
        assert build_session_rows(sessions, query="272f3d7c41cb") == []
        assert build_session_rows(sessions, query="/home/user") == []

    def test_matching_fork_keeps_unmatched_ancestors(self) -> None:
        sessions = [
            _meta("parent", title="unrelated"),
            _meta("fork", parent="parent", title="needle in fork"),
            _meta("other", title="unrelated too"),
        ]
        rows = build_session_rows(sessions, query="needle")
        assert _ids(rows) == ["parent", "fork"]
        by_id = {row.meta.session_id: row for row in rows}
        assert by_id["parent"].matched is False
        assert by_id["fork"].matched is True
        assert by_id["fork"].depth == 1

    def test_no_match_returns_empty(self) -> None:
        assert build_session_rows([_meta("aaa")], query="zzz-no-match") == []

    def test_empty_query_marks_all_matched(self) -> None:
        rows = build_session_rows([_meta("aaa"), _meta("bbb", parent="aaa")])
        assert all(row.matched for row in rows)
        assert all(not row.prompt_only and row.prompt_snippet == "" for row in rows)


class TestHiddenPromptTier:
    """Second search tier: user prompt text (``user_prompt_search_text``, falling
    back to the first-prompt ``title``) matches even when invisible,
    ranked after visible-cell hits."""

    def test_later_turn_prompt_matches(self) -> None:
        sessions = [
            _meta(
                "aaa",
                generated_title="修滚动条",
                prompts="先修滚动条\n然后帮我看看 worksteal 为什么闪退",
            ),
            _meta("bbb", generated_title="别的", prompts="完全无关的输入"),
        ]
        rows = build_session_rows(sessions, query="worksteal")
        assert _ids(rows) == ["aaa"]
        assert rows[0].prompt_only is True
        assert "worksteal" in rows[0].prompt_snippet

    def test_hidden_first_prompt_matches(self) -> None:
        sessions = [
            _meta("aaa", title="帮我读一下 kernel 源码", generated_title="架构分层报告"),
            _meta("bbb", title="unrelated prompt", generated_title="别的事情"),
        ]
        rows = build_session_rows(sessions, query="kernel")
        assert _ids(rows) == ["aaa"]
        assert rows[0].matched is True
        assert rows[0].prompt_only is True
        assert "kernel" in rows[0].prompt_snippet

    def test_prompt_only_rows_rank_after_column_matches(self) -> None:
        sessions = [
            # Newest: would list first under the default sort, but only its
            # hidden prompt matches.
            _meta("hidden-hit", title="please fix kernel bug", generated_title="随便聊聊"),
            _meta("visible-hit", minutes_old=10, title="kernel work"),
        ]
        rows = build_session_rows(sessions, query="kernel")
        assert _ids(rows) == ["visible-hit", "hidden-hit"]
        assert rows[0].prompt_only is False
        assert rows[1].prompt_only is True

    def test_displayed_title_match_is_not_prompt_only(self) -> None:
        rows = build_session_rows([_meta("aaa", title="kernel work")], query="kernel")
        assert rows[0].prompt_only is False

    def test_null_title_meta_survives_search(self) -> None:
        """A legacy envelope can carry ``title: null`` and the state reader
        passes it through despite the ``str`` annotation — a non-empty
        search must not crash on the None haystack (review-caught:
        ``None.lower()`` took down the whole browser)."""
        null_title = replace(_meta("nulled"), title=None)
        sessions = [null_title, _meta("matchy", prompts="ask about kernels")]
        rows = build_session_rows(sessions, query="kernels")
        assert _ids(rows) == ["matchy"]
        assert rows[0].prompt_only is True
        assert "kernels" in rows[0].prompt_snippet

    def test_prompt_only_fork_ranks_after_visible_sibling(self) -> None:
        """The partition applies within sibling groups too — a newer
        prompt-only fork must not overtake its visibly matching sibling."""
        sessions = [
            _meta("root", minutes_old=30, title="unrelated root"),
            _meta("fork-prompt", parent="root", prompts="kernel padding question", generated_title="随便聊聊"),
            _meta("fork-cell", minutes_old=10, parent="root", title="kernel visible"),
        ]
        rows = build_session_rows(sessions, query="kernel")
        assert _ids(rows) == ["root", "fork-cell", "fork-prompt"]

    def test_subtree_with_column_match_ranks_before_prompt_only_root(self) -> None:
        sessions = [
            # Prompt-only root is newest — still lists after the subtree
            # whose FORK visibly matches.
            _meta("prompt-root", title="kernel notes", generated_title="其他"),
            _meta("anchor-root", minutes_old=20, title="unrelated"),
            _meta("fork", minutes_old=5, parent="anchor-root", title="kernel deep dive"),
        ]
        rows = build_session_rows(sessions, query="kernel")
        assert _ids(rows) == ["anchor-root", "fork", "prompt-root"]

    def test_snippet_elides_long_prompt_context(self) -> None:
        prompt = "x" * 100 + " kernel " + "y" * 100
        rows = build_session_rows([_meta("aaa", title=prompt, generated_title="短标题")], query="kernel")
        snippet = rows[0].prompt_snippet
        assert snippet.startswith("…") and snippet.endswith("…")
        assert "kernel" in snippet
        assert len(snippet) < len(prompt)

    def test_snippet_survives_length_changing_lowercase(self) -> None:
        """ "İ" lowers to TWO code points, so a match index found in the
        lowered prompt drifts right of the real match in the source —
        enough İs pushed the old snippet window past the end of the
        string, yielding an empty tooltip (review-caught)."""
        prompt = "İ" * 40 + " flux heisenbug"
        rows = build_session_rows(
            [_meta("aaa", generated_title="短标题", prompts=prompt)],
            query="heisenbug",
        )
        assert rows[0].prompt_only is True
        assert "heisenbug" in rows[0].prompt_snippet

    def test_snippet_uses_whole_string_lowercase_match_position(self) -> None:
        """The snippet must anchor on the SAME lowered string the row
        filter searched.  Whole-string lower() gives a word-final capital
        sigma its final form, so a medial-sigma query only matches the
        later word-medial one — re-finding in a char-by-char lowered copy
        (where every capital sigma takes the medial form) would point the
        snippet at the first AΣ, evidence the filter never matched
        (review-caught)."""
        prompt = "İ AΣ " + "x" * 60 + " AΣA"
        rows = build_session_rows(
            [_meta("aaa", generated_title="短标题", prompts=prompt)],
            query="σ",  # noqa: RUF001
        )
        assert rows[0].prompt_only is True
        snippet = rows[0].prompt_snippet
        assert "AΣA" in snippet
        assert "İ" not in snippet


class TestRankSearchMatches:
    """The ranking a search across every session pages through: the same rule as the rows, cell matches first."""

    def test_visible_matches_rank_before_prompt_only_ones_each_in_listing_order(self) -> None:
        sessions = [
            _meta("p-newest", prompts="ask about kernels", generated_title="其他"),
            _meta("c-newer", minutes_old=5, title="kernel work"),
            _meta("none", minutes_old=7, title="unrelated"),
            _meta("p-older", minutes_old=9, prompts="the kernel again", generated_title="别的"),
            _meta("c-oldest", minutes_old=11, cwd="/repos/kernel"),
        ]
        assert _ranked(sessions, "  KERNEL ") == ["c-newer", "c-oldest", "p-newest", "p-older"]
        assert _ranked(sessions, "nothing like it") == []

    def test_ranking_selects_exactly_the_rows_the_page_shows(self) -> None:
        """The page re-filters its fresh metas by the same rule, so a result never vanishes from its page."""
        sessions = [
            _meta("aaa", title="fix the bug", cwd="/repo/alpha", turns=3, size=5 * 1024 * 1024),
            _meta("bbb", minutes_old=60, title="write docs", prompts="update the alpha guide"),
            _meta("ccc", minutes_old=90, parent="aaa", title="forked fix", prompts="more bug work"),
            _meta("4201eebc-ca45-4328-8882-272f3d7c41cb", title="uuid", cwd="/home/user/deep/project"),
        ]
        for query in ("alpha", "bug", "3", "MB", "ago", "project", "/home/user", "4201eebcca45", "272f3d7c41cb"):
            rows = build_session_rows(sessions, query=query)
            assert set(_ranked(sessions, query)) == {row.meta.session_id for row in rows if row.matched}, query

    def test_workflow_ranking_selects_exactly_the_rows_the_page_shows(self) -> None:
        sessions: list[SessionMeta] = [
            _workflow("wf-1", title="Code review", input_text="check the parser", size=5 * 1024 * 1024),
            _workflow("wf-2", minutes_old=60, title="Release notes", status="failed", input_text="review the draft"),
            _workflow("wf-3", minutes_old=90, title="Lint", status="running", runs=12),
        ]
        for query in ("review", "parser", "error", "running", "12", "wf1", "MB", "ago"):
            rows = build_workflow_rows(sessions, query=query, now=_BASE)
            ranked = _ranked(sessions, query)
            cell_matches = {row.meta.session_id for row in rows if not row.prompt_only}
            prompt_only = {row.meta.session_id for row in rows if row.prompt_only}
            assert (set(ranked[: len(cell_matches)]), set(ranked[len(cell_matches) :])) == (
                cell_matches,
                prompt_only,
            ), query

    def test_fork_anchors_are_the_unmatched_parents_up_each_fork_chain(self) -> None:
        """A result page holds only matches: these come along so its forks keep the tree the rows show."""
        entries = _entries(
            _meta("root", title="plain root"),
            _meta("mid", parent="root", title="plain middle"),
            _meta("leaf", parent="mid", title="needle leaf"),
            _meta("matched", title="needle parent"),
            _meta("child", parent="matched", title="needle child"),
            _meta("loop-a", parent="loop-b", title="plain a"),
            _meta("loop-b", parent="loop-a", title="plain b"),
            _meta("in-loop", parent="loop-a", title="needle in a loop"),
        )
        ranked = rank_search_matches(entries, "needle", status_labels=workflow_status_labels())
        # "matched" lists on another result page, where the fork's own chain ends.
        page = [entry for entry in ranked if entry.session_id != "matched"]
        anchors = fork_anchors(page, entries, ranked)
        # A corrupt parent cycle ends at the anchor it started from.
        assert [entry.session_id for entry in anchors] == ["mid", "root", "loop-a", "loop-b"]
        assert fork_anchors([entry for entry in ranked if entry.session_id == "child"], entries, ranked) == ()

    def test_workflow_sessions_match_their_run_title_status_and_input(self) -> None:
        sessions: list[SessionMeta] = [
            _workflow("wf-1", title="Code review", status="completed", input_text="check the parser"),
            _workflow("wf-2", title="Release notes", status="failed", input_text="draft notes"),
            _workflow("wf-3", title="ignored"),
        ]
        third = sessions[2]
        assert isinstance(third, WorkflowSessionMeta)
        third.latest_run = None
        assert _ranked(sessions, "review") == ["wf-1"]
        assert _ranked(sessions, "error") == ["wf-2"]  # the label the Status column shows
        assert _ranked(sessions, "failed") == []  # the internal status name shows nowhere
        assert _ranked(sessions, "parser") == ["wf-1"]
        assert _ranked(sessions, "ignored") == []
        rows = build_workflow_rows(sessions, query="parser")
        assert [(row.meta.session_id, row.prompt_only) for row in rows] == [("wf-1", True)]
        assert set(rows[0].cells) == {column.key for column in WORKFLOW_COLUMNS}
