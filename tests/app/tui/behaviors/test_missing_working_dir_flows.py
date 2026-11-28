# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""End-to-end flows for a working folder deleted or moved outside the app.

The user is asked for another folder, picks one in the folder picker (which
opens at the nearest folder that still exists), and the app continues there.
"""

from __future__ import annotations

import asyncio
import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest
from textual.pilot import Pilot
from textual.screen import Screen

from chrys.app.tui.app import ChrysApp
from chrys.app.tui.screens.dialogs.agent_load import AgentLoadDialog
from chrys.app.tui.screens.dialogs.confirm import ConfirmDialog
from chrys.app.tui.screens.dialogs.file_picker import FilePicker, _FilteredDirectoryTree
from chrys.app.tui.screens.main.screen import MainScreen
from chrys.app.tui.widgets.chrome.input_bar import InputBar
from chrys.foundation.events.bus import EventBus
from chrys.foundation.events.types import (
    AgentLoadFinished,
    AgentLoadStarted,
    Error,
    InvocationMessage,
    SessionReady,
    SessionRestore,
    SessionRestored,
    UserMessage,
    WorkspaceChange,
    WorkspaceChangeFinished,
    WorkspaceUpdated,
)
from chrys.foundation.i18n import MessageRef
from chrys.foundation.models.invocations import InvocationOrigin
from chrys.foundation.models.workspace import Workspace
from chrys.foundation.platform import get_platform
from chrys.kernel import Message
from chrys.orchestration.engine.run.working_dir import WORKING_DIR_MISSING_CODE
from chrys.service.profiles.agents.registry import AgentProfileRegistry
from chrys.service.profiles.agents.schema import AgentProfile
from chrys.service.profiles.models.registry import ModelProfileRegistry
from chrys.service.profiles.models.schema import ModelProfile
from chrys.service.state.store import JsonFileStateStore
from tests.support.event_capture import capture_event_sequence, collect_events
from tests.support.rollback_spec import PROFILE, ask
from tests.support.tui_app_harness import SessionGenerationEngine, make_chrys_app
from tests.support.tui_helpers import click_when_settled
from tests.support.waiting import wait_for
from tests.support.workspace_engine import start_workspace_engine
from tests.support.workspace_registry import write_cwd_hook

_PROFILE = AgentProfile(name="Code", display_name="Code", description="Test profile")


class _Registry:
    def list_profiles(self) -> list[AgentProfile]:
        return [_PROFILE]

    def load_all(self) -> None:
        return

    def get(self, name: str) -> AgentProfile | None:
        return _PROFILE if name == _PROFILE.name else None


class _Engine(SessionGenerationEngine):
    """Starts a session in *cwd*; the turn lifecycle is whatever the test installs."""

    def __init__(self, bus: EventBus, cwd: Path) -> None:
        self._bus = bus
        self._cwd = cwd
        self.workspace = Workspace.from_cwd(str(cwd))
        self.session = SimpleNamespace(
            workspace=self.workspace,
            workspace_id=None,
            owns_runtime_id=lambda session_id: session_id == "test-session",
        )
        self.turn_lifecycle_task: asyncio.Task[None] | None = None

    async def start(self, _profile: AgentProfile) -> None:
        await self._bus.publish(
            SessionReady(
                agent_profile=_PROFILE.name,
                display_name=_PROFILE.display_name,
                session_id="test-session",
                primary_cwd=str(self._cwd),
            )
        )


def _make_app(tmp_path: Path, bus: EventBus, engine: _Engine) -> ChrysApp:
    # A selectable model keeps the unconfigured-model send guard out of the way.
    models = ModelProfileRegistry()
    models.register(ModelProfile(id="model-profile", name="Configured Model", model_id="test-model"))
    return make_chrys_app(
        tmp_path / "state",
        engine=engine,
        event_bus=bus,
        agent_registry=_Registry(),
        model_registry=models,
    )


def _folders(tmp_path: Path) -> tuple[Path, Path, Path]:
    """The workspace that will be deleted, its parent, and another folder beside it."""
    parent = tmp_path / "projects"
    workspace = parent / "doomed"
    replacement = parent / "replacement"
    workspace.mkdir(parents=True)
    replacement.mkdir()
    return workspace, parent, replacement


async def _ready_main_screen(pilot: Pilot, cwd: Path) -> MainScreen:
    app = pilot.app
    assert isinstance(app, ChrysApp)

    def ready() -> bool:
        screen = app._main_screen
        return (
            screen is not None
            and app.screen is screen
            and screen._state.workspace.current_cwd == str(cwd)
            and not screen._state.run.agent_loading
        )

    await wait_for(ready, pilot=pilot, description="session ready in its workspace")
    screen = app._main_screen
    assert screen is not None
    return screen


async def _top_screen[S: Screen](pilot: Pilot, screen_type: type[S]) -> S:
    await wait_for(
        lambda: isinstance(pilot.app.screen, screen_type) and pilot.app.screen.is_mounted,
        pilot=pilot,
        description=f"{screen_type.__name__} on top",
    )
    screen = pilot.app.screen
    assert isinstance(screen, screen_type)
    return screen


async def _submit(pilot: Pilot, screen: MainScreen, text: str) -> InputBar:
    input_bar = screen.query_one(InputBar)
    input_bar.value = text
    await input_bar.action_submit()
    return input_bar


def _message_key(dialog: ConfirmDialog) -> str:
    message = dialog._message_value
    assert isinstance(message, MessageRef)
    return message.definition.key


async def _choose_folder(pilot: Pilot, dialog: ConfirmDialog, opens_at: Path, folder: Path) -> None:
    """Confirm the missing-folder dialog, then pick *folder* in the picker it opens."""
    await click_when_settled(pilot, "#confirm-yes")
    picker = await _top_screen(pilot, FilePicker)
    assert picker._initial_path == str(opens_at)
    tree = picker.query_one("#fsd-tree", _FilteredDirectoryTree)
    await wait_for(
        lambda: (
            tree.path == opens_at
            and any(node.data is not None and node.data.path == folder for node in tree.root.children)
        ),
        pilot=pilot,
        description="picker lists the folders beside the deleted one",
    )
    node = next(node for node in tree.root.children if node.data is not None and node.data.path == folder)
    tree.move_cursor(node)
    await wait_for(lambda: picker._selected_path == str(folder), pilot=pilot, description="folder highlighted")
    await click_when_settled(pilot, "#fsd-select")
    await wait_for(lambda: not picker.is_attached, pilot=pilot, description="picker closed")


async def test_refused_submit_keeps_the_draft_and_continues_in_the_chosen_folder(tmp_path: Path) -> None:
    workspace, parent, replacement = _folders(tmp_path)
    bus = EventBus()
    engine = _Engine(bus, workspace)
    sent: list[str] = []
    changes: list[WorkspaceChange] = []

    async def refuse(event: UserMessage) -> None:
        # What the engine does with a fresh prompt while the folder is gone.
        sent.append(event.text)
        await bus.publish(
            Error(
                code=WORKING_DIR_MISSING_CODE,
                message=f"Working directory no longer exists: {workspace}",
                session_id="test-session",
            )
        )

    await bus.subscribe(UserMessage, refuse)
    await bus.subscribe(WorkspaceChange, lambda event: collect_events(changes, event))
    app = _make_app(tmp_path, bus, engine)

    async with app.run_test(size=(120, 40)) as pilot:
        main_screen = await _ready_main_screen(pilot, workspace)
        shutil.rmtree(workspace)

        input_bar = await _submit(pilot, main_screen, "hello")
        dialog = await _top_screen(pilot, ConfirmDialog)

        assert sent == ["hello"]
        assert input_bar.value == "hello"
        assert dialog._title == "Working Folder Not Found"
        assert _message_key(dialog) == "tui.workspace.missing.message_submit"

        await _choose_folder(pilot, dialog, parent, replacement)
        await wait_for(lambda: bool(changes), pilot=pilot, description="workspace change published")

        assert [change.primary_cwd for change in changes] == [str(replacement)]
        assert app.screen is main_screen
        assert input_bar.value == "hello"


@pytest.mark.parametrize("ending", ["answer", "error"])
async def test_a_turn_that_outlived_its_folder_asks_for_another_when_it_ends(tmp_path: Path, ending: str) -> None:
    workspace, parent, replacement = _folders(tmp_path)
    bus = EventBus()
    engine = _Engine(bus, workspace)
    sent: list[str] = []
    changes: list[WorkspaceChange] = []

    async def accept(event: UserMessage) -> None:
        sent.append(event.text)
        event.started_turn = True

    await bus.subscribe(UserMessage, accept)
    await bus.subscribe(WorkspaceChange, lambda event: collect_events(changes, event))
    app = _make_app(tmp_path, bus, engine)

    async with app.run_test(size=(120, 40)) as pilot:
        main_screen = await _ready_main_screen(pilot, workspace)
        await _submit(pilot, main_screen, "hello")
        await wait_for(
            lambda: sent == ["hello"] and main_screen._state.run.agent_running and not main_screen._state.submit.active,
            pilot=pilot,
            description="turn running",
        )
        shutil.rmtree(workspace)

        # The backend ends the turn the usual way; nothing was refused.
        if ending == "answer":
            await bus.publish(
                InvocationMessage(
                    text="done",
                    is_final=True,
                    origin=InvocationOrigin("turn", "test-session", "turn-test", None),
                    session_id="test-session",
                )
            )
        else:
            await bus.publish(Error(code="executor_error", message="boom", session_id="test-session"))
        dialog = await _top_screen(pilot, ConfirmDialog)

        assert not main_screen._state.run.agent_running
        assert _message_key(dialog) == "tui.workspace.missing.message"
        await _choose_folder(pilot, dialog, parent, replacement)
        await wait_for(lambda: bool(changes), pilot=pilot, description="workspace change published")
        assert [change.primary_cwd for change in changes] == [str(replacement)]


async def test_turn_end_prompt_does_not_stack_on_a_refused_submit(tmp_path: Path) -> None:
    workspace, parent, replacement = _folders(tmp_path)
    bus = EventBus()
    engine = _Engine(bus, workspace)
    changes: list[WorkspaceChange] = []

    async def refuse(_event: UserMessage) -> None:
        await bus.publish(
            Error(
                code=WORKING_DIR_MISSING_CODE,
                message=f"Working directory no longer exists: {workspace}",
                session_id="test-session",
            )
        )

    await bus.subscribe(UserMessage, refuse)
    await bus.subscribe(WorkspaceChange, lambda event: collect_events(changes, event))
    app = _make_app(tmp_path, bus, engine)

    async with app.run_test(size=(120, 40)) as pilot:
        main_screen = await _ready_main_screen(pilot, workspace)

        # A turn ends while its save and after-turn hooks still run, and the
        # folder was deleted during it.
        release_lifecycle = asyncio.Event()
        engine.turn_lifecycle_task = asyncio.create_task(release_lifecycle.wait())  # type: ignore[assignment]
        main_screen._set_agent_running(True)
        shutil.rmtree(workspace)
        main_screen._set_agent_running(False)
        (after_turn,) = [worker for worker in app.workers if worker.name == "_prompt_after_turn"]

        # The user sends a message meanwhile; it is refused and asks first.
        await _submit(pilot, main_screen, "hello")
        dialog = await _top_screen(pilot, ConfirmDialog)
        assert _message_key(dialog) == "tui.workspace.missing.message_submit"

        release_lifecycle.set()

        def confirm_dialogs() -> list[Screen]:
            return [screen for screen in app.screen_stack if isinstance(screen, ConfirmDialog)]

        await wait_for(
            lambda: after_turn.is_finished or len(confirm_dialogs()) > 1,
            pilot=pilot,
            description="turn-end check finished",
        )
        assert confirm_dialogs() == [dialog]
        await after_turn.wait()
        assert app.screen is dialog

        await _choose_folder(pilot, dialog, parent, replacement)
        await wait_for(lambda: bool(changes), pilot=pilot, description="workspace change published")
        assert [change.primary_cwd for change in changes] == [str(replacement)]
        assert not [screen for screen in app.screen_stack if isinstance(screen, ConfirmDialog | FilePicker)]


async def test_resume_of_a_session_whose_folder_is_gone_opens_it_in_the_chosen_folder(tmp_path: Path) -> None:
    workspace, parent, replacement = _folders(tmp_path)
    deleted = parent / "deleted"
    bus = EventBus()
    engine = _Engine(bus, workspace)
    restores: list[SessionRestore] = []
    await bus.subscribe(SessionRestore, lambda event: collect_events(restores, event))
    app = _make_app(tmp_path, bus, engine)

    async with app.run_test(size=(120, 40)) as pilot:
        main_screen = await _ready_main_screen(pilot, workspace)
        store = main_screen._services.state_store
        assert store is not None
        await store.save_session("saved-session", {"messages": [Message("user", ["hello"])]}, primary_cwd=str(deleted))

        await _submit(pilot, main_screen, "/resume")
        dialog = await _top_screen(pilot, ConfirmDialog)

        assert _message_key(dialog) == "tui.workspace.missing.message_restore"
        # The resume's loading dialog made way for the question.
        assert not [screen for screen in app.screen_stack if isinstance(screen, AgentLoadDialog)]
        assert main_screen._state.run.agent_loading is False
        assert restores == []

        await _choose_folder(pilot, dialog, parent, replacement)
        await wait_for(lambda: bool(restores), pilot=pilot, description="session restore requested")

        (restore,) = restores
        assert restore.session_id == "saved-session"
        assert restore.primary_cwd == str(replacement)
        assert restore.confirmation is None


@pytest.mark.parametrize("has_workspace", [False, True], ids=["unspecified", "workspace"])
async def test_resume_current_session_after_its_folder_moves(
    agent_engine, monkeypatch, tmp_path: Path, has_workspace
) -> None:
    workspace, parent, replacement = _folders(tmp_path)
    hook_output = tmp_path / "restored-in.txt"
    write_cwd_hook(get_platform().config_dir, hook_output, event="session_restored", hook_id="record-restore")
    store = JsonFileStateStore(tmp_path / "state")
    registry = store.workspace_registry
    workspace_id = registry.create("Team", str(workspace)).id if has_workspace else None
    engine = await start_workspace_engine(agent_engine, monkeypatch, workspace, store=store, workspace_id=workspace_id)
    profiles = AgentProfileRegistry()
    profiles.register(PROFILE)
    app = make_chrys_app(
        tmp_path / "state",
        engine=engine,
        event_bus=engine.event_bus,
        agent_registry=profiles,
        profile_name=PROFILE.name,
    )

    async with app.run_test(size=(120, 40)) as pilot:
        screen = await _ready_main_screen(pilot, workspace)
        await ask(engine, "saved conversation")
        session_id = engine.session_id
        assert session_id is not None and await store.load_latest_session_id() == session_id
        assert engine.session.workspace_id == workspace_id
        history = engine.current.require_loaded().bindings.backend.history_state
        history["messages"].append(Message("user", ["only in memory"]))
        workspace.rename(parent / "moved")

        async with capture_event_sequence(
            engine.event_bus,
            AgentLoadStarted,
            AgentLoadFinished,
            SessionRestored,
            WorkspaceUpdated,
            WorkspaceChangeFinished,
        ) as events:
            await _submit(pilot, screen, "/resume")
            dialog = await _top_screen(pilot, ConfirmDialog)
            await _choose_folder(pilot, dialog, parent, replacement)
            await wait_for(
                lambda: any(isinstance(event, SessionRestored | WorkspaceChangeFinished) for event in events),
                pilot=pilot,
                description="resume reaches a terminal event",
            )
            (restored,) = [event for event in events if isinstance(event, SessionRestored)]
            assert (restored.session_id, restored.primary_cwd, restored.message_count) == (
                session_id,
                str(replacement),
                2,
            )
            assert [message.text for message in engine.history_messages if message.role == "user"] == [
                "saved conversation"
            ]
            assert engine.session.workspace_id is None
            assert registry.snapshot().workspace_id_for_session(session_id) is None
            # Opening the report creates it before write_text has flushed its contents.
            await wait_for(
                lambda: hook_output.is_file() and hook_output.stat().st_size > 0,
                pilot=pilot,
                description="session_restored hook wrote its report",
            )
            assert Path(hook_output.read_text(encoding="utf-8")).resolve() == replacement.resolve()
            assert not screen._state.run.agent_loading
            assert not screen._state.session.restoring_session
