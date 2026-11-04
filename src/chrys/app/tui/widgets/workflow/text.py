# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Localized workflow chrome; source, diagnostics and manifest names remain literal."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from chrys.app.tui.i18n import render_str
from chrys.app.tui.util.formatting import elapsed_parts
from chrys.foundation.i18n import MessageDef, MessageRef, msg
from chrys.foundation.i18n.formatting import format_message, sanitize_legacy_block, sanitize_legacy_scalar
from chrys.foundation.platform.files import surrogate_safe_text

if TYPE_CHECKING:
    from chrys.app.tui.i18n import LocaleController

NEXT_NODE = msg("tui.workflow.binding.next_node", fallback="Next node")
PREVIOUS_NODE = msg("tui.workflow.binding.previous_node", fallback="Previous node")
OPEN_NODE = msg("tui.workflow.binding.open_node", fallback="Open node")
NEXT_WORKFLOW = msg("tui.workflow.binding.next_workflow", fallback="Next workflow")
PREVIOUS_WORKFLOW = msg("tui.workflow.binding.previous_workflow", fallback="Previous workflow")
WORKSPACE_LOCKED_TITLE = msg("tui.workflow.workspace_locked_title", fallback="Workspace fixed")
WORKSPACE_LOCKED = msg(
    "tui.workflow.workspace_locked",
    fallback="This workflow session is bound to its workspace. Once a run has started, the workspace cannot be changed. Use /new to create a session in another directory.",
)
WORKSPACE_SOURCE_LOCKED = msg(
    "tui.workflow.workspace_source_locked",
    fallback="This workflow is located in the current working directory. Create a new session to select a workflow in another directory.",
)
NEW_SESSION = msg("tui.workflow.new_session", fallback="New Session")

MODE_CHAT = msg("tui.workflow.mode.chat", fallback="Chat")
MODE_WORKFLOW = msg("tui.workflow.mode.workflow", fallback="Workflow")
APP_MODE = msg("tui.workflow.mode.title", fallback="App Mode")
MODE_BADGE = msg("tui.workflow.mode.badge", fallback=" APP MODE: {mode} ")
MODE_BUSY_TITLE = msg("tui.workflow.mode.busy_title", fallback="Busy")
MODE_AGENT_BUSY = msg("tui.workflow.mode.agent_busy", fallback="Cannot switch app mode while the agent is busy")
MODE_WORKFLOW_BUSY = msg(
    "tui.workflow.mode.workflow_busy",
    fallback="Cannot switch app mode while a workflow is running",
)
CHAT_DESCRIPTION = msg("tui.workflow.mode.chat_description", fallback="Chat with an agent")
WORKFLOW_DESCRIPTION = msg("tui.workflow.mode.workflow_description", fallback="Select and run a workflow")
SESSION_TITLE = msg("tui.workflow.session_title", fallback="{name} · Session: {session_id}")
TITLE = msg("tui.workflow.title", fallback="Workflow")
BUILTIN = msg("tui.workflow.source.builtin", fallback="builtin")
GLOBAL = msg("tui.workflow.source.global", fallback="global")
PROJECT = msg("tui.workflow.source.project", fallback="project")
SESSION_LOCKED = msg(
    "tui.workflow.session_locked",
    fallback="This session already has runs. Create a new session to select another workflow.",
)
SHADOWED = msg("tui.workflow.shadowed", fallback="shadowed")
PREVIEW_TIMEOUT = msg("tui.workflow.preview_timeout", fallback="Workflow preview timed out.")
CONFIRM_TITLE = msg("tui.workflow.confirm.title", fallback="Trust workflow")
CONFIRM = msg("tui.workflow.confirm.button", fallback="Trust")
CANCEL = msg("tui.workflow.cancel", fallback="Cancel")
CANCEL_RUN_TITLE = msg("tui.workflow.cancel_run.title", fallback="Cancel workflow?")
CANCEL_RUN_MESSAGE = msg(
    "tui.workflow.cancel_run.message",
    fallback="Cancel workflow run {run_id}? This stops the entire workflow. This run cannot be resumed; you can only start a new run.",
)
CANCEL_RUN_CONFIRM = msg("tui.workflow.cancel_run.confirm", fallback="Cancel workflow")
KEEP_RUNNING = msg("tui.workflow.cancel_run.keep_running", fallback="Keep running")
START = msg("tui.workflow.start", fallback="▶ Start")
STARTING = msg("tui.workflow.starting", fallback="Starting…")
STOP = msg("tui.workflow.stop", fallback="■ Cancel")
RESULT = msg("tui.workflow.result", fallback="Result")
RESULT_TITLE = msg("tui.workflow.result_title", fallback="Result · {node}")
OUTPUTS = msg("tui.workflow.outputs", fallback="Outputs")
OUTPUT_SUMMARY_ONLY = msg(
    "tui.workflow.output.summary_only", fallback="Full output is unavailable. Only the saved summary is shown."
)
IDLE = msg("tui.workflow.state.idle", fallback="idle")
PENDING = msg("tui.workflow.state.pending", fallback="pending")
RUNNING = msg("tui.workflow.state.running", fallback="running")
RETRYING = msg("tui.workflow.state.retrying", fallback="retrying")
AWAITING_RETRY = msg("tui.workflow.state.awaiting_retry", fallback="awaiting retry")
COMPLETED = msg("tui.workflow.state.completed", fallback="completed")
FAILED = msg("tui.workflow.state.failed", fallback="failed")
SKIPPED = msg("tui.workflow.state.skipped", fallback="skipped")
CANCELLED = msg("tui.workflow.state.cancelled", fallback="cancelled")
PICK_HINT = msg("tui.workflow.pick_hint", fallback="Pick a workflow before starting.")
TURN_BUSY = msg(
    "tui.workflow.turn_busy", fallback="Wait for the current execution to finish before starting a workflow."
)
COMMAND = msg("tui.workflow.command", fallback="Open Workflow mode and select a workflow")
DEFAULT_MODEL = msg("tui.workflow.default_model", fallback="default model")
NODE_FAILED = msg("tui.workflow.state.node_failed", fallback="node failed")
LOOP_EXHAUSTED = msg("tui.workflow.state.loop_exhausted", fallback="loop exhausted")
WORKER_LOST = msg("tui.workflow.state.worker_lost", fallback="worker lost")
STORAGE_FAILED = msg("tui.workflow.state.storage_failed", fallback="storage failed")
ORPHANED = msg("tui.workflow.state.orphaned", fallback="orphaned")
NO_OUTPUTS = msg("tui.workflow.no_outputs", fallback="No outputs.")
GRAPH_VIEW = msg("tui.workflow.tab.graph", fallback="Workflow")
CODE_VIEW = msg("tui.workflow.tab.code", fallback="Source")
STALE = msg("tui.workflow.stale", fallback="This workflow changed. Reopen it to preview and confirm before starting.")
CODE_CHANGED = msg("tui.workflow.code_changed", fallback="This file differs from what was previewed.")
DELETE = msg("tui.workflow.delete", fallback="Delete workflow")
DELETE_PROJECT = msg(
    "tui.workflow.delete_project", fallback="Project files may be recoverable in Git. Run history stays."
)
DELETE_GLOBAL = msg("tui.workflow.delete_global", fallback="Global files cannot be recovered. Run history stays.")
DELETE_PACKAGE_NOTE = msg(
    "tui.workflow.delete_package_note", fallback="Only {entry} is deleted. The other files in {folder} are kept."
)
DELETE_ACTIVE = msg("tui.workflow.delete_active", fallback="Stop this workflow before deleting its source.")
INPUT = msg("tui.workflow.node.input", fallback="Input")
COPY_RUN_INPUT = msg("tui.workflow.copy_run_input_tooltip", fallback="Copy raw run input")
RUN_INPUT_COPIED = msg("tui.workflow.run_input_copied", fallback="Copied run input")
OUTPUT = msg("tui.workflow.node.output", fallback="Output")
TRANSCRIPT = msg("tui.workflow.node.transcript", fallback="Transcript")
PREVIOUS_RUN = msg("tui.workflow.node.previous_run", fallback="Previous run")
NO_RECORD = msg("tui.workflow.node.no_record", fallback="No record available.")
NO_TRANSCRIPT = msg("tui.workflow.node.no_transcript", fallback="Transcript not available.")
DATA_DROPPED = msg(
    "tui.workflow.node.data_dropped",
    fallback="Upstream structured data was dropped; this agent received only the text.",
)
ITERATION = msg("tui.workflow.node.iteration", fallback="Iteration {iteration}")
ATTEMPT = msg("tui.workflow.node.attempt", fallback="Attempt {attempt}")
RETRY = msg("tui.workflow.node.retry", fallback="Retry")
STDOUT_TRUNCATED = msg("tui.workflow.node.stdout_truncated", fallback="Captured stdout was truncated.")
VALUE_MARKDOWN = msg("tui.workflow.node.value.markdown", fallback="Markdown")
VALUE_PLAIN = msg("tui.workflow.node.value.plain", fallback="Plain text")
VALUE_DATA = msg("tui.workflow.node.value.data", fallback="Data")
VALUE_PROGRESS = msg("tui.workflow.node.value.progress", fallback="Progress messages")
STATUS_MESSAGES = msg("tui.workflow.status_messages", fallback="Recent status messages")
ELAPSED_SECONDS = msg("tui.workflow.elapsed.seconds", fallback="{count} second", plural_fallback="{count} seconds")
ELAPSED_MINUTES = msg("tui.workflow.elapsed.minutes", fallback="{count} minute", plural_fallback="{count} minutes")
ELAPSED_HOURS = msg("tui.workflow.elapsed.hours", fallback="{count} hour", plural_fallback="{count} hours")
ELAPSED_DAYS = msg("tui.workflow.elapsed.days", fallback="{count} day", plural_fallback="{count} days")
ELAPSED_MILLIS = msg("tui.workflow.elapsed.milliseconds", fallback="{count} ms", plural_fallback="{count} ms")
LAYOUT_VERTICAL = msg("tui.workflow.layout.vertical", fallback="↕ Vertical")
LAYOUT_HORIZONTAL = msg("tui.workflow.layout.horizontal", fallback="↔ Horizontal")
LOOP_ITERATION = msg("tui.workflow.loop_iteration", fallback="Iteration {iteration}/{maximum}")

PHASE_BODY = msg("tui.workflow.node.phase.body", fallback="Body")
PHASE_OUTGOING = msg("tui.workflow.node.phase.outgoing", fallback="Outgoing conditions")
PHASE_UNTIL = msg("tui.workflow.node.phase.until", fallback="Loop condition")
PHASE_COMBINE = msg("tui.workflow.node.phase.combine", fallback="Combine")
FAILED_PHASE = msg("tui.workflow.node.failed_phase", fallback="Failed during: {phase}")

PHASES = {"body": PHASE_BODY, "outgoing": PHASE_OUTGOING, "until": PHASE_UNTIL, "combine": PHASE_COMBINE}

SOURCES = {"builtin": BUILTIN, "global": GLOBAL, "project": PROJECT}


@dataclass(frozen=True, slots=True)
class StatePresentation:
    label: MessageDef
    node_component: str = "workflow-node--pending"
    marker: str = "○"
    edge_priority: int = 0
    edge_component: str = "workflow-edge--pending"


STATES = {
    "node_failed": StatePresentation(NODE_FAILED),
    "loop_exhausted": StatePresentation(LOOP_EXHAUSTED),
    "worker_lost": StatePresentation(WORKER_LOST),
    "storage_failed": StatePresentation(STORAGE_FAILED),
    "orphaned": StatePresentation(ORPHANED),
    "idle": StatePresentation(IDLE),
    "pending": StatePresentation(PENDING),
    "running": StatePresentation(RUNNING, "workflow-node--running", "●", 4, "workflow-edge--running"),
    "retrying": StatePresentation(RETRYING, "workflow-node--warning", "↻", 2, "workflow-edge--retrying"),
    "awaiting_retry": StatePresentation(
        AWAITING_RETRY, "workflow-node--error", "!", 3, "workflow-edge--awaiting_retry"
    ),
    "completed": StatePresentation(COMPLETED, "workflow-node--success", "✓", 1, "workflow-edge--completed"),
    "failed": StatePresentation(FAILED, "workflow-node--error", "✕", 3, "workflow-edge--failed"),
    "skipped": StatePresentation(SKIPPED, "workflow-node--skipped", "╌", 0, "workflow-edge--skipped"),
    "cancelled": StatePresentation(CANCELLED, "workflow-node--warning", "■", 2, "workflow-edge--cancelled"),
}


def state_presentation(state: str) -> StatePresentation:
    return STATES.get(state, STATES["pending"])


START_WORKFLOW = msg("tui.workflow.start_workflow", fallback="Start Workflow")
RUN_SETTINGS = msg("tui.workflow.run_settings", fallback="Run Settings")
INPUT_OPTIONAL = msg("tui.workflow.input_optional", fallback="Input · Optional")
INPUT_HINT = msg(
    "tui.workflow.input_hint",
    fallback="Initial text passed to this workflow.",
)
DIRECTORY_SESSION_BOUND = msg(
    "tui.workflow.directory_session_bound", fallback="This session is bound to its working directory."
)
DIRECTORY_SOURCE_BOUND = msg(
    "tui.workflow.directory_source_bound", fallback="This workflow is located in the working directory."
)
MODEL_LABEL = msg("tui.workflow.model", fallback="Default Model")
SELECT_MODEL = msg("tui.workflow.select_model", fallback="Select Model")
WORKING_DIRECTORY = msg("tui.workflow.working_directory", fallback="Working Directory")
CHANGE_DIRECTORY = msg("tui.workflow.change_directory", fallback="Browse")
INVALID_DIRECTORY = msg("tui.workflow.invalid_directory", fallback="Not a valid directory: {path}")


def shown(value: str, *, block: bool = False) -> str:
    """A file name, path or workflow-supplied text made safe to display: no controls, no lone surrogates."""
    safe = surrogate_safe_text(value)
    return sanitize_legacy_block(safe) if block else sanitize_legacy_scalar(safe)


def render(message: MessageRef, controller: LocaleController | None = None) -> str:
    return format_message(message) if controller is None else render_str(controller.localizer, message)


def state_label(state: str, controller: LocaleController | None = None) -> str:
    presentation = STATES.get(state)
    return render(presentation.label.bind(), controller) if presentation is not None else state


def phase_label(phase: str, controller: LocaleController | None = None) -> str:
    return render(PHASES[phase].bind(), controller) if phase in PHASES else phase


def iteration_label(iteration: int, maximum: int, controller: LocaleController | None = None) -> str:
    return render(LOOP_ITERATION.bind(iteration=iteration, maximum=maximum), controller)


def elapsed_label(seconds: float, controller: LocaleController | None = None) -> str:
    """Render at most two spelled-out duration units with localized plurals, milliseconds under a second."""
    units = {"d": ELAPSED_DAYS, "h": ELAPSED_HOURS, "m": ELAPSED_MINUTES, "s": ELAPSED_SECONDS, "ms": ELAPSED_MILLIS}
    return " ".join(render(units[unit].bind(count=value), controller) for value, unit in elapsed_parts(seconds))


RUN_TAB = msg("tui.workflow.run_tab", fallback="Run {number}")
LOAD_SESSION = msg("tui.workflow.load_session", fallback="Loading Workflow Session · {name}")
LOAD_WORKFLOW = msg("tui.workflow.load_workflow", fallback="Loading Workflow · {name}")
LOAD_DEFINITION = msg("tui.workflow.load.definition", fallback="Workflow definition")
LOAD_ENVIRONMENT = msg("tui.workflow.load.environment", fallback="Execution environment")
LOAD_GRAPH = msg("tui.workflow.load.graph", fallback="Workflow graph")
LOAD_SESSION_RECORD = msg("tui.workflow.load.session_record", fallback="Reading session record")
LOAD_SNAPSHOT = msg("tui.workflow.load.snapshot", fallback="Reading workflow snapshot")
LOAD_STATUS = msg("tui.workflow.load.status", fallback="Restoring run and node states")
SESSION_READ = msg("tui.workflow.session_read", fallback="Session record read")
SNAPSHOT_READ = msg("tui.workflow.snapshot_read", fallback="Workflow snapshot read")
STATUS_RESTORED = msg("tui.workflow.status_restored", fallback="Run status restored")
NODES_LOADED = msg("tui.workflow.nodes_loaded", fallback="Node states loaded: {loaded}/{total}")

OK = msg("tui.workflow.button.ok", fallback="OK")

LOAD_OUTPUT = msg("tui.workflow.output.load", fallback="Load output")
NATIVE_OUTPUT = msg("tui.workflow.output.native", fallback="Output outside nodes")
OUTPUT_TRUNCATED = msg("tui.workflow.output.truncated", fallback="Some output was omitted by the capture limit.")
ACP_STDERR = msg("tui.workflow.node.acp_stderr", fallback="ACP stderr: {path}")

RESTORE_FALLBACK = msg(
    "tui.workflow.restore.fallback",
    fallback="The latest run is unavailable. Showing the newest readable run: {run_id}.",
)
