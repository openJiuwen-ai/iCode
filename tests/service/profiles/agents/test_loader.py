# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tests for agent profile YAML loader."""

from __future__ import annotations

import logging
from pathlib import Path

import pytest
import yaml

from chrys.service.profiles.agents.loader import (
    AgentProfileLoadError,
    load_profile_files_from_dir,
    load_profile_from_yaml,
    load_profiles_from_dir,
)
from tests.support.paths import SRC_ROOT


@pytest.fixture
def tmp_yaml(tmp_path: Path) -> Path:
    """Create a minimal agent profile YAML file."""
    p = tmp_path / "test.yaml"
    p.write_text(
        """\
name: test-profile
display_name: "Test Profile"
description: "A test agent profile"
instructions: "You are a test agent."

tools:
  builtins:
    - filesystem.write
    - filesystem.read

approval:
  default: require
  overrides:
    filesystem.read.read_file: auto
    run_skill_script: require
  user_can_override: false
""",
        encoding="utf-8",
    )
    return p


def test_load_minimal_yaml(tmp_path: Path) -> None:
    p = tmp_path / "min.yaml"
    p.write_text("name: minimal\n", encoding="utf-8")
    profile = load_profile_from_yaml(p)
    assert profile.name == "minimal"
    assert profile.instructions == ""
    assert profile.tools.builtins == []
    assert profile.sub_agents.max_total_concurrency == 3


@pytest.mark.parametrize("encoding", ["utf-8-sig", "utf-16", "utf-32", "gb18030"])
def test_load_profile_detects_encoding_without_changing_instructions(tmp_path: Path, encoding: str) -> None:
    path = tmp_path / "legacy.yaml"
    raw = "name: legacy\ninstructions: 请阅读项目中的中文配置说明。\n".encode(encoding)
    path.write_bytes(raw)
    assert load_profile_from_yaml(path).instructions == "请阅读项目中的中文配置说明。"
    assert path.read_bytes() == raw


def test_damaged_profile_is_skipped_without_changing_its_bytes(tmp_path: Path) -> None:
    path = tmp_path / "damaged.yaml"
    raw = ("# 中文配置说明\n" * 20).encode() + b"name: damaged\ninstructions: abc\xffdef\n"
    path.write_bytes(raw)
    (tmp_path / "good.yaml").write_text("name: good\n", encoding="utf-8")
    with pytest.raises(AgentProfileLoadError, match="Cannot read"):
        load_profile_from_yaml(path)
    assert [profile.name for profile in load_profiles_from_dir(tmp_path)] == ["good"]
    assert path.read_bytes() == raw


@pytest.mark.parametrize(
    ("yaml_value", "expected"),
    [('"Plain text."', "Plain text."), ("null", ""), ("[First rule., Second rule.]", "First rule.\nSecond rule.")],
)
def test_load_instructions_text_joins_a_list_one_item_per_line(tmp_path: Path, yaml_value: str, expected: str) -> None:
    p = tmp_path / "instructions.yaml"
    p.write_text(
        f"""\
name: listed
instructions: {yaml_value}
skills:
  inline:
    - name: notes
      description: Notes.
      instructions: {yaml_value}
""",
        encoding="utf-8",
    )
    profile = load_profile_from_yaml(p)
    assert profile.instructions == expected
    assert profile.skills.inline[0].instructions == expected


@pytest.mark.parametrize("yaml_value", ["{rule: text}", "42", "[text, 1]"])
def test_load_rejects_instructions_that_are_not_text(tmp_path: Path, yaml_value: str) -> None:
    p = tmp_path / "bad_instructions.yaml"
    p.write_text(f"name: bad\ninstructions: {yaml_value}\n", encoding="utf-8")
    with pytest.raises(AgentProfileLoadError, match=r"field 'instructions' must be a string or a list of strings"):
        load_profile_from_yaml(p)

    skill = tmp_path / "bad_skill_instructions.yaml"
    skill.write_text(
        f"""\
name: bad
skills:
  inline:
    - name: notes
      description: Notes.
      instructions: {yaml_value}
""",
        encoding="utf-8",
    )
    with pytest.raises(AgentProfileLoadError, match=r"skills\.inline\[1\]\.instructions' must be a string"):
        load_profile_from_yaml(skill)


@pytest.mark.parametrize("item", ["Note: never push", "When editing files:"])
def test_load_names_the_instructions_item_yaml_read_as_a_mapping(tmp_path: Path, item: str) -> None:
    p = tmp_path / "colon_item.yaml"
    p.write_text(f"name: bad\ninstructions:\n  - Be brief.\n  - {item}\n  - Keep diffs small.\n", encoding="utf-8")
    with pytest.raises(AgentProfileLoadError, match=r"item 2 is a dict \(quote list items that contain ': ' or end"):
        load_profile_from_yaml(p)


def test_load_sub_agents_uses_default_concurrency_limits(tmp_path: Path) -> None:
    path = tmp_path / "sub-agents.yaml"
    path.write_text("name: parent\nsub_agents:\n  agents:\n    - profile: Explore\n", encoding="utf-8")

    profile = load_profile_from_yaml(path)

    assert profile.sub_agents.max_total_concurrency == 3
    assert profile.sub_agents.agents[0].max_concurrency == 3


@pytest.mark.parametrize(("value", "expected"), [("null", None), ("0", 0), ("100", 100), ("1234", 1234)])
def test_load_mcp_max_tool_result_tokens(tmp_path: Path, value: str, expected: int | None) -> None:
    path = tmp_path / "mcp-cap.yaml"
    path.write_text(
        f"name: capped\ntools:\n  mcp:\n    - name: srv\n      transport: stdio\n"
        f"      command: python\n      max_tool_result_tokens: {value}\n",
        encoding="utf-8",
    )

    profile = load_profile_from_yaml(path)

    assert profile.tools.mcp[0].max_tool_result_tokens == expected


@pytest.mark.parametrize("value", ["-1", "1", "99", "true", '"100"', "1.5"])
def test_load_mcp_rejects_invalid_max_tool_result_tokens(tmp_path: Path, value: str) -> None:
    path = tmp_path / "mcp-cap-invalid.yaml"
    path.write_text(
        f"name: capped\ntools:\n  mcp:\n    - name: srv\n      transport: stdio\n"
        f"      command: python\n      max_tool_result_tokens: {value}\n",
        encoding="utf-8",
    )

    with pytest.raises(AgentProfileLoadError, match="max_tool_result_tokens"):
        load_profile_from_yaml(path)


def test_load_legacy_hooks_section_warns(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    import logging

    p = tmp_path / "legacy-hooks.yaml"
    p.write_text(
        """\
name: legacy-hooks
hooks:
  before_tool_call: scripts/old.sh
""",
        encoding="utf-8",
    )

    with caplog.at_level(logging.WARNING, logger="chrys.service.profiles.agents.loader"):
        profile = load_profile_from_yaml(p)

    assert profile.name == "legacy-hooks"
    assert any("Ignoring legacy `hooks` section" in r.message for r in caplog.records)


def test_load_full_yaml(tmp_yaml: Path) -> None:
    profile = load_profile_from_yaml(tmp_yaml)
    assert profile.name == "test-profile"
    assert profile.display_name == "Test Profile"
    assert profile.instructions.strip() == "You are a test agent."
    assert profile.tools.builtins == ["filesystem.write", "filesystem.read"]
    assert profile.approval.default == "require"
    assert profile.approval.overrides == {
        "filesystem.read.read_file": "auto",
        "run_skill_script": "require",
    }
    assert profile.approval.user_can_override is False


@pytest.mark.parametrize(
    ("yaml_value", "expected"),
    [("-1", -1), ("0", 0), ("123456", 123_456), ("null", -1)],
)
def test_load_phase4_side_call_token_budget(tmp_path: Path, yaml_value: str, expected: int) -> None:
    path = tmp_path / "phase4-budget.yaml"
    path.write_text(
        f"name: budgeted\ncompaction:\n  phase4_side_call_token_budget: {yaml_value}\n",
        encoding="utf-8",
    )

    profile = load_profile_from_yaml(path)

    assert profile.compaction.phase4_side_call_token_budget == expected


def test_legacy_compaction_disable_is_preserved_for_one_load(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    path = tmp_path / "legacy-disabled.yaml"
    path.write_text(
        "name: legacy-disabled\ncompaction:\n  reserved_context_pct: 1.0\n",
        encoding="utf-8",
    )

    with caplog.at_level(logging.WARNING, logger="chrys.service.profiles.agents.loader"):
        profile = load_profile_from_yaml(path)

    assert not profile.compaction.enabled
    assert "stays disabled" in caplog.text


def test_legacy_thresholds_are_ignored_and_yaml_enabled_is_not_parsed(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    path = tmp_path / "legacy-thresholds.yaml"
    path.write_text(
        "name: legacy-thresholds\ncompaction:\n"
        "  enabled: false\n"
        "  reserved_context_pct: 0.2\n"
        "  compaction_target_pct: 0.5\n"
        "  force_compress_pct: 0.7\n",
        encoding="utf-8",
    )

    with caplog.at_level(logging.INFO, logger="chrys.service.profiles.agents.loader"):
        profile = load_profile_from_yaml(path)

    assert profile.compaction.enabled
    assert caplog.text.count("Ignoring obsolete compaction threshold field(s)") == 1


@pytest.mark.parametrize("yaml_value", ["null", "123", "[]"])
def test_load_rejects_non_string_last_words_supplement(tmp_path: Path, yaml_value: str) -> None:
    path = tmp_path / "invalid-last-words-supplement.yaml"
    path.write_text(
        f"name: invalid-supplement\ncompaction:\n  last_words_template: {yaml_value}\n",
        encoding="utf-8",
    )

    with pytest.raises(AgentProfileLoadError, match=r"compaction\.last_words_template.*must be a string"):
        load_profile_from_yaml(path)


@pytest.mark.parametrize("yaml_value", ["true", "-2", "1.5", '"123456"'])
def test_load_rejects_invalid_phase4_side_call_token_budget(tmp_path: Path, yaml_value: str) -> None:
    path = tmp_path / "invalid-phase4-budget.yaml"
    path.write_text(
        f"name: invalid-budget\ncompaction:\n  phase4_side_call_token_budget: {yaml_value}\n",
        encoding="utf-8",
    )

    with pytest.raises(
        AgentProfileLoadError,
        match=r"compaction\.phase4_side_call_token_budget.*integer >= -1 \(-1 means unlimited\) or null",
    ):
        load_profile_from_yaml(path)


def test_load_passes_approval_override_keys_through(tmp_path: Path) -> None:
    """YAML approval keys load verbatim — bare kinds are the runtime form."""
    p = tmp_path / "approval.yaml"
    p.write_text(
        """\
name: approval-test
approval:
  default: auto
  overrides:
    shell: require
    filesystem.write: require
    filesystem.write.write_file: skip
    custom_tool: skip
""",
        encoding="utf-8",
    )
    profile = load_profile_from_yaml(p)
    assert profile.approval.overrides == {
        "shell": "require",
        "filesystem.write": "require",
        "filesystem.write.write_file": "skip",
        "custom_tool": "skip",  # bare tool name — passed through
    }


@pytest.mark.parametrize(
    ("field", "yaml_value"),
    [
        ("approval.default", "maybe"),
        ("approval.overrides.run_skill_script", "yes"),
        ("approval.overrides.run_skill_script", "requre"),
    ],
)
def test_load_rejects_invalid_approval_rules(tmp_path: Path, field: str, yaml_value: str) -> None:
    """YAML booleans and typos cannot silently turn a secure rule into auto."""
    if field == "approval.default":
        approval_yaml = f"default: {yaml_value}"
    else:
        approval_yaml = f"overrides:\n    run_skill_script: {yaml_value}"
    path = tmp_path / "invalid-approval.yaml"
    path.write_text(
        f"name: invalid-approval\napproval:\n  {approval_yaml}\n",
        encoding="utf-8",
    )

    with pytest.raises(AgentProfileLoadError, match=field):
        load_profile_from_yaml(path)


def test_load_without_approval_section_defaults_todo_to_skip(tmp_path: Path) -> None:
    """A profile with no approval section gets the schema default overrides,
    which include ``todo: skip`` (todo_write only mutates engine-side state)."""
    p = tmp_path / "no-approval.yaml"
    p.write_text("name: no-approval\n", encoding="utf-8")
    profile = load_profile_from_yaml(p)
    assert profile.approval.overrides == {
        "shell": "require",
        "filesystem.write": "require",
        "todo": "skip",
    }


def test_load_declared_overrides_replace_schema_defaults(tmp_path: Path) -> None:
    """Declared overrides REPLACE the schema default (no merge) — a profile
    that wants ``todo: skip`` alongside its own overrides must declare it
    explicitly, which is why the builtin Code/QA YAMLs do."""
    p = tmp_path / "replace.yaml"
    p.write_text(
        """\
name: replace-test
approval:
  default: auto
  overrides:
    shell: require
""",
        encoding="utf-8",
    )
    profile = load_profile_from_yaml(p)
    assert profile.approval.overrides == {"shell": "require"}
    assert "todo" not in profile.approval.overrides


def test_load_strips_legacy_prefixed_approval_override_keys(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    """Pre-P1 YAML carried runtime-form ``chrys.*`` keys — strip with a warning."""
    import logging

    p = tmp_path / "legacy-approval.yaml"
    p.write_text(
        """\
name: legacy-approval
approval:
  overrides:
    chrys.shell: require
    chrys.filesystem.write.write_file: skip
    chrys.vendor_tool: auto
""",
        encoding="utf-8",
    )
    with caplog.at_level(logging.WARNING, logger="chrys.foundation.tool_kinds"):
        profile = load_profile_from_yaml(p)
    assert profile.approval.overrides == {
        "shell": "require",
        "filesystem.write.write_file": "skip",
        # Never matched a kind pre-P1 either — possibly a literal tool name.
        "chrys.vendor_tool": "auto",
    }
    assert sum("approval.overrides" in r.message for r in caplog.records) == 2


def test_load_tools_null_sections_mean_absent(tmp_path: Path) -> None:
    """YAML comment leftovers like `builtins:`/`mcp:` null must not reject the profile."""
    p = tmp_path / "null-sections.yaml"
    p.write_text(
        "name: nully\ntools:\n  builtins:\n  mcp:\n  custom:\n  web_search:\n  web_fetch:\n",
        encoding="utf-8",
    )
    profile = load_profile_from_yaml(p)
    assert profile.tools.builtins == []
    assert profile.tools.mcp == []
    assert profile.tools.web_search is None
    assert profile.tools.web_fetch is None


def test_load_tools_ignores_unknown_keys_and_keeps_unknown_categories(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    p = tmp_path / "unknown-tools-key.yaml"
    p.write_text(
        "name: compatible\ntools:\n  builtinz: []\n  builtins: [shell, future_category]\n",
        encoding="utf-8",
    )
    with caplog.at_level(logging.WARNING):
        profile = load_profile_from_yaml(p)
    # The registry skips a category it does not know; the profile keeps it, so a
    # save from this version never deletes another version's tool.
    assert profile.tools.builtins == ["shell", "future_category"]
    assert "builtinz" in caplog.text


def test_load_tools_drops_non_string_categories_with_a_warning(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    p = tmp_path / "stray-dash.yaml"
    p.write_text("name: stray\ntools:\n  builtins:\n    - shell\n    -\n    - 1\n", encoding="utf-8")
    with caplog.at_level(logging.WARNING):
        profile = load_profile_from_yaml(p)
    assert profile.tools.builtins == ["shell"]
    assert "non-string entries in tools.builtins" in caplog.text


def test_load_tools_accepts_legacy_yaml_boolean_web_modes(tmp_path: Path) -> None:
    p = tmp_path / "boolean-web-modes.yaml"
    p.write_text(
        "name: compatible\ntools:\n  web_search:\n    mode: off\n  web_fetch:\n    mode: on\n",
        encoding="utf-8",
    )
    profile = load_profile_from_yaml(p)
    assert profile.tools.web_search is not None and profile.tools.web_search.mode == "off"
    assert profile.tools.web_fetch is not None and profile.tools.web_fetch.mode == "on"


def test_invalid_user_profile_skip_logs_error(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    """A dropped user file silently falls back to a builtin; the skip must be loud."""
    import logging

    p = tmp_path / "Bad.yaml"
    p.write_text("name: bad\ntools:\n  builtins: shell\n", encoding="utf-8")
    with caplog.at_level(logging.ERROR):
        assert load_profile_files_from_dir(tmp_path) == []
    assert any("Bad.yaml" in record.message and record.levelno == logging.ERROR for record in caplog.records)


def test_load_missing_file(tmp_path: Path) -> None:
    with pytest.raises(AgentProfileLoadError, match="not found"):
        load_profile_from_yaml(tmp_path / "nope.yaml")


def test_load_invalid_yaml(tmp_path: Path) -> None:
    p = tmp_path / "bad.yaml"
    p.write_text("{{invalid yaml", encoding="utf-8")
    with pytest.raises(AgentProfileLoadError, match="Invalid YAML"):
        load_profile_from_yaml(p)


def test_load_missing_name(tmp_path: Path) -> None:
    p = tmp_path / "noname.yaml"
    p.write_text("description: no name field\n", encoding="utf-8")
    with pytest.raises(AgentProfileLoadError, match="missing required 'name'"):
        load_profile_from_yaml(p)


@pytest.mark.parametrize(
    "name",
    [
        "../escape",
        "/abs/path",
        "C:\\win",
        "nested/name",
        "back\\slash",
        ".",
        "..",
        "with\x00nul",
        # Windows-illegal filenames are rejected on every platform because
        # config directories get shared across machines.
        "CON",
        "nul.txt",
        "com1",
        "foo:bar",
        "wild*card",
        "what?",
        'quo"te',
        "pipe|name",
        "tab\tname",
    ],
)
def test_load_rejects_names_that_are_not_filename_safe(tmp_path: Path, name: str) -> None:
    p = tmp_path / "unsafe.yaml"
    p.write_text(yaml.safe_dump({"name": name}), encoding="utf-8")
    with pytest.raises(AgentProfileLoadError, match="not filename-safe"):
        load_profile_from_yaml(p)


@pytest.mark.parametrize("raw", ["''", "'   '", "123", "[a]"])
def test_load_rejects_non_string_or_blank_names(tmp_path: Path, raw: str) -> None:
    p = tmp_path / "bad.yaml"
    p.write_text(f"name: {raw}\n", encoding="utf-8")
    with pytest.raises(AgentProfileLoadError, match="must be a non-empty string"):
        load_profile_from_yaml(p)


def test_load_strips_surrounding_whitespace_from_name(tmp_path: Path) -> None:
    p = tmp_path / "padded.yaml"
    p.write_text("name: '  Padded Name '\n", encoding="utf-8")
    assert load_profile_from_yaml(p).name == "Padded Name"


def test_is_filename_safe_profile_name_accepts_plain_names() -> None:
    from chrys.service.profiles.agents.loader import is_filename_safe_profile_name

    assert is_filename_safe_profile_name("Code")
    assert is_filename_safe_profile_name("my-agent_2")
    assert is_filename_safe_profile_name("My Agent")
    assert is_filename_safe_profile_name("Code.")  # the on-disk name is Code..yaml, which is legal
    assert is_filename_safe_profile_name("console")  # only exact device names are reserved
    assert not is_filename_safe_profile_name("")
    assert not is_filename_safe_profile_name(" Code")


def test_load_not_a_mapping(tmp_path: Path) -> None:
    p = tmp_path / "list.yaml"
    p.write_text("- item1\n- item2\n", encoding="utf-8")
    with pytest.raises(AgentProfileLoadError, match="must be a mapping"):
        load_profile_from_yaml(p)


def test_load_profiles_from_dir(tmp_path: Path) -> None:
    (tmp_path / "a.yaml").write_text("name: alpha\n", encoding="utf-8")
    (tmp_path / "b.yaml").write_text("name: beta\n", encoding="utf-8")
    (tmp_path / "not_yaml.txt").write_text("ignored\n", encoding="utf-8")
    (tmp_path / "bad.yaml").write_text("- not a profile\n", encoding="utf-8")

    profiles = load_profiles_from_dir(tmp_path)
    names = [p.name for p in profiles]
    assert "alpha" in names
    assert "beta" in names
    assert len(profiles) == 2  # bad.yaml skipped


def test_load_profile_files_from_dir_returns_source_paths_yaml_before_yml(tmp_path: Path) -> None:
    (tmp_path / "zeta.yaml").write_text("name: zeta\n", encoding="utf-8")
    (tmp_path / "alpha.yml").write_text("name: alpha\n", encoding="utf-8")
    (tmp_path / ".skip.yaml").write_text("name: skip\n", encoding="utf-8")

    loaded = load_profile_files_from_dir(tmp_path)

    assert [(path.name, profile.name) for path, profile in loaded] == [("zeta.yaml", "zeta"), ("alpha.yml", "alpha")]
    assert [profile.name for profile in load_profiles_from_dir(tmp_path)] == ["zeta", "alpha"]


def test_load_profiles_from_dir_skips_dotfiles(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    hidden_path = tmp_path / ".metadata.yaml"
    hidden_path.write_text("not a profile\n", encoding="utf-8")
    (tmp_path / "good.yaml").write_text("name: good\n", encoding="utf-8")

    with caplog.at_level("WARNING", logger="chrys.service.profiles.agents.loader"):
        profiles = load_profiles_from_dir(tmp_path)

    assert [p.name for p in profiles] == ["good"]
    assert str(hidden_path) not in caplog.text


def test_load_profiles_from_dir_skips_malformed_nested_sections(tmp_path: Path) -> None:
    (tmp_path / "good.yaml").write_text("name: good\n", encoding="utf-8")
    (tmp_path / "bad_tools.yaml").write_text("name: bad\ntools:\n  - nope\n", encoding="utf-8")

    profiles = load_profiles_from_dir(tmp_path)

    assert [p.name for p in profiles] == ["good"]


def test_load_profiles_from_dir_logs_skipped_invalid_profile(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    (tmp_path / "good.yaml").write_text("name: good\n", encoding="utf-8")
    bad_path = tmp_path / "bad_tools.yaml"
    bad_path.write_text("name: bad\ntools:\n  - nope\n", encoding="utf-8")

    with caplog.at_level("WARNING"):
        profiles = load_profiles_from_dir(tmp_path)

    assert [p.name for p in profiles] == ["good"]
    assert f"Skipping invalid agent profile {bad_path}" in caplog.text


def test_load_rejects_malformed_nested_sections(tmp_path: Path) -> None:
    p = tmp_path / "bad_tools.yaml"
    p.write_text("name: bad\ntools:\n  - nope\n", encoding="utf-8")

    with pytest.raises(AgentProfileLoadError, match=r"tools.*mapping"):
        load_profile_from_yaml(p)


def test_load_profiles_from_nonexistent_dir(tmp_path: Path) -> None:
    result = load_profiles_from_dir(tmp_path / "nonexistent")
    assert result == []


def test_load_mcp_all_transports(tmp_path: Path) -> None:
    """Verify MCP transport types parse correctly from YAML."""
    p = tmp_path / "mcp.yaml"
    p.write_text(
        """\
name: mcp-test
tools:
  mcp:
    - name: local-server
      transport: stdio
      command: python
      args: [-m, my_server]
      env:
        API_KEY: secret
      encoding: utf-8
      tool_name_prefix: local
      timeout: 30

    - name: remote-api
      transport: http
      url: http://localhost:8080/mcp
      headers:
        Authorization: Bearer token
      resolve_header_templates: false
      terminate_on_close: true
      verify_ssl: false
      bypass_proxy: true
      env:
        SSL_CERT_FILE: /tmp/ca.pem
      description: Remote HTTP MCP
      allowed_tools: [tool_a, tool_b]

""",
        encoding="utf-8",
    )
    profile = load_profile_from_yaml(p)
    mcps = profile.tools.mcp
    assert len(mcps) == 2

    # stdio
    assert mcps[0].name == "local-server"
    assert mcps[0].transport == "stdio"
    assert mcps[0].command == "python"
    assert mcps[0].args == ["-m", "my_server"]
    assert mcps[0].env == {"API_KEY": "secret"}
    assert mcps[0].encoding == "utf-8"
    assert mcps[0].tool_name_prefix == "local"
    assert mcps[0].request_timeout == 30
    assert mcps[0].allowed_tools is None
    assert mcps[0].enabled is True

    # http
    assert mcps[1].name == "remote-api"
    assert mcps[1].transport == "http"
    assert mcps[1].url == "http://localhost:8080/mcp"
    assert mcps[1].headers == {"Authorization": "Bearer token"}
    assert mcps[1].resolve_header_templates is False
    assert mcps[1].terminate_on_close is True
    assert mcps[1].verify_ssl is False
    assert mcps[1].bypass_proxy is True
    assert mcps[1].env == {}
    assert mcps[1].description == "Remote HTTP MCP"
    assert mcps[1].allowed_tools == ["tool_a", "tool_b"]
    assert mcps[1].enabled is True


@pytest.mark.parametrize(
    ("prefix", "progressive", "message"),
    [
        ("github.v1", False, "underscores, and hyphens"),
        ("a" * 50, True, "invalid generated control.*maximum is 64"),
    ],
)
def test_load_mcp_rejects_provider_invalid_tool_name_prefix(
    tmp_path: Path,
    prefix: str,
    progressive: bool,
    message: str,
) -> None:
    p = tmp_path / "invalid_mcp_prefix.yaml"
    p.write_text(
        "\n".join(
            [
                "name: invalid-prefix",
                "tools:",
                "  mcp:",
                "    - name: remote",
                "      transport: stdio",
                "      command: python",
                f"      tool_name_prefix: {prefix}",
                f"      use_progressive_disclosure: {str(progressive).lower()}",
            ]
        ),
        encoding="utf-8",
    )

    with pytest.raises(AgentProfileLoadError, match=message):
        load_profile_from_yaml(p)


def test_load_mcp_allowed_tools_empty_list_is_explicit(tmp_path: Path) -> None:
    p = tmp_path / "mcp_empty_allowed_tools.yaml"
    p.write_text(
        """\
name: mcp-empty-allow-list
tools:
  mcp:
    - name: local-server
      transport: stdio
      command: python
      allowed_tools: []
""",
        encoding="utf-8",
    )

    profile = load_profile_from_yaml(p)

    assert profile.tools.mcp[0].allowed_tools == []


def test_load_mcp_progressive_options(tmp_path: Path) -> None:
    p = tmp_path / "mcp_progressive.yaml"
    p.write_text(
        """\
name: mcp-progressive
tools:
  mcp:
    - name: local-server
      transport: stdio
      command: python
      use_progressive_disclosure: true
      always_load: [search, read]
""",
        encoding="utf-8",
    )

    config = load_profile_from_yaml(p).tools.mcp[0]

    assert config.use_progressive_disclosure is True
    assert config.always_load == ["search", "read"]


@pytest.mark.parametrize("invalid", ["search", True, ["search", 7]])
def test_load_mcp_always_load_rejects_non_string_lists(tmp_path: Path, invalid: object) -> None:
    p = tmp_path / "mcp_invalid_always.yaml"
    p.write_text(
        yaml.safe_dump(
            {
                "name": "mcp-invalid-always",
                "tools": {
                    "mcp": [
                        {
                            "name": "local-server",
                            "transport": "stdio",
                            "command": "python",
                            "use_progressive_disclosure": True,
                            "always_load": invalid,
                        }
                    ]
                },
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(AgentProfileLoadError, match=r"always_load.*list of strings"):
        load_profile_from_yaml(p)


@pytest.mark.parametrize(
    ("policy", "message"),
    [
        ({"always_load": ["search"]}, "always_load requires use_progressive_disclosure=true"),
        (
            {"allowed_tools": [], "use_progressive_disclosure": True},
            "progressive disclosure requires at least one permitted tool",
        ),
        (
            {
                "allowed_tools": ["search"],
                "use_progressive_disclosure": True,
                "always_load": ["write"],
            },
            "always_load tools must also appear in allowed_tools: write",
        ),
    ],
)
def test_load_mcp_rejects_incoherent_tool_loading_policy(
    tmp_path: Path,
    policy: dict[str, object],
    message: str,
) -> None:
    p = tmp_path / "mcp_invalid_policy.yaml"
    p.write_text(
        yaml.safe_dump(
            {
                "name": "mcp-invalid-policy",
                "tools": {
                    "mcp": [
                        {
                            "name": "local-server",
                            "transport": "stdio",
                            "command": "python",
                            **policy,
                        }
                    ]
                },
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(AgentProfileLoadError, match=message):
        load_profile_from_yaml(p)


def test_load_mcp_enable_false(tmp_path: Path) -> None:
    """Verify MCP enabled flag is parsed from YAML."""
    p = tmp_path / "mcp_enable.yaml"
    p.write_text(
        """\
name: mcp-enable-test
tools:
  mcp:
    - name: disabled-server
      transport: stdio
      command: python
      enabled: false
""",
        encoding="utf-8",
    )
    profile = load_profile_from_yaml(p)
    mcps = profile.tools.mcp
    assert len(mcps) == 1
    assert mcps[0].name == "disabled-server"
    assert mcps[0].enabled is False

    """MCP server without transport field should raise."""
    p = tmp_path / "bad_mcp.yaml"
    p.write_text(
        """\
name: bad
tools:
  mcp:
    - name: no-transport
      command: python
""",
        encoding="utf-8",
    )
    with pytest.raises(AgentProfileLoadError, match="missing required 'transport'"):
        load_profile_from_yaml(p)


def test_load_mcp_load_prompts_false(tmp_path: Path) -> None:
    """Verify MCP load_prompts=false is parsed from YAML (defaults True otherwise)."""
    p = tmp_path / "mcp_load_prompts.yaml"
    p.write_text(
        """\
name: mcp-load-prompts-test
tools:
  mcp:
    - name: no-prompts
      transport: stdio
      command: python
      load_prompts: false
    - name: default-prompts
      transport: stdio
      command: python
""",
        encoding="utf-8",
    )
    profile = load_profile_from_yaml(p)
    mcps = profile.tools.mcp
    assert mcps[0].load_prompts is False
    assert mcps[1].load_prompts is True


def test_load_mcp_invalid_transport(tmp_path: Path) -> None:
    """MCP server with invalid transport should raise."""
    p = tmp_path / "bad_transport.yaml"
    p.write_text(
        """\
name: bad
tools:
  mcp:
    - name: grpc-server
      transport: grpc
      url: localhost:50051
""",
        encoding="utf-8",
    )
    with pytest.raises(AgentProfileLoadError, match="invalid transport 'grpc'"):
        load_profile_from_yaml(p)


def test_load_skills_full(tmp_path: Path) -> None:
    """Verify skills config with paths, inline skills, resources, and scripts."""
    p = tmp_path / "skills.yaml"
    p.write_text(
        """\
name: skills-test
skills:
  paths:
    - ./skills
    - ~/shared-skills
  inline:
    - name: review-code
      description: Review code for quality
      instructions: |
        Review the code carefully.
      resources:
        - name: style-guide
          description: Coding style rules
          content: |
            Use 4-space indentation.
        - name: checklist
          path: ./review-checklist.md
      scripts:
        - name: run-linter
          description: Run the project linter
          path: ./scripts/lint.py

    - name: summarize
      description: Summarize a file
      instructions: Provide a concise summary.
""",
        encoding="utf-8",
    )
    profile = load_profile_from_yaml(p)
    skills = profile.skills
    assert skills.paths == ["./skills", "~/shared-skills"]
    assert skills.script_timeout == 300  # default when not specified
    assert len(skills.inline) == 2

    # first inline skill
    review = skills.inline[0]
    assert review.name == "review-code"
    assert review.description == "Review code for quality"
    assert "Review the code carefully" in review.instructions
    assert len(review.resources) == 2
    assert review.resources[0].name == "style-guide"
    assert "4-space" in review.resources[0].content
    assert review.resources[1].name == "checklist"
    assert review.resources[1].path == "./review-checklist.md"
    assert len(review.scripts) == 1
    assert review.scripts[0].name == "run-linter"
    assert review.scripts[0].path == "./scripts/lint.py"

    # second inline skill
    summarize = skills.inline[1]
    assert summarize.name == "summarize"
    assert summarize.resources == []
    assert summarize.scripts == []


def test_load_inline_skill_rejects_missing_description(tmp_path: Path) -> None:
    p = tmp_path / "inline_missing_description.yaml"
    p.write_text(
        """\
name: bad-inline
skills:
  inline:
    - name: legacy
      instructions: Body.
""",
        encoding="utf-8",
    )

    with pytest.raises(AgentProfileLoadError, match=r"skills\.inline\[1\]\.description is required"):
        load_profile_from_yaml(p)


def test_load_inline_skill_rejects_non_spec_name(tmp_path: Path) -> None:
    p = tmp_path / "inline_bad_name.yaml"
    p.write_text(
        """\
name: bad-inline
skills:
  inline:
    - name: legacy_name
      description: Has an underscore
      instructions: Body.
""",
        encoding="utf-8",
    )

    with pytest.raises(
        AgentProfileLoadError, match=r"skills\.inline\[1\]\.name 'legacy_name' is not a valid skill name"
    ):
        load_profile_from_yaml(p)


def test_load_inline_skill_rejects_non_string_description(tmp_path: Path) -> None:
    p = tmp_path / "inline_non_string_description.yaml"
    p.write_text(
        """\
name: bad-inline
skills:
  inline:
    - name: legacy
      description: 42
      instructions: Body.
""",
        encoding="utf-8",
    )

    with pytest.raises(AgentProfileLoadError, match=r"skills\.inline\[1\]\.description must be a string"):
        load_profile_from_yaml(p)


def test_load_inline_skill_rejects_description_over_spec_limit(tmp_path: Path) -> None:
    p = tmp_path / "inline_long_description.yaml"
    long_description = "x" * 1025
    p.write_text(
        f"""\
name: bad-inline
skills:
  inline:
    - name: legacy
      description: {long_description}
      instructions: Body.
""",
        encoding="utf-8",
    )

    with pytest.raises(AgentProfileLoadError, match=r"1024 characters or fewer"):
        load_profile_from_yaml(p)


def test_load_skills_empty(tmp_path: Path) -> None:
    """No skills section should produce empty SkillsConfig."""
    p = tmp_path / "no_skills.yaml"
    p.write_text("name: no-skills\n", encoding="utf-8")
    profile = load_profile_from_yaml(p)
    assert profile.skills.paths == []
    assert profile.skills.inline == []
    assert profile.skills.script_timeout == 300
    # Default: auto-load ~/.agents/skills is enabled.
    assert profile.skills.auto_load_user_agents_skills is True


def test_load_skills_auto_load_user_agents_skills_disabled(tmp_path: Path) -> None:
    """Verify the auto_load_user_agents_skills flag is parsed from YAML."""
    p = tmp_path / "no_auto_load.yaml"
    p.write_text(
        """\
name: no-auto-load
skills:
  auto_load_user_agents_skills: false
""",
        encoding="utf-8",
    )
    profile = load_profile_from_yaml(p)
    assert profile.skills.auto_load_user_agents_skills is False


def test_load_skills_auto_load_cwd_agents_skills_default(tmp_path: Path) -> None:
    """Default (field absent) → auto_load_cwd_agents_skills is True."""
    p = tmp_path / "default_cwd.yaml"
    p.write_text(
        """\
name: default-cwd
skills: {}
""",
        encoding="utf-8",
    )
    profile = load_profile_from_yaml(p)
    assert profile.skills.auto_load_cwd_agents_skills is True


def test_load_skills_auto_load_cwd_agents_skills_disabled(tmp_path: Path) -> None:
    """Verify auto_load_cwd_agents_skills can be disabled via YAML."""
    p = tmp_path / "no_cwd_load.yaml"
    p.write_text(
        """\
name: no-cwd-load
skills:
  auto_load_cwd_agents_skills: false
""",
        encoding="utf-8",
    )
    profile = load_profile_from_yaml(p)
    assert profile.skills.auto_load_cwd_agents_skills is False


def test_load_skills_script_timeout(tmp_path: Path) -> None:
    """Verify script_timeout is parsed from YAML."""
    p = tmp_path / "timeout.yaml"
    p.write_text(
        """\
name: timeout-test
skills:
  paths:
    - ./skills
  script_timeout: 60
""",
        encoding="utf-8",
    )
    profile = load_profile_from_yaml(p)
    assert profile.skills.script_timeout == 60


def test_load_builtin_profiles() -> None:
    """Verify built-in agent profile YAML files load successfully."""
    builtins_dir = SRC_ROOT / "chrys" / "service" / "profiles" / "agents" / "builtins"
    profiles = load_profiles_from_dir(builtins_dir)
    names = {p.name for p in profiles}
    assert names == {"Code", "Explore", "General", "QA"}

    profiles_by_name = {profile.name: profile for profile in profiles}
    for name in ("Code", "QA"):
        sub_agents = profiles_by_name[name].sub_agents
        assert sub_agents.max_total_concurrency == 3
        assert all(ref.max_concurrency == 3 for ref in sub_agents.agents)


def test_builtin_last_words_templates_are_supplementary_only() -> None:
    builtins_dir = SRC_ROOT / "chrys" / "service" / "profiles" / "agents" / "builtins"
    paths = sorted(builtins_dir.glob("*.yaml"))
    agent_specific_emphasis = {
        "Code": "build/test/lint outcomes",
        "Explore": "exact search/glob patterns",
        "General": "cannot ask the caller for clarification",
        "QA": "git history or blame context",
    }
    templates: dict[str, str] = {}

    assert paths
    for path in paths:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        template = data["compaction"]["last_words_template"]
        templates[path.stem] = template
        assert set(data["compaction"]) == {"last_words_template"}, path.name
        assert agent_specific_emphasis[path.stem] in template, path.name
        assert "## " not in template, path.name
        for duplicated_base_guidance in (
            "tool-call history",
            "note replaces it",
            "Merge any previous progress note",
            "Work already completed",
            "Outstanding subtasks",
            "Key findings worth carrying forward",
            "Skills used this turn",
            "Things that matter most right now",
            "Dead ends to avoid",
        ):
            assert duplicated_base_guidance not in template, path.name

    assert "read-only exploration task" in templates["Explore"]
    assert "zero-hit searches" in templates["Explore"]
    assert "symbols/signatures/types/constants" in templates["Explore"]
    assert "architecture/library/naming decisions" in templates["General"]
    assert "read-only question-answering task" in templates["QA"]
    assert "symbols/signatures/types" in templates["QA"]
    assert "zero-hit results" in templates["QA"]


def test_load_builtin_profiles_carry_hardcoded_ids() -> None:
    """Built-in profiles must ship with stable hardcoded ids for sync/distribution."""
    builtins_dir = SRC_ROOT / "chrys" / "service" / "profiles" / "agents" / "builtins"
    profiles = {p.name: p for p in load_profiles_from_dir(builtins_dir)}
    assert profiles["Code"].id == "b011c0de0001"
    assert profiles["Explore"].id == "b011e80e0002"
    assert profiles["General"].id == "b0119e4e0004"
    assert profiles["QA"].id == "b0119a000005"


@pytest.mark.parametrize(
    "yaml_id,expected", [("abc123def456", "abc123def456"), ("'007'", "007"), ("'  padded  '", "padded")]
)
def test_load_id_field(tmp_path: Path, yaml_id: str, expected: str) -> None:
    """``id`` from YAML round-trips into ``AgentProfile.id``."""
    p = tmp_path / "with_id.yaml"
    p.write_text(f"name: ided\nid: {yaml_id}\n", encoding="utf-8")
    profile = load_profile_from_yaml(p)
    assert profile.id == expected


@pytest.mark.parametrize("id_field", ["", "id: null\n", "id: ''\n"])
def test_load_missing_id_yields_empty(tmp_path: Path, id_field: str) -> None:
    """Absent, null or empty ids retain the legacy registry migration behavior."""
    p = tmp_path / "no_id.yaml"
    p.write_text(f"name: legacy\n{id_field}", encoding="utf-8")
    profile = load_profile_from_yaml(p)
    assert profile.id == ""


@pytest.mark.parametrize(
    "yaml_id",
    [
        "42",
        "007",
        "true",
        "2026-01-01",
        "2026-13-01",
        "0",
        "false",
        "[]",
        "{}",
        "!!bool nope",
        "!!int ''",
        "!!timestamp nope",
    ],
)
def test_load_invalid_id_is_rejected_without_aborting_directory_load(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, yaml_id: str
) -> None:
    bad_path = tmp_path / "bad.yaml"
    bad_path.write_text(f"name: bad\nid: {yaml_id}\n", encoding="utf-8")
    good_path = tmp_path / "good.yaml"
    good_path.write_text("name: good\nid: '007'\n", encoding="utf-8")

    with pytest.raises(AgentProfileLoadError, match=r"'id' must be a string|Invalid YAML") as error:
        load_profile_from_yaml(bad_path)
    assert str(bad_path) in str(error.value)

    with caplog.at_level("WARNING", logger="chrys.service.profiles.agents.loader"):
        loaded = load_profile_files_from_dir(tmp_path)

    assert [(path, profile.name, profile.id) for path, profile in loaded] == [(good_path, "good", "007")]
    assert f"Skipping invalid agent profile {bad_path}" in caplog.text


def test_load_model_profile_id(tmp_path: Path) -> None:
    """``model.profile_id`` parses into ``ModelConfig.profile_id``."""
    p = tmp_path / "model.yaml"
    p.write_text(
        """\
name: model-test
model:
  profile_id: abcdef123456
""",
        encoding="utf-8",
    )
    profile = load_profile_from_yaml(p)
    assert profile.model.profile_id == "abcdef123456"


def test_load_model_legacy_profile_ignored(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    """Legacy ``model.profile`` (name-based) is ignored and logs a warning."""
    import logging

    p = tmp_path / "legacy.yaml"
    p.write_text(
        """\
name: legacy-test
model:
  profile: some-old-name
""",
        encoding="utf-8",
    )
    with caplog.at_level(logging.WARNING, logger="chrys.service.profiles.agents.loader"):
        profile = load_profile_from_yaml(p)
    assert profile.model.profile_id == ""
    assert any("legacy `model.profile`" in r.message for r in caplog.records)


def test_load_model_missing_section(tmp_path: Path) -> None:
    """No ``model`` section → default empty ``ModelConfig``."""
    p = tmp_path / "none.yaml"
    p.write_text("name: none-test\n", encoding="utf-8")
    profile = load_profile_from_yaml(p)
    assert profile.model.profile_id == ""


def test_load_mcp_expose_instructions_false(tmp_path: Path) -> None:
    """Verify MCP expose_instructions=false is parsed from YAML (defaults True otherwise)."""
    p = tmp_path / "mcp_expose_instructions.yaml"
    p.write_text(
        """\
name: mcp-expose-instructions-test
tools:
  mcp:
    - name: no-instructions
      transport: stdio
      command: python
      expose_instructions: false
    - name: default-instructions
      transport: stdio
      command: python
""",
        encoding="utf-8",
    )
    profile = load_profile_from_yaml(p)
    mcps = profile.tools.mcp
    assert mcps[0].expose_instructions is False
    assert mcps[1].expose_instructions is True
