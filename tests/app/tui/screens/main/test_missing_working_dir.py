# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tests for asking for another working folder once the current one is gone.

Three places ask: a submit the backend refused for that reason, the end of a
turn whose folder disappeared, and restoring a session whose saved folder is
gone. They share one prompt, so they never stack.
"""

from __future__ import annotations

import asyncio
import dataclasses
from collections.abc import Awaitable, Callable
from pathlib import Path
from types import SimpleNamespace

import pytest

from chrys.app.tui.i18n import LocaleController
from chrys.app.tui.screens.dialogs.confirm import ConfirmDialog
from chrys.app.tui.screens.dialogs.file_picker import FilePicker, FilePickerMode
from chrys.app.tui.screens.main.session_handlers import RestoreRequest, SessionHandler
from chrys.app.tui.screens.main.state import MainScreenServices, MainScreenState
from chrys.app.tui.screens.main.workspace_actions import MissingDirReason, WorkspaceCallbacks, WorkspaceController
from chrys.app.tui.widgets.chrome.input_bar import ChatDraft
from chrys.foundation.config.settings import Settings
from chrys.foundation.events.bus import EventBus
from chrys.foundation.events.types import Error, SessionRestore, SessionRestored, WorkspaceChange
from chrys.foundation.i18n import DisplayPath, MessageRef
from chrys.foundation.i18n.formatting import format_message
from chrys.kernel import Message
from chrys.orchestration.engine.run.working_dir import WORKING_DIR_MISSING_CODE
from chrys.orchestration.engine.session_lifecycle import _RESTORE_SESSION_CWD_MISSING
from chrys.service.state.store import JsonFileStateStore
from chrys.service.workspaces.registry import WorkspaceRegistry
from tests.support.async_tasks import wait_for_started
from tests.support.event_capture import collect_events
from tests.support.symlinks import symlink_or_skip
from tests.support.tui_helpers import main_screen_state_at, make_backend_handler, make_session_handler
from tests.support.waiting import wait_for, wait_until
from tests.support.workspace_engine import start_workspace_engine
from tests.support.workspace_registry import make_workdir_alias

# ──────────── workspace controller ─────────────────────────────────────


class _Engine:
    """The engine reads the workspace controller makes, as values a test can move."""

    def __init__(self) -> None:
        self.session_generation = 1
        self.turn_lifecycle_task: asyncio.Task[None] | None = None

    def execution_busy(self) -> bool:
        return False


@dataclasses.dataclass
class _Pushed:
    screen: object
    answer: Callable[[object], None]


class _DialogView:
    """Records each pushed screen; the test dismisses it through its result callback."""

    def __init__(self) -> None:
        self.pushed: list[_Pushed] = []

    def push_screen(self, screen: object, callback: object | None = None) -> object:
        if not callable(callback):
            raise AssertionError("every missing-folder prompt waits for its result")
        self.pushed.append(_Pushed(screen, callback))
        return None

    def notify(
        self, message: object, *, title: object, severity: str = "information", timeout: float | None = 3
    ) -> None:
        raise AssertionError(f"unexpected notify: {message!r}")


@dataclasses.dataclass
class _Harness:
    controller: WorkspaceController
    state: MainScreenState
    engine: _Engine
    view: _DialogView
    started: list[Callable[[], Awaitable[object]]]
    changes: list[WorkspaceChange]
    flags: SimpleNamespace

    def run_started(self) -> asyncio.Task[object]:
        """Run the one worker the controller asked for, as the screen would."""
        (work,) = self.started
        self.started.clear()
        return asyncio.create_task(work())

    async def dialog(self, count: int, task: asyncio.Task[object]) -> _Pushed:
        """Wait until *count* screens were pushed and return the last one."""
        await wait_for(
            lambda: len(self.view.pushed) >= count or task.done(),
            description=f"screen {count} pushed",
        )
        if len(self.view.pushed) < count:
            await task
            raise AssertionError(f"finished after {len(self.view.pushed)} screen(s), expected {count}")
        return self.view.pushed[count - 1]

    async def finish(self, task: asyncio.Task[object], *, screens: int) -> None:
        """Await *task*, failing at once rather than hanging if it pushes more than *screens* screens."""
        await wait_for(lambda: task.done() or len(self.view.pushed) > screens, description="worker finished")
        assert len(self.view.pushed) == screens
        await task


async def _harness(cwd: Path, *, locale_controller: LocaleController | None = None) -> _Harness:
    bus = EventBus()
    changes: list[WorkspaceChange] = []
    await bus.subscribe(WorkspaceChange, lambda event: collect_events(changes, event))
    engine = _Engine()
    state = main_screen_state_at(str(cwd))
    view = _DialogView()
    started: list[Callable[[], Awaitable[object]]] = []
    flags = SimpleNamespace(workflow_mode=False)
    controller = WorkspaceController(
        state=state,
        services=MainScreenServices(bus=bus, engine_provider=lambda: engine),
        view=view,  # type: ignore[arg-type]
        callbacks=WorkspaceCallbacks(
            start_worker=started.append,
            debug=lambda *_args: None,
            workflow_mode=lambda: flags.workflow_mode,
        ),
        locale_controller=locale_controller,
    )
    return _Harness(controller, state, engine, view, started, changes, flags)


def _layout(tmp_path: Path) -> tuple[Path, Path, Path]:
    """A deleted workspace two levels below its nearest existing parent, and another folder there."""
    parent = tmp_path / "projects"
    replacement = parent / "replacement"
    replacement.mkdir(parents=True)
    return parent / "deleted" / "nested", parent, replacement


def _message_key(dialog: object) -> str:
    assert isinstance(dialog, ConfirmDialog)
    message = dialog._message_value
    assert isinstance(message, MessageRef)
    return message.definition.key


def _message_path(dialog: object) -> str:
    assert isinstance(dialog, ConfirmDialog)
    message = dialog._message_value
    assert isinstance(message, MessageRef)
    path = dict(message.args)["path"]
    assert isinstance(path, DisplayPath)
    return path.value


def _held_lifecycle(*, fail: bool = False) -> tuple[asyncio.Task[None], asyncio.Event]:
    """A turn lifecycle (run, save, after-turn hooks) that ends when the event is set."""
    release = asyncio.Event()

    async def lifecycle() -> None:
        await release.wait()
        if fail:
            raise RuntimeError("final save failed")

    return asyncio.create_task(lifecycle()), release


@pytest.mark.parametrize(
    ("reason", "key", "text"),
    [
        (
            "submit",
            "tui.workspace.missing.message_submit",
            "{path} was deleted or moved. Choose a folder to continue. Your message is still in the input box.",
        ),
        ("turn_end", "tui.workspace.missing.message", "{path} was deleted or moved. Choose a folder to continue."),
        (
            "restore",
            "tui.workspace.missing.message_restore",
            "The folder of this session, {path}, was deleted or moved. Choose a folder to open the session in.",
        ),
        ("restore", "tui.workspace.missing.workspace_restore", None),
    ],
)
async def test_declining_the_missing_folder_dialog_opens_no_picker(
    tmp_path: Path, reason: MissingDirReason, key: str, text: str | None
) -> None:
    missing, _parent, _replacement = _layout(tmp_path)
    h = await _harness(missing)

    task = asyncio.create_task(h.controller.choose_replacement_dir(str(missing), text is None, reason=reason))
    confirm = await h.dialog(1, task)

    dialog = confirm.screen
    assert isinstance(dialog, ConfirmDialog)
    assert dialog._title == "Working Folder Not Found"
    assert dialog._confirm_label == "Choose Folder…"
    assert _message_key(dialog) == key
    assert _message_path(dialog) == str(missing)
    if text is None:
        assert "Restore it at its original path." in dialog._message
        assert "create a new workspace" not in dialog._message
        assert "Choosing another folder will open this session there and leave the workspace." in dialog._message
    else:
        assert dialog._message == text.format(path=missing)
    assert h.controller._missing_prompt_active is True

    confirm.answer(False)

    assert await task is None
    assert len(h.view.pushed) == 1
    assert h.controller._missing_prompt_active is False
    assert h.changes == []


async def test_missing_folder_dialog_follows_the_ui_language(tmp_path: Path) -> None:
    missing, _parent, _replacement = _layout(tmp_path)
    h = await _harness(missing, locale_controller=LocaleController(Settings(locale="zh-Hans")))

    task = asyncio.create_task(h.controller.choose_replacement_dir(str(missing), reason="turn_end"))
    confirm = await h.dialog(1, task)

    dialog = confirm.screen
    assert isinstance(dialog, ConfirmDialog)
    assert dialog._title == "工作文件夹不存在"
    assert dialog._confirm_label == "选择文件夹…"
    assert dialog._message == f"{missing} 已被删除或移动。请选择一个文件夹后再继续。"
    confirm.answer(False)
    assert await task is None


async def test_choosing_opens_the_folder_picker_at_the_nearest_existing_parent(tmp_path: Path) -> None:
    missing, parent, replacement = _layout(tmp_path)
    h = await _harness(missing)

    task = asyncio.create_task(h.controller.choose_replacement_dir(str(missing), reason="restore"))
    (await h.dialog(1, task)).answer(True)
    picked = await h.dialog(2, task)

    picker = picked.screen
    assert isinstance(picker, FilePicker)
    assert picker._mode == FilePickerMode.FOLDER
    assert picker._initial_path == str(parent)
    assert isinstance(picker._title, MessageRef)
    assert format_message(picker._title) == "Change Directory"
    # The picker is part of the same prompt: nothing else may open meanwhile.
    assert h.controller._missing_prompt_active is True

    picked.answer(str(replacement))

    assert await task == str(replacement)
    assert h.controller._missing_prompt_active is False
    # Choosing only returns the folder; the caller decides what to do with it.
    assert h.changes == []


@pytest.mark.parametrize("answer", ["cancel", "deleted_meanwhile"])
async def test_picker_without_an_existing_folder_returns_none(tmp_path: Path, answer: str) -> None:
    missing, _parent, _replacement = _layout(tmp_path)
    h = await _harness(missing)

    task = asyncio.create_task(h.controller.choose_replacement_dir(str(missing), reason="submit"))
    (await h.dialog(1, task)).answer(True)
    picked = await h.dialog(2, task)
    picked.answer(None if answer == "cancel" else str(tmp_path / "vanished"))

    assert await task is None
    assert h.controller._missing_prompt_active is False


@pytest.mark.parametrize("wait_at", ["confirmation", "picker"])
async def test_missing_folder_choice_expires_when_the_active_session_changes(tmp_path: Path, wait_at: str) -> None:
    missing, _parent, replacement = _layout(tmp_path)
    h = await _harness(missing)
    h.controller.prompt_missing_working_dir("submit")
    task = h.run_started()
    confirmation = await h.dialog(1, task)
    if wait_at == "confirmation":
        h.engine.session_generation += 1
        confirmation.answer(True)
        await h.finish(task, screens=1)
    else:
        confirmation.answer(True)
        picker = await h.dialog(2, task)
        h.engine.session_generation += 1
        picker.answer(str(replacement))
        await h.finish(task, screens=2)
    assert h.changes == []


async def test_subtitle_directory_picker_cannot_change_a_newer_session(tmp_path: Path) -> None:
    _missing, parent, replacement = _layout(tmp_path)
    h = await _harness(parent)
    h.controller.open_working_dir_picker()
    (picker,) = h.view.pushed
    h.engine.session_generation += 1
    picker.answer(str(replacement))
    assert h.started == []
    assert h.changes == []


async def test_a_second_prompt_while_one_is_open_does_not_stack(tmp_path: Path) -> None:
    missing, _parent, replacement = _layout(tmp_path)
    h = await _harness(missing)

    first = asyncio.create_task(h.controller.choose_replacement_dir(str(missing), reason="submit"))
    confirm = await h.dialog(1, first)

    second = asyncio.create_task(h.controller.choose_replacement_dir(str(missing), reason="restore"))
    await h.finish(second, screens=1)
    assert second.result() is None
    h.controller.prompt_missing_working_dir("turn_end")
    assert h.started == []
    # The end of a turn still checks; its prompt finds this one open and stays quiet.
    h.controller.check_after_turn()
    await h.finish(h.run_started(), screens=1)

    confirm.answer(True)
    picked = await h.dialog(2, first)
    third = asyncio.create_task(h.controller.choose_replacement_dir(str(missing), reason="restore"))
    await h.finish(third, screens=2)
    assert third.result() is None

    picked.answer(str(replacement))
    assert await first == str(replacement)

    # Released: the next missing-folder event asks again.
    h.controller.prompt_missing_working_dir("turn_end")
    assert len(h.started) == 1


async def test_cancelled_prompt_releases_the_prompt(tmp_path: Path) -> None:
    missing, _parent, _replacement = _layout(tmp_path)
    h = await _harness(missing)

    task = asyncio.create_task(h.controller.choose_replacement_dir(str(missing), reason="turn_end"))
    await h.dialog(1, task)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert h.controller._missing_prompt_active is False
    h.controller.prompt_missing_working_dir("turn_end")
    assert len(h.started) == 1


async def test_prompt_missing_working_dir_does_nothing_while_the_folder_exists(tmp_path: Path) -> None:
    h = await _harness(tmp_path)

    h.controller.prompt_missing_working_dir("submit")

    assert h.started == []


@pytest.mark.parametrize(
    "change",
    [
        "none",
        "session_generation",
        "run_generation",
        "agent_running",
        "agent_loading",
        "workflow_mode",
        "workflow_callback",
        "shell_mode",
        "workspace_empty",
        "workspace_confirm",
        "workspace_alias",
        "workspace_cancel",
        "workspace_session",
    ],
)
async def test_prompt_missing_working_dir_makes_the_chosen_folder_the_workspace(tmp_path: Path, change: str) -> None:
    missing, parent, replacement = _layout(tmp_path)
    h = await _harness(missing)
    registry = WorkspaceRegistry(tmp_path / "workspaces.json")
    chosen = replacement
    if change != "none":
        missing.mkdir(parents=True)
        workspace = registry.create("App", str(missing))
        missing.rmdir()
        missing.parent.rmdir()
        h.state.workspace.workspace_id = workspace.id
        h.state.workspace.workspace_name = workspace.name
        h.state.run.has_messages = change != "workspace_empty"
        if change == "workspace_alias":
            chosen = parent / "chosen-link"
            symlink_or_skip(chosen, replacement, target_is_directory=True)
    entered, release = asyncio.Event(), asyncio.Event()
    races_with_workflow_check = change in {
        "session_generation",
        "run_generation",
        "agent_running",
        "agent_loading",
        "workflow_mode",
        "shell_mode",
    }

    async def held_workflow_check(_cwd: str) -> bool:
        entered.set()
        await release.wait()
        return False

    if races_with_workflow_check:
        h.controller._callbacks = dataclasses.replace(h.controller._callbacks, apply_workflow_cwd=held_workflow_check)
    if change == "workflow_callback":

        async def apply_workflow(_cwd: str) -> bool:
            h.flags.workflow_mode = True
            return False

        h.controller._callbacks = dataclasses.replace(h.controller._callbacks, apply_workflow_cwd=apply_workflow)

    h.controller.prompt_missing_working_dir("submit")
    worker = h.run_started()
    confirm = await h.dialog(1, worker)
    assert _message_key(confirm.screen) == "tui.workspace.missing.message_submit"
    confirm.answer(True)
    picked = await h.dialog(2, worker)
    assert isinstance(picked.screen, FilePicker)
    assert picked.screen._initial_path == str(parent)
    picked.answer(str(chosen))
    if change == "workflow_callback":
        await worker
        assert h.changes == []
        return
    if races_with_workflow_check:
        await wait_for_started(entered, worker, description="the workflow-directory check")
    if change == "session_generation":
        h.engine.session_generation += 1
    elif change == "run_generation":
        h.state.run.generation += 1
    elif change in {"agent_running", "agent_loading"}:
        setattr(h.state.run, change, True)
    elif change == "workflow_mode":
        h.flags.workflow_mode = True
    elif change == "shell_mode":
        h.state.shell.active = True
    release.set()
    if change.startswith("workspace_") and change != "workspace_empty":
        leave = await h.dialog(3, worker)
        if change == "workspace_session":
            h.engine.session_generation += 1
        leave.answer(change != "workspace_cancel")
    elif change == "workspace_empty":
        await h.finish(worker, screens=2)
    await worker

    accepted = change in {"none", "workspace_empty", "workspace_confirm", "workspace_alias"}
    assert [event.primary_cwd for event in h.changes] == ([str(chosen)] if accepted else [])
    if change in {"workspace_confirm", "workspace_alias"}:
        assert h.changes[0].confirmation.resolved_cwd == str(replacement.resolve())


async def test_prompt_worker_skips_a_folder_that_came_back_before_it_ran(tmp_path: Path) -> None:
    missing = tmp_path / "restored"
    h = await _harness(missing)

    h.controller.prompt_missing_working_dir("submit")
    missing.mkdir()
    await h.finish(h.run_started(), screens=0)


@pytest.mark.parametrize("situation", ["submit_pending", "workflow_mode", "folder_exists"])
async def test_check_after_turn_starts_nothing(tmp_path: Path, situation: str) -> None:
    missing, _parent, _replacement = _layout(tmp_path)
    h = await _harness(tmp_path if situation == "folder_exists" else missing)
    if situation == "submit_pending":
        # A refused submit prompts on its own once the backend says why.
        h.state.submit.begin("hello")
    elif situation == "workflow_mode":
        h.flags.workflow_mode = True

    h.controller.check_after_turn()

    assert h.started == []


async def test_check_after_turn_waits_for_the_turn_lifecycle_then_asks(tmp_path: Path) -> None:
    missing, _parent, _replacement = _layout(tmp_path)
    h = await _harness(missing)
    lifecycle, release = _held_lifecycle()
    h.engine.turn_lifecycle_task = lifecycle

    h.controller.check_after_turn()
    # The lifecycle captured when the run ended is the one waited on.
    h.engine.turn_lifecycle_task = None
    worker = h.run_started()

    assert not await wait_until(lambda: bool(h.view.pushed) or worker.done(), timeout=0.2)

    release.set()
    confirm = await h.dialog(1, worker)

    assert lifecycle.done()
    assert _message_key(confirm.screen) == "tui.workspace.missing.message"
    assert _message_path(confirm.screen) == str(missing)
    confirm.answer(False)
    await worker
    assert h.changes == []


@pytest.mark.parametrize("deleted_during_wait", [True, False])
async def test_check_after_turn_looks_at_the_folder_once_the_turn_lifecycle_ends(
    tmp_path: Path, deleted_during_wait: bool
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    h = await _harness(workspace)
    lifecycle, release = _held_lifecycle()
    h.engine.turn_lifecycle_task = lifecycle

    # The folder still exists when the run ends; the save or a hook may yet remove it.
    h.controller.check_after_turn()
    worker = h.run_started()
    assert not await wait_until(worker.done, timeout=0.2)
    if deleted_during_wait:
        workspace.rmdir()
    release.set()

    if not deleted_during_wait:
        await h.finish(worker, screens=0)
        return
    confirm = await h.dialog(1, worker)
    assert _message_path(confirm.screen) == str(workspace)
    confirm.answer(False)
    await worker


@pytest.mark.parametrize("lifecycle_state", ["none", "finished"])
async def test_check_after_turn_asks_at_once_without_a_live_lifecycle(tmp_path: Path, lifecycle_state: str) -> None:
    missing, _parent, _replacement = _layout(tmp_path)
    h = await _harness(missing)
    if lifecycle_state == "finished":
        lifecycle, release = _held_lifecycle()
        release.set()
        await lifecycle
        h.engine.turn_lifecycle_task = lifecycle

    h.controller.check_after_turn()
    worker = h.run_started()
    confirm = await h.dialog(1, worker)

    assert _message_key(confirm.screen) == "tui.workspace.missing.message"
    confirm.answer(False)
    await worker


@pytest.mark.parametrize("ending", ["interrupted", "failed"])
async def test_check_after_turn_still_asks_when_the_lifecycle_did_not_finish_cleanly(
    tmp_path: Path, ending: str
) -> None:
    missing, _parent, _replacement = _layout(tmp_path)
    h = await _harness(missing)
    lifecycle, release = _held_lifecycle(fail=ending == "failed")
    h.engine.turn_lifecycle_task = lifecycle

    h.controller.check_after_turn()
    worker = h.run_started()
    assert not await wait_until(worker.done, timeout=0.2)
    if ending == "interrupted":
        lifecycle.cancel()
    else:
        release.set()

    confirm = await h.dialog(1, worker)
    confirm.answer(False)
    await worker

    assert lifecycle.cancelled() is (ending == "interrupted")
    if ending == "failed":
        assert isinstance(lifecycle.exception(), RuntimeError)


@pytest.mark.parametrize(
    "change",
    ["session_generation", "run_generation", "agent_running", "agent_loading", "workflow_mode"],
)
async def test_check_after_turn_stays_quiet_when_the_screen_moved_on_during_the_wait(
    tmp_path: Path, change: str
) -> None:
    missing, _parent, _replacement = _layout(tmp_path)
    h = await _harness(missing)
    lifecycle, release = _held_lifecycle()
    h.engine.turn_lifecycle_task = lifecycle

    h.controller.check_after_turn()
    worker = h.run_started()
    assert not await wait_until(worker.done, timeout=0.2)
    if change == "session_generation":
        h.engine.session_generation += 1
    elif change == "run_generation":
        h.state.run.generation += 1
    elif change == "agent_running":
        h.state.run.agent_running = True
    elif change == "agent_loading":
        h.state.run.agent_loading = True
    else:
        h.flags.workflow_mode = True
    release.set()
    await h.finish(worker, screens=0)

    assert h.controller._missing_prompt_active is False


async def test_cancelling_the_after_turn_wait_leaves_the_turn_lifecycle_running(tmp_path: Path) -> None:
    missing, _parent, _replacement = _layout(tmp_path)
    h = await _harness(missing)
    lifecycle, release = _held_lifecycle()
    h.engine.turn_lifecycle_task = lifecycle

    h.controller.check_after_turn()
    worker = h.run_started()
    assert not await wait_until(worker.done, timeout=0.2)

    worker.cancel()
    await asyncio.wait({worker})

    assert worker.cancelled()
    assert not lifecycle.done()
    release.set()
    await lifecycle
    assert not lifecycle.cancelled()
    assert h.view.pushed == []


# ──────────── refused submit ───────────────────────────────────────────


class _Composer:
    """Mirrors ``InputBar.restore_draft``: the hand-back writes only into an empty composer."""

    def __init__(self, value: str) -> None:
        self.value = value
        self.locked = True

    def draft(self) -> ChatDraft:
        return ChatDraft(self.value)

    def unlock_and_keep(self) -> None:
        self.locked = False

    def restore_draft(self, draft: str | ChatDraft) -> bool:
        text = draft.text if isinstance(draft, ChatDraft) else draft
        if not text or self.value:
            return False
        self.value = text
        return True


def _working_dir_missing_error() -> Error:
    return Error(
        code=WORKING_DIR_MISSING_CODE,
        message="Working directory no longer exists: /gone",
        session_id="session-1",
    )


@pytest.mark.parametrize(
    ("composer_text", "expected_reason", "expected_text"),
    [
        # The submit cleared the composer: the message goes back there.
        ("", "submit", "hello"),
        # The user typed something new meanwhile: it is theirs and stays.
        ("newer draft", "turn_end", "newer draft"),
    ],
)
async def test_refused_submit_restores_the_draft_then_asks_for_a_folder(
    composer_text: str, expected_reason: str, expected_text: str
) -> None:
    composer = _Composer(composer_text)
    running: list[bool] = []
    debug: list[tuple[str, str]] = []

    def query_one(cls: type) -> object:
        if cls.__name__ == "InputBar":
            return composer
        raise AssertionError(f"a refused submit shows no {cls.__name__} error")

    state = MainScreenState()
    state.submit.begin("hello")
    # The backend refuses the submit inside its publish, on this task.
    state.submit.publish_from_current_task()
    screen = SimpleNamespace(
        _state=state,
        _set_agent_running=running.append,
        query_one=query_one,
        _debug=lambda key, message: debug.append((key, message)),
    )
    handler = make_backend_handler(screen)
    prompts: list[tuple[str, str]] = []
    # The draft is back before the prompt opens; the prompt never writes it.
    handler._callbacks = dataclasses.replace(
        handler._callbacks,
        prompt_missing_working_dir=lambda reason: prompts.append((reason, composer.value)),
    )

    await handler.on_error(_working_dir_missing_error())

    assert prompts == [(expected_reason, expected_text)]
    assert composer.value == expected_text
    assert composer.locked is False
    assert state.submit.blocked is True
    # The refusal is the message's: no run started, and none ends.
    assert running == []
    assert debug == [("Error", "[working_dir_missing] Working directory no longer exists: /gone")]


async def test_working_dir_error_outside_a_submit_leaves_the_prompt_to_the_turn_end() -> None:
    flashes: list[object] = []
    chat_errors: list[str] = []

    class _InputBar:
        locked = False
        retry_mode = False
        _retry_label = ""

        def unlock_and_keep(self) -> None:
            return None

    class _StatusBar:
        def flash(self, message: object, *, error: bool = False) -> None:
            flashes.append(message)

    class _ChatPanel:
        async def add_error_notice(self, message: str, *, action_label: MessageRef | str | None = None) -> None:
            chat_errors.append(message)

    widgets = {"InputBar": _InputBar(), "StatusBar": _StatusBar(), "ChatPanel": _ChatPanel()}
    screen = SimpleNamespace(query_one=lambda cls: widgets[cls.__name__], _debug=lambda *_args: None)
    handler = make_backend_handler(screen)
    handler._agent_load_dialog = None
    prompts: list[str] = []
    handler._callbacks = dataclasses.replace(handler._callbacks, prompt_missing_working_dir=prompts.append)

    await handler.on_error(_working_dir_missing_error())

    assert prompts == []
    assert chat_errors == ["Working directory no longer exists: /gone"]
    assert len(flashes) == 1


# ──────────── session restore ──────────────────────────────────────────


@dataclasses.dataclass
class _RestoreProbe:
    handler: SessionHandler
    state: MainScreenState
    calls: list[tuple[str, object]]
    published: list[object]


async def _restore_probe(store: JsonFileStateStore, choice: str | None) -> _RestoreProbe:
    """A session handler whose loading UI marks the screen loading, as the real one does."""
    calls: list[tuple[str, object]] = []
    published: list[object] = []
    state = MainScreenState()

    async def begin_session_restore_load(session_id: str) -> None:
        calls.append(("loading_ui", session_id))
        state.run.agent_loading = True

    def cancel_agent_load() -> None:
        calls.append(("cancel_loading_ui", None))
        state.run.agent_loading = False

    async def publish(event: object) -> None:
        published.append(event)

    async def choose(missing: str, _workspace_restore: bool) -> str | None:
        # What the screen looks like while the user is asked.
        calls.append(("choose", (missing, state.run.agent_loading, state.session.restoring_session)))
        return choice

    screen = SimpleNamespace(
        _state=state,
        _services=MainScreenServices(bus=SimpleNamespace(publish=publish), state_store=store),
        _events=SimpleNamespace(
            begin_session_restore_load=begin_session_restore_load,
            cancel_agent_load=cancel_agent_load,
        ),
        _debug=lambda *_args: None,
    )
    handler = make_session_handler(screen)
    handler._callbacks = dataclasses.replace(handler._callbacks, choose_missing_working_dir=choose)
    return _RestoreProbe(handler, state, calls, published)


async def _saved_store(tmp_path: Path, session_id: str, primary_cwd: Path) -> JsonFileStateStore:
    store = JsonFileStateStore(tmp_path / "state")
    await store.save_session(session_id, {"messages": [Message("user", ["hello"])]}, primary_cwd=str(primary_cwd))
    return store


@pytest.mark.parametrize("change", ["none", "alias", "picker_owner", "background_owner"])
async def test_restore_with_a_missing_saved_folder_restores_in_the_chosen_one(tmp_path: Path, change: str) -> None:
    missing, _parent, replacement = _layout(tmp_path)
    store = await _saved_store(tmp_path, "saved", missing)
    chosen = replacement
    if change == "alias":
        chosen = replacement.parent / "chosen-link"
        symlink_or_skip(chosen, replacement, target_is_directory=True)
    probe = await _restore_probe(store, str(chosen))
    if change != "none":
        missing.mkdir(parents=True)
        registry = store.workspace_registry
        workspace = registry.create("Team", str(missing))
        registry.set_session_workspace("saved", workspace.id)
        missing.rmdir()

        choose = probe.handler._callbacks.choose_missing_working_dir

        async def choose_folder(path: str, workspace_restore: bool) -> str | None:
            result = await choose(path, workspace_restore)
            if change == "picker_owner":
                probe.state.runtime.main_usage_source_id = "other-session"
            return result

        async def stop_background() -> bool:
            if change == "background_owner":
                probe.state.runtime.main_usage_source_id = "other-session"
            return True

        probe.handler._callbacks = dataclasses.replace(
            probe.handler._callbacks,
            choose_missing_working_dir=choose_folder,
            confirm_stopping_background_tasks=stop_background,
        )

    result = await probe.handler.do_session_restore("saved")

    if change in {"picker_owner", "background_owner"}:
        assert result is RestoreRequest.DECLINED
        assert probe.published == []
        assert not probe.state.run.agent_loading
        assert not probe.state.session.restoring_session
        return
    assert result is RestoreRequest.REQUESTED
    assert probe.calls == [("choose", (str(missing), False, False)), ("loading_ui", "saved")]
    (restore,) = probe.published
    assert isinstance(restore, SessionRestore)
    assert restore.session_id == "saved"
    assert restore.primary_cwd == str(chosen)
    if change == "alias":
        assert restore.confirmation.resolved_cwd == str(replacement.resolve())
    assert probe.state.session.restoring_session is True


@pytest.mark.parametrize("has_workspace", [False, True], ids=["unspecified", "workspace"])
async def test_restore_stops_if_another_session_opens_while_its_loading_dialog_mounts(
    tmp_path: Path, has_workspace: bool, agent_engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    missing, _parent, replacement = _layout(tmp_path)
    missing.mkdir(parents=True)
    store = await _saved_store(tmp_path, "saved", missing)
    await store.save_session("newer", {"messages": [Message("user", ["newer history"])]}, primary_cwd=str(replacement))
    registry = store.workspace_registry
    workspace_id = registry.create("Team", str(missing)).id if has_workspace else None
    if workspace_id is not None:
        registry.set_session_workspace("saved", workspace_id)
    missing.rmdir()
    probe = await _restore_probe(store, str(replacement))
    engine = await start_workspace_engine(agent_engine, monkeypatch, replacement, store=store)
    bus = engine.event_bus
    probe.handler._services = dataclasses.replace(probe.handler._services, bus=bus, engine_provider=lambda: engine)
    probe.state.runtime.main_usage_source_id = engine.session_id or ""
    restored: list[str | None] = []

    async def project_restored(event: SessionRestored) -> None:
        restored.append(event.session_id)
        probe.state.runtime.main_usage_source_id = event.session_id or ""

    await bus.subscribe(SessionRestored, project_restored)
    await bus.subscribe(SessionRestore, lambda event: collect_events(probe.published, event))
    begin = probe.handler._agent_load.begin_session_restore_load

    async def begin_load(session_id: str) -> None:
        await begin(session_id)
        await bus.publish(SessionRestore(session_id="newer"), raise_handler_errors=True)
        assert engine.session_id == "newer"
        assert registry.snapshot().workspace_id_for_session("saved") == workspace_id

    monkeypatch.setattr(probe.handler._agent_load, "begin_session_restore_load", begin_load)

    assert await probe.handler.do_session_restore("saved") is RestoreRequest.DECLINED
    assert [event.session_id for event in probe.published] == ["newer"]
    assert restored == ["newer"]
    assert registry.snapshot().workspace_id_for_session("saved") == workspace_id
    assert engine.session_id == "newer"
    assert engine.workspace.primary_cwd == str(replacement)
    assert [message.text for message in engine.history_messages if message.role == "user"] == ["newer history"]


@pytest.mark.parametrize("owner_changes", [False, True], ids=["current", "stale"])
async def test_restore_missing_folder_error_asks_only_if_the_session_is_current(
    tmp_path: Path, owner_changes: bool
) -> None:
    missing, _parent, _replacement = _layout(tmp_path)
    missing.mkdir(parents=True)
    store = await _saved_store(tmp_path, "saved", missing)
    probe = await _restore_probe(store, None)

    async def publish(event):
        probe.published.append(event)
        missing.rmdir()
        error = Error(
            code="session_cwd_missing",
            session_id="saved",
            display_message=_RESTORE_SESSION_CWD_MISSING.bind(path=DisplayPath(missing)),
        )
        assert probe.handler.capture_restore_missing_cwd(error)
        if owner_changes:
            probe.state.runtime.main_usage_source_id = "other-session"

    probe.handler._services = dataclasses.replace(probe.handler._services, bus=SimpleNamespace(publish=publish))

    assert await probe.handler.do_session_restore("saved") is RestoreRequest.DECLINED
    expected = [("loading_ui", "saved"), ("cancel_loading_ui", None)]
    if not owner_changes:
        expected.append(("choose", (str(missing), False, False)))
    assert probe.calls == expected
    assert not probe.state.run.agent_loading
    assert not probe.state.session.restoring_session


async def test_declining_a_missing_saved_folder_keeps_the_current_session(tmp_path: Path) -> None:
    missing, _parent, _replacement = _layout(tmp_path)
    store = await _saved_store(tmp_path, "saved", missing)
    probe = await _restore_probe(store, None)

    result = await probe.handler.do_session_restore("saved")

    assert result is RestoreRequest.DECLINED
    assert probe.calls == [("choose", (str(missing), False, False))]
    assert probe.published == []
    assert probe.state.session.restoring_session is False


async def test_resume_closes_its_loading_dialog_before_asking(tmp_path: Path) -> None:
    missing, _parent, replacement = _layout(tmp_path)
    store = await _saved_store(tmp_path, "saved", missing)
    probe = await _restore_probe(store, str(replacement))

    await probe.handler.resume_session()

    assert probe.calls == [
        ("loading_ui", ""),
        ("cancel_loading_ui", None),
        ("choose", (str(missing), False, False)),
        ("loading_ui", "saved"),
    ]
    (restore,) = probe.published
    assert isinstance(restore, SessionRestore)
    assert restore.primary_cwd == str(replacement)


async def test_declined_resume_leaves_the_screen_idle(tmp_path: Path) -> None:
    missing, _parent, _replacement = _layout(tmp_path)
    store = await _saved_store(tmp_path, "saved", missing)
    probe = await _restore_probe(store, None)

    await probe.handler.resume_session()

    assert probe.calls == [("loading_ui", ""), ("cancel_loading_ui", None), ("choose", (str(missing), False, False))]
    assert probe.published == []
    assert probe.state.run.agent_loading is False
    assert probe.state.session.restoring_session is False


async def test_resume_cancels_its_lookup_dialog_when_the_user_keeps_background_tasks(tmp_path: Path) -> None:
    store = await _saved_store(tmp_path, "saved", tmp_path)
    probe = await _restore_probe(store, None)

    async def keep_background_tasks() -> bool:
        assert probe.state.run.agent_loading
        assert probe.state.session.restoring_session
        probe.calls.append(("keep_background_tasks", None))
        return False

    probe.handler._callbacks = dataclasses.replace(
        probe.handler._callbacks, confirm_stopping_background_tasks=keep_background_tasks
    )
    await probe.handler.resume_session()

    assert probe.calls == [("loading_ui", ""), ("keep_background_tasks", None), ("cancel_loading_ui", None)]
    assert probe.published == []
    assert not probe.state.run.agent_loading
    assert not probe.state.session.restoring_session


async def test_restore_with_an_existing_saved_folder_does_not_ask(tmp_path: Path) -> None:
    store = await _saved_store(tmp_path, "saved", tmp_path)
    probe = await _restore_probe(store, None)

    assert await probe.handler.do_session_restore("saved") is RestoreRequest.REQUESTED

    assert probe.calls == [("loading_ui", "saved")]
    (restore,) = probe.published
    assert isinstance(restore, SessionRestore)
    assert restore.primary_cwd == ""


async def test_reopening_current_workspace_session_checks_snapshot_primary_after_symlink_moves(tmp_path: Path) -> None:
    installed, moved, link = make_workdir_alias(tmp_path, "installed", "moved", "link")
    store = await _saved_store(tmp_path, "saved", installed)
    registry = store.workspace_registry
    workspace = registry.create("Team", str(link))
    registry.set_session_workspace("saved", workspace.id)
    probe = await _restore_probe(store, None)
    probe.state.runtime.main_usage_source_id = "saved"
    probe.state.workspace.current_cwd = str(installed)
    probe.state.workspace.workspace_id = workspace.id
    link.unlink()
    installed.rmdir()
    symlink_or_skip(link, moved, target_is_directory=True)

    assert await probe.handler.do_session_restore("saved") is RestoreRequest.REQUESTED
    assert probe.calls == [("loading_ui", "saved")]
    (restore,) = probe.published
    assert isinstance(restore, SessionRestore)
    assert restore.workspace_snapshot.session_workspace("saved") == workspace
    assert restore.opened_cwd == str(moved)
    assert restore.primary_cwd == ""
    assert restore.confirmation is None


async def test_restore_in_a_given_folder_does_not_look_at_the_saved_one(tmp_path: Path) -> None:
    missing, _parent, replacement = _layout(tmp_path)
    store = await _saved_store(tmp_path, "saved", missing)
    probe = await _restore_probe(store, None)

    result = await probe.handler.do_session_restore("saved", primary_cwd=str(replacement))

    assert result is RestoreRequest.REQUESTED
    assert probe.calls == [("loading_ui", "saved")]
    (restore,) = probe.published
    assert isinstance(restore, SessionRestore)
    assert restore.primary_cwd == str(replacement)


@pytest.mark.parametrize("session_id", ["unknown", "unreadable"])
async def test_restore_leaves_unknown_or_unreadable_sessions_to_the_restore_itself(
    tmp_path: Path, session_id: str
) -> None:
    store = JsonFileStateStore(tmp_path / "state")
    if session_id == "unreadable":
        session_file = store.session_dir(session_id) / "session.json"
        session_file.parent.mkdir(parents=True)
        session_file.write_text("{not json", encoding="utf-8")
    probe = await _restore_probe(store, None)

    assert await probe.handler.do_session_restore(session_id) is RestoreRequest.REQUESTED

    assert probe.calls == [("loading_ui", session_id)]
    (restore,) = probe.published
    assert isinstance(restore, SessionRestore)
    assert restore.session_id == session_id
