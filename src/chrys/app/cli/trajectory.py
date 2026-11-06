# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Trajectory analytics export command for the Chrys CLI."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

from chrys.app.cli.launch_cwd import launch_cwd_missing_message
from chrys.foundation.branding import APP_COMMAND
from chrys.foundation.config.settings import Settings, resolve_sessions_dir
from chrys.foundation.platform.files import atomic_create_text, atomic_write_text, surrogate_safe_text
from chrys.foundation.util.session_ids import SESSION_SHORT_ID_LEN

_FORMATS = ("perfetto", "json", "csv", "findings-csv", "turns")
_HEX_DIGITS = frozenset("0123456789abcdef")


def _write_stderr(message: str) -> None:
    """Write one display-only line through a strict-UTF-8-safe boundary."""
    sys.stderr.write(f"{surrogate_safe_text(message)}\n")


def _write_stdout(message: str) -> None:
    """Write one display-only line through a strict-UTF-8-safe boundary."""
    sys.stdout.write(f"{surrogate_safe_text(message)}\n")


def build_parser() -> argparse.ArgumentParser:
    """Build the ``chrys trajectory`` parser."""
    parser = argparse.ArgumentParser(
        prog=f"{APP_COMMAND} trajectory",
        description="Analyze a recorded session trajectory and export the results.",
        add_help=False,
    )
    parser.add_argument(
        "-h", "--help", action="help", default=argparse.SUPPRESS, help="Show this help message and exit"
    )
    parser.add_argument("command", nargs="?", choices=["export"], default="export", help=argparse.SUPPRESS)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--events", metavar="PATH", help="Path to a trajectory events.jsonl file")
    source.add_argument("--session", metavar="ID", help="Session id (or unique prefix) under the sessions directory")
    parser.add_argument(
        "--format",
        choices=_FORMATS,
        default="perfetto",
        help="Export format (default: perfetto)",
    )
    parser.add_argument("--out", metavar="PATH", required=True, help="Output file path")
    parser.add_argument("--force", action="store_true", help="Overwrite the output file if it exists")
    parser.add_argument(
        "--include-sensitive",
        action="store_true",
        help="Keep raw paths in the export instead of stable redaction digests",
    )
    return parser


def _resolve_session_dir(selector: str) -> Path | str:
    """Return the session directory for a session selector, or an error message."""
    sessions_dir = resolve_sessions_dir(create=False)
    if not sessions_dir.is_dir():
        return f"no sessions directory at {sessions_dir}"
    needle = selector.strip().replace("-", "").lower()
    if not needle:
        return "session id must not be empty"
    if len(needle) > SESSION_SHORT_ID_LEN and set(needle) <= _HEX_DIGITS:
        # Directories carry only the short projection of the session id, so
        # any longer selector — the full canonical id or a unique prefix of
        # it — can only match a folder through that projection.
        needle = needle[:SESSION_SHORT_ID_LEN]
    try:
        matches = sorted(
            entry
            for entry in sessions_dir.iterdir()
            if entry.is_dir() and entry.name.replace("-", "").lower().startswith(needle)
        )
    except OSError as error:
        return f"could not read sessions directory {sessions_dir}: {error}"
    if not matches:
        return f"no session matches '{selector}' under {sessions_dir}"
    if len(matches) > 1:
        names = ", ".join(entry.name for entry in matches[:5])
        return f"session id '{selector}' is ambiguous: {names}"
    return matches[0]


def _resolve_session_events(selector: str) -> Path | str:
    """Return the events path for a session selector, or an error message."""
    from chrys.service.trajectory.session import trajectory_events_path

    session_dir = _resolve_session_dir(selector)
    if isinstance(session_dir, str):
        return session_dir
    events = trajectory_events_path(session_dir)
    if not events.is_file():
        return f"session {session_dir.name} has no trajectory events log"
    return events


async def _turns_export_payload(session_dir: Path) -> dict[str, Any]:
    """Build the ``turns`` export: the same JSON ``session/turns`` returns.

    The turn registry is read through the same store the ACP server uses, so
    the two surfaces agree on ``userMessageId`` and on fork-inherited turns.
    """
    import asyncio

    from chrys.service.analytics.turn_trace import (
        TraceSessionLevel,
        TurnTrace,
        scan_turn_trace,
        trace_payload_with_registry,
    )
    from chrys.service.state.store import JsonFileStateStore
    from chrys.service.trajectory.session import trajectory_events_path

    store = JsonFileStateStore(resolve_sessions_dir(create=False))
    transcript = await store.load_session_transcript(session_dir.name, prefer_recovery=True)
    records = transcript.turns if transcript is not None else []
    events = trajectory_events_path(session_dir)
    if events.is_file():
        trace = await asyncio.to_thread(scan_turn_trace, events)
    else:
        # No log means no local execution: every registered turn reports
        # traceAvailable false rather than the export failing.
        trace = TurnTrace(
            session_id=session_dir.name,
            coverage="partial",
            gap_count=0,
            turns=[],
            superseded_turns=[],
            unassigned_passes=[],
            session_level=TraceSessionLevel(),
        )
    payload = trace_payload_with_registry(trace, records)
    if payload is None:  # pragma: no cover - only reachable through a turn filter
        raise ValueError(f"no turn data for session {session_dir.name}")
    payload["sessionId"] = session_dir.name
    return payload


def _events_output_conflict(out: Path, events: Path) -> str | None:
    """Return an error when ``out`` would clobber the events log itself.

    Hard links and other aliases are caught by comparing the files instead of
    the path strings; a missing output cannot be the existing log, so
    ``FileNotFoundError`` is not a conflict.
    """
    try:
        same_file = out.samefile(events)
    except FileNotFoundError:
        return None
    except OSError as error:
        return f"could not compare input and output paths: {error}"
    if same_file:
        return f"output file is the trajectory events source: {out}"
    return None


def _commit_export(out: Path, text: str, *, force: bool) -> str | None:
    """Write an export atomically, returning an error message instead of raising."""
    try:
        out.parent.mkdir(parents=True, exist_ok=True)
        if force:
            atomic_write_text(out, text)
        else:
            # The early existence check ran before a potentially long
            # analysis; only a no-clobber commit keeps the promise of not
            # overwriting without --force against a file created in between.
            atomic_create_text(out, text)
    except FileExistsError:
        return f"output file already exists: {out} (pass --force to overwrite)"
    except OSError as error:
        return f"could not write export to {out}: {error}"
    return None


def _export_turns(selector: str, out: Path, *, force: bool) -> int:
    """Write the ``turns`` export for one session.

    The analyzer path is skipped on purpose: the export is the projection
    merged with the turn registry, so a session whose log is damaged, or that
    never ran locally, still reports what the registry knows.
    """
    import asyncio

    from chrys.service.trajectory.session import trajectory_events_path

    session_dir = _resolve_session_dir(selector)
    if isinstance(session_dir, str):
        _write_stderr(f"Error: {session_dir}")
        return 1
    events = trajectory_events_path(session_dir)
    if events.is_file() and (conflict := _events_output_conflict(out, events)) is not None:
        _write_stderr(f"Error: {conflict}")
        return 1
    if out.exists() and not force:
        _write_stderr(f"Error: output file already exists: {out} (pass --force to overwrite)")
        return 1
    payload = asyncio.run(_turns_export_payload(session_dir))
    text = json.dumps(payload, indent=2, ensure_ascii=False) + "\n"
    if (write_error := _commit_export(out, text, force=force)) is not None:
        _write_stderr(f"Error: {write_error}")
        return 1
    _write_stdout(
        f"Wrote turns export to {out} "
        f"(session {session_dir.name}, {len(payload['turns'])} turns, coverage {payload['coverage']})"
    )
    return 0


def _prepare_runtime() -> Settings:
    """Load settings from their sources so paths match ``chrys run``.

    Without this the sessions directory would resolve against the bare shell
    environment, silently reading a different session root than the one the
    recording process wrote under when settings name one. The loaded
    settings are returned so the export analyzes with the same configuration
    (verify commands) the TUI dashboard uses, not built-in defaults.
    """
    from chrys.foundation.config.warnings import settings_warning_events
    from chrys.orchestration.startup import bootstrap_runtime

    bootstrap = bootstrap_runtime(configure_stdio=True, setup_telemetry=False, project_root=Path(os.getcwd()))
    for warning in (*bootstrap.warnings, *settings_warning_events(bootstrap.loaded)):
        _write_stderr(f"Warning: {warning.message}")
    return bootstrap.loaded.settings


def main(argv: list[str] | None = None) -> int:
    """Entry point for ``chrys trajectory``."""
    parser = build_parser()
    args = parser.parse_args(argv)
    _ = args.command
    if (missing_cwd := launch_cwd_missing_message(workdir_flag=False)) is not None:
        _write_stderr(f"Error: {missing_cwd}")
        return 1
    if args.format == "turns" and args.session is None:
        # The turns export merges the turn registry, which only a session
        # directory can be looked up in: a bare events log cannot name one.
        _write_stderr("Error: --format turns requires --session")
        return 1
    settings = _prepare_runtime()
    out = Path(args.out)
    if args.format == "turns":
        return _export_turns(args.session, out, force=args.force)
    if args.events is not None:
        events = Path(args.events)
        if not events.is_file():
            _write_stderr(f"Error: events file not found: {events}")
            return 1
    else:
        resolved = _resolve_session_events(args.session)
        if isinstance(resolved, str):
            _write_stderr(f"Error: {resolved}")
            return 1
        events = resolved
    if (conflict := _events_output_conflict(out, events)) is not None:
        _write_stderr(f"Error: {conflict}")
        return 1
    if out.exists() and not args.force:
        _write_stderr(f"Error: output file already exists: {out} (pass --force to overwrite)")
        return 1

    from chrys.service.analytics import AnalysisAvailability, TrajectoryAnalyzer
    from chrys.service.analytics.export import analysis_json, findings_csv, perfetto_trace, turns_csv

    analyzer = TrajectoryAnalyzer(verify_commands=settings.trajectory_verify_commands)
    analysis = analyzer.load(events)
    if analysis.availability is not AnalysisAvailability.AVAILABLE:
        reason = analysis.read_error or "no analyzable trajectory data"
        _write_stderr(f"Error: cannot analyze {events}: {reason}")
        return 1
    if analysis.diagnostics.integrity_unresolved:
        _write_stderr(
            "Warning: trajectory log integrity is unresolved; the export may omit events or report incomplete values."
        )
    if analysis.workflow_runs and args.format not in {"json", "perfetto"}:
        _write_stderr("Error: Workflow runs support JSON and Perfetto exports; Chat turn/findings CSV is unavailable.")
        return 1
    samples = analyzer.counter_samples()
    include_sensitive = args.include_sensitive
    if args.format == "perfetto":
        text = json.dumps(perfetto_trace(analysis, samples, include_sensitive=include_sensitive), ensure_ascii=False)
        text += "\n"
    elif args.format == "json":
        text = json.dumps(
            analysis_json(analysis, samples, include_sensitive=include_sensitive), indent=2, ensure_ascii=False
        )
        text += "\n"
    elif args.format == "csv":
        text = turns_csv(analysis)
    else:
        text = findings_csv(analysis, include_sensitive=include_sensitive)
    if (write_error := _commit_export(out, text, force=args.force)) is not None:
        _write_stderr(f"Error: {write_error}")
        return 1
    executions = f"{len(analysis.turns)} turns"
    if analysis.workflow_runs:
        executions += f", {len(analysis.workflow_runs)} workflow runs"
    _write_stdout(f"Wrote {args.format} export to {out} ({executions}, {len(analysis.findings)} findings)")
    return 0
