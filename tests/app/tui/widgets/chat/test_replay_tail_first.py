# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tail-first replay: the newest entries mount before replay returns, older history is prepended above them."""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Callable
from contextlib import ExitStack
from functools import partial
from pathlib import Path

import pytest
from textual import events
from textual.widget import AwaitMount, Widget

from chrys.app.tui.widgets.chat.messages import AgentMessage, ErrorMessage, ThinkingMessage, UserMessage
from chrys.app.tui.widgets.chat.panel import ChatPanel
from chrys.app.tui.widgets.chat.replay_mount import REPLAY_MOUNT_BATCH_SIZE
from chrys.app.tui.widgets.chat.tool_call import ToolGroup, ToolGroupTitle
from chrys.app.tui.widgets.markdown.widget import VirtualizedMarkdown
from chrys.app.tui.widgets.welcome import WelcomeWidget
from chrys.foundation.models.history_markers import ANSWER_SEGMENT_KEY
from chrys.foundation.tool_kinds import KIND_FILESYSTEM_READ, TOOL_CALL_KIND_METADATA_KEY
from chrys.kernel import Content
from tests.app.tui.widgets._scroll_gc import install_fake_chat_panel_gc
from tests.support.tui_helpers import (
    ChatPanelApp,
    _simulate_chat_panel_user_scroll_y,
    chat_content_children,
    title_text,
)
from tests.support.waiting import DEFAULT_WAIT_TIMEOUT, wait_for, wait_until, wait_until_quiet

pytestmark = pytest.mark.usefixtures("frozen_earlier_heap")

# At _SIZE either history replays as a tail batch and two prepend batches,
# so the first and the last prepended batch differ.
_TURNS = 36
_TOOL_TURNS = 24
_SIZE = (80, 24)


def _turns(count: int = _TURNS, *, with_tools: bool = False) -> list[dict[str, object]]:
    messages: list[dict[str, object]] = []
    for index in range(count):
        messages.append({"role": "user", "contents": [{"type": "text", "text": f"question {index}"}]})
        if with_tools:
            call_id = f"call-{index}"
            messages.append(
                {
                    "role": "assistant",
                    "contents": [
                        {
                            "type": "function_call",
                            "name": "read_file",
                            "call_id": call_id,
                            "arguments": {"path": f"/tmp/file_{index}.py"},
                        }
                    ],
                }
            )
            messages.append(
                {"role": "tool", "contents": [{"type": "function_result", "call_id": call_id, "result": "ok"}]}
            )
        messages.append(
            {"role": "assistant", "contents": [{"type": "text", "text": f"answer {index}\n\nmore about {index}"}]}
        )
    return messages


def _expected_messages(count: int = _TURNS) -> list[tuple[str, str]]:
    expected: list[tuple[str, str]] = []
    for index in range(count):
        expected.append(("You", f"question {index}"))
        expected.append(("Agent", f"answer {index}\n\nmore about {index}"))
    return expected


def _message_texts(panel: ChatPanel) -> list[str]:
    return [
        child.text if isinstance(child, AgentMessage) else child._text
        for child in panel.children
        if isinstance(child, (UserMessage, AgentMessage))
    ]


def _assert_document_order(panel: ChatPanel) -> None:
    seqs = [panel.transcript_seq(child) for child in chat_content_children(panel)]
    assert None not in seqs
    assert seqs == sorted(seqs)
    assert len(set(seqs)) == len(seqs)


def _placement(before: Widget | None, after: Widget | None) -> dict[str, Widget]:
    """The placement a wrapped ``mount`` was given: an explicit ``None`` would skip the panel's own place above its chrome."""
    return {key: widget for key, widget in (("before", before), ("after", after)) if widget is not None}


class _PrependGate:
    """Hold the background prepend right after its first batch mounted."""

    def __init__(self, panel: ChatPanel, monkeypatch: pytest.MonkeyPatch) -> None:
        self.release = asyncio.Event()
        self.prepended_batches = 0
        real = panel.mount_replay_batch

        async def gated(batch: list[Widget], *, before: Widget | None) -> None:
            await real(batch, before=before)
            if before is not None:
                self.prepended_batches += 1
                await self.release.wait()

        monkeypatch.setattr(panel, "mount_replay_batch", gated)

    async def wait_held(self, pilot: object) -> None:
        await wait_for(lambda: self.prepended_batches == 1, pilot=pilot, description="first prepend batch held")


def _hold_refresh_callbacks(
    panel: ChatPanel, monkeypatch: pytest.MonkeyPatch
) -> list[tuple[Callable[..., object], tuple[object, ...]]]:
    """Hold what the prepend queues for the next refresh, as a slow frame would."""
    queued: list[tuple[Callable[..., object], tuple[object, ...]]] = []
    controller = panel._replay_mount
    after_batch_mounted = controller._after_batch_mounted

    def hold_refresh_callbacks(batch: list[Widget]) -> None:
        with monkeypatch.context() as patch:
            patch.setattr(panel, "call_after_refresh", lambda callback, *args: queued.append((callback, args)) or True)
            after_batch_mounted(batch)

    monkeypatch.setattr(controller, "_after_batch_mounted", hold_refresh_callbacks)
    return queued


async def _wait_complete(panel: ChatPanel) -> None:
    await asyncio.wait_for(panel.wait_replay_complete(), timeout=DEFAULT_WAIT_TIMEOUT)


async def _settle(panel: ChatPanel, pilot: object) -> None:
    await wait_until_quiet(
        lambda: (panel.virtual_size.height, panel.scroll_y),
        description="chat layout settled",
        pilot=pilot,
    )


async def test_replay_returns_once_the_newest_entries_are_mounted(monkeypatch: pytest.MonkeyPatch) -> None:
    """A long restore mounts one bounded tail batch before returning, then prepends the rest."""
    messages = _turns()

    async with ChatPanelApp().run_test(size=_SIZE) as pilot:
        cp = pilot.app.query_one(ChatPanel)
        real_mount = cp.mount
        mounted_before_return: list[int] = []

        def mount_spy(*widgets: Widget, **kwargs: object):
            mounted_before_return.append(len(widgets))
            return real_mount(*widgets, **kwargs)

        monkeypatch.setattr(cp, "mount", mount_spy)
        await cp.replay_history(messages)
        tail = chat_content_children(cp)
        tail_mount_calls = list(mounted_before_return)
        monkeypatch.setattr(cp, "mount", real_mount)

        assert tail_mount_calls == [len(tail)]
        assert 2 <= len(tail) <= REPLAY_MOUNT_BATCH_SIZE
        assert _message_texts(cp)[-1] == f"answer {_TURNS - 1}\n\nmore about {_TURNS - 1}"
        assert cp.replay_in_progress
        assert len(cp.pending_transcript_widgets()) == 2 * _TURNS - len(tail)

        await _wait_complete(cp)

        assert not cp.replay_in_progress
        assert cp.pending_transcript_widgets() == []
        assert _message_texts(cp) == [text for _, text in _expected_messages()]
        _assert_document_order(cp)


async def test_bottom_following_view_stays_at_the_bottom_while_history_prepends(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with ChatPanelApp().run_test(size=_SIZE) as pilot:
        cp = pilot.app.query_one(ChatPanel)
        gate = _PrependGate(cp, monkeypatch)
        await cp.replay_history(_turns())
        await gate.wait_held(pilot)
        await _settle(cp, pilot)

        assert not cp._anchor_released
        assert cp.scroll_y == cp.max_scroll_y

        gate.release.set()
        await _wait_complete(cp)
        await _settle(cp, pilot)
        newest = next(child for child in reversed(cp.children) if isinstance(child, AgentMessage))

        assert not cp._anchor_released
        assert cp.scroll_y == cp.max_scroll_y
        assert cp.virtual_size.height > 3 * cp.size.height
        assert newest.text == f"answer {_TURNS - 1}\n\nmore about {_TURNS - 1}"
        assert newest.virtual_region.bottom > cp.scroll_y


async def test_scrolled_up_view_keeps_its_content_while_history_prepends(monkeypatch: pytest.MonkeyPatch) -> None:
    """Entries landing above a scrolled-up view shift the offset, not the content being read."""
    async with ChatPanelApp().run_test(size=_SIZE) as pilot:
        cp = pilot.app.query_one(ChatPanel)
        gate = _PrependGate(cp, monkeypatch)
        await cp.replay_history(_turns())
        await gate.wait_held(pilot)
        await _settle(cp, pilot)

        _simulate_chat_panel_user_scroll_y(cp, cp.max_scroll_y - 2 * cp.size.height)
        await _settle(cp, pilot)
        anchor = next(child for child in chat_content_children(cp) if child.virtual_region.bottom > cp.scroll_y + 1)
        view_offset = anchor.virtual_region.y - cp.scroll_y
        height_before = cp.virtual_size.height

        gate.release.set()
        await _wait_complete(cp)
        await _settle(cp, pilot)

        assert cp.virtual_size.height > height_before
        assert cp._anchor_released
        assert anchor.virtual_region.y - cp.scroll_y == view_offset


async def test_clear_mid_prepend_waits_for_the_batch_then_stops(monkeypatch: pytest.MonkeyPatch) -> None:
    scroll_controller_module, fake_gc = install_fake_chat_panel_gc(monkeypatch)

    async with ChatPanelApp().run_test(size=_SIZE) as pilot:
        cp = pilot.app.query_one(ChatPanel)
        gate = _PrependGate(cp, monkeypatch)
        await cp.replay_history(_turns())
        await gate.wait_held(pilot)

        clear = asyncio.create_task(cp.clear())
        assert not await wait_until(clear.done, timeout=0.2)
        assert chat_content_children(cp)

        gate.release.set()
        await asyncio.wait_for(clear, timeout=DEFAULT_WAIT_TIMEOUT)
        # The emptied panel's next layout clamps its scroll offset, which is no
        # scroll of the user's and holds no pause.
        await _settle(cp, pilot)

        assert [type(child) for child in chat_content_children(cp)] == [WelcomeWidget]
        assert not cp.replay_in_progress
        assert cp.pending_transcript_widgets() == []
        assert not scroll_controller_module.scroll_gc_paused()
        assert fake_gc.enabled is True
        # Only the held batch's young pass ran. The cleared transcript is
        # garbage now, so no full pass walks it; the next reclaim frees it.
        assert fake_gc.collect_generations == [0]
        assert not await wait_until(lambda: gate.prepended_batches > 1, timeout=0.2)


async def test_unmount_mid_prepend_cancels_it_and_releases_the_gc_pause(monkeypatch: pytest.MonkeyPatch) -> None:
    scroll_controller_module, fake_gc = install_fake_chat_panel_gc(monkeypatch)

    async with ChatPanelApp().run_test(size=_SIZE) as pilot:
        cp = pilot.app.query_one(ChatPanel)
        gate = _PrependGate(cp, monkeypatch)
        await cp.replay_history(_turns())
        await gate.wait_held(pilot)
        prepend = cp._replay_mount._task
        assert prepend is not None

        await cp.remove()

        assert not scroll_controller_module.scroll_gc_paused()
        assert fake_gc.enabled is True
        assert fake_gc.collect_generations == []
        await wait_for(prepend.done, pilot=pilot, description="prepend task finished")
        assert prepend.cancelled()
        assert gate.prepended_batches == 1


async def test_replay_gc_pause_spans_the_prepend_and_ends_with_one_collection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No automatic passes walk the growing transcript; young passes free each batch's cycles."""
    scroll_controller_module, fake_gc = install_fake_chat_panel_gc(monkeypatch)

    async with ChatPanelApp().run_test(size=_SIZE) as pilot:
        cp = pilot.app.query_one(ChatPanel)
        gate = _PrependGate(cp, monkeypatch)
        await cp.replay_history(_turns())
        await gate.wait_held(pilot)

        assert scroll_controller_module.scroll_gc_paused()
        assert fake_gc.enabled is False
        assert fake_gc.collect_generations == []

        gate.release.set()
        await _wait_complete(cp)
        # Replay's final scroll-to-end counts as a scroll and holds its own
        # debounced pause; wait that out to observe the replay's release.
        await wait_for(
            lambda: not cp._manual_scroll_gc_paused,
            pilot=pilot,
            description="scroll GC pause resumed",
        )

        assert not scroll_controller_module.scroll_gc_paused()
        assert fake_gc.enabled is True
        generations = fake_gc.collect_generations
        assert generations.count(2) == 1
        # Every prepend batch but the last is followed by a young pass; the
        # full pass right after the last one covers it.
        assert gate.prepended_batches >= 2
        assert generations[: generations.index(2)] == [0] * (gate.prepended_batches - 1)


async def test_short_replay_pauses_gc_while_mounting_and_collects_once(monkeypatch: pytest.MonkeyPatch) -> None:
    """Replay allocates only long-lived trees: no automatic passes over it, one full pass after."""
    scroll_controller_module, fake_gc = install_fake_chat_panel_gc(monkeypatch)

    async with ChatPanelApp().run_test(size=_SIZE) as pilot:
        cp = pilot.app.query_one(ChatPanel)
        real_mount = cp.mount
        gc_enabled_at_mount: list[bool] = []

        def mount_spy(*widgets: Widget, **kwargs: object):
            gc_enabled_at_mount.append(fake_gc.enabled)
            return real_mount(*widgets, **kwargs)

        monkeypatch.setattr(cp, "mount", mount_spy)
        await cp.replay_history(_turns(2))

        assert gc_enabled_at_mount == [False]
        assert not cp.replay_in_progress
        assert not scroll_controller_module.scroll_gc_paused()
        assert fake_gc.enabled is True
        assert fake_gc.collect_generations == [2]


async def test_copy_reads_the_whole_transcript_while_history_prepends(monkeypatch: pytest.MonkeyPatch) -> None:
    async with ChatPanelApp().run_test(size=_SIZE) as pilot:
        cp = pilot.app.query_one(ChatPanel)
        gate = _PrependGate(cp, monkeypatch)
        await cp.replay_history(_turns())
        await gate.wait_held(pilot)
        expected = _expected_messages()

        assert cp.pending_transcript_widgets()
        assert cp.get_all_messages() == expected
        assert cp.get_user_messages() == [entry for entry in expected if entry[0] == "You"]
        assert cp.get_agent_responses() == [entry for entry in expected if entry[0] == "Agent"]

        gate.release.set()
        await _wait_complete(cp)
        assert cp.get_all_messages() == expected


async def test_live_entry_during_prepend_stays_below_the_replayed_history(monkeypatch: pytest.MonkeyPatch) -> None:
    async with ChatPanelApp().run_test(size=_SIZE) as pilot:
        cp = pilot.app.query_one(ChatPanel)
        gate = _PrependGate(cp, monkeypatch)
        await cp.replay_history(_turns())
        await gate.wait_held(pilot)

        await cp.add_user_message("live question")
        gate.release.set()
        await _wait_complete(cp)

        assert _message_texts(cp) == [*(text for _, text in _expected_messages()), "live question"]
        _assert_document_order(cp)


async def test_entries_mounted_before_the_replay_stay_above_all_replayed_history(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A restore warning mounted before the replay stays on top; copy mid-prepend reads it first too."""
    async with ChatPanelApp().run_test(size=_SIZE) as pilot:
        cp = pilot.app.query_one(ChatPanel)
        gate = _PrependGate(cp, monkeypatch)
        await cp.add_user_message("earlier question")
        # SessionHandlers.on_session_restored: clear, the cwd warning, then the replay.
        await cp.add_error("Working directory no longer exists: /gone", action_label=None)
        await cp.replay_history(_turns())
        await gate.wait_held(pilot)
        expected = [("You", "earlier question"), *_expected_messages()]

        assert cp.pending_transcript_widgets()
        assert cp.get_all_messages() == expected
        assert cp.get_user_messages() == [entry for entry in expected if entry[0] == "You"]

        gate.release.set()
        await _wait_complete(cp)

        entries = chat_content_children(cp)
        assert isinstance(entries[0], UserMessage)
        assert isinstance(entries[1], ErrorMessage)
        assert _message_texts(cp) == [text for _, text in expected]
        assert cp.get_all_messages() == expected
        _assert_document_order(cp)


async def test_toc_jump_to_a_turn_still_pending_lands_once_it_mounts(monkeypatch: pytest.MonkeyPatch) -> None:
    async with ChatPanelApp().run_test(size=_SIZE) as pilot:
        cp = pilot.app.query_one(ChatPanel)
        gate = _PrependGate(cp, monkeypatch)
        await cp.replay_history(_turns())
        await gate.wait_held(pilot)
        oldest_turn = cp.toc_items[0].turn_id
        assert not any(isinstance(child, UserMessage) and child.id == oldest_turn for child in cp.children)

        cp.scroll_to_turn(oldest_turn)
        gate.release.set()
        await _wait_complete(cp)
        target = next(child for child in cp.children if isinstance(child, UserMessage) and child.id == oldest_turn)
        await wait_for(lambda: target.has_class("-highlighted"), pilot=pilot, description="pending turn highlighted")
        await _settle(cp, pilot)

        assert cp.scroll_y <= target.virtual_region.y <= cp.scroll_y + 1


def _jump_into_a_prepended_batch(
    panel: ChatPanel, monkeypatch: pytest.MonkeyPatch, *, batch: str, when: str
) -> tuple[list[tuple[str, bool]], ExitStack]:
    """Jump to a turn of the ``first`` or ``last`` prepended batch at ``when``, as a click in that window would.

    ``registered``: Textual has made the batch children, not mounted them.
    ``mounted``: the batch has mounted, before the prepend takes it back.
    ``handed_back``: the prepend is done with the batch, which lays out on the next refresh.

    Records each jump's turn id and whether the turn had geometry. The returned
    stack holds the screen's layout from the chosen batch's mount until the
    jump, as a frame that has not come yet would; the test owns closing it.
    """
    jumped: list[tuple[str, bool]] = []
    layout_hold = ExitStack()
    held: list[list[Widget]] = []
    real_mount = panel.mount
    real_mount_batch = panel.mount_replay_batch
    controller = panel._replay_mount
    after_batch_mounted = controller._after_batch_mounted

    def chosen() -> bool:
        return not jumped and not held and (batch == "first" or not panel.pending_transcript_widgets())

    def jump(entries: list[Widget]) -> None:
        turn = next(entry for entry in entries if isinstance(entry, UserMessage))
        if turn.id is not None:
            jumped.append((turn.id, bool(turn.region)))
            panel.scroll_to_turn(turn.id)

    def mount(*widgets: Widget, before: Widget | None = None, after: Widget | None = None) -> AwaitMount:
        mounting = real_mount(*widgets, **_placement(before, after))
        if when == "registered" and before is not None and chosen():
            jump(list(widgets))
        return mounting

    async def mount_replay_batch(entries: list[Widget], *, before: Widget | None) -> None:
        if when == "registered" or before is None or not chosen():
            await real_mount_batch(entries, before=before)
            return
        compositor = panel.screen._compositor
        # Whether an earlier scroll left the screen's map invalid here depends
        # on timing (a loaded runner): forced, so every run takes that path.
        compositor._full_map_invalidated = True
        layout_hold.enter_context(panel.app.batch_update())
        # A geometry read rebuilds an invalid map, and would place the held
        # batch without a frame: built here, before the batch mounts, the map
        # stays valid under the hold, so the batch has no geometry until the
        # hold ends.
        full_map = compositor.full_map
        assert panel in full_map
        held.append(entries)
        await real_mount_batch(entries, before=before)
        if when == "mounted":
            jump(entries)
            layout_hold.close()

    def hand_back(entries: list[Widget]) -> None:
        after_batch_mounted(entries)
        if when == "handed_back" and held and entries is held[0]:
            jump(entries)
            layout_hold.close()

    monkeypatch.setattr(panel, "mount", mount)
    monkeypatch.setattr(panel, "mount_replay_batch", mount_replay_batch)
    monkeypatch.setattr(controller, "_after_batch_mounted", hand_back)
    return jumped, layout_hold


@pytest.mark.parametrize("when", ["registered", "mounted", "handed_back"])
@pytest.mark.parametrize("batch", ["first", "last"])
async def test_a_toc_jump_into_a_batch_not_laid_out_yet_lands_once_it_is(
    monkeypatch: pytest.MonkeyPatch, batch: str, when: str
) -> None:
    """A prepended batch is among the panel's children from registration, without geometry until it lays out.

    The last batch lays out after the prepend has ended, and the jump still lands.
    """
    async with ChatPanelApp().run_test(size=_SIZE) as pilot:
        cp = pilot.app.query_one(ChatPanel)
        jumped, layout_hold = _jump_into_a_prepended_batch(cp, monkeypatch, batch=batch, when=when)
        with layout_hold:
            await cp.replay_history(_turns())
            await _wait_complete(cp)
        [(turn_id, had_geometry)] = jumped
        assert not had_geometry
        if batch == "last":
            assert turn_id == cp.toc_items[0].turn_id
        target = next(child for child in cp.children if isinstance(child, UserMessage) and child.id == turn_id)
        await wait_for(lambda: target.has_class("-highlighted"), pilot=pilot, description="the turn highlighted")
        await _settle(cp, pilot)

        assert cp.scroll_y <= target.virtual_region.y <= cp.scroll_y + 1
        assert cp.scroll_y < cp.max_scroll_y


@pytest.mark.parametrize("when", ["registered", "ended"])
@pytest.mark.parametrize(
    ("move", "start"),
    [("held_jump", "bottom"), ("jump", "bottom"), ("scroll", "bottom"), ("jump", "top"), ("scroll", "top")],
)
async def test_a_view_moved_before_the_last_batch_lays_out_stays_on_its_turn(
    monkeypatch: pytest.MonkeyPatch, move: str, start: str, when: str
) -> None:
    """The view moves to a turn while the last prepended batch is a child without geometry.

    ``held_jump``: a jump held for the first prepended batch lands, a slow
    frame having run that batch's refresh callback this late. ``jump``: a TOC
    click on a turn of that batch, which has its geometry. ``scroll``: the
    user scrolls that turn to the top. The view moves from the ``bottom`` it
    follows, or from the ``top``, where it shows a warning mounted before the
    replay, which no batch moves. ``registered``: right after the last batch
    registers; ``ended``: once the prepend has handed it back and ended. The
    last batch then lays out above the turn, and the view moves with it.
    """
    async with ChatPanelApp().run_test(size=_SIZE) as pilot:
        cp = pilot.app.query_one(ChatPanel)
        if start == "top":
            await cp.add_error("Working directory no longer exists: /gone", action_label=None)
        warning = next((child for child in cp.children if isinstance(child, ErrorMessage)), None)
        queued = _hold_refresh_callbacks(cp, monkeypatch)
        layout_hold = ExitStack()
        turns: list[UserMessage] = []
        last_batch: list[Widget] = []
        real_mount = cp.mount
        real_mount_batch = cp.mount_replay_batch

        def run_refresh_callbacks() -> None:
            callbacks = list(queued)
            queued.clear()
            for callback, args in callbacks:
                callback(*args)

        def move_view() -> None:
            if start == "bottom":
                # Following the bottom until now, so the view tracks no child yet.
                assert not cp.is_anchor_released()
            else:
                assert cp.is_anchor_released()
                assert cp.scroll_y == 0
            if move == "held_jump":
                # The first batch's callback; the last batch's, once queued, waits for its layout.
                callback, args = queued.pop(0)
                callback(*args)
            elif move == "jump":
                assert turns[0].id is not None
                cp.scroll_to_turn(turns[0].id)
            else:
                _simulate_chat_panel_user_scroll_y(cp, turns[0].virtual_region.y)

        def mount(*widgets: Widget, before: Widget | None = None, after: Widget | None = None) -> AwaitMount:
            mounting = real_mount(*widgets, **_placement(before, after))
            if before is not None and not turns:
                turns.append(next(widget for widget in widgets if isinstance(widget, UserMessage)))
                if move == "held_jump" and turns[0].id is not None:
                    cp.scroll_to_turn(turns[0].id)
            elif before is not None and when == "registered":
                move_view()
            return mounting

        async def mount_replay_batch(entries: list[Widget], *, before: Widget | None) -> None:
            if before is None or cp.pending_transcript_widgets():
                await real_mount_batch(entries, before=before)
                return
            last_batch.extend(entries)
            await wait_for(lambda: bool(turns[0].region), description="the first prepended batch laid out")
            if move != "held_jump":
                run_refresh_callbacks()
            if start == "top":
                _simulate_chat_panel_user_scroll_y(cp, 0)
                await wait_for(
                    lambda: (tracked := cp._scroll_controller._view_hold_child) is not None and tracked[0] is warning,
                    description="the view tracking the warning",
                )
            layout_hold.enter_context(cp.app.batch_update())
            await real_mount_batch(entries, before=before)
            if when == "registered":
                layout_hold.close()

        monkeypatch.setattr(cp, "mount", mount)
        monkeypatch.setattr(cp, "mount_replay_batch", mount_replay_batch)
        with layout_hold:
            await cp.replay_history(_turns())
            await _wait_complete(cp)
            if when == "ended":
                move_view()
        await wait_for(
            lambda: all(entry.region for entry in last_batch if isinstance(entry, UserMessage)),
            pilot=pilot,
            description="the last prepended batch laid out",
        )
        assert cp._scroll_controller.view_hold_active
        run_refresh_callbacks()
        await _settle(cp, pilot)

        assert cp.scroll_y <= turns[0].virtual_region.y <= cp.scroll_y + 1
        assert cp.scroll_y < cp.max_scroll_y
        # Nothing lands above the view any more.
        assert not cp._scroll_controller.view_hold_active


@pytest.mark.parametrize("gesture", ["wheel", "page_up_key"])
async def test_user_scroll_drops_a_toc_jump_still_waiting_for_its_turn(
    monkeypatch: pytest.MonkeyPatch, gesture: str
) -> None:
    """A pending jump must not yank a reader who scrolled elsewhere before its turn mounted."""
    async with ChatPanelApp().run_test(size=_SIZE) as pilot:
        cp = pilot.app.query_one(ChatPanel)
        gate = _PrependGate(cp, monkeypatch)
        await cp.replay_history(_turns())
        await gate.wait_held(pilot)
        await _settle(cp, pilot)
        oldest_turn = cp.toc_items[0].turn_id

        cp.scroll_to_turn(oldest_turn)
        before_gesture = cp.scroll_y
        if gesture == "wheel":
            cp.post_message(events.MouseScrollUp(None, 0, 0, 0, 0, 0, False, False, False))
        else:
            cp.focus()
            await pilot.press("pageup")
        await wait_for(lambda: cp.scroll_y < before_gesture, pilot=pilot, description="the user scrolled up")
        await _settle(cp, pilot)
        reading = next(child for child in chat_content_children(cp) if child.virtual_region.bottom > cp.scroll_y + 1)
        offset = reading.virtual_region.y - cp.scroll_y

        gate.release.set()
        await _wait_complete(cp)
        await _settle(cp, pilot)

        target = next(child for child in cp.children if isinstance(child, UserMessage) and child.id == oldest_turn)
        assert not target.has_class("-highlighted")
        assert reading.virtual_region.y - cp.scroll_y == offset


async def test_a_message_sent_drops_a_toc_jump_still_waiting_for_its_turn(monkeypatch: pytest.MonkeyPatch) -> None:
    """The new message is what the user looks at now; the turn they jumped to before must not take the view."""
    async with ChatPanelApp().run_test(size=_SIZE) as pilot:
        cp = pilot.app.query_one(ChatPanel)
        gate = _PrependGate(cp, monkeypatch)
        await cp.replay_history(_turns())
        await gate.wait_held(pilot)
        await _settle(cp, pilot)
        oldest_turn = cp.toc_items[0].turn_id

        cp.scroll_to_turn(oldest_turn)
        cp.agent_running = True
        await cp.add_user_message("new question")
        message = cp.query(UserMessage).last()
        await wait_for(lambda: bool(message.outer_size), pilot=pilot, description="the message laid out")

        gate.release.set()
        await _wait_complete(cp)
        await _settle(cp, pilot)

        target = next(child for child in cp.children if isinstance(child, UserMessage) and child.id == oldest_turn)
        assert not target.has_class("-highlighted")
        assert cp.scroll_y <= message.virtual_region.y < cp.scroll_y + cp.size.height


@pytest.mark.parametrize("refreshes", [0, 1])
async def test_a_toc_jump_right_after_a_restore_is_not_pulled_back_to_the_bottom(
    monkeypatch: pytest.MonkeyPatch, refreshes: int
) -> None:
    """The replay scrolls to the bottom once it has laid out; a jump before then, or in the next frame, stays put.

    Every callback the panel queues for a refresh from the end of the replay
    waits for the test, as slow frames would; the jump comes after
    ``refreshes`` rounds of them.
    """
    async with ChatPanelApp().run_test(size=_SIZE) as pilot:
        cp = pilot.app.query_one(ChatPanel)
        queued: list[Callable[[], object]] = []
        holding = False
        real_call_after_refresh = cp.call_after_refresh
        real_after_replay_history_mounted = cp.after_replay_history_mounted

        def call_after_refresh(callback: Callable[..., object], *args: object, **kwargs: object) -> bool:
            if not holding:
                return real_call_after_refresh(callback, *args, **kwargs)
            queued.append(partial(callback, *args, **kwargs))
            return True

        def after_replay_history_mounted() -> None:
            nonlocal holding
            holding = True
            real_after_replay_history_mounted()

        async def run_refresh_callbacks() -> None:
            callbacks = list(queued)
            queued.clear()
            for callback in callbacks:
                result = callback()
                if inspect.isawaitable(result):
                    await result

        monkeypatch.setattr(cp, "call_after_refresh", call_after_refresh)
        monkeypatch.setattr(cp, "after_replay_history_mounted", after_replay_history_mounted)
        # Short enough to mount in the tail batch alone.
        await cp.replay_history(_turns(8))
        assert holding
        assert not cp.replay_in_progress
        await _settle(cp, pilot)
        for _ in range(refreshes):
            await run_refresh_callbacks()
        target = [child for child in cp.children if isinstance(child, UserMessage)][1]
        assert target.id is not None
        cp.scroll_to_turn(target.id)
        for _ in range(3):
            await run_refresh_callbacks()
        assert not queued
        holding = False
        await _settle(cp, pilot)

        assert cp.scroll_y <= target.virtual_region.y <= cp.scroll_y + 1
        assert cp.scroll_y < cp.max_scroll_y


@pytest.mark.parametrize("superseded_by", [None, "user_scroll", "newer_jump"])
async def test_a_toc_jump_queued_for_the_next_refresh_yields_to_later_navigation(
    monkeypatch: pytest.MonkeyPatch, superseded_by: str | None
) -> None:
    """Once its turn mounts, a jump waits for a refresh; a scroll or jump before then wins."""
    async with ChatPanelApp().run_test(size=_SIZE) as pilot:
        cp = pilot.app.query_one(ChatPanel)
        gate = _PrependGate(cp, monkeypatch)
        queued = _hold_refresh_callbacks(cp, monkeypatch)
        await cp.replay_history(_turns())
        await gate.wait_held(pilot)
        oldest_turn = cp.toc_items[0].turn_id
        mounted_turn = next(child for child in cp.children if isinstance(child, UserMessage)).id
        assert mounted_turn is not None and mounted_turn != oldest_turn

        cp.scroll_to_turn(oldest_turn)
        gate.release.set()
        await _wait_complete(cp)
        assert queued, "the jump waits for the refresh after its turn mounted"
        if superseded_by == "user_scroll":
            cp.note_user_scroll()
        elif superseded_by == "newer_jump":
            cp.scroll_to_turn(mounted_turn)
        for callback, args in queued:
            callback(*args)
        await _settle(cp, pilot)

        oldest = next(child for child in cp.children if isinstance(child, UserMessage) and child.id == oldest_turn)
        assert oldest.has_class("-highlighted") is (superseded_by is None)
        if superseded_by == "newer_jump":
            newer = next(child for child in cp.children if isinstance(child, UserMessage) and child.id == mounted_turn)
            assert newer.has_class("-highlighted")
            assert cp.scroll_y <= newer.virtual_region.y <= cp.scroll_y + 1


async def test_a_toc_jump_queued_before_a_clear_stays_out_of_the_next_transcript(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The next transcript reuses the turn ids, so a jump queued for the old one must not land in it."""
    async with ChatPanelApp().run_test(size=_SIZE) as pilot:
        cp = pilot.app.query_one(ChatPanel)
        gate = _PrependGate(cp, monkeypatch)
        queued = _hold_refresh_callbacks(cp, monkeypatch)
        await cp.replay_history(_turns())
        await gate.wait_held(pilot)
        oldest_turn = cp.toc_items[0].turn_id
        cp.scroll_to_turn(oldest_turn)
        gate.release.set()
        # The prepend has ended; only the jump it queued is left of it.
        await _wait_complete(cp)
        assert queued, "the jump waits for the refresh after its turn mounted"

        await cp.clear()
        await cp.replay_history(_turns())
        await _wait_complete(cp)
        await _settle(cp, pilot)
        assert cp.toc_items[0].turn_id == oldest_turn
        assert cp.scroll_y == cp.max_scroll_y > 0
        for callback, args in queued:
            callback(*args)
        await _settle(cp, pilot)

        assert not any(child.has_class("-highlighted") for child in cp.children if isinstance(child, UserMessage))
        assert cp.scroll_y == cp.max_scroll_y


async def test_a_toc_jump_queued_before_an_unmount_never_runs(monkeypatch: pytest.MonkeyPatch) -> None:
    async with ChatPanelApp().run_test(size=_SIZE) as pilot:
        cp = pilot.app.query_one(ChatPanel)
        gate = _PrependGate(cp, monkeypatch)
        queued = _hold_refresh_callbacks(cp, monkeypatch)
        await cp.replay_history(_turns())
        await gate.wait_held(pilot)
        cp.scroll_to_turn(cp.toc_items[0].turn_id)
        gate.release.set()
        await _wait_complete(cp)
        assert queued, "the jump waits for the refresh after its turn mounted"

        await cp.remove()
        jumps: list[str] = []
        monkeypatch.setattr(cp, "scroll_to_turn", jumps.append)
        for callback, args in queued:
            callback(*args)

        assert jumps == []


@pytest.mark.parametrize("fold_processes", [False, True])
async def test_fold_all_during_prepend_reaches_tool_groups_mounted_later(
    monkeypatch: pytest.MonkeyPatch, fold_processes: bool
) -> None:
    # Folded turns hide entries, so the tail and the first batch mount all of the unfolded history but its first
    # prompt: the turns past it leave a tool group each pending, six here. More would only add work the wait must
    # outlast.
    count = _TOOL_TURNS + 6 if fold_processes else _TOOL_TURNS
    async with ChatPanelApp(fold_finished_turns=lambda: fold_processes).run_test(size=_SIZE) as pilot:
        cp = pilot.app.query_one(ChatPanel)
        gate = _PrependGate(cp, monkeypatch)
        await cp.replay_history(_turns(count, with_tools=True))
        await gate.wait_held(pilot)
        mounted_groups = list(cp.query(ToolGroup))
        assert mounted_groups
        assert all(group.collapsed for group in mounted_groups)
        assert any(isinstance(widget, ToolGroup) for widget in cp.pending_transcript_widgets())

        # Every group is collapsed (pending ones included), so fold-all expands.
        assert cp.toggle_fold_all() is False

        gate.release.set()
        await _wait_complete(cp)
        groups = list(cp.query(ToolGroup))

        assert len(groups) == count
        assert [group.collapsed for group in groups] == [False] * count
        # Everything is expanded now, so the next fold-all collapses.
        assert cp.toggle_fold_all() is True


async def test_fold_all_during_prepend_reaches_thinking_mounted_later(monkeypatch: pytest.MonkeyPatch) -> None:
    """Only the oldest turns thought, so fold-all finds no fold mounted yet: the pending thinking takes the choice."""
    thought_turns = 4
    messages = _turns()
    for message in messages[: 2 * thought_turns]:
        if message["role"] == "assistant":
            message["contents"] = [Content.from_text_reasoning(text="Thinking first.").to_dict(), *message["contents"]]

    async with ChatPanelApp(thinking_expanded=lambda: True).run_test(size=_SIZE) as pilot:
        cp = pilot.app.query_one(ChatPanel)
        gate = _PrependGate(cp, monkeypatch)
        await cp.replay_history(messages)
        await gate.wait_held(pilot)
        assert not cp.query(ThinkingMessage)
        assert any(isinstance(widget, ThinkingMessage) for widget in cp.pending_transcript_widgets())

        assert cp.toggle_fold_all() is False

        gate.release.set()
        await _wait_complete(cp)
        thinking = list(cp.query(ThinkingMessage))

        assert len(thinking) == thought_turns
        assert [message.collapsed for message in thinking] == [False] * len(thinking)
        assert all(message.query(VirtualizedMarkdown) for message in thinking)
        assert cp.toggle_fold_all() is True


async def test_fold_all_while_the_last_batch_mounts_outlasts_the_choice_before_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The last batch has left ``pending`` while it mounts; a fold-all then still decides how it ends up."""
    async with ChatPanelApp().run_test(size=_SIZE) as pilot:
        cp = pilot.app.query_one(ChatPanel)
        gate = _PrependGate(cp, monkeypatch)
        real_mount_batch = cp.mount_replay_batch
        folds: list[bool] = []

        async def mount_replay_batch(entries: list[Widget], *, before: Widget | None) -> None:
            await real_mount_batch(entries, before=before)
            if before is not None and not cp.pending_transcript_widgets():
                folds.append(cp.toggle_fold_all())

        monkeypatch.setattr(cp, "mount_replay_batch", mount_replay_batch)
        await cp.replay_history(_turns(_TOOL_TURNS, with_tools=True))
        await gate.wait_held(pilot)
        assert cp.toggle_fold_all() is False

        gate.release.set()
        await _wait_complete(cp)

        assert folds == [True]
        assert [group.collapsed for group in cp.query(ToolGroup)] == [True] * _TOOL_TURNS


async def test_a_folder_change_during_prepend_reaches_tool_groups_mounted_later(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Each turn's group read one file in the chat's folder, so its row names that file relative to the folder.
    folder = Path(Path(__file__).anchor, "work")
    messages = _turns(_TOOL_TURNS, with_tools=True)
    calls = [content for message in messages for content in message["contents"] if content["type"] == "function_call"]
    for index, call in enumerate(calls):
        call["arguments"] = {"path": str(folder / f"file_{index}.py")}
        call["additional_properties"] = {TOOL_CALL_KIND_METADATA_KEY: KIND_FILESYSTEM_READ}
    async with ChatPanelApp().run_test(size=_SIZE) as pilot:
        cp = pilot.app.query_one(ChatPanel)
        cp.set_workspace_cwd(str(folder))
        gate = _PrependGate(cp, monkeypatch)
        await cp.replay_history(messages)
        await gate.wait_held(pilot)
        assert any(isinstance(widget, ToolGroup) for widget in cp.pending_transcript_widgets())

        # The chat moves to another folder while older history waits to mount: the files lie outside it now. Mounted
        # rows name them anew, and the rows still to mount read the folder as they mount, not as replay planned them.
        cp.set_workspace_cwd(str(folder.parent / "elsewhere"))
        gate.release.set()
        await _wait_complete(cp)

        titles = [title_text(group.query_one(ToolGroupTitle))[2:] for group in cp.query(ToolGroup)]
        assert titles == [f"Read {folder / f'file_{index}.py'}" for index in range(_TOOL_TURNS)]


async def test_a_withdrawal_mid_prepend_waits_for_the_replay_then_removes_each_answer_it_names(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The answers a bare Continue drops leave the view whichever replay batch mounted them."""
    messages = _turns()
    for index, segment in ((1, "oldest"), (-1, "newest")):
        contents = messages[index]["contents"]
        assert isinstance(contents, list)
        contents[0]["additional_properties"] = {ANSWER_SEGMENT_KEY: segment}
    async with ChatPanelApp().run_test(size=_SIZE) as pilot:
        cp = pilot.app.query_one(ChatPanel)
        gate = _PrependGate(cp, monkeypatch)
        await cp.replay_history(messages)
        await gate.wait_held(pilot)

        withdrawal = asyncio.create_task(cp.withdraw_answer(("oldest", "newest")))
        await asyncio.sleep(0)
        assert not withdrawal.done()
        gate.release.set()
        await asyncio.wait_for(withdrawal, timeout=DEFAULT_WAIT_TIMEOUT)

        withdrawn = {"answer 0\n\nmore about 0", f"answer {_TURNS - 1}\n\nmore about {_TURNS - 1}"}
        assert _message_texts(cp) == [text for _, text in _expected_messages() if text not in withdrawn]


async def test_retirement_mid_prepend_waits_and_removes_the_whole_failed_turn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from chrys.foundation.models.history_markers import HistoryMarkerKind
    from chrys.kernel import Message

    messages = [Message("user", ["Retired opener"]).to_dict()]
    for index in range(90):
        messages.append(
            Message(
                "user",
                [f"Unanswered note {index}"],
                additional_properties={HistoryMarkerKind.INJECTED_KEY: True},
            ).to_dict()
        )
    messages.append(
        Message(
            "assistant", ["Stopped"], additional_properties={HistoryMarkerKind.KEY: HistoryMarkerKind.INTERRUPTED}
        ).to_dict()
    )
    async with ChatPanelApp().run_test(size=_SIZE) as pilot:
        cp = pilot.app.query_one(ChatPanel)
        gate = _PrependGate(cp, monkeypatch)
        cleanup = None
        try:
            await cp.replay_history(messages)
            await gate.wait_held(pilot)
            assert not any(message.text == "Retired opener" for message in cp.query(UserMessage))
            await cp.add_user_message("Replacement")
            cleanup = asyncio.create_task(cp.apply_fresh_turn_cleanup(retired_previous_turn=True, removed_item_ids=()))
            await asyncio.sleep(0)
            assert not cleanup.done()
            gate.release.set()
            await asyncio.wait_for(cleanup, timeout=DEFAULT_WAIT_TIMEOUT)
            cp.apply_turn_started(1)
            assert [message.text for message in cp.query(UserMessage)] == ["Replacement"]
            assert [(item.summary, item.turn_index) for item in cp.toc_items] == [("Replacement", 1)]
        finally:
            gate.release.set()
            if cleanup is not None:
                await cleanup
            await cp.wait_replay_complete()
