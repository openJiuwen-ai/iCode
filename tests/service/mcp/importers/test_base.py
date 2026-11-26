# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""The shared conversion every importer goes through."""

from __future__ import annotations

import json
import math
import os
import re
import sys
from typing import TYPE_CHECKING, Any
from unittest.mock import patch

import pytest

from chrys.service.mcp.adapter import MCPAdapter
from chrys.service.mcp.importers.base import (
    MAX_REQUEST_TIMEOUT_SECONDS,
    FieldUse,
    ImportContext,
    ImportWarning,
    Problem,
    SourceErrorKind,
    SourceReadError,
    SourceServer,
    ValueReason,
    Var,
    WarningKind,
    by_name,
    convert,
    linked_common_dir,
    main_checkout,
    merge_entries,
    merge_warnings,
    parse_json_object,
    read_text,
    render,
)
from chrys.service.mcp.importers.claude_code import ClaudeCodeSource
from chrys.service.mcp.importers.codex import CodexSource
from chrys.service.mcp.importers.opencode import OpenCodeSource
from chrys.service.mcp.importers.pi import PiSource
from chrys.service.mcp.owned import MCPTool
from chrys.service.mcp.validation import MAX_MCP_EXPOSED_TOOL_NAME_LENGTH, build_prefixed_mcp_name, normalize_mcp_name
from tests.service.mcp._helpers import _FakeConnectionTool, _load_fake_remote_tools, _mcp_remote_tool
from tests.support.symlinks import symlink_or_skip

if TYPE_CHECKING:
    from pathlib import Path

    from chrys.service.mcp.importers.base import McpImportSource


def _stdio(**fields: object) -> SourceServer:
    return SourceServer("srv", "/cfg.json", "stdio", command="run", env_path="servers.srv.env", **fields)


def _http(**fields: object) -> SourceServer:
    return SourceServer("srv", "/cfg.json", "http", url="https://x", headers_path="servers.srv.headers", **fields)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (("plain",), "plain"),
        (("Bearer ", Var("TOKEN")), "Bearer {{TOKEN}}"),
        (("{{literal}} ", Var("V")), "{{{{literal}}}} {{V}}"),
        (("{{", Var("V")), "{{{{{{V}}"),
        (("{", Var("V")), None),
        ((Var("V"), "}}"), None),
    ],
    ids=["literal", "variable", "escaped-braces", "even-braces-before", "odd-brace-before", "braces-after"],
)
def test_render_escapes_literal_braces_and_refuses_ambiguous_ones(value: tuple, expected: str | None) -> None:
    assert render(value) == expected


def test_convert_builds_the_server_with_a_tool_prefix_and_rounded_timeout() -> None:
    server = _stdio(
        args=("-y", "pkg"),
        env={"TOKEN": ("x-", Var("SECRET"))},
        enabled=False,
        description="from source",
        timeout_seconds=1.2,
        allowed_tools=("a",),
        progressive=True,
        always_load=("a",),
        cwd=(Var("ROOT"), "/server"),
        cwd_path="servers.srv.cwd",
    )

    candidate = convert(server)

    assert candidate.problem is None
    config = candidate.config
    assert config is not None
    assert (config.command, config.args, config.env) == ("run", ["-y", "pkg"], {"TOKEN": "x-{{SECRET}}"})
    assert (config.enabled, config.description, config.request_timeout) == (False, "from source", 2)
    assert (config.allowed_tools, config.use_progressive_disclosure, config.always_load) == (["a"], True, ["a"])
    assert config.tool_name_prefix == "srv"
    assert config.cwd == "{{ROOT}}/server"


def test_convert_caps_an_unbounded_timeout() -> None:
    config = convert(_stdio(timeout_seconds=math.inf)).config
    assert config is not None
    assert config.request_timeout == MAX_REQUEST_TIMEOUT_SECONDS


@pytest.mark.parametrize("line_ending", ["\n", "\r\n", "\r"], ids=["lf", "crlf", "cr"])
def test_convert_preserves_uniform_command_line_endings(line_ending: str) -> None:
    command = f"run{line_ending}server"
    args = (f"a{line_ending}b", line_ending, "", r"literal\r\n")

    candidate = convert(SourceServer("srv", "/cfg.json", "stdio", command=command, args=args))

    assert candidate.config is not None
    assert (candidate.config.command, candidate.config.args) == (command, list(args))


@pytest.mark.parametrize(
    ("command", "args", "field", "use"),
    [
        ("run", ("a\rb\nc",), "args[0]", FieldUse.ARGUMENT),
        ("run", ("a\r\nb\nc",), "args[0]", FieldUse.ARGUMENT),
        ("run", ("a\rb", "c\nd"), "args[0]", FieldUse.ARGUMENT),
        ("run", ("a\r\nb", "c\nd"), "args[1]", FieldUse.ARGUMENT),
        ("run\rserver", ("a\nb",), "command", FieldUse.COMMAND),
    ],
    ids=["mixed-cr-lf", "mixed-crlf-lf", "across-args-cr-lf", "across-args-crlf-lf", "command-and-args"],
)
def test_convert_refuses_the_command_part_whose_line_endings_would_change(
    command: str, args: tuple[str, ...], field: str, use: FieldUse
) -> None:
    server = SourceServer(
        "srv",
        "/cfg.json",
        "stdio",
        command=command,
        args=args,
        command_path="servers.srv.command",
        arg_paths=tuple(f"servers.srv.args[{index}]" for index in range(len(args))),
    )

    candidate = convert(server)

    assert candidate.config is None
    assert candidate.problem is Problem.VALUE_NOT_REPRESENTABLE
    assert candidate.reason is ValueReason.LINE_ENDINGS
    assert candidate.field_use is use
    assert candidate.problem_field == f"servers.srv.{field}"


@pytest.mark.parametrize("separator", ["\v", "\f", "\x1c", "\x1d", "\x1e", "\x85", "\u2028", "\u2029"])
def test_convert_refuses_argument_separators_the_command_editor_rewrites(separator: str) -> None:
    candidate = convert(_stdio(args=(f"a{separator}b",), arg_paths=("servers.srv.args[0]",)))

    assert candidate.config is None
    assert candidate.problem is Problem.VALUE_NOT_REPRESENTABLE
    assert candidate.reason is ValueReason.LINE_ENDINGS
    assert candidate.field_use is FieldUse.ARGUMENT
    assert candidate.problem_field == "servers.srv.args[0]"


@pytest.mark.parametrize("field", ["allowed_tools", "always_load"])
@pytest.mark.parametrize(
    ("names", "reason"),
    [
        (("read,delete",), ValueReason.CHARACTERS),
        ((" read",), ValueReason.WHITESPACE),
        (("read ",), ValueReason.WHITESPACE),
        (("",), ValueReason.EMPTY),
        ((" \t",), ValueReason.EMPTY),
    ],
)
def test_convert_refuses_tool_lists_the_editor_cannot_preserve(field, names, reason) -> None:
    server = _stdio(**{field: names, f"{field}_path": "servers.srv.tool_policy"})

    candidate = convert(server)

    assert candidate.config is None
    assert candidate.problem is Problem.VALUE_NOT_REPRESENTABLE
    assert candidate.problem_field == "servers.srv.tool_policy"
    assert candidate.reason is reason


@pytest.mark.parametrize("allowed", [None, (), ("read", "delete"), ("read file", "工具")])
def test_convert_keeps_representable_tool_scope(allowed) -> None:
    config = convert(_stdio(allowed_tools=allowed)).config

    assert config is not None
    assert config.allowed_tools == (None if allowed is None else list(allowed))


@pytest.mark.parametrize(
    ("key", "reason"),
    [
        ("", ValueReason.EMPTY),
        (" \t", ValueReason.EMPTY),
        (" TOKEN", ValueReason.WHITESPACE),
        ("TOKEN ", ValueReason.WHITESPACE),
        ("\tTOKEN", ValueReason.WHITESPACE),
        ("TOKEN\t", ValueReason.WHITESPACE),
        ("\u00a0TOKEN", ValueReason.WHITESPACE),
        ("TOKEN\u3000", ValueReason.WHITESPACE),
    ],
)
def test_convert_refuses_env_names_the_editor_cannot_preserve(key: str, reason: ValueReason) -> None:
    candidate = convert(_stdio(env={"TOKEN": ("original",), key: ("private-source-value",)}))

    assert candidate.config is None
    assert candidate.problem is Problem.VALUE_NOT_REPRESENTABLE
    assert candidate.field_use is FieldUse.ENV_NAME
    assert candidate.reason is reason
    assert candidate.problem_field.startswith("servers.srv.env[")
    assert json.loads(candidate.problem_field.removeprefix("servers.srv.env[").removesuffix("]")) == key
    assert "private-source-value" not in candidate.problem_field


@pytest.mark.parametrize("key", ["TOKEN", "A B", "A-B"])
def test_convert_keeps_representable_env_names(key: str) -> None:
    config = convert(_stdio(env={key: ("value",)})).config

    assert config is not None
    assert config.env == {key: "value"}


@pytest.mark.parametrize(
    ("server", "problem", "field"),
    [
        (SourceServer("srv", "/cfg.json", "sse"), Problem.UNSUPPORTED_CONNECTION, ""),
        (_http(blocked_field="servers.srv.url"), Problem.VALUE_NOT_REPRESENTABLE, "servers.srv.url"),
        (
            _http(blocked_field="servers.srv.url", blocked_problem=Problem.VARIABLE_NOT_SUPPORTED),
            Problem.VARIABLE_NOT_SUPPORTED,
            "servers.srv.url",
        ),
        (_stdio(env={"A": ("",)}), Problem.VALUE_NOT_REPRESENTABLE, "servers.srv.env.A"),
        (_stdio(env={"A": (" padded ",)}), Problem.VALUE_NOT_REPRESENTABLE, "servers.srv.env.A"),
        (_stdio(env={"A": ("{", Var("V"))}), Problem.VALUE_NOT_REPRESENTABLE, "servers.srv.env.A"),
        (_http(headers={"X": ("  ",)}), Problem.VALUE_NOT_REPRESENTABLE, "servers.srv.headers.X"),
        (_http(headers={"X": ("café",)}), Problem.VALUE_NOT_REPRESENTABLE, "servers.srv.headers.X"),
        (_http(headers={"X": ("a\x01b",)}), Problem.VALUE_NOT_REPRESENTABLE, "servers.srv.headers.X"),
        (_http(headers={"X": ("a\r\nb",)}), Problem.VALUE_NOT_REPRESENTABLE, "servers.srv.headers.X"),
        (_http(headers={"X Key": ("v",)}), Problem.VALUE_NOT_REPRESENTABLE, "servers.srv.headers.X Key"),
    ],
    ids=[
        "transport",
        "blocked",
        "blocked-with-reason",
        "empty-env",
        "env-whitespace",
        "env-brace",
        "empty-header",
        "non-ascii-header",
        "control-character-header",
        "line-break-in-header",
        "invalid-header-name",
    ],
)
def test_convert_refuses_values_icode_cannot_hold(server: SourceServer, problem: Problem, field: str) -> None:
    candidate = convert(server)

    assert (candidate.config, candidate.problem, candidate.problem_field) == (None, problem, field)
    assert candidate.transport == ("sse" if problem is Problem.UNSUPPORTED_CONNECTION else "")


@pytest.mark.parametrize("command", ["", " ", "\r\n"], ids=["empty", "space", "line-break"])
def test_convert_refuses_a_stdio_command_with_nothing_to_run(command: str) -> None:
    candidate = convert(SourceServer("srv", "/cfg.json", "stdio", command=command, command_path="servers.srv.command"))

    assert (candidate.config, candidate.problem) == (None, Problem.VALUE_NOT_REPRESENTABLE)
    assert (candidate.problem_field, candidate.reason, candidate.field_use) == (
        "servers.srv.command",
        ValueReason.EMPTY,
        FieldUse.COMMAND,
    )


@pytest.mark.parametrize(
    ("server", "field", "use"),
    [
        (SourceServer("s\x1brv", "/cfg.json", "stdio", command="run"), "", FieldUse.NAME),
        (
            SourceServer("srv", "/cfg.json", "stdio", command="r\x1b[2Jun", command_path="servers.srv.command"),
            "servers.srv.command",
            FieldUse.COMMAND,
        ),
        (
            _stdio(args=("-x", "a\x00b"), arg_paths=("servers.srv.args[0]", "servers.srv.args[1]")),
            "servers.srv.args[1]",
            FieldUse.ARGUMENT,
        ),
        (_stdio(env={"A\x07": ("v",)}), 'servers.srv.env["A\\u0007"]', FieldUse.ENV_NAME),
        (_stdio(env={"A": ("a\x9bb",)}), "servers.srv.env.A", FieldUse.ENV_VALUE),
        (
            SourceServer("srv", "/cfg.json", "http", url="https://x/\x1b]8;;", url_path="servers.srv.url"),
            "servers.srv.url",
            FieldUse.URL,
        ),
        (
            _stdio(allowed_tools=("re\x7fad",), allowed_tools_path="servers.srv.tools"),
            "servers.srv.tools",
            FieldUse.VALUE,
        ),
    ],
    ids=["name", "command", "argument", "env-name", "env-value-c1", "url", "tool-name"],
)
def test_convert_refuses_control_characters_the_fields_would_draw_raw(
    server: SourceServer, field: str, use: FieldUse
) -> None:
    candidate = convert(server)

    assert (candidate.config, candidate.problem) == (None, Problem.VALUE_NOT_REPRESENTABLE)
    assert (candidate.problem_field, candidate.reason, candidate.field_use) == (field, ValueReason.CHARACTERS, use)


@pytest.mark.parametrize("name", ["", " \t "])
def test_convert_refuses_names_that_the_editor_would_treat_as_empty(name: str) -> None:
    candidate = convert(SourceServer(name, "/cfg.json", "stdio", command="run"))

    assert candidate.config is None
    assert (candidate.problem, candidate.field_use, candidate.reason) == (
        Problem.VALUE_NOT_REPRESENTABLE,
        FieldUse.NAME,
        ValueReason.EMPTY,
    )


@pytest.mark.parametrize("url", ["https://a\n.example/x", "https://a/\tx", "https://a/\rx"], ids=["lf", "tab", "cr"])
def test_convert_refuses_a_line_break_or_tab_within_a_url(url: str) -> None:
    candidate = convert(SourceServer("srv", "/cfg.json", "http", url=url, url_path="servers.srv.url"))

    assert (candidate.config, candidate.problem_field, candidate.reason) == (
        None,
        "servers.srv.url",
        ValueReason.CHARACTERS,
    )


@pytest.mark.parametrize(
    ("url", "refused"),
    [
        ("https://x/team/%2e%2e/mcp", True),
        ("https://x/team/%2E/mcp", True),
        ("https://x/team/.%2E/mcp", True),
        ("https://x/team/%2e./mcp", True),
        ("https://x/team/report%2ejson", False),
        ("https://x/team?next=/%2e%2e/mcp", False),
        ("https://x/team/../mcp", False),
    ],
    ids=["double", "single", "mixed", "trailing-dot", "inside-segment", "query", "literal"],
)
def test_convert_refuses_encoded_dot_segments_in_the_url_path(url: str, refused: bool) -> None:
    # The other applications' URL parsers resolve them; iCode's HTTP client keeps them.
    candidate = convert(SourceServer("srv", "/cfg.json", "http", url=url, url_path="servers.srv.url"))

    if refused:
        assert (candidate.config, candidate.problem, candidate.problem_field, candidate.field_use) == (
            None,
            Problem.VALUE_NOT_REPRESENTABLE,
            "servers.srv.url",
            FieldUse.URL,
        )
    else:
        assert candidate.config is not None
        assert candidate.config.url == url


def test_convert_keeps_tabs_and_line_breaks_and_draws_the_description_safely() -> None:
    candidate = convert(_stdio(args=("a\tb", "line\nbreak"), description="note\x1b[31m\r\nmore\tend"))

    assert candidate.config is not None
    assert candidate.config.args == ["a\tb", "line\nbreak"]
    assert "\x1b" not in candidate.config.description and "\r" not in candidate.config.description
    assert candidate.config.description.endswith("\nmore\tend")


def test_convert_trims_header_whitespace_and_ignores_env_of_http_servers() -> None:
    # As fetch trims them, which the other applications send with.
    config = convert(_http(headers={"X": (" v\t",), "Y": ("w\r\n",)}, env={" IGNORED ": ("",)})).config

    assert config is not None
    assert (config.headers, config.env) == ({"X": "v", "Y": "w"}, {})


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://x/mcp", "https://x/mcp"),
        ("  https://x/mcp", "https://x/mcp"),
        ("https://x/mcp  ", "https://x/mcp"),
        ("\t\r\nhttps://x/mcp\r\n\t", "https://x/mcp"),
        ("\u00a0https://x/mcp\u3000", "https://x/mcp"),
        (" https://x/a b?key=a b ", "https://x/a b?key=a b"),
        (" https://x/mcp%20?key=%20 ", "https://x/mcp%20?key=%20"),
        (" HTTPS://X/Path?Key=HTTP:Value ", "https://X/Path?Key=HTTP:Value"),
    ],
    ids=[
        "unchanged",
        "leading",
        "trailing",
        "ascii-whitespace",
        "unicode-whitespace",
        "internal",
        "encoded",
        "scheme-case",
    ],
)
def test_convert_trims_only_surrounding_url_whitespace(url: str, expected: str) -> None:
    candidate = convert(SourceServer("srv", "/cfg.json", "http", url=url))

    assert candidate.problem is None
    assert candidate.config is not None
    assert candidate.config.url == expected


def test_convert_keeps_only_the_settings_of_the_servers_own_transport() -> None:
    http = convert(_http(command="run", args=("-x",), cwd=("bad\0path",))).config
    stdio = convert(_stdio(args=("-x",), url="https://x")).config

    assert http is not None and stdio is not None
    assert (http.command, http.args, http.url) == ("", [], "https://x")
    assert http.cwd == ""
    assert (stdio.command, stdio.args, stdio.url) == ("run", ["-x"], "")


def test_merge_entries_merges_nesting_deeper_than_the_recursion_limit() -> None:
    base = {"a": {"b": 1}}
    deep: dict[str, Any] = {}
    for _ in range(sys.getrecursionlimit() * 2):
        deep = {"a": deep}

    merged = merge_entries(base, deep)

    assert merged["a"]["b"] == 1
    assert merged["a"]["a"] is deep["a"]["a"]
    assert base == {"a": {"b": 1}}


def test_read_text_reads_utf8_only(tmp_path: Path) -> None:
    bom, gbk = tmp_path / "bom.json", tmp_path / "gbk.json"
    bom.write_bytes(b'\xef\xbb\xbf{"a": 1}')
    gbk.write_bytes('{"a": "中文"}'.encode("gbk"))

    assert read_text(bom) == '{"a": 1}'
    # Guessing another encoding would import other text than the source application reads.
    with pytest.raises(SourceReadError) as caught:
        read_text(gbk)
    assert caught.value.kind is SourceErrorKind.UNPARSABLE


def test_warnings_merge_one_per_kind_with_each_item_once() -> None:
    merged = merge_warnings(
        [
            ImportWarning(WarningKind.NOT_IMPORTED, ("a", "b")),
            ImportWarning(WarningKind.AUTH_NOT_IMPORTED),
            ImportWarning(WarningKind.NOT_IMPORTED, ("b", "c")),
        ]
    )

    assert merged == (
        ImportWarning(WarningKind.NOT_IMPORTED, ("a", "b", "c")),
        ImportWarning(WarningKind.AUTH_NOT_IMPORTED),
    )


@pytest.mark.parametrize(
    ("name", "prefix"),
    [
        ("github", r"github"),
        (" github ", r"github"),
        ("\tgithub\t", r"github"),
        ("\u00a0github\u00a0", r"github"),
        ("my server.v2", r"my_server_v2-[0-9a-f]{6}"),
        ("github-", r"github-[0-9a-f]{6}"),
        ("--", r"mcp_[0-9a-f]{6}"),
        ("a" * 40, r"a{17}-[0-9a-f]{6}"),
    ],
    ids=[
        "plain",
        "surrounding-spaces",
        "surrounding-tabs",
        "surrounding-nonbreaking-spaces",
        "unsafe-characters",
        "trimmed",
        "nothing-left",
        "too-long",
    ],
)
def test_prefix_is_made_from_the_server_name(name: str, prefix: str) -> None:
    config = convert(SourceServer(name, "/cfg.json", "stdio", command="run")).config

    assert config is not None
    assert config.name == name.strip()
    assert re.fullmatch(prefix, config.tool_name_prefix)


@pytest.mark.parametrize(
    "names",
    [("a" * 30 + "1", "a" * 30 + "2"), ("github", "github-"), ("docs.mcp", "docs_mcp")],
    ids=["same-start", "trimmed", "replaced"],
)
def test_names_that_end_up_alike_get_different_prefixes(names: tuple[str, str]) -> None:
    configs = [convert(SourceServer(name, "/c", "stdio", command="run")).config for name in names]

    assert all(configs)
    assert len({config.tool_name_prefix for config in configs if config}) == 2


@pytest.mark.parametrize(
    ("fields", "prefix"),
    [
        ({"allowed_tools": ("read", "filesystem_read")}, r"files"),
        ({"allowed_tools": ("files_read",)}, r"files-[0-9a-f]{6}"),
        ({"progressive": True, "always_load": ("files_read",)}, r"files-[0-9a-f]{6}"),
        ({"allowed_tools": ("files",)}, r"files-[0-9a-f]{6}"),
        ({"allowed_tools": ("files_" + "x" * 46,)}, r"file-[0-9a-f]{6}"),
        ({"allowed_tools": ("x" * 60,)}, r""),
        # iCode drops the leading ``_`` from the exposed name, so the plain prefix fits.
        ({"allowed_tools": ("__" + "a" * 58,)}, r"files"),
    ],
    ids=[
        "unrelated-names",
        "allowed-name-starts-with-prefix",
        "always-load-only",
        "allowed-name-is-prefix",
        "shortened-to-fit-a-long-name",
        "no-prefix-fits",
        "leading-underscores-dropped",
    ],
)
def test_a_prefix_never_spells_a_configured_tool_name_and_keeps_it_within_the_limit(
    fields: dict[str, Any], prefix: str
) -> None:
    # iCode would also read the source's remote name ``files_read`` as the prefixed name of a tool ``read``,
    # and a remote name made only of ``_``/``-`` as the prefix itself.
    config = convert(SourceServer("files", "/cfg.json", "stdio", command="run", **fields)).config

    assert config is not None
    assert re.fullmatch(prefix, config.tool_name_prefix)
    named = (*(config.allowed_tools or ()), *config.always_load)
    exposed = [build_prefixed_mcp_name(normalize_mcp_name(name), config.tool_name_prefix) for name in named]
    assert all(len(name) <= MAX_MCP_EXPOSED_TOOL_NAME_LENGTH for name in exposed)


def test_a_hashed_prefix_that_spells_a_configured_tool_name_is_replaced() -> None:
    first = convert(SourceServer("my server", "/c", "stdio", command="run")).config
    assert first is not None
    allowed = (f"{first.tool_name_prefix}_read",)

    config = convert(SourceServer("my server", "/c", "stdio", command="run", allowed_tools=allowed)).config

    assert config is not None
    assert config.tool_name_prefix != first.tool_name_prefix
    assert re.fullmatch(r"my_server-[0-9a-f]{6}", config.tool_name_prefix)


async def test_an_imported_allowlist_connects_with_only_the_remote_tool_it_names() -> None:
    # A ``files`` server allowing only ``files_read``: its remote ``read`` must not pass as that name.
    config = convert(SourceServer("files", "/cfg.json", "stdio", command="run", allowed_tools=("files_read",))).config
    assert config is not None
    tool = MCPTool(name=config.name, tool_name_prefix=config.tool_name_prefix, allowed_tools=config.allowed_tools)
    await _load_fake_remote_tools(tool, _mcp_remote_tool("read"), _mcp_remote_tool("files_read"))
    adapter = MCPAdapter()
    try:
        fake = _FakeConnectionTool(functions=tool.functions)
        with patch("chrys.service.mcp._connection._create_mcp_tool", return_value=fake):
            tools = await adapter.connect(config)

        assert [tool.name for tool in tools] == [f"{config.tool_name_prefix}_files_read"]
    finally:
        await adapter.disconnect_all()


_RELATIVE_SOURCES = [
    (ClaudeCodeSource, "CLAUDE_CONFIG_DIR", ".claude.json", '{"mcpServers": {"NAME": {"command": "run"}}}'),
    (CodexSource, "CODEX_HOME", "config.toml", '[mcp_servers.NAME]\ncommand = "run"\n'),
    (PiSource, "PI_CODING_AGENT_DIR", "mcp.json", '{"mcpServers": {"NAME": {"command": "run"}}}'),
    (
        OpenCodeSource,
        "XDG_CONFIG_HOME",
        "opencode/opencode.json",
        '{"mcp": {"NAME": {"type": "local", "command": ["run"]}}}',
    ),
]


@pytest.mark.parametrize(
    ("source", "variable", "file", "text"), _RELATIVE_SOURCES, ids=["claude-code", "codex", "pi", "opencode"]
)
def test_a_relative_folder_variable_is_found_from_the_workspace_never_the_process_folder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, source: type[McpImportSource], variable: str, file: str, text: str
) -> None:
    for folder in ("process", "ws"):
        (tmp_path / folder / "rel" / file).parent.mkdir(parents=True)
        (tmp_path / folder / "rel" / file).write_text(text.replace("NAME", folder), encoding="utf-8")
    monkeypatch.chdir(tmp_path / "process")
    ctx = ImportContext(str(tmp_path / "ws"), tmp_path / "home", {variable: "rel"})

    result = source().discover(ctx)

    assert source().detect(ctx)
    assert [candidate.name for candidate in result.candidates] == ["ws"]
    assert result.source_errors == ()


def test_a_later_same_named_server_replaces_the_whole_earlier_one() -> None:
    low = _stdio(env={"A": ("1",)})
    other = SourceServer("other", "/cfg.json", "stdio", command="x")
    high = SourceServer("srv", "/p.json", "stdio", command="y")

    assert by_name([low, other, high]) == [other, high]


@pytest.mark.parametrize("text", ["", "  \n"], ids=["empty", "blank"])
def test_parse_json_object_reads_blank_files(text: str) -> None:
    assert parse_json_object(text) == {}


def test_a_bom_left_after_read_text_skipped_one_is_unparsable() -> None:
    with pytest.raises(SourceReadError) as caught:
        parse_json_object('\ufeff{"a": 1}')

    assert caught.value.kind is SourceErrorKind.UNPARSABLE


def test_parse_json_object_reports_where_it_failed() -> None:
    with pytest.raises(SourceReadError) as caught:
        parse_json_object('{\n  "a": ,\n}')

    assert (caught.value.kind, caught.value.line, caught.value.column) == (SourceErrorKind.UNPARSABLE, 2, 8)


def test_parse_json_object_refuses_a_document_that_is_not_an_object() -> None:
    with pytest.raises(SourceReadError) as caught:
        parse_json_object("[]")

    assert caught.value.kind is SourceErrorKind.INVALID


def test_a_commondir_reaching_its_folder_by_another_name_is_not_followed(tmp_path: Path) -> None:
    owner, worktree = tmp_path / "owner", tmp_path / "wt"
    admin = owner / ".git" / "worktrees" / "wt"
    admin.mkdir(parents=True)
    worktree.mkdir()
    (owner / "child").mkdir()
    (tmp_path / "trusted").mkdir()
    # Through links (where they resolve before ".."), commondir names owner/.git; spelled out, tmp/trusted.
    symlink_or_skip(tmp_path / "link", owner / "child", target_is_directory=True)
    symlink_or_skip(owner / "trusted", owner / ".git", target_is_directory=True)
    (admin / "commondir").write_text(f"{tmp_path / 'link' / '..' / 'trusted'}\n", encoding="utf-8")
    (admin / "gitdir").write_text(f"{worktree / '.git'}\n", encoding="utf-8")
    (worktree / ".git").write_text(f"gitdir: {admin}\n", encoding="utf-8")

    assert (linked_common_dir(str(worktree)), main_checkout(str(worktree))) == (None, None)


def test_a_git_file_reaching_the_worktrees_folder_by_another_name_is_not_followed(tmp_path: Path) -> None:
    repo, worktree = tmp_path / "repo", tmp_path / "wt"
    admin = repo / ".git" / "worktrees" / "wt"
    admin.mkdir(parents=True)
    worktree.mkdir()
    (admin / "commondir").write_text("../..\n", encoding="utf-8")
    (admin / "gitdir").write_text(f"{worktree / '.git'}\n", encoding="utf-8")
    # Spelled through the link, "../.." from the worktree's Git folder names repo, not repo/.git.
    symlink_or_skip(repo / "alias", repo / ".git" / "worktrees", target_is_directory=True)
    (worktree / ".git").write_text(f"gitdir: {repo / 'alias' / 'wt'}\n", encoding="utf-8")

    assert (linked_common_dir(str(worktree)), main_checkout(str(worktree))) == (None, None)
    # The same worktree, its Git file as Git writes it.
    (worktree / ".git").write_text(f"gitdir: {admin}\n", encoding="utf-8")
    assert (linked_common_dir(str(worktree)), main_checkout(str(worktree))) == (str(repo / ".git"), str(repo))


@pytest.mark.parametrize("planted", ["oversized", "link", "fifo"])
def test_a_git_file_git_would_not_write_is_not_followed(tmp_path: Path, planted: str) -> None:
    repo, worktree = tmp_path / "repo", tmp_path / "wt"
    admin = repo / ".git" / "worktrees" / "wt"
    admin.mkdir(parents=True)
    worktree.mkdir()
    (admin / "commondir").write_text("../..\n", encoding="utf-8")
    (admin / "gitdir").write_text(f"{worktree / '.git'}\n", encoding="utf-8")
    pointer = f"gitdir: {admin}\n"
    if planted == "oversized":
        # Valid once stripped: only the size limit refuses it.
        (worktree / ".git").write_text(pointer + " " * (64 * 1024), encoding="utf-8")
    elif planted == "link":
        (tmp_path / "pointer").write_text(pointer, encoding="utf-8")
        symlink_or_skip(worktree / ".git", tmp_path / "pointer")
    else:
        if sys.platform == "win32":
            pytest.skip("FIFOs are POSIX only")
        # Opened for reading, a FIFO with no writer would block discovery.
        os.mkfifo(worktree / ".git")

    assert (linked_common_dir(str(worktree)), main_checkout(str(worktree))) == (None, None)
