# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Dialogs their owners report to, or push, while Textual removes them or the app shuts down render nothing."""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Callable
from pathlib import Path

import pytest
from textual.app import App, ComposeResult
from textual.screen import ModalScreen
from textual.widgets import Static

from chrys.app.tui.screens.dialogs.agent_load import AgentLoadDialog
from chrys.app.tui.screens.dialogs.approval.dialog import ApprovalDialog
from chrys.app.tui.screens.dialogs.confirm import ConfirmDialog
from chrys.app.tui.screens.dialogs.connection_test import ConnectionTestDialog
from chrys.app.tui.screens.dialogs.fork_session import ForkSessionDialog
from chrys.app.tui.screens.dialogs.image_compression import ImageCompressionDialog
from chrys.app.tui.screens.dialogs.vision_unsupported import VisionUnsupportedDialog
from chrys.app.tui.screens.main.hook_confirmation_dialog import HookConfirmationDialog
from chrys.app.tui.screens.models.screen import ModelConfigScreen
from chrys.foundation.events.types import HookConfirmationRequest
from chrys.foundation.tool_kinds import KIND_SHELL
from chrys.service.approval.judge import JudgeVerdict
from chrys.service.profiles.models.registry import ModelProfileRegistry
from chrys.service.profiles.models.schema import ModelProfile
from chrys.service.profiles.models.serializer import save_profile


def _report_workflow_load(dialog: AgentLoadDialog) -> None:
    dialog.update_title("Loading Workflow echo")
    dialog.update_progress("Loading definition", phase="workflow_definition", status="done")
    dialog.update_finish_progress("Showing workflow")
    dialog.finish("Workflow loaded")


def _report_agent_failure(dialog: AgentLoadDialog) -> None:
    dialog.set_result(False, "Agent failed")


def _report_fork(dialog: ForkSessionDialog) -> None:
    dialog.set_success("abc123")
    dialog.set_error("Fork failed")


def _report_connection(dialog: ConnectionTestDialog) -> None:
    dialog.set_result(True, "Connected")


def _report_compressed(dialog: ImageCompressionDialog) -> None:
    dialog.finish()


def _report_flagged(dialog: ApprovalDialog) -> None:
    dialog.receive_verdict(JudgeVerdict(approved=False, reason="Deletes files outside the workspace"))


def _bare[D: ModalScreen](cls: type[D]) -> D:
    return cls()


def _shell_approval(cls: type[ApprovalDialog]) -> ApprovalDialog:
    return cls("Code", "zsh", KIND_SHELL, {"command": "rm -rf build"})


# Each dialog, how to build it (or a subclass of it), and a report its owner sends it.
_DIALOGS: dict[str, tuple[type[ModalScreen], Callable, Callable]] = {
    "agent-load-finish": (AgentLoadDialog, _bare, _report_workflow_load),
    "agent-load-result": (AgentLoadDialog, _bare, _report_agent_failure),
    "fork-session": (ForkSessionDialog, _bare, _report_fork),
    "connection-test": (ConnectionTestDialog, _bare, _report_connection),
    "image-compression": (ImageCompressionDialog, _bare, _report_compressed),
    "approval-verdict": (ApprovalDialog, _shell_approval, _report_flagged),
}


def _css_path(base: type[ModalScreen]) -> Path:
    """*base*'s stylesheet: a relative CSS_PATH resolves against the subclass's module."""
    return Path(inspect.getfile(base)).parent / str(base.CSS_PATH)


class _Host(App[None]):
    def compose(self) -> ComposeResult:
        yield Static("placeholder")


@pytest.mark.parametrize("removal", ["pop", "app-exit"])
@pytest.mark.parametrize("kind", list(_DIALOGS))
async def test_reports_during_and_after_removal_are_buffered(kind: str, removal: str) -> None:
    base, build, report = _DIALOGS[kind]
    reported: list[bool] = []

    class Reporting(base):
        CSS_PATH = _css_path(base)

        def on_unmount(self) -> None:
            # Removal has pruned every child; the dialog itself has not unmounted yet.
            reported.append(not self.children)
            report(self)

    dialog = build(Reporting)
    app = _Host()
    async with app.run_test():
        await app.push_screen(dialog)
        if removal == "pop":
            await app.pop_screen()
    # Leaving run_test re-raises what a report raised on the way out.
    assert reported == [True]
    report(dialog)


async def _push_as_the_app_exits(dialog: ModalScreen, window: str) -> None:
    """Push *dialog* as the app shuts down, at the moment *window* names; leaving re-raises what its mount raised.

    - ``prune``: as the test's last step: the app then closes its screens, which prunes the dialog's children first;
    - ``late``: in the app's last running turn: the dialog mounts once the app stopped, and none of its children do;
    - ``exit``: in the turn the app begins to exit, as the user quits: the dialog is detached and no child mounts.
    """
    if window == "exit":

        class Quitting(_Host):
            def on_ready(self) -> None:
                self.exit()
                self.push_screen(dialog)

        app = Quitting()
        await app.run_async(headless=True)
        if app._exception is not None:
            raise app._exception
        return
    app = _Host()
    async with app.run_test():
        if window == "prune":
            app.push_screen(dialog)
        else:
            # Pushed as leaving run_test yields, before the app shuts down.
            asyncio.get_running_loop().call_soon(app.push_screen, dialog)


# What the dialog sees as it mounts in each window: whether it is pruning, the app running, and it attached.
_MOUNTED_IN = {"prune": (True, False, True), "late": (False, False, True), "exit": (False, True, False)}


@pytest.mark.parametrize("window", list(_MOUNTED_IN))
@pytest.mark.parametrize("reported", [False, True], ids=["unreported", "reported"])
@pytest.mark.parametrize("kind", list(_DIALOGS))
async def test_a_dialog_that_mounts_as_the_app_exits_renders_nothing(kind: str, reported: bool, window: str) -> None:
    """An owner can push its dialog as the app shuts down: its children are pruned before it mounts, or never mount."""
    base, build, report = _DIALOGS[kind]
    mounted: list[tuple[bool, bool, bool]] = []

    class Mounting(base):
        CSS_PATH = _css_path(base)

        def on_mount(self) -> None:
            # Dispatch runs the dialog's own handler after this one.
            mounted.append((self._pruning, self.app.is_running, self.is_attached))

    dialog = build(Mounting)
    if reported:
        report(dialog)
    await _push_as_the_app_exits(dialog, window)
    assert mounted == [_MOUNTED_IN[window]]


def _hook_request() -> HookConfirmationRequest:
    return HookConfirmationRequest(
        session_id="s1",
        request_id="r1",
        operation_id="op-r1",
        revision=1,
        run_id="run-r1",
        hook_id="guard",
        subject="tool",
        tool_name="edit_file",
    )


# Questions the backend puts to the user, who may be quitting meanwhile, and how to build each.
_QUESTIONS: dict[str, tuple[type[ModalScreen], Callable]] = {
    "vision-unsupported": (VisionUnsupportedDialog, lambda cls: cls("The model can't read images")),
    "vision-unsupported-paths": (
        VisionUnsupportedDialog,
        lambda cls: cls("The model can't read images", show_path_action=True),
    ),
    "confirm": (ConfirmDialog, _bare),
    "hook-confirmation": (HookConfirmationDialog, lambda cls: cls(_hook_request())),
}


@pytest.mark.parametrize("window", list(_MOUNTED_IN))
@pytest.mark.parametrize("kind", list(_QUESTIONS))
async def test_a_question_that_mounts_as_the_app_exits_renders_nothing(kind: str, window: str) -> None:
    base, build = _QUESTIONS[kind]
    mounted: list[tuple[bool, bool, bool]] = []

    class Mounting(base):
        CSS_PATH = _css_path(base)

        def on_mount(self) -> None:
            mounted.append((self._pruning, self.app.is_running, self.is_attached))

    await _push_as_the_app_exits(build(Mounting), window)
    assert mounted == [_MOUNTED_IN[window]]


def _models_page(cls: type[ModelConfigScreen]) -> ModelConfigScreen:
    save_profile(ModelProfile(id="main", name="Main", provider="openai", model_id="gpt-test"))
    registry = ModelProfileRegistry()
    registry.load_all()
    return cls(registry, "main")


@pytest.mark.parametrize("window", list(_MOUNTED_IN))
async def test_a_models_page_that_mounts_as_the_app_exits_renders_nothing(window: str) -> None:
    """The user can open the Models page and quit before it mounts: it lists and loads no profile then."""
    mounted: list[tuple[bool, bool, bool]] = []

    class Mounting(ModelConfigScreen):
        CSS_PATH = _css_path(ModelConfigScreen)

        def on_mount(self) -> None:
            # Dispatch runs the page's own handler after this one.
            mounted.append((self._pruning, self.app.is_running, self.is_attached))

    await _push_as_the_app_exits(_models_page(Mounting), window)
    assert mounted == [_MOUNTED_IN[window]]
