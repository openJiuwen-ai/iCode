# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Importer causes and localized advice agree without exposing source values."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from chrys.app.tui.screens.agents.import_mcp_view import (
    imported_config,
    names_field,
    original_setting,
    problem_text,
    source_error_text,
    warning_text,
)
from chrys.foundation.i18n import Localizer
from chrys.foundation.i18n.formatting import format_message
from chrys.service.mcp.importers import (
    FieldUse,
    ImportContext,
    ImportWarning,
    McpImportCandidate,
    McpSourceError,
    Problem,
    SourceErrorKind,
    ValueReason,
    WarningKind,
    base,
)
from chrys.service.mcp.importers.claude_code import ClaudeCodeSource
from chrys.service.mcp.importers.codex import CodexSource
from chrys.service.mcp.importers.opencode import OpenCodeSource
from chrys.service.mcp.importers.pi import PiSource
from chrys.service.profiles.agents.schema import MCPServerConfig


def discover(home: Path, source: str, entry: dict[str, object] | str) -> McpImportCandidate:
    ctx = ImportContext(None, home)
    if source == "codex":
        if not isinstance(entry, str):
            raise TypeError("Codex fixture must be TOML")
        path = home / ".codex" / "config.toml"
        text = "[mcp_servers.srv]\n" + entry
        importer = CodexSource()
    elif source == "opencode":
        path = home / ".config" / "opencode" / "opencode.json"
        text = json.dumps({"mcp": {"srv": entry}})
        importer = OpenCodeSource()
    elif source == "pi":
        path = home / ".pi" / "agent" / "mcp.json"
        text = json.dumps({"mcpServers": {"srv": entry}})
        importer = PiSource()
    else:
        path = home / ".claude.json"
        text = json.dumps({"mcpServers": {"srv": entry}})
        importer = ClaudeCodeSource()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    (candidate,) = importer.discover(ctx).candidates
    return candidate


@pytest.mark.parametrize("source", ["codex", "claude", "opencode", "pi"])
def test_sources_forward_unpreservable_env_names_to_shared_conversion(tmp_path, source) -> None:
    key = " TOKEN"
    quoted = json.dumps(key)
    entry: dict[str, object] | str
    if source == "codex":
        entry = f'command = "run"\nenv = {{ {quoted} = "private-source-value" }}\n'
    elif source == "opencode":
        entry = {"type": "local", "command": ["run"], "environment": {key: "private-source-value"}}
    else:
        entry = {"command": "run", "env": {key: "private-source-value"}}

    item = discover(tmp_path, source, entry)

    assert item.config is None
    assert item.problem is Problem.VALUE_NOT_REPRESENTABLE
    assert (item.field_use, item.reason) == (FieldUse.ENV_NAME, ValueReason.WHITESPACE)
    assert item.problem_field.endswith(f"[{quoted}]")
    assert "private-source-value" not in item.problem_field
    assert imported_config(item, source, format_message) is None


@pytest.mark.parametrize("locale", ["en", "zh-Hans"])
@pytest.mark.parametrize(("key", "reason"), [("", ValueReason.EMPTY), ("\tTOKEN", ValueReason.WHITESPACE)])
def test_unpreservable_env_names_have_localized_advice(locale, key, reason) -> None:
    quoted = json.dumps(key)
    item = McpImportCandidate(
        "srv",
        "/config",
        None,
        problem=Problem.VALUE_NOT_REPRESENTABLE,
        problem_field=f"servers.srv.env[{quoted}]",
        field_use=FieldUse.ENV_NAME,
        reason=reason,
    )
    localizer = Localizer(locale)
    text = localizer.render(problem_text(item, render=localizer.render))
    assert ("editor cannot preserve" if locale == "en" else "编辑器无法原样保留") in text
    assert ("original configuration file" if locale == "en" else "原配置文件") not in text
    assert ("invalid" if locale == "en" else "非法") not in text
    setting = localizer.render(original_setting(item.problem_field))
    assert f"[{quoted}]" in setting


@pytest.mark.parametrize("source", ["codex", "claude", "opencode", "pi"])
@pytest.mark.parametrize("in_command", [False, True])
def test_sources_report_the_exact_command_field_with_changed_line_endings(tmp_path, source, in_command) -> None:
    command = "private\rsource\nvalue" if in_command else "run"
    args = [] if in_command else ["safe", "private\rsource\nvalue"]
    if source == "codex":
        entry = f"command = {json.dumps(command)}\nargs = {json.dumps(args)}\n"
        field = "mcp_servers.srv." + ("command" if in_command else "args[1]")
    elif source == "opencode":
        entry = {"type": "local", "command": [command, *args]}
        field = f"mcp.srv.command[{0 if in_command else 2}]"
    else:
        entry = {"command": command, "args": args}
        field = "mcpServers.srv." + ("command" if in_command else "args[1]")
    item = discover(tmp_path, source, entry)

    assert item.config is None
    assert item.problem is Problem.VALUE_NOT_REPRESENTABLE
    assert item.reason is ValueReason.LINE_ENDINGS
    assert item.field_use is (FieldUse.COMMAND if in_command else FieldUse.ARGUMENT)
    assert item.problem_field == field


@pytest.mark.parametrize("locale", ["en", "zh-Hans"])
@pytest.mark.parametrize("use", [FieldUse.COMMAND, FieldUse.ARGUMENT])
def test_changed_line_endings_have_localized_advice_without_parameter_values(locale, use) -> None:
    item = McpImportCandidate(
        "srv",
        "/config",
        None,
        problem=Problem.VALUE_NOT_REPRESENTABLE,
        problem_field="servers.srv.args[0]",
        field_use=use,
        reason=ValueReason.LINE_ENDINGS,
    )
    localizer = Localizer(locale)
    text = localizer.render(problem_text(item, render=localizer.render))

    assert ("cannot be preserved during import" if locale == "en" else "换行形式无法原样保留") in text
    field = (
        ("startup command" if locale == "en" else "启动命令")
        if use is FieldUse.COMMAND
        else ("startup argument" if locale == "en" else "启动参数")
    )
    assert field in text
    assert "private" not in text
    assert ("add the server manually" if locale == "en" else "手动添加") not in text


@pytest.mark.parametrize("is_set", [False, True])
@pytest.mark.parametrize(
    ("source", "entry", "field", "expected"),
    [
        ("codex", 'url = "https://example.com"\nenv_http_headers = { X = "TOKEN" }\n', "headers", {"X": "{{TOKEN}}"}),
        (
            "codex",
            'url = "https://example.com"\nbearer_token_env_var = "TOKEN"\n',
            "headers",
            {"Authorization": "Bearer {{TOKEN}}"},
        ),
        (
            "claude",
            {"command": "run", "env": {"A": "${TOKEN}", "DIR": "${CLAUDE_PROJECT_DIR}"}},
            "env",
            {"A": "{{TOKEN}}", "DIR": "{{CLAUDE_PROJECT_DIR}}"},
        ),
        (
            "opencode",
            {"type": "local", "command": ["run"], "environment": {"A": "{env:TOKEN}"}},
            "env",
            {"A": "{{TOKEN}}"},
        ),
        (
            "opencode",
            {"type": "remote", "url": "https://example.com", "headers": {"X": "{env:TOKEN}"}},
            "headers",
            {"X": "{{TOKEN}}"},
        ),
    ],
)
def test_variable_references_are_preserved_without_import_warnings(
    tmp_path, monkeypatch, source, entry, field, expected, is_set
) -> None:
    for name in ("TOKEN", "CLAUDE_PROJECT_DIR"):
        if is_set:
            monkeypatch.setenv(name, "private-resolved-value")
        else:
            monkeypatch.delenv(name, raising=False)
    item = discover(tmp_path, source, entry)
    assert item.config is not None
    assert (item.config.env if field == "env" else item.config.headers) == expected
    assert item.warnings == ()
    saved = imported_config(item, source, Localizer("en").render)
    assert saved is not None
    assert "⚠" not in saved.description
    assert "private-resolved-value" not in saved.description


@pytest.mark.parametrize("locale", ["en", "zh-Hans"])
@pytest.mark.parametrize(
    ("source", "entry"),
    [
        ("codex", 'command = "run"\nunknown = "private-source-value"\n'),
        ("claude", {"command": "run", "unknown": "private-source-value"}),
        ("opencode", {"type": "local", "command": ["run"], "unknown": "private-source-value"}),
        ("pi", {"command": "run", "unknown": "private-source-value"}),
    ],
)
def test_unknown_settings_name_the_location_without_promising_support(tmp_path, source, entry, locale) -> None:
    item = discover(tmp_path, source, entry)
    localizer = Localizer(locale)
    assert item.config is not None
    assert item.config.command == "run"
    assert item.config.allowed_tools is None
    assert len(item.warnings) == 1
    warning = item.warnings[0]
    assert warning.kind is WarningKind.NOT_IMPORTED
    assert warning.items[0].endswith(".srv.unknown")
    saved = imported_config(item, source, localizer.render)
    assert saved is not None
    assert localizer.render(warning_text(warning, source)) in saved.description
    assert ("Original configuration field:" if locale == "en" else "原配置项") in saved.description
    assert warning.items[0] in saved.description
    assert "private-source-value" not in saved.description
    assert ("unsupported" if locale == "en" else "不支持") not in saved.description
    assert ("manually" if locale == "en" else "手动") not in saved.description


@pytest.mark.parametrize("locale", ["en", "zh-Hans"])
def test_oauth_advice_is_conditional_and_survives_import(locale: str) -> None:
    warning = ImportWarning(WarningKind.AUTH_NOT_IMPORTED)
    item = McpImportCandidate(
        "srv",
        "/config",
        MCPServerConfig(name="srv", transport="http", url="https://example.com"),
        warnings=(warning,),
    )
    localizer = Localizer(locale)
    saved = imported_config(item, "ChatGPT", localizer.render)
    assert saved is not None
    text = localizer.render(warning_text(warning, "ChatGPT"))
    assert text in saved.description
    if locale == "en":
        assert "does not yet support MCP OAuth" in text
        assert "If the server supports token" in text
        assert "If it only supports OAuth" in text
    else:
        assert "暂不支持 MCP OAuth" in text
        assert "如果服务器支持令牌" in text
        assert "如果仅支持 OAuth" in text


@pytest.mark.parametrize("locale", ["en", "zh-Hans"])
@pytest.mark.parametrize("kind", [WarningKind.STARTUP_WAIT, WarningKind.DEFAULT_TIMEOUT])
def test_timeout_warnings_name_the_source_application(locale: str, kind: WarningKind) -> None:
    text = Localizer(locale).render(warning_text(ImportWarning(kind), "Source App"))

    assert "Source App" in text
    assert "ChatGPT" not in text


def test_known_warnings_keep_the_connection_timeout_and_tool_results(tmp_path) -> None:
    codex = discover(
        tmp_path,
        "codex",
        'command = "run"\nstartup_timeout_sec = 5\ndefault_tools_approval_mode = "approve"\ntool_timeout_sec = 0\ndisabled_tools = ["write"]\n',
    )
    assert codex.config is not None
    assert (codex.config.command, codex.config.request_timeout, codex.config.allowed_tools) == ("run", None, [])
    assert [w.kind for w in codex.warnings] == [
        WarningKind.STARTUP_WAIT,
        WarningKind.APPROVAL,
        WarningKind.TOOLS_RESTRICTED,
        WarningKind.DEFAULT_TIMEOUT,
    ]
    claude = discover(
        tmp_path,
        "claude",
        {
            "type": "http",
            "url": "https://example.com",
            "headers": {
                "Authorization": "${ANTHROPIC_API_KEY:-private-credential}",
                "X-Region": "${REGION:-private-region}",
                "X-Zone": "${ZONE:-private-zone}",
            },
            "oauth": {},
        },
    )
    assert claude.config is not None
    assert claude.config.headers == {"X-Region": "{{REGION}}", "X-Zone": "{{ZONE}}"}
    assert claude.warnings == (
        ImportWarning(WarningKind.AUTH_HEADERS, ("mcpServers.srv.headers.Authorization",)),
        ImportWarning(WarningKind.AUTH_NOT_IMPORTED),
        ImportWarning(
            WarningKind.VARIABLE_DEFAULTS_DROPPED, ("mcpServers.srv.headers.X-Region", "mcpServers.srv.headers.X-Zone")
        ),
    )
    pi = discover(tmp_path, "pi", {"command": "run", "exposure": "codemode", "toolExposure": {"big_*": "direct"}})
    assert pi.config is not None
    assert pi.config.allowed_tools is None
    assert pi.config.use_progressive_disclosure
    assert [w.kind for w in pi.warnings] == [WarningKind.TOOL_LOADING]
    for locale in ("en", "zh-Hans"):
        localizer = Localizer(locale)
        for item in (codex, claude, pi):
            saved = imported_config(item, "Source", localizer.render)
            assert saved is not None
            assert "private-" not in saved.description
            for warning in item.warnings:
                assert localizer.render(warning_text(warning, "Source")) in saved.description
        text = localizer.render(warning_text(pi.warnings[0], "Pi"))
        assert ("Loading Strategy" if locale == "en" else "加载策略") in text
        assert ("turned off" if locale == "en" else "暂未启用") not in text


def test_pi_url_without_http_prefix_is_refused_in_preview(tmp_path: Path) -> None:
    item = discover(tmp_path, "pi", {"url": "https:example.org/mcp"})

    assert item.config is None
    assert item.problem is Problem.VALUE_NOT_REPRESENTABLE
    assert names_field(item)
    assert item.problem_field == "mcpServers.srv.url"
    assert format_message(problem_text(item)) == "URL must start with http:// or https://."


@pytest.mark.parametrize(
    ("source", "entry", "use", "reason"),
    [
        ("opencode", {"type": "remote", "url": "{file:private-source-value}"}, FieldUse.URL, ValueReason.FILE),
        ("opencode", {"type": "local", "command": ["{file:private-source-value}"]}, FieldUse.COMMAND, ValueReason.FILE),
        (
            "opencode",
            {"type": "local", "command": ["run", "{file:private-source-value}"]},
            FieldUse.ARGUMENT,
            ValueReason.FILE,
        ),
        (
            "opencode",
            {"type": "local", "command": ["run"], "environment": {"T": "{file:private-source-value}"}},
            FieldUse.ENV_VALUE,
            ValueReason.FILE,
        ),
        (
            "opencode",
            {"type": "local", "command": ["run"], "cwd": "{file:private-source-value}"},
            FieldUse.CWD,
            ValueReason.FILE,
        ),
        (
            "opencode",
            {"type": "remote", "url": "https://example.com", "headers": {"T": "{file:private-source-value}"}},
            FieldUse.HEADER_VALUE,
            ValueReason.FILE,
        ),
        ("pi", {"command": "run", "env": {"T": "!private-source-value"}}, FieldUse.ENV_VALUE, ValueReason.COMMAND),
        (
            "pi",
            {"url": "https://example.com", "headers": {"T": "!private-source-value"}},
            FieldUse.HEADER_VALUE,
            ValueReason.COMMAND,
        ),
        ("claude", {"type": "http", "url": "https://${SECRET_VALUE}"}, FieldUse.URL, ValueReason.VARIABLE),
        ("claude", {"command": "${SECRET_VALUE}"}, FieldUse.COMMAND, ValueReason.VARIABLE),
        ("claude", {"command": "run", "args": ["${SECRET_VALUE}"]}, FieldUse.ARGUMENT, ValueReason.VARIABLE),
        (
            "opencode",
            {"type": "local", "command": ["run"], "environment": {"T": "{env:private-source-value}"}},
            FieldUse.ENV_VALUE,
            ValueReason.VARIABLE,
        ),
        (
            "opencode",
            {"type": "local", "command": ["run"], "cwd": "{env:ROOT}/../private-source-value"},
            FieldUse.CWD,
            ValueReason.VARIABLE_PARENT,
        ),
        ("codex", 'command = "run"\nenv = { T = "" }\n', FieldUse.ENV_VALUE, ValueReason.EMPTY),
        (
            "codex",
            'command = "run"\nenv = { T = " private-source-value " }\n',
            FieldUse.ENV_VALUE,
            ValueReason.WHITESPACE,
        ),
        (
            "codex",
            'command = "run"\ncwd = " private-source-value "\n',
            FieldUse.CWD,
            ValueReason.WHITESPACE,
        ),
        (
            "codex",
            'url = "https://example.com"\nhttp_headers = { X = "a", x = "b" }\n',
            FieldUse.HEADER_VALUE,
            ValueReason.HEADER_CONFLICT,
        ),
        (
            "claude",
            {"type": "http", "url": "https://example.com", "headers": {"Bad Header": "private-source-value"}},
            FieldUse.HEADER_NAME,
            ValueReason.CHARACTERS,
        ),
        (
            "claude",
            {"type": "http", "url": "https://example.com", "headers": {"X": "中文-private-source-value"}},
            FieldUse.HEADER_VALUE,
            ValueReason.CHARACTERS,
        ),
    ],
)
@pytest.mark.parametrize("locale", ["en", "zh-Hans"])
def test_refused_values_record_the_cause_and_offer_only_supported_actions(
    tmp_path, source, entry, use, reason, locale
) -> None:
    item = discover(tmp_path, source, entry)
    assert item.config is None
    assert (item.field_use, item.reason) == (use, reason)
    assert item.problem_field
    localizer = Localizer(locale)
    text = localizer.render(problem_text(item, render=localizer.render))
    assert "private-source-value" not in text
    assert "private-source-value" not in item.problem_field
    assert ("Original configuration field:" if locale == "en" else "原配置项") not in text
    if reason is ValueReason.WHITESPACE:
        assert ("editor would remove" if locale == "en" else "编辑器会去掉") in text
        assert ("original configuration file" if locale == "en" else "原配置文件") not in text
    variable_advice = "reference an environment variable" if locale == "en" else "引用环境变量"
    command_advice = "get the value from a command" if locale == "en" else "取值命令"
    if reason in (ValueReason.FILE, ValueReason.COMMAND, ValueReason.VARIABLE):
        assert (variable_advice in text) == (use in (FieldUse.ENV_VALUE, FieldUse.HEADER_VALUE, FieldUse.CWD))
        assert (command_advice in text) == (use in (FieldUse.ENV_VALUE, FieldUse.HEADER_VALUE))
    if reason is ValueReason.FILE:
        assert ("stored in another file" if locale == "en" else "保存在另一个文件") in text
    elif reason is ValueReason.COMMAND:
        assert ("supplied by a command" if locale == "en" else "通过命令取值") in text


@pytest.mark.parametrize("locale", ["en", "zh-Hans"])
def test_incomplete_windows_paths_have_specific_advice(tmp_path, monkeypatch, locale) -> None:
    monkeypatch.setattr(base, "get_platform", lambda: SimpleNamespace(is_windows=True))
    item = discover(tmp_path, "pi", {"command": "run", "cwd": "C:tools"})
    assert item.config is None
    assert (item.field_use, item.reason) == (FieldUse.CWD, ValueReason.WINDOWS_PATH)
    localizer = Localizer(locale)
    text = localizer.render(problem_text(item, render=localizer.render))
    assert r"C:\tools" in text and "C:tools" in text
    assert ("full path" if locale == "en" else "完整路径") in text


def test_first_blocking_field_still_wins(tmp_path) -> None:
    item = discover(
        tmp_path,
        "opencode",
        {
            "type": "local",
            "command": ["{env:COMMAND}"],
            "environment": {"T": "{file:private-source-value}"},
            "cwd": "{file:other}",
        },
    )
    assert item.config is None
    assert item.problem_field == "mcp.srv.command[0]"
    assert (item.field_use, item.reason) == (FieldUse.COMMAND, ValueReason.VARIABLE)


def _unsupported(name: str, problem: Problem, field: str = "") -> McpImportCandidate:
    return McpImportCandidate(name, "/src/config.json", None, problem=problem, problem_field=field)


def test_each_warning_kind_reads_as_one_actionable_sentence() -> None:
    texts = [
        format_message(warning_text(ImportWarning(WarningKind.NOT_IMPORTED, ("a.cwd", "a.x")), "pi")),
        format_message(warning_text(ImportWarning(WarningKind.AUTH_NOT_IMPORTED), "pi")),
        format_message(warning_text(ImportWarning(WarningKind.TOOLS_RESTRICTED), "pi")),
    ]

    assert texts == [
        "Some settings from the original application were not copied. After import, test the connection and check that the features you need are available.",
        "Sign-in from pi cannot be carried over. iCode does not yet support MCP OAuth sign-in. If the server supports token authentication, configure “Headers” as its documentation describes. If it only supports OAuth sign-in, it cannot currently be used in iCode.",
        "The original application's tool restrictions cannot be carried over directly, so this server's tools are initially turned off after import. Choose the tools you need in “Available Tool Scope”.",
    ]


def test_problems_and_source_errors_name_the_setting_or_file() -> None:
    value = replace(_unsupported("x", Problem.VALUE_NOT_REPRESENTABLE, "mcpServers.x.url"), field_use=FieldUse.URL)
    located = McpSourceError("/a.json", SourceErrorKind.UNPARSABLE, line=3, column=7)
    invalid = McpSourceError("/a.json", SourceErrorKind.INVALID, field="mcp.x")

    variable = replace(
        _unsupported("x", Problem.VARIABLE_NOT_SUPPORTED, "mcpServers.x.url"),
        field_use=FieldUse.URL,
        reason=ValueReason.VARIABLE,
    )
    external = replace(
        _unsupported("x", Problem.VALUE_FROM_FILE_OR_COMMAND, "mcp.x.environment.T"),
        field_use=FieldUse.ENV_VALUE,
        reason=ValueReason.FILE,
    )
    sse = replace(_unsupported("x", Problem.UNSUPPORTED_CONNECTION), transport="sse")
    ws = replace(sse, transport="ws")
    remote = _unsupported("x", Problem.REMOTE_ENVIRONMENT, "mcp_servers.x.environment_id")
    assert [format_message(problem_text(c)) for c in (sse, ws, remote)] == [
        (
            "This server connects over SSE; iCode supports only STDIO and Streamable HTTP. "
            "If the server also offers one of these, add it manually."
        ),
        (
            "This server connects over WebSocket; iCode supports only STDIO and Streamable HTTP. "
            "If the server also offers one of these, add it manually."
        ),
        (
            "This configuration accesses the MCP server through a remote environment, which iCode cannot use. "
            "If you have a Streamable HTTP URL accessible from this computer "
            "or a STDIO command that runs here, add the server manually."
        ),
    ]
    # A connection refusal's sentence says it all; the others are introduced by their source setting.
    assert [names_field(c) for c in (sse, remote, variable, external, value)] == [False, False, True, True, True]
    assert [format_message(problem_text(c)) for c in (variable, external, value)] == [
        "The server URL contains an environment variable reference, which iCode does not expand here. Add the server manually and enter its full URL, replacing any environment variable references with their values.",
        "The environment variable value is stored in another file and cannot be imported automatically. Add the server manually and fill in the value using the server instructions. You can also reference an environment variable or get the value from a command.",
        "The server URL cannot be imported as written. Add the server manually and enter its full URL, replacing any environment variable references with their values.",
    ]
    assert format_message(source_error_text(located, "pi")) == (
        "Couldn't parse /a.json: syntax error at line 3, column 7. Correct it, then reopen import."
    )
    assert format_message(source_error_text(invalid, "pi")) == "/a.json has an invalid setting: mcp.x."
