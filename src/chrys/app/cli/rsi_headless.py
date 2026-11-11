# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Headless RSI case runner (app tier — may use ChrysSessionHost)."""

from __future__ import annotations

import time
from collections.abc import Callable
from pathlib import Path

from chrys.foundation.config.settings import Settings
from chrys.foundation.config.settings_store import LoadedSettings
from chrys.foundation.models.session_surface import SessionSurface
from chrys.orchestration.session_host import ChrysSessionHost
from chrys.service.approval.policy import ApprovalMode
from chrys.service.profiles.agents.registry import AgentProfileRegistry
from chrys.service.profiles.agents.schema import AgentProfile
from chrys.service.profiles.models.registry import ModelProfileRegistry
from chrys.service.rsi.case_runner import CaseRunner, CaseRunResult, StubCaseRunner, case_sessions_dir
from chrys.service.rsi.dataset import RsiCase
from chrys.service.rsi.errors import RsiBadRequest, RsiNotReady
from chrys.service.state.store import JsonFileStateStore


class HeadlessCaseRunner:
    """Run each case through ``ChrysSessionHost`` with ``ApprovalMode.BYPASS``."""

    def __init__(
        self,
        *,
        agent: str,
        settings: Settings | None = None,
        loaded_settings: LoadedSettings | None = None,
        model_registry: ModelProfileRegistry | None = None,
        agent_registry: AgentProfileRegistry | None = None,
        timeout: float | None = 300.0,
    ) -> None:
        cleaned = str(agent or "").strip()
        if not cleaned:
            raise RsiBadRequest("agent profile is required for headless evaluation")
        self.agent = cleaned
        self.settings = settings
        self.loaded_settings = loaded_settings
        self.model_registry = model_registry
        self.agent_registry = agent_registry
        self.timeout = timeout

    async def run_case(self, case: RsiCase, workdir: Path) -> CaseRunResult:
        workdir.mkdir(parents=True, exist_ok=True)
        sessions_root = case_sessions_dir(workdir)
        sessions_root.mkdir(parents=True, exist_ok=True)

        host = ChrysSessionHost(
            profile_name=self.agent,
            settings=self.settings,
            loaded_settings=self.loaded_settings,
            model_registry=self.model_registry,
            agent_registry=self.agent_registry,
            state_store=JsonFileStateStore(sessions_root),
            approval_mode=ApprovalMode.BYPASS,
            cwd=str(workdir.resolve()),
            allow_user_interaction=False,
            surface=SessionSurface.CLI,
        )
        if self.model_registry is not None:
            host.engine.pin_model_profile()
        started = time.monotonic()
        try:
            result = await host.run_until_final(case.prompt, timeout=self.timeout)
            return CaseRunResult(
                case_id=case.case_id,
                response_text=result.text,
                workdir=workdir,
                session_id=result.session_id or None,
            )
        except TimeoutError as exc:
            timeout = self.timeout
            if timeout is not None and time.monotonic() - started >= timeout:
                error = f"case run timed out after {timeout:g}s"
            else:
                error = str(exc) or type(exc).__name__
            return CaseRunResult(case_id=case.case_id, response_text="", workdir=workdir, error=error)
        except Exception as exc:
            return CaseRunResult(
                case_id=case.case_id,
                response_text="",
                workdir=workdir,
                error=str(exc) or type(exc).__name__,
            )
        finally:
            await host.shutdown()


def build_case_runner(
    *,
    stub: bool,
    agent: str | None = None,
    settings: Settings | None = None,
    loaded_settings: LoadedSettings | None = None,
    model_registry: ModelProfileRegistry | None = None,
    agent_registry: AgentProfileRegistry | None = None,
    timeout: float | None = 300.0,
) -> CaseRunner:
    """Factory used by the RSI CLI (stub stays in the service tier)."""
    if stub:
        return StubCaseRunner()
    if not agent:
        raise RsiNotReady("headless evaluation requires --agent (or pass --stub)")
    return HeadlessCaseRunner(
        agent=agent,
        settings=settings,
        loaded_settings=loaded_settings,
        model_registry=model_registry,
        agent_registry=agent_registry,
        timeout=timeout,
    )


def build_candidate_runner_factory(
    *,
    loaded_settings: LoadedSettings | None = None,
    model_registry: ModelProfileRegistry | None = None,
    timeout: float | None = 300.0,
) -> Callable[[AgentProfile], HeadlessCaseRunner]:
    """Return a runner factory that evaluates each candidate profile headlessly.

    Builtins and user profiles are loaded first so declared sub-agents
    (e.g. Code → Explore/General) resolve like a normal iCode session. The
    candidate is then registered on that private registry so the mutated
    profile shadows any same-name agent already on disk.
    """

    def factory(candidate: AgentProfile) -> HeadlessCaseRunner:
        registry = AgentProfileRegistry()
        registry.load_all()
        registry.register(candidate)
        return HeadlessCaseRunner(
            agent=candidate.name,
            loaded_settings=loaded_settings,
            model_registry=model_registry,
            agent_registry=registry,
            timeout=timeout,
        )

    return factory
