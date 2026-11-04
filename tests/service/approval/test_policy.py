# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tests for ``ApprovalPolicy`` rule resolution and ``ApprovalMode.from_string``."""

from __future__ import annotations

from typing import Any, cast
from unittest.mock import MagicMock

import pytest

from chrys.foundation.tool_approval import declare_requires_approval
from chrys.foundation.tool_kinds import KIND_CONTEXT, KIND_SKILL
from chrys.kernel import FunctionTool
from chrys.service.approval.policy import ApprovalMode, ApprovalPolicy
from chrys.service.profiles.agents.schema import ApprovalConfig
from chrys.service.skills.constants import RUN_SKILL_SCRIPT_TOOL_NAME


def _mock_tool(name: str, kind: str | None = None) -> MagicMock:
    """Create a mock FunctionTool with name and kind."""
    t = MagicMock()
    t.name = name
    t.chrys_kind = kind
    return t


def test_default_auto_does_not_require() -> None:
    policy = ApprovalPolicy(ApprovalConfig(default="auto"))
    assert not policy.should_require_approval("any_tool")


def test_default_require() -> None:
    policy = ApprovalPolicy(ApprovalConfig(default="require"))
    assert policy.should_require_approval("any_tool")


def test_default_config_skips_todo_kind() -> None:
    """The schema-default overrides carry ``todo: skip`` — even under a
    require-everything default, todo_write never prompts (it only mutates
    engine-side todo state; the visible list update IS the review surface)."""
    tools = [
        _mock_tool("todo_write", kind="todo"),
        _mock_tool("other_tool"),
    ]
    policy = ApprovalPolicy(ApprovalConfig(default="require"), tools)
    assert not policy.should_require_approval("todo_write")
    assert policy.should_require_approval("other_tool")


def test_bare_tool_name_override() -> None:
    tools = [
        _mock_tool("run_skill_script"),
        _mock_tool("load_skill"),
    ]
    policy = ApprovalPolicy(
        ApprovalConfig(default="auto", overrides={"run_skill_script": "require"}),
        tools=tools,
    )
    assert policy.should_require_approval("run_skill_script")
    assert not policy.should_require_approval("load_skill")


def test_kind_only_override() -> None:
    """A kind-only key (e.g. ``shell``) applies to ALL tools with that kind."""
    tools = [
        _mock_tool("zsh", kind="shell"),
        _mock_tool("bash", kind="shell"),
        _mock_tool("pwsh", kind="shell"),
        _mock_tool("powershell", kind="shell"),
        _mock_tool("read_file", kind="filesystem.read"),
    ]
    policy = ApprovalPolicy(
        ApprovalConfig(default="auto", overrides={"shell": "require"}),
        tools=tools,
    )
    assert policy.should_require_approval("zsh")
    assert policy.should_require_approval("bash")
    assert policy.should_require_approval("pwsh")
    assert policy.should_require_approval("powershell")
    assert not policy.should_require_approval("read_file")


def test_kind_dot_name_override() -> None:
    """``kind.tool_name`` applies only to the exact tool, not all tools of that kind."""
    tools = [
        _mock_tool("write_file", kind="filesystem.write"),
        _mock_tool("edit_file", kind="filesystem.write"),
        _mock_tool("read_file", kind="filesystem.read"),
    ]
    policy = ApprovalPolicy(
        ApprovalConfig(default="auto", overrides={"filesystem.write.write_file": "require"}),
        tools=tools,
    )
    assert policy.should_require_approval("write_file")
    # Same kind but different name — must NOT be affected
    assert not policy.should_require_approval("edit_file")
    assert not policy.should_require_approval("read_file")


def test_mixed_overrides() -> None:
    """Kind-only and kind.name overrides work together."""
    tools = [
        _mock_tool("zsh", kind="shell"),
        _mock_tool("bash", kind="shell"),
        _mock_tool("write_file", kind="filesystem.write"),
        _mock_tool("edit_file", kind="filesystem.write"),
        _mock_tool("read_file", kind="filesystem.read"),
    ]
    policy = ApprovalPolicy(
        ApprovalConfig(
            default="auto",
            overrides={
                "shell": "require",
                "filesystem.write": "require",
            },
        ),
        tools=tools,
    )
    assert policy.should_require_approval("zsh")
    assert policy.should_require_approval("bash")
    assert policy.should_require_approval("write_file")
    assert policy.should_require_approval("edit_file")
    assert not policy.should_require_approval("read_file")


def test_user_override_when_allowed() -> None:
    policy = ApprovalPolicy(ApprovalConfig(default="require", user_can_override=True))
    assert policy.should_require_approval("any_tool")

    ok = policy.set_user_override("skip")
    assert ok
    assert not policy.should_require_approval("any_tool")


def test_user_override_when_not_allowed() -> None:
    policy = ApprovalPolicy(ApprovalConfig(default="require", user_can_override=False))
    ok = policy.set_user_override("skip")
    assert not ok
    # Policy unchanged
    assert policy.should_require_approval("any_tool")


def test_clear_user_override() -> None:
    policy = ApprovalPolicy(ApprovalConfig(default="require", user_can_override=True))
    policy.set_user_override("skip")
    assert not policy.should_require_approval("any_tool")

    policy.set_user_override(None)
    assert policy.should_require_approval("any_tool")


def test_sub_agent_with_subset_tools() -> None:
    """Parent overrides for missing tools should not bleed to other tools of the same kind.

    Reproduces the bug where a sub-agent with only ``read_file`` (kind=filesystem.read)
    inherited ``filesystem.write: require`` and incorrectly required approval
    for ``read_file``.
    """
    tools = [
        _mock_tool("read_file", kind="filesystem.read"),
        _mock_tool("zsh", kind="shell"),
    ]
    policy = ApprovalPolicy(
        ApprovalConfig(
            default="auto",
            overrides={
                "shell": "require",
                "filesystem.write": "require",
            },
        ),
        tools=tools,
    )
    # kind-only "shell" matches the shell tool
    assert policy.should_require_approval("zsh")
    # kind-only "filesystem.write" must NOT apply to read_file (different kind)
    assert not policy.should_require_approval("read_file")


# ── ApprovalMode.from_string ────────────────────────────────────────


def test_approval_mode_from_string_valid() -> None:
    assert ApprovalMode.from_string("manual") is ApprovalMode.MANUAL
    assert ApprovalMode.from_string("auto") is ApprovalMode.AUTO
    assert ApprovalMode.from_string("bypass") is ApprovalMode.BYPASS


def test_approval_mode_from_string_normalizes_case_and_whitespace() -> None:
    assert ApprovalMode.from_string("  AUTO  ") is ApprovalMode.AUTO
    assert ApprovalMode.from_string("Manual") is ApprovalMode.MANUAL


def test_approval_mode_from_string_falls_back_on_invalid() -> None:
    assert ApprovalMode.from_string("") is ApprovalMode.MANUAL
    assert ApprovalMode.from_string(None) is ApprovalMode.MANUAL
    assert ApprovalMode.from_string("nonsense") is ApprovalMode.MANUAL


def test_approval_mode_from_string_custom_default() -> None:
    assert ApprovalMode.from_string("", default=ApprovalMode.AUTO) is ApprovalMode.AUTO
    assert ApprovalMode.from_string("garbage", default=ApprovalMode.BYPASS) is ApprovalMode.BYPASS


# ──────────────── provenance kinds in the override namespace ────────────────


def test_skill_kind_only_override_applies_to_skill_tools() -> None:
    """`skill: require` targets the whole skill tool family via KIND_SKILL."""
    tools = [
        _mock_tool("load_skill", kind=KIND_SKILL),
        _mock_tool("run_skill_script", kind=KIND_SKILL),
        _mock_tool("read_file", kind="filesystem.read"),
    ]
    policy = ApprovalPolicy(ApprovalConfig(default="auto", overrides={"skill": "require"}), tools=tools)
    assert policy.should_require_approval("load_skill")
    assert policy.should_require_approval("run_skill_script")
    assert not policy.should_require_approval("read_file")


def test_context_kind_only_override_applies_to_context_tools() -> None:
    tools = [
        _mock_tool("compress_context", kind=KIND_CONTEXT),
        _mock_tool("list_compressed_contexts", kind=KIND_CONTEXT),
        _mock_tool("zsh", kind="shell"),
    ]
    policy = ApprovalPolicy(ApprovalConfig(default="auto", overrides={"context": "require"}), tools=tools)
    assert policy.should_require_approval("compress_context")
    assert policy.should_require_approval("list_compressed_contexts")
    assert not policy.should_require_approval("zsh")


@pytest.mark.parametrize(
    ("tool_name", "tool_kind", "override_key"),
    [
        ("load_skill", KIND_SKILL, "skill"),
        ("compress_context", KIND_CONTEXT, "context"),
    ],
)
def test_kind_override_applies_to_runtime_added_tool(
    tool_name: str,
    tool_kind: str,
    override_key: str,
) -> None:
    """Live provenance enforces kind rules even when construction saw no tool."""
    policy = ApprovalPolicy(
        ApprovalConfig(default="auto", overrides={override_key: "require"}),
        tools=[],
    )

    assert policy.should_require_approval(tool_name, tool_kind)
    assert not policy.should_require_approval("unrelated_tool", "mcp")


@pytest.mark.parametrize(
    "overrides",
    [
        {"skill": "auto", "load_skill": "skip", "skill.load_skill": "require"},
        {"skill.load_skill": "require", "load_skill": "skip", "skill": "auto"},
    ],
)
def test_kind_qualified_tool_override_wins_independent_of_mapping_order(overrides: dict[str, str]) -> None:
    policy = ApprovalPolicy(ApprovalConfig(default="skip", overrides=overrides), tools=[])

    assert policy.should_require_approval("load_skill", KIND_SKILL)


@pytest.mark.parametrize(
    "overrides",
    [
        {"skill": "require", "load_skill": "skip"},
        {"load_skill": "skip", "skill": "require"},
    ],
)
def test_bare_tool_override_wins_over_kind_independent_of_mapping_order(overrides: dict[str, str]) -> None:
    policy = ApprovalPolicy(ApprovalConfig(default="require", overrides=overrides), tools=[])

    assert not policy.should_require_approval("load_skill", KIND_SKILL)


@pytest.mark.parametrize("default", ["auto", "skip"])
def test_a_declared_tool_has_a_secure_approval_floor(default: str) -> None:
    policy = ApprovalPolicy(ApprovalConfig(default=default, overrides={}), tools=[])

    assert policy.should_require_approval("run_skill_script", KIND_SKILL, tool_requires_approval=True)
    assert not policy.should_require_approval("run_skill_script", KIND_SKILL, tool_requires_approval=False)
    # The floor comes from the called tool, never from its name or kind.
    assert not policy.should_require_approval("run_skill_script", KIND_SKILL)


def test_the_initial_tool_list_supplies_the_declaration_a_caller_omits() -> None:
    declared = FunctionTool(name="deploy", func=lambda: "")
    declare_requires_approval(declared)
    plain = FunctionTool(name="status", func=lambda: "")
    policy = ApprovalPolicy(ApprovalConfig(default="auto", overrides={}), tools=[declared, plain])

    assert policy.should_require_approval("deploy")
    assert not policy.should_require_approval("status")
    # The live declaration wins over the initial list.
    assert not policy.should_require_approval("deploy", tool_requires_approval=False)
    assert policy.should_require_approval("status", tool_requires_approval=True)


@pytest.mark.parametrize(
    "override_key",
    [RUN_SKILL_SCRIPT_TOOL_NAME, f"{KIND_SKILL}.{RUN_SKILL_SCRIPT_TOOL_NAME}"],
)
@pytest.mark.parametrize("rule", ["auto", "skip"])
def test_explicit_tool_override_can_opt_out_of_run_skill_script_floor(
    override_key: str,
    rule: str,
) -> None:
    policy = ApprovalPolicy(
        ApprovalConfig(default="require", overrides={override_key: rule}),
        tools=[],
    )

    assert not policy.should_require_approval("run_skill_script", KIND_SKILL, tool_requires_approval=True)


@pytest.mark.parametrize("rule", ["auto", "skip"])
def test_kind_wide_skill_override_intentionally_opts_out_of_script_floor(rule: str) -> None:
    """A kind rule is explicit authority over every skill tool, including execution."""
    policy = ApprovalPolicy(
        ApprovalConfig(default="require", overrides={KIND_SKILL: rule}),
        tools=[],
    )

    assert not policy.should_require_approval(RUN_SKILL_SCRIPT_TOOL_NAME, KIND_SKILL, tool_requires_approval=True)
    assert not policy.should_require_approval("load_skill", KIND_SKILL)


def test_invalid_matching_override_fails_closed(caplog: pytest.LogCaptureFixture) -> None:
    config = ApprovalConfig(default="auto", overrides={})
    config.overrides[RUN_SKILL_SCRIPT_TOOL_NAME] = cast(Any, True)

    with caplog.at_level("ERROR", logger="chrys.service.approval.policy"):
        policy = ApprovalPolicy(config, tools=[])

    assert policy.should_require_approval(RUN_SKILL_SCRIPT_TOOL_NAME, KIND_SKILL, tool_requires_approval=False)
    assert any("Invalid approval rule" in record.message for record in caplog.records)


def test_invalid_default_fails_closed() -> None:
    config = ApprovalConfig(default=cast(Any, "maybe"), overrides={})
    policy = ApprovalPolicy(config, tools=[])

    assert policy.should_require_approval("ordinary_tool", "mcp")


def test_allowed_user_override_precedes_run_skill_script_floor() -> None:
    policy = ApprovalPolicy(
        ApprovalConfig(default="auto", overrides={}, user_can_override=True),
        tools=[],
    )
    assert policy.set_user_override("skip")

    assert not policy.should_require_approval("run_skill_script", KIND_SKILL, tool_requires_approval=True)


def test_invalid_user_override_fails_closed() -> None:
    policy = ApprovalPolicy(
        ApprovalConfig(default="auto", overrides={}, user_can_override=True),
        tools=[],
    )

    assert policy.set_user_override(cast(Any, True))
    assert policy.should_require_approval(RUN_SKILL_SCRIPT_TOOL_NAME, KIND_SKILL)


def test_reserved_kind_key_shadowing_tool_name_warns_when_kind_in_toolset(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A tool literally named `skill` next to KIND_SKILL tools: key applies to the kind, warns."""
    tools = [
        _mock_tool("load_skill", kind=KIND_SKILL),
        _mock_tool("skill", kind="mcp"),
    ]
    with caplog.at_level("WARNING", logger="chrys.service.approval.policy"):
        policy = ApprovalPolicy(ApprovalConfig(default="auto", overrides={"skill": "require"}), tools=tools)
    assert policy.should_require_approval("load_skill"), "the kind semantics must still win"
    assert not policy.should_require_approval("skill"), "the literally-named tool is NOT targeted"
    assert any("'mcp.skill'" in r.message for r in caplog.records), "the warning must carry the migration form"


def test_reserved_kind_key_shadowing_tool_name_warns_when_kind_absent(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A reserved kind can target future tools, never the same-named MCP tool."""
    tools = [_mock_tool("context", kind="mcp")]
    with caplog.at_level("WARNING", logger="chrys.service.approval.policy"):
        policy = ApprovalPolicy(ApprovalConfig(default="auto", overrides={"context": "require"}), tools=tools)
    assert not policy.should_require_approval("context")
    assert any("runtime-added tools" in r.message and "'mcp.context'" in r.message for r in caplog.records)


def test_kind_dot_name_form_targets_kind_named_tool_exactly() -> None:
    """The migration path: `mcp.context` reaches a tool literally named `context`."""
    tools = [
        _mock_tool("context", kind="mcp"),
        _mock_tool("other_remote", kind="mcp"),
    ]
    policy = ApprovalPolicy(ApprovalConfig(default="auto", overrides={"mcp.context": "require"}), tools=tools)
    assert policy.should_require_approval("context")
    assert not policy.should_require_approval("other_remote")


def test_no_shadow_warning_when_no_tool_bears_the_kind_name(caplog: pytest.LogCaptureFixture) -> None:
    tools = [_mock_tool("load_skill", kind=KIND_SKILL)]
    with caplog.at_level("WARNING", logger="chrys.service.approval.policy"):
        ApprovalPolicy(ApprovalConfig(default="auto", overrides={"skill": "require"}), tools=tools)
    assert not caplog.records


def test_canonical_tool_named_for_its_kind_does_not_warn(caplog: pytest.LogCaptureFixture) -> None:
    tools = [_mock_tool("web_search", kind="web_search")]
    with caplog.at_level("WARNING", logger="chrys.service.approval.policy"):
        policy = ApprovalPolicy(ApprovalConfig(default="auto", overrides={"web_search": "skip"}), tools=tools)
    assert not policy.should_require_approval("web_search")
    assert not caplog.records


@pytest.mark.parametrize("name", ["web_search", "web_fetch"])
def test_web_kind_key_still_reaches_the_mcp_tool_it_named_before_the_kind_existed(name, caplog) -> None:
    """A profile gating an MCP ``web_search`` keeps gating it now that the name is also a kind."""
    tools = [_mock_tool(name, kind="mcp"), _mock_tool("other_remote", kind="mcp")]
    with caplog.at_level("WARNING", logger="chrys.service.approval.policy"):
        policy = ApprovalPolicy(ApprovalConfig(default="auto", overrides={name: "require"}), tools=tools)
    assert policy.should_require_approval(name)
    assert policy.should_require_approval(name, name), "the local tool of that kind is covered too"
    assert not policy.should_require_approval("other_remote")
    assert not caplog.records
