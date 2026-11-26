# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tests for approval safety classification helpers."""

from __future__ import annotations

import os

import pytest

from chrys.service.approval.safety_classifier import (
    path_arg_may_access_sensitive_data,
    shell_arg_may_access_sensitive_data,
    shell_argv_may_access_sensitive_data,
    shell_command_may_access_sensitive_data,
    target_may_access_sensitive_data,
)


@pytest.mark.parametrize(
    ("argv", "sensitive"),
    [
        (("grep", "-F", "$DEMO_TOKEN", "README.md"), False),
        (("printenv", "DEMO_TOKEN"), True),
        (("env", "A=1", "printenv"), True),
        (("bash", "-c", "echo '$DEMO_TOKEN'"), False),
        (("bash", "-c", 'echo "$DEMO_TOKEN"'), True),
        (("eval", "echo", "$DEMO_TOKEN"), True),
        (("eval", "echo", "'$DEMO_TOKEN'"), False),
        (("command", "-v", "eval", "echo $DEMO_TOKEN"), False),
        (("command", "-pv", "eval", "echo $DEMO_TOKEN"), False),
        (("command", "-pp", "eval", "echo $DEMO_TOKEN"), True),
        (("timeout", "5", "bash", "-c", "echo $DEMO_TOKEN"), True),
        (("timeout", "--signal=TERM", "--", "5m", "/bin/bash", "-lc", "echo $DEMO_TOKEN"), True),
        (("nice", "-n", "5", "timeout", "5", "bash", "-c", "echo $DEMO_TOKEN"), True),
        (("xargs", "-0", "-n1", "bash", "-c", "echo $DEMO_TOKEN"), True),
        (("xargs", "-0", "-r", "-n", "1", "bash", "-c", "echo $DEMO_TOKEN"), True),
        (("xargs", "-0rn", "1", "bash", "-c", "echo $DEMO_TOKEN"), True),
        (("xargs", "-0rn1", "bash", "-c", "echo $DEMO_TOKEN"), True),
        (("XARGS.EXE", "-0rn1", "BASH.EXE", "-c", "echo $DEMO_TOKEN"), os.name == "nt"),
        (("xargs", "-0r", "--max-args=1", "--", "bash", "-c", "echo $DEMO_TOKEN"), True),
        (("xargs", "--null", "--max-args", "1", "bash", "-c", "echo $DEMO_TOKEN"), True),
        (("xargs", "-0rn", "1", "grep", "-F", "bash", "-c", "$DEMO_TOKEN"), False),
        (("xargs", "-0rd", "-", "grep", "-F", "$DEMO_TOKEN"), False),
        (("xargs", "-v", "bash", "-c", "echo $DEMO_TOKEN"), None),
        (("xargs", "-0rn"), None),
        (("xargs", "--max-args", "--", "bash", "-c", "echo $DEMO_TOKEN"), None),
        (("xargs", "--max-args="), None),
        (("xargs", "--version"), False),
        (("xargs", "-0r"), False),  # Default command is echo.
        (("timeout", "-vsTERM", "5", "bash", "-c", "echo $DEMO_TOKEN"), True),
        (("timeout", "-vs", "TERM", "5", "bash", "-c", "echo $DEMO_TOKEN"), True),
        (("timeout", "-vk", "5"), None),
        (("nice", "-n-5", "timeout", "-vsTERM", "5", "bash", "-c", "echo $DEMO_TOKEN"), True),
        (("nice", "-n"), None),
        (("nice", "--help"), False),
        (("sudo", "-nuroot", "nice", "-n1", "bash", "-c", "echo $DEMO_TOKEN"), True),
        (("sudo", "-nZ", "bash", "-c", "echo $DEMO_TOKEN"), None),
        (("timeout", "5", "env", "-iuDEMO", "bash", "-c", "echo $DEMO_TOKEN"), True),
        (("nice", "-n1", "env", "-S", "bash -c 'echo $DEMO_TOKEN'"), None),
        (("bash", "-c", "timeout 5 bash -c 'echo $DEMO_TOKEN'"), True),
        (("bash", "-c", "timeout 5 xargs -I{} bash -c 'echo $DEMO_TOKEN'"), None),
        (("bash", "-c", "xargs -I{} echo '{}' && echo \"$DEMO_TOKEN\""), True),
        (("echo", "bash", "-c", "$DEMO_TOKEN"), False),
        (("grep", "-F", "bash", "-c", "$DEMO_TOKEN"), False),
        (("timeout", "5", "grep", "-F", "bash", "-c", "$DEMO_TOKEN"), False),
        (("timeout", "--help", "5", "bash", "-c", "echo $DEMO_TOKEN"), False),
        (("xargs", "--replace=bash", "echo", "bash", "-c", "$DEMO_TOKEN"), None),
    ],
)
def test_certified_argv_keeps_literal_tokens_and_explicit_child_source_distinct(argv, sensitive):
    assert shell_argv_may_access_sensitive_data(argv, shell_name="bash") is sensitive


def test_shell_sensitive_analysis_caps_pathological_unclosed_substitutions() -> None:
    """Large malformed substitution input is conservatively judged instead of rescanned quadratically."""
    command = "$(" * 5000
    assert shell_command_may_access_sensitive_data(command, shell_name="zsh") is True


@pytest.mark.parametrize(
    "path",
    [
        "~/.git-credentials",
        "~/Library/Keychains/login.keychain-db",
        "~/.kube/config",
        "~/.config/gh/hosts.yml",
        "~/.azure/accessTokens.json",
        "~/.azure/msal_token_cache.json",
        "~/.azure/msal_token_cache.bin",
    ],
)
def test_path_arg_flags_common_credential_stores(path: str) -> None:
    assert path_arg_may_access_sensitive_data({"path": path}) is True


@pytest.mark.parametrize(
    "raw_args",
    [
        {"arguments": ["~/.git-credentials"]},
        {"args": {"input": "~/.ssh/id_rsa"}},
    ],
)
def test_opaque_skill_script_arguments_are_not_reinterpreted_as_path_fields(raw_args: dict[str, object]) -> None:
    """Script tokens stay opaque; mandatory script approval is their security boundary."""
    assert path_arg_may_access_sensitive_data(raw_args) is False


@pytest.mark.parametrize(
    "path",
    [
        ".git/config",
        ".git/config.worktree",
        ".git/hooks/pre-commit",
        "/workspace/repo/.git/hooks/post-checkout",
    ],
)
def test_target_flags_git_execution_surfaces(path: str) -> None:
    assert target_may_access_sensitive_data(path) is True


@pytest.mark.parametrize("command", ["echo $PATH", "echo %PATH%", "echo hi"])
def test_shell_classification_does_not_escalate_non_sensitive_commands(command: str) -> None:
    assert shell_command_may_access_sensitive_data(command) is False


@pytest.mark.parametrize("path", [".env.example", ".env.sample", ".env.template"])
def test_env_template_exclusions_are_not_sensitive_but_env_is(path: str) -> None:
    assert target_may_access_sensitive_data(path) is False
    assert target_may_access_sensitive_data(".env") is True


def test_shell_args_json_payload_classifies_and_malformed_json_fails_open() -> None:
    assert shell_arg_may_access_sensitive_data('{"command": "echo $API_KEY"}') is True
    assert shell_arg_may_access_sensitive_data('{"command":') is False
