# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Run input collection, separate from Chat drafts and workflow execution.

This renderer currently collects plain text. The owner receives a submitted
value only; future form renderers can use the same draft/submit boundary.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, ClassVar

from rich.text import Text
from textual import on
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.content import Content
from textual.widgets import Button, Static

from chrys.app.tui.binding_display import CANCEL_BINDING, localized_binding
from chrys.app.tui.screens.dialogs.base import BaseDialog
from chrys.app.tui.screens.dialogs.file_picker import FilePicker, FilePickerMode
from chrys.app.tui.widgets.buttons import ConfigActionButton
from chrys.app.tui.widgets.dialog_buttons import DialogButtonRow, DialogButtonSpec
from chrys.app.tui.widgets.editor import EditorIntent, MessageEditor
from chrys.app.tui.widgets.select import Select
from chrys.app.tui.widgets.workflow import text
from chrys.foundation.models.workflow_session import WorkflowModelSelection
from chrys.service.profiles.models.schema import is_model_profile_selectable

if TYPE_CHECKING:
    from textual.app import ComposeResult

    from chrys.app.tui.i18n import LocaleController
    from chrys.app.tui.screens.dialogs.file_picker import RecentPaths
    from chrys.foundation.i18n import MessageRef
    from chrys.service.profiles.models.registry import ModelProfileRegistry


@dataclass(frozen=True)
class WorkflowInputContext:
    title: str
    description: str
    source_kind: str
    working_directory: str
    has_kernel_agents: bool
    can_change_directory: bool
    directory_notice: MessageRef | None = None


@dataclass(frozen=True)
class WorkflowInputResult:
    text: str
    submitted: bool
    model_profile_id: str | None = None


class WorkflowInputDialog(BaseDialog[WorkflowInputResult]):
    BINDINGS: ClassVar[list] = [localized_binding("escape", "cancel", CANCEL_BINDING, show=False)]
    CSS_PATH = "workflow_input.tcss"

    def __init__(
        self,
        draft: str = "",
        *,
        context: WorkflowInputContext,
        change_directory: Callable[[str], Awaitable[WorkflowInputContext | None]] | None = None,
        recent_paths: RecentPaths | None = None,
        model: WorkflowModelSelection | None = None,
        model_registry: ModelProfileRegistry | None = None,
        locale_controller: LocaleController | None = None,
    ) -> None:
        super().__init__(dismiss_on_backdrop=False)
        self._draft = draft
        self._input_context = context
        self._change_directory = change_directory
        self._recent_paths = recent_paths
        self._changing_directory = False
        self._locale = locale_controller
        self._model = model
        self._profiles = (
            [profile for profile in model_registry.list_profiles() if is_model_profile_selectable(profile)]
            if model_registry is not None
            else []
        )
        self._initial_model = next(
            (
                profile.id
                for profile in self._profiles
                if model is not None
                and (profile.id, profile.name, profile.model_id) == (model.profile_id, model.name, model.model_id)
            ),
            "",
        )

    def compose(self) -> ComposeResult:
        with Vertical(id="workflow-input-dialog") as container:
            container.border_title = Text(text.render(text.START_WORKFLOW.bind(), self._locale))
            with VerticalScroll(id="workflow-input-body"):
                with Horizontal(id="workflow-input-identity"):
                    yield Static(id="workflow-input-title")
                    yield Static(id="workflow-input-source")
                yield Static(id="workflow-input-description")
                with Vertical(classes="workflow-input-section") as settings:
                    settings.border_title = Text(text.render(text.RUN_SETTINGS.bind(), self._locale))
                    models = [(Content.from_text(profile.name, markup=False), profile.id) for profile in self._profiles]
                    if self._model is not None and not self._initial_model:
                        models.insert(0, (Content.from_text(self._model.name, markup=False), ""))
                    with Horizontal(id="workflow-input-model-row", classes="workflow-input-setting"):
                        yield Static(
                            Text(text.render(text.MODEL_LABEL.bind(), self._locale)),
                            classes="workflow-input-setting-label",
                        )
                        yield Select(
                            models,
                            value=self._initial_model if self._model is not None else Select.NULL,
                            prompt=text.render(text.SELECT_MODEL.bind(), self._locale),
                            allow_blank=self._model is None,
                            disabled=not self._profiles,
                            id="workflow-input-model",
                        )
                    with Horizontal(classes="workflow-input-setting"):
                        yield Static(
                            Text(text.render(text.WORKING_DIRECTORY.bind(), self._locale)),
                            classes="workflow-input-setting-label",
                        )
                        yield Static(id="workflow-input-directory")
                        yield ConfigActionButton(
                            Text(text.render(text.CHANGE_DIRECTORY.bind(), self._locale)),
                            id="workflow-input-change-directory",
                            compact=True,
                        )
                    yield Static(id="workflow-input-directory-notice")
                    yield Static(id="workflow-input-directory-error")
                    yield Static(id="workflow-input-directory-loading")
                with Vertical(classes="workflow-input-section") as inputs:
                    inputs.border_title = Text(text.render(text.INPUT_OPTIONAL.bind(), self._locale))
                    yield Static(Text(text.render(text.INPUT_HINT.bind(), self._locale)), id="workflow-input-hint")
                    yield MessageEditor(text=self._draft, soft_wrap=True, show_line_numbers=False, id="workflow-input")
            yield DialogButtonRow(
                DialogButtonSpec(
                    Text(text.render(text.START.bind(), self._locale)), id="workflow-input-start", variant="success"
                ),
                DialogButtonSpec(
                    Text(text.render(text.CANCEL.bind(), self._locale)), id="workflow-input-cancel", variant="warning"
                ),
                id="workflow-input-actions",
            )

    def action_cancel(self) -> None:
        self._finish(False)

    def on_mount(self) -> None:
        if not self._composed_live:
            return
        self._refresh_context()
        self.query_one("#workflow-input-directory-error").display = False
        self.query_one(MessageEditor).focus()

    def _refresh_context(self) -> None:
        title = self.query_one("#workflow-input-title", Static)
        shown = text.shown(self._input_context.title)
        title.update(Text(shown))
        title.tooltip = Text(shown)
        source = text.SOURCES.get(self._input_context.source_kind)
        self.query_one("#workflow-input-source", Static).update(
            Text(text.render(source.bind(), self._locale) if source is not None else self._input_context.source_kind)
        )
        description = self.query_one("#workflow-input-description", Static)
        description.update(Text(self._input_context.description))
        description.display = bool(self._input_context.description)
        self.query_one("#workflow-input-model-row").display = self._input_context.has_kernel_agents
        directory = self.query_one("#workflow-input-directory", Static)
        directory.update(Text(self._input_context.working_directory))
        directory.tooltip = Text(self._input_context.working_directory)
        self.query_one("#workflow-input-change-directory").display = self._input_context.can_change_directory
        notice = self.query_one("#workflow-input-directory-notice", Static)
        notice.display = self._input_context.directory_notice is not None
        notice.update(
            Text(text.render(self._input_context.directory_notice, self._locale))
            if self._input_context.directory_notice is not None
            else Text()
        )

    @on(Button.Pressed, "#workflow-input-change-directory")
    def change_directory(self, event: Button.Pressed) -> None:
        event.stop()
        if self._changing_directory or not self._input_context.can_change_directory:
            return

        def selected(path: str | None) -> None:
            if path is None or path == self._input_context.working_directory or self._change_directory is None:
                return
            self._changing_directory = True
            self.query_one("#workflow-input-start", Button).disabled = True
            self.query_one("#workflow-input-change-directory", Button).disabled = True
            self.run_worker(self._apply_directory(path), group="directory", exclusive=True)

        self.app.push_screen(
            FilePicker(
                mode=FilePickerMode.FOLDER,
                initial_path=self._input_context.working_directory,
                title=text.WORKING_DIRECTORY.bind(),
                recent_paths=self._recent_paths,
            ),
            selected,
        )

    async def _apply_directory(self, path: str) -> None:
        if self._change_directory is None:
            raise RuntimeError("Changing the workflow directory requires a directory handler.")
        error = self.query_one("#workflow-input-directory-error", Static)
        error.display = False
        loading = self.query_one("#workflow-input-directory-loading", Static)
        loading.update(Text(text.render(text.LOAD_WORKFLOW.bind(name=self._input_context.title), self._locale)))
        loading.display = True
        try:
            context = await self._change_directory(path)
            if context is not None:
                self._input_context = context
                self._refresh_context()
        except (OSError, ValueError) as exc:
            error.update(Text(str(exc)))
            error.display = True
        finally:
            self._changing_directory = False
            if self.is_attached:
                loading.display = False
                self.query_one("#workflow-input-start", Button).disabled = False
                self.query_one("#workflow-input-change-directory", Button).disabled = False

    def _finish(self, submitted: bool) -> None:
        if submitted and self._changing_directory:
            return
        profile_id = self.query_one(Select).value
        changed_model = (
            profile_id
            if self._input_context.has_kernel_agents
            and isinstance(profile_id, str)
            and profile_id != self._initial_model
            else None
        )
        self.dismiss(WorkflowInputResult(self.query_one(MessageEditor).snapshot().text, submitted, changed_model))

    @on(Button.Pressed, "#workflow-input-start")
    def submit_input(self) -> None:
        self._finish(True)

    @on(Button.Pressed, "#workflow-input-cancel")
    def cancel_input(self) -> None:
        self._finish(False)

    @on(MessageEditor.IntentRequested)
    def editor_intent(self, event: MessageEditor.IntentRequested) -> None:
        event.stop()
        if event.intent is EditorIntent.ACCEPT:
            self._finish(True)
        elif event.intent in {EditorIntent.REQUEST_ESCAPE_CANCEL, EditorIntent.CANCEL_IMMEDIATE}:
            self._finish(False)
