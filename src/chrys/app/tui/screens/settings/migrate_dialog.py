# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Copy existing sessions from one sessions directory to another."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import TYPE_CHECKING, ClassVar

from rich.text import Text
from textual import on
from textual.containers import Horizontal, VerticalGroup
from textual.widgets import Button, Label, Static

from chrys.app.tui.binding_display import CLOSE_BINDING, localized_binding
from chrys.app.tui.i18n import render_str, widget_localizer
from chrys.app.tui.screens.dialogs.base import BaseDialog
from chrys.app.tui.widgets import ChrysLoadingIndicator, DialogButtonRow, DialogButtonSpec
from chrys.app.tui.widgets import EnhancedInput as Input
from chrys.foundation.branding import APP_DISPLAY_NAME
from chrys.foundation.i18n import DisplayPath, MessageRef, msg
from chrys.service.state.session_migration import MigrationReport, SessionMigrationError

if TYPE_CHECKING:
    from textual.app import ComposeResult

    from chrys.app.tui.screens.settings.ports import SessionStoragePorts

logger = logging.getLogger(__name__)

_TITLE = msg("tui.settings.migrate.title", fallback="Migrate sessions")
_FROM = msg("tui.settings.migrate.from", fallback="From")
_TO = msg("tui.settings.migrate.to", fallback="To")
_BROWSE = msg("tui.settings.migrate.browse", fallback="Browse")
_DESCRIPTION = msg(
    "tui.settings.migrate.description",
    fallback=(
        "Copies session folders that are not already present at the destination. "
        "Sessions open in a running {app} (including this one) are skipped — "
        "restart and run Migrate again to copy them. Nothing is deleted."
    ),
)
_CANCEL = msg("tui.settings.migrate.button.cancel", fallback="Cancel")
_MIGRATE = msg("tui.settings.migrate.button.migrate", fallback="Migrate")
_CLOSE = msg("tui.settings.migrate.button.close", fallback="Close")
_COPYING = msg("tui.settings.migrate.copying", fallback="Copying sessions")
_NEED_PATHS = msg("tui.settings.migrate.need_paths", fallback="Choose both a source and a destination.")
_FAILED = msg("tui.settings.migrate.failed", fallback="Migration could not run: {error}")
_NOTHING_TO_COPY = msg("tui.settings.migrate.nothing_to_copy", fallback="No sessions to copy from {path}.")
_SUMMARY = msg(
    "tui.settings.migrate.summary",
    fallback="Copied {copied} · already present {present} · active {active} · busy {busy} · failed {failed}",
)
_RESTART_HINT = msg(
    "tui.settings.migrate.restart_hint",
    fallback="Some sessions are open in a running {app} — restart and run Migrate again to copy them.",
)
_FAILED_ITEM = msg("tui.settings.migrate.failed_item", fallback="{path}: {reason}")

_MAX_FAILED_LINES = 8


class MigrateSessionsDialog(BaseDialog[None]):
    """From/To folders, one Migrate, then the report."""

    CSS_PATH = "settings.tcss"

    BINDINGS: ClassVar[list] = [
        localized_binding("escape", "close", CLOSE_BINDING, show=False, priority=True),
    ]

    def __init__(
        self,
        storage: SessionStoragePorts,
        *,
        source: Path | None,
        destination: Path | None,
    ) -> None:
        self._storage = storage
        self._source = "" if source is None else str(source)
        self._destination = "" if destination is None else str(destination)
        self._copying = False
        self._done = False
        super().__init__()

    def compose(self) -> ComposeResult:
        localizer = widget_localizer(self)
        with VerticalGroup(id="migrate-container") as container:
            container.border_title = Text(render_str(localizer, _TITLE.bind()))
            with VerticalGroup(id="migrate-body"):
                with Horizontal(classes="migrate-path-row"):
                    yield Label(render_str(localizer, _FROM.bind()), classes="migrate-path-label")
                    yield Input(value=self._source, id="migrate-from", classes="migrate-path-input")
                    yield Button(
                        render_str(localizer, _BROWSE.bind()),
                        id="migrate-browse-from",
                        classes="settings-row-link",
                        flat=True,
                    )
                with Horizontal(classes="migrate-path-row"):
                    yield Label(render_str(localizer, _TO.bind()), classes="migrate-path-label")
                    yield Input(value=self._destination, id="migrate-to", classes="migrate-path-input")
                    yield Button(
                        render_str(localizer, _BROWSE.bind()),
                        id="migrate-browse-to",
                        classes="settings-row-link",
                        flat=True,
                    )
                yield Static(
                    Text(render_str(localizer, _DESCRIPTION.bind(app=APP_DISPLAY_NAME))), id="migrate-description"
                )
                with Horizontal(id="migrate-status-line"):
                    yield Static("", id="migrate-status")
                    yield ChrysLoadingIndicator(id="migrate-loading")
            yield DialogButtonRow(
                DialogButtonSpec(Text(render_str(localizer, _MIGRATE.bind())), id="migrate-run", variant="primary"),
                DialogButtonSpec(Text(render_str(localizer, _CANCEL.bind())), id="migrate-cancel", variant="warning"),
                id="migrate-buttons",
            )

    def on_mount(self) -> None:
        if not self._composed_live:
            return
        self.query_one("#migrate-run", Button).focus()

    def _set_status(self, message: MessageRef | None, *, error: bool = False) -> None:
        status = self.query_one("#migrate-status", Static)
        status.set_class(error, "-error")
        if message is None:
            status.update("")
            status.display = False
            return
        status.display = True
        status.update(Text(render_str(widget_localizer(self), message)))

    def _set_report(self, report: MigrationReport) -> None:
        localizer = widget_localizer(self)
        lines = [
            render_str(
                localizer,
                _SUMMARY.bind(
                    copied=len(report.copied),
                    present=len(report.skipped_present),
                    active=len(report.skipped_active),
                    busy=len(report.skipped_busy),
                    failed=len(report.failed),
                ),
            )
        ]
        if report.skipped_active:
            lines.append(render_str(localizer, _RESTART_HINT.bind(app=APP_DISPLAY_NAME)))
        for path, reason in report.failed[:_MAX_FAILED_LINES]:
            lines.append(render_str(localizer, _FAILED_ITEM.bind(path=DisplayPath(path), reason=reason)))
        status = self.query_one("#migrate-status", Static)
        status.set_class(bool(report.failed), "-error")
        status.display = True
        status.update(Text("\n".join(lines)))

    def _finish(self) -> None:
        self._done = True
        self.query_one("#migrate-run", Button).disabled = True
        cancel = self.query_one("#migrate-cancel", Button)
        cancel.label = Text(render_str(widget_localizer(self), _CLOSE.bind()))
        cancel.focus()

    @on(Button.Pressed, "#migrate-cancel")
    def _on_cancel(self, event: Button.Pressed) -> None:
        event.stop()
        self.action_close()

    @on(Button.Pressed, "#migrate-browse-from")
    def _on_browse_from(self, event: Button.Pressed) -> None:
        event.stop()
        self._browse_into("#migrate-from")

    @on(Button.Pressed, "#migrate-browse-to")
    def _on_browse_to(self, event: Button.Pressed) -> None:
        event.stop()
        self._browse_into("#migrate-to")

    def _browse_into(self, selector: str) -> None:
        from chrys.app.tui.screens.dialogs.file_picker import FilePicker, FilePickerMode

        field = self.query_one(selector, Input)

        def _on_result(result: str | None) -> None:
            if result:
                field.value = result

        self.app.push_screen(
            FilePicker(mode=FilePickerMode.FOLDER, initial_path=field.value.strip() or None), _on_result
        )

    @on(Button.Pressed, "#migrate-run")
    def _on_copy(self, event: Button.Pressed) -> None:
        event.stop()
        if self._copying or self._done:
            return
        source = self.query_one("#migrate-from", Input).value.strip()
        destination = self.query_one("#migrate-to", Input).value.strip()
        if not source or not destination:
            self._set_status(_NEED_PATHS.bind(), error=True)
            return
        self._copying = True
        self._set_busy(True)
        self._set_status(_COPYING.bind())
        # A worker, not an awaited handler: awaiting here would park the
        # dialog's own message queue, and an Escape pressed mid-copy would be
        # replayed after the copy and close the report before it was read.
        self.run_worker(self._copy(source, destination), exclusive=True)

    async def _copy(self, source: str, destination: str) -> None:
        report: MigrationReport | None = None
        try:
            plan = await asyncio.to_thread(
                self._storage.plan_migration, Path(source).expanduser(), Path(destination).expanduser()
            )
            if plan.items or plan.rejected:
                report = await self._storage.run_migration(plan)
        except SessionMigrationError as exc:
            self._set_status(_FAILED.bind(error=str(exc)), error=True)
            return
        except Exception as exc:
            logger.warning("Session migration failed", exc_info=True)
            self._set_status(_FAILED.bind(error=str(exc)), error=True)
            return
        finally:
            self._copying = False
            self._set_busy(False)
        if report is None:
            self._set_status(_NOTHING_TO_COPY.bind(path=DisplayPath(plan.source_dir)))
        else:
            self._set_report(report)
        self._finish()

    def _set_busy(self, busy: bool) -> None:
        """While copying nothing can be pressed or edited; there is no cancel mid-copy."""
        for selector in ("#migrate-run", "#migrate-cancel", "#migrate-browse-from", "#migrate-browse-to"):
            self.query_one(selector, Button).disabled = busy
        for selector in ("#migrate-from", "#migrate-to"):
            self.query_one(selector, Input).disabled = busy
        self.query_one("#migrate-loading").display = busy

    def _allow_click_outside_dismiss(self) -> bool:
        return not self._copying

    def action_close(self) -> None:
        if self._copying:
            return
        self.dismiss(None)


__all__ = ["MigrateSessionsDialog"]
