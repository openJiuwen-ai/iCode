# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tests for the suggestion handler core: popup presentation, prompt history, and # / $ triggers."""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Callable
from dataclasses import replace

import pytest
from rich.cells import cell_len
from textual.css.query import NoMatches

from chrys.app.tui.i18n import LocaleController
from chrys.app.tui.screens.main import suggestions as suggestions_module
from chrys.app.tui.screens.main.suggestions import SuggestionHandler
from chrys.app.tui.screens.main.view_adapter import MainScreenViewAdapter
from chrys.app.tui.support import prompt_search as prompt_search_module
from chrys.app.tui.support.prompt_search import PromptMatch, PromptSearchIndex, PromptSearchResult
from chrys.app.tui.widgets.chrome.suggestion_list import SuggestionItem
from chrys.foundation.config.settings import Settings
from chrys.foundation.events.types import AgentRuntimeDetails, RuntimeSkillDetails
from chrys.service.profiles.models.registry import ModelProfileRegistry
from chrys.service.profiles.models.schema import ModelProfile
from chrys.service.prompt_shortcuts import PromptShortcut
from tests.support.tui_helpers import (
    AgentProfileStub,
    AgentRegistryStub,
    SuggestionScreen,
    make_suggestion_handler,
    make_suggestion_screen,
    suggestion_values,
    wait_for_prompt_history_query,
)
from tests.support.waiting import ENGINE_TURN_TIMEOUT, wait_for


def test_view_adapter_suggestions_ignore_teardown_nomatches() -> None:
    screen = make_suggestion_screen()

    def query_one(cls):
        if cls.__name__ == "SuggestionList":
            raise NoMatches("SuggestionList")
        return screen.input_bar

    screen.query_one = query_one
    view = MainScreenViewAdapter(screen, state=screen.state)  # type: ignore[arg-type]

    view.show_suggestions("files", [SuggestionItem(value="a.py", label="a.py", kind="file")])
    view.show_suggestions_loading("files", title="Files")
    view.update_suggestions([SuggestionItem(value="b.py", label="b.py", kind="file")])
    view.hide_suggestions()

    assert screen.input_bar.replacements == []


async def test_a_shortcut_read_landing_while_the_screen_is_torn_down_reads_no_widget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Removal and shutdown prune the screen's widgets while the screen is still
    attached: a shortcut read that lands then keeps its result but reads no
    draft and refreshes no list, as a widget it asked for would be gone."""
    started = threading.Event()
    release = threading.Event()
    landed = PromptShortcut(name="zznew", template="New body", path="/somewhere/zznew.md")

    def held_read(**_folders: object) -> list[PromptShortcut]:
        started.set()
        assert release.wait(ENGINE_TURN_TIMEOUT)
        return [landed]

    monkeypatch.setattr(suggestions_module, "load_prompt_shortcuts", held_read)
    screen = make_suggestion_screen()
    handler = make_suggestion_handler(screen)
    handler.build_slash_commands()
    screen.input_bar.value = "/zz"
    handler.on_slash_triggered()
    assert screen.input_bar.suggestions_active
    [read] = screen.workers
    assert await asyncio.to_thread(started.wait, ENGINE_TURN_TIMEOUT)

    # Teardown begins as Textual runs it: the screen is marked, then its widgets go,
    # innermost first: the input bar is still there, still showing the list, when
    # its own text area is not.
    screen._pruning = True

    class _PrunedInputBar:
        suggestions_active = True

        @property
        def value(self) -> str:
            raise NoMatches("#chat-input")

    pruned_bar = _PrunedInputBar()

    def pruned(cls: type) -> object:
        if cls.__name__ == "InputBar":
            return pruned_bar
        raise NoMatches(cls.__name__)

    screen.query_one = pruned
    release.set()
    await asyncio.wait_for(read.wait(), ENGINE_TURN_TIMEOUT)

    assert handler._shortcuts() == [landed]


def test_a_typed_name_resolves_to_one_answer_every_entry_acts_on() -> None:
    """Command, then skill, then shortcut (by name, case-insensitively); anything else names nothing."""
    screen = make_suggestion_screen()
    handler = make_suggestion_handler(screen)
    handler.build_slash_commands()
    screen.state.runtime.details = AgentRuntimeDetails(skill_details=[RuntimeSkillDetails(name="zzskill")])
    shortcut = PromptShortcut(name="ZzTrans", template="Translate", path="/somewhere/ZzTrans.md")
    # The read for the folders in force has landed.
    handler._prompt_shortcuts = [shortcut]
    handler._prompt_shortcuts_folders = handler._prompt_shortcut_folders()

    assert handler.resolve_typed_name("/help me").kind == "command"
    assert handler.resolve_typed_name("/zzskill do it").kind == "skill"
    typed = handler.resolve_typed_name("/zztrans formal hi")
    assert (typed.kind, typed.shortcut, typed.arguments) == ("shortcut", shortcut, "formal hi")
    assert handler.resolve_typed_name("/etc/hosts is broken").kind == "none"
    assert handler.resolve_typed_name("plain text").kind == "none"


def test_show_suggestions_titles_popup_per_mode() -> None:
    """The popup border names what is being suggested; files mode names the
    same root the file scanner is scoped to."""
    screen = make_suggestion_screen()
    handler = make_suggestion_handler(screen)
    screen.state.workspace_marker.current_cwd = "/tmp/title-root"

    handler._show_suggestions("commands", [])
    assert screen.suggestion_list.last_title == "Commands"

    handler._show_suggestions("agents", [])
    assert screen.suggestion_list.last_title == "Agents"

    handler._show_suggestions("models", [])
    assert screen.suggestion_list.last_title == "Models"

    handler._show_suggestions("files", [])
    assert screen.suggestion_list.last_title == "Files under /tmp/title-root"

    handler._show_suggestions("history", [])
    assert screen.suggestion_list.last_title == "Prompt History"


async def test_prompt_history_suggestions_are_global_recent_first_and_single_line() -> None:
    screen = make_suggestion_screen()
    screen.input_bar.prompt_history = ["old prompt", "middle\nline", "latest\r\nprompt"]
    handler = make_suggestion_handler(screen)

    await handler.show_prompt_history_async()
    await wait_for_prompt_history_query(handler)

    assert screen.input_bar.prompt_history_limits == [1000]
    assert screen.suggestion_list.last_mode == "history"
    assert screen.suggestion_list.last_title == "Prompt History"
    items = screen.suggestion_list.last_items
    assert [item.value for item in items if isinstance(item, SuggestionItem)] == [
        "latest\r\nprompt",
        "middle\nline",
        "old prompt",
    ]
    assert [item.label for item in items if isinstance(item, SuggestionItem)] == [
        "latest ↵ prompt",
        "middle ↵ line",
        "old prompt",
    ]
    assert all(item.kind == "history" for item in items if isinstance(item, SuggestionItem))
    assert all(item.marquee_start == 0 for item in items if isinstance(item, SuggestionItem))


async def test_long_prompt_history_label_is_cut_but_selection_keeps_the_whole_prompt() -> None:
    long_prompt = "line one\n" + "x" * 5000
    screen = make_suggestion_screen()
    screen.input_bar.prompt_history = [long_prompt]
    handler = make_suggestion_handler(screen)

    await handler.show_prompt_history_async()
    await wait_for_prompt_history_query(handler)

    (item,) = screen.suggestion_list.last_items
    assert isinstance(item, SuggestionItem)
    assert item.value == long_prompt
    assert item.label == "line one ↵ " + "x" * (300 - len("line one\n")) + "…"

    handler.on_suggestion_selected("history", item.value, execute=False, kind="history")
    assert screen.input_bar.value == long_prompt


def _matched_parts(item: SuggestionItem) -> list[str]:
    assert isinstance(item.label, str)
    return [item.label[start:end] for start, end in item.match_spans]


async def test_prompt_history_preview_shows_and_marks_the_match_past_the_head() -> None:
    prompt = "x" * 500 + " then the session restore failed " + "y" * 500
    screen = make_suggestion_screen()
    screen.input_bar.value = "restore"
    screen.input_bar.prompt_history = [prompt]
    handler = make_suggestion_handler(screen)

    await handler.show_prompt_history_async()
    await wait_for_prompt_history_query(handler)

    (item,) = screen.suggestion_list.last_items
    assert isinstance(item, SuggestionItem)
    assert isinstance(item.label, str)
    assert item.label.startswith("…")
    assert item.label.endswith("…")
    assert "then the session restore failed" in item.label
    assert _matched_parts(item) == ["restore"]
    assert item.value == prompt


async def test_prompt_history_preview_marks_the_match_and_sanitizes_controls() -> None:
    screen = make_suggestion_screen()
    screen.input_bar.value = "restore"
    screen.input_bar.prompt_history = ["\x1b[31msession restore\nnext line"]
    handler = make_suggestion_handler(screen)

    await handler.show_prompt_history_async()
    await wait_for_prompt_history_query(handler)

    (item,) = screen.suggestion_list.last_items
    assert isinstance(item, SuggestionItem)
    assert item.label == "\ufffd[31msession restore ↵ next line"
    assert _matched_parts(item) == ["restore"]


async def test_prompt_history_keeps_only_the_newest_copy_of_each_prompt() -> None:
    screen = make_suggestion_screen()
    screen.input_bar.prompt_history = ["continue", "fix bug", "continue", "write docs", "continue"]
    handler = make_suggestion_handler(screen)

    await handler.show_prompt_history_async()
    await wait_for_prompt_history_query(handler)

    assert suggestion_values(screen.suggestion_list.last_items) == ["continue", "write docs", "fix bug"]


async def test_prompt_history_searches_every_stored_prompt_but_lists_the_best_hundred() -> None:
    screen = make_suggestion_screen()
    screen.input_bar.prompt_history = ["needle from long ago"] + [f"prompt {n}" for n in range(500)]
    handler = make_suggestion_handler(screen)

    await handler.show_prompt_history_async()
    await wait_for_prompt_history_query(handler)
    assert screen.input_bar.prompt_history_limits == [1000]
    assert len(screen.suggestion_list.last_items) == 100
    assert suggestion_values(screen.suggestion_list.last_items)[0] == "prompt 499"

    handler.on_text_changed("needle")
    await wait_for_prompt_history_query(handler)
    assert suggestion_values(screen.suggestion_list.last_items) == ["needle from long ago"]


async def test_prompt_history_title_counts_matches_while_filtering() -> None:
    screen = make_suggestion_screen()
    screen.input_bar.prompt_history = [f"docs {n}" for n in range(150)] + ["fix bug"]
    handler = make_suggestion_handler(screen)

    await handler.show_prompt_history_async()
    await wait_for_prompt_history_query(handler)
    assert screen.suggestion_list.last_title == "Prompt History"

    handler.on_text_changed("fix")
    await wait_for_prompt_history_query(handler)
    assert screen.suggestion_list.last_title == "Prompt History · 1 match"

    handler.on_text_changed("docs")
    await wait_for_prompt_history_query(handler)
    assert screen.suggestion_list.last_title == "Prompt History · 100 of 150 matches"

    handler.on_text_changed("zzz")
    await wait_for_prompt_history_query(handler)
    assert screen.suggestion_list.last_title == "Prompt History"


async def test_reopening_prompt_history_reuses_indexed_prompts(monkeypatch: pytest.MonkeyPatch) -> None:
    indexed: list[str] = []
    real_index_entry = prompt_search_module._index_entry

    def counting_index_entry(prompt: str):
        indexed.append(prompt)
        return real_index_entry(prompt)

    monkeypatch.setattr(prompt_search_module, "_index_entry", counting_index_entry)
    screen = make_suggestion_screen()
    screen.input_bar.prompt_history = ["first", "second"]
    handler = make_suggestion_handler(screen)
    await handler.show_prompt_history_async()
    await wait_for_prompt_history_query(handler)
    handler.dismiss_suggestions()

    screen.input_bar.prompt_history = ["first", "second", "third"]
    await handler.show_prompt_history_async()
    await wait_for_prompt_history_query(handler)

    assert indexed == ["second", "first", "third"]
    assert suggestion_values(screen.suggestion_list.last_items) == ["third", "second", "first"]


async def test_prompt_history_grown_too_large_to_keep_is_not_cached(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(suggestions_module, "_PROMPT_HISTORY_CACHE_MAX_CHARS", 30)
    screen = make_suggestion_screen()
    screen.input_bar.prompt_history = ["older prompt", "newer prompt"]
    handler = make_suggestion_handler(screen)
    await handler.show_prompt_history_async()
    await wait_for_prompt_history_query(handler)
    assert handler._prompt_history_index_cache is not None

    handler.dismiss_suggestions()
    screen.input_bar.prompt_history = ["older prompt", "newer prompt", "a much longer newest prompt"]
    await handler.show_prompt_history_async()
    await wait_for_prompt_history_query(handler)

    # Still searchable while open; only the copy kept for reopening is dropped.
    assert suggestion_values(screen.suggestion_list.last_items) == [
        "a much longer newest prompt",
        "newer prompt",
        "older prompt",
    ]
    assert handler._prompt_history_index_cache is None


async def test_reopening_prompt_history_shows_cached_results_before_the_reload_finishes() -> None:
    screen = make_suggestion_screen()
    screen.input_bar.prompt_history = ["older", "newer"]
    handler = make_suggestion_handler(screen)
    await handler.show_prompt_history_async()
    await wait_for_prompt_history_query(handler)
    handler.dismiss_suggestions()

    started = asyncio.Event()
    release = asyncio.Event()

    async def slow_load(*, max_entries: int) -> list[str]:
        assert max_entries == 1000
        started.set()
        await release.wait()
        return ["older", "newer", "newest"]

    screen.input_bar.load_prompt_history = slow_load  # type: ignore[method-assign]
    revision = handler.start_prompt_history()
    task = asyncio.create_task(handler.show_prompt_history_async(revision=revision))
    await started.wait()
    await wait_for_prompt_history_query(handler)
    # The reload is still running, yet the earlier prompts are already listed.
    assert suggestion_values(screen.suggestion_list.last_items) == ["newer", "older"]

    release.set()
    await task
    await wait_for_prompt_history_query(handler)
    assert suggestion_values(screen.suggestion_list.last_items) == ["newest", "newer", "older"]


async def test_enter_on_cached_history_waits_for_a_prompt_submitted_just_before() -> None:
    screen = make_suggestion_screen()
    screen.input_bar.prompt_history = ["older"]
    handler = make_suggestion_handler(screen)
    await handler.show_prompt_history_async()
    await wait_for_prompt_history_query(handler)
    handler.dismiss_suggestions()

    started = asyncio.Event()
    release = asyncio.Event()

    async def slow_load(*, max_entries: int) -> list[str]:
        started.set()
        await release.wait()
        return ["older", "just submitted"]

    screen.input_bar.load_prompt_history = slow_load  # type: ignore[method-assign]
    revision = handler.start_prompt_history()
    task = asyncio.create_task(handler.show_prompt_history_async(revision=revision))
    await started.wait()
    await wait_for_prompt_history_query(handler)
    assert suggestion_values(screen.suggestion_list.last_items) == ["older"]
    # The cached rows lack the newest prompt: Enter waits for the reload.
    assert handler.on_suggestion_select(execute=True) is True
    assert screen.suggestion_list.selections == []

    release.set()
    await task
    await wait_for_prompt_history_query(handler)
    assert suggestion_values(screen.suggestion_list.last_items) == ["just submitted", "older"]
    assert screen.suggestion_list.last_keep_selection is False
    assert screen.suggestion_list.selections == [True]


async def test_enter_on_cached_history_applies_once_an_unchanged_reload_lands() -> None:
    screen = make_suggestion_screen()
    screen.input_bar.prompt_history = ["older"]
    handler = make_suggestion_handler(screen)
    await handler.show_prompt_history_async()
    await wait_for_prompt_history_query(handler)
    handler.dismiss_suggestions()

    release = asyncio.Event()

    async def slow_load(*, max_entries: int) -> list[str]:
        await release.wait()
        return ["older"]

    screen.input_bar.load_prompt_history = slow_load  # type: ignore[method-assign]
    revision = handler.start_prompt_history()
    task = asyncio.create_task(handler.show_prompt_history_async(revision=revision))
    await wait_for_prompt_history_query(handler)
    assert handler.on_suggestion_select(execute=True) is True
    assert screen.suggestion_list.selections == []

    release.set()
    await task
    assert screen.suggestion_list.selections == [True]


class _GatedRankIndex(PromptSearchIndex):
    """Holds each rank in its thread while ``gate`` is clear."""

    def __init__(self, prompts: list[str], *, previous: PromptSearchIndex | None = None) -> None:
        super().__init__(prompts, previous=previous)
        self.gate = threading.Event()
        self.gate.set()
        self.waiting = threading.Event()

    def search(
        self,
        query: str,
        *,
        limit: int,
        cells: int,
        head_chars: int,
        should_stop: Callable[[], bool] | None = None,
    ) -> PromptSearchResult:
        if not self.gate.is_set():
            self.waiting.set()
            self.gate.wait(timeout=ENGINE_TURN_TIMEOUT)
        return super().search(query, limit=limit, cells=cells, head_chars=head_chars, should_stop=should_stop)


async def test_reopening_shows_cached_results_when_an_unchanged_reload_lands_mid_search(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    built: list[_GatedRankIndex] = []

    def build(entries: list[str], *, previous: PromptSearchIndex | None = None) -> _GatedRankIndex:
        built.append(_GatedRankIndex(entries, previous=previous))
        return built[-1]

    monkeypatch.setattr(suggestions_module, "PromptSearchIndex", build)
    screen = make_suggestion_screen()
    screen.input_bar.prompt_history = ["older", "newer"]
    handler = make_suggestion_handler(screen)
    await handler.show_prompt_history_async()
    await wait_for_prompt_history_query(handler)
    handler.dismiss_suggestions()
    cached = built[0]
    cached.gate.clear()

    reopen = asyncio.create_task(handler.show_prompt_history_async())
    await wait_for(cached.waiting.is_set, description="search of the cached index held in its thread")
    # The reload finds the same prompts while that search is still running.
    await reopen
    cached.gate.set()
    await wait_for_prompt_history_query(handler)

    assert screen.suggestion_list.is_loading is False
    assert suggestion_values(screen.suggestion_list.last_items) == ["newer", "older"]


async def test_prompt_history_previews_refit_when_the_row_width_changes() -> None:
    prompt = "x" * 200 + " the session restore failed " + "y" * 200
    screen = make_suggestion_screen()
    screen.input_bar.value = "restore"
    screen.input_bar.prompt_history = [prompt]
    handler = make_suggestion_handler(screen)
    await handler.show_prompt_history_async()
    await wait_for_prompt_history_query(handler)
    (wide,) = screen.suggestion_list.last_items
    assert isinstance(wide, SuggestionItem)
    assert isinstance(wide.label, str)

    screen.suggestion_list.row_width = 30
    handler.on_suggestion_row_width_changed()
    await wait_for_prompt_history_query(handler)

    (narrow,) = screen.suggestion_list.last_items
    assert isinstance(narrow, SuggestionItem)
    assert isinstance(narrow.label, str)
    # A narrower row keeps less context before the match; both run on past the edge to scroll.
    assert 0 < narrow.label.index("restore") < wide.label.index("restore")
    assert len(narrow.label) > 30
    assert _matched_parts(narrow) == ["restore"]
    # The same rows refit: the list keeps the prompt the user selected.
    assert screen.suggestion_list.last_keep_selection is True

    handler.on_text_changed("session")
    await wait_for_prompt_history_query(handler)
    assert screen.suggestion_list.last_keep_selection is False


def test_prompt_history_previews_of_crlf_prompts_fit_the_row() -> None:
    prompt = "first line\r\nsecond line with docs\r\nthird\r\n" * 3
    for cells in range(12, 41):
        (match,) = PromptSearchIndex([prompt]).search("docs third", limit=1, cells=cells, head_chars=60).matches
        # Only the part before the scroll tail has to fit the row.
        label, _spans = SuggestionHandler._prompt_history_label(replace(match, tail_end=match.windows[-1][1]))
        assert cell_len(label) <= cells, (cells, label)


async def test_prompt_history_turned_off_says_so() -> None:
    screen = make_suggestion_screen()
    screen.input_bar.prompt_history = []
    screen.input_bar.prompt_history_on = False
    handler = make_suggestion_handler(screen)

    await handler.show_prompt_history_async()
    await wait_for_prompt_history_query(handler)

    assert screen.suggestion_list.last_items == []
    assert screen.suggestion_list.last_empty_text == (
        "Prompt history is off. Turn on Save prompt history in settings and restart to use it."
    )


async def test_prompt_history_opens_loading_popup_before_history_is_ready() -> None:
    screen = make_suggestion_screen()
    started = asyncio.Event()
    release = asyncio.Event()

    async def load_prompt_history(*, max_entries: int) -> list[str]:
        assert max_entries == 1000
        started.set()
        await release.wait()
        return ["ready prompt"]

    screen.input_bar.load_prompt_history = load_prompt_history  # type: ignore[method-assign]
    handler = make_suggestion_handler(screen)
    revision = handler.start_prompt_history()
    assert screen.suggestion_list.last_mode == "history"
    assert screen.suggestion_list.last_title == "Prompt History"
    assert screen.suggestion_list.is_loading is True
    assert screen.suggestion_list.last_items == []

    task = asyncio.create_task(handler.show_prompt_history_async(revision=revision))
    await started.wait()
    release.set()
    await task
    await wait_for_prompt_history_query(handler)

    assert screen.suggestion_list.is_loading is False
    assert suggestion_values(screen.suggestion_list.last_items) == ["ready prompt"]


async def test_mode_switch_during_prompt_history_load_does_not_replace_new_suggestions() -> None:
    screen = make_suggestion_screen()
    started = asyncio.Event()
    release = asyncio.Event()

    async def load_prompt_history(*, max_entries: int) -> list[str]:
        assert max_entries == 1000
        started.set()
        await release.wait()
        return ["stale prompt"]

    screen.input_bar.load_prompt_history = load_prompt_history  # type: ignore[method-assign]
    handler = make_suggestion_handler(screen)
    revision = handler.start_prompt_history()
    task = asyncio.create_task(handler.show_prompt_history_async(revision=revision))
    await started.wait()

    handler._show_suggestions("commands", [SuggestionItem(value="new", label="new")])
    release.set()
    await task
    await wait_for_prompt_history_query(handler)

    assert screen.suggestion_list.last_mode == "commands"
    assert screen.suggestion_list.is_loading is False
    assert suggestion_values(screen.suggestion_list.last_items) == ["new"]


def test_prompt_history_selection_restores_original_multiline_prompt() -> None:
    screen = make_suggestion_screen()
    screen.suggestion_list.is_visible = True
    handler = make_suggestion_handler(screen)
    handler._suggestion_mode = "history"

    handler.on_suggestion_selected("history", "first line\nsecond line", execute=False, kind="history")

    assert screen.input_bar.value == "first line\nsecond line"
    assert screen.suggestion_list.is_visible is False
    assert screen.submitted == []


async def test_typing_filters_prompt_history_and_clearing_restores_all() -> None:
    screen = make_suggestion_screen()
    screen.input_bar.prompt_history = ["Fix the session bug", "write docs", "Session storage\nnotes"]
    handler = make_suggestion_handler(screen)
    await handler.show_prompt_history_async()
    await wait_for_prompt_history_query(handler)

    handler.on_text_changed("session")
    await wait_for_prompt_history_query(handler)
    assert suggestion_values(screen.suggestion_list.last_items) == ["Session storage\nnotes", "Fix the session bug"]

    handler.on_text_changed("SESSION bug")
    await wait_for_prompt_history_query(handler)
    assert suggestion_values(screen.suggestion_list.last_items) == ["Fix the session bug"]

    handler.on_text_changed("nothing matches")
    await wait_for_prompt_history_query(handler)
    assert screen.suggestion_list.last_items == []
    assert screen.suggestion_list.last_empty_text == "No matching prompts. Try changing or clearing the input."
    assert screen.suggestion_list.is_visible is True
    assert handler.suggestion_mode == "history"

    handler.on_text_changed("")
    await wait_for_prompt_history_query(handler)
    assert suggestion_values(screen.suggestion_list.last_items) == [
        "Session storage\nnotes",
        "write docs",
        "Fix the session bug",
    ]


async def test_empty_prompt_history_says_there_is_no_history_yet() -> None:
    screen = make_suggestion_screen()
    screen.input_bar.value = "anything"
    screen.input_bar.prompt_history = []
    handler = make_suggestion_handler(screen)

    await handler.show_prompt_history_async()
    await wait_for_prompt_history_query(handler)

    assert screen.suggestion_list.last_items == []
    assert screen.suggestion_list.last_empty_text == "No prompt history yet."


async def test_prompt_history_opens_filtered_by_the_draft() -> None:
    screen = make_suggestion_screen()
    screen.input_bar.value = "docs"
    screen.input_bar.prompt_history = ["write docs", "fix bug", "review docs"]
    handler = make_suggestion_handler(screen)

    await handler.show_prompt_history_async()
    await wait_for_prompt_history_query(handler)
    assert suggestion_values(screen.suggestion_list.last_items) == ["review docs", "write docs"]

    handler.on_text_changed("docs review")
    await wait_for_prompt_history_query(handler)
    assert suggestion_values(screen.suggestion_list.last_items) == ["review docs"]


async def test_typing_while_prompt_history_loads_filters_loaded_entries() -> None:
    screen = make_suggestion_screen()
    started = asyncio.Event()
    release = asyncio.Event()

    async def load_prompt_history(*, max_entries: int) -> list[str]:
        assert max_entries == 1000
        started.set()
        await release.wait()
        return ["alpha", "beta"]

    screen.input_bar.load_prompt_history = load_prompt_history  # type: ignore[method-assign]
    handler = make_suggestion_handler(screen)
    revision = handler.start_prompt_history()
    task = asyncio.create_task(handler.show_prompt_history_async(revision=revision))
    await started.wait()

    screen.input_bar.value = "bet"
    handler.on_text_changed("bet")
    assert screen.suggestion_list.is_loading is True
    release.set()
    await task
    await wait_for_prompt_history_query(handler)

    assert suggestion_values(screen.suggestion_list.last_items) == ["beta"]


async def test_dismiss_drops_loaded_prompt_history() -> None:
    screen = make_suggestion_screen()
    screen.input_bar.prompt_history = ["alpha"]
    handler = make_suggestion_handler(screen)
    await handler.show_prompt_history_async()
    await wait_for_prompt_history_query(handler)

    handler.dismiss_suggestions()
    handler.on_text_changed("alpha")
    await wait_for_prompt_history_query(handler)

    assert screen.suggestion_list.is_visible is False
    assert handler.suggestion_mode is None


class _HeldRankIndex(PromptSearchIndex):
    """Records each ranked query and its thread; holds the first rank until released."""

    def __init__(self, prompts: list[str], *, previous: PromptSearchIndex | None = None) -> None:
        super().__init__(prompts, previous=previous)
        self.queries: list[str] = []
        self.threads: list[int] = []
        self.stops: list[Callable[[], bool] | None] = []
        self.first_started = threading.Event()
        self.release_first = threading.Event()

    def search(
        self,
        query: str,
        *,
        limit: int,
        cells: int,
        head_chars: int,
        should_stop: Callable[[], bool] | None = None,
    ) -> PromptSearchResult:
        self.queries.append(query)
        self.threads.append(threading.get_ident())
        self.stops.append(should_stop)
        if len(self.queries) == 1:
            self.first_started.set()
            self.release_first.wait(timeout=ENGINE_TURN_TIMEOUT)
        return super().search(query, limit=limit, cells=cells, head_chars=head_chars, should_stop=should_stop)


async def _open_history_with_held_rank(
    monkeypatch: pytest.MonkeyPatch, prompts: list[str]
) -> tuple[SuggestionScreen, SuggestionHandler, _HeldRankIndex]:
    """Open prompt history whose first ranking blocks in its thread until released."""
    built: list[_HeldRankIndex] = []

    def build(entries: list[str], *, previous: PromptSearchIndex | None = None) -> _HeldRankIndex:
        built.append(_HeldRankIndex(entries, previous=previous))
        return built[-1]

    monkeypatch.setattr(suggestions_module, "PromptSearchIndex", build)
    screen = make_suggestion_screen()
    screen.input_bar.prompt_history = prompts
    handler = make_suggestion_handler(screen)
    await handler.show_prompt_history_async()
    index = built[0]
    await wait_for(index.first_started.is_set, description="first history rank started")
    return screen, handler, index


async def test_prompt_history_search_ranks_off_the_event_loop_and_coalesces_keystrokes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    screen, handler, index = await _open_history_with_held_rank(monkeypatch, ["fix bug", "write docs", "review docs"])
    worker = handler._prompt_history_query_worker
    assert worker is not None
    assert not worker.is_finished

    # The first rank is still running in its thread: these keystrokes only queue.
    handler.on_text_changed("d")
    handler.on_text_changed("do")
    handler.on_text_changed("docs")
    assert index.queries == [""]
    index.release_first.set()
    await wait_for_prompt_history_query(handler)

    assert index.queries == ["", "docs"]
    assert threading.get_ident() not in index.threads
    assert suggestion_values(screen.suggestion_list.last_items) == ["review docs", "write docs"]


async def test_enter_during_a_history_search_selects_from_the_newest_results(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    screen, handler, index = await _open_history_with_held_rank(monkeypatch, ["fix bug", "write docs"])
    handler.on_text_changed("docs")
    assert handler.on_suggestion_select(execute=True) is True
    # The rows on screen belong to the query still ranking; nothing is chosen from them.
    assert screen.suggestion_list.selections == []

    index.release_first.set()
    await wait_for_prompt_history_query(handler)

    assert suggestion_values(screen.suggestion_list.last_items) == ["write docs"]
    assert screen.suggestion_list.selections == [True]


async def test_arrows_during_a_history_search_move_through_the_newest_results(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    screen, handler, index = await _open_history_with_held_rank(
        monkeypatch, ["fix bug", "write docs", "review docs", "read docs"]
    )
    view_moves: list[str] = []

    def record_move(direction: str) -> None:
        view_moves.append(direction)

    monkeypatch.setattr(handler._view, "move_suggestion_cursor", record_move)

    handler.on_text_changed("docs")
    handler.on_suggestion_navigate("down")
    handler.on_suggestion_navigate("down")
    assert handler.on_suggestion_select(execute=False) is True
    assert view_moves == []
    assert screen.suggestion_list.selections == []

    index.release_first.set()
    await wait_for_prompt_history_query(handler)

    assert suggestion_values(screen.suggestion_list.last_items) == ["read docs", "review docs", "write docs"]
    assert view_moves == ["down", "down"]
    assert screen.suggestion_list.selections == [False]


async def test_a_newer_history_query_stops_the_search_in_flight(monkeypatch: pytest.MonkeyPatch) -> None:
    _screen, handler, index = await _open_history_with_held_rank(monkeypatch, ["write docs"])
    (stop,) = index.stops
    assert stop is not None
    assert stop() is False

    handler.on_text_changed("docs")

    assert stop() is True
    index.release_first.set()
    await wait_for_prompt_history_query(handler)


async def test_reopening_during_a_history_search_stops_it_and_keeps_one_search_running(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    screen, handler, index = await _open_history_with_held_rank(monkeypatch, ["alpha", "beta"])
    worker = handler._prompt_history_query_worker
    (stop,) = index.stops
    assert stop is not None
    handler.dismiss_suggestions()
    assert stop() is True

    handler.start_prompt_history()
    # The stopped search's loop goes on to the reopened query; no second search starts beside it.
    assert handler._prompt_history_query_worker is worker
    assert index.queries == [""]
    index.release_first.set()
    await wait_for_prompt_history_query(handler)

    assert index.queries == ["", ""]
    assert suggestion_values(screen.suggestion_list.last_items) == ["beta", "alpha"]


async def test_history_rows_built_after_the_screen_detached_are_dropped(monkeypatch: pytest.MonkeyPatch) -> None:
    built_rows = threading.Event()
    release_rows = threading.Event()
    build_rows = SuggestionHandler._prompt_history_items.__func__

    def held_rows(cls: type[SuggestionHandler], matches: list[PromptMatch]) -> list[SuggestionItem]:
        built_rows.set()
        release_rows.wait(timeout=ENGINE_TURN_TIMEOUT)
        return build_rows(cls, matches)

    screen, handler, index = await _open_history_with_held_rank(monkeypatch, ["write docs"])
    index.release_first.set()
    await wait_for_prompt_history_query(handler)
    monkeypatch.setattr(SuggestionHandler, "_prompt_history_items", classmethod(held_rows))
    handler.on_text_changed("docs")
    assert handler.on_suggestion_select(execute=True) is True
    await wait_for(built_rows.is_set, description="rows being built in their thread")

    screen.is_attached = False
    release_rows.set()
    await wait_for_prompt_history_query(handler)

    # Neither the rows nor the held Enter reach a screen that is gone.
    assert suggestion_values(screen.suggestion_list.last_items) == ["write docs"]
    assert screen.suggestion_list.selections == []


async def test_dismiss_during_prompt_history_search_drops_its_result(monkeypatch: pytest.MonkeyPatch) -> None:
    screen, handler, index = await _open_history_with_held_rank(monkeypatch, ["alpha"])
    handler._show_suggestions("commands", [SuggestionItem(value="new", label="new")])
    index.release_first.set()
    await wait_for_prompt_history_query(handler)

    assert screen.suggestion_list.last_mode == "commands"
    assert suggestion_values(screen.suggestion_list.last_items) == ["new"]
    assert index.queries == [""]


async def test_update_suggestions_rerenders_border_title_in_active_locale() -> None:
    """Per-keystroke rebuilds re-supply the title so a popup opened before a
    locale switch does not keep its stale-language border."""
    screen = make_suggestion_screen()
    controller = LocaleController(Settings(locale="zh-Hans"))
    handler = make_suggestion_handler(screen, locale_controller=controller)
    handler.build_slash_commands()
    handler._suggestion_mode = "commands"

    handler.on_text_changed("/r")

    assert screen.suggestion_list.last_title == "命令"
    # Filtering before any shortcut read started one: the test owns it.
    for worker in screen.workers:
        await asyncio.wait_for(worker.wait(), ENGINE_TURN_TIMEOUT)


def test_model_trigger_filters_selectable_profiles_and_switches_by_profile_id() -> None:
    screen = make_suggestion_screen()
    registry = ModelProfileRegistry()
    registry.register(ModelProfile(id="current-id", name="Current", model_id="vendor/current"))
    registry.register(ModelProfile(id="fast-id", name="Fast Model", model_id="vendor/fast"))
    registry.register(ModelProfile(id="incomplete-id", name="Incomplete"))
    screen.services.model_registry = registry
    screen.services.active_model_profile_id = "current-id"
    handler = make_suggestion_handler(screen)

    handler.on_model_triggered()

    assert screen.suggestion_list.last_mode == "models"
    assert suggestion_values(screen.suggestion_list.last_items) == ["current-id", "fast-id"]
    items, disabled = handler._get_model_items()
    assert disabled == {"current-id"}
    assert items[0].label.plain == "◦ Current  vendor/current"

    handler.on_text_changed("$fast")
    assert suggestion_values(screen.suggestion_list.last_items) == ["fast-id"]

    handler.on_suggestion_selected("models", "fast-id", execute=True)
    assert screen.picked_models == ["fast-id"]
    assert screen.input_bar.value == ""
    assert screen.suggestion_list.is_visible is False


def test_pickers_stay_shut_while_the_agent_loads() -> None:
    """A switch that lands mid-load is dropped after the draft is already gone.

    The status-bar selectors block loading as well as running; the inline
    triggers have to agree, or the popup opens onto a choice that cannot be
    committed.
    """
    screen = make_suggestion_screen()
    registry = ModelProfileRegistry()
    registry.register(ModelProfile(id="model-id", name="Model", model_id="vendor/model"))
    screen.services.model_registry = registry
    screen.state.run.agent_loading = True
    handler = make_suggestion_handler(screen)

    handler.on_model_triggered()
    assert screen.suggestion_list.is_visible is False

    handler.on_agent_triggered()
    assert screen.suggestion_list.is_visible is False


@pytest.mark.parametrize("selection_source", ["agent", "override", "inherited"])
def test_model_trigger_does_not_bypass_locked_runtime_selection(selection_source: str) -> None:
    screen = make_suggestion_screen()
    registry = ModelProfileRegistry()
    registry.register(ModelProfile(id="model-id", name="Model", model_id="vendor/model"))
    screen.services.model_registry = registry
    screen.state.runtime.details_confirmed = True
    screen.state.runtime.details.model.profile_id = "model-id"
    screen.state.runtime.details.model.selection_source = selection_source  # type: ignore[assignment]
    handler = make_suggestion_handler(screen)

    handler.on_model_triggered()

    assert screen.suggestion_list.is_visible is False


@pytest.mark.parametrize(
    ("confirmed", "running", "current"),
    [(False, "", "current-id"), (True, "fast-id", "fast-id"), (True, "", "current-id")],
    ids=["before-runtime", "runtime-named", "runtime-named-none"],
)
def test_model_list_marks_the_model_the_session_runs(confirmed: bool, running: str, current: str) -> None:
    """The ``$`` list marks what runs (``running_model``); a runtime answer naming no model keeps the launch setting."""
    screen = make_suggestion_screen()
    _register_model_profiles(screen)
    screen.state.runtime.details_confirmed = confirmed
    screen.state.runtime.details.model.profile_id = running
    handler = make_suggestion_handler(screen)

    _items, disabled = handler._get_model_items()

    assert disabled == {current}


def _register_agent_profiles(screen: SuggestionScreen) -> None:
    screen.services.agent_registry = AgentRegistryStub(
        [
            AgentProfileStub(name="Code", description="Code agent"),
            AgentProfileStub(name="QA", description="QA agent"),
        ]
    )


def _register_model_profiles(screen: SuggestionScreen) -> None:
    registry = ModelProfileRegistry()
    registry.register(ModelProfile(id="current-id", name="Current", model_id="vendor/current"))
    registry.register(ModelProfile(id="fast-id", name="Fast Model", model_id="vendor/fast"))
    screen.services.model_registry = registry
    screen.services.active_model_profile_id = "current-id"


@pytest.mark.parametrize(
    ("mode", "register_profiles", "typed", "submits"),
    [
        pytest.param("agents", _register_agent_profiles, "#123", True, id="agent-trigger-unmatched-text"),
        pytest.param("agents", _register_agent_profiles, "#Code", False, id="agent-trigger-active-profile"),
        pytest.param("models", _register_model_profiles, "$not-a-model", True, id="model-trigger-unmatched-text"),
        pytest.param("models", _register_model_profiles, "$Current", False, id="model-trigger-active-profile"),
    ],
)
def test_trigger_enter_submits_unmatched_text_but_keeps_a_matched_draft(
    mode: str,
    register_profiles: Callable[[SuggestionScreen], None],
    typed: str,
    submits: bool,
) -> None:
    """Enter with nothing highlighted submits the draft, through the input bar's
    own submit path, only when the typed trigger matches no profile; text that
    names the active profile stays put."""
    screen = make_suggestion_screen()
    register_profiles(screen)
    screen.input_bar.value = typed
    screen.suggestion_list.is_visible = True
    handler = make_suggestion_handler(screen)
    handler._suggestion_mode = mode

    assert handler.on_suggestion_select(execute=True) is True

    if submits:
        assert screen.input_bar.submitted_drafts == [typed]
        assert screen.suggestion_list.is_visible is False
    else:
        assert screen.input_bar.value == typed
        assert screen.input_bar.submitted_drafts == []
        assert screen.suggestion_list.is_visible is True
    # The handler never sends the text itself: the input bar's router decides where it goes.
    assert screen.submitted == []


def test_command_suggestion_enter_without_selection_keeps_submit_fallback() -> None:
    screen = make_suggestion_screen()
    screen.input_bar.value = "/mcp"
    handler = make_suggestion_handler(screen)
    handler._suggestion_mode = "commands"
    handler.build_slash_commands()

    assert handler.on_suggestion_select(execute=True) is True

    # As Enter with the list closed: the input bar's own submit path takes the draft.
    assert screen.input_bar.submitted_drafts == ["/mcp"]
    assert screen.submitted == []
