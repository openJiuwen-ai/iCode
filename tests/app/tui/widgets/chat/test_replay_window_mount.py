# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""On-demand transcript mounting preserves logical order and coalesces requests."""

from __future__ import annotations

import asyncio

import pytest
from textual.geometry import Size
from textual.widget import Widget

from chrys.app.tui.widgets.chat.history_viewport import HistoryViewport
from chrys.app.tui.widgets.chat.messages import AgentMessage, ThinkingMessage, UserMessage
from chrys.app.tui.widgets.chat.panel import ChatPanel
from chrys.app.tui.widgets.chat.renderers.ask_user import AskUserToolCall
from chrys.app.tui.widgets.chat.renderers.file_edit import EditFileToolCall
from chrys.app.tui.widgets.chat.replay_mount import REPLAY_FOREGROUND_ENTRY_LIMIT, REPLAY_MOUNT_BATCH_SIZE
from chrys.app.tui.widgets.chat.tool_call import ToolGroup
from chrys.app.tui.widgets.diff_view.inline import InlineUnifiedDiffLines
from chrys.app.tui.widgets.markdown import VirtualizedMarkdown
from chrys.foundation.tool_kinds import KIND_ASK_USER, KIND_FILESYSTEM_WRITE
from tests.support.tui_helpers import ChatPanelApp, chat_content_children
from tests.support.waiting import wait_for


def _history() -> tuple[list[Widget], list[tuple[str, str]]]:
    roots: list[Widget] = []
    messages: list[tuple[str, str]] = []
    for index in range(6):
        prompt, answer = f"prompt {index}", f"answer {index}"
        roots.extend((UserMessage(prompt), AgentMessage(answer)))
        messages.extend((("You", prompt), ("Agent", answer)))
    return roots, messages


def _turns(count: int, prefix: str = "turn") -> list[Widget]:
    return [
        widget
        for index in range(count)
        for widget in (UserMessage(f"{prefix} question {index}"), AgentMessage(f"{prefix} answer {index}"))
    ]


def test_history_prefix_and_minimum_tail_share_the_foreground_limit() -> None:
    history = _turns(80)
    # For 80 turns the final page plus the newest turn has four pairs of roots.
    prefix = [Widget() for _ in range(REPLAY_FOREGROUND_ENTRY_LIMIT - 8)]
    entries = [*prefix, *history]
    viewport = HistoryViewport(entries)
    initial = viewport.prime(Size(100, 500))
    assert initial is not None
    prefix_count, tail_start, _ = initial
    assert prefix_count == len(prefix)
    assert prefix_count + len(entries[tail_start:]) == REPLAY_FOREGROUND_ENTRY_LIMIT
    assert HistoryViewport([Widget(), *entries]).prime(Size(100, 500)) is None


@pytest.mark.parametrize("with_prefix", [False, True], ids=["first-user", "leading-entry"])
async def test_windowed_replay_only_mounts_requested_roots_and_copy_keeps_document_order(with_prefix: bool) -> None:
    async with ChatPanelApp().run_test(size=(80, 24)) as pilot:
        panel = pilot.app.query_one(ChatPanel)
        await panel.dismiss_welcome_for_content()
        roots, messages = _history()
        prefix = [Widget()] if with_prefix else []
        replay = panel._replay_mount
        await replay.mount([*prefix, *roots], windowed_tail_start=len(prefix) + 10, windowed_prefix_count=len(prefix))
        assert replay.windowed
        assert replay.has_pending
        assert not replay.in_progress
        assert chat_content_children(panel) == [*prefix, *roots[10:]]
        assert panel.get_all_messages() == messages

        ready = asyncio.Event()
        replay.request_window([*roots[6:8], *roots[2:4]], ready.set)
        await wait_for(ready.is_set, pilot=pilot, description="non-contiguous history roots mounted")
        assert chat_content_children(panel) == [*prefix, *roots[2:4], *roots[6:8], *roots[10:]]
        assert replay.pending_widgets() == [*roots[:2], *roots[4:6], *roots[8:10]]
        assert panel.get_all_messages() == messages

        await replay.wait_complete()
        assert chat_content_children(panel) == [*prefix, *roots]
        assert not replay.has_pending
        assert panel.get_all_messages() == messages


@pytest.mark.parametrize("expanded", [True, False], ids=["expanded", "folded"])
async def test_windowed_replay_waits_only_for_visible_thinking_to_parse(
    monkeypatch: pytest.MonkeyPatch, expanded: bool
) -> None:
    text = "Cold thinking paragraph.\n\nMore cold thinking."
    entered, release, ready = asyncio.Event(), asyncio.Event(), asyncio.Event()
    parse = VirtualizedMarkdown._run_parse

    async def hold_thinking_parse(body, request):
        if body.source == text:
            entered.set()
            await release.wait()
        await parse(body, request)

    monkeypatch.setattr(VirtualizedMarkdown, "_run_parse", hold_thinking_parse)
    async with ChatPanelApp().run_test(size=(80, 24)) as pilot:
        panel = pilot.app.query_one(ChatPanel)
        await panel.dismiss_welcome_for_content()
        thinking = ThinkingMessage(text, collapsed=True)
        replay = panel._replay_mount
        await replay.mount([thinking, *_turns(1)], windowed_tail_start=1)
        replay.set_fold_state(collapsed=not expanded)
        replay.request_window([thinking], ready.set)
        try:
            if expanded:
                await wait_for(entered.is_set, pilot=pilot, description="cold thinking parse started")
                await pilot.pause()
                assert replay.needs_mount([thinking])
                assert not ready.is_set()
            else:
                await wait_for(ready.is_set, pilot=pilot, description="folded thinking window is ready")
                assert not entered.is_set()
        finally:
            release.set()

        await wait_for(ready.is_set, pilot=pilot, description="thinking window is ready")
        if expanded:
            body = thinking.query_one(VirtualizedMarkdown)
            assert body.display
            assert body.shown_source == text
        else:
            assert not thinking.query(VirtualizedMarkdown)


@pytest.mark.parametrize("expanded", [True, False], ids=["expanded", "folded"])
async def test_windowed_replay_waits_only_for_visible_deferred_tool_content(
    monkeypatch: pytest.MonkeyPatch, expanded: bool
) -> None:
    entered, release, ready = asyncio.Event(), asyncio.Event(), asyncio.Event()
    mount_content = EditFileToolCall.mount_pending_content

    async def hold_diff_mount(tool: EditFileToolCall) -> bool:
        entered.set()
        await release.wait()
        return await mount_content(tool)

    monkeypatch.setattr(EditFileToolCall, "mount_pending_content", hold_diff_mount)
    async with ChatPanelApp().run_test(size=(80, 24)) as pilot:
        panel = pilot.app.query_one(ChatPanel)
        await panel.dismiss_welcome_for_content()
        group = ToolGroup(live=False)
        await group.add_collapsed_replay_tool(
            "edit",
            "edit_file",
            KIND_FILESYSTEM_WRITE,
            args={"path": "src/app.py"},
            result="Edited src/app.py",
            file_snapshot=("old\n", "new\n"),
            lazy=True,
        )
        replay = panel._replay_mount
        await replay.mount([group, *_turns(1)], windowed_tail_start=1)
        replay.set_fold_state(collapsed=not expanded)
        replay.request_window([group], ready.set)
        try:
            if expanded:
                await wait_for(entered.is_set, pilot=pilot, description="cold tool diff mount started")
                await pilot.pause()
                assert replay.needs_mount([group])
                assert not ready.is_set()
            else:
                await wait_for(ready.is_set, pilot=pilot, description="folded tool window is ready")
                assert not entered.is_set()
        finally:
            release.set()

        await wait_for(ready.is_set, pilot=pilot, description="tool window is ready")
        if expanded:
            diff = group.query_one(InlineUnifiedDiffLines)
            assert diff.is_mounted
        else:
            assert group.get_tool("edit") is None


@pytest.mark.parametrize("updated", [False, True], ids=["unchanged", "changed"])
async def test_cold_ask_user_only_parses_new_question_text_before_the_window_is_ready(
    monkeypatch: pytest.MonkeyPatch, updated: bool
) -> None:
    initial_question = "Which option?"
    updated_question = "Which of these **new** options?"
    entered, release, ready = asyncio.Event(), asyncio.Event(), asyncio.Event()
    sources: list[str] = []
    mount = AskUserToolCall.on_mount
    complete = AskUserToolCall.set_complete
    update = VirtualizedMarkdown.update
    parse = VirtualizedMarkdown._run_parse

    async def mount_question_before_completion(tool: AskUserToolCall) -> None:
        mount(tool)
        await tool.query_one("#ask-question", VirtualizedMarkdown)._mounted_event.wait()

    def apply_recorded_answer(tool: AskUserToolCall, result: str, duration_ms: int = 0, **kwargs) -> None:
        if updated:
            tool.args = {"question": updated_question}
        complete(tool, result, duration_ms, **kwargs)

    def record_question_update(body: VirtualizedMarkdown, source: str):
        if body.id == "ask-question":
            sources.append(source)
        return update(body, source)

    async def hold_updated_question(body, request):
        if body.id == "ask-question" and body.source == updated_question:
            entered.set()
            await release.wait()
        await parse(body, request)

    monkeypatch.setattr(AskUserToolCall, "on_mount", mount_question_before_completion)
    monkeypatch.setattr(AskUserToolCall, "set_complete", apply_recorded_answer)
    monkeypatch.setattr(VirtualizedMarkdown, "update", record_question_update)
    monkeypatch.setattr(VirtualizedMarkdown, "_run_parse", hold_updated_question)
    async with ChatPanelApp().run_test(size=(80, 24)) as pilot:
        panel = pilot.app.query_one(ChatPanel)
        await panel.dismiss_welcome_for_content()
        group = ToolGroup(live=False)
        await group.add_collapsed_replay_tool(
            "ask",
            "ask_user",
            KIND_ASK_USER,
            args={"question": initial_question},
            result="User response: The first option",
            lazy=True,
        )
        replay = panel._replay_mount
        await replay.mount([group, *_turns(1)], windowed_tail_start=1)
        replay.set_fold_state(collapsed=False)
        replay.request_window([group], ready.set)
        try:
            if updated:
                await wait_for(entered.is_set, pilot=pilot, description="updated question is parsing")
                await pilot.pause()
                assert replay.needs_mount([group])
                assert not ready.is_set()
        finally:
            release.set()

        await wait_for(ready.is_set, pilot=pilot, description="the ask_user card is ready")
        question = group.query_one("#ask-question", VirtualizedMarkdown)
        assert question.shown_source == (updated_question if updated else initial_question)
        assert sources == ([initial_question, updated_question] if updated else [initial_question])


async def test_fold_all_rechecks_reopened_thinking_before_publishing_a_cold_batch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first_text = "First saved thought."
    later_text = "Later saved thought."
    later_started, release_later = asyncio.Event(), asyncio.Event()
    later_checked = asyncio.Event()
    reopened_started, release_reopened = asyncio.Event(), asyncio.Event()
    ready = asyncio.Event()
    reopening = False
    parse = VirtualizedMarkdown._run_parse

    async def hold_parses(body, request):
        if body.source == later_text and not later_started.is_set():
            later_started.set()
            await release_later.wait()
        elif body.source == first_text and reopening:
            reopened_started.set()
            await release_reopened.wait()
        await parse(body, request)

    monkeypatch.setattr(VirtualizedMarkdown, "_run_parse", hold_parses)
    async with ChatPanelApp().run_test(size=(80, 24)) as pilot:
        panel = pilot.app.query_one(ChatPanel)
        await panel.dismiss_welcome_for_content()
        first = ThinkingMessage(first_text, collapsed=True)
        later = ThinkingMessage(later_text, collapsed=True)
        wait_for_later = later.wait_for_replay_body

        async def check_later_body() -> None:
            await wait_for_later()
            later_checked.set()

        monkeypatch.setattr(later, "wait_for_replay_body", check_later_body)
        replay = panel._replay_mount
        await replay.mount([first, later, *_turns(1)], windowed_tail_start=2)
        replay.set_fold_state(collapsed=False)
        replay.request_window([first, later], ready.set)
        try:
            await wait_for(
                lambda: later_started.is_set() and not first.ends_folded(),
                pilot=pilot,
                description="first thought opened while the later parse is held",
            )
            assert panel.toggle_fold_all()
            await wait_for(
                lambda: not first.query(VirtualizedMarkdown) and not later.query(VirtualizedMarkdown),
                pilot=pilot,
                description="both folded bodies were removed",
            )
            reopening = True
            assert not panel.toggle_fold_all()
            await wait_for(reopened_started.is_set, pilot=pilot, description="first thought reopened")
            release_later.set()
            await wait_for(later_checked.is_set, pilot=pilot, description="replay finished its first readiness pass")
            assert replay.needs_mount([first, later])
            assert not ready.is_set()
        finally:
            release_later.set()
            release_reopened.set()

        await wait_for(ready.is_set, pilot=pilot, description="both reopened thoughts are ready")
        for message in (first, later):
            body = message.query_one(VirtualizedMarkdown)
            assert body.display
            assert body.shown_source == message.text


async def test_windowed_replay_latest_request_replaces_queued_cold_roots(monkeypatch: pytest.MonkeyPatch) -> None:
    async with ChatPanelApp().run_test(size=(80, 24)) as pilot:
        panel = pilot.app.query_one(ChatPanel)
        await panel.dismiss_welcome_for_content()
        roots, messages = _history()
        replay = panel._replay_mount
        await replay.mount(roots, windowed_tail_start=10)

        real_mount = panel.mount_replay_batch
        entered = asyncio.Event()
        release = asyncio.Event()
        superseded = asyncio.Event()
        ready = asyncio.Event()

        async def gated(batch: list[Widget], *, before: Widget | None) -> None:
            await real_mount(batch, before=before)
            if batch == roots[6:8]:
                entered.set()
                await release.wait()

        monkeypatch.setattr(panel, "mount_replay_batch", gated)
        replay.request_window([*roots[:2], *roots[6:8]], superseded.set)
        try:
            await wait_for(entered.is_set, pilot=pilot, description="the original batch is still initializing")
            assert replay.needs_mount(roots[6:8])
            assert panel.get_all_messages() == messages
            replay.request_window([*roots[2:4], *roots[6:10]], ready.set)
        finally:
            release.set()

        await wait_for(ready.is_set, pilot=pilot, description="the latest history window mounted")
        assert not superseded.is_set()
        assert chat_content_children(panel) == [*roots[2:4], *roots[6:]]
        assert panel.get_all_messages() == messages
        await replay.stop()
        assert not replay.windowed
        assert not replay.has_pending
        assert replay.pending_widgets() == []


async def test_windowed_replay_accepts_new_work_after_a_warm_request_in_eager_mode() -> None:
    async with ChatPanelApp().run_test(size=(80, 24)) as pilot:
        panel = pilot.app.query_one(ChatPanel)
        await panel.dismiss_welcome_for_content()
        roots, _messages = _history()
        replay = panel._replay_mount
        await replay.mount(roots, windowed_tail_start=10)

        loop = asyncio.get_running_loop()
        factory = loop.get_task_factory()
        loop.set_task_factory(asyncio.eager_task_factory)
        try:
            warm, cold = asyncio.Event(), asyncio.Event()
            replay.request_window(roots[10:], warm.set)
            await wait_for(warm.is_set, pilot=pilot, description="already mounted window is ready")
            assert not replay.in_progress

            replay.request_window(roots[:2], cold.set)
            await wait_for(cold.is_set, pilot=pilot, description="the following cold window still mounts")
            assert chat_content_children(panel) == [*roots[:2], *roots[10:]]
        finally:
            loop.set_task_factory(factory)


async def test_windowed_replay_can_retry_a_failed_mount(monkeypatch: pytest.MonkeyPatch) -> None:
    async with ChatPanelApp().run_test(size=(80, 24)) as pilot:
        panel = pilot.app.query_one(ChatPanel)
        await panel.dismiss_welcome_for_content()
        roots, messages = _history()
        replay = panel._replay_mount
        await replay.mount(roots, windowed_tail_start=10)
        original_mount = panel.mount_replay_batch
        ready, failed = asyncio.Event(), asyncio.Event()

        async def fail_mount(batch: list[Widget], *, before: Widget | None) -> None:
            raise RuntimeError("test interrupted archive mount")

        monkeypatch.setattr(panel, "mount_replay_batch", fail_mount)
        replay.request_window(roots[:2], ready.set, failed=failed.set)
        await wait_for(failed.is_set, pilot=pilot, description="mount failure delivered to requester")
        assert not ready.is_set()
        assert panel.get_all_messages() == messages

        monkeypatch.setattr(panel, "mount_replay_batch", original_mount)
        replay.request_window(roots[:2], ready.set)
        await wait_for(ready.is_set, pilot=pilot, description="retry mounted the original cold roots")
        assert chat_content_children(panel) == [*roots[:2], *roots[10:]]
        assert panel.get_all_messages() == messages


@pytest.mark.parametrize(
    ("overlap", "stop"), [(False, False), (True, False), (True, True)], ids=["disjoint", "overlap", "stop"]
)
@pytest.mark.parametrize("after_attach", [False, True], ids=["unmounted", "attached"])
async def test_failed_batch_only_fails_requests_using_its_roots(
    monkeypatch: pytest.MonkeyPatch, overlap: bool, stop: bool, after_attach: bool
) -> None:
    async with ChatPanelApp().run_test(size=(80, 24)) as pilot:
        panel = pilot.app.query_one(ChatPanel)
        await panel.dismiss_welcome_for_content()
        roots, messages = _history()
        replay = panel._replay_mount
        await replay.mount(roots, windowed_tail_start=10)
        real_mount = panel.mount_replay_batch
        entered, release = asyncio.Event(), asyncio.Event()
        old_ready, old_failed, ready, failed = (asyncio.Event() for _ in range(4))

        async def fail_first_batch(batch: list[Widget], *, before: Widget | None) -> None:
            if not entered.is_set():
                if after_attach:
                    await real_mount(batch, before=before)
                entered.set()
                await release.wait()
                raise RuntimeError("test interrupted the first requested batch")
            await real_mount(batch, before=before)

        def on_failure() -> None:
            failed.set()
            if stop:
                replay.request_stop()

        monkeypatch.setattr(panel, "mount_replay_batch", fail_first_batch)
        replay.request_window(roots[:2], old_ready.set, failed=old_failed.set)
        try:
            await wait_for(entered.is_set, pilot=pilot, description="the first window started mounting")
            latest = roots[1:3] if overlap else roots[4:6]
            replay.request_window(latest, ready.set, failed=on_failure)
            waits = (
                asyncio.create_task(replay.wait_complete()),
                asyncio.create_task(replay.wait_complete(widgets=roots[:2])),
                asyncio.create_task(replay.wait_complete(widgets=roots[8:10])),
            )
            await wait_for(lambda: len(replay._drain_waiters) == 3, pilot=pilot)
        finally:
            release.set()
        await wait_for(lambda: not replay.in_progress, pilot=pilot)
        full, affected, unrelated = await asyncio.gather(*waits, return_exceptions=True)
        assert isinstance(full, RuntimeError) and isinstance(affected, RuntimeError)
        assert unrelated is None
        assert not old_ready.is_set() and not old_failed.is_set()
        assert failed.is_set() == overlap
        assert ready.is_set() != overlap
        assert all(widget.is_attached for widget in roots[8:10]) != stop
        assert all(widget.is_attached for widget in latest) != overlap
        assert panel.get_all_messages() == messages


async def test_clear_stops_windowed_tail_mount_at_the_current_batch(monkeypatch: pytest.MonkeyPatch) -> None:
    async with ChatPanelApp().run_test(size=(80, 60)) as pilot:
        panel = pilot.app.query_one(ChatPanel)
        await panel.dismiss_welcome_for_content()
        roots = _turns(200)
        real_mount = panel.mount_replay_batch
        entered, release = asyncio.Event(), asyncio.Event()
        mounted: list[Widget] = []

        async def gate_first_batch(batch: list[Widget], *, before: Widget | None) -> None:
            await real_mount(batch, before=before)
            mounted.extend(batch)
            if not entered.is_set():
                entered.set()
                await release.wait()

        monkeypatch.setattr(panel, "mount_replay_batch", gate_first_batch)
        restore = asyncio.create_task(panel.mount_transcript_widgets(roots))
        await wait_for(entered.is_set, pilot=pilot, description="first restored tail batch mounted")
        clear = asyncio.create_task(panel.clear())
        try:
            await wait_for(
                lambda: panel._replay_mount._stop_requested,
                pilot=pilot,
                description="clear requests replay stop",
            )
            assert not clear.done()
            assert all(widget.parent is panel for widget in mounted)
        finally:
            release.set()
            await asyncio.gather(restore, clear)

        assert len(mounted) == REPLAY_MOUNT_BATCH_SIZE
        assert not panel.mounted_user_messages()
        assert not panel.mounted_agent_messages()
        assert panel._history_viewport is None
        assert panel._history_window is None
        assert not panel._replay_mount.has_pending

        replacement = _turns(1, "new")
        await panel.dismiss_welcome_for_content()
        await panel.mount_transcript_widgets(replacement)
        assert chat_content_children(panel) == replacement


async def test_append_during_eager_replay_does_not_install_a_cold_viewport(monkeypatch: pytest.MonkeyPatch) -> None:
    async with ChatPanelApp().run_test(size=(80, 24)) as pilot:
        panel = pilot.app.query_one(ChatPanel)
        await panel.dismiss_welcome_for_content()
        first = _turns(36)
        appended = _turns(80, "appended")
        real_mount = panel.mount_replay_batch
        entered, release = asyncio.Event(), asyncio.Event()

        async def gate_prepend(batch: list[Widget], *, before: Widget | None) -> None:
            await real_mount(batch, before=before)
            if before is not None and not entered.is_set():
                entered.set()
                await release.wait()

        monkeypatch.setattr(panel, "mount_replay_batch", gate_prepend)
        await panel.mount_transcript_widgets(first)
        try:
            await wait_for(entered.is_set, pilot=pilot, description="older replay is still prepending")
            await panel.mount_transcript_widgets(appended)
            assert all(widget.parent is panel for widget in appended)
            assert not panel._replay_mount.windowed
            assert panel._history_window is None
            assert panel._history_viewport is not None and not panel._history_viewport.measured
        finally:
            release.set()
            await panel.wait_replay_complete()

        assert chat_content_children(panel) == [*first, *appended]


async def test_append_waits_for_the_windowed_tail_before_mounting(monkeypatch: pytest.MonkeyPatch) -> None:
    async with ChatPanelApp().run_test(size=(80, 60)) as pilot:
        panel = pilot.app.query_one(ChatPanel)
        await panel.dismiss_welcome_for_content()
        first = _turns(200)
        appended = _turns(3, "appended")
        real_mount = panel.mount_replay_batch
        entered, release = asyncio.Event(), asyncio.Event()
        first_batch: list[Widget] = []

        async def gate_initial_batch(batch: list[Widget], *, before: Widget | None) -> None:
            await real_mount(batch, before=before)
            if not entered.is_set():
                first_batch.extend(batch)
                entered.set()
                await release.wait()

        monkeypatch.setattr(panel, "mount_replay_batch", gate_initial_batch)
        restore = asyncio.create_task(panel.mount_transcript_widgets(first))
        await wait_for(entered.is_set, pilot=pilot, description="the first tail batch mounted")
        append = asyncio.create_task(panel.mount_transcript_widgets(appended))
        try:
            await pilot.pause()
            assert chat_content_children(panel) == first_batch
            assert not append.done()
        finally:
            release.set()
            await asyncio.gather(restore, append)

        assert chat_content_children(panel) == [*first, *appended]
        assert not panel._replay_mount.has_pending


async def test_eager_replay_measures_the_archive_after_its_initial_mount(monkeypatch: pytest.MonkeyPatch) -> None:
    async with ChatPanelApp().run_test(size=(80, 24)) as pilot:
        panel = pilot.app.query_one(ChatPanel)
        await panel.dismiss_welcome_for_content()
        roots = _turns(60)
        real_mount = panel.mount_replay_batch

        async def measure_tail_before_prepend(batch: list[Widget], *, before: Widget | None) -> None:
            await real_mount(batch, before=before)
            if before is None:
                panel.arrange(panel.content_size)

        monkeypatch.setattr(panel, "mount_replay_batch", measure_tail_before_prepend)
        await panel.mount_transcript_widgets(roots)
        assert not panel._replay_mount.windowed
        await panel.wait_replay_complete()
        await wait_for(
            lambda: panel._history_viewport is not None and panel._history_viewport.measured,
            pilot=pilot,
            description="the complete eager replay populated the viewport index",
        )
        assert chat_content_children(panel) == roots


async def test_unmeasurable_first_viewport_keeps_cold_entries_accessible() -> None:
    async with ChatPanelApp().run_test(size=(80, 24)) as pilot:
        panel = pilot.app.query_one(ChatPanel)
        await panel.dismiss_welcome_for_content()
        roots = _turns(120)
        with pilot.app.batch_update():
            await panel.mount_transcript_widgets(roots)
            viewport = panel._history_viewport
            assert viewport is not None and viewport.last_layout is None
            assert not roots[0].is_attached
            roots[-2].display = False

        await wait_for(
            lambda: roots[0].is_attached and not panel._replay_mount.has_pending,
            pilot=pilot,
            description="ordinary layout made the remaining archive accessible",
        )
        assert chat_content_children(panel) == roots


@pytest.mark.parametrize("removed_index", [-8, -3], ids=["page-opener", "answer"])
async def test_removing_a_mounted_archive_root_drains_cold_history_without_painting_it(
    monkeypatch: pytest.MonkeyPatch, removed_index: int
) -> None:
    async with ChatPanelApp().run_test(size=(80, 24)) as pilot:
        panel = pilot.app.query_one(ChatPanel)
        await panel.dismiss_welcome_for_content()
        roots = _turns(120)
        oldest = roots[0]
        oldest.id = "oldest-turn"
        await panel.mount_transcript_widgets(roots)
        viewport = panel._history_viewport
        assert viewport is not None
        await wait_for(lambda: viewport.last_layout is not None, pilot=pilot, description="the tail was laid out")
        removed = roots[removed_index]
        assert removed.is_attached and not roots[0].is_attached
        assert viewport.last_layout is not None and removed in viewport.last_layout.widgets
        previous = viewport.last_layout
        real_mount = panel.mount_replay_batch
        entered, release = asyncio.Event(), asyncio.Event()

        async def gate_cold_batch(batch: list[Widget], *, before: Widget | None) -> None:
            entered.set()
            await release.wait()
            await real_mount(batch, before=before)

        monkeypatch.setattr(panel, "mount_replay_batch", gate_cold_batch)
        try:
            await removed.remove()
            assert not removed.is_attached
            await wait_for(entered.is_set, pilot=pilot, description="the invalid archive is being drained")
            layout = panel.arrange(panel.content_size)
            assert removed not in layout.widgets
            assert all(placement.widget is not removed for placement in layout.placements)
            assert removed not in pilot.app.screen._compositor.full_map
            assert layout.total_region == previous.total_region
            assert layout.placements == [
                placement for placement in previous.placements if placement.widget is not removed
            ]
            panel.scroll_to_turn("oldest-turn")
        finally:
            release.set()
        await wait_for(
            lambda: panel._history_viewport is None and oldest.region.overlaps(panel.content_region),
            pilot=pilot,
            description="native layout landed the jump requested during the drain",
        )
        assert chat_content_children(panel) == [widget for widget in roots if widget is not removed]


async def test_failed_final_cold_batch_does_not_poison_a_later_wait(monkeypatch: pytest.MonkeyPatch) -> None:
    async with ChatPanelApp().run_test(size=(80, 24)) as pilot:
        panel = pilot.app.query_one(ChatPanel)
        await panel.dismiss_welcome_for_content()
        roots, messages = _history()
        replay = panel._replay_mount
        await replay.mount(roots, windowed_tail_start=10)
        real_mount = panel.mount_replay_batch

        async def fail_after_mount(batch: list[Widget], *, before: Widget | None) -> None:
            await real_mount(batch, before=before)
            raise RuntimeError("test failure after the final batch attached")

        monkeypatch.setattr(panel, "mount_replay_batch", fail_after_mount)
        failures = await asyncio.gather(replay.wait_complete(), replay.wait_complete(), return_exceptions=True)
        for failure in failures:
            assert isinstance(failure, RuntimeError)
            assert str(failure) == "Could not mount all archived transcript entries"
        assert chat_content_children(panel) == roots
        assert panel.get_all_messages() == messages

        await replay.wait_complete()
        assert not replay.has_pending
