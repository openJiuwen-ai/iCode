# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tests for the Phase-4 compaction UX surfaces.

Covers the three layers added for live compaction visibility:

* :class:`CompactionCard` — the main-agent transcript card (running timer,
  summarized/failed/interrupted terminal states, LAST_WORDS markdown panel)
* :class:`LiveTranscriptRenderer` — card mounting closes the open tool
  group, completion routing by ``compaction_id``, dangling-card finalization
  when a run ends before ``CompactionFinished`` arrives
* :class:`ToolGroupRegistry` / :class:`SubAgentToolCall` — the nested
  "Compacting conversation..." line on sub-agent cards
"""

from __future__ import annotations

import pytest
from textual.app import App, ComposeResult
from textual.widget import Widget
from textual.widgets import Static

import chrys.app.tui.widgets.chat.compaction_card as compaction_card_module
from chrys.app.tui.i18n import LocaleController
from chrys.app.tui.widgets.chat.agent_transcript_surface import (
    TranscriptCompactionFinishedOp,
)
from chrys.app.tui.widgets.chat.compaction_card import CompactionCard, CompactionCardTitle
from chrys.app.tui.widgets.chat.live_renderer import LiveTranscriptRenderer
from chrys.app.tui.widgets.chat.panel import ChatPanel
from chrys.app.tui.widgets.chat.renderers.sub_agent import SubAgentToolCall
from chrys.app.tui.widgets.chat.toc_model import TurnTocModel
from chrys.app.tui.widgets.chat.tool_call import ToolCopyButton, ToolGroup, is_tool_copy_excluded
from chrys.app.tui.widgets.chat.tool_registry import ToolGroupRegistry
from chrys.app.tui.widgets.markdown import VirtualizedMarkdown
from chrys.foundation.config.settings import Settings
from chrys.foundation.tool_kinds import KIND_FILESYSTEM_READ
from tests.support.frozen_clock import freeze_monotonic
from tests.support.pilot_barrier import screen_is_settled
from tests.support.tui_helpers import ChatPanelApp, mount_sub_agent_detail
from tests.support.waiting import wait_for


class _FakeMount:
    def __init__(self, calls: list[str]) -> None:
        self.calls = calls
        self.widgets: list[Widget] = []

    async def mount_transcript_widget(self, widget: Widget) -> bool:
        self.calls.append(f"mount:{type(widget).__name__}")
        self.widgets.append(widget)
        return True


class _FakeUi:
    def __init__(self, calls: list[str]) -> None:
        self.calls = calls

    def schedule_anchor_sync(self) -> None:
        self.calls.append("anchor_sync")


class _FakeStatus:
    def __init__(self, calls: list[str]) -> None:
        self.calls = calls

    async def remove_trailing_status(self) -> None:
        self.calls.append("remove_status")


class _FakeQuery:
    def direct_children(self) -> list[Widget]:
        return []


class _FakeToolGroup:
    def __init__(self) -> None:
        self.collapsed = False

    def finalize_activity_header(self) -> None:
        return None

    def cancel_running(self, *, stopped_at: float | None = None) -> None:
        return None


def _make_renderer(calls: list[str]) -> tuple[LiveTranscriptRenderer, ToolGroupRegistry, _FakeMount]:
    status = _FakeStatus(calls)
    registry = ToolGroupRegistry(_FakeMount(calls), _FakeUi(calls), status)
    mount = _FakeMount(calls)
    renderer = LiveTranscriptRenderer(
        mount,
        _FakeUi(calls),
        status,
        _FakeQuery(),
        TurnTocModel(),
        registry,
    )
    return renderer, registry, mount


# --------------------------------------------------------------------- #
# CompactionCard widget states
# --------------------------------------------------------------------- #


class _CardApp(App):
    locale_controller = LocaleController(Settings(locale="en"))

    def compose(self) -> ComposeResult:
        yield CompactionCard("c-1")


@pytest.mark.asyncio
async def test_compaction_card_running_state_shows_its_running_dot_before_the_label() -> None:
    async with _CardApp().run_test() as pilot:
        await pilot.pause()
        card = pilot.app.query_one(CompactionCard)

        assert card.status == "running"
        title = card.query_one(CompactionCardTitle)
        assert title._work_running is True
        # The dot blinks between its lit and dim glyphs.
        label = title.render().plain
        assert label[:2] in {"• ", "◦ "}
        assert label[2:].startswith("Compacting conversation...")
        assert card.query_one("#compaction-note-panel").display is False


@pytest.mark.parametrize(
    ("locale", "tooltip", "summary_title"),
    [
        ("en", "Copy summary markdown", "Summary"),
        ("zh-Hans", "复制摘要 Markdown", "摘要"),
    ],
)
@pytest.mark.asyncio
async def test_compaction_summary_chrome_uses_mount_locale(
    locale: str,
    tooltip: str,
    summary_title: str,
) -> None:
    class LocalizedCardApp(App):
        def __init__(self) -> None:
            self.locale_controller = LocaleController(Settings(locale=locale))
            super().__init__()

        def compose(self) -> ComposeResult:
            yield CompactionCard("c-1")

    async with LocalizedCardApp().run_test() as pilot:
        await pilot.pause()
        card = pilot.app.query_one(CompactionCard)

        assert card.query_one("#compaction-header").query_one(ToolCopyButton).tooltip == tooltip
        assert str(card.query_one("#compaction-note-panel").border_title) == summary_title


@pytest.mark.asyncio
async def test_compaction_card_running_label_elapsed_trail_survives_rendering(monkeypatch: pytest.MonkeyPatch) -> None:
    """The elapsed suffix must survive actual segment rendering — the label
    goes through Content markup (terminal labels use theme variables like
    ``$warning`` that Rich inline styles cannot parse), and a rich-style
    implementation once silently dropped the trail.

    The card's clock is frozen: its once-a-second tick would otherwise move the
    trail past the asserted second whenever the runner stalls (a long GC pause)."""
    clock = freeze_monotonic(monkeypatch, compaction_card_module)

    async with _CardApp().run_test() as pilot:
        await pilot.pause()
        card = pilot.app.query_one(CompactionCard)
        clock.now += 65
        card._tick()
        await pilot.pause()

        label_widget = card.query_one("#compaction-label", Static)
        assert "(1m 5s)" in label_widget.render().plain
        rendered = "".join(segment.text for segment in label_widget.render_line(0))
        assert "Compacting conversation..." in rendered
        assert "(1m 5s)" in rendered


@pytest.mark.asyncio
async def test_compaction_card_header_is_copy_excluded_but_note_is_selectable() -> None:
    """Drag-selection must skip the header chrome (spinner glyphs, title,
    copy affordance) while the summary markdown stays selectable prose."""
    async with _CardApp().run_test() as pilot:
        await pilot.pause()
        card = pilot.app.query_one(CompactionCard)

        # The title carries the running dot.
        assert is_tool_copy_excluded(card.query_one("#compaction-label", Static))
        assert is_tool_copy_excluded(card.query_one(ToolCopyButton))
        assert not is_tool_copy_excluded(card.query_one("#compaction-note", VirtualizedMarkdown))


@pytest.mark.asyncio
async def test_compaction_card_completes_with_summary_collapsed_by_default() -> None:
    note = "## Progress\n\n- explored the codebase"
    async with _CardApp().run_test() as pilot:
        await pilot.pause()
        card = pilot.app.query_one(CompactionCard)

        card.set_complete(outcome="ok", duration_ms=68_000, last_words=note)
        await pilot.pause()

        assert card.status == "ok"
        assert card.query_one(CompactionCardTitle)._work_running is False
        label = card.query_one("#compaction-label", Static).render().plain
        assert "Conversation summarized ✓" in label
        assert "(1m 8s)" in label
        assert card.has_class("-done")
        assert card.collapsed is True
        assert card.query_one("#compaction-note-panel").display is False
        assert card.query_one("#compaction-note", VirtualizedMarkdown).source == note
        assert label.startswith("▸ ")


@pytest.mark.asyncio
async def test_compaction_card_success_surfaces_no_format_warning() -> None:
    """An accepted-note violation is deliberately not shown on the main
    card — the compaction succeeded, so no warning line lingers next to
    the summary (sub-agent lines and the ACP bridge still narrate it)."""
    async with _CardApp().run_test() as pilot:
        await wait_for(
            lambda: len(pilot.app.query(CompactionCard)) == 1,
            pilot=pilot,
            description="compaction card mounted",
        )
        card = pilot.app.query_one(CompactionCard)

        card.set_complete(outcome="ok", duration_ms=1_000, last_words="## Task\nDo it")
        await pilot.pause()

        assert not card.query("#compaction-format-warning")
        texts = [static.render().plain for static in card.query(Static)]
        assert not any("format warning" in text for text in texts)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("outcome", "expected_label", "expected_tone"),
    [
        ("failed", "Compaction failed", "$error"),
        ("canceled", "Compaction interrupted", "$warning"),
    ],
)
async def test_compaction_card_terminal_failure_states_hide_note_panel(
    outcome: str, expected_label: str, expected_tone: str
) -> None:
    async with _CardApp().run_test() as pilot:
        await pilot.pause()
        card = pilot.app.query_one(CompactionCard)

        card.set_complete(outcome=outcome, duration_ms=5_000)
        await pilot.pause()

        assert card.status == outcome
        rendered = card.query_one("#compaction-label", Static).render()
        assert expected_label in rendered.plain
        # Theme tone, not a literal color: the label styles through the
        # $warning/$error design tokens.
        assert any(expected_tone in str(span.style) for span in rendered.spans)
        assert not card.has_class("-done")
        assert card.query_one("#compaction-note-panel").display is False


@pytest.mark.asyncio
async def test_compaction_card_running_accent_flips_to_muted_on_finish() -> None:
    """While live the card wears the status-bar accent styling; any terminal
    outcome flips it back to the muted transcript tone via ``-finished``."""
    async with _CardApp().run_test() as pilot:
        await pilot.pause()
        card = pilot.app.query_one(CompactionCard)

        assert not card.has_class("-finished")
        running_color = card.query_one("#compaction-label", Static).styles.color

        card.set_complete(outcome="ok", duration_ms=5)
        await pilot.pause()

        assert card.has_class("-finished")
        finished_color = card.query_one("#compaction-label", Static).styles.color
        assert finished_color != running_color


@pytest.mark.asyncio
async def test_compaction_card_failure_reason_replaces_duration() -> None:
    """A safety-limit trip renders its cause where the duration would go."""
    async with _CardApp().run_test() as pilot:
        await pilot.pause()
        card = pilot.app.query_one(CompactionCard)

        card.set_complete(
            outcome="failed",
            duration_ms=201,
            failure_reason="20 attempts limit exceeded for current turn",
        )
        await pilot.pause()

        label = card.query_one("#compaction-label", Static).render().plain
        assert "Compaction failed (20 attempts limit exceeded for current turn)" in label
        assert "201ms" not in label


@pytest.mark.asyncio
async def test_compaction_card_retry_notices_quiet_then_visible_then_cleared() -> None:
    """The first three side-call retries stay hidden; later ones show as a
    warning line that any terminal state clears again."""
    async with _CardApp().run_test() as pilot:
        await pilot.pause()
        card = pilot.app.query_one(CompactionCard)
        notice = card.query_one("#compaction-retry-notice", Static)

        for attempt in range(1, 4):
            card.show_retry_notice("empty response", attempt, 5, 3)
        await pilot.pause()
        assert notice.display is False

        card.show_retry_notice("empty response", 4, 5, 7)
        await pilot.pause()
        assert notice.display is True
        assert notice.render().plain == "⚠ empty response — retrying in 7s (4/5)"

        card.set_complete(outcome="ok", duration_ms=1_000, last_words="## Task\nDo it")
        await pilot.pause()
        assert notice.display is False

        # Terminal card ignores stale notices.
        card.show_retry_notice("late", 5, 5, 3)
        await pilot.pause()
        assert notice.display is False


@pytest.mark.asyncio
async def test_compaction_card_retry_notice_keeps_provider_brackets_literal() -> None:
    """Provider error text with square brackets must render literally, not be
    parsed as Textual markup (which would restyle it or raise and drop the
    whole notice inside the suppress block), and embedded newlines must
    survive rendering."""
    async with _CardApp().run_test() as pilot:
        await pilot.pause()
        card = pilot.app.query_one(CompactionCard)
        notice = card.query_one("#compaction-retry-notice", Static)

        message = 'Error code: 400 - [type=missing] for field "note" [/invalid\nRetry-After: 30'
        for attempt in range(1, 5):
            card.show_retry_notice(message, attempt, 5, 3)
        await pilot.pause()

        assert notice.display is True
        assert notice.render().plain == f"⚠ {message} — retrying in 3s (4/5)"


@pytest.mark.asyncio
async def test_compaction_card_first_terminal_outcome_wins() -> None:
    """A late dangling-card sweep must not overwrite a real completion."""
    async with _CardApp().run_test() as pilot:
        await pilot.pause()
        card = pilot.app.query_one(CompactionCard)

        card.set_complete(outcome="ok", duration_ms=1_000, last_words="note text long enough")
        card.set_complete(outcome="canceled")
        await pilot.pause()

        assert card.status == "ok"
        label = card.query_one("#compaction-label", Static).render().plain
        assert "Conversation summarized" in label


@pytest.mark.asyncio
async def test_compaction_card_header_click_toggles_summary_panel() -> None:
    """The completed header expands/collapses the note panel like a ToolGroup."""
    async with _CardApp().run_test() as pilot:
        await pilot.pause()
        card = pilot.app.query_one(CompactionCard)
        card.set_complete(outcome="ok", duration_ms=32_000, last_words="## Note")
        await pilot.pause()

        await pilot.click(CompactionCardTitle)
        await pilot.pause()

        assert card.collapsed is False
        assert not card.has_class("-collapsed")
        assert card.query_one("#compaction-note-panel").display is True
        label = card.query_one("#compaction-label", Static).render().plain
        assert label.startswith("▾ ")

        await pilot.click(CompactionCardTitle)
        await pilot.pause()

        assert card.collapsed is True
        assert card.query_one("#compaction-note-panel").display is False
        label = card.query_one("#compaction-label", Static).render().plain
        assert label.startswith("▸ ")


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["failed", "canceled", None])
async def test_compaction_card_header_click_ignored_without_note(outcome: str | None) -> None:
    """Running cards and note-less terminal cards have nothing to toggle."""
    async with _CardApp().run_test() as pilot:
        await pilot.pause()
        card = pilot.app.query_one(CompactionCard)
        if outcome is not None:
            card.set_complete(outcome=outcome, duration_ms=1_000)
            await pilot.pause()

        card.on_compaction_card_title_clicked()
        await pilot.pause()

        assert card.collapsed is False
        assert card.query_one("#compaction-note-panel").display is False


@pytest.mark.asyncio
async def test_compaction_card_copy_button_copies_raw_markdown(monkeypatch: pytest.MonkeyPatch) -> None:
    """The trailing copy affordance copies the raw note markdown string."""
    copied: list[str] = []

    def _fake_copy(app: object, text: str, *, max_terminal_bytes: int | None = None) -> bool:
        copied.append(text)
        return True

    monkeypatch.setattr("chrys.app.tui.widgets.chat.compaction_card.copy_text_to_clipboards", _fake_copy)
    note = "## Progress Note\n\n- raw `markdown` **string**"
    async with _CardApp().run_test() as pilot:
        await pilot.pause()
        card = pilot.app.query_one(CompactionCard)
        assert card.query_one(ToolCopyButton).display is False

        card.set_complete(outcome="ok", duration_ms=1_000, last_words=note)
        await pilot.pause()
        assert card.query_one(ToolCopyButton).display is True

        await pilot.click(ToolCopyButton)
        await pilot.pause()

    assert copied == [note]


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["failed", "canceled"])
async def test_compaction_card_copy_button_stays_hidden_without_note(outcome: str) -> None:
    async with _CardApp().run_test() as pilot:
        await pilot.pause()
        card = pilot.app.query_one(CompactionCard)

        card.set_complete(outcome=outcome, duration_ms=1_000)
        await pilot.pause()

        assert card.query_one(ToolCopyButton).display is False


# --------------------------------------------------------------------- #
# LiveTranscriptRenderer routing
# --------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_add_compaction_start_closes_group_and_mounts_card() -> None:
    calls: list[str] = []
    renderer, registry, mount = _make_renderer(calls)
    group = _FakeToolGroup()
    registry.current_tool_group = group  # type: ignore[assignment]

    await renderer.add_compaction_start("c-1")

    assert group.collapsed is True
    assert registry.current_tool_group is None
    assert calls.index("remove_status") < calls.index("mount:CompactionCard")
    assert "mount:CompactionCard" in calls
    assert "anchor_sync" in calls
    assert isinstance(mount.widgets[0], CompactionCard)
    assert mount.widgets[0].compaction_id == "c-1"


@pytest.mark.asyncio
async def test_a_compaction_card_keeps_a_blank_row_below_a_folded_tool_group() -> None:
    """Thinking and tool rows have no vertical margin: the card's own top margin keeps the row between them."""
    app = ChatPanelApp()
    async with app.run_test(size=(70, 20)) as pilot:
        panel = app.query_one(ChatPanel)
        await panel.add_user_message("prompt")
        await panel.add_tool_start("r1", "read_file", KIND_FILESYSTEM_READ, args={})
        await panel.add_tool_result("r1", "read_file", "ok")
        await panel.add_compaction_start("c-1")
        group = panel.query_one(ToolGroup)
        card = panel.query_one(CompactionCard)
        await wait_for(
            lambda: group.collapsed and card.region.height > 0 and screen_is_settled(app, app.screen),
            pilot=pilot,
            description="the card mounted below the folded group",
        )

        assert card.region.y == group.region.bottom + 1


@pytest.mark.asyncio
async def test_complete_compaction_routes_by_id_and_untracks() -> None:
    calls: list[str] = []
    renderer, _registry, mount = _make_renderer(calls)
    await renderer.add_compaction_start("c-1")
    card = mount.widgets[0]
    assert isinstance(card, CompactionCard)

    renderer.complete_compaction("unknown", outcome="ok")
    assert card.status == "running"

    renderer.complete_compaction(
        "c-1",
        outcome="ok",
        duration_ms=42,
        last_words="note",
        format_violation='missing required heading "## Next"',
    )
    assert card.status == "ok"

    # Untracked after completion: a later dangling sweep leaves it alone.
    await renderer.cancel_running_tools()
    assert card.status == "ok"


@pytest.mark.asyncio
async def test_show_compaction_retry_routes_to_running_card_only() -> None:
    calls: list[str] = []
    renderer, _registry, mount = _make_renderer(calls)
    await renderer.add_compaction_start("c-1")
    done_card = mount.widgets[0]
    assert isinstance(done_card, CompactionCard)
    renderer.complete_compaction("c-1", outcome="ok", last_words="note")
    await renderer.add_compaction_start("c-2")
    live_card = mount.widgets[1]
    assert isinstance(live_card, CompactionCard)

    renderer.show_compaction_retry("empty response", 1, 5, 3)

    assert live_card._retry_notice_count == 1
    assert done_card._retry_notice_count == 0

    # No running card left — the stale notice is dropped, not crashed on.
    renderer.complete_compaction("c-2", outcome="failed")
    renderer.show_compaction_retry("empty response", 2, 5, 3)
    assert live_card._retry_notice_count == 1


@pytest.mark.asyncio
async def test_cancel_running_tools_finalizes_dangling_compaction_card() -> None:
    """Interrupt/error teardown must not leave a compaction card spinning."""
    calls: list[str] = []
    renderer, _registry, mount = _make_renderer(calls)
    await renderer.add_compaction_start("c-1")
    card = mount.widgets[0]
    assert isinstance(card, CompactionCard)

    await renderer.cancel_running_tools()

    assert card.status == "canceled"


@pytest.mark.asyncio
async def test_reset_for_clear_drops_compaction_tracking() -> None:
    calls: list[str] = []
    renderer, _registry, mount = _make_renderer(calls)
    await renderer.add_compaction_start("c-1")
    card = mount.widgets[0]
    assert isinstance(card, CompactionCard)

    renderer.reset_for_clear()
    renderer.complete_compaction("c-1", outcome="ok")

    assert card.status == "running"


# --------------------------------------------------------------------- #
# Sub-agent compaction line
# --------------------------------------------------------------------- #


class _SubAgentApp(App):
    def compose(self) -> ComposeResult:
        yield SubAgentToolCall("call-1", "Explore", args={"prompt": "investigate"})


@pytest.mark.asyncio
async def test_sub_agent_compaction_line_renders_running_then_completed() -> None:
    async with _SubAgentApp().run_test() as pilot:
        await pilot.pause()
        tool = pilot.app.query_one(SubAgentToolCall)
        surface = await mount_sub_agent_detail(tool, pilot)

        tool.add_compaction_start("c-1")
        await wait_for(
            lambda: len(surface.query(CompactionCard)) == 1,
            pilot=pilot,
            description="running sub-agent compaction detail",
        )

        entry = tool._inner_tools["compaction:c-1"]
        assert entry.status == "running"
        card = surface.query_one(CompactionCard)
        assert card.status == "running"

        tool.complete_compaction("c-1", outcome="ok", duration_ms=185_000)
        await pilot.pause()

        assert entry.status == "complete"
        assert "compaction:c-1" not in tool._inner_tools
        assert card.status == "ok"
        assert card._duration_ms == 185_000


@pytest.mark.asyncio
async def test_sub_agent_compaction_line_failure_states() -> None:
    async with _SubAgentApp().run_test() as pilot:
        await pilot.pause()
        tool = pilot.app.query_one(SubAgentToolCall)

        tool.add_compaction_start("c-1")
        tool.complete_compaction("c-1", outcome="failed")
        assert "compaction:c-1" not in tool._inner_tools

        tool.add_compaction_start("c-2")
        tool.complete_compaction("c-2", outcome="canceled")
        assert "compaction:c-2" not in tool._inner_tools
        surface = await mount_sub_agent_detail(tool, pilot)
        await wait_for(
            lambda: len(surface.query(CompactionCard)) == 2,
            pilot=pilot,
            description="failed and canceled sub-agent compaction details",
        )
        cards = list(surface.query(CompactionCard))
        assert [card.status for card in cards] == ["failed", "canceled"]

        tool.add_compaction_start("c-3")
        tool.complete_compaction(
            "c-3",
            outcome="failed",
            failure_reason="20 attempts limit exceeded for current turn",
        )
        await wait_for(
            lambda: len(surface.query(CompactionCard)) == 3,
            pilot=pilot,
            description="third sub-agent compaction detail",
        )
        cards = list(surface.query(CompactionCard))
        assert cards[-1].status == "failed"
        assert cards[-1]._failure_reason == "20 attempts limit exceeded for current turn"


@pytest.mark.asyncio
async def test_sub_agent_compaction_card_matches_main_surface_for_accepted_format_violation() -> None:
    violation = 'missing required heading "## Next"'
    async with _SubAgentApp().run_test() as pilot:
        await wait_for(
            lambda: len(pilot.app.query(SubAgentToolCall)) == 1,
            pilot=pilot,
            description="sub-agent card mounted",
        )
        tool = pilot.app.query_one(SubAgentToolCall)

        tool.add_compaction_start("c-1")
        tool.complete_compaction("c-1", outcome="ok", format_violation=violation)
        surface = await mount_sub_agent_detail(tool, pilot)
        await wait_for(
            lambda: len(surface.query(CompactionCard)) == 1 and surface.query_one(CompactionCard).status == "ok",
            pilot=pilot,
            description="completed sub-agent compaction detail",
        )

        card = surface.query_one(CompactionCard)
        assert card.status == "ok"
        assert "compaction:c-1" not in tool._inner_tools


@pytest.mark.asyncio
async def test_sub_agent_compaction_success_clears_stale_retry_banner() -> None:
    """A LAST_WORDS retry that later succeeds must not leave "Retrying in …"
    visible under a "Conversation compacted." line — success is forward
    progress, same rule as add_inner_tool_start."""
    async with _SubAgentApp().run_test() as pilot:
        await pilot.pause()
        tool = pilot.app.query_one(SubAgentToolCall)

        tool.add_compaction_start("c-1")
        tool.set_retry_attempt("LAST_WORDS compaction: overloaded", 2, 5, 30)
        await pilot.pause()
        assert tool.has_class("-retrying")

        tool.complete_compaction("c-1", outcome="ok", duration_ms=5_000)
        await pilot.pause()

        assert not tool.has_class("-retrying")
        assert tool.query_one("#sa-retry-banner", Static).render().plain == ""


def test_sub_agent_compaction_entry_does_not_count_as_tool_call() -> None:
    tool = SubAgentToolCall("call-1", "Explore", args={"prompt": "investigate"})

    tool.add_compaction_start("c-1")

    assert tool._total_inner_calls == 0
    assert "compaction:c-1" in tool._inner_tools


def test_registry_routes_sub_agent_compaction_by_invocation_id() -> None:
    calls: list[str] = []
    registry = ToolGroupRegistry(_FakeMount(calls), _FakeUi(calls), _FakeStatus(calls))
    widget = SubAgentToolCall("call-1", "Explore", args={"prompt": "investigate"})
    registry.sub_agent_widgets["inv-1"] = widget

    registry.add_sub_agent_compaction_start("Explore", "inv-1", "c-1")
    assert widget._inner_tools["compaction:c-1"].status == "running"

    registry.complete_sub_agent_compaction("inv-1", "c-1", outcome="ok", duration_ms=1_000)
    assert "compaction:c-1" not in widget._inner_tools
    finished = [
        operation
        for operation in widget._transcript_journal.operations
        if isinstance(operation, TranscriptCompactionFinishedOp)
    ]
    assert [(operation.compaction_id, operation.outcome) for operation in finished] == [("c-1", "ok")]

    # Unknown invocation ids are dropped silently on completion.
    registry.complete_sub_agent_compaction("inv-missing", "c-9", outcome="ok")


def test_registry_routes_committed_signal_to_retired_card_after_end_group() -> None:
    """A detached committed signal landing after an interrupt still counts.

    Interrupt rendering calls ``end_group()`` before the detached delivery
    task fires; the card stays in the transcript, so its retired routing
    entry must still receive the durable-round counter bump.  A transcript
    clear discards the cards and the retired entries with them.
    """
    calls: list[str] = []
    registry = ToolGroupRegistry(_FakeMount(calls), _FakeUi(calls), _FakeStatus(calls))
    widget = SubAgentToolCall("call-1", "Explore", args={"prompt": "investigate"})
    registry.sub_agent_widgets["inv-1"] = widget

    registry.end_group()
    assert "inv-1" not in registry.sub_agent_widgets

    registry.record_sub_agent_compaction_committed("inv-1", "c-1")
    assert widget._compaction_count == 1

    registry.reset_for_clear()
    registry.record_sub_agent_compaction_committed("inv-1", "c-2")
    assert widget._compaction_count == 1


def test_registry_routes_interleaved_parallel_sub_agent_compactions() -> None:
    """Two same-name sub-agents compacting in parallel must not cross-route.

    Mirrors the real event order: ``InvocationStarted`` links each
    invocation to its widget before any compaction event exists, so the
    interleaved started/finished events resolve strictly by invocation id —
    never by the FIFO fallback.
    """
    calls: list[str] = []
    registry = ToolGroupRegistry(_FakeMount(calls), _FakeUi(calls), _FakeStatus(calls))
    widget_a = SubAgentToolCall("call-a", "Explore", args={"prompt": "a"})
    widget_b = SubAgentToolCall("call-b", "Explore", args={"prompt": "b"})
    registry.sub_agent_widgets["inv-a"] = widget_a
    registry.sub_agent_widgets["inv-b"] = widget_b

    registry.add_sub_agent_compaction_start("Explore", "inv-a", "c-a")
    registry.add_sub_agent_compaction_start("Explore", "inv-b", "c-b")
    # B finishes first even though A started first.
    registry.complete_sub_agent_compaction("inv-b", "c-b", outcome="ok", duration_ms=2_000)

    assert widget_a._inner_tools["compaction:c-a"].status == "running"
    assert "compaction:c-b" not in widget_a._inner_tools
    assert "compaction:c-b" not in widget_b._inner_tools

    registry.complete_sub_agent_compaction("inv-a", "c-a", outcome="canceled")
    assert "compaction:c-a" not in widget_a._inner_tools
    outcomes_a = [
        operation.outcome
        for operation in widget_a._transcript_journal.operations
        if isinstance(operation, TranscriptCompactionFinishedOp)
    ]
    outcomes_b = [
        operation.outcome
        for operation in widget_b._transcript_journal.operations
        if isinstance(operation, TranscriptCompactionFinishedOp)
    ]
    assert outcomes_a == ["canceled"]
    assert outcomes_b == ["ok"]


def test_registry_routes_compaction_after_tool_name_fallback_link() -> None:
    calls: list[str] = []
    registry = ToolGroupRegistry(_FakeMount(calls), _FakeUi(calls), _FakeStatus(calls))
    widget = SubAgentToolCall("call-1", "Explore", args={"prompt": "investigate"})
    registry.unlinked_sub_agents.setdefault("explore_agent", []).append(widget)
    registry.link_sub_agent_invocation(
        "",
        "inv-1",
        "Explore Agent",
        tool_name="explore_agent",
    )

    registry.add_sub_agent_compaction_start("Explore Agent", "inv-1", "c-1")

    assert registry.sub_agent_widgets["inv-1"] is widget
    assert "compaction:c-1" in widget._inner_tools
