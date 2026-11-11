# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Headless RSI case runs: where their sessions go, how a failed run is reported and who caused it."""

from __future__ import annotations

from pathlib import Path
from typing import Any, ClassVar

import pytest

from chrys.app.cli import rsi_headless
from chrys.foundation.errors import ErrorKind
from chrys.foundation.events.types import Error
from chrys.foundation.models.failures import FailureFacts
from chrys.orchestration.session_host import HeadlessRunError, HeadlessRunResult
from chrys.service.rsi.case_runner import case_sessions_dir
from chrys.service.rsi.dataset import RsiCase, ScriptExpect


class _FakeEngine:
    def pin_model_profile(self) -> None:
        return None


class _FakeHost:
    created: ClassVar[list[_FakeHost]] = []
    outcome: ClassVar[HeadlessRunResult | BaseException] = HeadlessRunResult(text="done", session_id="s-1")

    def __init__(
        self,
        *,
        profile_name: str,
        settings: Any,
        loaded_settings: Any,
        model_registry: Any,
        agent_registry: Any,
        state_store: Any,
        approval_mode: Any,
        cwd: str,
        allow_user_interaction: bool,
        surface: Any,
    ) -> None:
        del profile_name, settings, loaded_settings, model_registry, agent_registry, state_store
        del approval_mode, allow_user_interaction, surface
        self.cwd = cwd
        self.engine = _FakeEngine()
        self.shut_down = False
        _FakeHost.created.append(self)

    async def run_until_final(self, message: str, *, timeout: float | None = None) -> HeadlessRunResult:
        del message, timeout
        if isinstance(self.outcome, BaseException):
            raise self.outcome
        return self.outcome

    async def shutdown(self) -> None:
        self.shut_down = True


def _case() -> RsiCase:
    return RsiCase(case_id="c1", prompt="Do it", expect=ScriptExpect(response_contains=("ok",)), script=None)


@pytest.fixture
def fake_host(monkeypatch: pytest.MonkeyPatch) -> type[_FakeHost]:
    monkeypatch.setattr(_FakeHost, "created", [])
    monkeypatch.setattr(rsi_headless, "ChrysSessionHost", _FakeHost)
    return _FakeHost


@pytest.mark.asyncio
async def test_case_sessions_are_kept_beside_the_agent_folder(tmp_path: Path, fake_host: type[_FakeHost]) -> None:
    workdir = tmp_path / "case_workspaces" / "c1"
    result = await rsi_headless.HeadlessCaseRunner(agent="Code").run_case(_case(), workdir)
    assert result.ok
    assert result.response_text == "done"
    (host,) = fake_host.created
    assert host.cwd == str(workdir.resolve())
    assert host.shut_down is True
    sessions = case_sessions_dir(workdir)
    assert sessions.is_dir()
    assert sessions.parent == workdir.parent
    assert list(workdir.iterdir()) == []


def _turn_failed(message: str, kind: ErrorKind) -> HeadlessRunError:
    facts = FailureFacts(kind=kind, retryable=False)
    return HeadlessRunError(Error(code="executor_error", message=message, facts=facts))


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("timeout", "raised", "error", "cause"),
    [
        (0.0, TimeoutError(), "case run timed out after 0s", "timeout"),
        (300.0, TimeoutError("lock wait timed out"), "lock wait timed out", "environment"),
        (300.0, RuntimeError("profile not found"), "profile not found", "environment"),
        (300.0, _turn_failed("upstream 503", ErrorKind.SERVER_ERROR), "upstream 503", "environment"),
        (300.0, _turn_failed("rate limited", ErrorKind.RATE_LIMITED), "rate limited", "environment"),
        (300.0, _turn_failed("bad", ErrorKind.UNKNOWN), "bad", "environment"),
        (300.0, _turn_failed("too long", ErrorKind.CONTEXT_OVERFLOW), "too long", "agent"),
        (300.0, _turn_failed("413", ErrorKind.PAYLOAD_TOO_LARGE), "413", "agent"),
        (300.0, _turn_failed("blocked", ErrorKind.CONTENT_FILTERED), "blocked", "agent"),
        (
            300.0,
            HeadlessRunError(Error(code="headless_interaction_required", message="asked: Which?")),
            "asked: Which?",
            "agent",
        ),
        (
            300.0,
            HeadlessRunError(Error(code="hook_blocked", message="blocked by hook", refused_input="prompt")),
            "blocked by hook",
            "environment",
        ),
    ],
    ids=[
        "case-timeout",
        "other-timeout",
        "host-error",
        "server-error",
        "rate-limited",
        "unknown-kind",
        "context-overflow",
        "payload-too-large",
        "content-filtered",
        "headless-question",
        "hook-blocked",
    ],
)
async def test_failed_case_run_reports_what_failed_and_who_caused_it(
    tmp_path: Path,
    fake_host: type[_FakeHost],
    monkeypatch: pytest.MonkeyPatch,
    timeout: float,
    raised: BaseException,
    error: str,
    cause: str,
) -> None:
    monkeypatch.setattr(fake_host, "outcome", raised)
    runner = rsi_headless.HeadlessCaseRunner(agent="Code", timeout=timeout)
    result = await runner.run_case(_case(), tmp_path / "case_workspaces" / "c1")
    assert (result.error, result.error_cause) == (error, cause)
    assert fake_host.created[0].shut_down is True
