# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Markdown rendering of successful MCP connection reports."""

from __future__ import annotations

import pytest

from chrys.app.tui.screens.agents.panels.mcp import _format_mcp_report
from chrys.foundation.i18n import Localizer


def test_format_mcp_report_renders_markdown_report() -> None:
    from chrys.service.mcp.adapter import MCPTestReport
    from chrys.service.profiles.agents.schema import MCPServerConfig

    config = MCPServerConfig(name="everything", transport="stdio", command="npx", args=["-y", "@mcp/everything"])
    report = MCPTestReport(
        server_name="everything",
        server_title="Everything Server",
        server_version="1.0.0",
        protocol_version="2025-06-18",
        capabilities=("logging", "prompts", "tools", "tools.listChanged"),
        instructions="Use these tools wisely.",
        tools=(("echo", "Echo a message."), ("add", "")),
        prompts=(("greet", "Say hello."),),
        initial_tool_names=None,
    )

    text = _format_mcp_report(config, report)

    assert "**Everything Server** · `everything` · v1.0.0 · MCP `2025-06-18`" in text
    assert "Connected via `stdio`: `npx -y @mcp/everything`" in text
    # Standard capability groups always render, marked advertised or not;
    # nested flags appear as dotted rows.
    assert "| `tools` | ✓ |" in text
    assert "| `tools.listChanged` | ✓ |" in text
    assert "| `resources` | — |" in text
    assert "| `completions` | — |" in text
    assert "> *Use these tools wisely.*" in text
    assert "2 tools available to the model." in text
    assert "- **echo** — Echo a message." in text
    assert "- **add**" in text
    assert "1 prompt template loaded as a tool." in text
    assert "- **greet** — Say hello." in text
    # Progressive disclosure off: no section at all.
    assert "On-demand Loading" not in text
    assert "Working directory:" not in text


@pytest.mark.parametrize(
    ("locale", "label"),
    [("en", "Working directory: "), ("zh-Hans", "工作目录：")],  # noqa: RUF001 — Chinese punctuation
)
@pytest.mark.parametrize("transport", ["stdio", "http"])
def test_format_mcp_report_shows_effective_stdio_directory_below_command(
    locale: str, label: str, transport: str
) -> None:
    from chrys.service.mcp.adapter import MCPTestReport
    from chrys.service.profiles.agents.schema import MCPServerConfig

    config = MCPServerConfig(
        name="server", transport=transport, command="python", url="https://example.com/mcp", cwd="unresolved"
    )
    report = MCPTestReport(
        server_name="server",
        server_title=None,
        server_version=None,
        protocol_version=None,
        capabilities=(),
        instructions=None,
        tools=(),
        prompts=(),
        initial_tool_names=None,
        effective_cwd="/work/a `b`/[c]*|<d>\\folder\n### fake",
    )

    text = _format_mcp_report(config, report, render_message=Localizer(locale).render)

    if transport == "stdio":
        sections = text.split("\n\n")
        assert "`stdio`" in sections[1]
        assert "`python`" in sections[1]
        assert sections[2] == f"{label}`/work/a 'b'/[c]*|<d>\\folder ### fake`"
        assert sections[3].startswith("### ")
        assert "unresolved" not in text
    else:
        assert label not in text
        assert "/work/" not in text


def test_format_mcp_report_states_absences_instead_of_vanishing() -> None:
    from chrys.service.mcp.adapter import MCPTestReport
    from chrys.service.profiles.agents.schema import MCPServerConfig

    config = MCPServerConfig(name="bare", transport="http", url="https://api.example.com/mcp", load_prompts=False)
    report = MCPTestReport(
        server_name=None,
        server_title=None,
        server_version=None,
        protocol_version=None,
        capabilities=("tools",),
        instructions=None,
        tools=(),
        prompts=(),
        initial_tool_names=("mcp_list_tools", "search"),
    )

    text = _format_mcp_report(config, report)

    assert "*The server did not report its identity.*" in text
    assert "Connected via `http`: `https://api.example.com/mcp`" in text
    assert "### Instructions\n\n*Not advertised.*" in text
    assert "*The server supports tools, but none are available (check the available-tool scope).*" in text
    assert "*Prompt loading is disabled for this server.*" in text
    assert "### On-demand Loading" in text
    assert "- `mcp_list_tools`" in text
    assert "- `search`" in text


def test_format_mcp_report_neutralizes_remote_markdown_structure() -> None:
    """Crafted server strings must stay inline: no forged headings, bullets, or rows."""
    from chrys.service.mcp.adapter import MCPTestReport
    from chrys.service.profiles.agents.schema import MCPServerConfig

    config = MCPServerConfig(name="evil", transport="http", url="https://evil.example/mcp")
    report = MCPTestReport(
        server_name="evil`server",
        server_title="Spoof\n\n### Connection Failed",
        server_version="1.0\n# v2",
        protocol_version="x`y",
        capabilities=("tools", "experimental.bad|pipe"),
        instructions="### Fake Section\nline one\n- fake bullet",
        tools=(("hack", "desc\n\n## Forged Heading \x1b[2J\x1b]52;c;evil\x07"),),
        prompts=(),
        initial_tool_names=("mcp`list",),
    )

    text = _format_mcp_report(config, report)

    # Newlines collapse to spaces, so remote text cannot open a block of its own.
    assert "### Connection Failed" not in text.splitlines()
    assert "**Spoof ### Connection Failed**" in text
    assert "v1.0 # v2" in text
    # Backticks are swapped for quotes, so code spans cannot be broken out of.
    assert "`evil'server`" in text
    assert "MCP `x'y`" in text
    # Instructions starting with a block marker stay behind our quote+italic
    # markers — blockquote content is block-parsed, so a bare leading "###"
    # would otherwise render as a real heading.
    assert "> *### Fake Section line one - fake bullet*" in text
    assert "- **hack** — desc ## Forged Heading" in text
    assert "- `mcp'list`" in text
    # Terminal escape/control sequences are stripped, not passed through to
    # the terminal (screen clears, OSC-52 clipboard writes).
    assert "\x1b" not in text
    assert "\x07" not in text
    # Table cells escape pipes so rows keep their column count.
    assert "| `experimental.bad\\|pipe` | ✓ |" in text


def test_format_mcp_report_bounds_identity_and_capability_rows() -> None:
    """Server-controlled identity and capability lists cannot bloat the report."""
    from chrys.service.mcp.adapter import MCPTestReport
    from chrys.service.profiles.agents.schema import MCPServerConfig

    config = MCPServerConfig(name="big", transport="stdio", command="python")
    report = MCPTestReport(
        server_name="big",
        server_title="T" * 500,
        server_version=None,
        protocol_version=None,
        capabilities=("experimental." + "a" * 300, *(f"experimental.cap{i:03d}" for i in range(45))),
        instructions=None,
        tools=(),
        prompts=(),
        initial_tool_names=None,
    )

    text = _format_mcp_report(config, report)

    assert "**" + "T" * 120 + "…**" in text
    assert "T" * 121 not in text
    # Standard groups always render, even when extras alone would fill the
    # 40-row budget; extras only occupy the remaining slots.
    for standard in ("completions", "logging", "prompts", "resources", "tools"):
        assert f"| `{standard}` | — |" in text
    # Each extra name is clamped: 46 extras, 35 shown, one of them huge.
    assert "experimental." + "a" * 107 + "…" in text
    assert "a" * 200 not in text
    assert "*…and 11 more.*" in text
    assert "| Capability | Advertised |" in text
    assert text.count("| `") == 40

    localized = _format_mcp_report(config, report, render_message=Localizer("zh-Hans").render)
    assert "*…另有 11 个。*" in localized
    assert "| 能力 | 已声明 |" in localized
