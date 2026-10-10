# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tests for the hooks YAML/JSON loader."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from chrys.service.hooks.events import HookEvent
from chrys.service.hooks.loader import (
    HooksConfigError,
    load_hooks_dir,
    load_hooks_file,
    load_hooks_project,
    merge_hooks_files,
    parse_hooks_dict,
)
from chrys.service.hooks.schema import HookConfig, HookRun, HookSettings, HooksFile, MergedHooksFile, SkippedHook


@pytest.mark.parametrize("encoding", ["utf-8-sig", "utf-16", "utf-32", "gb18030"])
@pytest.mark.parametrize("suffix", ["yaml", "json"])
def test_hooks_file_detects_encoding(tmp_path: Path, encoding: str, suffix: str) -> None:
    description = "在执行工具前检查命令参数。记录本次操作的结果和详细说明。"
    # JSON is also YAML, so both loaders see exactly the same non-ASCII values.
    text = json.dumps(
        {
            "hooks": [
                {
                    "id": "guard",
                    "description": description,
                    "event": "before_tool_call",
                    "run": {"type": "command", "argv": ["echo", description]},
                }
            ]
        },
        ensure_ascii=False,
    )
    src = tmp_path / f"hooks.{suffix}"
    src.write_bytes(text.encode(encoding))

    file = load_hooks_file(src)

    assert file.hooks[0].description == description
    assert file.hooks[0].run.argv == ["echo", description]


@pytest.mark.parametrize("suffix", ["yaml", "json"])
def test_damaged_hooks_are_reported_as_config_errors(tmp_path: Path, suffix: str) -> None:
    src = tmp_path / f"hooks.{suffix}"
    text = json.dumps(
        {
            "hooks": [
                {
                    "id": "guard",
                    "description": "中文配置说明" * 20,
                    "event": "before_tool_call",
                    "run": {"type": "command", "argv": ["echo", "SENTINEL"]},
                }
            ]
        },
        ensure_ascii=False,
    )
    src.write_bytes(text.encode().replace(b"SENTINEL", b"abc\xffdef"))

    with pytest.raises(HooksConfigError, match="decode"):
        load_hooks_file(src)


def test_empty_file_returns_defaults() -> None:
    file = parse_hooks_dict({})
    assert file.version == 1
    assert file.hooks == []
    assert file.settings.shutdown_grace_seconds == 5.0


def test_minimal_valid_hook() -> None:
    file = parse_hooks_dict(
        {
            "version": 1,
            "hooks": [
                {
                    "id": "h1",
                    "event": "before_tool_call",
                    "run": {"type": "command", "argv": ["/bin/true"]},
                }
            ],
        }
    )
    assert len(file.hooks) == 1
    h = file.hooks[0]
    assert h.id == "h1"
    assert h.event is HookEvent.BEFORE_TOOL_CALL
    assert h.execution.mode == "fire_and_forget"
    assert h.execution.detach is False
    assert h.run.argv == ["/bin/true"]


def test_yaml_roundtrip(tmp_path: Path) -> None:
    src = tmp_path / "hooks.yaml"
    src.write_text(
        """\
version: 1
settings:
  shutdown_grace_seconds: 2.5
  max_parallel_hooks: 8
hooks:
  - id: guard
    event: before_tool_call
    match:
      profile: Code
      tool_kind: shell
      args:
        command:
          regex: 'rm\\s+-rf'
    run:
      type: script
      path: scripts/guard.py
    execution:
      mode: blocking
      timeout_seconds: 7
      on_error: block
""",
        encoding="utf-8",
    )
    file = load_hooks_file(src)
    assert file.source == str(src)
    assert file.settings.shutdown_grace_seconds == 2.5
    assert file.settings.max_parallel_hooks == 8
    h = file.hooks[0]
    assert h.match.profile == "Code"
    assert h.match.tool_kind == "shell"
    assert "command" in h.match.args
    assert h.match.args["command"].regex == "rm\\s+-rf"
    assert h.execution.mode == "blocking"
    assert h.execution.timeout_seconds == 7


def test_match_tool_kind_accepts_canonical_bare_kind() -> None:
    file = parse_hooks_dict(
        {
            "version": 1,
            "hooks": [
                {
                    "id": "h",
                    "event": "before_tool_call",
                    "match": {"tool_kind": "filesystem.write"},
                    "run": {"type": "command", "argv": ["/bin/true"]},
                }
            ],
        }
    )
    assert file.hooks[0].match.tool_kind == "filesystem.write"


def test_match_tool_kind_strips_legacy_prefixed_kind(caplog: pytest.LogCaptureFixture) -> None:
    """Pre-P1 configs carried runtime-form ``chrys.*`` kinds — strip with a warning."""
    import logging

    with caplog.at_level(logging.WARNING, logger="chrys.foundation.tool_kinds"):
        file = parse_hooks_dict(
            {
                "version": 1,
                "hooks": [
                    {
                        "id": "legacy",
                        "event": "before_tool_call",
                        "match": {"tool_kind": "chrys.shell"},
                        "run": {"type": "command", "argv": ["/bin/true"]},
                    },
                    {
                        "id": "unknown",
                        "event": "before_tool_call",
                        "match": {"tool_kind": "chrys.vendor.custom"},
                        "run": {"type": "command", "argv": ["/bin/true"]},
                    },
                ],
            }
        )
    assert file.hooks[0].match.tool_kind == "shell"
    # Never matched a kind pre-P1 either — left for the user to see verbatim.
    assert file.hooks[1].match.tool_kind == "chrys.vendor.custom"
    assert sum("match.tool_kind" in r.message for r in caplog.records) == 1


def test_match_tool_kind_leaves_unknown_values_and_arg_keys_alone() -> None:
    file = parse_hooks_dict(
        {
            "version": 1,
            "hooks": [
                {
                    "id": "h",
                    "event": "before_tool_call",
                    "match": {
                        "tool_kind": "vendor.custom",
                        "args": {
                            "shell": {"equals": "bash"},
                            "filesystem.write": {"contains": "literal arg name"},
                        },
                    },
                    "run": {"type": "command", "argv": ["/bin/true"]},
                }
            ],
        }
    )
    match = file.hooks[0].match
    assert match.tool_kind == "vendor.custom"
    assert set(match.args) == {"shell", "filesystem.write"}


def test_json_loader(tmp_path: Path) -> None:
    src = tmp_path / "hooks.json"
    src.write_text(
        '{"version": 1, "hooks": [{"id": "j", "event": "session_start", '
        '"run": {"type": "command", "argv": ["/bin/echo", "hi"]}}]}',
        encoding="utf-8",
    )
    file = load_hooks_file(src)
    assert file.hooks[0].run.argv == ["/bin/echo", "hi"]


def test_load_hooks_dir_returns_empty_when_missing(tmp_path: Path) -> None:
    file = load_hooks_dir(tmp_path)
    assert file.hooks == []
    assert file.source == ""


def test_unknown_event_rejected() -> None:
    with pytest.raises(HooksConfigError, match="unknown event"):
        parse_hooks_dict(
            {
                "version": 1,
                "hooks": [{"id": "h", "event": "not_a_real_event", "run": {"type": "command", "argv": ["/bin/true"]}}],
            }
        )


_GUARDS_WITH_A_BAD_REGEX = """\
version: 1
hooks:
  - id: audit
    event: after_tool_call
    run: {type: command, argv: [/bin/true]}
  - id: guard
    event: before_tool_call
    match:
      args:
        command:
          regex: 'rm\\s+(-rf'
    run: {type: command, argv: [/bin/true]}
    execution: {mode: blocking}
  - id: guard-sudo
    event: before_tool_call
    match:
      args:
        command:
          regex: '^sudo\\b'
    run: {type: command, argv: [/bin/true]}
    execution: {mode: blocking}
"""


def _regex_error(pattern: str) -> str:
    with pytest.raises(re.error) as caught:
        re.compile(pattern)
    return str(caught.value)


def test_invalid_match_regex_skips_only_that_hook(tmp_path: Path) -> None:
    src = tmp_path / "hooks.yaml"
    src.write_text(_GUARDS_WITH_A_BAD_REGEX, encoding="utf-8")

    file = load_hooks_file(src)

    assert [hook.id for hook in file.hooks] == ["audit", "guard-sudo"]
    assert file.skipped_hooks == [
        SkippedHook(
            id="guard",
            reason=f"match.args.command.regex is not a valid regular expression: {_regex_error(r'rm\s+(-rf')}",
        )
    ]
    assert file.source == str(src)


@pytest.mark.parametrize(
    ("pattern", "error_type"),
    [
        pytest.param("a{4294967296}", OverflowError, id="repeat-count-overflow"),
        pytest.param("(" * 5000 + ")" * 5000, RecursionError, id="nesting-too-deep"),
    ],
)
def test_regex_the_parser_rejects_outside_re_error_skips_only_that_hook(
    pattern: str, error_type: type[Exception]
) -> None:
    with pytest.raises(error_type) as caught:
        re.compile(pattern)

    file = parse_hooks_dict(
        {
            "version": 1,
            "hooks": [
                {
                    "id": "guard",
                    "event": "before_tool_call",
                    "match": {"args": {"command": {"regex": pattern}}},
                    "run": {"type": "command", "argv": ["/bin/true"]},
                },
                {
                    "id": "guard-sudo",
                    "event": "before_tool_call",
                    "match": {"args": {"command": {"regex": "^sudo "}}},
                    "run": {"type": "command", "argv": ["/bin/true"]},
                },
            ],
        }
    )

    assert [hook.id for hook in file.hooks] == ["guard-sudo"]
    assert file.skipped_hooks == [
        SkippedHook(
            id="guard",
            reason=f"match.args.command.regex is not a valid regular expression: {caught.value}",
        )
    ]


def test_disabled_hook_with_an_invalid_regex_loads_unreported() -> None:
    file = parse_hooks_dict(
        {
            "version": 1,
            "hooks": [
                {
                    "id": "guard",
                    "event": "before_tool_call",
                    "enabled": False,
                    "match": {"args": {"command": {"regex": "rm\\s+(-rf"}}},
                    "run": {"type": "command", "argv": ["/bin/true"]},
                }
            ],
        }
    )

    assert [(hook.id, hook.enabled) for hook in file.hooks] == [("guard", False)]
    assert file.skipped_hooks == []


def test_invalid_regex_does_not_hide_other_errors_in_the_hook() -> None:
    with pytest.raises(HooksConfigError, match=r"^hooks\[0\]: 'enabled' must be a boolean"):
        parse_hooks_dict(
            {
                "version": 1,
                "hooks": [
                    {
                        "id": "guard",
                        "event": "before_tool_call",
                        "enabled": "yes",
                        "match": {"args": {"command": {"regex": "rm\\s+(-rf"}}},
                        "run": {"type": "command", "argv": ["/bin/true"]},
                    }
                ],
            }
        )


def test_unknown_root_keys_rejected() -> None:
    with pytest.raises(HooksConfigError, match="Unknown keys in hooks config root"):
        parse_hooks_dict({"version": 1, "hooks": [], "surprise": True})


def test_unsupported_version_rejected() -> None:
    with pytest.raises(HooksConfigError, match="Unsupported hooks config version"):
        parse_hooks_dict({"version": 2})


def test_duplicate_id_rejected() -> None:
    with pytest.raises(HooksConfigError, match="duplicate id"):
        parse_hooks_dict(
            {
                "version": 1,
                "hooks": [
                    {"id": "dup", "event": "session_start", "run": {"type": "command", "argv": ["/bin/true"]}},
                    {"id": "dup", "event": "session_end", "run": {"type": "command", "argv": ["/bin/true"]}},
                ],
            }
        )


@pytest.mark.parametrize("hook_id", ["x" * 513, "contains\tcontrol"])
def test_hook_id_must_be_a_bounded_printable_trajectory_identifier(hook_id: str) -> None:
    with pytest.raises(HooksConfigError, match="printable and no longer than 512 characters"):
        parse_hooks_dict(
            {
                "version": 1,
                "hooks": [
                    {
                        "id": hook_id,
                        "event": "session_start",
                        "run": {"type": "command", "argv": ["/bin/true"]},
                    }
                ],
            }
        )


def test_blocking_plus_detach_rejected() -> None:
    with pytest.raises(HooksConfigError, match=r"detach=true requires execution\.mode='fire_and_forget'"):
        parse_hooks_dict(
            {
                "version": 1,
                "hooks": [
                    {
                        "id": "x",
                        "event": "before_tool_call",
                        "run": {"type": "command", "argv": ["/bin/true"]},
                        "execution": {"mode": "blocking", "detach": True},
                    }
                ],
            }
        )


def test_script_requires_path() -> None:
    with pytest.raises(HooksConfigError, match=r"run\.path is required"):
        parse_hooks_dict(
            {
                "version": 1,
                "hooks": [{"id": "x", "event": "session_start", "run": {"type": "script"}}],
            }
        )


def test_command_requires_argv() -> None:
    with pytest.raises(HooksConfigError, match=r"run\.argv must be a non-empty list"):
        parse_hooks_dict(
            {
                "version": 1,
                "hooks": [{"id": "x", "event": "session_start", "run": {"type": "command"}}],
            }
        )


def test_shell_requires_string() -> None:
    with pytest.raises(HooksConfigError, match=r"run\.shell is required"):
        parse_hooks_dict(
            {
                "version": 1,
                "hooks": [{"id": "x", "event": "session_start", "run": {"type": "shell"}}],
            }
        )


def test_unknown_keys_rejected() -> None:
    with pytest.raises(HooksConfigError, match="unknown keys"):
        parse_hooks_dict(
            {
                "version": 1,
                "hooks": [
                    {
                        "id": "x",
                        "event": "session_start",
                        "run": {"type": "command", "argv": ["/bin/true"]},
                        "what": "no",
                    }
                ],
            }
        )


def test_negative_timeout_rejected() -> None:
    with pytest.raises(HooksConfigError, match="timeout_seconds must be > 0"):
        parse_hooks_dict(
            {
                "version": 1,
                "hooks": [
                    {
                        "id": "x",
                        "event": "session_start",
                        "run": {"type": "command", "argv": ["/bin/true"]},
                        "execution": {"timeout_seconds": 0},
                    }
                ],
            }
        )


def test_invalid_enabled_type_rejected() -> None:
    with pytest.raises(HooksConfigError, match="'enabled' must be a boolean"):
        parse_hooks_dict(
            {
                "version": 1,
                "hooks": [
                    {
                        "id": "x",
                        "event": "session_start",
                        "enabled": "false",
                        "run": {"type": "command", "argv": ["/bin/true"]},
                    }
                ],
            }
        )


def test_invalid_match_arg_value_type_rejected() -> None:
    with pytest.raises(HooksConfigError, match=r"match\.args\.path\.contains must be a string"):
        parse_hooks_dict(
            {
                "version": 1,
                "hooks": [
                    {
                        "id": "x",
                        "event": "before_tool_call",
                        "run": {"type": "command", "argv": ["/bin/true"]},
                        "match": {"args": {"path": {"contains": 3}}},
                    }
                ],
            }
        )


def test_invalid_settings_types_rejected() -> None:
    with pytest.raises(HooksConfigError, match=r"settings\.max_parallel_hooks must be an integer"):
        parse_hooks_dict(
            {
                "version": 1,
                "settings": {"max_parallel_hooks": "many"},
                "hooks": [],
            }
        )


def test_broad_before_tool_call_hook_warns(caplog: pytest.LogCaptureFixture) -> None:
    parse_hooks_dict(
        {
            "version": 1,
            "hooks": [
                {
                    "id": "global-gate",
                    "event": "before_tool_call",
                    "run": {"type": "command", "argv": ["/bin/true"]},
                }
            ],
        }
    )
    assert "will run for every tool call" in caplog.text


def test_blocking_durable_hook_warns(caplog: pytest.LogCaptureFixture) -> None:
    parse_hooks_dict(
        {
            "version": 1,
            "hooks": [
                {
                    "id": "blocking-durable",
                    "event": "session_end",
                    "run": {"type": "command", "argv": ["/bin/true"]},
                    "execution": {"mode": "blocking", "delivery": "durable"},
                }
            ],
        }
    )
    assert "blocking hooks are awaited inline" in caplog.text


def test_load_hooks_project_no_file_returns_none(tmp_path: Path) -> None:
    assert load_hooks_project(tmp_path) is None


def test_load_hooks_project_yaml(tmp_path: Path) -> None:
    hooks_dir = tmp_path / ".chrys" / "hooks"
    hooks_dir.mkdir(parents=True)
    (hooks_dir / "hooks.yaml").write_text(
        """\
version: 1
hooks:
  - id: project-hook
    event: before_tool_call
    run: { type: command, argv: ["/bin/true"] }
""",
        encoding="utf-8",
    )
    file = load_hooks_project(tmp_path)
    assert file is not None
    assert file.source == str(hooks_dir / "hooks.yaml")
    assert len(file.hooks) == 1
    assert file.hooks[0].id == "project-hook"


def test_load_hooks_project_yml_extension(tmp_path: Path) -> None:
    hooks_dir = tmp_path / ".chrys" / "hooks"
    hooks_dir.mkdir(parents=True)
    (hooks_dir / "hooks.yml").write_text("version: 1\nhooks: []\n", encoding="utf-8")
    file = load_hooks_project(tmp_path)
    assert file is not None
    assert file.source.endswith("hooks.yml")


def test_load_hooks_project_json_extension(tmp_path: Path) -> None:
    hooks_dir = tmp_path / ".chrys" / "hooks"
    hooks_dir.mkdir(parents=True)
    (hooks_dir / "hooks.json").write_text('{"version": 1, "hooks": []}', encoding="utf-8")
    file = load_hooks_project(tmp_path)
    assert file is not None
    assert file.source.endswith("hooks.json")


def test_load_hooks_project_yaml_wins_over_others(tmp_path: Path) -> None:
    hooks_dir = tmp_path / ".chrys" / "hooks"
    hooks_dir.mkdir(parents=True)
    (hooks_dir / "hooks.yaml").write_text("version: 1\nhooks: []\n", encoding="utf-8")
    (hooks_dir / "hooks.yml").write_text("version: 1\nhooks: []\n", encoding="utf-8")
    (hooks_dir / "hooks.json").write_text('{"version": 1, "hooks": []}', encoding="utf-8")
    file = load_hooks_project(tmp_path)
    assert file is not None
    assert file.source.endswith("hooks.yaml")


def test_load_hooks_project_parse_error_raises(tmp_path: Path) -> None:
    hooks_dir = tmp_path / ".chrys" / "hooks"
    hooks_dir.mkdir(parents=True)
    (hooks_dir / "hooks.yaml").write_text(": invalid: yaml: :", encoding="utf-8")
    with pytest.raises(HooksConfigError):
        load_hooks_project(tmp_path)


def _hook(*, id_: str, event: str = "before_tool_call") -> HookConfig:
    from chrys.service.hooks.events import HookEvent

    return HookConfig(
        id=id_,
        event=HookEvent(event),
        run=HookRun(type="command", argv=["/bin/true"]),
    )


def test_hook_helper_builds_real_hook_run() -> None:
    hook = _hook(id_="p1")
    assert isinstance(hook.run, HookRun)


def test_merge_both_none_returns_empty() -> None:
    merged = merge_hooks_files(None, None)
    assert isinstance(merged, MergedHooksFile)
    assert merged.hooks == []
    assert merged.project is None
    assert merged.global_ is None
    assert merged.settings == HookSettings()


def test_merge_project_only() -> None:
    project = HooksFile(hooks=[_hook(id_="p1")], source="/proj/hooks.yaml")
    merged = merge_hooks_files(project, None)
    assert merged.project is project
    assert merged.global_ is None
    assert [h.id for h in merged.hooks] == ["p1"]
    assert merged.sources == ["/proj/hooks.yaml"]


def test_merge_global_only() -> None:
    global_ = HooksFile(hooks=[_hook(id_="g1")], source="/cfg/hooks.yaml")
    merged = merge_hooks_files(None, global_)
    assert merged.project is None
    assert merged.global_ is global_
    assert [h.id for h in merged.hooks] == ["g1"]
    assert merged.sources == ["/cfg/hooks.yaml"]


@pytest.mark.parametrize("project_spelling", ["home", "HOME"])
def test_merge_same_source_file_uses_global_once(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, project_spelling: str
) -> None:
    source = tmp_path / "home" / ".chrys" / "hooks" / "hooks.yaml"
    source.parent.mkdir(parents=True)
    source.write_text("version: 1\nhooks: []\n", encoding="utf-8")
    project_source = tmp_path / project_spelling / ".chrys" / "hooks" / "hooks.yaml"
    if not project_source.exists():
        pytest.skip("Case-sensitive filesystem: the other spelling is another file")
    project = HooksFile(hooks=[_hook(id_="same")], source=str(project_source))
    global_ = HooksFile(hooks=[_hook(id_="same")], source=str(source))

    with caplog.at_level("WARNING", logger="chrys.service.hooks.loader"):
        merged = merge_hooks_files(project, global_)

    assert merged.project is None
    assert merged.global_ is global_
    assert [h.id for h in merged.hooks] == ["same"]
    assert merged.sources == [str(source)]
    assert not caplog.records


def test_merge_both_preserves_project_first_ordering() -> None:
    project = HooksFile(
        hooks=[_hook(id_="p1"), _hook(id_="p2"), _hook(id_="p3")],
        source="/proj/hooks.yaml",
    )
    global_ = HooksFile(
        hooks=[_hook(id_="g1"), _hook(id_="g2")],
        source="/cfg/hooks.yaml",
    )
    merged = merge_hooks_files(project, global_)
    assert [h.id for h in merged.hooks] == ["p1", "p2", "p3", "g1", "g2"]
    assert merged.sources == ["/proj/hooks.yaml", "/cfg/hooks.yaml"]


def test_merge_same_id_keeps_both_no_dedup() -> None:
    project = HooksFile(hooks=[_hook(id_="lint")], source="/proj/hooks.yaml")
    global_ = HooksFile(hooks=[_hook(id_="lint")], source="/cfg/hooks.yaml")
    merged = merge_hooks_files(project, global_)
    assert [h.id for h in merged.hooks] == ["lint", "lint"]


def test_merge_same_id_emits_warning(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    project = HooksFile(hooks=[_hook(id_="lint")], source="/proj/hooks.yaml")
    global_ = HooksFile(hooks=[_hook(id_="lint")], source="/cfg/hooks.yaml")
    with caplog.at_level("WARNING", logger="chrys.service.hooks.loader"):
        merge_hooks_files(project, global_)
    assert any("lint" in record.message and "/proj/hooks.yaml" in record.message for record in caplog.records)


def test_merge_settings_project_wins() -> None:
    project = HooksFile(
        hooks=[],
        source="/proj/hooks.yaml",
        settings=HookSettings(shutdown_grace_seconds=10.0),
        settings_overrides={"shutdown_grace_seconds"},
    )
    global_ = HooksFile(
        hooks=[],
        source="/cfg/hooks.yaml",
        settings=HookSettings(shutdown_grace_seconds=5.0),
        settings_overrides={"shutdown_grace_seconds"},
    )
    merged = merge_hooks_files(project, global_)
    assert merged.settings.shutdown_grace_seconds == 10.0


def test_merge_settings_global_fallback() -> None:
    project = HooksFile(hooks=[], source="/proj/hooks.yaml")
    global_ = HooksFile(
        hooks=[],
        source="/cfg/hooks.yaml",
        settings=HookSettings(shutdown_grace_seconds=5.0),
        settings_overrides={"shutdown_grace_seconds"},
    )
    merged = merge_hooks_files(project, global_)
    assert merged.settings.shutdown_grace_seconds == 5.0


def test_merge_settings_project_explicit_default_overrides_global() -> None:
    project = HooksFile(
        hooks=[],
        source="/proj/hooks.yaml",
        settings=HookSettings(shutdown_grace_seconds=5.0),
        settings_overrides={"shutdown_grace_seconds"},
    )
    global_ = HooksFile(
        hooks=[],
        source="/cfg/hooks.yaml",
        settings=HookSettings(shutdown_grace_seconds=9.0),
        settings_overrides={"shutdown_grace_seconds"},
    )
    merged = merge_hooks_files(project, global_)
    assert merged.settings.shutdown_grace_seconds == 5.0


def test_merge_settings_default_fallback() -> None:
    merged = merge_hooks_files(
        HooksFile(source="/proj/hooks.yaml"),
        HooksFile(source="/cfg/hooks.yaml"),
    )
    assert merged.settings.shutdown_grace_seconds == 5.0
    assert merged.settings.max_parallel_hooks == 4
