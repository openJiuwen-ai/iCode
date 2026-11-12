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

    # Isolate explicit static-argv matching from built-in readonly parity.
    policy = Policy("enforce", "regression", (Rule("allow", Route.ALLOW, Matcher("argv_exact", argv)),))
    assert route(command, "bash", policy).route is not Route.ALLOW


@pytest.mark.parametrize("command", ["$'rm' x", r"$'\x72m' x", "rm x\n)"])
def test_reliable_block_evidence_survives_quotes_and_trailing_syntax_errors(command):
    from chrys.service.approval.prejudge_v2 import Matcher, Policy, Rule

    policy = Policy(
        "enforce",
        "regression",
        (Rule("block-rm", Route.BLOCK, Matcher("prefix_first", ("rm",))),),
    )
    result = route(command, "bash", policy)
    assert result.route is Route.BLOCK, (command, result)
    if ")" in command:
        assert not analyze(command).syntax_valid
        assert not analyze(command).coverage_complete


def test_quoted_tilde_remains_literal():
    from chrys.service.approval.prejudge_v2 import Matcher, Policy, Rule

    policy = Policy("enforce", "regression", (Rule("allow", Route.ALLOW, Matcher("argv_exact", ("echo", "~"))),))
    assert route("echo '~'", "bash", policy).route is Route.ALLOW


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        ("ls -la", Route.ALLOW),
        ("ls *.py", Route.ALLOW),
        ("cat file.txt | head -20 | grep pattern", Route.ALLOW),
        ("git --no-optional-locks status --short", Route.ALLOW),
        ("git status --short", Route.DEFER),
        ("git push origin main", Route.DEFER),
        ("find . -delete", Route.DEFER),
        ("echo rm", Route.DEFER),
        ("echo 'find -delete is dangerous'", Route.ALLOW),
        ("python3 --version", Route.ALLOW),
        ("python3 script.py", Route.DEFER),
        ("pip list", Route.ALLOW),
        ("pip install package", Route.DEFER),
        ("ruff check --no-cache --no-fix src tests", Route.ALLOW),
        ("ruff check --fix src", Route.DEFER),
        ("ls > output.txt", Route.DEFER),
        ("echo $(rm fixture)", Route.DEFER),
    ],
)
def test_builtin_yaml_readonly_real_ast(tmp_path, command, expected):
    """Built-in rules are data interpreted by the real Bash AST engine."""
    file = tmp_path / "prejudge.yaml"
    file.write_text('schema_version: 1\nmode: "enforce"\npolicy_id: builtin-smoke\nrules: []\n', encoding="utf-8")
    policy = load_policy(file)
    assert policy.readonly_rules
    result = route(command, "bash", policy)
    assert result.route is expected, (command, expected, result)


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        ("ls -la", True),
        ("ls *.py", True),
        ("git --no-optional-locks status --short", True),
        ("git status --short", False),
        ("git push origin main", False),
        ("echo rm", False),
        ("python3 --version", True),
        ("pip install package", False),
    ],
)
def test_builtin_ast_matches_legacy_for_supported_cases(tmp_path, command, expected):
    """Compare a documented supported slice; old checker is a test oracle ONLY."""
    from chrys.service.tools.builtins.shell_filter import is_safe_readonly_command

    assert is_safe_readonly_command(command, shell_name="bash") is expected
    file = tmp_path / "prejudge.yaml"
    file.write_text('schema_version: 1\nmode: "enforce"\npolicy_id: parity\nrules: []\n', encoding="utf-8")
    assert (route(command, "bash", load_policy(file)).route is Route.ALLOW) is expected


def test_assignment_prefix_blocks_but_cannot_certify_complete_argv():
    from chrys.service.approval.prejudge_v2 import Matcher, Policy, Rule

    block = Policy(
        "enforce",
        "regression",
        (Rule("block-rm", Route.BLOCK, Matcher("prefix_first", ("rm",))),),
    )
    allow = Policy(
        "enforce",
        "regression",
        (Rule("allow-rm", Route.ALLOW, Matcher("argv_exact", ("rm", "fixture"))),),
    )
    result = analyze("FOO=1 rm fixture")
    assert any(node.prefix[:1] == ("rm",) for node in result.executable_nodes)
    assert route("FOO=1 rm fixture", "bash", block).route is Route.BLOCK
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


def test_error_recovery_never_allows_malformed_command():
    from chrys.service.approval.prejudge_v2 import Matcher, Policy, Rule

    policy = Policy(
        "enforce",
        "regression",
        (Rule("allow-rm", Route.ALLOW, Matcher("argv_exact", ("rm", "x"))),),
    )
    result = analyze("rm x\n)")
    assert not result.syntax_valid
    assert not result.coverage_complete
    assert route("rm x\n)", "bash", policy).route is Route.DEFER


@pytest.mark.parametrize(
    ("command", "allowed"),
    [
        ("git log --oneline -5", True),
        ("git log --no-ext-diff --no-textconv -p -1", True),
        ("git log -p -1", False),
        ("git log --oneline --output=history.txt -5", False),
        ("awk '{print $1}' file.txt", True),
        ("awk '{ print $12 }'", True),
        ("awk '{print $1}' -f script.awk", False),
        ("awk '{print $1}' --source='BEGIN {system(\"unsafe_tool\")}'", False),
    ],
)
def test_builtin_numeric_log_and_field_print_match_legacy(tmp_path, command, allowed):
    from chrys.service.tools.builtins.shell_filter import is_safe_readonly_command

    assert is_safe_readonly_command(command, shell_name="bash") is allowed
    file = tmp_path / "prejudge.yaml"
    file.write_text('schema_version: 1\nmode: "enforce"\npolicy_id: regression\nrules: []\n', encoding="utf-8")
    result = route(command, "bash", load_policy(file))
    assert (result.route is Route.ALLOW) is allowed, result
