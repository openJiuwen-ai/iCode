# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tool call widgets — ToolCall (individual) and ToolGroup (collapsible container).

ToolGroup groups consecutive tool calls within a single agent turn.
Parallel tool calls appear together, each with its own spinner/status.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from collections.abc import Callable, Sequence
from contextlib import suppress
from dataclasses import dataclass, field, replace
from functools import partial
from typing import TYPE_CHECKING, Any, ClassVar, Literal, Protocol, cast, runtime_checkable

from rich.style import Style
from rich.text import Text
from textual.containers import Horizontal, Vertical
from textual.css.query import NoMatches
from textual.message import Message
from textual.reactive import reactive
from textual.selection import Selection
from textual.timer import Timer
from textual.widget import Widget
from textual.widgets import Static

from chrys.app.tui.clipboard import OSC52_COPY_MAX_BYTES, copy_text_to_clipboards, terminal_clipboard_size
from chrys.app.tui.copy_messages import COPIED_TITLE
from chrys.app.tui.i18n import render_str, render_text, widget_localizer
from chrys.app.tui.support.gc_freeze import (
    GcAbsorbReason,
    GcAbsorbRequested,
    GcReclaimReason,
    GcReclaimRequested,
    GcRemovedContent,
    current_gc_freeze_epoch,
)
from chrys.app.tui.util.spinner import SPINNER_FRAMES
from chrys.app.tui.util.static_update import update_static_in_place
from chrys.app.tui.util.visibility import is_widget_shown_on_active_screen
from chrys.app.tui.widgets.chat.file_snapshot import FileSnapshotPayload
from chrys.app.tui.widgets.chat.fold_gap import FoldGap
from chrys.app.tui.widgets.chat.fold_rows import (
    FOLDED_ARROW,
    OPEN_ARROW,
    TOOLS_LABEL,
    FoldRow,
    FoldRowStyles,
    ToolOutcome,
    ToolTally,
    duration_words,
    fit_fold_row,
    tool_category,
)
from chrys.app.tui.widgets.chat.process_visibility import ProcessMember
from chrys.app.tui.widgets.chat.single_call_row import SingleCallRow, single_call_row
from chrys.app.tui.widgets.chat.tool_view_builders import (
    TOOL_VIEW_EMPTY,
    TOOL_VIEW_OUTPUT,
    ToolViewImage,
    build_code_view,
    build_output_views,
    build_params_view,
)
from chrys.app.tui.widgets.chat.transcript_panel import note_fold
from chrys.app.tui.widgets.click_affordance import ClickAffordance
from chrys.app.tui.widgets.loading import RunningTitle
from chrys.foundation.i18n import Localizer, MessageRef, msg
from chrys.foundation.i18n.formatting import format_message, sanitize_legacy_block, sanitize_legacy_scalar
from chrys.foundation.patches.textual_precompose import precompose_tree
from chrys.foundation.platform.files import surrogate_safe_text
from chrys.foundation.tool_kinds import (
    KIND_ASK_USER,
    KIND_FILESYSTEM_READ,
    KIND_FILESYSTEM_WRITE,
    KIND_SEARCH,
    KIND_SHELL,
    KIND_SKILL,
    KIND_SLEEP,
    KIND_SUB_AGENT,
)
from chrys.foundation.tool_result_metadata import (
    legacy_error_text_applies,
    result_text_exit_code,
    shell_exit_code_from_metadata,
    shell_timed_out_from_metadata,
    tool_result_metadata_failure_state,
    tool_result_metadata_is_rejected,
)
from chrys.service.tools.builtins.sleep import MAX_SLEEP_SECONDS, normalize_sleep_seconds

# Protocol attributes expected on any tool widget (BaseToolCard or custom renderer):
#   call_id, tool_name, status, result_text
#   set_complete(result, duration_ms), set_error(error), update_args(args)

ToolRenderStatus = Literal["complete", "error", "rejected"]
logger = logging.getLogger(__name__)

_TOOL_DETAILS_COPIED = msg(
    "tui.tool.copy.details",
    fallback="Copied {tool_name} execution details",
)
_TOOL_DETAILS_COPIED_TOO_LARGE = msg(
    "tui.tool.copy.details_too_large",
    fallback="Copied {tool_name} execution details (terminal clipboard skipped: payload too large)",
)
_TOOL_DETAILS_COPIED_UNAVAILABLE = msg(
    "tui.tool.copy.details_unavailable",
    fallback="Copied {tool_name} execution details (terminal clipboard unavailable)",
)

TOOL_CARD_ACTION_VIEW = msg("tui.tool_card.action.view", fallback="view")
TOOL_CARD_ACTION_COPY = msg("tui.tool_card.action.copy", fallback="copy")
TOOL_CARD_ACTION_VIEW_TOOLTIP = msg(
    "tui.tool_card.action.view_tooltip",
    fallback="View full tool input and output",
)
TOOL_CARD_ACTION_COPY_TOOLTIP = msg(
    "tui.tool_card.action.copy_tooltip",
    fallback="Copy tool execution details",
)
TOOL_CARD_RUNNING = msg("tui.tool_card.status.running", fallback="running")
TOOL_CARD_APPROVED = msg("tui.tool_card.status.approved", fallback="approved")
TOOL_CARD_REJECTED = msg("tui.tool_card.status.rejected", fallback="Rejected")
TOOL_CARD_ERRORED = msg("tui.tool_card.status.errored", fallback="Errored")
TOOL_CARD_ERRORED_WITH_CODE = msg(
    "tui.tool_card.status.errored_with_code",
    fallback="Errored {code}",
)
TOOL_CARD_COMPLETED = msg("tui.tool_card.status.completed", fallback="Completed")
TOOL_CARD_INTERRUPTED = msg("tui.tool_card.status.interrupted", fallback="Interrupted")
TOOL_CARD_SKIPPED = msg("tui.tool_card.status.skipped", fallback="Skipped")
_TOOL_CARD_IMAGE_RESOLUTION = msg(
    "tui.tool_card.image.resolution",
    fallback="Resolution: {width}x{height}",
)
_TOOL_CARD_IMAGE_TYPE = msg("tui.tool_card.image.type", fallback="Type: {media_type}")
_TOOL_ACTIVITY_RUNNING = msg("tui.tool_card.group.activity.running", fallback="Running {tool}")
_TOOL_ACTIVITY_SHELL = msg("tui.tool_card.group.activity.shell", fallback="Running shell command")
_TOOL_ACTIVITY_READING = msg("tui.tool_card.group.activity.reading", fallback="Reading {path}")
_TOOL_ACTIVITY_WRITING = msg("tui.tool_card.group.activity.writing", fallback="Writing {path}")
_TOOL_ACTIVITY_EDITING = msg("tui.tool_card.group.activity.editing", fallback="Editing {path}")
_TOOL_ACTIVITY_SEARCHING = msg("tui.tool_card.group.activity.searching", fallback="Searching for {pattern}")
_TOOL_ACTIVITY_FINDING = msg("tui.tool_card.group.activity.finding", fallback="Finding {pattern}")
_TOOL_ACTIVITY_LOADING_SKILL = msg("tui.tool_card.group.activity.loading_skill", fallback="Loading skill {skill}")
_TOOL_ACTIVITY_READING_SKILL = msg(
    "tui.tool_card.group.activity.reading_skill",
    fallback="Reading skill resource {resource}",
)
_TOOL_ACTIVITY_RUNNING_SKILL = msg(
    "tui.tool_card.group.activity.running_skill",
    fallback="Running skill script {script}",
)
_TOOL_ACTIVITY_WAITING_FOR_ANSWER = msg(
    "tui.tool_card.group.activity.waiting_for_answer",
    fallback="Waiting for your answer",
)
_TOOL_ACTIVITY_RUNNING_AGENT = msg("tui.tool_card.group.activity.running_agent", fallback="Running {agent}")
_TOOL_ACTIVITY_AGENT_DETAIL = msg(
    "tui.tool_card.group.activity.agent_detail",
    fallback="Running {agent}: {activity}",
)
_TOOL_ACTIVITY_SLEEPING = msg("tui.tool_card.group.activity.sleeping", fallback="Sleeping")
_TOOL_ACTIVITY_SLEEPING_REASON = msg("tui.tool_card.group.activity.sleeping_reason", fallback="Sleeping: {reason}")
_TOOL_ACTIVITY_SLEEP_LEFT = msg("tui.tool_card.group.activity.sleep_left", fallback="{duration} left")


@runtime_checkable
class _ToolArgsUpdatable(Protocol):
    def update_args(self, args: dict[str, Any]) -> None:
        """Refresh the displayed arguments for a mounted tool widget."""


@runtime_checkable
class _ToolProgressUpdatable(Protocol):
    def update_progress(self, lines: list[str]) -> None:
        """Refresh the displayed streaming progress for a mounted tool widget."""


@runtime_checkable
class _ToolStartRestorable(Protocol):
    def restore_started_at(self, started_at: float) -> None:
        """Count a running call's time from when it started (``time.monotonic()``), not from when the widget was built."""


@runtime_checkable
class _HostedStatusUpdatable(Protocol):
    def update_hosted_status(self, status: str, provider_status: str) -> None:
        """Refresh provider-hosted lifecycle text."""


@runtime_checkable
class _ToolDisplayStateSerializable(Protocol):
    def compact_display_state(self) -> dict[str, Any] | None:
        """Return compact renderer-specific state needed to rebuild the visible card."""


@runtime_checkable
class _ToolDisplayStateRestorable(Protocol):
    def restore_compact_display_state(self, state: dict[str, Any]) -> None:
        """Restore compact renderer-specific state before terminal rendering."""


@runtime_checkable
class _ToolErrorPayloadRestorable(Protocol):
    def restore_error_payload(
        self,
        error: str,
        *,
        image_contents: list[Any],
        metadata: dict[str, Any] | None,
        artifacts: list[dict[str, Any]],
    ) -> None:
        """Restore structured output retained before a terminal failure."""


@runtime_checkable
class _ToolPendingDiffMountable(Protocol):
    async def mount_diff_if_pending(self) -> bool:
        """Mount an expensive inline diff that was deferred while collapsed."""


@runtime_checkable
class _ToolPendingContentMountable(Protocol):
    async def mount_pending_content(self) -> bool:
        """Mount expensive inline content that was deferred while collapsed."""


@runtime_checkable
class _ToolCollapsedContentReleasable(Protocol):
    def release_collapsed_content(self) -> None:
        """Release expensive inline content after the parent group collapses."""


class _CompletableToolRenderer(Protocol):
    """Completion surface required from every registered tool renderer."""

    status: str

    def set_complete(self, result: str, duration_ms: int = 0, **kwargs: Any) -> None: ...

    def set_error(self, error: str) -> None: ...


def _completion_renderer(widget: Widget) -> _CompletableToolRenderer:
    """View a registered Widget through the renderer's trusted structural contract."""
    return cast("_CompletableToolRenderer", widget)


class ToolViewRequested(Message):
    """Request that the owning screen open a detailed tool-view modal."""

    def __init__(
        self,
        *,
        title: str,
        input_widgets: list[Widget],
        output_widgets: list[Widget],
        raw_input: str = "",
        raw_output: str = "",
        copy_enabled: bool = True,
        initial_tab: str = "input",
    ) -> None:
        super().__init__()
        self.title = title
        self.input_widgets = input_widgets
        self.output_widgets = output_widgets
        self.raw_input = raw_input
        self.raw_output = raw_output
        self.copy_enabled = copy_enabled
        self.initial_tab = initial_tab


@dataclass
class _NestedToolActivity:
    """Latest presentation state for one tool running inside a sub-agent."""

    tool_name: str
    tool_kind: str
    args: dict[str, Any]


@dataclass
class _ToolRecord:
    """Minimal state needed to rebuild a completed collapsed tool widget."""

    call_id: str
    tool_name: str
    tool_kind: str
    args_summary: str
    args: dict[str, Any] | None
    status: str = "running"
    result: str = ""
    duration_ms: int = 0
    duration_known: bool = False
    timestamp: str = ""
    replay_timing: bool = False
    file_snapshot: FileSnapshotPayload | None = None
    approval: str | None = None
    metadata: dict[str, Any] | None = None
    image_contents: list[Any] = field(default_factory=list, repr=False, compare=False)
    lazy: bool = False
    display_state: dict[str, Any] | None = None
    completed_via_result: bool = False
    provider_hosted: bool = False
    hosted_family: str = ""
    provider: str = ""
    provider_item_type: str = ""
    provider_status: str = ""
    provider_call_id: str = ""
    canonical_status: str = "running"
    artifacts: list[dict[str, Any]] = field(default_factory=list)
    activity_order: int = 0
    nested_activities: dict[str, _NestedToolActivity] = field(default_factory=dict)
    nested_latest_activity: _ToolActivity | None = None
    started_at: float = field(default_factory=time.monotonic)
    """When the call started, by ``time.monotonic()``: a running sleep counts down from it."""


def _activity_arg(args: dict[str, Any] | None, key: str, *, path: bool = False) -> str:
    """Return one single-line, markup-safe activity argument."""
    if not args or not isinstance(value := args.get(key), str) or not value:
        return ""
    if path:
        value = surrogate_safe_text(value)
    return sanitize_legacy_scalar(value)


type _ToolActivity = MessageRef | str


_ACTIVITY_ARG_KEYS = frozenset(
    {
        "agent",
        "agent_name",
        "path",
        "pattern",
        "reason",
        "resource_name",
        "script_name",
        "skill_name",
    }
)


def _compact_activity_args(args: dict[str, Any] | None) -> dict[str, Any]:
    """Keep only string fields that can appear in an activity title."""
    if not args:
        return {}
    return {key: value for key, value in args.items() if key in _ACTIVITY_ARG_KEYS and isinstance(value, str)}


def _tool_activity_reference(tool_name: str, tool_kind: str, args: dict[str, Any] | None) -> _ToolActivity:
    """Build localized activity prose for a running tool record."""
    safe_name = sanitize_legacy_scalar(tool_name)
    if tool_kind == KIND_SHELL:
        if reason := _activity_arg(args, "reason"):
            return reason
        return _TOOL_ACTIVITY_SHELL.bind()
    if tool_kind == KIND_FILESYSTEM_READ and (path := _activity_arg(args, "path", path=True)):
        return _TOOL_ACTIVITY_READING.bind(path=path)
    if tool_kind == KIND_FILESYSTEM_WRITE and (path := _activity_arg(args, "path", path=True)):
        reference = _TOOL_ACTIVITY_EDITING if tool_name == "edit_file" else _TOOL_ACTIVITY_WRITING
        return reference.bind(path=path)
    if tool_kind == KIND_SEARCH and (pattern := _activity_arg(args, "pattern")):
        reference = _TOOL_ACTIVITY_FINDING if tool_name == "glob" else _TOOL_ACTIVITY_SEARCHING
        return reference.bind(pattern=pattern)
    if tool_kind == KIND_SKILL:
        skill = _activity_arg(args, "skill_name")
        if tool_name == "load_skill" and skill:
            return _TOOL_ACTIVITY_LOADING_SKILL.bind(skill=skill)
        if tool_name == "read_skill_resource":
            resource = _activity_arg(args, "resource_name")
            if skill and resource:
                resource = f"{skill}/{resource}"
            if resource := resource or skill:
                return _TOOL_ACTIVITY_READING_SKILL.bind(resource=resource)
        if tool_name == "run_skill_script":
            script = _activity_arg(args, "script_name")
            if skill and script:
                script = f"{skill}/{script}"
            if script := script or skill:
                return _TOOL_ACTIVITY_RUNNING_SKILL.bind(script=script)
    if tool_kind == KIND_ASK_USER:
        return _TOOL_ACTIVITY_WAITING_FOR_ANSWER.bind()
    if tool_kind == KIND_SLEEP:
        return _sleep_activity(args)
    if tool_kind == KIND_SUB_AGENT:
        agent = _activity_arg(args, "agent_name") or _activity_arg(args, "agent") or safe_name
        return _TOOL_ACTIVITY_RUNNING_AGENT.bind(agent=agent)
    return _TOOL_ACTIVITY_RUNNING.bind(tool=safe_name)


def _sleep_activity(args: dict[str, Any] | None) -> MessageRef:
    """What a sleep call does: it sleeps, and why when it says."""
    if reason := _activity_arg(args, "reason"):
        return _TOOL_ACTIVITY_SLEEPING_REASON.bind(reason=reason)
    return _TOOL_ACTIVITY_SLEEPING.bind()


def tool_activity_reference(tool_name: str, tool_kind: str, args: dict[str, Any] | None) -> _ToolActivity:
    """Return the shared semantic activity used by group and nested-agent summaries."""
    return _tool_activity_reference(tool_name, tool_kind, args)


@dataclass(frozen=True)
class _SleepCountdown:
    """A running sleep call's time left: the seconds it asked for, counted from when it started."""

    started_at: float
    seconds: int
    activity: MessageRef
    """What it does, as its title's head says it: rendered with the time left, in the same locale."""

    def seconds_left(self) -> int:
        """The whole seconds it has left now, as its card counts them."""
        return max(0, self.seconds - int(time.monotonic() - self.started_at))


def _sleep_countdown(record: _ToolRecord) -> _SleepCountdown | None:
    """The time left of *record*, a running sleep call; None for any other call."""
    if record.tool_kind != KIND_SLEEP or record.provider_hosted or record.status != "running" or not record.args:
        return None
    seconds = normalize_sleep_seconds(record.args.get("seconds"))
    if seconds is None or not 0 <= seconds <= MAX_SLEEP_SECONDS:
        return None
    return _SleepCountdown(record.started_at, seconds, _sleep_activity(record.args))


def _slept_ms(record: _ToolRecord) -> int:
    """How long a finished sleep call slept: the time it took, at most what it asked for, or, not known, what it
    asked for if it answered that it slept it out."""
    if record.tool_kind != KIND_SLEEP or record.status == "running":
        return 0
    seconds = normalize_sleep_seconds((record.args or {}).get("seconds"))
    asked_ms = seconds * 1000 if seconds is not None and 0 <= seconds <= MAX_SLEEP_SECONDS else None
    if record.duration_known:
        # Counted from when the TUI got its start, it can take in an approval it waited for: it slept no longer.
        return record.duration_ms if asked_ms is None else min(record.duration_ms, asked_ms)
    metadata = record.metadata or {}
    if (
        record.status != "complete"
        # Replay shows a call no result answered (the session stopped while it slept) as complete, with no result.
        or not record.result
        or metadata.get("sleep_skipped") is True
        or metadata.get("sleep_interrupted") is True
    ):
        return 0
    return asked_ms or 0


def _ran_ms(record: _ToolRecord, stopped_at: float | None) -> int:
    """How long *record* ran until *stopped_at* (``time.monotonic()``), now if not given."""
    ended = time.monotonic() if stopped_at is None else stopped_at
    return max(0, int((ended - record.started_at) * 1000))


def _record_outcome(record: _ToolRecord) -> ToolOutcome:
    """How a folded row counts *record*: a rejected call failed, a cancelled one was interrupted."""
    if record.canonical_status == "interrupted":
        return "interrupted"
    return "failed" if record.status in {"error", "rejected"} else ""


def _record_activity_reference(record: _ToolRecord, render_reference: Callable[[MessageRef], str]) -> _ToolActivity:
    """Return the most specific current activity for a top-level record."""
    if record.tool_kind == KIND_SUB_AGENT and record.nested_latest_activity is not None:
        nested_activity = record.nested_latest_activity
        detail = nested_activity if isinstance(nested_activity, str) else render_reference(nested_activity)
        agent = (
            _activity_arg(record.args, "agent_name")
            or _activity_arg(record.args, "agent")
            or sanitize_legacy_scalar(record.tool_name)
        )
        return _TOOL_ACTIVITY_AGENT_DETAIL.bind(agent=agent, activity=detail)
    return _tool_activity_reference(record.tool_name, record.tool_kind, record.args)


def _compact_tool_metadata(metadata: dict[str, Any] | None) -> dict[str, Any] | None:
    """Drop bulky snapshot payloads from metadata retained by collapsed tool records."""
    if not metadata:
        return metadata
    compacted = dict(metadata)
    compacted.pop("file_snapshot", None)
    compacted.pop("shell_file_snapshots", None)
    return compacted


def fmt_duration(ms: int) -> str:
    """Format milliseconds as a compact human string, from ``120ms`` through ``1d 4h``."""
    if ms < 1000:
        return f"{ms}ms"
    seconds = ms // 1000
    if seconds < 60:
        return f"{seconds}s"
    m, s = divmod(seconds, 60)
    if m < 60:
        return f"{m}m {s}s" if s else f"{m}m"
    h, m = divmod(m, 60)
    if h >= 24:
        d, h = divmod(h, 24)
        return f"{d}d {h}h" if h else f"{d}d"
    if m == 0:
        return f"{h}h"
    return f"{h}h {m}m"


_ARGS_DISPLAY_MAX = 120
"""Max characters for tool call arguments display."""

_RESULT_DISPLAY_MAX = 250
"""Max characters for tool call result display."""

_IMAGE_PLACEHOLDER_RE = re.compile(r"^\[image/[^]\s]+ image\]$")

_TERMINAL_TOOL_STATUSES = frozenset({"complete", "error", "rejected"})
_TERMINAL_CANONICAL_STATUSES = frozenset({"completed", "failed", "interrupted"})

_PENDING_CONTENT_MOUNT_CONCURRENCY = 4
"""Maximum deferred tool contents prepared concurrently after expanding a group."""


if TYPE_CHECKING:
    from textual.app import ComposeResult, RenderResult
    from textual.events import Click, Leave, MouseMove
    from textual.geometry import Offset
    from textual.visual import VisualType

    _ToolCopyExcludedBase = Widget
else:
    _ToolCopyExcludedBase = object

TOOL_COPY_EXCLUDED_CLASS = "-copy-excluded"
"""CSS class marking tool-renderer widgets whose text should not be copied."""

TOOL_COPY_INCLUDED_CLASS = "-copy-included"
"""CSS class opting a subtree back into copy inside an excluded tool card."""


def _content_additional_properties(content: Any) -> dict[str, Any]:
    if isinstance(content, dict):
        extra = content.get("additional_properties")
    else:
        # Kernel Content is an external boundary for the TUI.
        extra = getattr(content, "additional_properties", None)
    return extra if isinstance(extra, dict) else {}


def _content_attr(content: Any, attr: str) -> Any:
    if isinstance(content, dict):
        return content.get(attr)
    # Kernel Content is an external boundary for the TUI.
    return getattr(content, attr, None)


def _positive_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int) and value > 0:
        return value
    return None


def _image_metadata_value(content: Any, key: str) -> Any:
    extra = _content_additional_properties(content)
    return extra.get(key) if key in extra else _content_attr(content, key)


def _image_result_display(result: str, image_contents: Sequence[Any] | None) -> str:
    if not image_contents or not result:
        return result

    lines: list[str] = []
    for line in result.splitlines():
        stripped = line.strip()
        if _IMAGE_PLACEHOLDER_RE.match(stripped):
            continue
        lines.append(line)
    return "\n".join(lines).strip()


def result_text_is_error(result: str, *, exit_code_authoritative: bool = False) -> bool:
    """Return true for tool result text that follows the UI error convention."""
    if exit_code_authoritative:
        exit_code = result_text_exit_code(result)
        if exit_code is not None:
            return exit_code != 0
    return result.lstrip().startswith("Error:")


def tool_result_is_error(
    result: str,
    approval: str | None,
    tool_kind: str = "",
    metadata: dict[str, Any] | None = None,
    tool_name: str = "",
) -> bool:
    return _tool_result_error_state(result, approval, tool_kind, metadata, tool_name)


def tool_result_render_status(
    result: str,
    approval: str | None,
    tool_kind: str = "",
    metadata: dict[str, Any] | None = None,
    tool_name: str = "",
    *,
    parsed_error: bool = False,
    parsed_error_overrides_structured_success: bool = False,
) -> ToolRenderStatus:
    """Return the terminal display status for a completed tool result."""
    if tool_result_is_rejected(approval, metadata):
        return "rejected"
    if _tool_result_error_state(
        result,
        approval,
        tool_kind,
        metadata,
        tool_name,
        parsed_error=parsed_error,
        parsed_error_overrides_structured_success=parsed_error_overrides_structured_success,
    ):
        return "error"
    return "complete"


def _tool_result_error_state(
    result: str,
    approval: str | None,
    tool_kind: str = "",
    metadata: dict[str, Any] | None = None,
    tool_name: str = "",
    *,
    parsed_error: bool = False,
    parsed_error_overrides_structured_success: bool = False,
) -> bool:
    if tool_result_is_rejected(approval, metadata):
        return False
    metadata_state = tool_result_metadata_failure_state(metadata)
    if metadata_state is True:
        return True
    if metadata_state is False:
        return parsed_error and parsed_error_overrides_structured_success
    if parsed_error:
        return True
    if tool_kind == KIND_SHELL:
        exit_code = shell_exit_code_from_metadata(metadata)
        if exit_code is not None:
            return exit_code != 0
        if shell_timed_out_from_metadata(metadata):
            return True
    if not legacy_error_text_applies(tool_kind, tool_name):
        return False
    return result_text_is_error(result, exit_code_authoritative=tool_kind == KIND_SHELL)


def tool_result_is_rejected(approval: str | None, metadata: dict[str, Any] | None = None) -> bool:
    """Return true when a tool result should render with rejected styling."""
    if approval == "user_rejected":
        return True
    return tool_result_metadata_is_rejected(metadata)


def _image_border_subtitle(
    image_contents: list[Any] | None,
    fallback_size: tuple[int, int] | None,
    *,
    render_message: Callable[[MessageRef], str] = format_message,
) -> str:
    if not image_contents:
        return ""
    first = image_contents[0]
    width = _positive_int(_image_metadata_value(first, "width"))
    height = _positive_int(_image_metadata_value(first, "height"))
    if (width is None or height is None) and fallback_size is not None:
        width, height = fallback_size
    media_type = _image_metadata_value(first, "media_type") or _content_attr(first, "media_type")
    if not isinstance(media_type, str):
        media_type = ""

    parts: list[str] = []
    if width is not None and height is not None:
        parts.append(render_message(_TOOL_CARD_IMAGE_RESOLUTION.bind(width=width, height=height)))
    if media_type:
        parts.append(render_message(_TOOL_CARD_IMAGE_TYPE.bind(media_type=sanitize_legacy_scalar(media_type))))
    return " · ".join(parts)


class ToolCopyExcludedMixin(_ToolCopyExcludedBase):
    """Mixin for tool UI that should be omitted from screen-level copy."""

    ALLOW_SELECT: ClassVar[bool] = False
    image_contents: tuple[Any, ...] | list[Any] = ()

    if TYPE_CHECKING:
        approval: str | None
        args: dict[str, Any]
        args_summary: str
        call_id: str
        duration_ms: int
        metadata: dict[str, Any] | None
        result_text: str
        status: str
        tool_kind: str
        tool_name: str

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.add_class(TOOL_COPY_EXCLUDED_CLASS)

    def get_selection(self, selection: Selection) -> tuple[str, str] | None:
        """Tool renderer chrome/results are visual context, not transcript text."""
        return None

    def _show_tool_copy_button(self) -> None:
        """Reveal the copy and view affordances after a terminal state."""
        with suppress(Exception):
            self.query_one(ToolCardHeader).show_actions()

    def _render_message(self, reference: MessageRef) -> str:
        return render_str(widget_localizer(self), reference)

    def _defer_tool_copy(self) -> bool:
        """Return true when a subclass has scheduled copy handling itself."""
        return False

    def _defer_tool_view(self) -> bool:
        """Return true when a subclass has scheduled view handling itself."""
        return False

    def _tool_copy_status(self) -> str:
        """Return a stable terminal status label for the copy payload."""
        result = self.result_text.strip()
        if self.has_class("-rejected"):
            return "rejected"
        if result.lower() == "cancelled":
            return "cancelled"
        if self.status == "error" or self.has_class("-error"):
            return "error"
        if tool_result_is_error(result, self.approval, self.tool_kind, self.metadata, self.tool_name):
            return "error"
        if self.status == "complete" or self.has_class("-success") or self.has_class("-complete"):
            return "completed"
        return self.status

    def _tool_copy_input(self) -> tuple[str, str]:
        """Return ``(language, text)`` for the tool input section."""
        if self.args:
            return "json", json.dumps(self.args, indent=2, ensure_ascii=False, default=str)
        if self.args_summary:
            return "text", self.args_summary
        return "json", "{}"

    def _tool_input_args(self) -> dict[str, Any]:
        """Return structured input args from live state or replay summary."""
        if self.args:
            return self.args
        if not self.args_summary:
            return {}
        try:
            parsed = json.loads(self.args_summary)
        except TypeError, ValueError:
            return {}
        return parsed if isinstance(parsed, dict) else {}

    def _tool_copy_sections(self) -> list[tuple[str, str, str]]:
        """Return ``(title, language, text)`` output sections for the copy payload."""
        return [
            (
                self._render_message(TOOL_VIEW_OUTPUT.bind()),
                "text",
                self.result_text or self._render_message(TOOL_VIEW_EMPTY.bind()),
            )
        ]

    def format_tool_execution_copy(self) -> str:
        """Format this tool invocation as a paste-friendly Markdown transcript."""
        input_lang, input_text = self._tool_copy_input()
        lines = [
            f"# {self.tool_name}",
            "",
            f"- **Status:** `{self._tool_copy_status()}`",
        ]
        if self.duration_ms:
            lines.append(f"- **Duration:** `{fmt_duration(self.duration_ms)}`")
        if self.call_id:
            lines.append(f"- **Call ID:** `{self.call_id}`")

        lines.extend(["", "## Input", _fenced_block(input_lang, input_text)])
        for title, language, text in self._tool_copy_sections():
            lines.extend(
                ["", f"## {title}", _fenced_block(language, text or self._render_message(TOOL_VIEW_EMPTY.bind()))]
            )
        return "\n".join(lines).rstrip() + "\n"

    def copy_tool_execution(self) -> None:
        """Copy full tool input/output to the OS clipboard, and OSC52 when small."""
        payload = self.format_tool_execution_copy()

        terminal_payload_too_large = terminal_clipboard_size(payload) > OSC52_COPY_MAX_BYTES
        localizer = widget_localizer(self)
        if copy_text_to_clipboards(self.app, payload, max_terminal_bytes=OSC52_COPY_MAX_BYTES):
            self.notify(
                render_str(localizer, _TOOL_DETAILS_COPIED.bind(tool_name=self.tool_name)),
                title=render_str(localizer, COPIED_TITLE.bind()),
                timeout=2,
                markup=False,
            )
        elif terminal_payload_too_large:
            self.notify(
                render_str(localizer, _TOOL_DETAILS_COPIED_TOO_LARGE.bind(tool_name=self.tool_name)),
                title=render_str(localizer, COPIED_TITLE.bind()),
                timeout=3,
                markup=False,
            )
        else:
            self.notify(
                render_str(localizer, _TOOL_DETAILS_COPIED_UNAVAILABLE.bind(tool_name=self.tool_name)),
                title=render_str(localizer, COPIED_TITLE.bind()),
                timeout=3,
                markup=False,
            )

    def on_tool_copy_button_clicked(self, event: ToolCopyButton.Clicked) -> None:
        """Handle clicks from the header copy affordance."""
        event.stop()
        if self._defer_tool_copy():
            return
        self.copy_tool_execution()

    # --- View modal ---

    def _tool_view_title(self) -> str:
        """Return the modal title for the detailed view."""
        return self.tool_name

    def _view_dark(self) -> bool:
        """Whether the active Textual theme is dark — drives syntax styling."""
        try:
            return self.app.current_theme.dark
        except Exception:
            return True

    def _tool_view_input_widgets(self) -> list[Widget]:
        """Build the Input tab content. Subclasses may override for richer views."""
        dark = self._view_dark()
        if self.args:
            return build_params_view(self.args, dark=dark, render_message=self._render_message)
        language, text = self._tool_copy_input()
        return [build_code_view(language, text, dark=dark, render_message=self._render_message)]

    def _tool_view_output_widgets(self) -> list[Widget]:
        """Build the Output tab content. Subclasses may override for richer views."""
        if self.image_contents:
            widgets: list[Widget] = [ToolViewImage(self.image_contents)]
            display = _image_result_display(self.result_text, self.image_contents)
            if display:
                widgets.extend(
                    build_output_views(
                        [(self._render_message(TOOL_VIEW_OUTPUT.bind()), "text", display)],
                        dark=self._view_dark(),
                        render_message=self._render_message,
                    )
                )
            return widgets
        return build_output_views(
            self._tool_copy_sections(),
            dark=self._view_dark(),
            render_message=self._render_message,
        )

    def _tool_view_raw_input(self) -> str:
        """Return the raw Input payload to copy (JSON for structured args)."""
        args = self._tool_input_args()
        if args:
            return json.dumps(args, indent=2, ensure_ascii=False, default=str)
        return self._tool_copy_input()[1]

    def _tool_view_raw_output(self) -> str:
        """Return the raw Output payload to copy (concatenated section text)."""
        sections = self._tool_copy_sections()
        if len(sections) == 1:
            return sections[0][2] or self._render_message(TOOL_VIEW_EMPTY.bind())
        empty = self._render_message(TOOL_VIEW_EMPTY.bind())
        return "\n\n".join(f"{title}\n{text or empty}" for title, _, text in sections)

    def _tool_view_copy_enabled(self) -> bool:
        """Whether the detail modal should expose its whole-payload copy binding."""
        return True

    def open_tool_view(self) -> None:
        """Request a modal showing the full, untruncated tool input and output."""
        self.post_message(
            ToolViewRequested(
                title=self._tool_view_title(),
                input_widgets=self._tool_view_input_widgets(),
                output_widgets=self._tool_view_output_widgets(),
                raw_input=self._tool_view_raw_input(),
                raw_output=self._tool_view_raw_output(),
                copy_enabled=self._tool_view_copy_enabled(),
            )
        )

    def on_tool_view_button_clicked(self, event: ToolViewButton.Clicked) -> None:
        """Handle clicks from the header view affordance."""
        event.stop()
        if self._defer_tool_view():
            return
        self.open_tool_view()


class ToolViewButton(ClickAffordance):
    """Legacy view affordance widget; retained as the ``Clicked`` message namespace.

    :class:`ToolCardHeader` now renders the affordance text itself and posts
    ``ToolViewButton.Clicked`` so existing ``on_tool_view_button_clicked``
    handlers keep working unchanged.
    """

    class Clicked(Message):
        """Posted when the view affordance is clicked."""

    CLICK_MESSAGE = Clicked

    def __init__(self) -> None:
        super().__init__("view", classes="tool-view-btn")

    def on_mount(self) -> None:
        localizer = widget_localizer(self)
        self.update(render_text(localizer, TOOL_CARD_ACTION_VIEW.bind()))
        self.tooltip = render_str(localizer, TOOL_CARD_ACTION_VIEW_TOOLTIP.bind())


class ToolCopyButton(ClickAffordance):
    """Legacy copy affordance widget; retained as the ``Clicked`` message namespace.

    See :class:`ToolViewButton` — :class:`ToolCardHeader` posts
    ``ToolCopyButton.Clicked`` for clicks on its copy zone.
    """

    class Clicked(Message):
        """Posted when the copy affordance is clicked."""

    CLICK_MESSAGE = Clicked

    def __init__(self, *, tooltip_message: MessageRef | None = None) -> None:
        super().__init__("copy", classes="tool-copy-btn")
        self._tooltip_message = tooltip_message

    def on_mount(self) -> None:
        localizer = widget_localizer(self)
        self.update(render_text(localizer, TOOL_CARD_ACTION_COPY.bind()))
        tooltip = self._tooltip_message or TOOL_CARD_ACTION_COPY_TOOLTIP.bind()
        self.tooltip = render_str(localizer, tooltip)


class ToolCardHeader(Static):
    """Single-widget tool header: label plus right-aligned view/copy affordances.

    Replaces the former five-widget row (``ToolHeader`` container, label
    ``Static``, ``ToolViewButton``, ``ToolActionSeparator``,
    ``ToolCopyButton``). Tool cards dominate long transcripts, and every
    mounted widget pays per-frame arrange/compositor cost while visible, so
    the header renders the whole row itself and reimplements the affordance
    behaviors: zone hit-testing for clicks, per-zone hover styling and
    tooltip, and a pointer cursor over the actions.

    The widget subclasses ``Static`` and treats its content as the label, so
    renderer code that does ``query_one("#x-label", Static).update(...)``
    keeps working unchanged. Markup parsing is disabled: labels are Rich
    ``Text`` (styling travels in spans), and a plain string — which may quote
    model output containing ``[...]`` — must render literally instead of
    raising ``MarkupError``.
    """

    ALLOW_SELECT = False

    COMPONENT_CLASSES: ClassVar[set[str]] = {
        "toolcardheader--action",
        "toolcardheader--action-hover",
    }

    DEFAULT_CSS = """
    ToolCardHeader {
        width: 100%;
        height: auto;
    }
    ToolCardHeader > .toolcardheader--action {
        color: $text-muted;
        text-style: dim not bold;
    }
    ToolCardHeader > .toolcardheader--action-hover {
        color: $accent;
        text-style: underline not dim not bold;
    }
    """

    # Rendered actions cell: " view / copy " — 1-cell pads mirror the CSS
    # padding of the legacy affordance widgets.
    _ACTION_ZONES: ClassVar[tuple[tuple[str, int, int], ...]] = (("view", 1, 5), ("copy", 8, 12))
    _ACTIONS_WIDTH: ClassVar[int] = 13
    _ZONE_TOOLTIPS: ClassVar[dict[str, MessageRef]] = {
        "view": TOOL_CARD_ACTION_VIEW_TOOLTIP.bind(),
        "copy": TOOL_CARD_ACTION_COPY_TOOLTIP.bind(),
    }

    def __init__(
        self,
        label: Text | str = "",
        *,
        id: str | None = None,
        view_only_label: MessageRef | None = None,
    ) -> None:
        super().__init__(label, id=id, markup=False)
        self.add_class(TOOL_COPY_EXCLUDED_CLASS)
        self._action_mode: Literal["hidden", "view_only", "full"] = (
            "view_only" if view_only_label is not None else "hidden"
        )
        self._hover_zone: str | None = None
        self._view_label = TOOL_CARD_ACTION_VIEW.fallback
        self._copy_label = TOOL_CARD_ACTION_COPY.fallback
        self._view_only_message = view_only_label
        self._view_only_label = format_message(view_only_label) if view_only_label is not None else ""

    def on_mount(self) -> None:
        self._resolve_action_labels()

    def update(self, content: VisualType = "", *, layout: bool = True) -> None:
        """Replace the label; relayout only when the new label changes the header's size.

        Running cards rewrite their label with the elapsed time every second, and a
        same-size label needs a repaint, not a reflow of the whole transcript. An explicit
        ``layout=False`` still skips the layout unconditionally.
        """
        if layout:
            update_static_in_place(self, content)
        else:
            super().update(content, layout=False)

    def _resolve_action_labels(self) -> None:
        localizer = widget_localizer(self)
        self._view_label = render_str(localizer, TOOL_CARD_ACTION_VIEW.bind())
        self._copy_label = render_str(localizer, TOOL_CARD_ACTION_COPY.bind())
        if self._view_only_message is not None:
            self._view_only_label = render_str(localizer, self._view_only_message)

    def get_selection(self, selection: Selection) -> tuple[str, str] | None:
        """Header chrome is visual context, not transcript text."""
        return None

    @property
    def actions_visible(self) -> bool:
        """Whether the view/copy affordances are currently shown."""
        return self._action_mode != "hidden"

    @property
    def copy_action_visible(self) -> bool:
        """Whether the terminal copy affordance is currently shown."""
        return self._action_mode == "full"

    def show_actions(self) -> None:
        """Reveal the view/copy affordances after a terminal tool state."""
        if self._action_mode == "full":
            return
        self._action_mode = "full"
        self._hover_zone = None
        if self.is_mounted:
            self._resolve_action_labels()
        # Showing the actions narrows the label column, which can rewrap a
        # long label — a layout refresh, but only once per card completion.
        self.clear_cached_dimensions()
        self.refresh(layout=True)

    def append_replay_timing(
        self,
        *,
        timestamp: str,
        duration_ms: int,
        show_duration: bool,
    ) -> None:
        """Append replay-only timing omitted by the renderer's normal label."""
        if not timestamp and not show_duration:
            return
        label = self._label_renderable().copy()
        if show_duration:
            label.append(f" ({fmt_duration(duration_ms)})", style="dim")
        if timestamp:
            label.append(f" {timestamp}", style="dim")
        self.update(label)

    def _label_renderable(self) -> Text:
        content = self.content
        if isinstance(content, Text):
            return content
        return Text(str(content))

    def _zone_style(self, name: str) -> Style:
        # Component classes resolve theme variables like ``$text-muted``
        # ("auto 60%") through the stylesheet. Never feed
        # ``app.get_css_variables()`` values into Rich style strings: Rich
        # cannot parse tokens like "auto 60%" and silently drops the whole
        # style, leaving the span to inherit ancestor colors. The full
        # (non-partial) style is needed for the alpha blend against the
        # background (partial resolution yields unblended #ffffff), but its
        # stamped background color must be stripped so spans stay transparent
        # on ANSI passthrough themes.
        full = self.get_component_rich_style(name)
        return full.without_color + Style.from_color(full.color)

    def _actions_text(self) -> Text:
        base = self._zone_style("toolcardheader--action")
        hover = self._zone_style("toolcardheader--action-hover")
        actions = Text(no_wrap=True)
        actions.append(" ")
        view_label = self._view_only_label if self._action_mode == "view_only" else self._view_label
        actions.append(view_label, style=hover if self._hover_zone == "view" else base)
        if self._action_mode == "full":
            actions.append(" / ", style=base)
            actions.append(self._copy_label, style=hover if self._hover_zone == "copy" else base)
        actions.append(" ")
        return actions

    def _actions_width(self) -> int:
        """Return the rendered action-cell width for the current mode."""
        if self._action_mode == "view_only":
            return Text(self._view_only_label).cell_len + 2
        return self._ACTIONS_WIDTH

    def render(self) -> RenderResult:
        if not self.actions_visible:
            return self.visual
        from rich.table import Table

        row = Table.grid(expand=True)
        row.add_column(ratio=1)
        row.add_column(width=self._actions_width())
        row.add_row(self._label_renderable(), self._actions_text())
        return row

    def _zone_at(self, offset: Offset) -> str | None:
        """Map a content-area offset to ``"view"``/``"copy"``, or ``None``."""
        if not self.actions_visible or offset.y != 0:
            return None
        actions_width = self._actions_width()
        actions_x = self.content_size.width - actions_width
        if actions_x < 0:
            # Header narrower than the actions cell: the rendered grid is
            # squeezed, so zone geometry no longer matches what's on screen.
            return None
        x = offset.x - actions_x
        if self._action_mode == "view_only":
            return "view" if 1 <= x < actions_width - 1 else None
        for zone, start, end in self._ACTION_ZONES:
            if start <= x < end:
                return zone
        return None

    def _set_hover_zone(self, zone: str | None) -> None:
        if zone == self._hover_zone:
            return
        self._hover_zone = zone
        reference = self._ZONE_TOOLTIPS.get(zone) if zone else None
        self.tooltip = render_str(widget_localizer(self), reference) if reference is not None else None
        self.styles.pointer = "pointer" if zone else "default"
        self.refresh()

    def on_mouse_move(self, event: MouseMove) -> None:
        self._set_hover_zone(self._zone_at(event.get_content_offset_capture(self)))

    def on_leave(self, event: Leave) -> None:
        self._set_hover_zone(None)

    def on_click(self, event: Click) -> None:
        zone = self._zone_at(event.get_content_offset_capture(self))
        if zone is None:
            return
        event.prevent_default()
        event.stop()
        if zone == "view":
            self.post_message(ToolViewButton.Clicked())
        else:
            self.post_message(ToolCopyButton.Clicked())


def is_tool_copy_excluded(widget: Widget) -> bool:
    """Return true when *widget* is inside tool UI excluded from copying.

    Walked nearest-ancestor-first so the closest marker wins: a subtree
    carrying ``TOOL_COPY_INCLUDED_CLASS`` can opt back into selection/copy
    inside an otherwise excluded card.
    """
    for ancestor in widget.ancestors_with_self:
        if ancestor.has_class(TOOL_COPY_INCLUDED_CLASS):
            return False
        if ancestor.has_class(TOOL_COPY_EXCLUDED_CLASS):
            return True
    return False


def _fenced_block(language: str, text: str) -> str:
    """Return a Markdown tilde fence long enough for *text*."""
    text = _sanitize_copy_text(text)
    max_run = max((len(match.group(0)) for match in re.finditer(r"~+", text)), default=0)
    fence = "~" * max(3, max_run + 1)
    lang = language.strip()
    opener = f"{fence}{lang}" if lang else fence
    return f"{opener}\n{text}\n{fence}"


def _sanitize_copy_text(text: str) -> str:
    """Escape terminal control characters in copied tool payloads."""
    sanitized: list[str] = []
    changed = False
    for char in text:
        codepoint = ord(char)
        if (codepoint < 0x20 and char not in {"\n", "\t"}) or 0x7F <= codepoint <= 0x9F:
            sanitized.append(f"\\x{codepoint:02x}")
            changed = True
        else:
            sanitized.append(char)
    if not changed:
        return text
    return "".join(sanitized)


# ---------------------------------------------------------------------------
# BaseToolCard / ToolCall — single tool invocation
# ---------------------------------------------------------------------------


class BaseToolCard(ToolCopyExcludedMixin, Widget):
    """Shared state and copy/view behavior for tool renderer cards."""

    status: reactive[str] = reactive("running")
    _SPINNERS: ClassVar[str] = SPINNER_FRAMES

    def __init__(
        self,
        call_id: str,
        tool_name: str,
        args_summary: str = "",
        args: dict[str, Any] | None = None,
    ) -> None:
        super().__init__()
        self.call_id = call_id
        self.tool_name = tool_name
        self.args_summary = args_summary
        self.args = args or {}
        self.tool_kind = ""
        self.result_text = ""
        self.metadata: dict[str, Any] | None = {}
        self.image_contents: tuple[Any, ...] | list[Any] = []
        self.duration_ms = 0
        self.approval: str | None = None
        self.provider_hosted = False
        self.hosted_family = ""
        self.provider = ""
        self.provider_item_type = ""
        self.provider_status = ""
        self.provider_call_id = ""
        self.canonical_status = "running"
        self.artifacts: list[dict[str, Any]] = []

    def configure_hosted(
        self,
        *,
        provider_hosted: bool,
        hosted_family: str,
        provider: str,
        provider_item_type: str,
        provider_status: str,
        provider_call_id: str,
        canonical_status: str,
        artifacts: list[dict[str, Any]] | None = None,
    ) -> None:
        """Install provider-hosted presentation metadata before mount."""
        self.provider_hosted = provider_hosted
        self.hosted_family = hosted_family
        self.provider = provider
        self.provider_item_type = provider_item_type
        self.provider_status = provider_status
        self.provider_call_id = provider_call_id
        self.canonical_status = canonical_status
        self.artifacts = list(artifacts or [])


class ToolCall(BaseToolCard):
    """Single tool call with animated spinner, name, args, result, and duration."""

    DEFAULT_CSS = """
    ToolCall {
        padding: 0 0 0 2;
        height: auto;
        margin: 0 0 1 0;
    }
    ToolCall #tc-label {
        height: auto;
    }
    ToolCall #tc-body {
        height: auto;
        color: $text-muted;
    }
    ToolCall > #tc-panel {
        height: auto;
        margin: 0 0 0 2;
        border: round $tui-border-primary 50%;
        border-title-color: $text-error;
        border-title-style: bold;
        border-subtitle-align: right;
        border-subtitle-color: $text-muted;
        padding: 0 1;
    }
    ToolCall.-success > #tc-panel {
        border: round $tui-border-success 30%;
        border-title-color: $success;
        border-subtitle-color: $success;
        border-title-style: not bold;
    }
    ToolCall.-error > #tc-panel {
        border: round $tui-border-error 50%;
        border-title-color: $text-error;
        border-subtitle-color: $text-error;
        border-title-style: not bold;
    }
    ToolCall.-rejected > #tc-panel {
        border: round $tui-border-warning 50%;
        border-title-color: $warning;
        border-subtitle-color: $warning;
        border-title-style: not bold;
    }
    """

    def __init__(
        self,
        call_id: str,
        tool_name: str,
        args_summary: str = "",
        args: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(call_id, tool_name, args_summary, args=args)
        self._spin_idx = 0
        self._timer: Timer | None = None
        # Progress snapshots may claim the body while the card is still
        # running; the spinner timer must not repaint over them.
        self._body_pinned = False

    def _label_text(self, duration_ms: int = 0) -> Text:
        t = Text()
        t.append("• ", style="bold")
        t.append(self.tool_name, style="bold")
        if duration_ms:
            t.append(f" ({fmt_duration(duration_ms)})", style="dim")
        return t

    @staticmethod
    def _format_args_title(args_summary: str) -> str:
        """Format JSON args as ``key: value key2: value2`` for border title."""
        try:
            parsed = json.loads(args_summary)
        except Exception:
            return sanitize_legacy_scalar(args_summary)
        if not isinstance(parsed, dict):
            return sanitize_legacy_scalar(args_summary)
        parts: list[str] = []
        for k, v in parsed.items():
            if isinstance(v, str):
                parts.append(f'{k}: "{sanitize_legacy_scalar(v)}"')
            else:
                parts.append(f"{k}: {sanitize_legacy_scalar(json.dumps(v, ensure_ascii=False))}")
        return " ".join(parts)

    def _border_title_text(self) -> str:
        title = self.tool_name
        if self.args_summary:
            title = self._format_args_title(self.args_summary) or self.tool_name
            if len(title) > _ARGS_DISPLAY_MAX:
                title = title[:_ARGS_DISPLAY_MAX] + "..."
        return title

    def compose(self) -> ComposeResult:
        yield ToolCardHeader(self._label_text(), id="tc-label")
        panel = Widget(id="tc-panel")
        panel.border_title = Text(self._border_title_text())
        yield panel

    def update_identity(self, tool_name: str, tool_kind: str) -> None:
        """Show the call's new name and kind; the card keeps the renderer its first start chose."""
        if (tool_name, tool_kind) == (self.tool_name, self.tool_kind):
            return
        self.tool_name = tool_name
        self.tool_kind = tool_kind
        with suppress(Exception):
            self.query_one("#tc-label", Static).update(self._label_text(self.duration_ms))
            self.query_one("#tc-panel").border_title = Text(self._border_title_text())

    def update_args(self, args: dict[str, Any]) -> None:
        """Refresh the displayed arguments after an approval edit."""
        self.args = args
        self.args_summary = json.dumps(args, ensure_ascii=False) if args else ""
        with suppress(Exception):
            self.query_one("#tc-panel").border_title = Text(self._border_title_text())

    def on_mount(self) -> None:
        panel = self.query_one("#tc-panel")
        # One body Static hosts the spinner while running and the result text
        # after completion — content swaps instead of paired display toggles.
        panel.mount(Static(self._render_spinner(), id="tc-body"))
        self._timer = self.set_interval(0.12, self._spin)

    def on_unmount(self) -> None:
        if self._timer is not None:
            self._timer.stop()

    def _spin(self) -> None:
        if self.status == "running" and not self._body_pinned:
            self._spin_idx = (self._spin_idx + 1) % len(self._SPINNERS)
            # Only a shown card repaints: repainting a widget outside the visible cut (a collapsed
            # group, scrolled away, under a modal) makes the next geometry read rebuild the
            # compositor map for the whole transcript. Every tick renders the card's current
            # state, so the first tick after the card shows paints it.
            if not is_widget_shown_on_active_screen(self):
                return
            from contextlib import suppress

            with suppress(Exception):
                update_static_in_place(self.query_one("#tc-body", Static), self._render_spinner())

    def _render_spinner(self) -> Text:
        t = Text()
        t.append(f"{self._SPINNERS[self._spin_idx]} ", style="yellow")
        t.append(render_str(widget_localizer(self), TOOL_CARD_RUNNING.bind()), style="yellow")
        return t

    def _truncate_result(self, text: str) -> str:
        """Truncate result text for display. Subclasses may override."""
        if len(text) > _RESULT_DISPLAY_MAX:
            return text[:_RESULT_DISPLAY_MAX] + "..."
        return text

    def _render_image_contents(self, image_contents: list[Any] | None) -> None:
        from chrys.app.tui.widgets.chat.image_preview import extract_image_previews
        from chrys.app.tui.widgets.chat.image_view import TerminalImages

        previews = extract_image_previews(image_contents)
        existing: TerminalImages | None = None
        with suppress(Exception):
            existing = self.query_one("#tc-images", TerminalImages)
        if not previews:
            if existing is not None:
                existing.remove()
            return
        try:
            panel = self.query_one("#tc-panel")
        except NoMatches:
            return
        subtitle = _image_border_subtitle(
            image_contents,
            previews[0].image.size if len(previews) == 1 else None,
            render_message=lambda reference: render_str(widget_localizer(self), reference),
        )
        if subtitle:
            panel.border_subtitle = Text(subtitle)
        if existing is not None:
            existing.set_previews(previews)
        else:
            with suppress(Exception):
                panel.mount(TerminalImages(previews, id="tc-images"))

    def set_complete(self, result: str, duration_ms: int = 0, **kwargs: Any) -> None:
        """Mark this tool call as complete."""
        from contextlib import suppress

        self.result_text = result
        self.duration_ms = duration_ms
        self.approval = kwargs.get("approval")
        metadata = kwargs.get("metadata")
        self.metadata = metadata if isinstance(metadata, dict) else {}
        render_status = tool_result_render_status(result, self.approval, self.tool_kind, self.metadata, self.tool_name)
        self.status = render_status
        if self._timer is not None:
            self._timer.stop()

        with suppress(Exception):
            self.query_one("#tc-label", Static).update(self._label_text(duration_ms))

        panel: Widget | None = None
        with suppress(NoMatches):
            panel = self.query_one("#tc-panel")
        approval = self.approval
        if approval == "user_approved" and panel is not None:
            panel.border_subtitle = render_text(widget_localizer(self), TOOL_CARD_APPROVED.bind())
        elif render_status == "rejected" and panel is not None:
            panel.border_subtitle = render_text(widget_localizer(self), TOOL_CARD_REJECTED.bind())

        if render_status == "rejected":
            self.add_class("-rejected")
        elif render_status == "error":
            if panel is not None:
                panel.border_subtitle = render_text(widget_localizer(self), TOOL_CARD_ERRORED.bind())
            self.add_class("-error")
        else:
            self.add_class("-success")
        image_contents = kwargs.get("image_contents")
        self.image_contents = list(image_contents or [])
        display = _image_result_display(result, image_contents)
        display = self._truncate_result(display) if display else ""
        with suppress(Exception):
            self.query_one("#tc-body", Static).update(Text(sanitize_legacy_block(display)))
        self._render_image_contents(image_contents)
        self.add_class("-done")
        self._show_tool_copy_button()

    def set_error(self, error: str) -> None:
        """Mark this tool call as failed."""
        from contextlib import suppress

        self.result_text = error
        self.status = "error"
        if self._timer is not None:
            self._timer.stop()

        self.add_class("-error")
        with suppress(Exception):
            self.query_one("#tc-body", Static).update(Text(sanitize_legacy_block(error)))
        self.add_class("-done")
        self._show_tool_copy_button()


# ---------------------------------------------------------------------------
# ToolGroupTitle — clickable header that toggles collapse
# ---------------------------------------------------------------------------


class ToolGroupTitle(ToolCopyExcludedMixin, RunningTitle, ClickAffordance):
    """Clickable title for ToolGroup — posts Clicked message on click.

    While its group runs, a blinking dot takes the fold arrow's place and a band of light sweeps across the title.
    """

    # Few: restyling the transcript resolves each one for every tool group.
    COMPONENT_CLASSES: ClassVar[set[str]] = {
        "toolgrouptitle--detail",
        "toolgrouptitle--shimmer",
        "toolgrouptitle--failed",
        "toolgrouptitle--running",
    }

    DEFAULT_CSS = """
    ToolGroupTitle {
        color: $tui-tool-group-title;
        height: auto;
    }
    /* What the calls did, what the latest running one does once it ends, and the calls interrupted: the colour
       tool rows have among fold rows. */
    ToolGroupTitle > .toolgrouptitle--detail {
        color: $text-warning;
        text-style: not bold;
    }
    /* What the running dot, lit, and the middle of the band sweeping across the running row brighten to. */
    ToolGroupTitle > .toolgrouptitle--shimmer {
        color: $text;
        text-style: not bold;
    }
    ToolGroupTitle > .toolgrouptitle--failed {
        color: $text-error;
        text-style: not bold;
    }
    /* What the latest running call does, dim until it ends and the row takes the detail's colour. */
    ToolGroupTitle > .toolgrouptitle--running {
        color: $text-muted;
        text-style: not bold;
    }
    """

    class Clicked(Message):
        """Posted when the title is clicked."""

    CLICK_MESSAGE = Clicked
    SHIMMER_COMPONENT = "toolgrouptitle--shimmer"

    def __init__(self, row: FoldRow, *, running: bool = False, countdown: _SleepCountdown | None = None) -> None:
        self._row = row
        self._countdown = countdown
        super().__init__("")
        self.set_running(running)

    def show(self, row: FoldRow, *, running: bool = False, countdown: _SleepCountdown | None = None) -> None:
        """Show *row*, after the running dot while *running*, and the time *countdown* has left after its head.

        The title stays one line, so it repaints without layout.
        """
        if self.set_running(running) or row != self._row or countdown != self._countdown:
            self._row = row
            self._countdown = countdown
            self.refresh(layout=False)

    def _shown_row(self) -> FoldRow:
        """The row with the time left as it paints: a running title repaints every frame while shown, and only then,
        so the time left needs no timer of its own. Its head is rendered with it, so both read in one locale."""
        countdown = self._countdown
        if countdown is None:
            return self._row
        localizer = widget_localizer(self)
        head = render_str(localizer, countdown.activity)
        left = render_str(
            localizer, _TOOL_ACTIVITY_SLEEP_LEFT.bind(duration=duration_words(localizer, countdown.seconds_left()))
        )
        return replace(self._row, head=head, kept=left)

    def render(self) -> Text:
        """The row cut to the title's width, its failed and interrupted counts kept in view.

        The fold arrow and the separators keep the title's own colour; what the calls did takes the detail's.
        """
        shown = self._shown_row()
        width = self.content_size.width or self.size.width
        detail = self.text_style_of("toolgrouptitle--detail")
        styles = FoldRowStyles(
            detail=detail,
            failed=self.text_style_of("toolgrouptitle--failed"),
            interrupted=detail,
            separator=Style(),
        )
        if not self._work_running:
            return fit_fold_row(shown, width, styles)
        row = fit_fold_row(shown, max(width - 2, 0), styles)
        # The running call's activity is the head, with what it keeps in view; with none yet, the band crosses the
        # counts.
        end = min(len(shown.lead) or len(row.plain), len(row.plain))
        running = self.text_style_of("toolgrouptitle--running")
        row.stylize(running, 0, end)
        return self.running_title(row, end, base=running.color)


# ---------------------------------------------------------------------------
# ToolGroup — collapsible container of ToolCall widgets
# ---------------------------------------------------------------------------


class ToolGroup(ToolCopyExcludedMixin, FoldGap, ProcessMember):
    """Collapsible group of tool calls within a single agent turn.

    Consecutive tool starts are grouped together. While they run, the title shows
    the latest activity; once the group closes, how many calls of each kind it made,
    or, for its one call that succeeded, what that call did.
    """

    DEFAULT_CSS = """
    /* As thinking and the text between tool calls: no vertical margin, so their thin guides meet in one line. */
    ToolGroup {
        margin: 0;
        padding: 0 1;
        border-left: solid $tui-border-neutral-gray $border-opacity;
        height: auto;
    }
    ToolGroup > #tg-header {
        height: 1;
        width: 100%;
    }
    ToolGroup > #tg-header > ToolGroupTitle {
        height: 1;
        width: 1fr;
        overflow: hidden;
    }
    ToolGroup > Vertical {
        height: auto;
    }
    /* Open: its own guide under the title's fold arrow, its calls under the label, as open thinking text. */
    ToolGroup > #tg-content {
        border-left: solid $tui-border-neutral-gray $border-opacity;
    }
    ToolGroup > #tg-content > * {
        padding-left: 1;
    }
    """

    collapsed: reactive[bool] = reactive(False)

    @dataclass
    class ProcessStateChanged(Message):
        """A process owner may retry its fold after tool or interaction state changes."""

        group: ToolGroup

    def __init__(
        self,
        *,
        live: bool = True,
        tool_widget_configurator: Callable[[Widget], None] | None = None,
        workspace_cwd: Callable[[], str] | None = None,
    ) -> None:
        """*workspace_cwd* gives the folder the chat works in, which a path inside it reads relative to; None keeps
        paths as the calls gave them, as a transcript that may run elsewhere does."""
        self._collapse_lock_call_ids: set[str] = set()
        self._process_status: tuple[ToolTally, bool, bool] | None = None
        super().__init__()
        self._live = live
        self._tool_widget_configurator = tool_widget_configurator
        self._workspace_cwd = workspace_cwd
        self._tools: dict[str, Widget] = {}
        self._tool_records: dict[str, _ToolRecord] = {}
        self._activity_sequence = 0
        self._activity_header_finalized = not live
        self._retained_activity_text = ""
        self._done = 0
        self._pending_content_worker: Any | None = None
        self._content_structure_lock = asyncio.Lock()
        self._content_mounted = True
        self._content_freeze_epoch: int | None = None
        """GC freeze epoch read before the oldest mounted tool widget was built; None while none is."""
        self.set_content_visible(False)  # hidden until first tool is added

    def compose(self) -> ComposeResult:
        with Horizontal(id="tg-header"):
            yield ToolGroupTitle(self._title_row(), running=self.shows_running, countdown=self._title_countdown())
        content = Vertical(id="tg-content")
        content.display = not self.collapsed
        yield content

    def on_unmount(self) -> None:
        self._cancel_pending_content_mounts()

    def _accepts_tool_mounts(self) -> bool:
        """Whether renderer widgets may still be attached below this group."""
        return self.is_attached and not self._closing and not self._pruning

    def get_tool(self, call_id: str) -> Widget | None:
        """Return the tool widget for ``call_id``, or ``None`` if not in this group.

        Public accessor used by :class:`ChatPanel` to resolve the widget
        mounted for a given tool call without reaching into the internal
        ``_tools`` map.
        """
        return self._tools.get(call_id)

    def reveal_tool(self, call_id: str) -> Widget | None:
        """Expand the group and return the mounted widget for ``call_id`` if available."""
        if self.collapsed:
            self.collapsed = False
        if not self._content_mounted:
            return None
        return self._tools.get(call_id)

    def lock_collapse_for(self, call_id: str) -> None:
        """Keep this group expanded while a live inline control is active."""
        if not call_id:
            return
        self._collapse_lock_call_ids.add(call_id)
        if self.collapsed:
            self.collapsed = False
        self._notify_process_state()

    def unlock_collapse_for(self, call_id: str) -> None:
        """Release a live inline control's collapse lock."""
        self._collapse_lock_call_ids.discard(call_id)
        self._notify_process_state()

    @property
    def collapse_locked(self) -> bool:
        """True while live inline controls require this group to stay expanded."""
        return bool(self._collapse_lock_call_ids)

    def validate_collapsed(self, collapsed: bool) -> bool:
        """Prevent any caller from hiding live inline controls."""
        if collapsed and self.collapse_locked:
            return False
        return collapsed

    def is_tool_running(self, call_id: str) -> bool:
        """Return whether the logical tool record for ``call_id`` is still running."""
        record = self._tool_records.get(call_id)
        return record is not None and record.status == "running"

    def _next_activity_order(self) -> int:
        self._activity_sequence += 1
        return self._activity_sequence

    def finalize_activity_header(self) -> None:
        """Switch to the count title once this group reaches a non-tool boundary.

        A still-running record continues to win until it actually finishes;
        this matters for sub-agents whose group can close while their work is
        still routed to the detached card.
        """
        if self._activity_header_finalized:
            return
        self._activity_header_finalized = True
        self._update_title()

    def clear_ask_user_inline_prompts(self) -> None:
        """Remove inline ask_user controls mounted in this group."""
        from chrys.app.tui.widgets.chat.renderers.ask_user import AskUserToolCall

        for tc in self._tools.values():
            if isinstance(tc, AskUserToolCall):
                tc.clear_inline_prompt()
        self._collapse_lock_call_ids.clear()
        self._notify_process_state()

    async def add_tool(
        self,
        call_id: str,
        tool_name: str,
        tool_kind: str,
        args_summary: str = "",
        args: dict[str, Any] | None = None,
        *,
        provider_hosted: bool = False,
        hosted_family: str = "",
        provider: str = "",
        provider_item_type: str = "",
        provider_status: str = "",
        provider_call_id: str = "",
        canonical_status: str = "running",
        started_at: float | None = None,
    ) -> None:
        """Add a running tool call to this group, started at *started_at* (``time.monotonic()``), or now."""
        from chrys.app.tui.widgets.chat.tool_renderers import create_tool_widget

        if not self._accepts_tool_mounts():
            return
        previous = self._tool_records.get(call_id)
        if previous is not None and previous.status == "running":
            # Started again while it runs, the call goes on from when it first started.
            started_at = previous.started_at
        elif started_at is None:
            started_at = time.monotonic()
        self._tool_records[call_id] = _ToolRecord(
            call_id=call_id,
            tool_name=tool_name,
            tool_kind=tool_kind,
            args_summary=args_summary,
            args=args,
            provider_hosted=provider_hosted,
            hosted_family=hosted_family,
            provider=provider,
            provider_item_type=provider_item_type,
            provider_status=provider_status,
            provider_call_id=provider_call_id,
            canonical_status=canonical_status,
            activity_order=self._next_activity_order(),
            started_at=started_at,
        )
        self.set_content_visible(True)
        async with self._content_structure_lock:
            # The structure lock may have yielded behind a collapse restore or
            # prune. Revalidate before constructing a renderer: Textual keeps
            # composed descendants queryable briefly even when their ancestor
            # can no longer accept mounts.
            if not self._accepts_tool_mounts():
                self._tool_records.pop(call_id, None)
                return
            if existing := self._tools.get(call_id):
                running = isinstance(existing, BaseToolCard) and existing.status == "running"
                if record := self._tool_records.get(call_id):
                    self._apply_tool_record_state(record, existing)
                # A running call's start again shows its name, kind and arguments on its card, as far as the
                # renderer its first start chose updates them in place. A finished card keeps the call its
                # result answered.
                if running and isinstance(existing, ToolCall):
                    existing.update_identity(tool_name, tool_kind)
                if running and args is not None and isinstance(existing, _ToolArgsUpdatable):
                    existing.update_args(args)
                self._update_title()
                return
            if not self._content_mounted:
                await self._restore_completed_tool_widgets_locked(allow_collapsed=True)
                self._update_title()
                return

            self._note_content_build_epoch()
            if provider_hosted:
                tc = create_tool_widget(
                    call_id,
                    tool_name,
                    tool_kind,
                    args_summary,
                    args=args,
                    provider_hosted=True,
                    hosted_family=hosted_family,
                    provider=provider,
                    provider_item_type=provider_item_type,
                    provider_status=provider_status,
                    provider_call_id=provider_call_id,
                    canonical_status=canonical_status,
                )
            else:
                tc = create_tool_widget(call_id, tool_name, tool_kind, args_summary, args=args)
            self._configure_tool_widget(tc)
            self._restore_start(self._tool_records.get(call_id), tc)
            self._tools[call_id] = tc
            # EventBus delivery normally leaves a scheduler turn between start
            # and result. Nested transcript journals can replay both back to
            # back, so compose the renderer tree before mounting its root; a
            # terminal update may then safely query renderer descendants as
            # soon as ``add_tool`` returns.
            precompose_tree([tc])
            await self.query_one("#tg-content").mount(tc)
            if record := self._tool_records.get(call_id):
                self._apply_tool_record_state(record, tc)
            self._content_mounted = True
        self._update_title()

    async def add_collapsed_replay_tool(
        self,
        call_id: str,
        tool_name: str,
        tool_kind: str,
        args_summary: str = "",
        args: dict[str, Any] | None = None,
        *,
        result: str = "",
        duration_ms: int = 0,
        duration_known: bool = False,
        timestamp: str = "",
        file_snapshot: FileSnapshotPayload | None = None,
        approval: str | None = None,
        metadata: dict[str, Any] | None = None,
        image_contents: list[Any] | None = None,
        artifacts: list[dict[str, Any]] | None = None,
        provider_hosted: bool = False,
        hosted_family: str = "",
        provider: str = "",
        provider_item_type: str = "",
        provider_status: str = "",
        provider_call_id: str = "",
        canonical_status: str = "completed",
        lazy: bool = False,
    ) -> None:
        """Add a completed replay tool without constructing its renderer widget."""
        async with self._content_structure_lock:
            render_status = (
                "error"
                if canonical_status in {"failed", "interrupted"}
                else tool_result_render_status(result, approval, tool_kind, metadata, tool_name)
            )
            self._tool_records[call_id] = _ToolRecord(
                call_id=call_id,
                tool_name=tool_name,
                tool_kind=tool_kind,
                args_summary=args_summary,
                args=args,
                status=render_status,
                result=result,
                duration_ms=duration_ms,
                duration_known=duration_known or duration_ms != 0,
                timestamp=timestamp,
                replay_timing=True,
                file_snapshot=file_snapshot,
                approval=approval,
                metadata=_compact_tool_metadata(metadata),
                image_contents=list(image_contents or []),
                lazy=lazy,
                completed_via_result=not provider_hosted or canonical_status == "completed",
                provider_hosted=provider_hosted,
                hosted_family=hosted_family,
                provider=provider,
                provider_item_type=provider_item_type,
                provider_status=provider_status,
                provider_call_id=provider_call_id,
                canonical_status=canonical_status,
                artifacts=list(artifacts or []),
                activity_order=self._next_activity_order(),
            )
            self._tools.pop(call_id, None)
            self.set_content_visible(True)
            self._content_mounted = False
            self._reconcile_done_count()
            if not self.collapsed:
                self.collapsed = True
            else:
                self._update_title()

    def update_tool_args(self, call_id: str, args: dict[str, Any]) -> None:
        """Refresh arguments for a running tool card."""
        record = self._tool_records.get(call_id)
        if (
            record is not None
            and record.canonical_status in _TERMINAL_CANONICAL_STATUSES
            and (record.completed_via_result or record.canonical_status == "interrupted")
        ):
            # Same window as complete_tool: a status-only terminal transition
            # still awaits the real result item, which may carry the arguments.
            logger.debug("Ignoring late arguments for terminal tool call %s", call_id)
            return
        tc = self._tools.get(call_id)
        if isinstance(tc, _ToolArgsUpdatable):
            tc.update_args(args)
        if record is not None:
            record.args = args
            record.args_summary = json.dumps(args, ensure_ascii=False) if args else ""
        self._update_title()

    def update_nested_tool_activity(
        self,
        parent_call_id: str,
        call_id: str,
        *,
        tool_name: str | None = None,
        tool_kind: str | None = None,
        args: dict[str, Any] | None = None,
        status: str | None = None,
    ) -> None:
        """Update presentation-only state for a tool running inside a sub-agent."""
        record = self._tool_records.get(parent_call_id)
        if record is None or record.status != "running":
            return
        if status in _TERMINAL_CANONICAL_STATUSES:
            if record.nested_activities.pop(call_id, None) is not None:
                self._update_title()
            return
        activity = record.nested_activities.get(call_id)
        if activity is None:
            if tool_name is None:
                return
            activity = _NestedToolActivity(
                tool_name=tool_name,
                tool_kind=tool_kind or "",
                args=_compact_activity_args(args),
            )
            record.nested_activities[call_id] = activity
        else:
            if tool_name is not None:
                activity.tool_name = tool_name
            if tool_kind is not None:
                activity.tool_kind = tool_kind
            if args is not None:
                activity.args = _compact_activity_args(args)
        order = self._next_activity_order()
        record.activity_order = order
        record.nested_latest_activity = _tool_activity_reference(
            activity.tool_name,
            activity.tool_kind,
            activity.args,
        )
        self._update_title()

    def update_sub_agent_activity(self, parent_call_id: str, activity: _ToolActivity | None) -> None:
        """Replace a running sub-agent's latest assistant/tool activity; None shows the agent's own."""
        record = self._tool_records.get(parent_call_id)
        if record is None or record.status != "running" or record.tool_kind != KIND_SUB_AGENT:
            return
        record.nested_latest_activity = activity
        record.activity_order = self._next_activity_order()
        self._update_title()

    def update_sub_agent_display_name(self, parent_call_id: str, agent_name: str) -> None:
        """Apply the runtime display name without replacing the tool arguments."""
        if not agent_name:
            return
        record = self._tool_records.get(parent_call_id)
        if record is None or record.status != "running" or record.tool_kind != KIND_SUB_AGENT:
            return
        record.args = {**(record.args or {}), "agent_name": agent_name}
        record.activity_order = self._next_activity_order()
        self._update_title()

    def _capture_tool_display_state(self, call_id: str, tc: Widget | None) -> None:
        """Store compact renderer display state on the logical tool record."""
        record = self._tool_records.get(call_id)
        if record is not None and isinstance(tc, _ToolDisplayStateSerializable):
            record.display_state = tc.compact_display_state()

    def complete_tool(
        self,
        call_id: str,
        result: str,
        duration_ms: int = 0,
        *,
        image_contents: list[Any] | None = None,
        file_snapshot: FileSnapshotPayload | None = None,
        approval: str | None = None,
        metadata: dict[str, Any] | None = None,
        artifacts: list[dict[str, Any]] | None = None,
        provider_status: str = "",
        canonical_status: str = "completed",
        lazy: bool = False,
    ) -> None:
        """Mark a tool call as complete.

        If *lazy* is True, the tool widget defers expensive diff
        construction until the group is first expanded (used during replay).
        """
        record = self._tool_records.get(call_id)
        if (
            record is not None
            and record.canonical_status in _TERMINAL_CANONICAL_STATUSES
            and (record.completed_via_result or record.canonical_status == "interrupted")
        ):
            logger.debug("Ignoring late result for terminal tool call %s", call_id)
            return
        was_terminal = record.status in _TERMINAL_TOOL_STATUSES if record is not None else False
        if record is not None:
            record.status = (
                "error"
                if canonical_status == "interrupted"
                else tool_result_render_status(
                    result,
                    approval,
                    record.tool_kind,
                    metadata,
                    record.tool_name,
                )
            )
            record.result = result
            record.duration_ms = duration_ms
            record.duration_known = True
            record.file_snapshot = file_snapshot
            record.approval = approval
            record.metadata = _compact_tool_metadata(metadata)
            record.image_contents = list(image_contents or [])
            record.artifacts = list(artifacts or [])
            record.provider_status = provider_status or record.provider_status
            record.canonical_status = canonical_status
            record.lazy = lazy
            record.completed_via_result = True
            record.nested_activities.clear()
        tc = self._tools.get(call_id)
        if tc is not None:
            if isinstance(tc, BaseToolCard):
                tc.canonical_status = canonical_status
                tc.provider_status = provider_status or tc.provider_status
                tc.artifacts = list(artifacts or [])
                tc.metadata = dict(metadata or {})
            renderer = _completion_renderer(tc)
            if canonical_status == "interrupted":
                renderer.set_error(result or "interrupted")
            else:
                renderer.set_complete(
                    result,
                    duration_ms,
                    image_contents=image_contents,
                    file_snapshot=file_snapshot,
                    approval=approval,
                    metadata=metadata,
                    artifacts=artifacts,
                    lazy=lazy,
                )
            self._capture_tool_display_state(call_id, tc)
        if record is not None or tc is not None:
            if not was_terminal:
                self._done += 1
            self.unlock_collapse_for(call_id)
            self._update_title()
            if self.all_complete and self.collapsed:
                self.call_later(self._release_completed_tool_widgets)

    def _terminal_record_count(self) -> int:
        """Return how many logical tool records are already terminal."""
        return sum(1 for record in self._tool_records.values() if record.status in _TERMINAL_TOOL_STATUSES)

    def _reconcile_done_count(self) -> None:
        """Keep done count consistent after hidden-record state transitions."""
        if self._tool_records:
            self._done = self._terminal_record_count()

    def _mark_record_error(self, call_id: str, error: str, stopped_at: float | None = None) -> None:
        """Mark one logical tool record as errored, stopped at *stopped_at* (``time.monotonic()``), now if not given."""
        if record := self._tool_records.get(call_id):
            if record.status == "running" and not record.duration_known:
                # It ran until it stopped, as the timing a replay reads from the interrupted call says: a sleep's
                # summary counts it.
                record.duration_ms = _ran_ms(record, stopped_at)
                record.duration_known = True
            record.status = "error"
            record.result = error
            record.canonical_status = "interrupted" if error == "cancelled" else "failed"
            record.completed_via_result = False
            record.nested_activities.clear()

    def cancel_running(self, *, stopped_at: float | None = None) -> None:
        """Mark all still-running tool calls as cancelled, at *stopped_at* (``time.monotonic()``), now if not given.

        A view built later from what a transcript recorded gives the time the interrupt or error was recorded.
        """
        for call_id, tc in self._tools.items():
            renderer = _completion_renderer(tc)
            if renderer.status == "running":
                renderer.set_error("cancelled")
                self._mark_record_error(call_id, "cancelled", stopped_at)
        for call_id, record in self._tool_records.items():
            if record.status == "running":
                self._mark_record_error(call_id, "cancelled", stopped_at)
        self._collapse_lock_call_ids.clear()
        self._reconcile_done_count()
        self._update_title()

    @property
    def all_complete(self) -> bool:
        """True when all tool calls in this group have finished."""
        total = len(self._tool_records) if self._tool_records else len(self._tools)
        return self._done == total and total > 0

    def _notify_process_state(self) -> None:
        if not self.is_attached:
            # Replay fills a group before it mounts it: the process header reads the group as it is planned.
            return
        status = (self.tool_tally, self.all_complete, self.collapse_locked)
        if status != self._process_status:
            self._process_status = status
            self.post_message(self.ProcessStateChanged(self))

    @property
    def tool_tally(self) -> ToolTally:
        """How many calls of each kind this group made, not counting its sub-agents' own, and how they ended."""
        records = [record for record in self._tool_records.values() if record is not None]
        return ToolTally.of(
            ((tool_category(record.tool_kind, record.hosted_family), _record_outcome(record)) for record in records),
            slept_ms=sum(map(_slept_ms, records)),
        )

    @property
    def shows_running(self) -> bool:
        """Whether the title shows the running dot: through the open tool phase and while any call still runs."""
        return not self._activity_header_finalized or any(
            record is not None and record.status == "running" for record in self._tool_records.values()
        )

    def _latest_running(self) -> _ToolRecord | None:
        """The call that started last of those still running."""
        running = [
            record for record in self._tool_records.values() if record is not None and record.status == "running"
        ]
        return max(running, key=lambda candidate: candidate.activity_order) if running else None

    def _title_countdown(self) -> _SleepCountdown | None:
        """The time left the title shows after the latest running call's activity: a sleep's."""
        record = self._latest_running()
        return None if record is None else _sleep_countdown(record)

    def _title_row(self) -> FoldRow:
        """The title's row: what the latest running call does, or what the calls did; the fold arrow once it stops."""
        localizer = widget_localizer(self)
        record = self._latest_running()
        if record is not None:
            activity = _record_activity_reference(record, lambda reference: render_str(localizer, reference))
            title = activity if isinstance(activity, str) else render_str(localizer, activity)
            self._retained_activity_text = title
            return FoldRow(title)
        if not self._activity_header_finalized and self._retained_activity_text:
            return FoldRow(self._retained_activity_text)
        tally = self.tool_tally
        actions = tally.action_words(localizer)
        failed, interrupted = tally.outcomes(localizer)
        subject = ""
        if (single := self._single_call_row(localizer)) is not None:
            actions, subject = (single.words,), single.subject
        if self.shows_running:
            head = "" if actions else render_str(localizer, TOOLS_LABEL.bind())
            return FoldRow(head, actions, failed, interrupted, subject=subject)
        arrow = FOLDED_ARROW if self.collapsed else OPEN_ARROW
        return FoldRow(f"{arrow} ", actions, failed, interrupted, subject=subject)

    def _single_call_row(self, localizer: Localizer) -> SingleCallRow | None:
        """What the group's one call did, once it succeeded; None for more calls, or one that failed or still runs."""
        records = [record for record in self._tool_records.values() if record is not None]
        if len(records) != 1:
            return None
        (record,) = records
        if record.status == "running" or _record_outcome(record):
            return None
        return single_call_row(
            localizer,
            record.tool_name,
            record.tool_kind,
            record.args,
            hosted_family=record.hosted_family,
            slept_ms=_slept_ms(record),
            workspace_cwd=self._workspace_cwd() if self._workspace_cwd is not None else "",
        )

    def refresh_title(self) -> None:
        """Show the title again, as what it reads from outside the group changed: the folder paths read relative to.

        Its calls did not change, so the process block it belongs to hears nothing.
        """
        self._show_title()

    def _update_title(self) -> None:
        self._notify_process_state()
        self._show_title()

    def _show_title(self) -> None:
        try:
            title = self.query_one(ToolGroupTitle)
        except NoMatches:
            # Not composed yet, as replay builds its groups: compose works the row out from the calls it then has.
            return
        title.show(self._title_row(), running=self.shows_running, countdown=self._title_countdown())

    def takes_fold_gap(self) -> bool:
        return True

    def ends_folded(self) -> bool:
        return self.collapsed

    def render(self) -> RenderResult:
        # Its header and calls cover the rest: only its gap shows its own content.
        return self.gap_text() if self.gap_above else super().render()

    def watch_collapsed(self, collapsed: bool) -> None:
        try:
            content = self.query_one("#tg-content")
        except NoMatches:
            return
        content.display = not collapsed
        self._update_title()
        if collapsed:
            note_fold(self)
            self._cancel_pending_content_mounts()
            self._release_expensive_tool_content()
            self.call_later(self._release_completed_tool_widgets)
        else:
            # Folded, a running card did not count: it shows its time as it is now in the frame that opens it.
            for call_id, tc in self._tools.items():
                self._restore_start(self._tool_records.get(call_id), tc)
            if self._content_mounted:
                # On first expand, trigger lazy diff mounting for any tool
                # widgets that deferred their diff construction during replay.
                self._schedule_pending_content_mounts()
            else:
                self.call_later(self._restore_completed_tool_widgets)

    def _note_content_build_epoch(self) -> None:
        """Record the GC freeze epoch before building the first mounted tool widget.

        Read before any widget of the subtree exists, it bounds the whole subtree: an
        unchanged epoch at prune time proves no freeze captured any part of it.
        """
        if self._content_freeze_epoch is None:
            self._content_freeze_epoch = current_gc_freeze_epoch()

    def _young_content_watch(self, content: Widget) -> GcRemovedContent | None:
        """Describe the mounted content about to be pruned if no freeze has captured it.

        Returns None when the epoch moved since the oldest child was built (or was never
        recorded), so the prune keeps its idle full reclaim. Otherwise the coordinator
        weakly watches every node of the subtree and reclaims only if one outlives the next
        freezing action's collection.
        """
        built_at_epoch = self._content_freeze_epoch
        if built_at_epoch is None or built_at_epoch != current_gc_freeze_epoch():
            return None
        return GcRemovedContent.watch(
            built_at_epoch=built_at_epoch,
            nodes=(node for child in content.children for node in child.walk_children(with_self=True)),
        )

    def _release_expensive_tool_content(self) -> None:
        """Let child tool widgets drop expensive mounted content before pruning."""
        for tc in self._tools.values():
            if isinstance(tc, _ToolCollapsedContentReleasable):
                tc.release_collapsed_content()

    async def _release_completed_tool_widgets(self) -> None:
        """Prune completed tool widget subtrees while the group is collapsed."""
        async with self._content_structure_lock:
            await self._release_completed_tool_widgets_locked()

    def _completed_widget_pruning_supported(self) -> bool:
        """Return whether every renderer in this group has restorable state."""
        return not any(
            record is not None and record.tool_kind == KIND_SUB_AGENT for record in self._tool_records.values()
        )

    async def _release_completed_tool_widgets_locked(self) -> None:
        """Prune completed widgets while holding the structure lock."""
        if not self.collapsed or not self.all_complete or not self._content_mounted:
            return
        # SubAgentToolCall owns presentation state that _ToolRecord does not
        # snapshot (progress totals, compaction counts, and retry/pause
        # history). Pruning is currently all-or-nothing for the content
        # subtree, so retain the whole mixed group until sub-agent cards gain
        # a complete compact-display-state protocol.
        if not self._completed_widget_pruning_supported():
            return
        with suppress(Exception):
            self._release_expensive_tool_content()
            content = self.query_one("#tg-content")
            if not content.children:
                self._content_mounted = False
                self._tools = {}
                self._content_freeze_epoch = None
                return
            young_content = self._young_content_watch(content)
            await content.remove_children()
            # Textual's FIFO arrangement cache keeps prior child placements
            # across node-version changes. Drop those detached placements before
            # reporting the removal so the removed subtree is no longer
            # reachable from this permanently mounted container.
            content._clear_arrangement_cache()
            # Refresh NodeList's cached displayed-child view. A hidden
            # container is not laid out again, so Textual would otherwise keep
            # the removed widget list reachable until a later expansion.
            _ = content.displayed_children
            self._content_mounted = False
            self._tools = {}
            self._content_freeze_epoch = None
            # A subtree no freeze has captured is young garbage: the coordinator reclaims
            # only if it outlives the next freezing action's collection, instead of
            # rescanning the whole heap for it.
            self.post_message(
                GcReclaimRequested(GcReclaimReason.STABLE_CONTENT_REMOVED, prompt=False, removed=young_content)
            )
            if not self.collapsed or not self.all_complete:
                await self._restore_completed_tool_widgets_locked(allow_collapsed=not self.all_complete)

    async def _restore_completed_tool_widgets(self, *, allow_collapsed: bool = False) -> None:
        """Rebuild completed tool widgets when a collapsed group is expanded."""
        async with self._content_structure_lock:
            await self._restore_completed_tool_widgets_locked(allow_collapsed=allow_collapsed)

    async def _restore_completed_tool_widgets_locked(self, *, allow_collapsed: bool = False) -> None:
        """Rebuild tool widgets while holding the structure lock."""
        if not self._accepts_tool_mounts() or (self.collapsed and not allow_collapsed) or self._content_mounted:
            return
        from chrys.app.tui.widgets.chat.tool_renderers import create_tool_widget

        content = self.query_one("#tg-content")
        rebuilt: dict[str, Widget] = {}
        records = list(self._tool_records.values())
        if records:
            self._note_content_build_epoch()
        widgets: list[Widget] = []
        for record in records:
            if record.provider_hosted:
                tc = create_tool_widget(
                    record.call_id,
                    record.tool_name,
                    record.tool_kind,
                    record.args_summary,
                    args=record.args,
                    provider_hosted=True,
                    hosted_family=record.hosted_family,
                    provider=record.provider,
                    provider_item_type=record.provider_item_type,
                    provider_status=record.provider_status,
                    provider_call_id=record.provider_call_id,
                    canonical_status=record.canonical_status,
                    artifacts=record.artifacts,
                )
            else:
                tc = create_tool_widget(
                    record.call_id,
                    record.tool_name,
                    record.tool_kind,
                    record.args_summary,
                    args=record.args,
                )
            self._configure_tool_widget(tc)
            self._restore_start(record, tc)
            rebuilt[record.call_id] = tc
            widgets.append(tc)
        if widgets:
            pending_mount = content.mount(*widgets)
            # Textual registers every widget synchronously before returning the
            # awaitable. Publish the matching map/state in the same critical
            # section so no caller can start a second full restore.
            self._tools = rebuilt
            self._content_mounted = True
            await pending_mount
        else:
            self._tools = rebuilt
            self._content_mounted = True
        for record in records:
            tc = rebuilt[record.call_id]
            self._apply_tool_record_state(record, tc)
        if self.collapsed:
            await self._release_completed_tool_widgets_locked()
            return
        self._schedule_pending_content_mounts(base_subtree_restored=True)
        if self.collapsed:
            await self._release_completed_tool_widgets_locked()

    def _configure_tool_widget(self, widget: Widget) -> None:
        if self._tool_widget_configurator is not None:
            self._tool_widget_configurator(widget)

    @staticmethod
    def _restore_start(record: _ToolRecord | None, tc: Widget) -> None:
        """Count a running call's card from when the call started: given before it composes, its first frame does."""
        if record is not None and record.status == "running" and isinstance(tc, _ToolStartRestorable):
            tc.restore_started_at(record.started_at)

    def _apply_tool_record_state(self, record: _ToolRecord, tc: Widget) -> None:
        """Apply authoritative logical state to a newly mounted renderer."""
        if isinstance(tc, BaseToolCard):
            tc.metadata = dict(record.metadata or {})
        self._restore_start(record, tc)
        if record.display_state is not None and isinstance(tc, _ToolDisplayStateRestorable):
            tc.restore_compact_display_state(record.display_state)
        renderer = _completion_renderer(tc)
        duration_rendered = False
        # canonical_status covers status-only hosted completions: their
        # completed_via_result flag is deliberately reset to let a late
        # terminal result still land, so it cannot carry rebuild state.
        if record.completed_via_result or record.canonical_status == "completed":
            renderer.set_complete(
                record.result,
                record.duration_ms,
                image_contents=record.image_contents,
                file_snapshot=record.file_snapshot,
                approval=record.approval,
                metadata=record.metadata,
                artifacts=record.artifacts,
                lazy=record.lazy,
            )
            # Completion renderers include non-zero durations in their own
            # labels. Zero-duration spans still need the replay-only suffix.
            duration_rendered = record.duration_ms != 0
        elif record.status == "error":
            if isinstance(tc, _ToolErrorPayloadRestorable):
                tc.restore_error_payload(
                    record.result,
                    image_contents=record.image_contents,
                    metadata=record.metadata,
                    artifacts=record.artifacts,
                )
            else:
                renderer.set_error(record.result)
        if record.replay_timing and (record.timestamp or record.duration_known):
            with suppress(NoMatches):
                tc.query_one(ToolCardHeader).append_replay_timing(
                    timestamp=record.timestamp,
                    duration_ms=record.duration_ms,
                    show_duration=record.duration_known and not duration_rendered,
                )

    def _cancel_pending_content_mounts(self) -> None:
        """Cancel deferred content work for this group."""
        worker = self._pending_content_worker
        if worker is None:
            return
        self._pending_content_worker = None
        with suppress(Exception):
            worker.cancel()

    def _schedule_pending_content_mounts(self, *, base_subtree_restored: bool = False) -> None:
        """Start deferred tool content mounting outside the expand callback."""
        if not self._accepts_tool_mounts() or self.collapsed or not self._content_mounted:
            return
        self._cancel_pending_content_mounts()
        # A factory, not a coroutine: an exclusive group cancels a worker that never
        # started, and only a coroutine created at start leaves nothing unawaited.
        self._pending_content_worker = self.run_worker(
            partial(self._mount_pending_diffs, base_subtree_restored=base_subtree_restored),
            group=f"tool-group-{id(self)}-pending-content",
            exclusive=True,
            exit_on_error=False,
        )

    async def _mount_pending_diffs(self, *, base_subtree_restored: bool = False) -> None:
        """Mount deferred expensive content for tools that used lazy=True."""
        if not self._accepts_tool_mounts():
            return
        pending_tools = [
            tc
            for tc in list(self._tools.values())
            if isinstance(tc, _ToolPendingContentMountable | _ToolPendingDiffMountable)
        ]
        if not pending_tools:
            if base_subtree_restored and not self.collapsed and self._content_mounted:
                self.post_message(GcAbsorbRequested(GcAbsorbReason.STABLE_CONTENT_MOUNTED))
            return

        semaphore = asyncio.Semaphore(_PENDING_CONTENT_MOUNT_CONCURRENCY)

        async def mount_one(tc: Widget) -> bool:
            async with semaphore:
                if not self._accepts_tool_mounts() or self.collapsed or not self._content_mounted:
                    return False
                if isinstance(tc, _ToolPendingContentMountable):
                    return await tc.mount_pending_content()
                if isinstance(tc, _ToolPendingDiffMountable):
                    return await tc.mount_diff_if_pending()
                return False

        tasks = [asyncio.create_task(mount_one(tc)) for tc in pending_tools]
        mounted_content = False
        try:
            for task in asyncio.as_completed(tasks):
                mounted_content = await task or mounted_content
                if self.collapsed or not self._content_mounted:
                    for pending in tasks:
                        if not pending.done():
                            pending.cancel()
                    break
        except asyncio.CancelledError:
            for task in tasks:
                task.cancel()
            raise
        finally:
            await asyncio.gather(*tasks, return_exceptions=True)
        if (base_subtree_restored or mounted_content) and not self.collapsed and self._content_mounted:
            self.post_message(GcAbsorbRequested(GcAbsorbReason.STABLE_CONTENT_MOUNTED))

    def update_tool_progress(
        self,
        call_id: str,
        lines: list[str],
        *,
        image_contents: list[Any] | None = None,
        snapshot_metadata: dict[str, Any] | None = None,
        provider_status: str = "",
    ) -> None:
        """Forward streaming progress lines to a running tool widget."""
        record = self._tool_records.get(call_id)
        if record is not None:
            if record.canonical_status in _TERMINAL_CANONICAL_STATUSES:
                logger.debug("Ignoring late progress for terminal tool call %s", call_id)
                return
            record.image_contents = list(image_contents or record.image_contents)
            record.metadata = {**(record.metadata or {}), **(snapshot_metadata or {})}
            record.provider_status = provider_status or record.provider_status
        tc = self._tools.get(call_id)
        if isinstance(tc, _ToolProgressUpdatable):
            if isinstance(tc, BaseToolCard):
                tc.image_contents = list(image_contents or tc.image_contents)
                tc.metadata = {**(tc.metadata or {}), **(snapshot_metadata or {})}
                tc.provider_status = provider_status or tc.provider_status
            tc.update_progress(lines)
            self._capture_tool_display_state(call_id, tc)

    def update_tool_status(
        self,
        call_id: str,
        status: str,
        *,
        provider_status: str = "",
        metadata: dict[str, Any] | None = None,
        stopped_at: float | None = None,
    ) -> None:
        """Apply one monotonic canonical lifecycle transition; one that ends the call ends it at *stopped_at*.

        *stopped_at* is ``time.monotonic()`` as the status was recorded, now if not given: a view built later from
        what a transcript recorded gives it.
        """
        record = self._tool_records.get(call_id)
        if record is None:
            return
        if record.canonical_status in _TERMINAL_CANONICAL_STATUSES:
            logger.debug("Ignoring late status %s for terminal tool call %s", status, call_id)
            return
        if record.canonical_status == "running" and status == "pending":
            logger.debug("Ignoring regressive pending status for running tool call %s", call_id)
            return
        record.provider_status = provider_status or record.provider_status
        record.metadata = {**(record.metadata or {}), **(metadata or {})}
        tc = self._tools.get(call_id)
        if isinstance(tc, BaseToolCard):
            tc.provider_status = record.provider_status
            tc.metadata = dict(record.metadata or {})
        if status not in _TERMINAL_CANONICAL_STATUSES:
            record.canonical_status = status
            if isinstance(tc, BaseToolCard):
                tc.canonical_status = status
            if isinstance(tc, _HostedStatusUpdatable):
                tc.update_hosted_status(status, record.provider_status)
            if tc is not None:
                tc.refresh()
            return
        result_text = str((metadata or {}).get("result_text", ""))
        # What ran until the status ended it counts, as a stop's does: a sleep's summary counts it.
        ran_ms = _ran_ms(record, stopped_at) if record.status == "running" and not record.duration_known else None
        if status == "completed":
            self.complete_tool(
                call_id,
                result_text,
                # A function call a status completes is one whose result lost the race with its parent's (a
                # sub-agent's settling its calls as it ends): it ran until then. A hosted call shows no time.
                ran_ms if ran_ms is not None and not record.provider_hosted else 0,
                image_contents=record.image_contents,
                metadata=record.metadata,
                artifacts=record.artifacts,
                provider_status=record.provider_status,
                canonical_status=status,
            )
            record.completed_via_result = False
        else:
            error = result_text or ("interrupted" if status == "interrupted" else "Error: Provider tool failed")
            if ran_ms is not None:
                record.duration_ms = ran_ms
                record.duration_known = True
            record.canonical_status = status
            record.status = "error"
            record.result = error
            record.completed_via_result = False
            record.nested_activities.clear()
            if tc is not None:
                if isinstance(tc, BaseToolCard):
                    tc.canonical_status = status
                if isinstance(tc, _HostedStatusUpdatable):
                    tc.update_hosted_status(status, record.provider_status)
                _completion_renderer(tc).set_error(error)
            self._done += 1
            self._update_title()

    def on_tool_group_title_clicked(self) -> None:
        if self.collapse_locked:
            return
        self.collapsed = not self.collapsed
