# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tests for the approval-safe shell boundary."""

from __future__ import annotations

import pytest

from chrys.service.approval.safety_classifier import shell_command_may_access_sensitive_data
from chrys.service.tools.builtins.shell_filter import classify_shell_for_approval


@pytest.mark.parametrize(
    ("command", "argv", "execution"),
    [
        ("ls -la src", ("ls", "-la", "src"), "exec"),
        ("cat README.md", ("cat", "README.md"), "exec"),
        ("cat 'file with space'", ("cat", "file with space"), "exec"),
        ('cat "file with space"', ("cat", "file with space"), "exec"),
        ("grep TODO src/chrys/service", ("grep", "TODO", "src/chrys/service"), "exec"),
        ("pwd", ("pwd",), "shell"),
        ("ps", ("ps",), "exec"),
        ("ps aux", ("ps", "aux"), "exec"),
        ("ps -ef", ("ps", "-ef"), "exec"),
        ("git log --oneline -5", ("git", "log", "--oneline", "-5"), "exec"),
        (
            "git diff --cached --no-ext-diff --no-textconv",
            ("git", "diff", "--cached", "--no-ext-diff", "--no-textconv"),
            "exec",
        ),
        (
            "git for-each-ref --format='%(refname)' refs/heads",
            ("git", "for-each-ref", "--format=%(refname)", "refs/heads"),
            "exec",
        ),
        ("git branch --list 'feature/*'", ("git", "branch", "--list", "feature/*"), "exec"),
        ("git tag --list 'v*'", ("git", "tag", "--list", "v*"), "exec"),
        (
            "git check-mailmap '<A U Thor <author@example.com>>'",
            ("git", "check-mailmap", "<A U Thor <author@example.com>>"),
            "exec",
        ),
        ("sed -n 1p README.md", ("sed", "-n", "1p", "README.md"), "exec"),
        ("find . -name README.md", ("find", ".", "-name", "README.md"), "exec"),
        ("python --version", ("python", "--version"), "exec"),
        ("pip list --format=json", ("pip", "list", "--format=json"), "exec"),
        ("uniq -- input.txt", ("uniq", "--", "input.txt"), "exec"),
        ("ruff --version", ("ruff", "--version"), "exec"),
        ("echo hello", ("echo", "hello"), "shell"),
        ('echo "a b"', ("echo", "a b"), "shell"),
        ("echo '(not execution)'", ("echo", "(not execution)"), "shell"),
        ("printf %s", ("printf", "%s"), "shell"),
    ],
)
def test_classifier_returns_validated_argv(command: str, argv: tuple[str, ...], execution: str) -> None:
    decision = classify_shell_for_approval(command, shell_name="bash")

    assert decision.safe is True
    assert decision.command is not None
    assert decision.command.argv == argv
    assert decision.command.execution == execution
    assert decision.reason == ""


@pytest.mark.parametrize(
    ("command", "reason"),
    [
        ("rg --pre ./x.sh -e y .", "not_approval_safe:rg"),
        ("rg --pre=./x.sh -e y .", "not_approval_safe:rg"),
        ("rg -n foo .", "not_approval_safe:rg"),
        ("rg --pre$IFS./x.sh -e y .", "shell_syntax"),
        ("rg --p''re ./x.sh -e y .", "not_approval_safe:rg"),
        ("r''g --pre ./x.sh -e y .", "not_approval_safe:rg"),
        ("\\rg --pre ./x.sh -e y .", "shell_syntax"),
        ("rg --pre-glob README.md --pre ./x.sh -e y .", "not_approval_safe:rg"),
        ("cat .env*", "shell_syntax"),
        ('cat "$HOME/x"', "shell_syntax"),
        ("cat {a,b}", "shell_syntax"),
        ("ls | cat", "shell_syntax"),
        ("cat > out", "shell_syntax"),
        ("date -s 2020-01-01", "not_approval_safe:date"),
        ("hostname evil", "not_approval_safe:hostname"),
        ("tree -o out .", "not_approval_safe:tree"),
        ("less README.md", "not_approval_safe:less"),
        ("more README.md", "not_approval_safe:more"),
        ("bash -c id", "not_approval_safe:bash"),
        ("git status --short", "not_approval_safe:git-worktree"),
        ("git --no-optional-locks status --short", "not_approval_safe:git-worktree"),
        ("git config --global --list", "not_approval_safe:git-config"),
        ("git --no-pager config --list", "not_approval_safe:git-config"),
        ("git --no-pager --literal-pathspecs --glob-pathspecs config --list", "not_approval_safe:git-config"),
        ("git -C . config --list", "not_approval_safe:git-config"),
        ("git -c core.pager=evil log", "not_approval_safe:git"),
        ("git diff --no-ext-diff --no-textconv", "not_approval_safe:git-worktree"),
        ("ps eww -p 1", "not_approval_safe:ps"),
        ("ps -E", "not_approval_safe:ps"),
        ("ps auxe", "not_approval_safe:ps"),
        ("ps -o command", "not_approval_safe:ps"),
        ("find . -exec sh -c id +", "not_approval_safe:find"),
        ("find . -delete", "not_approval_safe:find"),
        ("sed -i s/a/b/ file", "not_approval_safe:sed"),
        ("sed -e s/a/b/e file", "not_approval_safe:sed"),
        ("python -c id", "not_approval_safe:python"),
        ("pytest --version", "not_approval_safe:pytest"),
        ("pytest --help", "not_approval_safe:pytest"),
        ("pytest -h", "not_approval_safe:pytest"),
        ("pip list --log out.log", "not_approval_safe:pip"),
        ("pip show --log out.log", "not_approval_safe:pip"),
        ("pip freeze --log out.log", "not_approval_safe:pip"),
        ("uniq -- input.txt output.txt", "not_approval_safe:uniq"),
        ("jq -n env", "not_approval_safe:jq"),
        ("env", "not_approval_safe:env"),
        ("env A=", "not_approval_safe:env"),
        ("en''v", "not_approval_safe:env"),
        ("'env'", "not_approval_safe:env"),
    ],
)
def test_classifier_requires_approval(command: str, reason: str) -> None:
    decision = classify_shell_for_approval(command, shell_name="bash")

    assert decision.safe is False
    assert decision.command is None
    assert decision.reason == reason


def test_git_bash_keeps_safe_commands_on_its_shell() -> None:
    """Git Bash's core tools live in its own usr/bin; keep shell lookup semantics."""
    for command in ("ls -la", "cat README.md", "git log --oneline"):
        decision = classify_shell_for_approval(command, shell_name="git_bash")

        assert decision.safe is True
        assert decision.command is not None
        assert decision.command.execution == "shell"


@pytest.mark.parametrize("shell_name", ["pwsh", "powershell", "cmd"])
def test_windows_shells_are_not_auto_approved(shell_name: str) -> None:
    """Their argument grammar needs a separate parser; fail closed until then."""
    decision = classify_shell_for_approval("Get-ChildItem", shell_name=shell_name)

    assert decision.safe is False
    assert decision.reason == f"shell_not_supported:{shell_name}"


def test_windows_shells_do_not_auto_approve_even_simple_external_commands() -> None:
    assert classify_shell_for_approval("git log --oneline", shell_name="pwsh").safe is False
    assert classify_shell_for_approval("ls", shell_name="cmd").safe is False


def test_classifier_is_pure_and_has_no_sensitive_target_gate() -> None:
    """Sensitivity is a second gate: the classifier only proves the command shape.

    ``cat .env`` and ``/proc/self/environ`` are safe shapes but must never reach
    auto-approval; the approval middleware checks the sensitive-target predicate
    separately. ``env`` is not an auto-approved shape at all: it dumps the
    environment with or without an empty assignment.
    """
    for command in ("cat .env", "cat /proc/self/environ"):
        decision = classify_shell_for_approval(command, shell_name="bash")
        assert decision.safe is True
        assert shell_command_may_access_sensitive_data(command) is True
    for command in ("env", "env A=", "en''v"):
        assert classify_shell_for_approval(command, shell_name="bash").safe is False


@pytest.mark.parametrize("shell_name", ["", "ash", "fish", "csh", "tcsh"])
def test_unmodeled_shells_are_not_auto_approved(shell_name: str) -> None:
    """An unknown or non-POSIX shell grammar fails closed instead of assuming bash."""
    decision = classify_shell_for_approval("ls", shell_name=shell_name)

    assert decision.safe is False
    assert decision.command is None
    assert decision.reason.startswith("shell_not_supported:")


def test_zsh_equals_expansion_requires_approval() -> None:
    """zsh expands ``=cmd`` even after quotes; the parsed argv is not what runs."""
    decision = classify_shell_for_approval("echo =foo", shell_name="zsh")

    assert decision.safe is False
    assert decision.reason == "zsh_equals_expansion"


@pytest.mark.parametrize("command", ["", "   ", "\n", "ls\ncat"])
def test_empty_or_multiline_commands_are_not_safe(command: str) -> None:
    assert classify_shell_for_approval(command, shell_name="bash").safe is False
