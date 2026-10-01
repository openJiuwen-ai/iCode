# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""ApprovalDialog — modal dialog for tool call approval."""

from __future__ import annotations

import contextlib
import json
from typing import TYPE_CHECKING, Any, ClassVar

from rich.markup import escape
from rich.text import Text
from textual import events, on
from textual.binding import Binding
from textual.containers import HorizontalGroup, VerticalGroup
from textual.screen import ModalScreen
from textual.widgets import Button, Select, Static, TextArea

from chrys.app.tui.behaviors.insert_clipboard import INSERT_CLIPBOARD_BINDINGS, InsertClipboardScreenMixin
from chrys.app.tui.behaviors.right_click_copy import RightClickScreenCopyMixin
from chrys.app.tui.i18n import render_str, widget_localizer
from chrys.app.tui.widgets import ChrysLoadingIndicator, EnhancedTextArea, StableAutoHeightScroll
from chrys.foundation.i18n import MessageDef, msg
from chrys.foundation.i18n.formatting import format_message
from chrys.foundation.tool_kinds import KIND_FILESYSTEM_READ, KIND_FILESYSTEM_WRITE, KIND_SHELL

if TYPE_CHECKING:
    from textual.app import ComposeResult

    from chrys.app.tui.screens.dialogs.approval.body import ApprovalBody
    from chrys.service.approval.judge import JudgeVerdict

# Keys shown as the detail line (extracted from args, not repeated in the body).
# Lookup is keyed by the chrys tool kind (``chrys.foundation.tool_kinds`` constants).
_DETAIL_KEYS: dict[str, tuple[str, ...]] = {
    KIND_SHELL: ("reason",),
    KIND_FILESYSTEM_READ: ("path",),
    KIND_FILESYSTEM_WRITE: ("path",),
}

# Rationale-style arg keys any tool may carry (remote agents included);
# tried after the kind-specific keys so e.g. shell still prefers "reason".
_DEFAULT_DETAIL_KEYS: tuple[str, ...] = ("reason", "description")

# Keys to hide from the args body (always uninteresting or redundant)
_HIDDEN_KEYS = {"reason", "max_tokens"}

# Human header labels for bridged remote (ACP) requests, keyed by the
# event's presentation_kind (``display_tool_kind`` values plus the broker's
# "remote" fallback). Unknown values also land on "Remote tool".
_RUN_COMMAND = msg("tui.approval.presentation.run_command", fallback="Run command")
_READ_FILES = msg("tui.approval.presentation.read_files", fallback="Read files")
_EDIT_FILES = msg("tui.approval.presentation.edit_files", fallback="Edit files")
_SEARCH = msg("tui.approval.presentation.search", fallback="Search")
_REMOTE_TOOL = msg("tui.approval.presentation.remote_tool", fallback="Remote tool")
_APPROVAL_REQUIRED = msg("tui.approval.required_title", fallback="Approval Required")
_EVALUATING = msg("tui.approval.evaluating", fallback="Evaluating")
_REASON_PLACEHOLDER = msg(
    "tui.approval.reason_placeholder",
    fallback="Reason (optional, sent to agent on Decline)",
)
_APPROVE = msg("tui.approval.button.approve", fallback="Approve (Y)")
_DECLINE = msg("tui.approval.button.decline", fallback="Decline (N)")
_DAA_ONCE = msg("tui.approval.daa.once", fallback="Allow once")
_DAA_EXACT_SESSION = msg("tui.approval.daa.exact_session", fallback="Remember — session")
_DAA_EXACT_PROJECT = msg("tui.approval.daa.exact_project", fallback="Remember — project")
_DAA_PREFIX_SESSION = msg("tui.approval.daa.prefix_session", fallback="Remember PREFIX — session")
_DAA_PREFIX_PROJECT = msg("tui.approval.daa.prefix_project", fallback="Remember PREFIX — project")
_DAA_DESCRIPTION = msg(
    "tui.approval.daa.description",
    fallback="EXACT remembers all arguments and execution context below. Edits apply once only.",
)
_DAA_FILES = msg(
    "tui.approval.daa.files",
    fallback="Allow further modifications to these files in the selected scope; contents may change. Edits here apply once only.",
)
_DAA_COMMAND = msg(
    "tui.approval.daa.command",
    fallback="Session remembers the normalized command below, including every argument. Project EXACT retains the full request and execution context. Edits here apply once only.",
)
_DAA_PREFIX = msg(
    "tui.approval.daa.prefix", fallback="PREFIX authorizes these tokens and any appended literal arguments: {prefix}"
)
_FLAGGED = msg("tui.approval.flagged", fallback="Flagged by Auto-Review")

_PRESENTATION_LABELS: dict[str, MessageDef] = {
    KIND_SHELL: _RUN_COMMAND,
    KIND_FILESYSTEM_READ: _READ_FILES,
    KIND_FILESYSTEM_WRITE: _EDIT_FILES,
    "search": _SEARCH,
    "remote": _REMOTE_TOOL,
}

# The bridged tool_name is ``acp:`` + the remote-chosen title (spoof-proof
# namespace, mandated by the sub-agent design §9.2) — display strips it.
_ACP_TITLE_PREFIX = "acp:"

_REMOTE_TITLE_MAX_CHARS = 160


def _detail_candidates(tool_kind: str, tool_name: str) -> tuple[str, ...]:
    """Ordered arg keys eligible for the detail line under the header."""
    keys = _DETAIL_KEYS.get(tool_kind, ())
    if not keys and tool_name in ("write_file", "edit_file"):
        keys = ("path",)
    return keys + tuple(k for k in _DEFAULT_DETAIL_KEYS if k not in keys)


def _build_detail(tool_kind: str, tool_name: str, args: dict[str, Any]) -> str:
    """Extract a single human-readable detail line from args."""
    for k in _detail_candidates(tool_kind, tool_name):
        val = args.get(k, "")
        if val:
            return str(val)
    return ""


def _build_args_lines(
    args: dict[str, Any],
    detail_key: str,
    hidden_keys: frozenset[str] | None = None,
) -> list[tuple[str, str]]:
    """Build displayable (label, value) pairs, skipping the detail key and hidden keys."""
    extra_hidden = hidden_keys or frozenset()
    skip = _HIDDEN_KEYS | extra_hidden | ({detail_key} if detail_key else set())
    lines: list[tuple[str, str]] = []
    for k, v in args.items():
        if k in skip:
            continue
        lines.append((k, str(v)))
    return lines


class _ApprovalReasonTextArea(EnhancedTextArea):
    """Optional decline reason input; Enter/Ctrl+J/Shift+Enter inserts a newline."""

    async def _on_key(self, event: events.Key) -> None:
        if event.key in ("enter", "ctrl+j", "shift+enter"):
            event.stop()
            event.prevent_default()
            start, end = self.selection
            if self._replace_via_keyboard("\n", start, end):
                self.scroll_cursor_visible()
            return
        await super()._on_key(event)


class _ApprovalButton(Button, inherit_bindings=False):
    """Approval dialog button without Enter-to-press behavior."""

    BINDINGS: ClassVar[list] = []


class ApprovalDialog(
    RightClickScreenCopyMixin,
    InsertClipboardScreenMixin,
    ModalScreen[tuple[bool, str, dict[str, Any] | None] | None],
):
    """Modal dialog for tool call approval.

    User must explicitly approve or decline — Esc is disabled.
    Dismisses with ``(approved, reason, modified_args)`` tuple.

    When ``judging=True`` (AUTO mode), the dialog shows a loading indicator
    at the bottom while the LLM judge evaluates.  Call ``receive_verdict()``
    to deliver the result: approved verdicts auto-dismiss; flagged verdicts
    display the reason and let the user decide.  The user can always click
    Approve/Decline at any time, even while the judge is still running.
    """

    BINDINGS: ClassVar[list] = [
        Binding("escape", "noop", show=False, priority=True),
        Binding("left", "switch_focus", show=False),
        Binding("right", "switch_focus", show=False),
        Binding("y,Y", "approve", show=False),
        Binding("n,N", "decline", show=False),
        *INSERT_CLIPBOARD_BINDINGS,
    ]

    CSS_PATH = "approval.tcss"

    def __init__(
        self,
        caller_name: str,
        tool_name: str,
        tool_kind: str = "",
        args: dict[str, Any] | None = None,
        judging: bool = False,
        approval_body: ApprovalBody | None = None,
        presentation_kind: str = "",
        daa_exact: str = "",
        daa_prefix: tuple[str, ...] = (),
    ) -> None:
        self._daa_exact = daa_exact
        self._daa_prefix = daa_prefix
        self._daa_description = _DAA_DESCRIPTION
        self._daa_session = True
        self._daa_project = True
        if daa_exact:
            try:
                details = json.loads(daa_exact)
            except ValueError:
                details = {}
            if "file_paths" in details:
                self._daa_description = _DAA_FILES
            elif "command_session" in details:
                self._daa_description = _DAA_COMMAND
                self._daa_session = details["command_session"] is not None
                self._daa_project = details.get("project_exact", False)
        self.daa_choice = ""
        self._tool_name = tool_name
        self._tool_kind = tool_kind
        self._presentation_kind = presentation_kind
        self._args = args or {}
        self._caller_name = caller_name
        self._judging = judging
        self._approval_body = approval_body
        body_detail = approval_body.detail if approval_body is not None else None
        # Bridged (ACP) requests publish tool_kind="" and carry the chrys
        # kind in presentation_kind — fall back so e.g. a remote
        # filesystem.read still gets its path as the detail line.
        detail_kind = tool_kind or presentation_kind
        self._detail = body_detail if body_detail is not None else _build_detail(detail_kind, tool_name, self._args)
        # Find which key was used for the detail line so we can skip it in body
        self._detail_key = ""
        if isinstance(self._detail, str) and self._detail:
            for k in _detail_candidates(detail_kind, tool_name):
                if str(self._args.get(k, "")) == self._detail:
                    self._detail_key = k
                    break
        hidden_keys = approval_body.hidden_arg_keys if approval_body is not None else frozenset()
        self._arg_lines = _build_args_lines(self._args, self._detail_key, hidden_keys)
        self._dismissed = False
        self._dismiss_completed = False
        self._dismiss_result: tuple[bool, str, dict[str, Any] | None] | None = None
        self._user_decision_submitted = False
        self._dismiss_on_resume = False
        super().__init__()

    def compose(self) -> ComposeResult:
        localizer = widget_localizer(self)
        with VerticalGroup(id="approval-container") as container:
            container.border_title = Text(render_str(localizer, _APPROVAL_REQUIRED.bind()))
            if self._caller_name:
                container.border_subtitle = Text(self._caller_name)
            # Scrollable body — grows to fill, scrolls when tall.
            with StableAutoHeightScroll(id="approval-inner", can_focus=False):
                if self._presentation_kind:
                    label_definition = _PRESENTATION_LABELS.get(self._presentation_kind, _REMOTE_TOOL)
                    label = render_str(localizer, label_definition.bind())
                    yield Static(
                        f"[reverse] {escape(label)} [/reverse]",
                        id="approval-tool",
                        markup=True,
                    )
                    if remote_title := self._novel_remote_title():
                        yield Static(Text(remote_title), id="approval-remote-title")
                else:
                    yield Static(
                        f"[reverse] {escape(self._tool_name)} [/reverse]",
                        id="approval-tool",
                        markup=True,
                    )
                if self._detail:
                    detail = self._detail if isinstance(self._detail, str) else render_str(localizer, self._detail)
                    yield Static(Text(detail), id="approval-detail")
                if self._approval_body is not None:
                    yield from self._approval_body.widgets
                if self._arg_lines:
                    with VerticalGroup(id="approval-args"):
                        for label, value in self._arg_lines:
                            arg_box = Static(Text(value), classes="approval-arg-box")
                            arg_box.border_title = Text(label)
                            yield arg_box
                if self._daa_exact:
                    yield Static(Text(render_str(localizer, self._daa_description.bind())))
                    yield Static(Text(self._daa_exact), id="daa-exact")
                    if self._daa_prefix:
                        yield Static(
                            Text(render_str(localizer, _DAA_PREFIX.bind(prefix=repr(list(self._daa_prefix))))),
                            id="daa-prefix",
                        )
            # Docked footer — separator + judge + buttons always pinned to bottom.
            with VerticalGroup(id="approval-footer"):
                separator = Static("\u2500" * 200, id="approval-separator", markup=False)
                yield separator
                separator.display = self._judging
                with VerticalGroup(id="approval-judge") as judge_area:
                    judge_area.border_title = Text(render_str(localizer, _EVALUATING.bind()))
                    yield ChrysLoadingIndicator(id="approval-judge-loading")
                    yield Static(id="approval-concern")
                judge_area.display = self._judging
                reason_input = _ApprovalReasonTextArea(
                    id="approval-reason",
                    compact=True,
                    soft_wrap=True,
                    show_line_numbers=False,
                )
                reason_input.placeholder = render_str(localizer, _REASON_PLACEHOLDER.bind())
                yield reason_input
                if self._daa_exact:
                    choices = [
                        (Text(render_str(localizer, _DAA_ONCE.bind())), ""),
                    ]
                    if self._daa_session:
                        choices.append((Text(render_str(localizer, _DAA_EXACT_SESSION.bind())), "EXACT_SESSION"))
                    if self._daa_project:
                        choices.append((Text(render_str(localizer, _DAA_EXACT_PROJECT.bind())), "EXACT_PROJECT"))
                    if self._daa_prefix:
                        choices.extend(
                            [
                                (Text(render_str(localizer, _DAA_PREFIX_SESSION.bind())), "PREFIX_SESSION"),
                                (Text(render_str(localizer, _DAA_PREFIX_PROJECT.bind())), "PREFIX_PROJECT"),
                            ]
                        )
                    yield Select(choices, value="", allow_blank=False, id="daa-choice")
                with HorizontalGroup(id="approval-buttons"):
                    yield _ApprovalButton(
                        Text(render_str(localizer, _APPROVE.bind())),
                        id="approval-yes",
                        variant="success",
                        flat=True,
                    )
                    yield _ApprovalButton(
                        Text(render_str(localizer, _DECLINE.bind())),
                        id="approval-no",
                        variant="error",
                        flat=True,
                    )

    def _novel_remote_title(self) -> str:
        """Remote-chosen title, only when it adds information beyond the body.

        For shell-style requests the remote title is the command itself and
        already fills an arg box — repeating it as a header is pure noise.
        Probe with a bounded prefix so the broker's over-2000-char digest
        suffix can never defeat the containment check.
        """
        title = self._tool_name.removeprefix(_ACP_TITLE_PREFIX).strip().strip("`").strip()
        if not title:
            return ""
        definition = _PRESENTATION_LABELS.get(self._presentation_kind, _REMOTE_TOOL)
        english_label = format_message(definition.bind())
        localized_label = render_str(widget_localizer(self), definition.bind())
        if title.casefold() in {english_label.casefold(), localized_label.casefold()}:
            # A title that merely echoes the header chip adds nothing.
            return ""
        probe = title[:120]
        haystacks = [str(value) for value in self._args.values()]
        if self._detail:
            detail = self._detail if isinstance(self._detail, str) else render_str(widget_localizer(self), self._detail)
            haystacks.append(detail)
        if any(probe in haystack for haystack in haystacks):
            return ""
        if len(title) > _REMOTE_TITLE_MAX_CHARS:
            return title[: _REMOTE_TITLE_MAX_CHARS - 1] + "…"
        return title

    def on_mount(self) -> None:
        self.query_one("#approval-yes", Button).focus()
        if self._dismissed:
            # The dialog counts as mounted only once its mount handlers have returned.
            self.call_later(self._dismiss_if_top)

    @property
    def is_dismissed(self) -> bool:
        """True once the dialog has been dismissed (approve/decline/verdict)."""
        return self._dismissed

    @property
    def user_decision_submitted(self) -> bool:
        """True once the user has clicked Approve or Decline."""
        return self._user_decision_submitted

    def _safe_dismiss(
        self,
        result: tuple[bool, str, dict[str, Any] | None] | None,
        *,
        user_decision: bool = False,
    ) -> None:
        """Dismiss the dialog at most once, preventing double-dismiss races.

        Hides the container synchronously before calling ``dismiss()`` so the
        user gets instant visual feedback: Textual's ``dismiss`` schedules the
        result callback via ``call_next`` and the DOM removal as an
        ``AwaitComplete`` — until those run, the dialog stays visible and any
        further button clicks hit this same guard and silently no-op.  Hiding
        the container here guarantees the dialog disappears the moment the
        verdict lands, even when the judge auto-approves from an event-bus
        handler (where the Textual message pump might be busy).
        """
        if self._dismissed:
            return
        self._dismissed = True
        self._dismiss_result = result
        self._user_decision_submitted = user_decision
        with contextlib.suppress(Exception):
            self.query_one("#approval-container").display = False
        self._dismiss_if_top()

    def _dismiss_if_top(self) -> None:
        if self._dismiss_completed:
            return
        if not self.is_mounted:
            # Removing a dialog that is still composing mounts its widgets without their children.
            return
        is_top = False
        with contextlib.suppress(Exception):
            is_top = self.app.screen is self
        if not is_top:
            self._dismiss_on_resume = True
            return
        self._dismiss_on_resume = False
        self._dismiss_completed = True
        self.dismiss(self._dismiss_result)

    def dismiss_due_to_cancellation(self) -> None:
        """Close an abandoned request without manufacturing a user decision."""
        self._safe_dismiss(None)

    def on_screen_resume(self, _event: events.ScreenResume) -> None:
        if self._dismiss_on_resume:
            self._dismiss_on_resume = False
            # Resume is queued; another modal may already cover us again.
            self._dismiss_if_top()

    def receive_verdict(self, verdict: JudgeVerdict) -> None:
        """Deliver the LLM judge result to the dialog.

        Called by the event handler once the judge finishes.  If the verdict
        approves the tool call, the dialog auto-dismisses.  Otherwise it
        shows the concern and lets the user decide.

        If the user already clicked Approve/Decline, this is a no-op.
        """
        if self._dismissed:
            return

        if verdict.approved:
            self._safe_dismiss((True, "", None))
            return

        # Flagged — hide spinner, show concern, switch border to error
        judge_area = self.query_one("#approval-judge", VerticalGroup)
        judge_area.border_title = Text(render_str(widget_localizer(self), _FLAGGED.bind()))
        judge_area.add_class("judge-flagged")
        self.query_one("#approval-judge-loading").display = False
        concern_widget = self.query_one("#approval-concern", Static)
        concern_widget.update(Text(verdict.reason))
        concern_widget.display = True

    @on(TextArea.Changed, "#approval-reason")
    def _on_reason_changed(self, event: TextArea.Changed) -> None:
        self._refresh_approve_disabled(event.text_area.text)

    def _refresh_approve_disabled(self, raw_reason: str) -> None:
        approve = self.query_one("#approval-yes", Button)
        should_disable = bool(raw_reason)
        approve_had_focus = approve.has_focus
        approve.disabled = should_disable
        if should_disable and approve_had_focus:
            self.query_one("#approval-no", Button).focus()

    @on(Button.Pressed, "#approval-yes")
    def _on_approve(self, event: Button.Pressed) -> None:
        if event.button.disabled:
            return
        modified_args_fn = self._approval_body.modified_args if self._approval_body is not None else None
        modified_args = modified_args_fn() if modified_args_fn is not None else None
        reason = self.query_one("#approval-reason", _ApprovalReasonTextArea).text.strip()
        if self._daa_exact and not modified_args:
            selected = self.query_one("#daa-choice", Select).value
            self.daa_choice = selected if isinstance(selected, str) else ""
        self._safe_dismiss((True, reason, modified_args), user_decision=True)

    @on(Button.Pressed, "#approval-no")
    def _on_decline(self, event: Button.Pressed) -> None:
        reason = self.query_one("#approval-reason", _ApprovalReasonTextArea).text.strip()
        self._safe_dismiss((False, reason, None), user_decision=True)

    def action_switch_focus(self) -> None:
        yes = self.query_one("#approval-yes", Button)
        no = self.query_one("#approval-no", Button)
        if yes.has_focus:
            no.focus()
        else:
            yes.focus()

    def action_approve(self) -> None:
        approve = self.query_one("#approval-yes", Button)
        if not approve.disabled:
            approve.press()

    def action_decline(self) -> None:
        self.query_one("#approval-no", Button).press()

    def action_noop(self) -> None:
        """Swallow Esc — user must explicitly approve or decline."""
