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
        ("git log --oneline -5", ("git", "log", "--oneline", "-5"), "exec"),
        ("git diff --no-ext-diff --no-textconv", ("git", "diff", "--no-ext-diff", "--no-textconv"), "exec"),
        (
            "git --no-optional-locks status --short",
            ("git", "--no-optional-locks", "status", "--short"),
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
        ("git status --short", "not_approval_safe:git"),
        ("git config --global --list", "not_approval_safe:git-config"),
        ("git --no-pager config --list", "not_approval_safe:git-config"),
        ("git --no-pager --literal-pathspecs --glob-pathspecs config --list", "not_approval_safe:git-config"),
        ("git -C . config --list", "not_approval_safe:git-config"),
        ("git -c core.pager=evil log", "not_approval_safe:git"),
        ("find . -exec sh -c id +", "not_approval_safe:find"),
        ("find . -delete", "not_approval_safe:find"),
        ("sed -i s/a/b/ file", "not_approval_safe:sed"),
        ("sed -e s/a/b/e file", "not_approval_safe:sed"),
        ("python -c id", "not_approval_safe:python"),
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
    separately.
    """
    for command in ("cat .env", "cat /proc/self/environ", "env"):
        decision = classify_shell_for_approval(command, shell_name="bash")
        assert decision.safe is True
        assert shell_command_may_access_sensitive_data(command) is True


@pytest.mark.parametrize("command", ["", "   ", "\n", "ls\ncat"])
def test_empty_or_multiline_commands_are_not_safe(command: str) -> None:
    assert classify_shell_for_approval(command, shell_name="bash").safe is False
