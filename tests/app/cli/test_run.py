# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tests for ``chrys run``."""

from __future__ import annotations

import asyncio
import io
import json
import logging
import os
import re
import sys
from collections.abc import Callable, Coroutine
from pathlib import Path
from types import SimpleNamespace
from typing import Any, ClassVar
from unittest.mock import create_autospec

import pytest

from chrys.app.cli import headless
from chrys.app.cli import run as run_cli
from chrys.foundation.branding import APP_COMMAND
from chrys.foundation.config.coercion import CoerceReason, invalid
from chrys.foundation.config.context import EvalContext
from chrys.foundation.config.process_env import freeze_process_env
from chrys.foundation.config.settings import Settings
from chrys.foundation.config.settings_store import LoadedSettings, SettingsWarning, load_settings
from chrys.foundation.config.spec import SettingOrigin, Source
from chrys.foundation.config.user_settings import user_settings_path
from chrys.foundation.config.warnings import settings_warning_events
from chrys.foundation.events.bus import EventBus
from chrys.foundation.events.types import (
    AGENT_LOAD_PHASE_MCP,
    AGENT_LOAD_STATUS_RUNNING,
    AgentLoadFinished,
    AgentLoadProgress,
    AgentRuntimeDetails,
    Error,
    Event,
    InvocationContentDelta,
    InvocationContentFinished,
    InvocationToolCallStart,
    RuntimeModelDetails,
    Warning,
)
from chrys.foundation.i18n import DisplaySequence, Localizer
from chrys.foundation.i18n.formatting import format_message
from chrys.foundation.models.invocations import InvocationOrigin
from chrys.orchestration import session_host as session_host_module
from chrys.orchestration import startup as startup_module
from chrys.orchestration.session_host import (
    AgentProfileNotFoundError,
    AmbiguousSessionIdError,
    HeadlessRunError,
    HeadlessRunResult,
    SessionNotFoundError,
)
from chrys.service.profiles.models.schema import ModelProfile
from tests.support.streams import FailingTextStream


@pytest.fixture(autouse=True)
def _pin_locale_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """Shield assertions from the developer's ambient CHRYS_LOCALE or system locale."""
    monkeypatch.setenv("CHRYS_LOCALE", "en")


class FakeHost:
    """Minimal ``ChrysSessionHost`` stand-in for CLI tests."""

    instances: ClassVar[list[FakeHost]] = []
    restored_loaded: ClassVar[LoadedSettings | None] = None
    run_events: ClassVar[list[Event]] = []
    """Events handed to ``on_event`` during the fake turn, in order."""
    start_events: ClassVar[list[Event]] = []
    """Events published on the host bus while ``start()`` runs."""

    def __init__(self, **kwargs) -> None:
        self.kwargs = kwargs
        self.shutdown_called = False
        self.started = False
        self.session_id = "session-1"
        self.model_profile_pinned = False
        self.event_bus = EventBus()
        self.engine = SimpleNamespace(
            pin_model_profile=lambda: setattr(self, "model_profile_pinned", True),
            loaded_settings=type(self).restored_loaded or LoadedSettings(settings=Settings(), provenance={}),
            runtime_details=AgentRuntimeDetails(model=RuntimeModelDetails(name="Fake Model")),
            workspace=None,
        )
        self.on_event: Callable[[Event], None] | None = None
        self.running_background_tasks = 0
        FakeHost.instances.append(self)

    async def start(self) -> None:
        self.started = True
        for event in type(self).start_events:
            await self.event_bus.publish(event)

    async def run_until_final(
        self,
        prompt: str,
        *,
        timeout: float | None = None,
        on_event: Callable[[Event], None] | None = None,
    ) -> HeadlessRunResult:
        self.prompt = prompt
        self.timeout = timeout
        self.on_event = on_event
        self.run_cwd = Path.cwd()
        if on_event is not None:
            for event in type(self).run_events:
                on_event(event)
        return HeadlessRunResult(text="final text", session_id="session-1", events=[])

    async def shutdown(self) -> None:
        self.shutdown_called = True


class FakeModelRegistry:
    """Small model registry stand-in for explicit ``--model`` CLI tests."""

    profiles: ClassVar[list[ModelProfile]] = [
        ModelProfile(id="model-id", name="Friendly Model"),
    ]
    instances: ClassVar[list[FakeModelRegistry]] = []

    def __init__(self) -> None:
        FakeModelRegistry.instances.append(self)

    def load_all(self) -> int:
        return len(self.profiles)

    def get(self, profile_id: str) -> ModelProfile | None:
        for profile in self.profiles:
            if profile.id == profile_id:
                return profile
        return None

    def list_profiles(self) -> list[ModelProfile]:
        return list(self.profiles)


def _patch_host(monkeypatch: pytest.MonkeyPatch) -> None:
    FakeHost.instances.clear()
    FakeHost.restored_loaded = None
    FakeHost.run_events = []
    FakeHost.start_events = []
    monkeypatch.setattr(run_cli, "ChrysSessionHost", FakeHost)


def _patch_runtime(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_host(monkeypatch)
    monkeypatch.setattr(headless, "prepare_runtime", _settings)


def _patch_model_registry(monkeypatch: pytest.MonkeyPatch) -> None:
    FakeModelRegistry.instances.clear()
    monkeypatch.setattr(run_cli, "ModelProfileRegistry", FakeModelRegistry)


def _prepared_runtime(
    settings: Settings | None = None,
    *,
    warnings: list[Warning] | None = None,
) -> headless.PreparedRuntime:
    resolved_settings = settings or Settings()
    return headless.PreparedRuntime(
        loaded=LoadedSettings(settings=resolved_settings, provenance={}),
        localizer=Localizer("en"),
        pending_warnings=list(warnings or []),
    )


def _settings(*, restoring_session: bool = False) -> headless.PreparedRuntime:
    return _prepared_runtime()


def _run_in_locales(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    argv: list[str],
) -> dict[str, tuple[int, str, str]]:
    results: dict[str, tuple[int, str, str]] = {}
    for locale in ("en", "zh-Hans"):
        monkeypatch.setenv("CHRYS_LOCALE", locale)
        rc = run_cli.main(argv)
        output = capsys.readouterr()
        results[locale] = (rc, output.out, output.err)
    return results


def _patch_failure_host(monkeypatch: pytest.MonkeyPatch, failure_factory) -> None:
    class FailureHost(FakeHost):
        async def run_until_final(
            self,
            prompt: str,
            *,
            timeout: float | None = None,
            on_event: Callable[[Event], None] | None = None,
        ) -> HeadlessRunResult:
            self.prompt = prompt
            self.timeout = timeout
            raise failure_factory()

    monkeypatch.setattr(run_cli, "ChrysSessionHost", FailureHost)


def _fake_bootstrap(
    *,
    warnings: list[Warning] | None = None,
) -> Callable[..., startup_module.RuntimeBootstrap]:
    """Stand in for ``bootstrap_runtime`` without skipping the settings load.

    The retry policy under test is decided *inside* the load, from the
    ``eval_context`` the entrypoint passes, so a double that hands back a
    pre-made ``Settings`` would assert nothing about the thing it names.
    """

    def _bootstrap(
        *, configure_stdio: bool, eval_context: EvalContext, project_root: Path | None
    ) -> startup_module.RuntimeBootstrap:
        assert configure_stdio is True
        loaded = load_settings(eval_context=eval_context, project_root=project_root)
        return startup_module.RuntimeBootstrap(loaded=loaded, warnings=list(warnings or []))

    return _bootstrap


def _patch_bootstrap_warning(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_host(monkeypatch)
    monkeypatch.setattr(
        headless,
        "bootstrap_runtime",
        _fake_bootstrap(warnings=[Warning(code="invalid_no_proxy", message="NO_PROXY is invalid for httpx.")]),
    )


def test_run_command_configures_null_logging_when_unconfigured() -> None:
    root = logging.getLogger()
    original_handlers = root.handlers[:]
    root.handlers.clear()
    try:
        headless.configure_logging()

        assert any(isinstance(handler, logging.NullHandler) for handler in root.handlers)
    finally:
        root.handlers[:] = original_handlers


@pytest.mark.parametrize("quiet", (False, True), ids=("progress", "quiet"))
def test_run_command_prints_final_text(monkeypatch: pytest.MonkeyPatch, capsys, quiet: bool) -> None:
    _patch_runtime(monkeypatch)

    rc = run_cli.main(["hello", "--agent", "Headless", *(["--quiet"] if quiet else [])])

    out = capsys.readouterr()
    assert rc == 0
    # The answer alone goes to stdout; progress, when shown, ends with the summary on stderr.
    assert out.out == "final text\n"
    if quiet:
        assert out.err == ""
    else:
        assert re.fullmatch(r"\n✓ Done · \d+\.\ds · 0 tool calls · session session1\n", out.err)
    assert FakeHost.instances[0].kwargs["profile_name"] == "Headless"
    assert FakeHost.instances[0].kwargs["session_id"] is None
    assert FakeHost.instances[0].kwargs["on_successful_turn"] is run_cli.on_buddy_successful_turn
    assert FakeHost.instances[0].shutdown_called


@pytest.mark.parametrize("as_json", (False, True), ids=("text", "json"))
def test_run_command_says_it_stops_the_commands_the_run_left_running(
    monkeypatch: pytest.MonkeyPatch, capsys, as_json: bool
) -> None:
    class BackgroundHost(FakeHost):
        async def run_until_final(self, prompt: str, **kwargs: Any) -> HeadlessRunResult:
            self.running_background_tasks = 2
            return await super().run_until_final(prompt, **kwargs)

    _patch_runtime(monkeypatch)
    monkeypatch.setattr(run_cli, "ChrysSessionHost", BackgroundHost)

    rc = run_cli.main(["hello", "-a", "Headless", "--quiet", *(["--json"] if as_json else [])])

    err = capsys.readouterr().err
    assert rc == 0
    if as_json:
        assert json.loads(err) == {
            "warning": "Stopping 2 background commands the run left running.",
            "code": "background_tasks_stopping",
        }
    else:
        assert err == "Warning: Stopping 2 background commands the run left running.\n"


def test_a_failed_run_still_says_it_stops_the_commands_it_left_running(monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    class FailingHost(FakeHost):
        async def run_until_final(self, prompt: str, **kwargs: Any) -> HeadlessRunResult:
            self.running_background_tasks = 1
            raise HeadlessRunError(Error(code="boom", message="failed", session_id="session-1"))

    _patch_runtime(monkeypatch)
    monkeypatch.setattr(run_cli, "ChrysSessionHost", FailingHost)

    rc = run_cli.main(["hello", "-a", "Headless", "--quiet"])

    assert rc == 1
    assert capsys.readouterr().err == "Warning: Stopping 1 background command the run left running.\nError: failed\n"
    assert FailingHost.instances[0].shutdown_called


def test_run_command_accepts_short_flags(monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    _patch_runtime(monkeypatch)
    _patch_model_registry(monkeypatch)

    rc = run_cli.main(["hello", "-a", "Headless", "-m", "Friendly Model", "-s", "abc"])

    out = capsys.readouterr()
    assert rc == 0
    assert out.out == "final text\n"
    assert FakeHost.instances[0].kwargs["profile_name"] == "Headless"
    assert FakeHost.instances[0].kwargs["session_id"] == "abc"
    assert FakeHost.instances[0].kwargs["loaded_settings"].settings.model_profile == "model-id"
    assert FakeHost.instances[0].kwargs["loaded_settings"].settings.model_profile_override == ""


def test_run_command_model_sets_active_profile(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _patch_host(monkeypatch)
    monkeypatch.setattr(
        headless,
        "prepare_runtime",
        lambda *, restoring_session: _prepared_runtime(Settings(model_profile="env-model")),
    )
    _patch_model_registry(monkeypatch)

    rc = run_cli.main(["hello", "--agent", "Headless", "--model", "Friendly Model", "--quiet"])

    out = capsys.readouterr()
    assert rc == 0
    assert out.err == ""
    settings = FakeHost.instances[0].kwargs["loaded_settings"].settings
    assert settings.model_profile == "model-id"
    assert settings.model_profile_override == ""
    assert settings.model_profile_override_sub_agents is False
    assert FakeHost.instances[0].kwargs["model_registry"] is FakeModelRegistry.instances[0]
    # Host-local selection: pinned so a reload cannot revert it, and never
    # parked in the process environment.
    assert FakeHost.instances[0].model_profile_pinned
    assert "CHRYS_MODEL_PROFILE" not in os.environ


def test_run_command_reports_missing_model_selection_text_mode(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _patch_runtime(monkeypatch)
    _patch_model_registry(monkeypatch)

    rc = run_cli.main(["hello", "--agent", "Headless", "--model", "Missing"])

    out = capsys.readouterr()
    assert rc == 1
    assert out.out == ""
    assert out.err == "Error: Model profile not found: Missing. Available model profiles: Friendly Model (model-id)\n"
    assert FakeHost.instances == []


def test_run_command_reports_missing_model_selection_json_mode(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _patch_runtime(monkeypatch)
    _patch_model_registry(monkeypatch)

    rc = run_cli.main(["hello", "--agent", "Headless", "--model", "Missing", "--json"])

    out = capsys.readouterr()
    assert rc == 1
    assert out.out == ""
    assert json.loads(out.err) == {
        "error": "Model profile not found: Missing. Available model profiles: Friendly Model (model-id)",
        "code": "model_profile_not_found",
    }
    assert FakeHost.instances == []


def test_run_command_reads_task_file_as_prompt(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys) -> None:
    task = tmp_path / "task.md"
    task.write_bytes(b"first line\nsecond line\n")
    _patch_runtime(monkeypatch)

    rc = run_cli.main(["--task", str(task), "--agent", "Headless", "--quiet"])

    out = capsys.readouterr()
    assert rc == 0
    assert out.err == ""
    assert FakeHost.instances[0].prompt == "first line\nsecond line\n"


def test_run_command_accepts_short_task_flag_and_non_utf8_file(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys,
) -> None:
    task = tmp_path / "task.txt"
    task.write_bytes("cafe\u0301\n".encode("utf-16"))
    _patch_runtime(monkeypatch)

    rc = run_cli.main(["-t", str(task), "--agent", "Headless", "--quiet"])

    out = capsys.readouterr()
    assert rc == 0
    assert out.err == ""
    assert FakeHost.instances[0].prompt == "cafe\u0301\n"


def test_run_command_task_file_strips_utf8_bom(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys) -> None:
    task = tmp_path / "task.md"
    task.write_bytes(b"\xef\xbb\xbfhello")
    _patch_runtime(monkeypatch)

    rc = run_cli.main(["--task", str(task), "--agent", "Headless", "--quiet"])

    out = capsys.readouterr()
    assert rc == 0
    assert out.err == ""
    assert FakeHost.instances[0].prompt == "hello"


def test_run_command_empty_task_file_passes_empty_prompt(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys,
) -> None:
    task = tmp_path / "empty.md"
    task.write_bytes(b"")
    _patch_runtime(monkeypatch)

    rc = run_cli.main(["--task", str(task), "--agent", "Headless", "--quiet"])

    out = capsys.readouterr()
    assert rc == 0
    assert out.err == ""
    assert FakeHost.instances[0].prompt == ""


def test_run_command_task_file_resolves_relative_to_workdir(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys,
) -> None:
    outside_task_dir = tmp_path / "tasks"
    outside_task_dir.mkdir()
    (outside_task_dir / "x.md").write_text("outside", encoding="utf-8")
    workdir = tmp_path / "project"
    inside_task_dir = workdir / "tasks"
    inside_task_dir.mkdir(parents=True)
    (inside_task_dir / "x.md").write_text("inside", encoding="utf-8")
    _patch_runtime(monkeypatch)
    monkeypatch.chdir(tmp_path)

    rc = run_cli.main(["--task", "tasks/x.md", "--agent", "Headless", "--quiet"])

    out = capsys.readouterr()
    assert rc == 0
    assert out.err == ""
    assert FakeHost.instances[0].prompt == "outside"

    FakeHost.instances.clear()
    rc = run_cli.main(["--task", "tasks/x.md", "--agent", "Headless", "-C", str(workdir), "--quiet"])

    out = capsys.readouterr()
    assert rc == 0
    assert out.err == ""
    assert FakeHost.instances[0].prompt == "inside"


def test_run_command_changes_directory_before_bootstrapping(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys,
) -> None:
    """Bootstrap reads project settings, so ``-C`` must land before it runs."""
    _patch_host(monkeypatch)
    seen: list[Path] = []

    def recording_prepare(*, restoring_session: bool) -> headless.PreparedRuntime:
        seen.append(Path(os.getcwd()))
        return _prepared_runtime()

    monkeypatch.setattr(headless, "prepare_runtime", recording_prepare)
    workdir = tmp_path / "project"
    workdir.mkdir()
    monkeypatch.chdir(tmp_path)

    rc = run_cli.main(["hello", "--agent", "Headless", "-C", str(workdir), "--quiet"])

    assert rc == 0
    assert capsys.readouterr().err == ""
    assert [path.resolve() for path in seen] == [workdir.resolve()]


def test_run_command_json_output(monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    _patch_runtime(monkeypatch)

    rc = run_cli.main(["hello", "--agent", "Headless", "--session", "abc", "--json"])

    out = capsys.readouterr()
    assert rc == 0
    assert out.err == ""
    assert out.out.endswith("\n")
    payload = json.loads(out.out)
    assert payload["session_id"] == "session-1"
    assert payload["result"] == "final text"
    assert isinstance(payload["duration"], (int, float))
    assert payload["duration"] >= 0
    assert FakeHost.instances[0].kwargs["session_id"] == "abc"


def test_run_command_bootstrap_warning_text_mode(monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    _patch_bootstrap_warning(monkeypatch)

    rc = run_cli.main(["hello", "--agent", "Headless"])

    out = capsys.readouterr()
    assert rc == 0
    assert out.out == "final text\n"
    lines = out.err.splitlines()
    assert lines[0] == "Warning: NO_PROXY is invalid for httpx."
    assert lines[-1].startswith("✓ Done · ")


def test_prepare_runtime_applies_headless_retry_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CHRYS_MAX_TRANSIENT_RETRIES", raising=False)
    monkeypatch.setattr(
        headless,
        "bootstrap_runtime",
        _fake_bootstrap(),
    )

    settings = headless.prepare_runtime().settings

    assert settings.frontend_default_max_transient_retries == 18
    assert settings.effective_max_transient_retries() == 18


def test_prepare_runtime_bootstraps_project_free_for_a_session_restore(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A restore's settings come from the saved session's root, not the cwd."""
    bootstrap = create_autospec(headless.bootstrap_runtime, side_effect=_fake_bootstrap())
    monkeypatch.setattr(headless, "bootstrap_runtime", bootstrap)

    headless.prepare_runtime(restoring_session=True)
    headless.prepare_runtime()

    assert [call.kwargs["project_root"] for call in bootstrap.call_args_list] == [None, Path.cwd()]


def test_run_command_marks_the_runtime_as_restoring_only_with_a_session(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _patch_host(monkeypatch)
    seen: list[bool] = []

    def _record(*, restoring_session: bool) -> headless.PreparedRuntime:
        seen.append(restoring_session)
        return _prepared_runtime()

    monkeypatch.setattr(headless, "prepare_runtime", _record)

    assert run_cli.main(["hello", "--agent", "Headless", "-s", "abc"]) == 0
    assert run_cli.main(["hello", "--agent", "Headless"]) == 0
    # A blank id is "no session" to the host, so it must be one here too —
    # a project-free bootstrap ahead of a fresh session would silently drop
    # the working directory's project layer.
    assert run_cli.main(["hello", "--agent", "Headless", "-s", "   "]) == 0
    capsys.readouterr()
    assert seen == [True, False, False]
    assert FakeHost.instances[2].kwargs["session_id"] is None
    assert not FakeHost.instances[2].started


def test_prepare_runtime_composes_the_same_root_independent_warnings_for_a_restore(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Environment and user warnings hold for any root, so a restore keeps them — wording included."""
    monkeypatch.setenv("CHRYS_MAX_TRANSIENT_RETRIES", "nonsense")
    monkeypatch.setattr(
        headless,
        "bootstrap_runtime",
        _fake_bootstrap(warnings=[Warning(code="invalid_no_proxy", message="NO_PROXY is invalid for httpx.")]),
    )

    restoring = headless.prepare_runtime(restoring_session=True)
    fresh = headless.prepare_runtime()

    expected = ["invalid_no_proxy", "invalid_max_transient_retries"]
    assert [warning.code for warning in restoring.pending_warnings] == expected
    assert [warning.code for warning in fresh.pending_warnings] == expected


def _project_rejection() -> SettingsWarning:
    return SettingsWarning(
        key="approval.default_mode",
        origin=SettingOrigin(layer=Source.PROJECT, path=Path("/repo/.chrys/settings.yaml")),
        outcome=invalid("bypass", CoerceReason.LOOSENS_USER_BASELINE),
    )


def test_restore_delta_warnings_reports_only_what_the_bootstrap_could_not_see() -> None:
    user_warning = SettingsWarning(
        key="llm.retry.max_transient",
        origin=SettingOrigin(layer=Source.USER, path=Path("/home/me/.chrys/settings.yaml")),
        outcome=invalid("nonsense", CoerceReason.EXPECTED_NON_NEGATIVE_INT),
    )
    retry_env_warning = SettingsWarning(
        key="llm.retry.max_transient",
        origin=SettingOrigin(layer=Source.ENV),
        outcome=invalid("nonsense", CoerceReason.EXPECTED_NON_NEGATIVE_INT),
    )
    bootstrap_loaded = LoadedSettings(settings=Settings(), provenance={}, warnings=(user_warning,))
    restored_loaded = LoadedSettings(
        settings=Settings(),
        provenance={},
        warnings=(user_warning, retry_env_warning, _project_rejection()),
    )
    pending = settings_warning_events(bootstrap_loaded)

    delta = headless.restore_delta_warnings(restored_loaded, pending)

    # The shared user verdict is already printed, and the env-var retry verdict
    # keeps its compatibility wording in the pending list — only the project
    # layer's rejection is new.
    assert [warning.code for warning in delta] == ["setting_rejected"]
    assert f"approval.default_mode in {Path('/repo/.chrys/settings.yaml')}" in delta[0].message


def test_run_command_writes_the_target_roots_warnings_in_text_mode(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _patch_runtime(monkeypatch)
    restored_loaded = LoadedSettings(settings=Settings(), provenance={}, warnings=(_project_rejection(),))
    FakeHost.restored_loaded = restored_loaded

    rc = run_cli.main(["hello", "--agent", "Headless", "-s", "abc"])

    out = capsys.readouterr()
    assert rc == 0
    assert out.out == "final text\n"
    (expected,) = settings_warning_events(restored_loaded)
    lines = out.err.splitlines()
    assert lines[:3] == [
        "• Restoring session abc…",
        f"Warning: {expected.message}",
        "• Restored session session1 · Fake Model",
    ]
    assert lines[3] == ""
    assert lines[4].startswith("✓ Done · ")
    assert len(lines) == 5
    assert FakeHost.instances[0].started


def test_run_command_quiet_restore_writes_only_the_target_roots_warnings(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _patch_runtime(monkeypatch)
    restored_loaded = LoadedSettings(settings=Settings(), provenance={}, warnings=(_project_rejection(),))
    FakeHost.restored_loaded = restored_loaded

    rc = run_cli.main(["hello", "--agent", "Headless", "-s", "abc", "--quiet"])

    out = capsys.readouterr()
    assert rc == 0
    assert out.out == "final text\n"
    (expected,) = settings_warning_events(restored_loaded)
    assert out.err == f"Warning: {expected.message}\n"


def test_run_command_writes_the_target_roots_warnings_in_json_mode(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _patch_runtime(monkeypatch)
    restored_loaded = LoadedSettings(settings=Settings(), provenance={}, warnings=(_project_rejection(),))
    FakeHost.restored_loaded = restored_loaded

    rc = run_cli.main(["hello", "--agent", "Headless", "-s", "abc", "--json"])

    out = capsys.readouterr()
    assert rc == 0
    (expected,) = settings_warning_events(restored_loaded)
    assert json.loads(out.err.splitlines()[0]) == {
        "warning": expected.message,
        "code": "setting_rejected",
    }


def test_run_command_without_a_session_does_not_drive_an_early_start(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _patch_runtime(monkeypatch)
    FakeHost.restored_loaded = LoadedSettings(settings=Settings(), provenance={}, warnings=(_project_rejection(),))

    rc = run_cli.main(["hello", "--agent", "Headless", "--quiet"])

    out = capsys.readouterr()
    assert rc == 0
    assert out.err == ""
    assert not FakeHost.instances[0].started


@pytest.mark.parametrize(("raw", "expected"), [("7", 7), ("0", 0)])
def test_prepare_runtime_explicit_retry_env_wins_over_headless_default(
    monkeypatch: pytest.MonkeyPatch,
    raw: str,
    expected: int,
) -> None:
    monkeypatch.setenv("CHRYS_MAX_TRANSIENT_RETRIES", raw)
    monkeypatch.setattr(
        headless,
        "bootstrap_runtime",
        _fake_bootstrap(),
    )

    settings = headless.prepare_runtime().settings

    assert settings.frontend_default_max_transient_retries == 18
    assert settings.effective_max_transient_retries() == expected


@pytest.mark.parametrize(
    ("raw", "expected_message"),
    [
        (
            "invalid",
            (
                "Ignoring invalid CHRYS_MAX_TRANSIENT_RETRIES='invalid'; "
                "expected a non-negative integer and will use the frontend default."
            ),
        ),
        (
            "-3",
            (
                "Ignoring invalid CHRYS_MAX_TRANSIENT_RETRIES='-3'; "
                "expected a non-negative integer and will use the frontend default."
            ),
        ),
        (
            "75",
            "CHRYS_MAX_TRANSIENT_RETRIES=75 exceeds the limit of 50; clamping to 50.",
        ),
    ],
)
def test_prepare_runtime_queues_retry_env_warning_without_writing_stderr(
    monkeypatch: pytest.MonkeyPatch,
    capsys,
    raw: str,
    expected_message: str,
) -> None:
    monkeypatch.setenv("CHRYS_MAX_TRANSIENT_RETRIES", raw)
    monkeypatch.setattr(
        headless,
        "bootstrap_runtime",
        _fake_bootstrap(),
    )

    prepared = headless.prepare_runtime()

    assert capsys.readouterr().err == ""
    assert len(prepared.pending_warnings) == 1
    warning = prepared.pending_warnings[0]
    assert warning.code == "invalid_max_transient_retries"
    assert warning.message == expected_message
    assert warning.display_message is not None


def test_prepare_runtime_reports_a_document_retry_value_without_naming_the_variable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The env-var wording would send someone to a variable they never set."""
    user_file = user_settings_path()
    user_file.parent.mkdir(parents=True, exist_ok=True)
    user_file.write_text("llm:\n  retry:\n    max_transient: nonsense\n", encoding="utf-8")
    freeze_process_env()
    monkeypatch.setattr(headless, "bootstrap_runtime", _fake_bootstrap())

    (queued,) = headless.prepare_runtime().pending_warnings

    assert queued.code == "setting_rejected"
    assert "CHRYS_MAX_TRANSIENT_RETRIES" not in queued.message
    assert "llm.retry.max_transient" in queued.message
    assert str(user_file) in queued.message


def test_run_command_passes_headless_retry_default_to_session_host(
    monkeypatch: pytest.MonkeyPatch,
    capsys,
) -> None:
    monkeypatch.delenv("CHRYS_MAX_TRANSIENT_RETRIES", raising=False)
    _patch_host(monkeypatch)
    monkeypatch.setattr(
        headless,
        "bootstrap_runtime",
        _fake_bootstrap(),
    )

    assert run_cli.main(["hello", "--agent", "Headless"]) == 0

    assert capsys.readouterr().out == "final text\n"
    settings = FakeHost.instances[0].kwargs["loaded_settings"].settings
    assert settings.frontend_default_max_transient_retries == 18
    assert settings.effective_max_transient_retries() == 18


def test_run_command_bootstrap_warning_json_mode(monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    _patch_bootstrap_warning(monkeypatch)

    rc = run_cli.main(["hello", "--agent", "Headless", "--json"])

    out = capsys.readouterr()
    assert rc == 0
    assert out.err.endswith("\n")
    assert json.loads(out.err) == {
        "warning": "NO_PROXY is invalid for httpx.",
        "code": "invalid_no_proxy",
    }


def test_run_command_json_warning_does_not_corrupt_stdout(monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    _patch_bootstrap_warning(monkeypatch)

    rc = run_cli.main(["hello", "--agent", "Headless", "--json"])

    out = capsys.readouterr()
    assert rc == 0
    payload = json.loads(out.out)
    assert payload["session_id"] == "session-1"
    assert payload["result"] == "final text"


def test_run_command_reports_headless_error(monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    class ErrorHost(FakeHost):
        async def run_until_final(
            self,
            prompt: str,
            *,
            timeout: float | None = None,
            on_event: Callable[[Event], None] | None = None,
        ) -> HeadlessRunResult:
            self.prompt = prompt
            self.timeout = timeout
            event = Error(code="boom", message="failed", session_id="session-1")
            raise HeadlessRunError(event)

    _patch_runtime(monkeypatch)
    monkeypatch.setattr(run_cli, "ChrysSessionHost", ErrorHost)

    rc = run_cli.main(["hello", "--agent", "Headless"])

    out = capsys.readouterr()
    assert rc == 1
    assert out.err == "Error: failed\n"
    assert ErrorHost.instances[0].shutdown_called


def test_run_command_reports_headless_error_as_json(monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    class ErrorHost(FakeHost):
        async def run_until_final(
            self,
            prompt: str,
            *,
            timeout: float | None = None,
            on_event: Callable[[Event], None] | None = None,
        ) -> HeadlessRunResult:
            self.prompt = prompt
            self.timeout = timeout
            event = Error(code="boom", message='quote "and" newline\n', session_id="session-1")
            raise HeadlessRunError(event)

    _patch_runtime(monkeypatch)
    monkeypatch.setattr(run_cli, "ChrysSessionHost", ErrorHost)

    rc = run_cli.main(["hello", "--agent", "Headless", "--json"])

    out = capsys.readouterr()
    assert rc == 1
    assert out.out == ""
    assert out.err.endswith("\n")
    payload = json.loads(out.err)
    assert payload == {"error": 'quote "and" newline\n', "code": "boom", "session_id": "session-1"}
    assert ErrorHost.instances[0].shutdown_called


def test_run_command_json_error_omits_session_id_when_absent(monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    # An error raised before any session exists must not emit a null/empty
    # session_id field — the key is present only when a runner can actually
    # use it to locate a persisted trajectory.
    class NoSessionErrorHost(FakeHost):
        async def run_until_final(
            self,
            prompt: str,
            *,
            timeout: float | None = None,
            on_event: Callable[[Event], None] | None = None,
        ) -> HeadlessRunResult:
            self.prompt = prompt
            self.timeout = timeout
            event = Error(code="boom", message="failed early", session_id=None)
            raise HeadlessRunError(event)

    _patch_runtime(monkeypatch)
    monkeypatch.setattr(run_cli, "ChrysSessionHost", NoSessionErrorHost)

    rc = run_cli.main(["hello", "--agent", "Headless", "--json"])

    out = capsys.readouterr()
    assert rc == 1
    payload = json.loads(out.err)
    assert payload == {"error": "failed early", "code": "boom"}
    assert NoSessionErrorHost.instances[0].shutdown_called


def test_run_command_reports_session_not_found_with_typed_code(monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    class SessionNotFoundHost(FakeHost):
        async def run_until_final(
            self,
            prompt: str,
            *,
            timeout: float | None = None,
            on_event: Callable[[Event], None] | None = None,
        ) -> HeadlessRunResult:
            raise SessionNotFoundError("Session not found: deadbeef. Recent sessions: aabbcc")

    _patch_runtime(monkeypatch)
    monkeypatch.setattr(run_cli, "ChrysSessionHost", SessionNotFoundHost)

    rc = run_cli.main(["hello", "-a", "Headless", "-s", "deadbeef", "--json"])

    out = capsys.readouterr()
    assert rc == 1
    assert out.out == ""
    payload = json.loads(out.err)
    assert payload == {
        "error": "Session not found: deadbeef. Recent sessions: aabbcc",
        "code": "session_not_found",
    }
    assert SessionNotFoundHost.instances[0].shutdown_called


def test_run_command_reports_session_ambiguous_with_typed_code(monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    class AmbiguousHost(FakeHost):
        async def run_until_final(
            self,
            prompt: str,
            *,
            timeout: float | None = None,
            on_event: Callable[[Event], None] | None = None,
        ) -> HeadlessRunResult:
            raise AmbiguousSessionIdError("Session id 'abc' is ambiguous.")

    _patch_runtime(monkeypatch)
    monkeypatch.setattr(run_cli, "ChrysSessionHost", AmbiguousHost)

    rc = run_cli.main(["hello", "-a", "Headless", "-s", "abc", "--json"])

    out = capsys.readouterr()
    assert rc == 1
    assert out.out == ""
    payload = json.loads(out.err)
    assert payload == {
        "error": "Session id 'abc' is ambiguous.",
        "code": "session_ambiguous",
    }


def test_run_command_reports_profile_not_found_with_typed_code(monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    class ProfileMissingHost(FakeHost):
        async def run_until_final(
            self,
            prompt: str,
            *,
            timeout: float | None = None,
            on_event: Callable[[Event], None] | None = None,
        ) -> HeadlessRunResult:
            raise AgentProfileNotFoundError("Agent profile not found: Missing. Available profiles: Headless")

    _patch_runtime(monkeypatch)
    monkeypatch.setattr(run_cli, "ChrysSessionHost", ProfileMissingHost)

    rc = run_cli.main(["hello", "-a", "Missing", "--json"])

    out = capsys.readouterr()
    assert rc == 1
    assert out.out == ""
    payload = json.loads(out.err)
    assert payload == {
        "error": "Agent profile not found: Missing. Available profiles: Headless",
        "code": "profile_not_found",
    }


def test_run_command_changes_working_directory(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys) -> None:
    workdir = tmp_path / "project"
    workdir.mkdir()
    _patch_runtime(monkeypatch)
    monkeypatch.chdir(tmp_path)

    rc = run_cli.main(["hello", "--agent", "Headless", "--workdir", str(workdir), "--quiet"])

    out = capsys.readouterr()
    assert rc == 0
    assert out.err == ""
    assert FakeHost.instances[0].run_cwd == workdir
    assert FakeHost.instances[0].kwargs["cwd"] == str(workdir)


def test_run_command_accepts_short_workdir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys) -> None:
    workdir = tmp_path / "project"
    workdir.mkdir()
    _patch_runtime(monkeypatch)
    monkeypatch.chdir(tmp_path)

    rc = run_cli.main(["hello", "--agent", "Headless", "-C", str(workdir), "--quiet"])

    out = capsys.readouterr()
    assert rc == 0
    assert out.err == ""
    assert FakeHost.instances[0].run_cwd == workdir
    assert FakeHost.instances[0].kwargs["cwd"] == str(workdir)


def test_run_command_reports_missing_working_directory(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys) -> None:
    _patch_runtime(monkeypatch)
    monkeypatch.chdir(tmp_path)

    rc = run_cli.main(["hello", "--agent", "Headless", "--workdir", "missing"])

    out = capsys.readouterr()
    assert rc == 1
    assert out.out == ""
    assert out.err == "Error: Working directory does not exist: missing\n"
    assert FakeHost.instances == []


def test_run_command_rejects_cwd_pointing_at_file(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys) -> None:
    not_a_dir = tmp_path / "regular.txt"
    not_a_dir.write_text("hello", encoding="utf-8")
    _patch_runtime(monkeypatch)
    monkeypatch.chdir(tmp_path)

    rc = run_cli.main(["hello", "--agent", "Headless", "--workdir", str(not_a_dir)])

    out = capsys.readouterr()
    assert rc == 1
    assert out.out == ""
    assert out.err.startswith("Error: Working directory is not a directory:")
    assert str(not_a_dir.resolve()) in out.err
    assert FakeHost.instances == []


def test_run_command_reports_missing_task_file_before_runtime(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys,
) -> None:
    runtime_calls: list[bool] = []

    def _runtime(*, restoring_session: bool) -> headless.PreparedRuntime:
        runtime_calls.append(True)
        return _prepared_runtime()

    _patch_host(monkeypatch)
    monkeypatch.setattr(headless, "prepare_runtime", _runtime)
    monkeypatch.chdir(tmp_path)

    rc = run_cli.main(["--task", "missing.md", "--agent", "Headless", "--json"])

    out = capsys.readouterr()
    assert rc == 1
    assert out.out == ""
    assert json.loads(out.err) == {
        "error": "Task file does not exist: missing.md",
        "code": "task_file_not_found",
    }
    assert runtime_calls == []
    assert FakeHost.instances == []


def test_run_command_rejects_task_directory(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys) -> None:
    task_dir = tmp_path / "tasks"
    task_dir.mkdir()
    _patch_runtime(monkeypatch)

    rc = run_cli.main(["--task", str(task_dir), "--agent", "Headless"])

    out = capsys.readouterr()
    assert rc == 1
    assert out.out == ""
    assert out.err.startswith("Error: Task path is not a file:")
    assert str(task_dir.resolve()) in out.err
    assert FakeHost.instances == []


def test_run_command_rejects_task_stdin_sentinel(monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    _patch_runtime(monkeypatch)

    rc = run_cli.main(["--task", "-", "--agent", "Headless", "--json"])

    out = capsys.readouterr()
    assert rc == 1
    assert out.out == ""
    assert json.loads(out.err) == {
        "error": "Task file does not exist: -",
        "code": "task_file_not_found",
    }
    assert FakeHost.instances == []


def test_run_command_reports_task_read_failure(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys) -> None:
    task = tmp_path / "task.md"
    task.write_text("content", encoding="utf-8")
    resolved_task = task.resolve()
    real_read_bytes = Path.read_bytes

    def _read_bytes(path: Path) -> bytes:
        if path == resolved_task:
            raise OSError("permission denied")
        return real_read_bytes(path)

    _patch_runtime(monkeypatch)
    monkeypatch.setattr(Path, "read_bytes", _read_bytes)

    rc = run_cli.main(["--task", str(task), "--agent", "Headless", "--json"])

    out = capsys.readouterr()
    assert rc == 1
    assert out.out == ""
    payload = json.loads(out.err)
    assert payload["code"] == "task_file_read_failed"
    assert payload["error"].startswith(f"Failed to read task file: {resolved_task}:")
    assert "permission denied" in payload["error"]
    assert FakeHost.instances == []


def test_run_command_reports_timeout_with_exit_code_124(monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    def _raise_timeout(coro: Coroutine[Any, Any, int]) -> int:
        coro.close()
        raise TimeoutError

    monkeypatch.setattr(asyncio, "run", _raise_timeout)

    rc = run_cli.main(["hello", "--agent", "Headless"])

    out = capsys.readouterr()
    assert rc == 124
    assert out.out == ""
    assert out.err == "Error: Agent run timed out.\n"


def test_run_command_reports_timeout_as_json(monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    def _raise_timeout(coro: Coroutine[Any, Any, int]) -> int:
        coro.close()
        raise TimeoutError

    monkeypatch.setattr(asyncio, "run", _raise_timeout)

    rc = run_cli.main(["hello", "--agent", "Headless", "--json"])

    out = capsys.readouterr()
    assert rc == 124
    assert out.out == ""
    payload = json.loads(out.err)
    assert payload == {"error": "Agent run timed out.", "code": "timeout"}


def test_run_command_reports_keyboard_interrupt(monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    def _raise_keyboard_interrupt(coro: Coroutine[Any, Any, int]) -> int:
        coro.close()
        raise KeyboardInterrupt

    monkeypatch.setattr(asyncio, "run", _raise_keyboard_interrupt)

    rc = run_cli.main(["hello", "--agent", "Headless"])

    out = capsys.readouterr()
    assert rc == 130
    assert out.out == ""
    assert out.err == "Error: Interrupted by user.\n"


def test_run_command_restore_progress_shows_what_the_start_publishes_once(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _patch_runtime(monkeypatch)
    refused = Warning(code="mcp.connect_failed", message="github: refused")
    FakeHost.start_events = [
        AgentLoadProgress(phase=AGENT_LOAD_PHASE_MCP, status=AGENT_LOAD_STATUS_RUNNING, subject="github"),
        refused,
        AgentLoadFinished(agent_profile="Headless", display_name="Reviewer"),
    ]
    # The turn's stream repeats the start's warning and ready event; neither prints twice.
    FakeHost.run_events = [
        Warning(code=refused.code, message=refused.message),
        AgentLoadFinished(agent_profile="Headless", session_id="session-1"),
    ]

    assert run_cli.main(["hello", "--agent", "Headless", "-s", "abc"]) == 0

    out = capsys.readouterr()
    assert out.out == "final text\n"
    lines = out.err.splitlines()
    assert lines[:4] == [
        "• Restoring session abc…",
        "• Connecting MCP server github…",
        "Warning: github: refused",
        "• Restored session session1 · Reviewer · Fake Model",
    ]
    assert lines[4:5] == [""]
    assert lines[5].startswith("✓ Done · ")
    assert len(lines) == 6


def test_run_command_quiet_restore_shows_a_warning_the_start_publishes_once(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _patch_runtime(monkeypatch)
    refused = Warning(code="mcp.connect_failed", message="github: refused")
    FakeHost.start_events = [
        AgentLoadProgress(phase=AGENT_LOAD_PHASE_MCP, status=AGENT_LOAD_STATUS_RUNNING, subject="github"),
        refused,
        AgentLoadFinished(agent_profile="Headless", display_name="Reviewer"),
    ]
    FakeHost.run_events = [Warning(code=refused.code, message=refused.message)]

    assert run_cli.main(["hello", "--agent", "Headless", "-s", "abc", "--quiet"]) == 0

    out = capsys.readouterr()
    assert out.out == "final text\n"
    assert out.err == "Warning: github: refused\n"
    assert FakeHost.instances[0].started


@pytest.mark.parametrize("raw", ["invalid", "75"])
def test_progress_counts_the_retry_warning_reported_under_either_wording(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], raw: str
) -> None:
    monkeypatch.setenv("CHRYS_MAX_TRANSIENT_RETRIES", raw)
    _patch_host(monkeypatch)
    monkeypatch.setattr(headless, "bootstrap_runtime", _fake_bootstrap())
    prepared = headless.prepare_runtime()
    (pending,) = prepared.pending_warnings
    # What an engine start composes for the same verdict: the generic wording, not the compatibility one.
    generic = settings_warning_events(prepared.loaded)
    assert generic
    assert all(event.message != pending.message for event in generic)
    FakeHost.run_events = list(generic)

    assert run_cli.main(["hello", "--agent", "Headless"]) == 0

    warnings = [line for line in capsys.readouterr().err.splitlines() if line.startswith("Warning: ")]
    assert warnings == [f"Warning: {pending.message}"]


@pytest.mark.parametrize("flag", ["--quiet", "--json"])
def test_quiet_output_keeps_the_warnings_a_run_publishes_and_json_keeps_stderr_as_before(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], flag: str
) -> None:
    _patch_runtime(monkeypatch)
    _patch_host(monkeypatch)
    origin = InvocationOrigin("turn", "session-1", "turn-1", None)
    warning = Warning(message="github: refused", code="mcp.connect_failed")
    FakeHost.run_events = [
        warning,
        InvocationToolCallStart(origin=origin, call_id="c1", tool_name="shell", args={"command": "x"}),
        Warning(message="github: refused", code="mcp.connect_failed"),
    ]

    assert run_cli.main(["hello", "--agent", "Headless", flag]) == 0

    err = capsys.readouterr().err
    # Quiet shows no activity, but a warning published mid-run still reaches stderr, once.
    assert err == ("Warning: github: refused\n" if flag == "--quiet" else "")


@pytest.mark.parametrize(("flag", "streamed"), [(None, False), (None, True), ("--quiet", True)])
def test_a_closed_stderr_still_gets_the_answer_to_stdout(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], flag: str | None, streamed: bool
) -> None:
    _patch_runtime(monkeypatch)
    origin = InvocationOrigin("turn", "session-1", "turn-1", None)
    # Without streamed events the success summary is the first write to fail.
    FakeHost.run_events = (
        [
            Warning(message="github: refused", code="mcp.connect_failed"),
            InvocationToolCallStart(origin=origin, call_id="c1", tool_name="shell", args={"command": "x"}),
        ]
        if streamed
        else []
    )
    stderr = FailingTextStream()
    monkeypatch.setattr(sys, "stderr", stderr)

    assert run_cli.main(["hello", "--agent", "Headless", *([flag] if flag else [])]) == 0

    assert capsys.readouterr().out == "final text\n"
    # The first failed write ends progress; nothing retries the closed stream.
    assert stderr.writes == 1


def test_an_interrupted_run_ends_its_progress_with_the_error(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    class InterruptedHost(FakeHost):
        async def run_until_final(
            self,
            prompt: str,
            *,
            timeout: float | None = None,
            on_event: Callable[[Event], None] | None = None,
        ) -> HeadlessRunResult:
            assert on_event is not None
            origin = InvocationOrigin("turn", "session-1", "turn-1", None)
            on_event(InvocationToolCallStart(origin=origin, call_id="c1", tool_name="shell", args={"command": "x"}))
            raise KeyboardInterrupt

    _patch_runtime(monkeypatch)
    monkeypatch.setattr(run_cli, "ChrysSessionHost", InterruptedHost)

    assert run_cli.main(["hello", "--agent", "Headless"]) == 130

    out = capsys.readouterr()
    assert out.out == ""
    # No success summary: the error is the last line.
    assert out.err == "→ shell\nError: Interrupted by user.\n"
    assert InterruptedHost.instances[-1].shutdown_called


def test_a_failed_run_shows_the_answer_text_it_streamed_before_the_error(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A call cut off mid-stream ends the text before it as completed; no tool card or final answer follows."""

    class CutOffHost(FakeHost):
        async def run_until_final(
            self,
            prompt: str,
            *,
            timeout: float | None = None,
            on_event: Callable[[Event], None] | None = None,
        ) -> HeadlessRunResult:
            assert on_event is not None
            origin = InvocationOrigin("turn", "session-1", "turn-1", None)
            on_event(InvocationContentDelta(origin=origin, segment_id="s1", text="I'll write the config now."))
            on_event(InvocationContentFinished(origin=origin, segment_id="s1"))
            raise HeadlessRunError(Error(code="boom", message="failed", session_id="session-1"))

    _patch_runtime(monkeypatch)
    monkeypatch.setattr(run_cli, "ChrysSessionHost", CutOffHost)

    assert run_cli.main(["hello", "--agent", "Headless"]) == 1

    out = capsys.readouterr()
    assert out.out == ""
    assert out.err == "I'll write the config now.\nError: failed\n"


def test_run_command_requires_agent() -> None:
    parser = run_cli.build_parser()

    with pytest.raises(SystemExit):
        parser.parse_args(["hello"])


@pytest.mark.parametrize(
    "argv",
    [
        ["--agent", "Headless"],
        ["hello", "--task", "task.md", "--agent", "Headless"],
    ],
)
def test_run_command_requires_exactly_one_prompt_source(argv: list[str], capsys) -> None:
    with pytest.raises(SystemExit) as excinfo:
        run_cli.main(argv)

    out = capsys.readouterr()
    assert excinfo.value.code == 2
    assert "provide either a prompt or --task FILE, not both" in out.err


def test_run_command_help_shows_workdir_option() -> None:
    parser = run_cli.build_parser()

    help_text = parser.format_help()

    assert "-C DIR" in help_text
    assert "--workdir DIR" in help_text
    assert "--cwd" not in help_text
    assert "--dir" not in help_text


def test_run_command_help_shows_task_option() -> None:
    parser = run_cli.build_parser()

    help_text = parser.format_help()

    assert "-t FILE" in help_text
    assert "--task FILE" in help_text
    assert "encoding auto-detected" in help_text
    assert "resolved relative to --workdir" in help_text


def test_run_command_help_uses_capitalized_help_text() -> None:
    help_text = run_cli.build_parser().format_help()

    assert "Show this help message and exit" in help_text
    assert "show this help message and exit" not in help_text


def test_run_command_help_shows_agent_option() -> None:
    parser = run_cli.build_parser()

    help_text = parser.format_help()

    assert "-a AGENT" in help_text
    assert "--agent AGENT" in help_text
    assert "Agent profile id, name, or display name" in help_text
    assert "-p PROFILE" not in help_text
    assert "--profile PROFILE" not in help_text


def test_run_command_help_shows_model_option() -> None:
    parser = run_cli.build_parser()

    help_text = parser.format_help()

    assert "-m MODEL" in help_text
    assert "--model MODEL" in help_text
    assert "Active model profile id or name" in help_text
    assert "--profile PROFILE" not in help_text


@pytest.mark.parametrize("old_option", ["-p", "--profile"])
def test_run_command_rejects_old_agent_aliases(old_option: str) -> None:
    parser = run_cli.build_parser()

    with pytest.raises(SystemExit):
        parser.parse_args(["hello", old_option, "Headless"])


@pytest.mark.parametrize("old_option", ["--cwd", "--dir"])
def test_run_command_rejects_old_workdir_aliases(old_option: str) -> None:
    parser = run_cli.build_parser()

    with pytest.raises(SystemExit):
        parser.parse_args(["hello", "--agent", "Headless", old_option, "."])


def test_run_command_rejects_timeout_option() -> None:
    parser = run_cli.build_parser()

    with pytest.raises(SystemExit):
        parser.parse_args(["hello", "--agent", "Headless", "--timeout", "1"])


def test_run_command_rejects_stream_option() -> None:
    parser = run_cli.build_parser()

    with pytest.raises(SystemExit):
        parser.parse_args(["hello", "--agent", "Headless", "--stream"])


def test_parser_sanitizes_controls_in_unknown_option_without_changing_argparse_transport(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    hostile_option = "--unknown\x1b\noption"
    monkeypatch.setenv("CHRYS_LOCALE", "zh-Hans")

    with pytest.raises(SystemExit) as excinfo:
        run_cli.main(["hello", "--agent", "Headless", "--json", hostile_option])

    output = capsys.readouterr()
    assert excinfo.value.code == 2
    assert output.out == ""
    assert output.err.startswith(f"usage: {APP_COMMAND} run")
    assert "error: unrecognized arguments: --unknown��option" in output.err
    assert "\x1b" not in output.err
    assert "--unknown�\noption" not in output.err


def test_prompt_task_conflict_with_json_remains_an_argparse_error(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("CHRYS_LOCALE", "zh-Hans")

    with pytest.raises(SystemExit) as excinfo:
        run_cli.main(["hello", "--task", "task.md", "--agent", "Headless", "--json"])

    output = capsys.readouterr()
    assert excinfo.value.code == 2
    assert output.out == ""
    assert output.err.startswith(f"usage: {APP_COMMAND} run")
    assert "provide either a prompt or --task FILE, not both" in output.err
    with pytest.raises(json.JSONDecodeError):
        json.loads(output.err)


def test_phase_two_workdir_failure_is_english_on_both_transports_across_locales(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _patch_runtime(monkeypatch)
    monkeypatch.chdir(tmp_path)

    human = _run_in_locales(monkeypatch, capsys, ["hello", "--agent", "Headless", "--workdir", "missing"])
    machine = _run_in_locales(
        monkeypatch,
        capsys,
        ["hello", "--agent", "Headless", "--workdir", "missing", "--json"],
    )

    assert human["en"] == human["zh-Hans"] == (1, "", "Error: Working directory does not exist: missing\n")
    assert machine["en"] == machine["zh-Hans"]
    assert json.loads(machine["en"][2]) == {
        "error": "Working directory does not exist: missing",
        "code": "error",
    }


def test_phase_two_task_failure_sanitizes_human_detail_before_localization_exists(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _patch_runtime(monkeypatch)
    monkeypatch.chdir(tmp_path)
    task_name = "missing\nname.md"

    # Windows rejects control characters in filenames before the existence
    # check (WinError 123), which would divert this argument into the
    # read-failed branch. Force the missing-file outcome for this one name so
    # both platforms exercise the not-found detail that sanitization guards.
    class _ForcedMissingPath(type(Path())):
        def resolve(self, strict: bool = False) -> Path:
            if task_name in str(self):
                raise FileNotFoundError(str(self))
            return super().resolve(strict)

    monkeypatch.setattr(run_cli, "Path", _ForcedMissingPath)

    human = _run_in_locales(monkeypatch, capsys, ["--task", task_name, "--agent", "Headless"])
    machine = _run_in_locales(monkeypatch, capsys, ["--task", task_name, "--agent", "Headless", "--json"])

    expected_human = "Error: Task file does not exist: missing�name.md\n"
    assert human["en"] == human["zh-Hans"] == (1, "", expected_human)
    assert machine["en"] == machine["zh-Hans"]
    assert json.loads(machine["en"][2]) == {
        "error": "Task file does not exist: missing\nname.md",
        "code": "task_file_not_found",
    }


def test_phase_two_preparation_failure_is_english_on_both_transports_across_locales(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def _prepare_runtime(*, restoring_session: bool) -> headless.PreparedRuntime:
        raise RuntimeError("prepare failed")

    _patch_host(monkeypatch)
    monkeypatch.setattr(headless, "prepare_runtime", _prepare_runtime)

    human = _run_in_locales(monkeypatch, capsys, ["hello", "--agent", "Headless"])
    machine = _run_in_locales(monkeypatch, capsys, ["hello", "--agent", "Headless", "--json"])

    assert human["en"] == human["zh-Hans"] == (1, "", "Error: prepare failed\n")
    assert machine["en"] == machine["zh-Hans"]
    assert json.loads(machine["en"][2]) == {"error": "prepare failed", "code": "error"}


def test_bootstrap_and_retry_warnings_stay_english_across_locales(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    display_message = startup_module._INVALID_NO_PROXY_ENV.bind(var="NO_PROXY", value=repr("bad"))

    _patch_host(monkeypatch)
    monkeypatch.setenv("CHRYS_MAX_TRANSIENT_RETRIES", "invalid")
    monkeypatch.setattr(
        headless,
        "bootstrap_runtime",
        _fake_bootstrap(
            warnings=[
                Warning(
                    code="invalid_no_proxy",
                    message=format_message(display_message),
                    display_message=display_message,
                )
            ],
        ),
    )

    human = _run_in_locales(monkeypatch, capsys, ["hello", "--agent", "Headless"])
    machine = _run_in_locales(monkeypatch, capsys, ["hello", "--agent", "Headless", "--json"])

    assert human["en"] == human["zh-Hans"]
    assert human["en"][0] == 0
    assert "Warning: NO_PROXY is invalid for httpx" in human["en"][2]
    assert "Ignoring invalid CHRYS_MAX_TRANSIENT_RETRIES='invalid'" in human["en"][2]
    assert machine["en"][2] == machine["zh-Hans"][2]
    assert [json.loads(line) for line in machine["en"][2].splitlines()] == [
        {
            "warning": "NO_PROXY is invalid for httpx and was ignored ('bad'). Remove or fix this environment value.",
            "code": "invalid_no_proxy",
        },
        {
            "warning": (
                "Ignoring invalid CHRYS_MAX_TRANSIENT_RETRIES='invalid'; "
                "expected a non-negative integer and will use the frontend default."
            ),
            "code": "invalid_max_transient_retries",
        },
    ]


def test_semantic_headless_error_stays_english_across_locales(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def _failure() -> HeadlessRunError:
        display_message = run_cli._MODEL_PROFILE_NOT_FOUND.bind(model="Missing")
        event = Error(
            code="boom",
            message="Model profile not found: Missing",
            session_id="session-1",
            display_message=display_message,
        )
        return HeadlessRunError(event)

    _patch_runtime(monkeypatch)
    _patch_failure_host(monkeypatch, _failure)

    human = _run_in_locales(monkeypatch, capsys, ["hello", "--agent", "Headless"])
    machine = _run_in_locales(monkeypatch, capsys, ["hello", "--agent", "Headless", "--json"])

    assert human["en"] == human["zh-Hans"] == (1, "", "Error: Model profile not found: Missing\n")
    assert machine["en"][2] == machine["zh-Hans"][2]
    assert json.loads(machine["en"][2]) == {
        "error": "Model profile not found: Missing",
        "code": "boom",
        "session_id": "session-1",
    }


@pytest.mark.parametrize(
    ("code", "detail_line"),
    [
        pytest.param("executor_error", "  detail: Connection error.\n", id="turn-failure"),
        pytest.param("boom", "", id="other"),
    ],
)
def test_classified_headless_error_says_what_went_wrong_with_the_raw_detail(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    code: str,
    detail_line: str,
) -> None:
    from chrys.foundation.branding import APP_DISPLAY_NAME
    from chrys.foundation.errors.display import _DNS_FAILED, _MAYBE_OFFLINE

    def _failure() -> HeadlessRunError:
        event = Error(
            code=code,
            message="Connection error.",
            session_id="session-1",
            display_message=_DNS_FAILED.bind(host="api.example.com"),
            display_hint=_MAYBE_OFFLINE.bind(app=APP_DISPLAY_NAME),
        )
        return HeadlessRunError(event)

    _patch_runtime(monkeypatch)
    _patch_failure_host(monkeypatch, _failure)

    human = _run_in_locales(monkeypatch, capsys, ["hello", "--agent", "Headless"])
    machine = _run_in_locales(monkeypatch, capsys, ["hello", "--agent", "Headless", "--json"])

    english = (
        "Can't resolve api.example.com. Check your network connection and DNS; "
        "if the address is wrong, fix the base URL in the model profile. "
        "The device running iCode doesn't seem to have a network connection. Check it first."
    )
    assert human["en"] == human["zh-Hans"] == (1, "", f"Error: {english}\n{detail_line}")
    # Machine output keeps the raw text alone.
    assert machine["en"][2] == machine["zh-Hans"][2]
    assert json.loads(machine["en"][2]) == {"error": "Connection error.", "code": code, "session_id": "session-1"}


def test_legacy_headless_error_stays_english_and_sanitizes_detail_across_locales(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    detail = "failed\x1b\nafter provider response"

    def _failure() -> HeadlessRunError:
        event = Error(code="boom", message=detail, session_id="session-1")
        return HeadlessRunError(event)

    _patch_runtime(monkeypatch)
    _patch_failure_host(monkeypatch, _failure)

    human = _run_in_locales(monkeypatch, capsys, ["hello", "--agent", "Headless"])
    machine = _run_in_locales(monkeypatch, capsys, ["hello", "--agent", "Headless", "--json"])

    assert (
        human["en"]
        == human["zh-Hans"]
        == (
            1,
            "",
            "Error: failed��after provider response\n",
        )
    )
    assert machine["en"][2] == machine["zh-Hans"][2]
    assert json.loads(machine["en"][2]) == {
        "error": detail,
        "code": "boom",
        "session_id": "session-1",
    }


@pytest.mark.parametrize(
    ("failure_factory", "code", "exit_code", "english"),
    [
        pytest.param(TimeoutError, "timeout", 124, "Agent run timed out.", id="timeout"),
        pytest.param(
            KeyboardInterrupt,
            "interrupted",
            130,
            "Interrupted by user.",
            id="interrupt",
        ),
    ],
)
def test_fixed_failures_stay_english_across_locales(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    failure_factory,
    code: str,
    exit_code: int,
    english: str,
) -> None:
    _patch_runtime(monkeypatch)
    _patch_failure_host(monkeypatch, failure_factory)

    human = _run_in_locales(monkeypatch, capsys, ["hello", "--agent", "Headless"])
    machine = _run_in_locales(monkeypatch, capsys, ["hello", "--agent", "Headless", "--json"])

    assert human["en"] == human["zh-Hans"] == (exit_code, "", f"Error: {english}\n")
    assert machine["en"][0] == machine["zh-Hans"][0] == exit_code
    assert machine["en"][2] == machine["zh-Hans"][2]
    assert json.loads(machine["en"][2]) == {"error": english, "code": code}


@pytest.mark.parametrize(
    ("failure_factory", "code", "english"),
    [
        pytest.param(
            lambda: AgentProfileNotFoundError(
                "Agent profile not found: Missing",
                display_message=session_host_module._AGENT_PROFILE_NOT_FOUND.bind(name="Missing"),
            ),
            "profile_not_found",
            "Agent profile not found: Missing",
            id="agent-without-available",
        ),
        pytest.param(
            lambda: AgentProfileNotFoundError(
                "Agent profile not found: Missing. Available profiles: Code, QA",
                display_message=session_host_module._AGENT_PROFILE_NOT_FOUND_WITH_AVAILABLE.bind(
                    name="Missing",
                    available=DisplaySequence(("Code", "QA")),
                ),
            ),
            "profile_not_found",
            "Agent profile not found: Missing. Available profiles: Code, QA",
            id="agent-with-available",
        ),
        pytest.param(
            lambda: SessionNotFoundError(
                "Session not found: deadbeef",
                display_message=session_host_module._SESSION_NOT_FOUND.bind(session_id="deadbeef"),
            ),
            "session_not_found",
            "Session not found: deadbeef",
            id="session-without-recent",
        ),
        pytest.param(
            lambda: SessionNotFoundError(
                "Session not found: deadbeef. Recent sessions: aabbcc, ddeeff",
                display_message=session_host_module._SESSION_NOT_FOUND_WITH_RECENT.bind(
                    session_id="deadbeef",
                    recent=DisplaySequence(("aabbcc", "ddeeff")),
                ),
            ),
            "session_not_found",
            "Session not found: deadbeef. Recent sessions: aabbcc, ddeeff",
            id="session-with-recent",
        ),
        pytest.param(
            lambda: AmbiguousSessionIdError(
                "Session id 'abc' is ambiguous.",
                display_message=session_host_module._SESSION_ID_AMBIGUOUS.bind(session_id="abc"),
            ),
            "session_ambiguous",
            "Session id 'abc' is ambiguous.",
            id="ambiguous-session",
        ),
        pytest.param(
            lambda: run_cli.ModelProfileNotFoundError(
                "Model profile not found: Missing",
                display_message=run_cli._MODEL_PROFILE_NOT_FOUND.bind(model="Missing"),
            ),
            "model_profile_not_found",
            "Model profile not found: Missing",
            id="model-without-available",
        ),
        pytest.param(
            lambda: run_cli.ModelProfileNotFoundError(
                "Model profile not found: Missing. Available model profiles: Friendly Model (model-id)",
                display_message=run_cli._MODEL_PROFILE_NOT_FOUND_WITH_AVAILABLE.bind(
                    model="Missing",
                    available=DisplaySequence(("Friendly Model (model-id)",)),
                ),
            ),
            "model_profile_not_found",
            "Model profile not found: Missing. Available model profiles: Friendly Model (model-id)",
            id="model-with-available",
        ),
    ],
)
def test_profile_and_session_failures_stay_english_across_locales(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    failure_factory,
    code: str,
    english: str,
) -> None:
    _patch_runtime(monkeypatch)
    _patch_failure_host(monkeypatch, failure_factory)

    human = _run_in_locales(monkeypatch, capsys, ["hello", "--agent", "Headless"])
    machine = _run_in_locales(monkeypatch, capsys, ["hello", "--agent", "Headless", "--json"])

    assert human["en"] == human["zh-Hans"] == (1, "", f"Error: {english}\n")
    assert machine["en"][2] == machine["zh-Hans"][2]
    assert json.loads(machine["en"][2]) == {"error": english, "code": code}


def test_generic_failure_stays_english_and_sanitizes_detail_across_locales(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    detail = "provider failed\x1b\nafter response"
    _patch_runtime(monkeypatch)
    _patch_failure_host(monkeypatch, lambda: RuntimeError(detail))

    human = _run_in_locales(monkeypatch, capsys, ["hello", "--agent", "Headless"])
    machine = _run_in_locales(monkeypatch, capsys, ["hello", "--agent", "Headless", "--json"])

    assert (
        human["en"]
        == human["zh-Hans"]
        == (
            1,
            "",
            "Error: provider failed��after response\n",
        )
    )
    assert machine["en"][2] == machine["zh-Hans"][2]
    assert json.loads(machine["en"][2]) == {"error": detail, "code": "error"}


def test_each_main_invocation_uses_a_fresh_prepared_runtime_holder(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    display_message = startup_module._INVALID_NO_PROXY_ENV.bind(var="NO_PROXY", value=repr("bad"))
    warning = Warning(
        code="invalid_no_proxy",
        message=format_message(display_message),
        display_message=display_message,
    )
    calls = 0

    def _prepare_runtime(*, restoring_session: bool) -> headless.PreparedRuntime:
        nonlocal calls
        calls += 1
        if calls == 1:
            return _prepared_runtime(Settings(locale="zh-Hans"), warnings=[warning])
        raise RuntimeError("runtime preparation failed")

    _patch_host(monkeypatch)
    monkeypatch.setattr(headless, "prepare_runtime", _prepare_runtime)

    assert run_cli.main(["hello", "--agent", "Headless"]) == 0
    first = capsys.readouterr()
    assert "Warning: NO_PROXY is invalid for httpx" in first.err

    assert run_cli.main(["hello", "--agent", "Headless"]) == 1
    second = capsys.readouterr()
    assert second.out == ""
    assert second.err == "Error: runtime preparation failed\n"
    assert "Warning:" not in second.err


def test_missing_model_selection_without_available_profiles_binds_short_display_variant(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(FakeModelRegistry, "profiles", [])
    monkeypatch.setattr(run_cli, "ModelProfileRegistry", FakeModelRegistry)

    with pytest.raises(run_cli.ModelProfileNotFoundError) as excinfo:
        run_cli._apply_active_model_selection(Settings(), "Missing")

    assert headless.exception_message(excinfo.value) == "Model profile not found: Missing"
    assert excinfo.value.display_message is not None
    assert excinfo.value.display_message.definition is run_cli._MODEL_PROFILE_NOT_FOUND


class _Stdout(io.StringIO):
    def __init__(self, *, tty: bool) -> None:
        super().__init__()
        self._tty = tty

    def isatty(self) -> bool:
        return self._tty


@pytest.mark.parametrize("tty", [True, False])
def test_write_result_neutralizes_terminal_controls_but_keeps_layout(
    monkeypatch: pytest.MonkeyPatch, tty: bool
) -> None:
    stdout = _Stdout(tty=tty)
    monkeypatch.setattr(run_cli.sys, "stdout", stdout)
    text = "all:\n\tgo build\r\n\x1b]52;c;cGF5bG9hZA==\x07done\rspoof"

    run_cli._write_result(HeadlessRunResult(text=text, session_id="s"), as_json=False, duration=0.0)

    if tty:
        assert stdout.getvalue() == "all:\n\tgo build\n�]52;c;cGF5bG9hZA==�done\nspoof\n"
    else:
        # Redirected output is data for another program: byte-for-byte.
        assert stdout.getvalue() == text + "\n"
