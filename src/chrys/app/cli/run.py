# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Headless ``chrys run`` command."""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
import os
import sys
import time
from pathlib import Path

from chrys.app.cli import headless
from chrys.app.cli.headless import PreparedRuntime
from chrys.app.cli.launch_cwd import LAUNCH_CWD_MISSING_CODE, launch_cwd_missing_message
from chrys.app.cli.progress import ProgressWriter, RunContext, TurnProgress, guarded, progress_console
from chrys.app.features.buddy.lifecycle import on_successful_turn as on_buddy_successful_turn
from chrys.app.parsing import SanitizingArgumentParser
from chrys.foundation.branding import APP_COMMAND, APP_DISPLAY_NAME
from chrys.foundation.config.settings_store import LoadedSettings
from chrys.foundation.config.spec import Source
from chrys.foundation.errors.display import DISPLAY_WITH_HINT
from chrys.foundation.i18n import DisplaySequence, MessageRef, msg
from chrys.foundation.i18n.formatting import format_message, sanitize_terminal_block
from chrys.foundation.models.session_surface import SessionSurface
from chrys.foundation.platform.signals import hangup_as_interrupt
from chrys.foundation.text.encoding import decode_bytes
from chrys.orchestration.session_host import (
    AgentProfileNotFoundError,
    AmbiguousSessionIdError,
    ChrysSessionHost,
    HeadlessRunError,
    HeadlessRunResult,
    SessionNotFoundError,
)
from chrys.service.approval.policy import ApprovalMode
from chrys.service.profiles.file_scan import ProfileDirScan, unreadable_file_notes
from chrys.service.profiles.models.registry import ModelProfileRegistry
from chrys.service.profiles.models.resolver import (
    format_available_profile_labels,
    loaded_with_active_model_profile,
    resolve_profile_selector,
)
from chrys.service.profiles.models.schema import ModelProfile

_HEADLESS_RUN_TIMEOUT = msg(
    "run.headless_timeout",
    fallback="Agent run timed out.",
)
_INTERRUPTED = msg(
    "run.interrupted",
    fallback="Interrupted by user.",
)
_BACKGROUND_TASKS_STOPPING = msg(
    "run.background_tasks_stopping",
    fallback="Stopping {count} background command the run left running.",
    plural_fallback="Stopping {count} background commands the run left running.",
)
_SESSION_CWD_MISSING_HINT = msg(
    "run.session_cwd_missing_hint",
    fallback="Pass -C <dir> to continue it in another directory.",
)
_MODEL_PROFILE_NOT_FOUND = msg(
    "run.model_profile_not_found",
    fallback="Model profile not found: {model}",
)
_MODEL_PROFILE_NOT_FOUND_WITH_AVAILABLE = msg(
    "run.model_profile_not_found_with_available",
    fallback="Model profile not found: {model}. Available model profiles: {available}",
)
_MODEL_PROFILE_UNREADABLE = msg(
    "run.model_profile_unreadable",
    fallback="Model profile not found: {model}. Could not read these profile files: {files}",
)


@dataclasses.dataclass(slots=True)
class PreparedRuntimeHolder:
    """Per-invocation handoff from ``run_command`` to ``main`` handlers."""

    runtime: PreparedRuntime | None = None


def build_parser() -> argparse.ArgumentParser:
    """Build the ``chrys run`` argument parser."""
    parser = SanitizingArgumentParser(
        prog=f"{APP_COMMAND} run",
        description=f"Run an {APP_DISPLAY_NAME} agent headlessly until the final response.",
        add_help=False,
    )
    parser.add_argument(
        "-h", "--help", action="help", default=argparse.SUPPRESS, help="Show this help message and exit"
    )
    parser.add_argument("prompt", nargs="?", help="Prompt to send to the agent")
    parser.add_argument(
        "-t",
        "--task",
        metavar="FILE",
        default=None,
        help="Read prompt from text file (encoding auto-detected, resolved relative to --workdir)",
    )
    parser.add_argument("-a", "--agent", required=True, help="Agent profile id, name, or display name to run")
    parser.add_argument(
        "-m",
        "--model",
        metavar="MODEL",
        default=None,
        help="Active model profile id or name to use as the fallback model for this run",
    )
    parser.add_argument("-s", "--session", default=None, help="Optional session id to restore before running")
    parser.add_argument(
        "-C",
        "--workdir",
        metavar="DIR",
        dest="cwd",
        default=None,
        help="Working directory for the run",
    )
    parser.add_argument("--json", action="store_true", help="Emit JSON output")
    parser.add_argument(
        "-q",
        "--quiet",
        action="store_true",
        help="Do not show progress on stderr; only warnings, errors and the final response are printed",
    )
    return parser


class TaskFileError(Exception):
    """Raised when a ``chrys run --task`` file cannot be loaded."""

    def __init__(self, message: str, *, code: str) -> None:
        super().__init__(message)
        self.code = code


class ModelProfileNotFoundError(KeyError):
    """Raised when an explicit ``chrys run --model`` selector cannot be resolved."""

    def __init__(self, message: str, *, display_message: MessageRef | None = None) -> None:
        self.display_message = display_message
        super().__init__(message)


def _apply_active_model_selection(
    loaded: LoadedSettings,
    model_profile: str | None,
) -> tuple[LoadedSettings, ModelProfileRegistry | None]:
    model = (model_profile or "").strip()
    if not model:
        return loaded, None
    registry = ModelProfileRegistry()
    target, scan = registry.read_profiles()
    registry.install_profiles(target, scan)
    profile = resolve_profile_selector(registry, model)
    if profile is None:
        raise _model_profile_not_found(model, registry, scan)
    return loaded_with_active_model_profile(loaded, profile, Source.CLI), registry


def _model_profile_not_found(
    model: str, registry: ModelProfileRegistry, scan: ProfileDirScan[ModelProfile]
) -> ModelProfileNotFoundError:
    """The error for a model selector nothing resolved, naming any file that could not be read."""
    if (notes := unreadable_file_notes(scan.invalid)) is not None:
        display_message = _MODEL_PROFILE_UNREADABLE.bind(model=model, files=notes)
    elif format_available_profile_labels(registry):
        profiles = sorted(registry.list_profiles(), key=lambda item: item.name.casefold())
        display_message = _MODEL_PROFILE_NOT_FOUND_WITH_AVAILABLE.bind(
            model=model, available=DisplaySequence(f"{item.name} ({item.id})" for item in profiles)
        )
    else:
        display_message = _MODEL_PROFILE_NOT_FOUND.bind(model=model)
    return ModelProfileNotFoundError(format_message(display_message), display_message=display_message)


def _write_result(result: HeadlessRunResult, *, as_json: bool, duration: float) -> None:
    if as_json:
        payload = {
            "session_id": result.session_id,
            "result": result.text,
            "duration": round(duration, 3),
        }
        headless.write_json(payload)
        return
    # Redirected output is data for another program and stays byte-for-byte.
    text = sanitize_terminal_block(result.text) if sys.stdout.isatty() else result.text
    sys.stdout.write(text)
    if not text.endswith("\n"):
        sys.stdout.write("\n")


def _turn_progress(args: argparse.Namespace, host: ChrysSessionHost, prepared: PreparedRuntime) -> TurnProgress | None:
    """Progress on stderr (only its warnings for quiet output); nothing for JSON."""
    if args.json:
        return None

    def context() -> RunContext:
        engine = host.engine
        workspace = engine.workspace
        return RunContext(
            model=engine.runtime_details.model.name,
            workdir=workspace.primary_cwd if workspace is not None else "",
        )

    writer = ProgressWriter(
        progress_console(), reported_warnings=headless.reported_warning_keys(prepared), quiet=args.quiet
    )
    return TurnProgress(writer, render=prepared.localizer.render, context=context)


def _apply_cwd(cwd: str | None) -> str | None:
    if not cwd:
        return None
    path = Path(cwd).expanduser()
    try:
        resolved = path.resolve(strict=True)
    except OSError as exc:
        message = f"Working directory does not exist: {cwd}"
        raise FileNotFoundError(message) from exc
    if not resolved.is_dir():
        message = f"Working directory is not a directory: {resolved}"
        raise NotADirectoryError(message)
    os.chdir(resolved)
    return os.fspath(resolved)


def _read_task_file(task: str) -> str:
    if task == "-":
        raise TaskFileError("Task file does not exist: -", code="task_file_not_found")

    path = Path(task).expanduser()
    try:
        resolved = path.resolve(strict=True)
    except FileNotFoundError as exc:
        message = f"Task file does not exist: {task}"
        raise TaskFileError(message, code="task_file_not_found") from exc
    except OSError as exc:
        message = f"Failed to read task file: {path}: {exc}"
        raise TaskFileError(message, code="task_file_read_failed") from exc

    if not resolved.is_file():
        message = f"Task path is not a file: {resolved}"
        raise TaskFileError(message, code="task_file_not_file")

    try:
        raw = resolved.read_bytes()
    except FileNotFoundError as exc:
        message = f"Task file does not exist: {task}"
        raise TaskFileError(message, code="task_file_not_found") from exc
    except IsADirectoryError as exc:
        message = f"Task path is not a file: {resolved}"
        raise TaskFileError(message, code="task_file_not_file") from exc
    except OSError as exc:
        message = f"Failed to read task file: {resolved}: {exc}"
        raise TaskFileError(message, code="task_file_read_failed") from exc

    return decode_bytes(raw)


def _resolve_prompt(args: argparse.Namespace) -> str:
    if args.task is not None:
        return _read_task_file(args.task)
    if args.prompt is None:
        message = "Prompt source was not validated."
        raise ValueError(message)
    return args.prompt


async def run_command(args: argparse.Namespace, holder: PreparedRuntimeHolder) -> int:
    """Execute parsed ``chrys run`` args."""
    cwd = _apply_cwd(args.cwd)
    prompt = _resolve_prompt(args)
    # Normalized once, to the host's own reading of the flag: the host strips
    # the id and treats a blank one as "no session", and a project-free
    # bootstrap for a run that then starts fresh would silently drop the
    # working directory's project layer.
    session_id = (args.session or "").strip()
    prepared = headless.prepare_runtime(restoring_session=bool(session_id))
    holder.runtime = prepared
    headless.write_pending_warnings(prepared, as_json=args.json)
    loaded, model_registry = _apply_active_model_selection(prepared.loaded, args.model)
    host = ChrysSessionHost(
        profile_name=args.agent,
        session_id=session_id or None,
        loaded_settings=loaded,
        model_registry=model_registry,
        approval_mode=ApprovalMode.BYPASS,
        cwd=cwd,
        on_successful_turn=on_buddy_successful_turn,
        surface=SessionSurface.CLI,
    )
    if model_registry is not None:
        # --model was applied host-locally (CLI provenance, no process pointer);
        # pin it so a settings reload cannot revert the run to the global default.
        host.engine.pin_model_profile()
    progress = _turn_progress(args, host, prepared)
    started = time.monotonic()
    try:
        if session_id:
            # The restore loads settings from the saved session's own root, and
            # the run stream opens only after it, so it never carries what the
            # restore publishes — so the restore is driven here and the target
            # root's additions are written before the run. ``start()`` is
            # idempotent; the run does not restore twice.
            if progress is None:
                await host.start()
            else:
                progress.restoring(session_id)
                async with progress.observe_restore(host.event_bus):
                    await host.start()
            delta = headless.restore_delta_warnings(host.engine.loaded_settings, prepared.pending_warnings)
            if progress is None:
                headless.write_warning_events(delta, prepared.localizer, as_json=args.json)
            else:
                progress.warnings(delta)
                progress.restored(host.session_id or "")
        try:
            result = await host.run_until_final(prompt, on_event=None if progress is None else guarded(progress.handle))
        except BaseException:
            if progress is not None:
                progress.failed()
            raise
        duration = time.monotonic() - started
        if progress is not None:
            progress.succeeded(duration=duration, session_id=result.session_id)
        _write_result(result, as_json=args.json, duration=duration)
        return 0
    finally:
        try:
            # Also when the run failed: shutting down stops them all the same.
            if count := host.running_background_tasks:
                headless.write_warning(
                    _localized_or_english(
                        _BACKGROUND_TASKS_STOPPING.bind(count=count), as_json=args.json, runtime=prepared
                    ),
                    as_json=args.json,
                    code="background_tasks_stopping",
                )
        finally:
            await host.shutdown()


def _localized_or_english(
    reference: MessageRef,
    *,
    as_json: bool,
    runtime: PreparedRuntime | None,
) -> str:
    if as_json or runtime is None:
        return format_message(reference)
    return runtime.localizer.render(reference)


def _exception_display_message(
    exc: AgentProfileNotFoundError | AmbiguousSessionIdError | SessionNotFoundError | ModelProfileNotFoundError,
    *,
    as_json: bool,
    runtime: PreparedRuntime | None,
) -> str:
    english = headless.exception_message(exc)
    if as_json or runtime is None or exc.display_message is None:
        return english
    return runtime.localizer.render(exc.display_message)


def main(argv: list[str] | None = None) -> int:
    """Entry point for ``chrys run``."""
    headless.configure_logging()
    parser = build_parser()
    args = parser.parse_args(argv)
    if (args.prompt is None) == (args.task is None):
        parser.error("provide either a prompt or --task FILE, not both")
    if (missing_cwd := launch_cwd_missing_message(workdir_flag=True, workdir=args.cwd)) is not None:
        headless.write_error(missing_cwd, as_json=args.json, code=LAUNCH_CWD_MISSING_CODE)
        return 1
    holder = PreparedRuntimeHolder()
    try:
        with hangup_as_interrupt():
            return asyncio.run(run_command(args, holder))
    except TaskFileError as exc:
        headless.write_error(str(exc), as_json=args.json, code=exc.code)
        return 1
    except HeadlessRunError as exc:
        runtime = holder.runtime
        event = exc.event
        detail = None
        if args.json or runtime is None or event.display_message is None:
            message = headless.exception_message(exc)
        else:
            message = runtime.localizer.render(event.display_message)
            if event.display_hint is not None:
                message = runtime.localizer.render(
                    DISPLAY_WITH_HINT.bind(message=message, hint=runtime.localizer.render(event.display_hint))
                )
            if event.code == "executor_error":
                # The display says what went wrong; the raw text is the evidence.
                detail = event.message.strip() or None
        if event.code == "session_cwd_missing":
            hint = _localized_or_english(_SESSION_CWD_MISSING_HINT.bind(), as_json=args.json, runtime=runtime)
            message = _localized_or_english(
                DISPLAY_WITH_HINT.bind(message=message, hint=hint), as_json=args.json, runtime=runtime
            )
        headless.write_error(
            message,
            as_json=args.json,
            code=event.code or "headless_run_error",
            session_id=event.session_id,
            detail=detail,
        )
        return 1
    except SessionNotFoundError as exc:
        headless.write_error(
            _exception_display_message(exc, as_json=args.json, runtime=holder.runtime),
            as_json=args.json,
            code="session_not_found",
        )
        return 1
    except AmbiguousSessionIdError as exc:
        headless.write_error(
            _exception_display_message(exc, as_json=args.json, runtime=holder.runtime),
            as_json=args.json,
            code="session_ambiguous",
        )
        return 1
    except AgentProfileNotFoundError as exc:
        headless.write_error(
            _exception_display_message(exc, as_json=args.json, runtime=holder.runtime),
            as_json=args.json,
            code="profile_not_found",
        )
        return 1
    except ModelProfileNotFoundError as exc:
        headless.write_error(
            _exception_display_message(exc, as_json=args.json, runtime=holder.runtime),
            as_json=args.json,
            code="model_profile_not_found",
        )
        return 1
    except TimeoutError:
        headless.write_error(
            _localized_or_english(_HEADLESS_RUN_TIMEOUT.bind(), as_json=args.json, runtime=holder.runtime),
            as_json=args.json,
            code="timeout",
        )
        return 124
    except KeyboardInterrupt:
        headless.write_error(
            _localized_or_english(_INTERRUPTED.bind(), as_json=args.json, runtime=holder.runtime),
            as_json=args.json,
            code="interrupted",
        )
        return 130
    except Exception as exc:
        headless.write_error(headless.exception_message(exc), as_json=args.json)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
