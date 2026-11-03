# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tests for BackendEventHandler event routing and the agent-message render gate."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from chrys.app.tui.i18n import LocaleController
from chrys.app.tui.screens.main.event_handlers import (
    _AGENT_FAILED_TO_LOAD,
    _UNKNOWN_ERROR,
    BackendEventHandler,
)
from chrys.app.tui.screens.main.session_handlers import (
    _PROFILE_SWITCH_INDICATOR,
    _WORKING_DIRECTORY_INDICATOR,
)
from chrys.app.tui.screens.main.state import MainScreenState, RunState
from chrys.app.tui.support.gc_freeze import (
    GcAbsorbReason,
    GcAbsorbRequested,
)
from chrys.app.tui.widgets.sidebar.tasks import TodoListState
from chrys.foundation.config.settings import Settings
from chrys.foundation.events.types import (
    ApprovalModeUpdated,
    CompactionFinished,
    CompactionStarted,
    ContextCompressed,
    InvocationAnswerWithdrawn,
    InvocationContextPressure,
    InvocationMessage,
    InvocationStarted,
    InvocationToolCallResult,
    SettingsReloaded,
    TodoListUpdated,
    UserInjectResult,
)
from chrys.foundation.i18n import DisplayPath, Localizer, MessageRef
from chrys.foundation.i18n.formatting import format_message
from chrys.foundation.models.invocations import InvocationOrigin
from chrys.foundation.models.todos import TodoItem
from chrys.foundation.tool_result_metadata import TOOL_INTERRUPTED_METADATA_KEY
from chrys.service.approval.policy import ApprovalMode
from tests.support.tui_helpers import (
    fake_session_title,
    make_backend_handler,
    status_text,
)


def test_event_error_fallbacks_keep_english_and_localize_chinese() -> None:
    assert format_message(_UNKNOWN_ERROR.bind()) == "Unknown error"
    assert format_message(_AGENT_FAILED_TO_LOAD.bind()) == "Agent failed to load."
    chinese = Localizer("zh-Hans")
    assert chinese.render(_UNKNOWN_ERROR.bind()) == "未知错误"
    assert chinese.render(_AGENT_FAILED_TO_LOAD.bind()) == "智能体加载失败。"


def test_session_indicators_keep_english_and_localize_chinese() -> None:
    profile = _PROFILE_SWITCH_INDICATOR.bind(from_label="Code", to_label="QA")
    workspace = _WORKING_DIRECTORY_INDICATOR.bind(path=DisplayPath("/repo"))
    assert format_message(profile) == "Agent profile switched: Code → QA"
    assert format_message(workspace) == "Working directory → /repo"
    chinese = Localizer("zh-Hans")
    assert chinese.render(profile) == "智能体配置已切换：Code → QA"  # noqa: RUF001
    assert chinese.render(workspace) == "工作目录 → /repo"


def test_approval_mode_updated_updates_state_header_and_notification() -> None:
    notifications: list[tuple[str, str, str, float | None]] = []
    debug_log: list[tuple[str, str]] = []
    screen = SimpleNamespace(
        _debug=lambda key, message="": debug_log.append((key, message)),
        header_approval_mode=ApprovalMode.MANUAL,
        notify=lambda message, *, title, severity="information", timeout=3, markup=False: notifications.append(
            (message, title, severity, timeout)
        ),
    )
    handler = make_backend_handler(screen)

    asyncio.run(handler.on_approval_mode_updated(ApprovalModeUpdated(mode=ApprovalMode.AUTO.value)))

    assert handler.approval_mode is ApprovalMode.AUTO
    assert screen.header_approval_mode is ApprovalMode.AUTO
    assert notifications == [("Approval mode: AUTO", "Approval", "information", 2)]
    assert debug_log[-1] == ("ApprovalMode", "AUTO")


def test_settings_reloaded_reprojects_notification_settings() -> None:
    # The delivery service holds a projection taken at app init; a reload that
    # only installed new settings must trigger a re-projection or an external
    # document edit would never reach it.
    calls: list[str] = []
    screen = SimpleNamespace(_refresh_notification_settings=lambda: calls.append("refreshed"))
    handler = make_backend_handler(screen)

    asyncio.run(handler.on_settings_reloaded(SettingsReloaded()))

    assert calls == ["refreshed"]


def test_settings_reloaded_reprojects_the_verify_command_word_list() -> None:
    # Same LIVE-tier routing as the notification projection: the dashboard's
    # classification word list is captured at construction, so a reload that
    # only installed a new value must push it through or actions keep being
    # classified against the previous document until the screen is rebuilt.
    calls: list[str] = []
    screen = SimpleNamespace(_refresh_trajectory_verify_commands=lambda: calls.append("reprojected"))
    handler = make_backend_handler(screen)

    asyncio.run(handler.on_settings_reloaded(SettingsReloaded()))

    assert calls == ["reprojected"]


# ──────────── on_sub_agent_paused (interrupt-race gate) ─────────────────
#
# During a user interrupt the screen flips ``agent_running`` to False
# BEFORE the backend cascade tears down live sub-agent controllers.  A
# ``InvocationPaused`` event that was already in-flight when the interrupt
# fired can therefore arrive at the TUI handler AFTER the UI has already
# considered the run stopped.  Without a gate the card would flicker
# into a paused state carrying Retry/Abort buttons that point at a
# controller the engine has already dropped — see event_handlers.py
# comment for the full rationale.


def _make_pause_handler(agent_running: bool) -> tuple[BackendEventHandler, list[tuple]]:
    """Build a handler whose mock screen records ChatPanel calls.

    When the gate passes, ``query_one(ChatPanel)`` must be called and
    then ``panel.sub_agent_paused(...)`` must record its args in the
    returned list.  When the gate short-circuits, the list stays empty
    and ``query_one`` is never invoked.
    """
    calls: list[tuple] = []

    class _FakePanel:
        def sub_agent_paused(self, *args) -> None:
            calls.append(args)

        def sub_agent_resumed_after_pause(self, invocation_id: str) -> None:
            calls.append(("resumed", invocation_id))

        def sub_agent_cascade_aborted(self, invocation_id: str) -> None:
            calls.append(("cascade_aborted", invocation_id))

        def sub_agent_aborted(self, invocation_id: str, last_error: str) -> None:
            calls.append(("aborted", invocation_id))

    def _query_one(_cls):
        return _FakePanel()

    screen = SimpleNamespace(_state=MainScreenState(run=RunState(agent_running=agent_running)), query_one=_query_one)
    handler = make_backend_handler(screen)
    return handler, calls


async def _run_pause(handler: BackendEventHandler, *, parent: InvocationOrigin | None = None) -> None:
    from chrys.foundation.events.types import InvocationPaused

    event = InvocationPaused(
        reason="stream_stall",
        last_error="boom",
        retry_attempts=3,
        diagnostic_path="/session/approvals/acp.log",
        origin=InvocationOrigin("sub_agent", "", "inv-1", parent),
    )
    await handler.on_sub_agent_paused(event)


def test_on_sub_agent_paused_forwards_when_running() -> None:
    handler, calls = _make_pause_handler(agent_running=True)
    asyncio.run(_run_pause(handler))
    assert calls == [("inv-1", "stream_stall", "boom", 3, "/session/approvals/acp.log", None)]


def test_on_sub_agent_paused_gated_after_interrupt() -> None:
    """Late paused event after the interrupt clears ``agent_running``
    is dropped — no ChatPanel lookup, no paused card."""
    handler, calls = _make_pause_handler(agent_running=False)
    asyncio.run(_run_pause(handler))
    assert calls == []


def test_on_sub_agent_paused_ignores_a_workflow_node_child() -> None:
    """A workflow node's sub-agent has no card in the chat transcript: its pause is dropped
    even while a chat turn is running."""
    handler, calls = _make_pause_handler(agent_running=True)
    asyncio.run(_run_pause(handler, parent=InvocationOrigin("workflow_node", "", "node-run", None)))
    assert calls == []
    assert handler.attention_mark() == ""


@pytest.mark.parametrize(
    ("origin", "expected"),
    [
        (InvocationOrigin("turn", "s1", "turn-1", None), [("chat", ("seg-1",))]),
        (
            InvocationOrigin("sub_agent", "s1", "inv-1", InvocationOrigin("turn", "s1", "turn-1", None)),
            [("sub_agent", "inv-1", ("seg-1",))],
        ),
        # A workflow node's details hear it from the workflow projector; the chat has no card for its children.
        (InvocationOrigin("workflow_node", "s1", "node-1", None), []),
        (
            InvocationOrigin("sub_agent", "s1", "inv-2", InvocationOrigin("workflow_node", "s1", "node-1", None)),
            [],
        ),
    ],
    ids=["turn", "chat-sub-agent", "workflow-node", "workflow-child"],
)
def test_a_withdrawn_answer_leaves_the_view_that_shows_it(origin: InvocationOrigin, expected: list[tuple]) -> None:
    calls: list[tuple] = []

    class _FakePanel:
        async def withdraw_answer(self, segment_ids: tuple[str, ...]) -> None:
            calls.append(("chat", segment_ids))

        def sub_agent_answer_withdrawn(self, invocation_id: str, segment_ids: tuple[str, ...]) -> None:
            calls.append(("sub_agent", invocation_id, segment_ids))

    screen = SimpleNamespace(_state=MainScreenState(run=RunState()), query_one=lambda _cls: _FakePanel())
    handler = make_backend_handler(screen)

    asyncio.run(handler.on_answer_withdrawn(InvocationAnswerWithdrawn(origin=origin, segment_ids=("seg-1",))))

    assert calls == expected


@pytest.mark.parametrize("settle", ["decided", "resumed", "cascade_aborted", "aborted", "next_run"])
def test_a_paused_chat_sub_agent_waits_for_a_retry_decision_until_settled(settle: str) -> None:
    from chrys.foundation.events.types import InvocationAborted, InvocationCascadeAborted, InvocationResumed

    handler, _calls = _make_pause_handler(agent_running=True)
    assert handler.attention_mark() == ""
    asyncio.run(_run_pause(handler))
    assert handler.attention_mark() == "!"

    origin = InvocationOrigin("sub_agent", "", "inv-1", None)
    if settle == "decided":
        handler.sub_agent_decided("inv-1")
    elif settle == "resumed":
        asyncio.run(handler.on_sub_agent_resumed_after_pause(InvocationResumed(origin=origin)))
    elif settle == "cascade_aborted":
        asyncio.run(handler.on_sub_agent_cascade_aborted(InvocationCascadeAborted(origin=origin)))
    elif settle == "aborted":
        asyncio.run(handler.on_sub_agent_aborted(InvocationAborted(origin=origin)))
    else:
        handler.reset_chat_attention()

    assert handler.attention_mark() == ""
    asyncio.run(_run_pause(handler))
    assert handler.attention_mark() == "!", "a later failure pauses it again"


def test_an_ignored_late_pause_does_not_wait_for_the_user() -> None:
    handler, _calls = _make_pause_handler(agent_running=False)
    asyncio.run(_run_pause(handler))
    assert handler.attention_mark() == ""


def test_a_pause_its_chat_run_ended_without_settling_no_longer_waits() -> None:
    """A cancel can end a paused child with no event; a workflow run started next must not show its ``!``."""
    handler, _calls = _make_pause_handler(agent_running=True)
    asyncio.run(_run_pause(handler))
    assert handler.attention_mark() == "!"
    handler._state.run.agent_running = False
    assert handler.attention_mark() == ""


# ──────────── compaction events: status bar text ────────────────────────
#
# While Phase-4 compaction runs, the status bar must read "Compacting
# conversation..." instead of the stale "Thinking"/"Running: <tool>" text,
# and flip back to "Thinking" once the note is in — but only while the run
# is still live, so a canceled-outcome event arriving during interrupt
# teardown cannot resurrect the hidden status bar (``show`` re-adds
# ``-visible``).


def _make_compaction_handler(agent_running: bool) -> tuple[BackendEventHandler, list[tuple]]:
    """Build a handler whose mock screen records ChatPanel/StatusBar calls."""
    calls: list[tuple] = []

    class _FakeWidget:
        async def add_compaction_start(self, compaction_id: str) -> None:
            calls.append(("add", compaction_id))

        def complete_compaction(self, compaction_id: str, **kwargs: object) -> None:
            calls.append(
                (
                    "complete",
                    compaction_id,
                    kwargs.get("outcome"),
                    kwargs.get("format_violation"),
                    kwargs.get("failure_reason"),
                )
            )

        def show(self, text: MessageRef | str) -> None:
            calls.append(("show", status_text(text)))

    widget = _FakeWidget()
    screen = SimpleNamespace(
        _state=MainScreenState(run=RunState(agent_running=agent_running)), query_one=lambda _cls: widget
    )
    handler = make_backend_handler(screen)
    return handler, calls


def test_on_compaction_started_sets_status_bar_to_compacting() -> None:
    handler, calls = _make_compaction_handler(agent_running=True)
    asyncio.run(handler.on_compaction_started(CompactionStarted(compaction_id="c-1")))
    assert calls.index(("add", "c-1")) < calls.index(("show", "Compacting conversation..."))


def test_on_compaction_started_gated_after_interrupt() -> None:
    handler, calls = _make_compaction_handler(agent_running=False)
    asyncio.run(handler.on_compaction_started(CompactionStarted(compaction_id="c-1")))
    assert calls == []


def test_on_compaction_finished_restores_thinking_status_while_running() -> None:
    handler, calls = _make_compaction_handler(agent_running=True)
    violation = 'missing required heading "## Next"'
    event = CompactionFinished(compaction_id="c-1", outcome="ok", duration_ms=5, format_violation=violation)
    asyncio.run(handler.on_compaction_finished(event))
    assert ("complete", "c-1", "ok", violation, "") in calls
    assert ("show", "Thinking") in calls


def test_on_compaction_finished_does_not_resurrect_status_after_interrupt() -> None:
    """The card still gets finalized, but the hidden status bar stays hidden."""
    handler, calls = _make_compaction_handler(agent_running=False)
    event = CompactionFinished(compaction_id="c-1", outcome="canceled")
    asyncio.run(handler.on_compaction_finished(event))
    assert ("complete", "c-1", "canceled", "", "") in calls
    assert not any(call[0] == "show" for call in calls)


def test_on_compaction_finished_canceled_never_restores_thinking_status() -> None:
    """Canceled = interrupted run: during teardown the event can arrive while
    ``agent_running`` is still True, and re-showing "Thinking" would overwrite
    the "Interrupted by user" flash and stick forever."""
    handler, calls = _make_compaction_handler(agent_running=True)
    event = CompactionFinished(compaction_id="c-1", outcome="canceled")
    asyncio.run(handler.on_compaction_finished(event))
    assert ("complete", "c-1", "canceled", "", "") in calls
    assert not any(call[0] == "show" for call in calls)


def test_on_compaction_finished_threads_failure_reason_to_card() -> None:
    handler, calls = _make_compaction_handler(agent_running=True)
    reason = "20 attempts limit exceeded for current turn"
    event = CompactionFinished(compaction_id="c-1", outcome="failed", duration_ms=201, failure_reason=reason)
    asyncio.run(handler.on_compaction_finished(event))
    assert ("complete", "c-1", "failed", "", reason) in calls


def test_on_context_compressed_forwards_turn_range_to_sidebar() -> None:
    calls: list[tuple[object, ...]] = []

    class _FakeChatPanel:
        async def add_context_fold(
            self,
            context_id: str,
            summary: str,
            freed_messages: int,
            turn_range: tuple[int, int],
        ) -> None:
            calls.append(("fold", context_id, summary, freed_messages, turn_range))

        def mark_turn_range_compressed(self, turn_range: tuple[int, int]) -> bool:
            calls.append(("mark", turn_range))
            return True

    class _FakeContextPanel:
        def add_compressed_block(
            self,
            context_id: str,
            summary: str,
            freed_messages: int,
            turn_range: tuple[int, int],
        ) -> None:
            calls.append(("sidebar", context_id, summary, freed_messages, turn_range))

    chat = _FakeChatPanel()
    sidebar = SimpleNamespace(context_panel=_FakeContextPanel())

    def query_one(cls: type) -> object:
        if cls.__name__ == "ChatPanel":
            return chat
        if cls.__name__ == "SidebarPanel":
            return sidebar
        raise AssertionError(f"unexpected query_one({cls.__name__})")

    screen = SimpleNamespace(query_one=query_one, _update_toc=lambda: calls.append(("toc",)))
    handler = make_backend_handler(screen)

    asyncio.run(
        handler.on_context_compressed(
            ContextCompressed(
                compressed_context_id="ctx_930083b2",
                summary="Completed turns",
                freed_messages=18,
                turn_range=(1, 4),
            )
        )
    )

    assert ("fold", "ctx_930083b2", "Completed turns", 18, (1, 4)) in calls
    assert ("sidebar", "ctx_930083b2", "Completed turns", 18, (1, 4)) in calls
    assert ("toc",) in calls


def test_on_context_pressure_surfaces_user_visible_warning() -> None:
    notifications: list[tuple[str, str, str]] = []
    debug_calls: list[tuple[str, str]] = []

    def notify(message: str, *, title: str, severity: str, **_kwargs: object) -> None:
        notifications.append((message, title, severity))

    screen = SimpleNamespace(notify=notify, _debug=lambda *args: debug_calls.append(args))
    handler = make_backend_handler(screen)

    asyncio.run(
        handler.on_context_pressure(
            InvocationContextPressure(
                origin=InvocationOrigin("turn", "", "test-turn", None),
                reason="side_call_budget",
                attempts=2,
                side_call_tokens=300_000,
                side_call_token_budget=300_000,
                source="main",
            )
        )
    )

    assert notifications == [
        (
            (
                "Conversation context compaction stopped because the progress-note token budget was exhausted. "
                "The active task may exceed its model window."
            ),
            "Warning",
            "warning",
        )
    ]
    assert debug_calls[-1] == (
        "InvocationContextPressure",
        "main:side_call_budget attempts=2 side_calls=300,000/300,000",
    )


def test_on_context_pressure_does_not_suppress_later_sub_agent_invocations() -> None:
    notifications: list[str] = []

    def notify(message: str, **_kwargs: object) -> None:
        notifications.append(message)

    screen = SimpleNamespace(notify=notify, _debug=lambda *_args: None)
    handler = make_backend_handler(screen)

    for invocation_id in ("inv-1", "inv-2"):
        asyncio.run(
            handler.on_context_pressure(
                InvocationContextPressure(
                    origin=InvocationOrigin("sub_agent", "", invocation_id, None),
                    reason="no_progress",
                    source="sub_agent",
                )
            )
        )

    assert len(notifications) == 2
    assert notifications[0] == notifications[1]


def test_sub_agent_invocation_start_forwards_display_name() -> None:
    calls: list[tuple[str, str, str, str, str]] = []

    class _FakePanel:
        def link_sub_agent_invocation(
            self,
            parent_call_id: str,
            invocation_id: str,
            agent_name: str,
            sub_agent_log_file: str = "",
            *,
            tool_name: str = "",
        ) -> None:
            calls.append((parent_call_id, invocation_id, agent_name, sub_agent_log_file, tool_name))

    panel = _FakePanel()
    screen = SimpleNamespace(_state=MainScreenState(run=RunState(agent_running=True)), query_one=lambda _cls: panel)
    handler = make_backend_handler(screen)

    asyncio.run(
        handler.on_sub_agent_invocation_start(
            InvocationStarted(
                parent_call_id="parent-1",
                agent_name="Explore Agent",
                tool_name="explore_agent",
                sub_agent_log_file="Explore_invocation-1.json",
                origin=InvocationOrigin("sub_agent", "", "invocation-1", None),
            )
        )
    )

    assert calls == [("parent-1", "invocation-1", "Explore Agent", "Explore_invocation-1.json", "explore_agent")]


def test_sub_agent_interrupted_tool_result_preserves_canonical_status() -> None:
    calls: list[dict[str, object]] = []

    class _FakePanel:
        def complete_sub_agent_tool(self, *_args: object, **kwargs: object) -> None:
            calls.append(kwargs)

    panel = _FakePanel()
    screen = SimpleNamespace(_state=MainScreenState(run=RunState(agent_running=True)), query_one=lambda _cls: panel)
    handler = make_backend_handler(screen)

    asyncio.run(
        handler.on_sub_agent_tool_result(
            InvocationToolCallResult(
                agent_name="Explore Agent",
                tool_name="read_file",
                call_id="inner-1",
                result="(interrupted)",
                metadata={TOOL_INTERRUPTED_METADATA_KEY: True},
                provider_status="interrupted",
                origin=InvocationOrigin("sub_agent", "", "invocation-1", None),
            )
        )
    )

    assert calls == [
        {
            "image_contents": [],
            "artifacts": [],
            "approval": None,
            "metadata": {TOOL_INTERRUPTED_METADATA_KEY: True},
            "provider_status": "interrupted",
            "canonical_status": "interrupted",
        }
    ]


# ──────────── on_retry_attempt: prepare_retry wiring ───────────────────
#
# On a main-agent stream stall, ``StreamRetryLoop`` calls
# ``_restore_history`` to roll ``chrys_history`` back to before the failed
# attempt.  The TUI must mirror this by finalising any pending
# intermediate-text / tool-group widgets from the failed run — otherwise
# new tool calls from the retry would mount under the stale assistant
# block.  The wiring contract: ``on_retry_attempt`` calls
# ``panel.prepare_retry()`` BEFORE ``panel.add_retry(...)`` so the retry
# notice lands after the cleanup.


def test_on_retry_attempt_calls_prepare_retry_before_add_retry() -> None:
    """Regression: wiring between the ``RetryAttempt`` event and the
    ``ChatPanel.prepare_retry()`` cleanup hook.  Order matters — the
    retry notice must be mounted AFTER pending widgets are finalised."""
    from chrys.foundation.events.types import InvocationRetryAttempt

    call_log: list[str] = []

    class _FakePanel:
        async def prepare_retry(self) -> None:
            call_log.append("prepare_retry")

        async def add_retry(self, *_args, deadline: float | None = None, wait_id: str = "") -> None:
            call_log.append("add_retry")

    class _FakeStatusBar:
        def show(self, _msg: str) -> None:
            call_log.append("status_show")

    # Local sentinel classes so ``query_one`` can dispatch by type
    # without importing the real widgets (which would require a running
    # Textual app for instantiation).
    panel_inst = _FakePanel()
    status_inst = _FakeStatusBar()

    def _query_one(cls):
        # Match by class NAME so the real widget imports inside
        # on_retry_attempt resolve to our fakes.
        if cls.__name__ == "ChatPanel":
            return panel_inst
        if cls.__name__ == "StatusBar":
            return status_inst
        raise AssertionError(f"unexpected query_one({cls.__name__})")

    def _debug(_tag: str, _msg: str) -> None:
        pass

    screen = SimpleNamespace(
        _state=MainScreenState(run=RunState(agent_running=True)), query_one=_query_one, _debug=_debug
    )
    handler = make_backend_handler(screen)

    event = InvocationRetryAttempt(
        message="Stream stalled",
        attempt=1,
        max_attempts=5,
        delay_seconds=3,
        origin=InvocationOrigin("turn", "", "turn-test", None),
    )
    asyncio.run(handler.on_retry_attempt(event))

    # Critical order: prepare_retry must run BEFORE add_retry so the
    # retry notice mounts after the stale tool group is closed.
    assert call_log[0] == "prepare_retry"
    assert call_log[1] == "add_retry"


def test_on_retry_attempt_compaction_scope_uses_detail_without_parsing_message() -> None:
    """Phase-4 side-call retries land on the live compaction card — they
    must not finalize pending widgets, mount the transcript banner, or
    flip the status bar to "Retrying"."""
    from chrys.foundation.events.types import InvocationRetryAttempt

    call_log: list[object] = []

    class _FakePanel:
        async def prepare_retry(self) -> None:
            call_log.append("prepare_retry")

        async def add_retry(self, *_args, deadline: float | None = None, wait_id: str = "") -> None:
            call_log.append("add_retry")

        def show_compaction_retry(self, message: str, attempt: int, max_attempts: int, delay_seconds: int) -> None:
            call_log.append(("compaction_retry", message, attempt, max_attempts, delay_seconds))

    class _FakeStatusBar:
        def show(self, _msg: str) -> None:
            call_log.append("status_show")

    panel_inst = _FakePanel()
    status_inst = _FakeStatusBar()

    def _query_one(cls):
        if cls.__name__ == "ChatPanel":
            return panel_inst
        if cls.__name__ == "StatusBar":
            return status_inst
        raise AssertionError(f"unexpected query_one({cls.__name__})")

    screen = SimpleNamespace(
        _state=MainScreenState(run=RunState(agent_running=True)), query_one=_query_one, _debug=lambda *_: None
    )
    handler = make_backend_handler(screen)

    event = InvocationRetryAttempt(
        message="opaque compatibility diagnostic",
        attempt=2,
        max_attempts=5,
        delay_seconds=7,
        scope="compaction",
        detail="empty response",
        origin=InvocationOrigin("turn", "", "turn-test", None),
    )
    asyncio.run(handler.on_retry_attempt(event))

    # The card gets the semantic detail verbatim; nothing else fires.
    assert call_log == [("compaction_retry", "empty response", 2, 5, 7)]


def test_retry_attempt_display_message_localizes_banner_and_keeps_english_bytes() -> None:
    """The transcript retry banner prefers the producer's display reference;
    the compaction card keeps the raw separated detail regardless."""
    from chrys.foundation.errors.display import _STREAM_STALLED
    from chrys.foundation.events.types import InvocationRetryAttempt
    from chrys.foundation.i18n import DisplayBlock
    from chrys.orchestration.engine.build.builder import _RETRY_LAST_WORDS_COMPACTION

    def _make(locale_controller: LocaleController | None) -> tuple[object, list[str], list[str]]:
        banners: list[str] = []
        cards: list[str] = []

        class _FakePanel:
            async def prepare_retry(self) -> None:
                pass

            async def add_retry(self, message: str, *_args, deadline: float | None = None, wait_id: str = "") -> None:
                banners.append(message)

            def show_compaction_retry(self, message: str, *_args) -> None:
                cards.append(message)

        class _FakeStatusBar:
            def show(self, _msg) -> None:
                pass

        panel = _FakePanel()
        status = _FakeStatusBar()

        def _query_one(cls):
            if cls.__name__ == "ChatPanel":
                return panel
            if cls.__name__ == "StatusBar":
                return status
            raise AssertionError(f"unexpected query_one({cls.__name__})")

        screen = SimpleNamespace(
            _state=MainScreenState(run=RunState(agent_running=True)), query_one=_query_one, _debug=lambda *_: None
        )
        return make_backend_handler(screen, locale_controller=locale_controller), banners, cards

    stalled = InvocationRetryAttempt(
        message="Stream stalled",
        attempt=1,
        max_attempts=5,
        delay_seconds=3,
        display_message=_STREAM_STALLED.bind(),
        origin=InvocationOrigin("turn", "", "turn-test", None),
    )

    handler, banners, _cards = _make(LocaleController(Settings(locale="zh-Hans")))
    asyncio.run(handler.on_retry_attempt(stalled))
    assert banners == ["响应流中断"]

    handler, banners, _cards = _make(None)
    asyncio.run(handler.on_retry_attempt(stalled))
    assert banners == [stalled.message]

    compaction = InvocationRetryAttempt(
        message="LAST_WORDS compaction: empty response",
        attempt=2,
        max_attempts=5,
        delay_seconds=7,
        scope="compaction",
        detail="empty response",
        display_message=_RETRY_LAST_WORDS_COMPACTION.bind(reason=DisplayBlock("empty response")),
        origin=InvocationOrigin("turn", "", "turn-test", None),
    )
    handler, banners, cards = _make(LocaleController(Settings(locale="zh-Hans")))
    asyncio.run(handler.on_retry_attempt(compaction))
    assert banners == []
    assert cards == ["empty response"]


def test_on_retry_attempt_skipped_when_not_running() -> None:
    """After interrupt the handler drops stale RetryAttempt events —
    including the cleanup call."""
    from chrys.foundation.events.types import InvocationRetryAttempt

    call_log: list[str] = []

    class _FakePanel:
        async def prepare_retry(self) -> None:
            call_log.append("prepare_retry")

        async def add_retry(self, *_args, deadline: float | None = None, wait_id: str = "") -> None:
            call_log.append("add_retry")

    def _query_one(_cls):
        raise AssertionError("query_one must not be called when the agent is not running")

    screen = SimpleNamespace(
        _state=MainScreenState(run=RunState(agent_running=False)), query_one=_query_one, _debug=lambda *_: None
    )
    handler = make_backend_handler(screen)

    event = InvocationRetryAttempt(
        message="Stream stalled",
        attempt=1,
        max_attempts=5,
        delay_seconds=3,
        origin=InvocationOrigin("turn", "", "turn-test", None),
    )
    asyncio.run(handler.on_retry_attempt(event))

    assert call_log == []


# ──────────── injection outcome unlock safety ────────────────────────────


def test_consumed_injection_unlocks_even_if_chat_render_fails() -> None:
    """A consumed injection must not leave the input bar locked if rendering fails."""
    calls: list[object] = []

    class _FakeInputBar:
        def __init__(self) -> None:
            self.locked = True

        def unlock_and_clear(self) -> None:
            calls.append("unlock_and_clear")
            self.locked = False

    class _FakeChatPanel:
        async def add_user_message(self, text: str, *, is_injection: bool = False, **_kwargs: object) -> None:
            calls.append(("add_user_message", text, is_injection))
            raise RuntimeError("render failed")

    input_bar = _FakeInputBar()
    panel = _FakeChatPanel()

    def query_one(cls: type):
        if cls.__name__ == "InputBar":
            return input_bar
        if cls.__name__ == "ChatPanel":
            return panel
        raise AssertionError(f"unexpected query_one({cls})")

    screen = SimpleNamespace(
        query_one=query_one,
        _update_toc=lambda: calls.append("update_toc"),
        _debug=lambda *_args: calls.append("debug"),
    )
    handler = make_backend_handler(screen)

    async def _run() -> None:
        await handler.on_injection_outcome(UserInjectResult(text="queued text", consumed=True))

    try:
        asyncio.run(_run())
    except RuntimeError as exc:
        assert str(exc) == "render failed"
    else:
        raise AssertionError("render failure should propagate")

    assert input_bar.locked is False
    assert calls == [
        ("add_user_message", "queued text", True),
        "unlock_and_clear",
    ]


def _make_injection_outcome_screen(calls: list[object]) -> SimpleNamespace:
    """Mock screen capturing input-bar and chat-panel effects of injection results."""

    class _FakeInputBar:
        def __init__(self) -> None:
            self.locked = True

        def unlock_and_clear(self) -> None:
            calls.append("unlock_and_clear")
            self.locked = False

        def unlock_and_keep(self) -> None:
            calls.append("unlock_and_keep")
            self.locked = False

    class _FakeChatPanel:
        async def add_user_message(self, text: str, *, is_injection: bool = False, **_kwargs: object) -> None:
            calls.append(("add_user_message", text, is_injection))

    input_bar = _FakeInputBar()
    panel = _FakeChatPanel()

    def query_one(cls: type):
        if cls.__name__ == "InputBar":
            return input_bar
        if cls.__name__ == "ChatPanel":
            return panel
        raise AssertionError(f"unexpected query_one({cls})")

    return SimpleNamespace(
        query_one=query_one,
        _state=MainScreenState(run=RunState(agent_running=True)),
        _session_title=SimpleNamespace(set_terminal_title_for_user_message=lambda text: calls.append(("title", text))),
        _update_toc=lambda: None,
        _debug=lambda *_args: None,
    )


def test_stale_abandoned_injection_result_leaves_input_untouched() -> None:
    """An abandoned result for a cancelled id must not disturb re-editing."""
    calls: list[object] = []
    screen = _make_injection_outcome_screen(calls)
    handler = make_backend_handler(screen)
    # The user Esc-cancelled and requeued: a NEW injection is now pending.
    screen._state.pending_injection.begin("new-id", "edited text")

    asyncio.run(handler.on_injection_outcome(UserInjectResult(text="old text", consumed=False, injection_id="old-id")))

    assert calls == []
    assert screen._state.pending_injection.matches("new-id")


def test_stale_consumed_injection_result_renders_bubble_only() -> None:
    """A consumed result for a cancelled id shows the bubble but keeps the input."""
    calls: list[object] = []
    screen = _make_injection_outcome_screen(calls)
    handler = make_backend_handler(screen)
    # The user Esc-cancelled (pending cleared) before the consumed result landed.

    asyncio.run(
        handler.on_injection_outcome(UserInjectResult(text="delivered anyway", consumed=True, injection_id="old-id"))
    )

    assert calls == [("title", "delivered anyway"), ("add_user_message", "delivered anyway", True)]
    assert screen._state.pending_injection.active is False


def test_matching_abandoned_injection_result_unlocks_and_clears_pending() -> None:
    """The tracked injection's abandoned result restores the input for reuse."""
    calls: list[object] = []
    screen = _make_injection_outcome_screen(calls)
    handler = make_backend_handler(screen)
    screen._state.pending_injection.begin("inj-1", "queued text")

    asyncio.run(
        handler.on_injection_outcome(UserInjectResult(text="queued text", consumed=False, injection_id="inj-1"))
    )

    assert calls == ["unlock_and_keep"]
    assert screen._state.pending_injection.active is False


def test_matching_consumed_injection_result_unlocks_and_clears_pending() -> None:
    """The tracked injection's consumed result renders and clears the input."""
    calls: list[object] = []
    screen = _make_injection_outcome_screen(calls)
    handler = make_backend_handler(screen)
    screen._state.pending_injection.begin("inj-1", "queued text")

    asyncio.run(handler.on_injection_outcome(UserInjectResult(text="queued text", consumed=True, injection_id="inj-1")))

    assert calls == [("title", "queued text"), ("add_user_message", "queued text", True), "unlock_and_clear"]
    assert screen._state.pending_injection.active is False


def test_backend_handler_defers_agent_message_while_user_bubble_is_rendering() -> None:
    state = MainScreenState()
    state.render_gate.begin()

    def query_one(_cls: object) -> object:
        raise AssertionError("agent message should be deferred")

    screen = SimpleNamespace(
        _state=state,
        query_one=query_one,
    )
    handler = make_backend_handler(screen)
    event = InvocationMessage(text="fast", is_final=True, origin=InvocationOrigin("turn", "", "turn-test", None))

    asyncio.run(handler.on_agent_message(event))

    replayed: list[object] = []

    async def replay(deferred: object) -> None:
        replayed.append(deferred)

    asyncio.run(state.render_gate.drain(replay))
    assert replayed == [event]


def test_final_agent_message_keeps_gate_and_prestamps_terminal_absorb(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from textual import _time, events

    order: list[str] = []
    input_events: list[events.Key] = []

    class _GcMessages(list[object]):
        def append(self, message: object) -> None:
            order.append("gc")
            super().append(message)

    class _FakeStatusBar:
        def flash_completed(self) -> None:
            self.flash("Completed in 1s")

        def flash(self, _message: str) -> None:
            order.append("status")

    class _FakeChatPanel:
        async def add_agent_message(self, *_args: object, **_kwargs: object) -> None:
            assert handler._state.run.agent_running is True
            input_events.append(events.Key("x", "x"))
            order.append("render")

    status = _FakeStatusBar()
    panel = _FakeChatPanel()

    def query_one(cls: type) -> object:
        if cls.__name__ == "StatusBar":
            return status
        if cls.__name__ == "ChatPanel":
            return panel
        raise AssertionError(f"unexpected query_one({cls.__name__})")

    gc_messages = _GcMessages()
    screen = SimpleNamespace(
        _state=MainScreenState(run=RunState(agent_running=True)),
        _gc_messages=gc_messages,
        _session_title=fake_session_title(mark_terminal_title_completed=lambda: order.append("completed")),
        _set_agent_running=lambda _value: order.append("idle"),
        query_one=query_one,
        _debug=lambda *_args: None,
    )

    handler = make_backend_handler(screen)
    terminal_event = InvocationMessage(
        text="done", is_final=True, origin=InvocationOrigin("turn", "", "turn-test", None)
    )
    source_times = iter([10.0, 20.0])
    monkeypatch.setattr(_time, "get_time", lambda: next(source_times))
    asyncio.run(handler.on_agent_message(terminal_event))

    assert order == ["status", "render", "completed", "idle", "gc"]
    assert len(gc_messages) == 1
    assert isinstance(gc_messages[0], GcAbsorbRequested)
    assert gc_messages[0].reason is GcAbsorbReason.TURN_TERMINAL
    assert gc_messages[0].terminal_boundary is True
    assert gc_messages[0].time == 10.0
    assert input_events[0].time == 20.0


def test_final_agent_message_tells_when_its_turn_began() -> None:
    """The answer is timed from when the user sent the message or pressed Continue or Retry, as the backend keeps it."""
    began = datetime(2026, 8, 19, 1, 58, tzinfo=UTC)
    finished = began + timedelta(minutes=6)
    rendered: list[dict[str, object]] = []

    class _FakeStatusBar:
        def flash_completed(self) -> None:
            pass

    class _FakeChatPanel:
        async def add_agent_message(
            self,
            text: str,
            *,
            structured_output_completed: bool = False,
            created_at: datetime | None = None,
            turn_started_at: datetime | None = None,
        ) -> None:
            rendered.append({"text": text, "created_at": created_at, "turn_started_at": turn_started_at})

    status = _FakeStatusBar()
    panel = _FakeChatPanel()
    screen = SimpleNamespace(
        _state=MainScreenState(run=RunState(agent_running=True, started_at=began)),
        _gc_messages=[],
        _session_title=fake_session_title(),
        _set_agent_running=lambda _value: None,
        query_one=lambda cls: status if cls.__name__ == "StatusBar" else panel,
        _debug=lambda *_args: None,
    )
    handler = make_backend_handler(screen)

    asyncio.run(
        handler.on_agent_message(
            InvocationMessage(
                text="done", is_final=True, origin=InvocationOrigin("turn", "", "turn-test", None), timestamp=finished
            )
        )
    )

    assert rendered == [{"text": "done", "created_at": finished, "turn_started_at": began}]


def test_final_agent_message_render_failure_releases_turn_without_absorb() -> None:
    class _FakeStatusBar:
        def flash_completed(self) -> None:
            self.flash("Completed in 1s")

        def flash(self, _message: str) -> None:
            pass

    class _FailingChatPanel:
        async def add_agent_message(self, *_args: object, **_kwargs: object) -> None:
            assert handler._state.run.agent_running is True
            raise RuntimeError("render failed")

    status = _FakeStatusBar()
    panel = _FailingChatPanel()

    def query_one(cls: type) -> object:
        if cls.__name__ == "StatusBar":
            return status
        if cls.__name__ == "ChatPanel":
            return panel
        raise AssertionError(f"unexpected query_one({cls.__name__})")

    running: list[bool] = []
    gc_messages: list[object] = []
    screen = SimpleNamespace(
        _state=MainScreenState(run=RunState(agent_running=True)),
        _gc_messages=gc_messages,
        _session_title=fake_session_title(),
        _set_agent_running=running.append,
        query_one=query_one,
        _debug=lambda *_args: None,
    )
    handler = make_backend_handler(screen)

    with pytest.raises(RuntimeError, match="render failed"):
        asyncio.run(
            handler.on_agent_message(
                InvocationMessage(text="done", is_final=True, origin=InvocationOrigin("turn", "", "turn-test", None))
            )
        )

    assert running == [False]
    assert handler._state.run.agent_running is False
    assert gc_messages == []


def test_prior_run_terminal_message_cannot_stop_new_retry() -> None:
    started_at = datetime.now(UTC)
    state = MainScreenState()
    state.run.agent_running = True
    state.run.generation = 2
    state.run.started_at = started_at

    def query_one(_cls: object) -> object:
        raise AssertionError("stale terminal event must not touch the current retry UI")

    screen = SimpleNamespace(_state=state, query_one=query_one)
    handler = make_backend_handler(screen)

    asyncio.run(
        handler.on_agent_message(
            InvocationMessage(
                text="old final",
                is_final=True,
                timestamp=started_at - timedelta(milliseconds=1),
                origin=InvocationOrigin("turn", "", "turn-test", None),
            )
        )
    )

    assert state.run.agent_running is True
    assert state.run.generation == 2


async def test_terminal_render_completion_cannot_stop_successor_generation() -> None:
    render_started = asyncio.Event()
    release_render = asyncio.Event()
    running: list[bool] = []
    gc_messages: list[object] = []
    state = MainScreenState()
    state.run.agent_running = True
    state.run.generation = 1
    state.run.started_at = datetime.now(UTC)

    class _Status:
        def flash_completed(self) -> None:
            self.flash("Completed in 1s")

        def flash(self, _message: str) -> None:
            return

    class _Panel:
        async def add_agent_message(self, *_args: object, **_kwargs: object) -> None:
            render_started.set()
            await release_render.wait()

    def query_one(cls: type) -> object:
        if cls.__name__ == "StatusBar":
            return _Status()
        if cls.__name__ == "ChatPanel":
            return _Panel()
        raise AssertionError(f"unexpected query_one({cls.__name__})")

    completed: list[None] = []
    screen = SimpleNamespace(
        _state=state,
        _gc_messages=gc_messages,
        _session_title=fake_session_title(mark_terminal_title_completed=lambda: completed.append(None)),
        _set_agent_running=running.append,
        query_one=query_one,
        _debug=lambda *_args: None,
    )
    handler = make_backend_handler(screen)
    terminal = asyncio.create_task(
        handler.on_agent_message(
            InvocationMessage(
                text="old final",
                is_final=True,
                timestamp=state.run.started_at + timedelta(milliseconds=1),
                origin=InvocationOrigin("turn", "", "turn-test", None),
            )
        )
    )
    await render_started.wait()
    state.run.generation = 2
    state.run.agent_running = True
    release_render.set()
    await terminal

    assert state.run.agent_running is True
    assert state.run.generation == 2
    assert running == []
    assert completed == []
    assert gc_messages == []


# ---------------------------------------------------------------------------
# Todo list (Tasks panel) wiring
# ---------------------------------------------------------------------------


def test_todo_list_updated_sets_todo_state_and_debug_logs() -> None:
    """TodoListUpdated routes the full list into screen.todo_state."""

    debug_calls: list[tuple[str, str]] = []
    screen = SimpleNamespace(
        _debug=lambda key, message="": debug_calls.append((key, message)),
    )
    handler = make_backend_handler(screen)
    items = [
        TodoItem(content="write tests", status="completed"),
        TodoItem(content="run suite", status="in_progress", active_form="Running suite"),
        TodoItem(content="ship it"),
    ]

    asyncio.run(handler.on_todo_list_updated(TodoListUpdated(items=items, session_id="session-1")))

    assert screen.todo_state == TodoListState(items=tuple(items))
    assert ("TodoListUpdated", "1/3 done") in debug_calls
