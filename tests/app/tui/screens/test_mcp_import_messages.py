# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Importer causes and localized advice agree without exposing source values."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from chrys.app.tui.screens.agents.import_mcp_view import imported_config, original_setting, problem_text, warning_text
from chrys.foundation.i18n import Localizer
from chrys.service.mcp.importers import (
    FieldUse,
    ImportContext,
    McpImportCandidate,
    Problem,
    ValueReason,
    WarningKind,
    base,
)
from chrys.service.mcp.importers.claude_code import ClaudeCodeSource
from chrys.service.mcp.importers.codex import CodexSource
from chrys.service.mcp.importers.opencode import OpenCodeSource
from chrys.service.mcp.importers.pi import PiSource


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
@pytest.mark.parametrize("locale", ["en", "zh-Hans"])
@pytest.mark.parametrize(
    ("key", "quoted", "reason"),
    [
        ("", '""', ValueReason.EMPTY),
        (" \t", r'" \t"', ValueReason.EMPTY),
        (" TOKEN", '" TOKEN"', ValueReason.WHITESPACE),
        ("TOKEN ", '"TOKEN "', ValueReason.WHITESPACE),
        ("TOKEN\t", r'"TOKEN\t"', ValueReason.WHITESPACE),
        ("\u00a0TOKEN", r'"\u00a0TOKEN"', ValueReason.WHITESPACE),
    ],
)
def test_unpreservable_env_names_are_refused_with_visible_whitespace(
    tmp_path, source, locale, key, quoted, reason
) -> None:
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
    assert (item.field_use, item.reason) == (FieldUse.ENV_NAME, reason)
    assert item.problem_field.endswith(f"[{quoted}]")
    localizer = Localizer(locale)
    text = localizer.render(problem_text(item, source, localizer.render))
    assert ("editor cannot preserve" if locale == "en" else "编辑器无法原样保留") in text
    assert ("original configuration file" if locale == "en" else "原配置文件") in text
    assert ("invalid" if locale == "en" else "非法") not in text
    setting = localizer.render(original_setting(item.problem_field))
    assert f"[{quoted}]" in setting
    assert "private-source-value" not in text + setting
    assert imported_config(item, source, localizer.render) is None


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
    assert ("Original setting:" if locale == "en" else "原设置") in saved.description
    assert warning.items[0] in saved.description
    assert "private-source-value" not in saved.description
    assert ("unsupported" if locale == "en" else "不支持") not in saved.description
    assert ("manually" if locale == "en" else "手动") not in saved.description


@pytest.mark.parametrize("source", ["codex", "claude", "opencode", "pi"])
@pytest.mark.parametrize("locale", ["en", "zh-Hans"])
def test_oauth_advice_is_conditional_and_survives_import(tmp_path, source, locale) -> None:
    entry = (
        'url = "https://example.com"\n'
        if source == "codex"
        else {
            "url": "https://example.com",
            "type": "remote" if source == "opencode" else "http",
            "oauth": {},
        }
    )
    item = discover(tmp_path, source, entry)
    assert [warning.kind for warning in item.warnings] == [WarningKind.AUTH_NOT_IMPORTED]
    localizer = Localizer(locale)
    saved = imported_config(item, source, localizer.render)
    assert saved is not None
    text = localizer.render(warning_text(item.warnings[0], source))
    assert text in saved.description
    if locale == "en":
        assert "does not yet support MCP OAuth" in text
        assert "If the server supports token" in text
        assert "If it only supports OAuth" in text
    else:
        assert "暂不支持 MCP OAuth" in text
        assert "如果服务器支持令牌" in text
        assert "如果仅支持 OAuth" in text


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
            "headers": {"Authorization": "${ANTHROPIC_API_KEY}", "X-Other": "kept"},
            "oauth": {},
        },
    )
    assert claude.config is not None
    assert claude.config.headers == {"X-Other": "kept"}
    assert [w.kind for w in claude.warnings] == [WarningKind.AUTH_HEADERS, WarningKind.AUTH_NOT_IMPORTED]
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
            for warning in item.warnings:
                assert localizer.render(warning_text(warning, "Source")) in saved.description
        text = localizer.render(warning_text(pi.warnings[0], "Pi"))
        assert ("Loading Strategy" if locale == "en" else "加载策略") in text
        assert ("turned off" if locale == "en" else "暂未启用") not in text


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
    text = localizer.render(problem_text(item, source, localizer.render))
    assert "private-source-value" not in text
    assert "private-source-value" not in item.problem_field
    assert ("Original setting:" if locale == "en" else "原设置") not in text
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
    text = localizer.render(problem_text(item, "Pi", localizer.render))
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
