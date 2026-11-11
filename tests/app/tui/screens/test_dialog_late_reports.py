# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Dialogs their owners report to, or push, while Textual removes them or the app shuts down render nothing."""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Callable
from pathlib import Path

import pytest
from textual.app import App, ComposeResult
from textual.screen import ModalScreen, Screen
from textual.theme import Theme
from textual.widgets import Static

from chrys.app.tui.i18n import LocaleController
from chrys.app.tui.screens.agents.config import AgentsConfigScreen
from chrys.app.tui.screens.agents.mcp_choices import collect_mcp_choices
from chrys.app.tui.screens.agents.mcp_picker import MCPPickerDialog
from chrys.app.tui.screens.agents.picker import AgentsScreen
from chrys.app.tui.screens.dialogs.agent_load import AgentLoadDialog
from chrys.app.tui.screens.dialogs.approval.dialog import ApprovalDialog
from chrys.app.tui.screens.dialogs.approval.mode import ApprovalModeScreen
from chrys.app.tui.screens.dialogs.confirm import ConfirmDialog
from chrys.app.tui.screens.dialogs.connection_test import ConnectionTestDialog
from chrys.app.tui.screens.dialogs.diagram import DiagramDialog
from chrys.app.tui.screens.dialogs.editor import EditorDialog
from chrys.app.tui.screens.dialogs.file_picker import FilePicker
from chrys.app.tui.screens.dialogs.fork_session import ForkSessionDialog
from chrys.app.tui.screens.dialogs.image_compression import ImageCompressionDialog
from chrys.app.tui.screens.dialogs.man_page import ManPageDialog
from chrys.app.tui.screens.dialogs.new_folder import NewFolderDialog
from chrys.app.tui.screens.dialogs.session_title import SessionTitleDialog
from chrys.app.tui.screens.dialogs.vision_unsupported import VisionUnsupportedDialog
from chrys.app.tui.screens.dialogs.workflow_input import WorkflowInputContext, WorkflowInputDialog
from chrys.app.tui.screens.diff.rollback_modal import RollbackModal
from chrys.app.tui.screens.diff.screen import DiffScreen
from chrys.app.tui.screens.guides.screen import GuideDialog
from chrys.app.tui.screens.main.hook_confirmation_dialog import HookConfirmationDialog
from chrys.app.tui.screens.menus.languages import LanguagesScreen
from chrys.app.tui.screens.models.picker import ModelsScreen
from chrys.app.tui.screens.models.screen import ModelConfigScreen
from chrys.app.tui.screens.sessions.screen import SessionsScreen
from chrys.app.tui.screens.settings import SettingsDialog
from chrys.app.tui.screens.settings.migrate_dialog import MigrateSessionsDialog
from chrys.app.tui.screens.themes.picker import ThemesScreen
from chrys.app.tui.screens.themes.save import SaveThemeDialog
from chrys.app.tui.themes.store import UserThemeStore
from chrys.app.tui.widgets.chrome.commands import ManPageSpec, ManPageVerbatimBlock
from chrys.app.tui.widgets.editor import EditorBufferSnapshot
from chrys.app.tui.widgets.markdown.diagram.model import CompiledDiagram, DiagramKind
from chrys.foundation.config.settings import Settings
from chrys.foundation.events.types import HookConfirmationRequest
from chrys.foundation.tool_kinds import KIND_SHELL
from chrys.service.approval.judge import JudgeVerdict
from chrys.service.approval.policy import ApprovalMode
from chrys.service.profiles.agents.registry import AgentProfileRegistry
from chrys.service.profiles.agents.schema import MCPServerConfig, ToolsConfig
from chrys.service.profiles.models.registry import ModelProfileRegistry
from chrys.service.profiles.models.schema import ModelProfile
from chrys.service.profiles.models.serializer import save_profile
from chrys.service.state.store import JsonFileStateStore
from tests.app.tui.screens._agent_config_support import make_profile
from tests.app.tui.screens.settings.support import StubPorts, StubSessionStorage


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


def _css_path(base: type[Screen]) -> Path | None:
    """*base*'s stylesheet: a relative CSS_PATH resolves against the subclass's module."""
    if base.CSS_PATH is None:
        return None
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


async def _push_as_the_app_exits(dialog: Screen, window: str) -> None:
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


def _agents() -> AgentProfileRegistry:
    registry = AgentProfileRegistry()
    registry.load_all()
    return registry


def _models() -> ModelProfileRegistry:
    registry = ModelProfileRegistry()
    registry.register(ModelProfile(id="main", name="Main", provider="openai", model_id="gpt-test"))
    return registry


def _mcp_picker(cls: type[MCPPickerDialog]) -> MCPPickerDialog:
    server = MCPServerConfig(name="files", transport="stdio", command="/bin/files")
    return cls(collect_mcp_choices([make_profile("Code", tools=ToolsConfig(mcp=[server]))]), [])


def _diagram(cls: type[DiagramDialog]) -> DiagramDialog:
    rows = ("A --> B",)
    return cls(
        CompiledDiagram(source="flowchart LR\n    A --> B", kind=DiagramKind.FLOWCHART, width=7, height=1, rows=rows)
    )


def _workflow_input(cls: type[WorkflowInputDialog]) -> WorkflowInputDialog:
    context = WorkflowInputContext(
        title="echo",
        description="",
        source_kind="builtin",
        working_directory=str(Path.cwd()),
        has_kernel_agents=False,
        can_change_directory=False,
    )
    return cls(context=context)


def _save_theme(cls: type[SaveThemeDialog]) -> SaveThemeDialog:
    return cls(
        Theme("draft", primary="#004578"),
        UserThemeStore(Path.cwd()),
        None,
        preview=lambda _theme: None,
        saved=lambda _theme, _revision: None,
        restore=lambda: None,
        locale_controller=LocaleController(Settings()),
        preview_error=str,
    )


# Pages and dialogs the user opens, who may quit before they mount, and how to build each.
_OPENED: dict[str, tuple[type[Screen], Callable]] = {
    "agents": (AgentsScreen, lambda cls: cls(_agents(), current_profile="Code")),
    "agent-config": (AgentsConfigScreen, lambda cls: cls(_agents(), current_profile="Code", initial_profile="Code")),
    "approval-mode": (ApprovalModeScreen, lambda cls: cls(ApprovalMode.MANUAL)),
    "diagram": (DiagramDialog, _diagram),
    "diff": (DiffScreen, lambda cls: cls({}, cwd=str(Path.cwd()), subtitle_parts=("macOS",))),
    "editor": (EditorDialog, lambda cls: cls(EditorBufferSnapshot("draft", (0, 5)))),
    "file-picker": (FilePicker, lambda cls: cls(initial_path=str(Path.cwd()))),
    "guide": (GuideDialog, _bare),
    "languages": (LanguagesScreen, lambda cls: cls("en")),
    "man-page": (ManPageDialog, lambda cls: cls([ManPageSpec("demo", (ManPageVerbatimBlock("text", indent=0),))])),
    "mcp-picker": (MCPPickerDialog, _mcp_picker),
    "migrate-sessions": (
        MigrateSessionsDialog,
        lambda cls: cls(StubSessionStorage(Path.cwd()), source=Path.cwd() / "sessions", destination=None),
    ),
    "model-picker": (ModelsScreen, lambda cls: cls(_models(), "main")),
    "models-page": (ModelConfigScreen, _models_page),
    "new-folder": (NewFolderDialog, lambda cls: cls(Path.cwd())),
    "rollback": (RollbackModal, lambda cls: cls(tracker=None, available_turns=[0, 1], cwd=str(Path.cwd()))),
    "save-theme": (SaveThemeDialog, _save_theme),
    "session-title": (SessionTitleDialog, lambda cls: cls(auto_title="Auto title")),
    "sessions": (SessionsScreen, lambda cls: cls(JsonFileStateStore(Path.cwd()))),
    "settings": (SettingsDialog, lambda cls: cls(StubPorts())),
    "themes": (ThemesScreen, lambda cls: cls("textual-dark")),
    "workflow-input": (WorkflowInputDialog, _workflow_input),
}


@pytest.mark.parametrize("window", list(_MOUNTED_IN))
@pytest.mark.parametrize("kind", list(_OPENED))
async def test_a_page_the_user_opens_as_the_app_exits_renders_nothing(kind: str, window: str) -> None:
    """The user can open a page or dialog and quit before it mounts: it lists, loads and focuses nothing then."""
    base, build = _OPENED[kind]
    mounted: list[tuple[bool, bool, bool]] = []

    class Mounting(base):
        CSS_PATH = _css_path(base)

        def on_mount(self) -> None:
            # Dispatch runs the page's own handler after this one.
            mounted.append((self._pruning, self.app.is_running, self.is_attached))

    await _push_as_the_app_exits(build(Mounting), window)
    assert mounted == [_MOUNTED_IN[window]]
