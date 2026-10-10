# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tests for ``chrys acp``."""

from __future__ import annotations

import io
import logging
from typing import Any

import pytest

from chrys.app.acp.transport import AbortableMessageSender
from chrys.app.cli import acp as acp_cli
from chrys.foundation.config.env_layers import freeze_process_env
from chrys.foundation.config.settings import Settings
from chrys.foundation.config.settings_store import LoadedSettings, load_settings, persist
from chrys.orchestration.startup import RuntimeBootstrap


def test_acp_command_defaults_to_code_agent() -> None:
    parser = acp_cli.build_parser()

    args = parser.parse_args([])

    assert args.agent == "Code"


def test_acp_command_accepts_agent_option() -> None:
    parser = acp_cli.build_parser()

    args = parser.parse_args(["--agent", "QA", "--approval", "auto"])

    assert args.agent == "QA"
    assert args.approval == "auto"


def test_acp_command_accepts_short_agent_option() -> None:
    parser = acp_cli.build_parser()

    args = parser.parse_args(["-a", "QA"])

    assert args.agent == "QA"


def test_acp_command_ask_user_timeout_defaults_to_none() -> None:
    parser = acp_cli.build_parser()

    args = parser.parse_args([])

    assert args.ask_user_timeout is None


def test_acp_command_accepts_ask_user_timeout() -> None:
    parser = acp_cli.build_parser()

    args = parser.parse_args(["--ask-user-timeout", "120"])

    assert args.ask_user_timeout == 120


def test_acp_command_help_shows_agent_option() -> None:
    parser = acp_cli.build_parser()

    help_text = parser.format_help()

    assert "-a AGENT" in help_text
    assert "--agent AGENT" in help_text
    assert "Agent profile id, name, or display name" in help_text
    assert "-p PROFILE" not in help_text
    assert "--profile PROFILE" not in help_text


def test_acp_help_uses_capitalized_help_text() -> None:
    help_text = acp_cli.build_parser().format_help()

    assert "Show this help message and exit" in help_text
    assert "show this help message and exit" not in help_text


def test_configure_logging_stderr_handler_blocks_propagated_info_records() -> None:
    """OTel sessions raise the ``chrys`` logger to INFO, and the root *logger*
    level does not filter records propagated from child loggers that already
    accepted them — the stderr handler must filter at WARNING itself, or the
    per-tool INFO lines (chrys.kernel.loop) spill onto ACP stderr."""
    root = logging.getLogger()
    saved_handlers = root.handlers[:]
    saved_level = root.level
    chrys_logger = logging.getLogger("chrys")
    saved_chrys_level = chrys_logger.level
    for handler in saved_handlers:
        root.removeHandler(handler)
    try:
        acp_cli._configure_logging()
        (handler,) = root.handlers
        assert isinstance(handler, logging.StreamHandler)
        assert handler.level == logging.WARNING
        stream = io.StringIO()
        handler.setStream(stream)

        chrys_logger.setLevel(logging.INFO)  # what setup_otel() does
        loop_logger = logging.getLogger("chrys.kernel.loop")
        loop_logger.info("Function name: echo")
        assert "Function name" not in stream.getvalue()
        loop_logger.warning("tool failed")
        assert "tool failed" in stream.getvalue()
    finally:
        chrys_logger.setLevel(saved_chrys_level)
        for handler in root.handlers[:]:
            root.removeHandler(handler)
        root.setLevel(saved_level)
        for handler in saved_handlers:
            root.addHandler(handler)


def test_prepare_runtime_loads_dotenv_from_resolved_workdir(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, Any] = {}
    loaded = LoadedSettings(settings=Settings(), provenance={})

    def fake_bootstrap_runtime(**kwargs: Any) -> RuntimeBootstrap:
        seen.update(kwargs)
        return RuntimeBootstrap(loaded=loaded)

    monkeypatch.setattr(acp_cli, "bootstrap_runtime", fake_bootstrap_runtime)

    assert acp_cli._prepare_runtime("/repo") is loaded
    assert seen["dotenv_cwd"] == "/repo"
    assert seen["dotenv_override"] is True
    assert seen["configure_stdio"] is True


def test_prepare_runtime_reports_rejected_settings_on_stderr(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Every root used to keep only ``.settings``, so these said nothing."""
    loaded = load_settings(env={"CHRYS_SESSION_TITLE_AUTO": "nonsense"})
    monkeypatch.setattr(acp_cli, "bootstrap_runtime", lambda **_kwargs: RuntimeBootstrap(loaded=loaded))

    acp_cli._prepare_runtime()

    assert "CHRYS_SESSION_TITLE_AUTO" in capsys.readouterr().err


def test_prepare_runtime_uses_interactive_transient_retry_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CHRYS_MAX_TRANSIENT_RETRIES", raising=False)
    monkeypatch.setattr(
        acp_cli,
        "bootstrap_runtime",
        lambda **_kwargs: RuntimeBootstrap(loaded=LoadedSettings(settings=Settings.from_env(), provenance={})),
    )

    loaded = acp_cli._prepare_runtime()

    assert loaded.settings.effective_max_transient_retries() == 10


@pytest.mark.asyncio
@pytest.mark.parametrize("approval_timeout", [0, 600, 45])
async def test_run_command_wires_buddy_callback_and_approval_timeout(
    monkeypatch: pytest.MonkeyPatch, approval_timeout: int
) -> None:
    freeze_process_env()
    assert persist({"approval.timeout_seconds": approval_timeout}).written == {
        "approval.timeout_seconds": approval_timeout
    }
    captured: dict[str, Any] = {}

    class _Registry:
        def load_all(self) -> None:
            return None

    class _Manager:
        def __init__(self, **kwargs: Any) -> None:
            captured["manager_kwargs"] = kwargs

        async def shutdown(self) -> None:
            captured["shutdown"] = True

    class _Server:
        def __init__(self, manager: _Manager, *, initial_vision: bool, permission_timeout_seconds: float) -> None:
            captured["server_manager"] = manager
            captured["initial_vision"] = initial_vision
            captured["permission_timeout_seconds"] = permission_timeout_seconds

    async def _run_agent(server: _Server) -> None:
        captured["server"] = server

    monkeypatch.setattr(
        acp_cli,
        "_prepare_runtime",
        lambda _process_cwd=None: load_settings(),
    )
    monkeypatch.setattr(acp_cli, "AgentProfileRegistry", _Registry)
    monkeypatch.setattr(acp_cli, "ModelProfileRegistry", _Registry)
    monkeypatch.setattr(acp_cli, "AcpSessionManager", _Manager)
    monkeypatch.setattr(acp_cli, "ChrysAcpServer", _Server)
    monkeypatch.setattr(acp_cli, "_initial_vision", lambda **_kwargs: False)
    monkeypatch.setattr(acp_cli, "_serve_agent", _run_agent)

    args = acp_cli.build_parser().parse_args(["--agent", "Code"])

    assert await acp_cli.run_command(args) == 0
    assert captured["manager_kwargs"]["on_successful_turn"] is acp_cli.on_buddy_successful_turn
    assert captured["permission_timeout_seconds"] == approval_timeout
    assert captured["initial_vision"] is False
    assert captured["shutdown"] is True


@pytest.mark.parametrize("listen_fails", [False, True])
async def test_serve_agent_speaks_the_unstable_protocol_and_always_closes(
    monkeypatch: pytest.MonkeyPatch, listen_fails: bool
) -> None:
    reader, writer, server = object(), object(), object()
    calls: list[str] = []
    captured: dict[str, Any] = {}

    async def _streams(*, limit: int) -> tuple[object, object]:
        captured["limit"] = limit
        return reader, writer

    class _Connection:
        def __init__(
            self,
            to_agent: object,
            input_stream: object,
            output_stream: object,
            listening: bool = True,
            *,
            use_unstable_protocol: bool = False,
            sender_factory: object = None,
        ) -> None:
            captured.update(
                agent=to_agent,
                input_stream=input_stream,
                output_stream=output_stream,
                listening=listening,
                use_unstable_protocol=use_unstable_protocol,
                sender_factory=sender_factory,
            )

        async def listen(self) -> None:
            calls.append("listen")
            if listen_fails:
                raise RuntimeError("receive loop failed")

        async def close(self) -> None:
            calls.append("close")

    monkeypatch.setattr(acp_cli, "stdio_streams", _streams)
    monkeypatch.setattr(acp_cli, "AgentSideConnection", _Connection)

    if listen_fails:
        with pytest.raises(RuntimeError, match="receive loop failed"):
            await acp_cli._serve_agent(server)
    else:
        await acp_cli._serve_agent(server)

    assert calls == ["listen", "close"]
    assert captured == {
        "limit": acp_cli.DEFAULT_STDIO_BUFFER_LIMIT_BYTES,
        "agent": server,
        "input_stream": writer,
        "output_stream": reader,
        "listening": False,
        "use_unstable_protocol": True,
        "sender_factory": AbortableMessageSender,
    }


@pytest.mark.parametrize("old_option", ["-p", "--profile"])
def test_acp_command_rejects_old_agent_aliases(old_option: str) -> None:
    parser = acp_cli.build_parser()

    with pytest.raises(SystemExit):
        parser.parse_args([old_option, "Code"])
