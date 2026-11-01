# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Which ACP launch fields can hold a ``{{!command}}``; the launch itself runs it (``with_fresh_tokens``)."""

from __future__ import annotations

from pathlib import Path

import pytest

import chrys.service.command_credentials as credentials_module
from chrys.foundation.models.session_env import SessionEnvironment
from chrys.foundation.platform import get_platform
from chrys.foundation.util.env_templates import COMMAND_HANDLE_RE
from chrys.orchestration.invoker.acp_spec import resolve_acp_spec
from chrys.service.acp_client import AcpConfigError
from chrys.service.profiles.agents.schema import AcpAgentConfig


def _resolve(tmp_path: Path, config: AcpAgentConfig):
    runtime = SessionEnvironment(cwd=str(tmp_path), platform=get_platform())
    return resolve_acp_spec(config, runtime, tmp_path / "stderr.log", workspace_roots=[str(tmp_path)])


@pytest.mark.parametrize("field", ["command", "cwd"])
def test_what_runs_and_where_it_runs_cannot_come_from_a_command(tmp_path: Path, field: str) -> None:
    config = AcpAgentConfig(**{"command": "agent", field: "{{!tool --password hunter2}}"})

    with pytest.raises(AcpConfigError) as caught:
        _resolve(tmp_path, config)

    assert "not supported" in str(caught.value)
    assert "hunter2" not in str(caught.value)


def test_arguments_and_environment_stay_handles_and_run_nothing_until_the_launch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(credentials_module, "_credentials", {})
    config = AcpAgentConfig(command="agent", args=["--key={{!tool get}}"], env={"TOKEN": "Bearer {{!tool get}}"})

    spec = _resolve(tmp_path, config)

    assert COMMAND_HANDLE_RE.fullmatch(spec.args[0].removeprefix("--key="))
    assert COMMAND_HANDLE_RE.fullmatch(spec.env["TOKEN"].removeprefix("Bearer "))
    assert credentials_module._credentials == {}
