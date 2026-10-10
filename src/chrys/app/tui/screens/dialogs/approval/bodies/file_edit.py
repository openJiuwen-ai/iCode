# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Approval dialog bodies for filesystem write tools."""

from __future__ import annotations

import asyncio
import os
from contextlib import suppress
from typing import TYPE_CHECKING, Any

from rich.text import Text
from textual.containers import VerticalGroup
from textual.message import Message
from textual.widgets import Static

from chrys.app.tui.i18n import render_str, render_text, widget_localizer
from chrys.app.tui.screens.dialogs.approval.body import ApprovalBody, ApprovalBypass, register_approval_body
from chrys.foundation.i18n import DisplayBlock, DisplayPath, MessageRef, msg
from chrys.foundation.i18n.formatting import format_message
from chrys.foundation.tool_kinds import KIND_FILESYSTEM_WRITE
from chrys.service.tools.builtins.filesystem import FileToolPreviewError, plan_edit_file, plan_write_file

if TYPE_CHECKING:
    from textual.app import ComposeResult

    from chrys.foundation.i18n import Localizer


_PREPARING_DIFF = msg("tui.approval.file_edit.preparing_diff", fallback="Preparing diff for {path}...")
_PREPARE_DIFF_ERROR = msg(
    "tui.approval.file_edit.prepare_diff_error",
    fallback="Error: failed to prepare diff — {detail}",
    multiline=True,
)
_PLANNED_DIFF = msg("tui.approval.file_edit.planned_diff", fallback="Planned Diff")
_CONTENT = msg("tui.approval.file_edit.content", fallback="Content")
_REPLACEMENTS = msg(
    "tui.approval.file_edit.replacements",
    fallback="{count} replacement",
    plural_fallback="{count} replacements",
)

type LocalizedText = MessageRef | str


class ApprovalDiffPreview(VerticalGroup):
    """Prepare and mount a unified ``DiffView`` inside an approval dialog."""

    class Ready(Message):
        """The diff or its preparation error is ready for display."""

    def __init__(
        self,
        path: str,
        before: str,
        after: str,
        *,
        title: LocalizedText,
        subtitle: LocalizedText = "",
    ) -> None:
        super().__init__(classes="approval-diff-preview")
        self.is_ready = False
        self._path = path
        self._before = before
        self._after = after
        self._display_path = os.path.basename(path) or path
        self._title = title
        self._subtitle = subtitle
        self.border_title = Text(title if isinstance(title, str) else format_message(title))
        if subtitle:
            self.border_subtitle = Text(subtitle if isinstance(subtitle, str) else format_message(subtitle))

    def compose(self) -> ComposeResult:
        localizer = widget_localizer(self)
        self.border_title = Text(_render_value(localizer, self._title))
        if self._subtitle:
            self.border_subtitle = Text(_render_value(localizer, self._subtitle))
        yield Static(
            render_text(localizer, _PREPARING_DIFF.bind(path=DisplayPath(self._display_path))),
            classes="approval-diff-placeholder",
        )

    def on_mount(self) -> None:
        self.run_worker(self._prepare_and_mount(), exclusive=True, group="approval-diff")

    async def _prepare_and_mount(self) -> None:
        from chrys.app.tui.widgets.diff_view import DiffView

        dv = DiffView(self._path, self._path, self._before, self._after)
        dv.split = False
        dv.auto_height = True
        try:
            await dv.prepare()
        except Exception as exc:
            with suppress(Exception):
                self.query_one(".approval-diff-placeholder", Static).update(
                    render_text(
                        widget_localizer(self),
                        _PREPARE_DIFF_ERROR.bind(detail=DisplayBlock(str(exc))),
                    )
                )
                self._mark_ready()
            return
        with suppress(Exception):
            await self.mount(dv)
            await self.query_one(".approval-diff-placeholder", Static).remove()
            self._mark_ready()

    def _mark_ready(self) -> None:
        self.is_ready = True
        self.post_message(self.Ready())


def _string_arg(args: dict[str, Any], key: str) -> str | None:
    value = args.get(key)
    return value if isinstance(value, str) else None


def _render_value(localizer: Localizer, value: LocalizedText) -> str:
    if isinstance(value, str):
        return value
    return render_str(localizer, value)


def _bool_arg(args: dict[str, Any], key: str, default: bool = False) -> bool:
    value = args.get(key, default)
    return value if isinstance(value, bool) else default


def _bypass(error: FileToolPreviewError) -> ApprovalBody:
    return ApprovalBody(
        bypass=ApprovalBypass(
            approved=True,
            reason="",
            debug_reason=error.kind,
        )
    )


async def _build_write_file_body(
    tool_kind: str,
    args: dict[str, Any],
    workspace_cwd: str | None = None,
) -> ApprovalBody | None:
    if tool_kind != KIND_FILESYSTEM_WRITE:
        return None

    path = _string_arg(args, "path")
    content = _string_arg(args, "content")
    if path is None or content is None:
        return ApprovalBody(
            bypass=ApprovalBypass(
                approved=True,
                debug_reason="invalid_args",
            )
        )

    overwrite = _bool_arg(args, "overwrite")
    plan = await asyncio.to_thread(plan_write_file, path, content, overwrite, base_cwd=workspace_cwd or None)
    if isinstance(plan, FileToolPreviewError):
        return _bypass(plan)

    before = "" if plan.content_only_reason else plan.before_display_content
    title = _PLANNED_DIFF.bind() if plan.overwrites_existing and not plan.content_only_reason else _CONTENT.bind()
    return ApprovalBody(
        widgets=[
            ApprovalDiffPreview(
                plan.resolved_path,
                before,
                plan.display_content,
                title=title,
                subtitle=plan.content_only_reason,
            )
        ],
        hidden_arg_keys=frozenset({"content"}),
    )


async def _build_edit_file_body(
    tool_kind: str,
    args: dict[str, Any],
    workspace_cwd: str | None = None,
) -> ApprovalBody | None:
    if tool_kind != KIND_FILESYSTEM_WRITE:
        return None

    path = _string_arg(args, "path")
    old_string = _string_arg(args, "old_string")
    new_string = _string_arg(args, "new_string")
    if path is None or old_string is None or new_string is None:
        return ApprovalBody(
            bypass=ApprovalBypass(
                approved=True,
                debug_reason="invalid_args",
            )
        )

    replace_all = _bool_arg(args, "replace_all")
    plan = await asyncio.to_thread(
        plan_edit_file, path, old_string, new_string, replace_all, base_cwd=workspace_cwd or None
    )
    if isinstance(plan, FileToolPreviewError):
        return _bypass(plan)

    return ApprovalBody(
        widgets=[
            ApprovalDiffPreview(
                plan.resolved_path,
                plan.before_text,
                plan.after_text,
                title=_PLANNED_DIFF.bind(),
                subtitle=_REPLACEMENTS.bind(count=plan.replacements),
            )
        ],
        hidden_arg_keys=frozenset({"old_string", "new_string"}),
    )


register_approval_body("write_file", _build_write_file_body)
register_approval_body("edit_file", _build_edit_file_body)
