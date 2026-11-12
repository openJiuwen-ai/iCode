# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Run the actual Bash grammar when the optional feature is installed."""

from __future__ import annotations

import pytest

from chrys.service.approval.prejudge_v2 import Route, analyze, load_policy, route

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
def test_nested_executable_rm_keeps_block_evidence(cmd):
    result = analyze(cmd, dialect="bash")
    assert any(node.prefix[:1] == ("rm",) for node in result.executable_nodes)


def test_yaml_matchers_with_real_bash_ast(tmp_path):
    """Do not mock either the parser or the policy loader."""
    file = tmp_path / "prejudge.yaml"
    file.write_text(
        'schema_version: 1\nmode: "enforce"\npolicy_id: smoke\n'
        "rules:\n"
        "  - id: block-rm\n    effect: block\n    target: command\n"
        "    match:\n      prefix_first: [rm]\n"
        "  - id: allow-status\n    effect: allow\n    target: command\n"
        "    match:\n      argv_exact: [git, status]\n"
        "  - id: allow-echo\n    effect: allow\n    target: command\n"
        "    match:\n      argv_first: [echo]\n",
        encoding="utf-8",
    )
    policy = load_policy(file)
    cases = (
        ("git status", Route.ALLOW),
        ("rm fixture", Route.BLOCK),
        ('echo "rm"', Route.ALLOW),
        ("git status && rm fixture", Route.BLOCK),
        ("git status && unknown_tool", Route.DEFER),
        ('echo "$(rm fixture)"', Route.BLOCK),
        ('rm "$TARGET"', Route.BLOCK),
        ("git status > out.txt", Route.DEFER),
        ("/bin/rm -rf fixture", Route.DEFER),
        ("command rm fixture", Route.DEFER),
    )
    for command, expected in cases:
        result = route(command, "bash", policy)
        assert result.route is expected, (command, expected, result)
