# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tests for RollbackController: picker projection fencing, dispatch guards and results."""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from types import SimpleNamespace

import pytest
from rich.text import Text

from chrys.app.tui.screens.diff import RollbackProgressModal
from chrys.app.tui.screens.diff.rollback_modal import RollbackLoadCancelled
from chrys.app.tui.screens.main.ports import RollbackView
from chrys.app.tui.screens.main.rollback_controller import RollbackController
from chrys.app.tui.screens.main.state import MainScreenServices, MainScreenState
from chrys.app.tui.screens.main.view_adapter import MainScreenViewAdapter
from chrys.app.tui.support.gc_freeze import (
    GcAbsorbReason,
    GcReclaimReason,
    GcReclaimRequested,
)
from chrys.app.tui.widgets.sidebar.context import ContextUsageState
from chrys.app.tui.widgets.sidebar.tasks import TodoListState
from chrys.foundation.events.bus import EventBus
from chrys.foundation.events.types import (
    RollbackResult,
    UserRollback,
    WorkspaceUpdated,
)
from chrys.foundation.models.todos import TodoItem
from tests.support.tui_helpers import (
    ScreenSetters,
    fake_session_title,
    main_screen_parts,
    main_screen_state_at,
    make_session_handler,
)


class _RollbackProjectionFenceMixin:
    """Minimal backend projection-fence surface for rollback controller tests."""

    rollback_projection_fenced = False
    build_generation = 1
    workspace_primary_cwd = "/repo/workspace"

    async def begin_rollback_projection(
        self,
        *,
        session_id: str | None,
        session_generation: int,
    ) -> str:
        assert session_id == "session-1"
        assert session_generation in {1, 7}
        self.rollback_projection_fenced = True
        return "test-rollback-projection"

    def finish_rollback_projection(self, owner: str) -> None:
        assert owner == "test-rollback-projection"
        assert self.rollback_projection_fenced
        self.rollback_projection_fenced = False


def _adapter_for(screen: SimpleNamespace) -> MainScreenViewAdapter:
    return MainScreenViewAdapter(screen, state=MainScreenState())  # type: ignore[arg-type]


def _make_rollback_controller(
    *,
    view: RollbackView,
    bus: EventBus | None = None,
    engine_provider: Callable[[], object] = lambda: None,
    workspace_cwd: Callable[[], str] = lambda: "/repo/workspace",
    is_agent_busy: Callable[[], bool] = lambda: False,
    current_session_id: Callable[[], str] = lambda: "session-1",
    session_generation: Callable[[], int] = lambda: 1,
    turn_lifecycle_task: Callable[[], asyncio.Task[None] | None] = lambda: None,
    profile_name: Callable[[], str] = lambda: "Code",
    reset_welcome_workspace_marker: Callable[[str], None] = lambda _cwd: None,
    set_has_messages: Callable[[bool], None] = lambda _value: None,
    post_gc_message: Callable[[GcReclaimRequested], object] = lambda _message: None,
    debug: Callable[[str, str], None] = lambda *_args: None,
    invalidate_file_index: Callable[[], None] = lambda: None,
    confirm_stopping_background_tasks: Callable[[int], Awaitable[bool]] | None = None,
) -> RollbackController:
    """Build a RollbackController whose collaborators are inert unless overridden."""
    return RollbackController(
        services=MainScreenServices(bus=bus if bus is not None else EventBus(), engine_provider=engine_provider),
        view=view,
        workspace_cwd=workspace_cwd,
        is_agent_busy=is_agent_busy,
        current_session_id=current_session_id,
        session_generation=session_generation,
        turn_lifecycle_task=turn_lifecycle_task,
        profile_name=profile_name,
        reset_welcome_workspace_marker=reset_welcome_workspace_marker,
        set_has_messages=set_has_messages,
        post_gc_message=post_gc_message,
        debug=debug,
        invalidate_file_index=invalidate_file_index,
        confirm_stopping_background_tasks=confirm_stopping_background_tasks,
    )


def _make_rollback_controller_for_test(screen: SimpleNamespace) -> RollbackController:
    """Wire a RollbackController to the fake's ``_state`` and ``_services``, as MainScreen wires its own.

    The session id, generation and turn-lifecycle task stay inert defaults.
    """
    state, services, _live_diff = main_screen_parts(screen)
    setters = ScreenSetters(screen, state, services)

    def _reset_welcome_workspace_marker(cwd: str) -> None:
        state.workspace_marker.original_cwd = None
        setters.set_workspace_cwd(cwd)

    def _post_gc_message(message: object) -> None:
        messages = getattr(screen, "_gc_messages", None)
        if isinstance(messages, list):
            messages.append(message)

    return RollbackController(
        services=services,
        view=MainScreenViewAdapter(screen, state=state, state_store=services.state_store),  # type: ignore[arg-type]
        workspace_cwd=lambda: state.workspace_marker.current_cwd,
        is_agent_busy=lambda: state.run.agent_running or state.run.agent_loading or services.execution_busy(),
        current_session_id=lambda: "session-1",
        session_generation=lambda: 1,
        turn_lifecycle_task=lambda: None,
        profile_name=lambda: state.runtime.profile,
        reset_welcome_workspace_marker=_reset_welcome_workspace_marker,
        set_has_messages=setters.set_has_messages,
        post_gc_message=_post_gc_message,
        debug=screen._debug,
        invalidate_file_index=lambda: None,
    )


def test_show_rollback_uses_workspace_cwd(monkeypatch) -> None:
    import chrys.app.tui.screens.diff as diff_pkg

    seen_cwds: list[str] = []
    state_loaders: list[object] = []
    state_reads: list[str] = []
    pushed: list[object] = []

    class _FakeRollbackModal:
        def __init__(self, *, cwd: str, **kwargs: object) -> None:
            seen_cwds.append(cwd)
            state_loaders.append(kwargs["load_state"])

    class _Coordinator:
        def __init__(self, marker: str) -> None:
            self.marker = marker

        def augment_rollback_plan(self, _tracker, _plan, **_kwargs: object) -> str:
            return self.marker

    class _FakeEngine(_RollbackProjectionFenceMixin):
        mutation_tracker = object()
        mutation_coordinator = _Coordinator("old")
        current_turn_number = 3
        conversation_revision = 7

        async def begin_rollback_projection(
            self,
            *,
            session_id: str | None,
            session_generation: int,
        ) -> str:
            # Model a queued workspace rebuild winning the shared gate before
            # the picker projection acquires it.
            self.build_generation = 2
            self.workspace_primary_cwd = "/repo/rebuilt-workspace"
            self.mutation_coordinator = _Coordinator("rebuilt")
            return await super().begin_rollback_projection(
                session_id=session_id,
                session_generation=session_generation,
            )

        def available_rollback_turns(self) -> list[int]:
            state_reads.append("targets")
            return [1, 2]

        def turn_prompt_previews(self) -> dict[int, str]:
            state_reads.append("prompts")
            return {}

        def execution_busy(self) -> bool:
            return False

    monkeypatch.setattr(diff_pkg, "RollbackModal", _FakeRollbackModal)
    engine = _FakeEngine()
    screen = SimpleNamespace(
        _state=main_screen_state_at("/repo/workspace"),
        _services=MainScreenServices(bus=EventBus(), engine_provider=lambda: engine),
        _set_has_messages=lambda _value: None,
        _debug=lambda *_args: None,
        app=SimpleNamespace(push_screen=lambda modal, _callback=None: pushed.append(modal)),
        notify=lambda *_args, **_kwargs: None,
    )

    _make_rollback_controller_for_test(screen).show_rollback()

    assert seen_cwds == ["/repo/workspace"]
    assert len(pushed) == 1
    # The screen is pushed before snapshot payloads or history are read.
    assert state_reads == []
    assert len(state_loaders) == 1
    state = asyncio.run(state_loaders[0]())
    assert state is not None
    assert state_reads == ["targets", "prompts"]
    assert state.build_generation == 2
    assert state.workspace_cwd == "/repo/rebuilt-workspace"
    assert state.plan_augment is not None
    assert state.plan_augment(object(), state.workspace_cwd) == "rebuilt"


@pytest.mark.parametrize(
    ("arg", "expected_target", "expected_relative"),
    (("1", 0, 1), ("to 1", 1, None)),
)
def test_rollback_commands_preserve_relative_and_absolute_selectors(
    arg: str,
    expected_target: int,
    expected_relative: int | None,
) -> None:
    published: list[UserRollback] = []
    pushed: list[object] = []
    bus = EventBus()

    async def record_rollback(event: UserRollback) -> None:
        published.append(event)

    asyncio.run(bus.subscribe(UserRollback, record_rollback))

    class _FakeEngine(_RollbackProjectionFenceMixin):
        mutation_tracker = object()
        mutation_coordinator = None
        current_turn_number = 3
        conversation_revision = 7

        def available_rollback_turns(self) -> list[int]:
            return [0, 1, 2]

        def turn_prompt_previews(self) -> dict[int, str]:
            return {}

    screen = SimpleNamespace(
        app=SimpleNamespace(push_screen=lambda modal, _callback=None: pushed.append(modal)),
        notify=lambda *_args, **_kwargs: None,
    )
    controller = _make_rollback_controller(view=_adapter_for(screen), bus=bus, engine_provider=_FakeEngine)

    controller.show_rollback(arg)

    assert len(pushed) == 1
    progress = pushed[0]
    assert isinstance(progress, RollbackProgressModal)
    assert published == []
    asyncio.run(progress._operation())
    assert len(published) == 1
    assert published[0].target_turn == expected_target
    assert published[0].relative_turns == expected_relative
    assert published[0].revert_changes is True
    assert published[0].session_id == "session-1"


def test_rollback_direct_command_normalizes_empty_chat_session_id() -> None:
    published: list[UserRollback] = []
    pushed: list[object] = []
    bus = EventBus()

    async def record_rollback(event: UserRollback) -> None:
        published.append(event)

    asyncio.run(bus.subscribe(UserRollback, record_rollback))
    screen = SimpleNamespace(
        app=SimpleNamespace(push_screen=lambda modal, _callback=None: pushed.append(modal)),
        notify=lambda *_args, **_kwargs: None,
    )
    controller = _make_rollback_controller(
        view=_adapter_for(screen),
        bus=bus,
        engine_provider=lambda: SimpleNamespace(current_turn_number=1),
        current_session_id=str,
    )

    controller.show_rollback("1")
    progress = pushed[0]
    assert isinstance(progress, RollbackProgressModal)
    asyncio.run(progress._operation())

    assert len(published) == 1
    assert published[0].session_id is None


async def test_rollback_direct_command_delegates_fencing_and_target_resolution_to_backend() -> None:
    published: list[UserRollback] = []
    pushed: list[object] = []
    target_reads: list[str] = []
    lifecycle_release = asyncio.Event()
    replacement_release = asyncio.Event()
    bus = EventBus()

    async def record_rollback(event: UserRollback) -> None:
        published.append(event)

    async def wait_for_release(release: asyncio.Event) -> None:
        await release.wait()

    await bus.subscribe(UserRollback, record_rollback)
    captured_task = asyncio.create_task(wait_for_release(lifecycle_release))
    replacement_task = asyncio.create_task(wait_for_release(replacement_release))
    lifecycle_ref = [captured_task]

    class _FakeEngine(_RollbackProjectionFenceMixin):
        mutation_tracker = object()
        mutation_coordinator = None
        current_turn_number = 3
        conversation_revision = 7

        def available_rollback_turns(self) -> list[int]:
            target_reads.append("targets")
            return [0, 1, 2]

        def turn_prompt_previews(self) -> dict[int, str]:
            return {}

    screen = SimpleNamespace(
        app=SimpleNamespace(push_screen=lambda modal, _callback=None: pushed.append(modal)),
        notify=lambda *_args, **_kwargs: None,
    )
    controller = _make_rollback_controller(
        view=_adapter_for(screen), bus=bus, engine_provider=_FakeEngine, turn_lifecycle_task=lambda: lifecycle_ref[0]
    )

    try:
        controller.show_rollback("1")
        progress = pushed[0]
        assert isinstance(progress, RollbackProgressModal)

        # Direct commands do not scan snapshots or await lifecycle state in
        # the frontend. The backend event handler owns both operations under
        # its transition fence.
        lifecycle_ref[0] = replacement_task
        await progress._operation()
        assert target_reads == []
        assert len(published) == 1
        assert published[0].target_turn == 0
        assert published[0].relative_turns == 1
        assert not captured_task.done()
        assert not replacement_task.done()
    finally:
        lifecycle_release.set()
        await asyncio.gather(captured_task, return_exceptions=True)
        replacement_task.cancel()
        await asyncio.gather(replacement_task, return_exceptions=True)


@pytest.mark.parametrize("use_picker", [False, True])
async def test_rollback_publication_propagates_handler_failure_to_progress_modal(
    monkeypatch: pytest.MonkeyPatch,
    use_picker: bool,
) -> None:
    import chrys.app.tui.screens.diff as diff_pkg

    pushed: list[object] = []
    callbacks: list[object] = []
    state_loaders: list[object] = []
    handled: list[UserRollback] = []
    bus = EventBus()

    async def fail_rollback(event: UserRollback) -> None:
        handled.append(event)
        raise RuntimeError("rollback handler failed")

    await bus.subscribe(UserRollback, fail_rollback)

    class _FakeRollbackModal:
        def __init__(self, **kwargs: object) -> None:
            state_loaders.append(kwargs["load_state"])

    class _FakeEngine(_RollbackProjectionFenceMixin):
        mutation_tracker = object()
        mutation_coordinator = None
        current_turn_number = 3
        conversation_revision = 7

        def available_rollback_turns(self) -> list[int]:
            return [0, 1, 2]

        def turn_prompt_previews(self) -> dict[int, str]:
            return {}

    def push_screen(modal: object, callback: object | None = None) -> None:
        pushed.append(modal)
        if callback is not None:
            callbacks.append(callback)

    monkeypatch.setattr(diff_pkg, "RollbackModal", _FakeRollbackModal)
    screen = SimpleNamespace(
        app=SimpleNamespace(push_screen=push_screen),
        notify=lambda *_args, **_kwargs: None,
    )
    controller = _make_rollback_controller(view=_adapter_for(screen), bus=bus, engine_provider=_FakeEngine)

    controller.show_rollback("" if use_picker else "1")
    if use_picker:
        state = await state_loaders[0]()  # type: ignore[operator]
        assert state is not None
        callbacks[0]((2, False, None))  # type: ignore[operator]

    progress = pushed[-1]
    assert isinstance(progress, RollbackProgressModal)
    with pytest.raises(RuntimeError, match="rollback handler failed"):
        await progress._operation()
    assert len(handled) == 1


@pytest.mark.parametrize("confirmed", [False, True])
@pytest.mark.parametrize("use_picker", [False, True])
async def test_rollback_asks_before_stopping_the_background_tasks_of_later_turns(
    monkeypatch: pytest.MonkeyPatch,
    use_picker: bool,
    confirmed: bool,
) -> None:
    import chrys.app.tui.screens.diff as diff_pkg

    pushed: list[object] = []
    callbacks: list[object] = []
    state_loaders: list[object] = []
    published: list[UserRollback] = []
    asked: list[int] = []
    bus = EventBus()

    async def record_rollback(event: UserRollback) -> None:
        published.append(event)

    async def confirm(target_turn: int) -> bool:
        asked.append(target_turn)
        return confirmed

    await bus.subscribe(UserRollback, record_rollback)

    class _FakeRollbackModal:
        def __init__(self, **kwargs: object) -> None:
            state_loaders.append(kwargs["load_state"])

    class _FakeEngine(_RollbackProjectionFenceMixin):
        mutation_tracker = object()
        mutation_coordinator = None
        current_turn_number = 3
        conversation_revision = 7

        def available_rollback_turns(self) -> list[int]:
            return [0, 1, 2]

        def turn_prompt_previews(self) -> dict[int, str]:
            return {}

    def push_screen(modal: object, callback: object | None = None) -> None:
        pushed.append(modal)
        if callback is not None:
            callbacks.append(callback)

    monkeypatch.setattr(diff_pkg, "RollbackModal", _FakeRollbackModal)
    screen = SimpleNamespace(app=SimpleNamespace(push_screen=push_screen), notify=lambda *_args, **_kwargs: None)
    controller = _make_rollback_controller(
        view=_adapter_for(screen), bus=bus, engine_provider=_FakeEngine, confirm_stopping_background_tasks=confirm
    )

    controller.show_rollback("" if use_picker else "2")
    if use_picker:
        assert await state_loaders[0]() is not None  # type: ignore[operator]
        callbacks[0]((1, False, None))  # type: ignore[operator]

    progress = pushed[-1]
    assert isinstance(progress, RollbackProgressModal)
    await progress._operation()
    # Both selectors land on turn 1: the tasks turns 2 and 3 started would stop.
    assert asked == [1]
    assert len(published) == (1 if confirmed else 0)


async def test_rollback_picker_loader_revalidates_owner_after_lifecycle_wait(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import chrys.app.tui.screens.diff as diff_pkg

    pushed: list[object] = []
    state_loaders: list[object] = []
    notifications: list[str] = []
    target_reads: list[str] = []
    session_generation_ref = [7]
    lifecycle_release = asyncio.Event()
    bus = EventBus()

    async def wait_for_release() -> None:
        await lifecycle_release.wait()

    lifecycle_task = asyncio.create_task(wait_for_release())

    class _FakeRollbackModal:
        def __init__(self, **kwargs: object) -> None:
            state_loaders.append(kwargs["load_state"])

    class _FakeEngine:
        mutation_tracker = object()
        mutation_coordinator = None
        current_turn_number = 3

        def available_rollback_turns(self) -> list[int]:
            target_reads.append("targets")
            return [0, 1, 2]

        def turn_prompt_previews(self) -> dict[int, str]:
            return {}

    monkeypatch.setattr(diff_pkg, "RollbackModal", _FakeRollbackModal)
    screen = SimpleNamespace(
        notify=lambda message, **_kwargs: notifications.append(message),
        app=SimpleNamespace(push_screen=lambda modal, _callback=None: pushed.append(modal)),
    )
    controller = _make_rollback_controller(
        view=_adapter_for(screen),
        bus=bus,
        engine_provider=_FakeEngine,
        session_generation=lambda: session_generation_ref[0],
        turn_lifecycle_task=lambda: lifecycle_task,
    )

    controller.show_rollback()
    assert len(state_loaders) == 1
    operation = asyncio.create_task(state_loaders[0]())  # type: ignore[operator]
    await asyncio.sleep(0)
    assert target_reads == []

    session_generation_ref[0] += 1
    lifecycle_release.set()
    with pytest.raises(RollbackLoadCancelled):
        await asyncio.wait_for(operation, timeout=1)

    assert target_reads == []
    assert notifications == ["Rollback cancelled because the active session changed."]


async def test_rollback_picker_loader_waits_for_exact_captured_lifecycle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import chrys.app.tui.screens.diff as diff_pkg

    pushed: list[object] = []
    state_loaders: list[object] = []
    state_reads: list[str] = []
    captured_release = asyncio.Event()

    async def wait_for_release(release: asyncio.Event) -> None:
        await release.wait()

    captured_task = asyncio.create_task(wait_for_release(captured_release))
    lifecycle_ref: list[asyncio.Task[None] | None] = [captured_task]

    class _FakeRollbackModal:
        def __init__(self, **kwargs: object) -> None:
            state_loaders.append(kwargs["load_state"])

    class _FakeEngine(_RollbackProjectionFenceMixin):
        mutation_tracker = object()
        mutation_coordinator = None
        current_turn_number = 3
        conversation_revision = 7

        def available_rollback_turns(self) -> list[int]:
            state_reads.append("targets")
            return [0, 1, 2]

        def turn_prompt_previews(self) -> dict[int, str]:
            state_reads.append("prompts")
            return {}

    monkeypatch.setattr(diff_pkg, "RollbackModal", _FakeRollbackModal)
    screen = SimpleNamespace(
        notify=lambda *_args, **_kwargs: None,
        app=SimpleNamespace(push_screen=lambda modal, _callback=None: pushed.append(modal)),
    )
    controller = _make_rollback_controller(
        view=_adapter_for(screen),
        engine_provider=_FakeEngine,
        session_generation=lambda: 7,
        turn_lifecycle_task=lambda: lifecycle_ref[0],
    )

    controller.show_rollback()
    assert len(state_loaders) == 1
    # The engine may clear its visible lifecycle slot during final cleanup;
    # the loader must still await the exact task captured when the modal opened.
    lifecycle_ref[0] = None
    operation = asyncio.create_task(state_loaders[0]())  # type: ignore[operator]
    await asyncio.sleep(0)
    assert state_reads == []

    captured_release.set()
    state = await asyncio.wait_for(operation, timeout=1)
    assert state is not None
    assert state_reads == ["targets", "prompts"]


async def test_rollback_picker_loader_discards_projection_if_run_revision_changes_during_scan(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import chrys.app.tui.screens.diff as diff_pkg

    state_loaders: list[object] = []
    notifications: list[str] = []
    conversation_revision = [7]
    scan_started = threading.Event()
    release_scan = threading.Event()

    class _FakeRollbackModal:
        def __init__(self, **kwargs: object) -> None:
            state_loaders.append(kwargs["load_state"])

    class _FakeEngine(_RollbackProjectionFenceMixin):
        mutation_tracker = object()
        mutation_coordinator = None
        current_turn_number = 3

        @property
        def conversation_revision(self) -> int:
            return conversation_revision[0]

        def available_rollback_turns(self) -> list[int]:
            assert self.rollback_projection_fenced
            scan_started.set()
            assert release_scan.wait(timeout=1)
            return [0, 1, 2]

        def turn_prompt_previews(self) -> dict[int, str]:
            return {}

    monkeypatch.setattr(diff_pkg, "RollbackModal", _FakeRollbackModal)
    screen = SimpleNamespace(
        notify=lambda message, **_kwargs: notifications.append(message),
        app=SimpleNamespace(push_screen=lambda *_args, **_kwargs: None),
    )
    controller = _make_rollback_controller(
        view=_adapter_for(screen), engine_provider=_FakeEngine, session_generation=lambda: 7
    )

    controller.show_rollback()
    operation = asyncio.create_task(state_loaders[0]())  # type: ignore[operator]
    assert await asyncio.to_thread(scan_started.wait, 1)
    conversation_revision[0] = 8
    release_scan.set()

    with pytest.raises(RollbackLoadCancelled):
        await asyncio.wait_for(operation, timeout=1)
    assert notifications == ["Rollback picker cancelled because the conversation changed."]


async def test_rollback_picker_loader_keeps_projection_fenced_until_cancelled_scan_quiesces(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import chrys.app.tui.screens.diff as diff_pkg

    state_loaders: list[object] = []
    scan_started = threading.Event()
    release_scan = threading.Event()
    projection_released = asyncio.Event()

    class _FakeRollbackModal:
        def __init__(self, **kwargs: object) -> None:
            state_loaders.append(kwargs["load_state"])

    class _FakeEngine(_RollbackProjectionFenceMixin):
        mutation_tracker = object()
        mutation_coordinator = None
        current_turn_number = 3
        conversation_revision = 7

        def available_rollback_turns(self) -> list[int]:
            assert self.rollback_projection_fenced
            scan_started.set()
            assert release_scan.wait(timeout=2)
            return [0, 1, 2]

        def turn_prompt_previews(self) -> dict[int, str]:
            assert self.rollback_projection_fenced
            return {}

        def finish_rollback_projection(self, owner: str) -> None:
            super().finish_rollback_projection(owner)
            projection_released.set()

    engine = _FakeEngine()
    monkeypatch.setattr(diff_pkg, "RollbackModal", _FakeRollbackModal)
    screen = SimpleNamespace(
        notify=lambda *_args, **_kwargs: None,
        app=SimpleNamespace(push_screen=lambda *_args, **_kwargs: None),
    )
    controller = _make_rollback_controller(
        view=_adapter_for(screen), engine_provider=lambda: engine, session_generation=lambda: 7
    )

    controller.show_rollback()
    operation = asyncio.create_task(state_loaders[0]())  # type: ignore[operator]
    assert await asyncio.to_thread(scan_started.wait, 1)

    operation.cancel()
    with pytest.raises(asyncio.CancelledError):
        await operation
    assert engine.rollback_projection_fenced is True
    assert projection_released.is_set() is False

    release_scan.set()
    await asyncio.wait_for(projection_released.wait(), timeout=1)
    assert engine.rollback_projection_fenced is False


@pytest.mark.parametrize("drift", ["build", "workspace"])
async def test_rollback_picker_preview_rejects_runtime_rebuild_drift(
    monkeypatch: pytest.MonkeyPatch,
    drift: str,
) -> None:
    import chrys.app.tui.screens.diff as diff_pkg

    state_loaders: list[object] = []
    notifications: list[str] = []

    class _FakeRollbackModal:
        def __init__(self, **kwargs: object) -> None:
            state_loaders.append(kwargs["load_state"])

    class _FakeEngine(_RollbackProjectionFenceMixin):
        mutation_tracker = object()
        mutation_coordinator = None
        current_turn_number = 3
        conversation_revision = 7

        def available_rollback_turns(self) -> list[int]:
            return [0, 1, 2]

        def turn_prompt_previews(self) -> dict[int, str]:
            return {}

    engine = _FakeEngine()
    monkeypatch.setattr(diff_pkg, "RollbackModal", _FakeRollbackModal)
    screen = SimpleNamespace(
        notify=lambda message, **_kwargs: notifications.append(message),
        app=SimpleNamespace(push_screen=lambda *_args, **_kwargs: None),
    )
    controller = _make_rollback_controller(
        view=_adapter_for(screen), engine_provider=lambda: engine, session_generation=lambda: 7
    )

    controller.show_rollback()
    state = await state_loaders[0]()  # type: ignore[operator]
    assert state is not None
    if drift == "build":
        engine.build_generation += 1
    else:
        engine.workspace_primary_cwd = "/repo/rebuilt-workspace"

    assert state.projection_acquire is not None
    with pytest.raises(RollbackLoadCancelled):
        await state.projection_acquire()

    assert engine.rollback_projection_fenced is False
    assert notifications == ["Rollback picker cancelled because the workspace or runtime changed."]


def test_rollback_command_rejects_removed_revert_suffix() -> None:
    pushed: list[object] = []
    notifications: list[str] = []

    class _FakeEngine:
        current_turn_number = 3

    screen = SimpleNamespace(
        app=SimpleNamespace(push_screen=lambda modal, _callback=None: pushed.append(modal)),
        notify=lambda message, **_kwargs: notifications.append(message),
    )
    controller = _make_rollback_controller(view=_adapter_for(screen), engine_provider=_FakeEngine)

    controller.show_rollback("1 revert")

    assert pushed == []
    assert notifications == ["Usage: /rollback, /rollback N, or /rollback to N."]


def test_rollback_confirmation_binds_picker_projection_before_default_file_revert(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import chrys.app.tui.screens.diff as diff_pkg

    published: list[UserRollback] = []
    pushed: list[object] = []
    modal_callbacks: list[object] = []
    state_loaders: list[object] = []
    conversation_revision = [7]
    bus = EventBus()

    async def record_rollback(event: UserRollback) -> None:
        published.append(event)

    asyncio.run(bus.subscribe(UserRollback, record_rollback))

    class _FakeRollbackModal:
        def __init__(self, **kwargs: object) -> None:
            state_loaders.append(kwargs["load_state"])

    class _FakeEngine(_RollbackProjectionFenceMixin):
        mutation_tracker = object()
        mutation_coordinator = None
        current_turn_number = 3

        @property
        def conversation_revision(self) -> int:
            return conversation_revision[0]

        def available_rollback_turns(self) -> list[int]:
            return [0, 1, 2]

        def turn_prompt_previews(self) -> dict[int, str]:
            return {}

    def push_screen(modal: object, callback: object | None = None) -> None:
        pushed.append(modal)
        if callback is not None:
            modal_callbacks.append(callback)

    monkeypatch.setattr(diff_pkg, "RollbackModal", _FakeRollbackModal)
    screen = SimpleNamespace(
        app=SimpleNamespace(push_screen=push_screen),
        notify=lambda *_args, **_kwargs: None,
    )
    controller = _make_rollback_controller(view=_adapter_for(screen), bus=bus, engine_provider=_FakeEngine)

    controller.show_rollback()

    assert len(pushed) == 1
    assert len(modal_callbacks) == 1
    state = asyncio.run(state_loaders[0]())  # type: ignore[operator]
    assert state is not None
    conversation_revision[0] = 8
    modal_callbacks[0]((2, True, ["/repo/workspace/a.py"]))  # type: ignore[operator]

    assert published == []
    assert len(pushed) == 2
    progress = pushed[1]
    assert isinstance(progress, RollbackProgressModal)
    asyncio.run(progress._operation())
    assert len(published) == 1
    assert published[0].target_turn == 2
    assert published[0].expected_current_turn == 3
    assert published[0].expected_conversation_revision == 7
    assert published[0].expected_build_generation == 1
    assert published[0].expected_workspace_cwd == "/repo/workspace"
    assert published[0].revert_changes is True
    assert published[0].selected_paths == ["/repo/workspace/a.py"]
    assert published[0].session_id == "session-1"


@pytest.mark.parametrize("owner_changed", [False, True])
async def test_rollback_picker_confirmation_rechecks_owner_after_projection(
    monkeypatch: pytest.MonkeyPatch,
    owner_changed: bool,
) -> None:
    import chrys.app.tui.screens.diff as diff_pkg

    published: list[UserRollback] = []
    pushed: list[object] = []
    modal_callbacks: list[object] = []
    state_loaders: list[object] = []
    notifications: list[str] = []
    session_generation_ref = [1]
    lifecycle_release = asyncio.Event()
    bus = EventBus()

    async def record_rollback(event: UserRollback) -> None:
        published.append(event)

    async def wait_for_release() -> None:
        await lifecycle_release.wait()

    await bus.subscribe(UserRollback, record_rollback)
    lifecycle_task = asyncio.create_task(wait_for_release())

    class _FakeRollbackModal:
        def __init__(self, **kwargs: object) -> None:
            state_loaders.append(kwargs["load_state"])

    class _FakeEngine(_RollbackProjectionFenceMixin):
        mutation_tracker = object()
        mutation_coordinator = None
        current_turn_number = 3
        conversation_revision = 7

        def available_rollback_turns(self) -> list[int]:
            return [0, 1, 2]

        def turn_prompt_previews(self) -> dict[int, str]:
            return {}

    def push_screen(modal: object, callback: object | None = None) -> None:
        pushed.append(modal)
        if callback is not None:
            modal_callbacks.append(callback)

    monkeypatch.setattr(diff_pkg, "RollbackModal", _FakeRollbackModal)
    screen = SimpleNamespace(
        app=SimpleNamespace(push_screen=push_screen),
        notify=lambda message, **_kwargs: notifications.append(message),
    )
    controller = _make_rollback_controller(
        view=_adapter_for(screen),
        bus=bus,
        engine_provider=_FakeEngine,
        session_generation=lambda: session_generation_ref[0],
        turn_lifecycle_task=lambda: lifecycle_task,
    )

    controller.show_rollback()
    lifecycle_release.set()
    state = await state_loaders[0]()  # type: ignore[operator]
    assert state is not None
    modal_callbacks[0]((2, False, None))  # type: ignore[operator]
    progress = pushed[1]
    assert isinstance(progress, RollbackProgressModal)

    if owner_changed:
        session_generation_ref[0] += 1
    await asyncio.wait_for(progress._operation(), timeout=1)
    if owner_changed:
        assert published == []
        assert notifications == ["Rollback cancelled because the active session changed."]
    else:
        assert len(published) == 1
        assert published[0].target_turn == 2
        assert published[0].expected_current_turn == 3
        assert published[0].expected_conversation_revision == 7
        assert published[0].expected_build_generation == 1
        assert published[0].expected_workspace_cwd == "/repo/workspace"


@dataclass
class _DispatchOwnership:
    """Owner identity the rollback dispatch guard re-reads before publishing."""

    session_id: str = "session-1"
    session_generation: int = 7
    agent_busy: bool = False


def _switch_to_another_session(ownership: _DispatchOwnership) -> None:
    ownership.session_id = "session-2"


def _switch_away_and_back(ownership: _DispatchOwnership) -> None:
    # The visible ID matches again, but the monotonic engine generation
    # still proves this is a different owner.
    ownership.session_generation = 8


def _start_the_agent(ownership: _DispatchOwnership) -> None:
    ownership.agent_busy = True


SESSION_CHANGED_NOTIFICATION = "Rollback cancelled because the active session changed."
AGENT_BUSY_NOTIFICATION = "Cannot roll back while the agent is running."


@pytest.mark.parametrize(
    ("invalidate", "expected_notification"),
    [
        pytest.param(_switch_to_another_session, SESSION_CHANGED_NOTIFICATION, id="session-id-changed"),
        pytest.param(_switch_away_and_back, SESSION_CHANGED_NOTIFICATION, id="session-generation-changed"),
        pytest.param(_start_the_agent, AGENT_BUSY_NOTIFICATION, id="agent-became-busy"),
    ],
)
def test_rollback_direct_command_cancels_when_ownership_changes_before_dispatch(
    invalidate: Callable[[_DispatchOwnership], None],
    expected_notification: str,
) -> None:
    pushed: list[object] = []
    notifications: list[str] = []
    published: list[UserRollback] = []
    ownership = _DispatchOwnership()
    bus = EventBus()

    async def record_rollback(event: UserRollback) -> None:
        published.append(event)

    asyncio.run(bus.subscribe(UserRollback, record_rollback))

    class _FakeEngine:
        mutation_tracker = object()
        mutation_coordinator = None
        current_turn_number = 3

        def available_rollback_turns(self) -> list[int]:
            return [0, 1, 2]

        def turn_prompt_previews(self) -> dict[int, str]:
            return {}

    screen = SimpleNamespace(
        notify=lambda message, **_kwargs: notifications.append(message),
        app=SimpleNamespace(push_screen=lambda modal, _callback=None: pushed.append(modal)),
    )
    controller = _make_rollback_controller(
        view=_adapter_for(screen),
        bus=bus,
        engine_provider=_FakeEngine,
        is_agent_busy=lambda: ownership.agent_busy,
        current_session_id=lambda: ownership.session_id,
        session_generation=lambda: ownership.session_generation,
    )

    controller.show_rollback("1")
    assert len(pushed) == 1

    invalidate(ownership)
    progress = pushed[0]
    assert isinstance(progress, RollbackProgressModal)
    asyncio.run(progress._operation())

    assert published == []
    assert notifications == [expected_notification]


def test_rollback_result_restores_rolled_back_user_text() -> None:
    notifications: list[tuple[str, dict]] = []
    restored: list[str] = []

    class _RollbackView:
        def notify(
            self,
            message: str,
            *,
            title: str,
            severity: str = "information",
            timeout: float | None = 3,
        ) -> None:
            notifications.append((message, {"title": title, "severity": severity, "timeout": timeout}))

        def open_rollback_modal(self, *_args: object, **_kwargs: object) -> None:
            raise AssertionError("unexpected modal")

        async def restore_welcome_rollback(self, *, session_id: str | None, profile: str, cwd: str) -> None:
            raise AssertionError("unexpected welcome restore")

        def restore_input_text(self, text: str) -> None:
            restored.append(text)

    controller = _make_rollback_controller(view=_RollbackView())

    asyncio.run(
        controller.on_result(
            RollbackResult(session_id="session-1", target_turn=1, rolled_back_user_text="second prompt")
        )
    )

    assert restored == ["second prompt"]
    assert notifications[-1][1].get("severity") == "information"


def test_a_rollback_result_invalidates_the_resident_file_index() -> None:
    """The engine only publishes the result; the ``@`` snapshot learns of the restore here."""

    class _RollbackView:
        def notify(
            self,
            message: str,
            *,
            title: str,
            severity: str = "information",
            timeout: float | None = 3,
        ) -> None:
            pass

        def open_rollback_modal(self, *_args: object, **_kwargs: object) -> None:
            raise AssertionError("unexpected modal")

        async def restore_welcome_rollback(self, *, session_id: str | None, profile: str, cwd: str) -> None:
            raise AssertionError("unexpected welcome restore")

        def restore_input_text(self, text: str) -> None:
            return None

    invalidations: list[str] = []
    controller = _make_rollback_controller(
        view=_RollbackView(),
        invalidate_file_index=lambda: invalidations.append("invalidated"),
    )

    asyncio.run(controller.on_result(RollbackResult(session_id="session-1", target_turn=1)))

    assert invalidations == ["invalidated"]


def test_welcome_rollback_restores_input_after_welcome_state() -> None:
    calls: list[tuple[object, ...]] = []

    class _RollbackView:
        def notify(
            self,
            message: str,
            *,
            title: str,
            severity: str = "information",
            timeout: float | None = 3,
        ) -> None:
            calls.append(("notify", message))

        def open_rollback_modal(self, *_args: object, **_kwargs: object) -> None:
            raise AssertionError("unexpected modal")

        async def restore_welcome_rollback(self, *, session_id: str | None, profile: str, cwd: str) -> None:
            calls.append(("welcome", session_id, profile, cwd))

        def restore_input_text(self, text: str) -> None:
            calls.append(("input", text))

    controller = _make_rollback_controller(
        view=_RollbackView(),
        reset_welcome_workspace_marker=lambda cwd: calls.append(("workspace", cwd)),
        set_has_messages=lambda value: calls.append(("has_messages", value)),
    )

    asyncio.run(
        controller.on_result(
            RollbackResult(session_id="session-1", target_turn=0, rolled_back_user_text="first prompt")
        )
    )

    assert calls[:4] == [
        ("has_messages", False),
        ("workspace", "/repo/workspace"),
        ("welcome", "session-1", "Code", "/repo/workspace"),
        ("input", "first prompt"),
    ]


def test_rollback_result_surfaces_exclusions_and_warnings() -> None:
    """Plan exclusions/warnings reach the toast even with zero restore results.

    A plan whose every file is excluded restores nothing, so the terminal
    result still needs to report exclusions explicitly.
    """
    notifications: list[tuple[str, dict]] = []
    debug_details: list[str] = []
    state = main_screen_state_at("/repo/workspace")
    state.runtime.profile = "Code"
    screen = SimpleNamespace(
        _state=state,
        notify=lambda msg, **kwargs: notifications.append((msg, kwargs)),
        _debug=lambda _title, detail: debug_details.append(detail),
    )

    asyncio.run(
        _make_rollback_controller_for_test(screen).on_result(
            RollbackResult(
                session_id="session-1",
                target_turn=2,
                restore_results=[],
                exclusions=[
                    ("/ws/shared.py", "contested"),
                    ("/ws/peer[7].txt", "foreign"),
                    ("/ws/logo.png", "move_poisoned"),
                    ("/ws/big.log", "unrestorable"),
                ],
                warnings=["1 other session is writing to this workspace."],
            )
        )
    )

    msg, kwargs = notifications[-1]
    assert kwargs.get("severity") == "warning"
    # Toasts render as plain text, so paths like ``peer[7].txt`` stay literal.
    rendered = msg
    assert "4 files excluded from revert" in rendered
    # Reasons render with the modal's human labels, not raw enum values.
    assert "shared.py (modified internally and externally)" in rendered
    assert "peer[7].txt (changed by another session)" in rendered
    assert "(+1 more)" in rendered
    assert "1 other session is writing" in rendered
    assert any("4 excluded" in detail and "1 warning" in detail for detail in debug_details)


def test_show_rollback_defers_attribution_refresh_to_modal(monkeypatch) -> None:
    """The modal opens first and owns its post-paint attribution refresh."""
    import chrys.app.tui.screens.diff as diff_pkg

    pushed: list[object] = []
    refreshed: list[bool] = []
    order: list[str] = []
    state_loaders: list[object] = []

    class _FakeRollbackModal:
        def __init__(self, **kwargs: object) -> None:
            order.append("modal")
            pushed.append(self)
            state_loaders.append(kwargs["load_state"])

    class _FakeEngine(_RollbackProjectionFenceMixin):
        mutation_tracker = object()
        mutation_coordinator = object()
        current_turn_number = 3
        conversation_revision = 7

        def available_rollback_turns(self) -> list[int]:
            return [1, 2]

        def turn_prompt_previews(self) -> dict[int, str]:
            return {}

        async def refresh_mutation_attribution(self, *, force: bool = False) -> bool:
            order.append("refresh")
            refreshed.append(force)
            return False

    monkeypatch.setattr(diff_pkg, "RollbackModal", _FakeRollbackModal)
    screen = SimpleNamespace(
        app=SimpleNamespace(push_screen=lambda modal, _callback=None: None),
        notify=lambda *_args, **_kwargs: None,
    )
    controller = _make_rollback_controller(view=_adapter_for(screen), engine_provider=_FakeEngine)

    controller.show_rollback()

    assert refreshed == []
    assert order == ["modal"]
    assert len(pushed) == 1
    assert len(state_loaders) == 1
    state = asyncio.run(state_loaders[0]())
    assert state is not None
    assert state.attribution_refresh is not None
    asyncio.run(state.attribution_refresh())
    assert refreshed == [True]


def test_welcome_rollback_keeps_logo_metadata_and_suppresses_chdir_marker() -> None:
    """Rolling back all turns returns to a populated welcome screen, not chat history mode."""

    calls: list[tuple[str, object]] = []
    welcome_updates: list[tuple[str, str]] = []
    system_messages: list[str] = []
    terminal_title_cwds: list[str] = []

    class _FakePanel:
        border_subtitle = None

        async def clear(self) -> None:
            calls.append(("clear", None))

        def set_session_id(self, session_id: str) -> None:
            calls.append(("session_id", session_id))

        def set_workspace_cwd(self, cwd: str) -> None:
            calls.append(("workspace_cwd", cwd))
            self.border_subtitle = Text(cwd)

        def update_welcome(self, *, profile: str = "", cwd: str = "") -> None:
            welcome_updates.append((profile, cwd))

        async def add_system(self, text: str, *, key: str | None = None) -> None:
            system_messages.append(text)

        async def update_system(self, _key: str, new_text: str) -> None:
            system_messages.append(new_text)

        async def remove_system(self, _key: str) -> None:
            calls.append(("remove_system", _key))

    class _FakeInputBar:
        retry_mode = True
        has_messages = True

        def set_paste_cwd(self, cwd: str) -> None:
            calls.append(("paste_cwd", cwd))

        def set_clipboard_image_dir(self, directory: object) -> None:
            calls.append(("clipboard_image_dir", directory))

    class _FakeContextPanel:
        def reset(self) -> None:
            calls.append(("context_reset", None))

    class _FakeSidebarPanel:
        context_panel = _FakeContextPanel()

    class _FakeShellPanel:
        async def change_directory(self, cwd: str) -> None:
            calls.append(("shell_cwd", cwd))

    panel = _FakePanel()
    input_bar = _FakeInputBar()
    sidebar = _FakeSidebarPanel()
    shell = _FakeShellPanel()

    def query_one(cls: type):
        if cls.__name__ == "ChatPanel":
            return panel
        if cls.__name__ == "InputBar":
            return input_bar
        if cls.__name__ == "SidebarPanel":
            return sidebar
        if cls.__name__ == "ShellPanel":
            return shell
        raise AssertionError(f"unexpected query_one({cls.__name__})")

    state = main_screen_state_at("/repo/current")
    state.runtime.profile = "Code Agent"
    state.run.has_messages = True
    state.workspace_marker.original_cwd = "/repo/original"
    screen = SimpleNamespace(
        _state=state,
        _gc_messages=[],
        context_usage_state=ContextUsageState.with_window(
            used_tokens=12_000,
            max_context_tokens=180_000,
            total_session_tokens=64_000,
        ),
        _suggestions=SimpleNamespace(discard_file_index=lambda: None),
        query_one=query_one,
        _update_toc=lambda: calls.append(("toc", None)),
        _session_title=fake_session_title(
            set_terminal_title_for_cwd=terminal_title_cwds.append,
            reset_session_title_state=lambda: calls.append(("reset_title_state", None)),
        ),
        notify=lambda *_args, **_kwargs: None,
        _debug=lambda *_args: None,
    )

    def set_has_messages(value: bool) -> None:
        input_bar.has_messages = value

    screen._set_has_messages = set_has_messages

    asyncio.run(
        _make_rollback_controller_for_test(screen).on_result(RollbackResult(session_id="session-1", target_turn=0))
    )
    assert ("reset_title_state", None) in calls
    asyncio.run(make_session_handler(screen).on_workspace_updated(WorkspaceUpdated(primary_cwd="/repo/next")))

    assert state.run.has_messages is False
    assert state.workspace_marker.original_cwd is None
    assert input_bar.retry_mode is False
    assert screen.context_usage_state == ContextUsageState.with_window(
        used_tokens=0,
        max_context_tokens=180_000,
    )
    assert ("Code Agent", "/repo/current") in welcome_updates
    assert welcome_updates[-1] == ("Code Agent", "/repo/next")
    assert panel.border_subtitle.plain == "/repo/next"
    assert ("workspace_cwd", "/repo/current") in calls
    assert [message.reason for message in screen._gc_messages] == [
        GcReclaimReason.ROLLBACK_WELCOME,
        GcAbsorbReason.WORKSPACE_UI_UPDATED,
    ]
    assert screen._gc_messages[0].prompt is True
    assert ("workspace_cwd", "/repo/next") in calls
    assert ("paste_cwd", "/repo/next") in calls
    assert system_messages == []
    assert terminal_title_cwds == ["/repo/next"]


def test_welcome_rollback_clears_todo_state() -> None:
    """Rolling back to turn 0 clears the Tasks panel alongside the welcome reset."""

    class _FakePanel:
        border_subtitle = None

        async def clear(self) -> None:
            return

        def set_session_id(self, session_id: str) -> None:
            return

        def set_workspace_cwd(self, cwd: str) -> None:
            self.border_subtitle = Text(cwd)

        def update_welcome(self, *, profile: str = "", cwd: str = "") -> None:
            return

    class _FakeInputBar:
        retry_mode = True
        has_messages = True

        def set_paste_cwd(self, cwd: str) -> None:
            return

        def set_clipboard_image_dir(self, directory: object) -> None:
            return

    class _FakeContextPanel:
        def reset(self) -> None:
            return

    class _FakeSidebarPanel:
        context_panel = _FakeContextPanel()

    panel = _FakePanel()
    input_bar = _FakeInputBar()
    sidebar = _FakeSidebarPanel()

    def query_one(cls: type):
        if cls.__name__ == "ChatPanel":
            return panel
        if cls.__name__ == "InputBar":
            return input_bar
        if cls.__name__ == "SidebarPanel":
            return sidebar
        raise AssertionError(f"unexpected query_one({cls.__name__})")

    state = main_screen_state_at("/repo/current")
    state.runtime.profile = "Code Agent"
    state.run.has_messages = True
    state.workspace_marker.original_cwd = "/repo/original"
    screen = SimpleNamespace(
        _state=state,
        _set_has_messages=lambda _value: None,
        context_usage_state=None,
        todo_state=TodoListState(items=(TodoItem(content="obsolete", status="in_progress"),)),
        query_one=query_one,
        _update_toc=lambda: None,
        _session_title=fake_session_title(),
        notify=lambda *_args, **_kwargs: None,
        _debug=lambda *_args: None,
    )
    asyncio.run(
        _make_rollback_controller_for_test(screen).on_result(RollbackResult(session_id="session-1", target_turn=0))
    )

    assert screen.todo_state == TodoListState()
