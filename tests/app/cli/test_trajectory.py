# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tests for the trajectory export CLI command."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest

from chrys.app.cli import trajectory as trajectory_module
from chrys.app.cli.trajectory import main
from chrys.foundation.config.settings import Settings
from chrys.service.analytics import AnalysisAvailability, SessionCounterSamples, TrajectoryAnalysis, TrajectoryAnalyzer
from tests.service.analytics._events import EventLog

_NS = 1_000_000_000

_REAL_PREPARE_RUNTIME = trajectory_module._prepare_runtime


@pytest.fixture(autouse=True)
def prepare_runtime_calls(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Stub the environment bootstrap: it mutates process-global state."""
    calls: list[str] = []

    def prepare() -> Settings:
        calls.append("prepare")
        return Settings()

    monkeypatch.setattr(trajectory_module, "_prepare_runtime", prepare)
    return calls


def _write_events(path: Path) -> None:
    log = EventLog()
    log.coverage()
    log.add("turn.started", 0, payload={"turn_number": 1})
    log.span(
        "tool.operation",
        "a" * 32,
        0,
        _NS,
        start_payload={"tool_name": "read", "tool_kind": "filesystem.read"},
    )
    log.add("turn.finished", _NS, payload={"end_reason": "cancelled", "duration_ms": 0})
    path.parent.mkdir(parents=True, exist_ok=True)
    log.write(path)


def test_export_perfetto_writes_trace_and_summary(tmp_path, capsys) -> None:
    events = tmp_path / "events.jsonl"
    _write_events(events)
    out = tmp_path / "trace.json"

    assert main(["export", "--events", str(events), "--out", str(out)]) == 0

    trace = json.loads(out.read_text(encoding="utf-8"))
    assert isinstance(trace["traceEvents"], list)
    captured = capsys.readouterr()
    assert "Wrote perfetto export" in captured.out
    assert "1 turns" in captured.out
    assert captured.err == ""


def test_export_refuses_existing_output_without_force(tmp_path, capsys) -> None:
    events = tmp_path / "events.jsonl"
    _write_events(events)
    out = tmp_path / "trace.json"
    out.write_text("occupied", encoding="utf-8")

    assert main(["export", "--events", str(events), "--out", str(out)]) == 1
    assert "--force" in capsys.readouterr().err
    assert out.read_text(encoding="utf-8") == "occupied"

    assert main(["export", "--events", str(events), "--out", str(out), "--force"]) == 0
    assert out.read_text(encoding="utf-8") != "occupied"


@pytest.mark.parametrize("use_alias", [False, True])
def test_export_force_refuses_to_overwrite_the_events_source(tmp_path, capsys, use_alias: bool) -> None:
    events = tmp_path / "events.jsonl"
    _write_events(events)
    original = events.read_bytes()
    out = tmp_path / "events-alias.jsonl" if use_alias else events
    if use_alias:
        out.hardlink_to(events)

    assert main(["export", "--events", str(events), "--out", str(out), "--force"]) == 1

    assert "trajectory events source" in capsys.readouterr().err
    assert events.read_bytes() == original
    assert out.read_bytes() == original


def test_export_json_and_csv_formats(tmp_path) -> None:
    events = tmp_path / "events.jsonl"
    _write_events(events)

    json_out = tmp_path / "analysis.json"
    assert main(["export", "--events", str(events), "--format", "json", "--out", str(json_out)]) == 0
    payload = json.loads(json_out.read_text(encoding="utf-8"))
    assert payload["schema"] == "chrys.trajectory.export/1"
    assert str(payload["session"]["path"]).startswith("redacted:")

    csv_out = tmp_path / "turns.csv"
    assert main(["export", "--events", str(events), "--format", "csv", "--out", str(csv_out)]) == 0
    assert csv_out.read_text(encoding="utf-8").startswith("turn_number,turn_id")

    findings_out = tmp_path / "findings.csv"
    assert main(["export", "--events", str(events), "--format", "findings-csv", "--out", str(findings_out)]) == 0
    assert findings_out.read_text(encoding="utf-8").startswith("rule_id,severity")


@pytest.mark.parametrize("format_", ["perfetto", "json", "csv", "findings-csv"])
def test_export_warns_when_integrity_damage_can_hide_data(tmp_path, capsys, format_: str) -> None:
    events = tmp_path / "events.jsonl"
    events.write_text("not-json\n", encoding="utf-8")
    out = tmp_path / f"analysis-{format_}.out"

    assert main(["export", "--events", str(events), "--format", format_, "--out", str(out)]) == 0

    assert out.is_file()
    assert "Warning: trajectory log integrity is unresolved" in capsys.readouterr().err


@pytest.mark.parametrize("format_", ["perfetto", "json", "csv", "findings-csv"])
def test_export_warns_for_an_existing_empty_log(tmp_path, capsys, format_: str) -> None:
    events = tmp_path / "events.jsonl"
    events.write_bytes(b"")
    out = tmp_path / f"analysis-{format_}.out"

    assert main(["export", "--events", str(events), "--format", format_, "--out", str(out)]) == 0

    assert out.is_file()
    assert "Warning: trajectory log integrity is unresolved" in capsys.readouterr().err


def test_json_export_surfaces_an_entirely_corrupt_events_log(tmp_path) -> None:
    events = tmp_path / "events.jsonl"
    events.write_text("not-json\n", encoding="utf-8")
    out = tmp_path / "analysis.json"

    assert main(["export", "--events", str(events), "--format", "json", "--out", str(out)]) == 0

    payload = json.loads(out.read_text(encoding="utf-8"))
    assert payload["diagnostics"]["corrupt_line_count"] == 1
    assert payload["overview"]["elapsed_ns"] == {
        "value": 0,
        "precision": "unresolved",
        "reason": "session trajectory integrity is unresolved: corrupt lines",
    }


def test_json_export_with_full_payload_round_trips_surrogateescaped_event_path(tmp_path, monkeypatch) -> None:
    events = tmp_path / "events-\udcff.jsonl"
    out = tmp_path / "analysis.json"
    analysis = TrajectoryAnalysis(
        availability=AnalysisAvailability.AVAILABLE,
        path=events,
        generation=1,
    )
    original_is_file = Path.is_file

    def is_file(path: Path) -> bool:
        return path == events or original_is_file(path)

    monkeypatch.setattr(Path, "is_file", is_file)
    monkeypatch.setattr(TrajectoryAnalyzer, "load", lambda _self, _path: analysis)
    monkeypatch.setattr(TrajectoryAnalyzer, "counter_samples", lambda _self: SessionCounterSamples({}, {}))

    assert (
        main(
            [
                "export",
                "--events",
                str(events),
                "--format",
                "json",
                "--out",
                str(out),
                "--include-sensitive",
            ]
        )
        == 0
    )

    payload = json.loads(out.read_text(encoding="utf-8"))
    assert payload["session"]["path"] == str(events)


def test_export_errors_on_missing_events_file(tmp_path, capsys) -> None:
    out = tmp_path / "trace.json"

    assert main(["export", "--events", str(tmp_path / "absent.jsonl"), "--out", str(out)]) == 1

    assert "events file not found" in capsys.readouterr().err
    assert not out.exists()


def test_export_error_paths_are_safe_for_strict_utf8_output(tmp_path, monkeypatch) -> None:
    class StrictUtf8Stream:
        def __init__(self) -> None:
            self.output = bytearray()

        def write(self, value: str) -> int:
            self.output.extend(value.encode("utf-8"))
            return len(value)

    stream = StrictUtf8Stream()
    events = tmp_path / "absent-\udcff.jsonl"
    monkeypatch.setattr(trajectory_module.sys, "stderr", stream)

    assert main(["export", "--events", str(events), "--out", str(tmp_path / "trace.json")]) == 1

    assert b"absent-\\udcff.jsonl" in stream.output


def test_export_resolves_session_id_prefixes(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("CHRYS_SESSION_ROOT_DIR", str(tmp_path / "root"))
    sessions = tmp_path / "root" / "sessions"
    _write_events(sessions / "abc123def456" / "trajectory" / "events.jsonl")
    out = tmp_path / "trace.json"

    assert main(["export", "--session", "abc123", "--out", str(out)]) == 0
    assert out.is_file()

    (sessions / "abc999000111").mkdir()
    assert main(["export", "--session", "abc", "--out", str(out), "--force"]) == 1
    assert "ambiguous" in capsys.readouterr().err

    assert main(["export", "--session", "ffff", "--out", str(out), "--force"]) == 1
    assert "no session matches" in capsys.readouterr().err

    assert main(["export", "--session", "abc999", "--out", str(out), "--force"]) == 1
    assert "has no trajectory events log" in capsys.readouterr().err


def test_export_resolves_full_canonical_session_ids(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("CHRYS_SESSION_ROOT_DIR", str(tmp_path / "root"))
    sessions = tmp_path / "root" / "sessions"
    _write_events(sessions / "4201eebcca45" / "trajectory" / "events.jsonl")
    out = tmp_path / "trace.json"

    full_id = "4201eebc-ca45-4328-8882-272f3d7c41cb"
    assert main(["export", "--session", full_id, "--out", str(out)]) == 0
    assert out.is_file()

    dashless_upper = full_id.replace("-", "").upper()
    assert main(["export", "--session", dashless_upper, "--out", str(out), "--force"]) == 0

    # A unique prefix longer than the short folder projection still selects
    # the session: only its first short-id-length characters can match.
    assert main(["export", "--session", full_id.replace("-", "")[:16], "--out", str(out), "--force"]) == 0
    assert main(["export", "--session", full_id[:21], "--out", str(out), "--force"]) == 0

    # A 32-char selector that is not hex must not be truncated into an
    # accidental prefix match against the short directory name.
    lookalike = "4201eebcca45" + "z" * 20
    assert main(["export", "--session", lookalike, "--out", str(out), "--force"]) == 1
    assert "no session matches" in capsys.readouterr().err


def test_export_refuses_output_created_during_analysis_without_force(tmp_path, monkeypatch, capsys) -> None:
    events = tmp_path / "events.jsonl"
    _write_events(events)
    out = tmp_path / "trace.json"
    real_load = TrajectoryAnalyzer.load

    def load_and_occupy(self: TrajectoryAnalyzer, path: Path) -> TrajectoryAnalysis:
        out.write_text("occupied", encoding="utf-8")
        return real_load(self, path)

    monkeypatch.setattr(TrajectoryAnalyzer, "load", load_and_occupy)

    assert main(["export", "--events", str(events), "--out", str(out)]) == 1
    assert "--force" in capsys.readouterr().err
    assert out.read_text(encoding="utf-8") == "occupied"


def test_export_analyzes_with_the_configured_verify_commands(tmp_path, monkeypatch) -> None:
    """The export must classify with the same settings the TUI dashboard uses."""
    events = tmp_path / "events.jsonl"
    _write_events(events)
    out = tmp_path / "trace.json"
    monkeypatch.setattr(trajectory_module, "_prepare_runtime", lambda: Settings(trajectory_verify_commands="proofread"))
    captured: list[str] = []
    real_init = TrajectoryAnalyzer.__init__

    def capture_init(self: TrajectoryAnalyzer, *args: Any, **kwargs: Any) -> None:
        captured.append(kwargs["verify_commands"])
        real_init(self, *args, **kwargs)

    monkeypatch.setattr(TrajectoryAnalyzer, "__init__", capture_init)

    assert main(["export", "--events", str(events), "--out", str(out)]) == 0
    assert captured == ["proofread"]


def test_export_reports_session_directory_read_errors(tmp_path, monkeypatch, capsys) -> None:
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    monkeypatch.setattr(trajectory_module, "resolve_sessions_dir", lambda *, create=False: sessions)
    original_iterdir = Path.iterdir

    def denied(path: Path):
        if path == sessions:
            raise OSError("permission denied")
        return original_iterdir(path)

    monkeypatch.setattr(Path, "iterdir", denied)

    assert main(["export", "--session", "abc", "--out", str(tmp_path / "trace.json")]) == 1
    assert "could not read sessions directory" in capsys.readouterr().err


def test_export_prepares_the_runtime_before_resolving_the_sessions_dir(tmp_path, monkeypatch, capsys) -> None:
    """Settings must load first or the export reads the wrong session root."""
    order: list[str] = []

    def prepare() -> Settings:
        order.append("prepare")
        return Settings()

    monkeypatch.setattr(trajectory_module, "_prepare_runtime", prepare)

    def resolve(*, create: bool = True) -> Path:
        assert create is False
        order.append("resolve")
        return tmp_path / "absent"

    monkeypatch.setattr(trajectory_module, "resolve_sessions_dir", resolve)

    assert main(["export", "--session", "abc", "--out", str(tmp_path / "trace.json")]) == 1

    assert order == ["prepare", "resolve"]
    assert "no sessions directory" in capsys.readouterr().err


def test_prepare_runtime_bootstraps_without_telemetry_and_reports_warnings(monkeypatch, capsys) -> None:
    from chrys.foundation.config.settings_store import LoadedSettings
    from chrys.foundation.events.types import Warning
    from chrys.orchestration import startup as startup_module
    from chrys.orchestration.startup import RuntimeBootstrap

    loaded_settings = Settings(trajectory_verify_commands="proofread")

    def fake_bootstrap_runtime(*, configure_stdio: bool, setup_telemetry: bool, project_root: Path) -> RuntimeBootstrap:
        assert configure_stdio is True
        assert setup_telemetry is False
        # The working directory's project layer must apply, like the TUI and
        # ``chrys run``, or project-level verify commands are silently lost.
        assert project_root == Path(os.getcwd())
        return RuntimeBootstrap(
            loaded=LoadedSettings(settings=loaded_settings, provenance={}),
            warnings=[Warning(code="probe", message="probe warning")],
        )

    monkeypatch.setattr(startup_module, "bootstrap_runtime", fake_bootstrap_runtime)

    assert _REAL_PREPARE_RUNTIME() is loaded_settings

    assert "Warning: probe warning" in capsys.readouterr().err


def test_export_turns_writes_the_registry_merged_projection(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("CHRYS_SESSION_ROOT_DIR", str(tmp_path / "root"))
    session_dir = tmp_path / "root" / "sessions" / "abc123def456"
    _write_events(session_dir / "trajectory" / "events.jsonl")
    out = tmp_path / "turns.json"
    recorded: list[tuple[str, bool]] = []

    class _Store:
        def __init__(self, _root: Path) -> None:
            pass

        async def load_session_transcript(self, session_id: str, *, prefer_recovery: bool = False):
            from chrys.service.state.store import SessionTranscript
            from chrys.service.trajectory.state import TurnRecord

            recorded.append((session_id, prefer_recovery))
            return SessionTranscript(
                messages=[],
                turns=[TurnRecord(turn_number=1, turn_id="a" * 32, user_message_id="msg-1")],
            )

    monkeypatch.setattr("chrys.service.state.store.JsonFileStateStore", _Store)

    assert main(["export", "--session", "abc123", "--format", "turns", "--out", str(out)]) == 0

    payload = json.loads(out.read_text(encoding="utf-8"))
    assert payload["sessionId"] == "abc123def456"
    assert recorded == [("abc123def456", True)]
    captured = capsys.readouterr()
    assert "Wrote turns export" in captured.out
    assert "session abc123def456" in captured.out


def test_export_turns_requires_a_session(tmp_path, capsys) -> None:
    events = tmp_path / "events.jsonl"
    _write_events(events)
    out = tmp_path / "turns.json"

    assert main(["export", "--events", str(events), "--format", "turns", "--out", str(out)]) == 1

    assert "--format turns requires --session" in capsys.readouterr().err
    assert not out.exists()


def test_export_turns_refuses_to_overwrite_the_events_source(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("CHRYS_SESSION_ROOT_DIR", str(tmp_path / "root"))
    events = tmp_path / "root" / "sessions" / "abc123def456" / "trajectory" / "events.jsonl"
    _write_events(events)
    original = events.read_bytes()

    assert main(["export", "--session", "abc123", "--format", "turns", "--out", str(events), "--force"]) == 1

    assert "trajectory events source" in capsys.readouterr().err
    assert events.read_bytes() == original


def test_export_turns_reports_atomic_write_errors(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("CHRYS_SESSION_ROOT_DIR", str(tmp_path / "root"))
    _write_events(tmp_path / "root" / "sessions" / "abc123def456" / "trajectory" / "events.jsonl")
    out = tmp_path / "turns.json"

    def fail_write(_path: Path, _text: str) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(trajectory_module, "atomic_create_text", fail_write)

    assert main(["export", "--session", "abc123", "--format", "turns", "--out", str(out)]) == 1
    assert "could not write export" in capsys.readouterr().err
    assert not out.exists()


def test_export_reports_atomic_write_errors(tmp_path, monkeypatch, capsys) -> None:
    events = tmp_path / "events.jsonl"
    _write_events(events)
    out = tmp_path / "trace.json"

    def fail_write(_path: Path, _text: str) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(trajectory_module, "atomic_create_text", fail_write)

    assert main(["export", "--events", str(events), "--out", str(out)]) == 1
    assert "could not write export" in capsys.readouterr().err
    assert not out.exists()
