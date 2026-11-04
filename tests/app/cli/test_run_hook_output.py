# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""``icode run`` on a real host: hook runs and hook failures leave the headless output unchanged.

The hooks file sits in the per-test config dir as a user's would, and the
hooks are real subprocesses.
"""

from __future__ import annotations

import json
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Annotated, Any
from unittest.mock import create_autospec

import pytest

import chrys.orchestration.engine.build.builder as builder_module
from chrys.app.cli import headless
from chrys.app.cli import run as run_cli
from chrys.foundation.config.settings import Settings
from chrys.foundation.config.settings_store import LoadedSettings
from chrys.foundation.events.types import HookExecutionsChanged
from chrys.foundation.i18n import Localizer
from chrys.foundation.platform import get_platform
from chrys.kernel import FunctionTool
from chrys.orchestration.session_host import ChrysSessionHost
from chrys.service.approval.policy import ApprovalMode
from chrys.service.llm.mock import MockChatClient, MockResponse
from tests.orchestration.workflows._hosting import make_host
from tests.support.tool_packs import install_tools


def _real_host(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, client: MockChatClient, tool: FunctionTool, snapshots: list[Any]
) -> None:
    """As ``tests/app/cli/test_run_streaming.py``: a real host on a mock model with *tool* as its only builtin."""

    async def create_client(
        _profile: Any,
        on_intermediate_text: Callable[[str], None] | None = None,
        **_routing: object,
    ) -> MockChatClient:
        client._on_intermediate_text = on_intermediate_text
        return client

    def build(*, profile_name: str, loaded_settings: LoadedSettings, approval_mode: ApprovalMode, **_kwargs: object):
        assert approval_mode is ApprovalMode.BYPASS
        host = make_host(tmp_path, project=tmp_path, profile_name=profile_name, loaded_settings=loaded_settings)

        async def keep(event: HookExecutionsChanged) -> None:
            snapshots.append(event)

        host_bus = host.event_bus
        original_start = host.start

        async def start(*args: Any, **kwargs: Any) -> Any:
            await host_bus.subscribe(HookExecutionsChanged, keep)
            return await original_start(*args, **kwargs)

        host.start = start  # type: ignore[method-assign]
        return host

    def prepare_runtime(*, restoring_session: bool = False) -> headless.PreparedRuntime:
        loaded = LoadedSettings(settings=Settings(model_profile="mock-profile"), provenance={})
        return headless.PreparedRuntime(loaded=loaded, localizer=Localizer("en"), pending_warnings=[])

    monkeypatch.setattr(
        builder_module, "create_client", create_autospec(builder_module.create_client, side_effect=create_client)
    )
    install_tools(monkeypatch, [tool])
    monkeypatch.setattr(run_cli, "ChrysSessionHost", create_autospec(ChrysSessionHost, side_effect=build))
    monkeypatch.setattr(
        headless, "prepare_runtime", create_autospec(headless.prepare_runtime, side_effect=prepare_runtime)
    )


def _write_hooks() -> None:
    hooks_dir = get_platform().config_dir / "hooks"
    hooks_dir.mkdir(parents=True, exist_ok=True)

    def entry(hook_id: str, script: str) -> dict[str, Any]:
        return {
            "id": hook_id,
            "event": "before_tool_call",
            "match": {"tool_name": "echo_tool"},
            "run": {"type": "command", "argv": [sys.executable, "-c", script]},
            "execution": {"mode": "blocking", "timeout_seconds": 30, "on_error": "warn"},
        }

    document = {"version": 1, "hooks": [entry("ok-guard", "pass"), entry("bad-guard", "import sys; sys.exit(3)")]}
    (hooks_dir / "hooks.json").write_text(json.dumps(document), encoding="utf-8")


@pytest.mark.parametrize("mode", ["text", "json"])
async def test_hook_runs_and_failures_print_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], mode: str
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    for name in ("HOME", "USERPROFILE", "APPDATA"):
        monkeypatch.setenv(name, str(home))
    _write_hooks()

    async def echo(message: Annotated[str, "Text to echo"]) -> str:
        return f"echo: {message}"

    client = MockChatClient(
        responses=[
            MockResponse(tool_calls=[("echo_tool", "c1", {"message": "one"})]),
            MockResponse(tool_calls=[("echo_tool", "c2", {"message": "two"})]),
            MockResponse(tool_calls=[("echo_tool", "c3", {"message": "three"})]),
            MockResponse(text="final answer"),
        ]
    )
    snapshots: list[HookExecutionsChanged] = []
    _real_host(monkeypatch, tmp_path, client, FunctionTool(func=echo, name="echo_tool", description="Echo"), snapshots)
    flags = ["--json"] if mode == "json" else []
    args = run_cli.build_parser().parse_args(["hello", "--agent", "Headless", *flags])

    exit_code = await run_cli.run_command(args, run_cli.PreparedRuntimeHolder())
    captured = capsys.readouterr()

    assert exit_code == 0
    # The hooks really ran on every call, and their runs were published on the bus.
    assert client.call_count == 4
    assert {view.hook_id for event in snapshots for view in event.executions} == {"ok-guard", "bad-guard"}
    if mode == "json":
        assert captured.err == ""
        assert set(json.loads(captured.out)) == {"session_id", "result", "duration"}
    else:
        assert captured.out == "final answer\n"
    # No failure is told: the manager only logs it.
    assert "Warning" not in captured.err, captured.err
    everything = captured.out + captured.err
    # HookExecutionsChanged has no headless output: neither hook's runs are shown.
    assert "ok-guard" not in everything
    assert "bad-guard" not in everything
    for fragment in ("Running hook", "Hook queued", "hook running", "Waiting for hook"):
        assert fragment not in everything


def test_snapshots_are_not_a_headless_or_acp_run_event() -> None:
    """Headless and ACP streams forward only these types; the TUI-only snapshot is not one."""
    from chrys.orchestration import session_host

    assert HookExecutionsChanged not in session_host._HEADLESS_RUN_EVENT_TYPES
