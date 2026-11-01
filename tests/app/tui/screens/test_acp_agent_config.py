# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""App-driven tests for external ACP agent configuration."""

from __future__ import annotations

import os
import tempfile
from types import SimpleNamespace

import pytest
from acp import schema as acp_schema
from textual.app import App, ComposeResult
from textual.widgets import Select, Static, TabbedContent

from chrys.app.tui.screens.agents.config import AgentsConfigScreen
from chrys.app.tui.screens.agents.panels.acp import (
    _AUTH_METHODS_HEADING,
    _CONFIG_OPTIONS_HEADING,
    _MODELS_HEADING,
    _STDERR_TAIL_LABEL,
    AcpConfigPanel,
    _format_handshake,
    _handshake_payload,
)
from chrys.app.tui.screens.agents.panels.subagents import SubAgentsConfigPanel
from chrys.app.tui.screens.dialogs.connection_test import ConnectionTestDialog
from chrys.app.tui.widgets.markdown import VirtualizedMarkdown
from chrys.foundation.i18n import Localizer
from chrys.foundation.i18n.formatting import format_message
from chrys.service.profiles.agents.registry import AgentProfileRegistry
from chrys.service.profiles.agents.schema import (
    AcpAgentConfig,
    AgentProfile,
    MCPServerConfig,
    MemoryConfig,
    ModelConfig,
    SkillsConfig,
    SubAgentRef,
    SubAgentsConfig,
    ToolsConfig,
)
from tests.support.waiting import wait_for


def test_acp_report_chrome_keeps_english_and_localizes_chinese() -> None:
    assert format_message(_MODELS_HEADING.bind()) == "Models"
    assert format_message(_CONFIG_OPTIONS_HEADING.bind()) == "Config Options"
    assert format_message(_AUTH_METHODS_HEADING.bind()) == "Auth Methods"
    assert format_message(_STDERR_TAIL_LABEL.bind()) == "stderr tail (UI only):"

    chinese = Localizer("zh-Hans")
    assert chinese.render(_MODELS_HEADING.bind()) == "模型"
    assert chinese.render(_CONFIG_OPTIONS_HEADING.bind()) == "配置选项"
    assert chinese.render(_AUTH_METHODS_HEADING.bind()) == "认证方式"
    assert chinese.render(_STDERR_TAIL_LABEL.bind()) == "标准错误尾部（仅限界面）："  # noqa: RUF001


class _PanelApp(App):
    def __init__(self, panel: Static) -> None:
        self._panel = panel
        super().__init__()

    def compose(self) -> ComposeResult:
        yield self._panel


@pytest.mark.asyncio
async def test_acp_panel_round_trips_executable_and_windows_quoted_arguments() -> None:
    executable = r"C:\Program Files\Remote Agent\agent.exe"
    args = [
        "--config",
        r"C:\Program Files\Remote Agent\agent config.json",
        'literal "quoted" value',
        r"trailing\\",
    ]
    config = AcpAgentConfig(
        command=executable,
        args=args,
        env={"API_TOKEN": "{{ACP_TOKEN}}"},
        cwd=r"C:\workspace root",
        allow_external_cwd=True,
        session_mode="accept_edits",
        model_id="remote-model",
        config_options={"profile": "strict", "telemetry": False},
        best_effort_options=True,
        result_mode="transcript",
        handshake_timeout_seconds=12.5,
        idle_timeout_seconds=0,
    )
    panel = AcpConfigPanel(config, workspace_cwd=r"C:\workspace root")

    async with _PanelApp(panel).run_test() as pilot:
        await wait_for(
            lambda: len(list(panel.query(".acp-argument-row"))) == len(args),
            pilot=pilot,
            description="ACP argument rows",
        )
        harvested = panel.get_config()
        assert harvested == config
        assert panel.query_one("#acp-mode-caution", Static).display is True


@pytest.mark.parametrize(
    ("mode", "unsafe"),
    [
        ("acceptEdits", True),
        ("bypassPermissions", True),
        ("full-access", True),
        ("full_access", True),
        ("FullAccess", True),
        ("yolo", True),
        ("default", False),
        ("plan", False),
        ("read-only", False),
        ("auto", False),
        ("dontAsk", False),
        ("", False),
    ],
)
def test_unsafe_mode_heuristic_covers_known_prompt_skipping_families(mode: str, unsafe: bool) -> None:
    assert AcpConfigPanel._unsafe_mode(mode) is unsafe


@pytest.mark.filterwarnings("error::UserWarning")
def test_format_handshake_renders_markdown_report() -> None:
    handshake = SimpleNamespace(
        agent_info=acp_schema.Implementation(name="stub-agent", title="Stub", version="1.2.3"),
        capabilities=acp_schema.AgentCapabilities(
            loadSession=True,
            promptCapabilities=acp_schema.PromptCapabilities(image=True, audio=False, embeddedContext=True),
            mcpCapabilities=acp_schema.McpCapabilities(http=True, sse=False),
            sessionCapabilities=acp_schema.SessionCapabilities(fork={}, list={}),
        ),
        modes=acp_schema.SessionModeState(
            currentModeId="auto",
            availableModes=[
                acp_schema.SessionMode(id="manual", name="Manual", description="Prompt every call."),
                acp_schema.SessionMode(id="auto", name="Auto", description="Judge screens calls."),
            ],
        ),
        models=None,
        config_options=(
            acp_schema.SessionConfigOptionSelect(
                id="model",
                name="Model",
                type="select",
                currentValue="fast",
                options=[
                    acp_schema.SessionConfigSelectOption(name="Fast", value="fast", description="Quick model."),
                    acp_schema.SessionConfigSelectOption(name="Deep", value="deep"),
                ],
            ),
            acp_schema.SessionConfigOptionBoolean(id="telemetry", name="Telemetry", type="boolean", currentValue=False),
        ),
        auth_methods=(
            acp_schema.EnvVarAuthMethod(
                id="api-key",
                name="API key",
                type="env_var",
                description="Set the key.",
                vars=[acp_schema.AuthEnvVar(name="STUB_KEY", optional=False, secret=True)],
            ),
        ),
    )

    report = _format_handshake(handshake)

    assert "**Stub** · `stub-agent` · v1.2.3" in report
    # Capability tree flattens to dotted rows; booleans map to marks and
    # nested empty objects (session.fork) are presence markers.
    assert "| `loadSession` | ✓ |" in report
    assert "| `prompt.audio` | — |" in report
    assert "| `mcp.sse` | — |" in report
    assert "| `session.fork` | ✓ |" in report
    # Modes render as a current-caption plus definition bullets.
    assert "Current: **Auto** (`auto`)" in report
    assert "- **Manual** (`manual`) — Prompt every call." in report
    # Absent sections say so instead of vanishing.
    assert "### Models\n\n*Not advertised.*" in report
    # Config options: header line with current value, then choice bullets.
    assert "**Model** · `model` · select · current: `fast`" in report
    assert "- **Fast** (`fast`) — Quick model." in report
    assert "- **Deep** (`deep`)" in report
    assert "**Telemetry** · `telemetry` · boolean · current: `false`" in report
    # Auth methods list their env vars.
    assert "- **API key** (`api-key`) — Set the key. Env: `STUB_KEY`" in report

    localized = _format_handshake(handshake, render_message=Localizer("zh-Hans").render)
    assert "### 能力" in localized
    assert "### 模式" in localized
    assert "| 能力 | 支持 |" in localized
    assert "当前：**Auto** (`auto`)" in localized  # noqa: RUF001
    assert "### 模型\n\n*未声明。*" in localized
    assert "**Model** · `model` · select · 当前值：`fast`" in localized  # noqa: RUF001
    assert "- **API key** (`api-key`) — Set the key. 环境变量：`STUB_KEY`" in localized  # noqa: RUF001


def test_handshake_payload_normalizes_only_omitted_auth_default() -> None:
    omitted = _handshake_payload(acp_schema.AgentCapabilities(loadSession=True))
    explicit = _handshake_payload(acp_schema.AgentCapabilities(auth={}, loadSession=True))

    assert "auth" not in omitted
    assert explicit["auth"] == {}


def test_format_handshake_neutralizes_remote_markdown_structure() -> None:
    """Crafted agent strings must stay inline: no forged headings, bullets, or rows."""
    handshake = SimpleNamespace(
        agent_info=acp_schema.Implementation(
            name="evil`agent", title="Spoof\n\n### Connection Failed", version="1.0\n# v2"
        ),
        capabilities=None,
        modes=acp_schema.SessionModeState(
            currentModeId="x`y",
            availableModes=[
                acp_schema.SessionMode(
                    id="x`y",
                    name="Evil\n\n### Fake Section",
                    description="line one\n\n- fake bullet",
                ),
            ],
        ),
        models=acp_schema.SessionModelState(
            currentModelId="m1",
            availableModels=[acp_schema.ModelInfo(modelId="m1", name="a|b", description="pipe | cell")],
        ),
        config_options=(),
        auth_methods=(),
    )

    report = _format_handshake(handshake)

    # Newlines collapse to spaces, so remote text cannot open a block of its own.
    assert "### Connection Failed" not in report.splitlines()
    assert "**Spoof ### Connection Failed**" in report
    assert "v1.0 # v2" in report
    # Backticks are swapped for quotes, so code spans cannot be broken out of.
    assert "`evil'agent`" in report
    assert "- **Evil ### Fake Section** (`x'y`) — line one - fake bullet" in report
    assert "Current: **Evil ### Fake Section** (`x'y`)" in report
    # Table cells escape pipes so rows keep their column count.
    assert "| a\\|b | `m1` | pipe \\| cell |" in report


@pytest.mark.asyncio
async def test_acp_panel_test_button_opens_ephemeral_session_and_force_closes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    class _FakeClient:
        stderr_tail = ""

        def __init__(self, _spec, _callbacks) -> None:
            calls.append("init")

        async def connect(self) -> None:
            calls.append("connect")

        async def open_session(self):
            calls.append("open_session")
            return SimpleNamespace(
                agent_info=acp_schema.Implementation(name="stub", version="1.0"),
                capabilities=acp_schema.AgentCapabilities(auth=None, loadSession=True),
                modes=None,
                models=None,
                config_options=(),
                auth_methods=(),
            )

        async def force_close(self) -> None:
            calls.append("force_close")

    monkeypatch.setattr("chrys.app.tui.screens.agents.panels.acp.AcpAgentClient", _FakeClient)
    panel = AcpConfigPanel(AcpAgentConfig(command="stub-agent"), workspace_cwd="/workspace")

    async with _PanelApp(panel).run_test() as pilot:
        panel.query_one("#acp-test").press()
        await wait_for(
            lambda: isinstance(pilot.app.screen, ConnectionTestDialog),
            pilot=pilot,
            description="ACP test dialog",
        )
        dialog = pilot.app.screen
        assert isinstance(dialog, ConnectionTestDialog)
        # The markdown widget mounts synchronously with the result, but its
        # source fills in asynchronously on mount — poll for the content.
        await wait_for(
            lambda: (
                bool(dialog.query("#connection-test-markdown"))
                and bool(dialog.query_one("#connection-test-markdown", VirtualizedMarkdown).source)
            ),
            pilot=pilot,
            description="ACP handshake result",
        )

        # Success renders the Markdown report in a widened dialog; the plain
        # text body is gone with the rest of the loading chrome.
        assert dialog.has_class("-markdown")
        assert not dialog.query("#connection-test-message")
        report = dialog.query_one("#connection-test-markdown", VirtualizedMarkdown).source
        assert "**stub** · v1.0" in report
        assert "### Capabilities" in report
        assert "| `loadSession` | ✓ |" in report
        assert calls == ["init", "connect", "open_session", "force_close"]


@pytest.mark.asyncio
async def test_acp_panel_test_stderr_log_path_has_no_symlinked_parents(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # macOS $TMPDIR sits under the /var -> private/var symlink, which the
    # secure stderr-log open rejects; the test flow must hand the client a
    # fully resolved log path. Route the tempdir through a symlink to model
    # that on any platform.
    real = tmp_path / "real-tmp"
    real.mkdir()
    link = tmp_path / "link-tmp"
    try:
        link.symlink_to(real, target_is_directory=True)
    except OSError:
        pytest.skip("symlinks unavailable on this platform")
    monkeypatch.setattr(tempfile, "tempdir", str(link))

    specs = []

    class _FakeClient:
        stderr_tail = ""

        def __init__(self, spec, _callbacks) -> None:
            specs.append(spec)

        async def connect(self) -> None:
            pass

        async def open_session(self):
            return SimpleNamespace(
                agent_info=acp_schema.Implementation(name="stub", version="1.0"),
                capabilities=acp_schema.AgentCapabilities(auth=None, loadSession=True),
                modes=None,
                models=None,
                config_options=(),
                auth_methods=(),
            )

        async def force_close(self) -> None:
            pass

    monkeypatch.setattr("chrys.app.tui.screens.agents.panels.acp.AcpAgentClient", _FakeClient)
    panel = AcpConfigPanel(AcpAgentConfig(command="stub-agent"), workspace_cwd="/workspace")

    async with _PanelApp(panel).run_test() as pilot:
        panel.query_one("#acp-test").press()
        await wait_for(
            lambda: isinstance(pilot.app.screen, ConnectionTestDialog),
            pilot=pilot,
            description="ACP test dialog",
        )
        dialog = pilot.app.screen
        assert isinstance(dialog, ConnectionTestDialog)
        await wait_for(
            lambda: bool(dialog.query("#connection-test-markdown")),
            pilot=pilot,
            description="ACP handshake result",
        )

    assert len(specs) == 1
    stderr_path = specs[0].stderr_log_path
    assert os.path.realpath(stderr_path) == str(stderr_path)


@pytest.mark.asyncio
async def test_acp_panel_validates_expanded_cwd_template_like_real_invocation(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "workspace"
    outside = tmp_path / "outside"
    workspace.mkdir()
    outside.mkdir()
    monkeypatch.setenv("CHRYS_ACP_TEST_CWD", str(outside))
    panel = AcpConfigPanel(
        AcpAgentConfig(command="agent", cwd="{{CHRYS_ACP_TEST_CWD}}"),
        workspace_cwd=str(workspace),
    )

    async with _PanelApp(panel).run_test() as pilot:
        await wait_for(
            lambda: bool(list(panel.query("#acp-command"))),
            pilot=pilot,
            description="ACP panel inputs",
        )
        errors = panel.validate()
        assert any("outside all workspace roots" in error for error in errors)

        monkeypatch.delenv("CHRYS_ACP_TEST_CWD")
        errors = panel.validate()
        assert any("CHRYS_ACP_TEST_CWD" in error for error in errors)

        monkeypatch.setenv("CHRYS_ACP_TEST_CWD", str(workspace / "inner"))
        assert panel.validate() == []


@pytest.mark.parametrize("field", ["command", "cwd"])
def test_acp_panel_test_refuses_commands_for_what_runs_and_where(tmp_path, field: str) -> None:
    panel = AcpConfigPanel(AcpAgentConfig(command="agent"), workspace_cwd=str(tmp_path))
    config = AcpAgentConfig(**{"command": "agent", field: "{{!tool --password hunter2}}"})

    with pytest.raises(ValueError, match="not supported") as caught:
        panel._test_spec(config, tmp_path / "stderr.log")

    assert "hunter2" not in str(caught.value)


def test_acp_panel_test_leaves_commands_in_arguments_and_environment_to_the_launch(tmp_path) -> None:
    panel = AcpConfigPanel(AcpAgentConfig(command="agent"), workspace_cwd=str(tmp_path))
    config = AcpAgentConfig(command="agent", args=["{{!tool get}}"], env={"TOKEN": "{{!tool get}}"})

    spec = panel._test_spec(config, tmp_path / "stderr.log")

    assert spec.args[0].startswith("chrys-cmd-")
    assert spec.env["TOKEN"] == spec.args[0]


def test_canonicalize_profile_matches_acp_panel_harvest_normalization() -> None:
    profile = AgentProfile(
        name="External",
        sub_agent_only=True,
        acp=AcpAgentConfig(
            command="  /usr/bin/agent  ",
            args=["  keep raw  ", ""],
            env={"  TOKEN  ": "  raw value  ", "   ": "dropped"},
            cwd=" /workspace ",
            session_mode=" accept_edits ",
            model_id=" remote ",
            config_options={" profile ": " raw ", " ": True},
        ),
    )

    AgentsConfigScreen._canonicalize_profile_for_ui(profile)

    acp = profile.acp
    assert acp is not None
    assert acp.command == "/usr/bin/agent"
    assert acp.cwd == "/workspace"
    assert acp.session_mode == "accept_edits"
    assert acp.model_id == "remote"
    assert acp.args == ["  keep raw  ", ""]
    assert acp.env == {"TOKEN": "  raw value  "}
    assert acp.config_options == {"profile": " raw "}


@pytest.mark.asyncio
async def test_agent_type_switch_sanitizes_hidden_sections_and_mounts_only_acp_tab() -> None:
    registry = AgentProfileRegistry()
    registry.load_builtins()
    profile = AgentProfile(
        name="Custom",
        display_name="Custom",
        description="Custom agent",
        instructions="model instructions",
        tools=ToolsConfig(
            builtins=["read_file"],
            custom=["custom_tool"],
            mcp=[MCPServerConfig(name="remote", transport="http", url="https://example.test")],
        ),
        skills=SkillsConfig(paths=["skills"]),
        sub_agents=SubAgentsConfig(agents=[SubAgentRef(profile="Explore")]),
        memory=MemoryConfig(files=["README.md"]),
    )
    registry.register(profile)
    screen = AgentsConfigScreen(registry, current_profile="Custom", initial_profile="Custom")

    class _ScreenHost(App):
        def compose(self) -> ComposeResult:
            yield Static("host")

    async with _ScreenHost().run_test() as pilot:
        await pilot.app.push_screen(screen)
        await wait_for(
            lambda: bool(list(screen.query("#bc-agent-type"))) and not screen._hydrating,
            pilot=pilot,
            timeout=10,
            description="agent type selector",
        )
        selector = screen.query_one("#bc-agent-type", Select)
        assert selector.disabled is False
        # Reproduce the loaded-CI race deterministically: the Select posts its
        # Changed message asynchronously, so hydration can re-arm between the
        # caller observing the settled screen and the handler receiving the
        # user's edit.  The edit must be queued instead of silently discarded.
        screen._hydrating = True
        selector.value = "acp"
        await wait_for(
            lambda: screen._pending_agent_type_change == (screen._selected_draft_key, "acp"),
            pilot=pilot,
            timeout=10,
            description="queued ACP agent type change",
        )
        screen._finish_hydrating(screen._hydrating_generation, 0)

        await wait_for(
            lambda: screen._drafts[screen._selected_draft_key].profile.acp is not None and not screen._hydrating,
            pilot=pilot,
            timeout=10,
            description="ACP profile hydration",
        )
        tabs = screen.query_one("#ac-tabs", TabbedContent)
        tabs.active = "acp"
        await wait_for(
            lambda: bool(list(screen.query(AcpConfigPanel))) and not screen._hydrating,
            pilot=pilot,
            timeout=10,
            description="ACP tab panel",
        )
        draft = screen._drafts[screen._selected_draft_key]
        assert draft.dirty is True
        assert draft.profile.sub_agent_only is True
        assert draft.profile.instructions == ""
        assert draft.profile.tools == ToolsConfig()
        assert draft.profile.skills.paths == []
        assert draft.profile.skills.inline == []
        assert draft.profile.sub_agents == SubAgentsConfig()
        assert draft.profile.memory == MemoryConfig()
        assert draft.profile.model == ModelConfig()

        assert tabs.get_tab("acp").display is True
        for tab_id in ("instructions", "tools", "sub-agents", "skills", "mcp", "memory", "compaction"):
            assert tabs.get_tab(tab_id).display is False
        assert screen.query_one("#bc-sub-agent-only").disabled is True
        model_section = list(screen.query(".bc-model-section"))
        assert len(model_section) == 4  # separator, title, desc, row
        for section_widget in model_section:
            assert section_widget.display is False


@pytest.mark.asyncio
async def test_builtin_agent_type_is_locked_to_model_driven() -> None:
    registry = AgentProfileRegistry()
    registry.load_builtins()
    screen = AgentsConfigScreen(registry, current_profile="Code", initial_profile="Code")

    class _ScreenHost(App):
        def compose(self) -> ComposeResult:
            yield Static("host")

    async with _ScreenHost().run_test() as pilot:
        await pilot.app.push_screen(screen)
        await wait_for(
            lambda: bool(list(screen.query("#bc-agent-type"))) and not screen._hydrating,
            pilot=pilot,
            timeout=10,
            description="builtin agent type selector",
        )

        selector = screen.query_one("#bc-agent-type", Select)
        assert selector.value == "builtin"
        assert selector.disabled is True


@pytest.mark.asyncio
async def test_sub_agent_profile_select_marks_external_profiles_with_acp_suffix() -> None:
    parent = AgentProfile(name="Parent")
    external = AgentProfile(
        name="Remote",
        display_name="Remote Helper",
        sub_agent_only=True,
        acp=AcpAgentConfig(command="remote-agent"),
    )
    panel = SubAgentsConfigPanel(
        SubAgentsConfig(agents=[SubAgentRef(profile="Remote")]),
        current_profile_name=parent.name,
        available_profiles=[parent, external],
    )

    async with _PanelApp(panel).run_test() as pilot:
        await wait_for(
            lambda: bool(list(panel.query("#sa-profile-0"))),
            pilot=pilot,
            description="sub-agent profile select",
        )
        select = panel.query_one("#sa-profile-0", Select)
        labels = [str(label) for label, _value in select._options]
        assert "Remote Helper (ACP)" in labels
