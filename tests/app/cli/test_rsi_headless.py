# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Headless RSI case runs: where their sessions go and how a failed run is reported."""

from __future__ import annotations

from pathlib import Path
from typing import Any, ClassVar

import pytest

from chrys.app.cli import rsi_headless
from chrys.orchestration.session_host import HeadlessRunResult
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


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("timeout", "raised", "error"),
    [
        (0.0, TimeoutError(), "case run timed out after 0s"),
        (300.0, TimeoutError("lock wait timed out"), "lock wait timed out"),
        (300.0, RuntimeError("provider refused"), "provider refused"),
    ],
)
async def test_failed_case_run_reports_what_failed(
    tmp_path: Path,
    fake_host: type[_FakeHost],
    monkeypatch: pytest.MonkeyPatch,
    timeout: float,
    raised: BaseException,
    error: str,
) -> None:
    monkeypatch.setattr(fake_host, "outcome", raised)
    runner = rsi_headless.HeadlessCaseRunner(agent="Code", timeout=timeout)
    result = await runner.run_case(_case(), tmp_path / "case_workspaces" / "c1")
    assert result.error == error
    assert fake_host.created[0].shut_down is True
