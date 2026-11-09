# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""The shared conversion every importer goes through."""

from __future__ import annotations

import math
import re
import sys
from typing import TYPE_CHECKING, Any

import pytest

from chrys.service.mcp.importers.base import (
    MAX_REQUEST_TIMEOUT_SECONDS,
    ImportWarning,
    Problem,
    SourceErrorKind,
    SourceReadError,
    SourceServer,
    Var,
    WarningKind,
    by_name,
    convert,
    merge_entries,
    merge_warnings,
    parse_json_object,
    read_text,
    render,
)

if TYPE_CHECKING:
    from pathlib import Path


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
    )

    candidate = convert(server)

    assert candidate.problem is None
    config = candidate.config
    assert config is not None
    assert (config.command, config.args, config.env) == ("run", ["-y", "pkg"], {"TOKEN": "x-{{SECRET}}"})
    assert (config.enabled, config.description, config.request_timeout) == (False, "from source", 2)
    assert (config.allowed_tools, config.use_progressive_disclosure, config.always_load) == (["a"], True, ["a"])
    assert config.tool_name_prefix == "srv"


def test_convert_caps_an_unbounded_timeout() -> None:
    config = convert(_stdio(timeout_seconds=math.inf)).config
    assert config is not None
    assert config.request_timeout == MAX_REQUEST_TIMEOUT_SECONDS


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


def test_convert_trims_header_whitespace_and_ignores_env_of_http_servers() -> None:
    # As fetch trims them, which the other applications send with.
    config = convert(_http(headers={"X": (" v\t",), "Y": ("w\r\n",)}, env={"IGNORED": ("",)})).config

    assert config is not None
    assert (config.headers, config.env) == ({"X": "v", "Y": "w"}, {})


def test_convert_keeps_only_the_settings_of_the_servers_own_transport() -> None:
    http = convert(_http(command="run", args=("-x",))).config
    stdio = convert(_stdio(args=("-x",), url="https://x")).config

    assert http is not None and stdio is not None
    assert (http.command, http.args, http.url) == ("", [], "https://x")
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
        ("my server.v2", r"my_server_v2-[0-9a-f]{6}"),
        ("github-", r"github-[0-9a-f]{6}"),
        ("--", r"mcp_[0-9a-f]{6}"),
        ("a" * 40, r"a{17}-[0-9a-f]{6}"),
    ],
    ids=["plain", "unsafe-characters", "trimmed", "nothing-left", "too-long"],
)
def test_prefix_is_made_from_the_server_name(name: str, prefix: str) -> None:
    config = convert(SourceServer(name, "/cfg.json", "stdio", command="run")).config

    assert config is not None
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


def test_a_later_same_named_server_replaces_the_whole_earlier_one() -> None:
    low = _stdio(env={"A": ("1",)})
    other = SourceServer("other", "/cfg.json", "stdio", command="x")
    high = SourceServer("srv", "/p.json", "stdio", command="y")

    assert by_name([low, other, high]) == [other, high]


@pytest.mark.parametrize(
    ("text", "expected"),
    [("", {}), ("  \n", {}), ('﻿{"a": 1}', {"a": 1})],
    ids=["empty", "blank", "bom"],
)
def test_parse_json_object_reads_blank_and_bom_files(text: str, expected: dict) -> None:
    assert parse_json_object(text) == expected


def test_parse_json_object_reports_where_it_failed() -> None:
    with pytest.raises(SourceReadError) as caught:
        parse_json_object('{\n  "a": ,\n}')

    assert (caught.value.kind, caught.value.line, caught.value.column) == (SourceErrorKind.UNPARSABLE, 2, 8)


def test_parse_json_object_refuses_a_document_that_is_not_an_object() -> None:
    with pytest.raises(SourceReadError) as caught:
        parse_json_object("[]")

    assert caught.value.kind is SourceErrorKind.INVALID
