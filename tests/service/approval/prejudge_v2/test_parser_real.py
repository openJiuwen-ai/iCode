# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Run the actual Bash grammar when the optional feature is installed."""

from __future__ import annotations

import importlib.util
import os

import pytest

from chrys.service.approval.prejudge_v2 import Route, analyze, load_policy, route

# These tests are a release gate, not an optional smoke test, on CI.
# Local development without native parser packages may still skip the module.
if os.environ.get("CI") and (
    importlib.util.find_spec("tree_sitter") is None or importlib.util.find_spec("tree_sitter_bash") is None
):
    raise RuntimeError("CI requires the pinned Tree-sitter dependencies; real-parser tests must not skip")
pytest.importorskip("tree_sitter")
pytest.importorskip("tree_sitter_bash")


@pytest.mark.parametrize(
    "cmd",
    [
        "git status",
        "git status && echo ok",
        "git status | wc -l",
        "git status; echo ok",
        "git 'status'",
        "g''it status",
        "{ git status; echo ok; }",
        "(git status; echo ok)",
    ],
)
def test_real_ast_never_mistakes_unknown_syntax_for_full_coverage(cmd):
    result = analyze(cmd, dialect="bash")
    assert result.syntax_valid
    assert result.executable_nodes
    assert result.coverage_complete
    assert all(node.argv is not None for node in result.executable_nodes)


@pytest.mark.parametrize(
    "cmd",
    [
        "rm -rf fixture 2>/dev/null",
        "FOO=1 rm fixture",
        "if true; then rm fixture; fi",
        "echo $(rm fixture)",
        'rm "$TARGET"',
        "git status > out.txt",
        "{rm,-rf,fixture}",
        "r? fixture",
        "command rm fixture",
        "env rm fixture",
        "time rm fixture",
    ],
)
def test_unsupported_commands_cannot_be_fully_allowed(cmd):
    result = analyze(cmd, dialect="bash")
    assert not result.coverage_complete


@pytest.mark.parametrize("dialect", ["zsh", "pwsh", "cmd", "dash"])
def test_unsupported_shell_dialect_is_explicit_defer(dialect):
    result = analyze("git status", dialect=dialect)
    assert "unsupported_dialect" in result.unsupported
    assert not result.coverage_complete


@pytest.mark.parametrize(
    "cmd",
    [
        "rm -rf fixture 2>/dev/null",
        "FOO=1 rm fixture",
        "if true; then rm fixture; fi",
        "echo $(rm fixture)",
        "! rm fixture",
        "export A=$(rm fixture)",
    ],
)
def test_nested_executables_do_not_allow_unsupported_syntax(cmd):
    from chrys.service.approval.prejudge_v2 import Matcher, Policy, Rule

    result = analyze(cmd, dialect="bash")
    assert not result.coverage_complete
    policy = Policy(
        "enforce",
        "nested",
        tuple(Rule(name, Route.ALLOW, Matcher("argv_first", (name,))) for name in ("rm", "echo", "true", "export")),
    )
    assert route(cmd, "bash", policy).route is Route.DEFER


def test_yaml_matchers_with_real_bash_ast(tmp_path):
    """Do not mock either the parser or the policy loader."""
    file = tmp_path / "prejudge.yaml"
    file.write_text(
        'schema_version: 1\nmode: "enforce"\npolicy_id: smoke\n'
        "rules:\n"
        "  - id: allow-status\n    effect: allow\n    target: command\n"
        "    match:\n      argv_exact: [git, status]\n"
        "  - id: allow-echo\n    effect: allow\n    target: command\n"
        "    match:\n      argv_first: [echo]\n",
        encoding="utf-8",
    )
    policy = load_policy(file)
    cases = (
        ("git status", Route.ALLOW),
        ("rm fixture", Route.DEFER),
        ('echo "rm"', Route.ALLOW),
        ("git status && rm fixture", Route.DEFER),
        ("git status && unknown_tool", Route.DEFER),
        ('echo "$(rm fixture)"', Route.DEFER),
        ('rm "$TARGET"', Route.DEFER),
        ("git status > out.txt", Route.DEFER),
        ("/bin/rm -rf fixture", Route.DEFER),
        ("command rm fixture", Route.DEFER),
    )
    for command, expected in cases:
        result = route(command, "bash", policy)
        assert result.route is expected, (command, expected, result)


@pytest.mark.parametrize(
    ("command", "argv"),
    [
        ("git\\\nstatus", ("git", "status")),
        ('rm -f "pyproject."*', ("rm", "-f", "pyproject.*")),
        ("rm -f {a,b}.txt", ("rm", "-f", "{a,b}.txt")),
        ("echo ~", ("echo", "~")),
    ],
)
def test_expansions_never_mistaken_for_static_exact_argv(command, argv):
    from chrys.service.approval.prejudge_v2 import Matcher, Policy, Rule

    # Structured matching must not confuse shell expansion with literal argv.
    policy = Policy("enforce", "regression", (Rule("allow", Route.ALLOW, Matcher("argv_exact", argv)),))
    assert route(command, "bash", policy).route is not Route.ALLOW


@pytest.mark.parametrize("command", ["$'rm' x", r"$'\x72m' x"])
def test_ansi_c_literals_match_complete_argv(command):
    from chrys.service.approval.prejudge_v2 import Matcher, Policy, Rule

    policy = Policy(
        "enforce",
        "regression",
        (Rule("allow-rm", Route.ALLOW, Matcher("argv_exact", ("rm", "x"))),),
    )
    analysis = analyze(command)
    assert analysis.syntax_valid
    assert analysis.coverage_complete
    assert analysis.executable_nodes[0].argv == ("rm", "x")
    result = route(command, "bash", policy)
    assert result.route is Route.ALLOW, (command, result)


def test_quoted_tilde_remains_literal():
    from chrys.service.approval.prejudge_v2 import Matcher, Policy, Rule

    policy = Policy("enforce", "regression", (Rule("allow", Route.ALLOW, Matcher("argv_exact", ("echo", "~"))),))
    assert route("echo '~'", "bash", policy).route is Route.ALLOW


def test_assignment_prefix_cannot_certify_complete_argv():
    from chrys.service.approval.prejudge_v2 import Matcher, Policy, Rule

    allow = Policy(
        "enforce",
        "regression",
        (Rule("allow-rm", Route.ALLOW, Matcher("argv_exact", ("rm", "fixture"))),),
    )
    result = analyze("FOO=1 rm fixture")
    assert any(node.prefix[:1] == ("rm",) for node in result.executable_nodes)
    assert route("FOO=1 rm fixture", "bash", allow).route is Route.DEFER


def test_numeric_pipeline_arguments_remain_static():
    result = analyze("cat file.txt | head -20 | grep pattern")
    assert result.syntax_valid
    assert result.coverage_complete
    assert [node.argv for node in result.executable_nodes] == [
        ("cat", "file.txt"),
        ("head", "-20"),
        ("grep", "pattern"),
    ]


@pytest.mark.parametrize("command", ["rm x\n)", "git status &&", "echo ok; ("])
def test_malformed_command_always_defers(command):
    from chrys.service.approval.prejudge_v2 import Matcher, Policy, Rule

    policy = Policy(
        "enforce",
        "regression",
        (Rule("allow-rm", Route.ALLOW, Matcher("argv_exact", ("rm", "x"))),),
    )
    result = analyze(command)
    assert not result.syntax_valid
    assert not result.coverage_complete
    assert route(command, "bash", policy).route is Route.DEFER
