# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Smoke / integration tests for the project-level hooks feature.

Exercises the end-to-end flow: a project hooks file on disk + a
global hooks file on disk + the ``merge_hooks_files`` loader function
producing a ``MergedHooksFile`` that ``HookManager`` consumes.

These tests do not run a full engine.start(); they construct the
manager directly with a synthetic merged file. End-to-end coverage
of the call-site wiring belongs in tests/integration/engine/ if
needed — the existing project doesn't end-to-end test the global
hook path either, so we match that depth.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
import yaml

from chrys.service.hooks.events import HookEvent
from chrys.service.hooks.loader import (
    HooksConfigError,
    load_hooks_dir,
    load_workdir_hooks,
    merge_hooks_files,
)
from chrys.service.hooks.manager import HookManager
from chrys.service.hooks.schema import HookConfig, HookExecution, HookRun, HooksFile, MergedHooksFile


def _hook_config(*, id_: str, event: str, command: str = "/bin/true") -> HookConfig:
    return HookConfig(
        id=id_,
        event=HookEvent(event),
        run=HookRun(type="command", argv=[command]),
    )


def _write_hooks_file(path: Path, hooks: list[HookConfig]) -> Path:
    hooks_dir = path / "hooks"
    hooks_dir.mkdir(parents=True, exist_ok=True)
    target = hooks_dir / "hooks.yaml"
    data = {
        "version": 1,
        "hooks": [
            {
                "id": h.id,
                "event": str(h.event),
                "run": {"type": "command", "argv": h.run.argv},
                "execution": {
                    "mode": h.execution.mode,
                    "detach": h.execution.detach,
                    "delivery": h.execution.delivery,
                    "timeout_seconds": h.execution.timeout_seconds,
                    "on_error": h.execution.on_error,
                },
            }
            for h in hooks
        ],
    }
    target.write_text(yaml.safe_dump(data), encoding="utf-8")
    return target


def _build_manager(
    tmp_path: Path,
    workdir_hooks: list[HookConfig] | None,
    global_hooks: list[HookConfig] | None,
) -> HookManager | None:
    workdir = tmp_path / "workdir"
    config_dir = tmp_path / "config"
    workdir.mkdir(parents=True, exist_ok=True)
    config_dir.mkdir(parents=True, exist_ok=True)

    workdir_file: HooksFile | None = None
    if workdir_hooks is not None:
        path = _write_hooks_file(workdir / ".chrys", workdir_hooks)
        workdir_file = load_workdir_hooks(workdir)
        assert workdir_file is not None
        assert workdir_file.source == str(path)

    global_file: HooksFile | None = None
    if global_hooks is not None:
        _write_hooks_file(config_dir, global_hooks)
        global_file = load_hooks_dir(config_dir)
        assert global_file is not None

    merged = merge_hooks_files(workdir=workdir_file, global_=global_file)
    if not merged.hooks:
        return None
    return HookManager(file=merged, hooks_dir=config_dir / "hooks")


def test_workdir_only_no_global(tmp_path: Path) -> None:
    mgr = _build_manager(
        tmp_path,
        workdir_hooks=[_hook_config(id_="p1", event="before_tool_call")],
        global_hooks=None,
    )
    assert mgr is not None
    assert mgr.sources()
    assert len(mgr.file.hooks) == 1


def test_global_only_no_workdir(tmp_path: Path) -> None:
    mgr = _build_manager(
        tmp_path,
        workdir_hooks=None,
        global_hooks=[_hook_config(id_="g1", event="before_tool_call")],
    )
    assert mgr is not None
    assert len(mgr.file.hooks) == 1


def test_neither_returns_none(tmp_path: Path) -> None:
    mgr = _build_manager(tmp_path, None, None)
    assert mgr is None


def test_both_no_collision(tmp_path: Path) -> None:
    mgr = _build_manager(
        tmp_path,
        workdir_hooks=[_hook_config(id_="p1", event="before_tool_call")],
        global_hooks=[_hook_config(id_="g1", event="before_tool_call")],
    )
    assert mgr is not None
    assert [h.id for h in mgr.file.hooks] == ["p1", "g1"]
    assert len(mgr.sources()) == 2


def test_same_id_both_run_with_workdir_first(tmp_path: Path) -> None:
    mgr = _build_manager(
        tmp_path,
        workdir_hooks=[_hook_config(id_="lint", event="before_tool_call")],
        global_hooks=[_hook_config(id_="lint", event="before_tool_call")],
    )
    assert mgr is not None
    assert [h.id for h in mgr.file.hooks] == ["lint", "lint"]


@pytest.mark.asyncio
async def test_blocking_event_workdir_block_short_circuits_global(
    tmp_path: Path,
) -> None:
    marker = tmp_path / "global-ran.txt"
    mgr = _build_manager(
        tmp_path,
        workdir_hooks=[
            HookConfig(
                id="guard",
                event=HookEvent.BEFORE_TOOL_CALL,
                run=HookRun(
                    type="command",
                    argv=[
                        sys.executable,
                        "-c",
                        (
                            "import json, os, pathlib; "
                            'pathlib.Path(os.environ["CHRYS_HOOK_RESULT"]).write_text('
                            'json.dumps({"action": "block", "reason": "project denied"}), encoding="utf-8")'
                        ),
                    ],
                ),
                execution=HookExecution(mode="blocking", timeout_seconds=5),
            )
        ],
        global_hooks=[
            HookConfig(
                id="global-after-block",
                event=HookEvent.BEFORE_TOOL_CALL,
                run=HookRun(
                    type="command",
                    argv=[
                        sys.executable,
                        "-c",
                        f'import pathlib; pathlib.Path(r"{marker}").write_text("ran", encoding="utf-8")',
                    ],
                ),
                execution=HookExecution(mode="blocking", timeout_seconds=5),
            )
        ],
    )
    assert mgr is not None
    decision = await mgr.fire(HookEvent.BEFORE_TOOL_CALL, {"cwd": str(tmp_path), "session_id": "s1"})
    assert decision.blocked is True
    assert "project denied" in decision.block_reason
    assert not marker.exists()


def test_workdir_parse_error_raises(tmp_path: Path) -> None:
    workdir = tmp_path / "workdir"
    hooks_dir = workdir / ".chrys" / "hooks"
    hooks_dir.mkdir(parents=True)
    (hooks_dir / "hooks.yaml").write_text(": bad: yaml: :", encoding="utf-8")

    with pytest.raises(HooksConfigError):
        load_workdir_hooks(workdir)


def test_relative_path_resolves_to_workdir(tmp_path: Path) -> None:
    workdir = tmp_path / "workdir"
    scripts_dir = workdir / ".chrys" / "hooks" / "scripts"
    scripts_dir.mkdir(parents=True)
    (scripts_dir / "foo.py").write_text("#!/usr/bin/env python3\n")
    (workdir / ".chrys" / "hooks" / "hooks.yaml").write_text(
        """\
version: 1
hooks:
  - id: project-script
    event: before_tool_call
    run:
      type: script
      path: scripts/foo.py
""",
        encoding="utf-8",
    )
    file = load_workdir_hooks(workdir)
    assert file is not None
    assert file.hooks[0].run.path == str((scripts_dir / "foo.py").resolve())


def test_settings_merge_global_default_retained_when_workdir_settings_missing(tmp_path: Path) -> None:
    workdir = tmp_path / "workdir"
    config_dir = tmp_path / "config"
    workdir.mkdir()
    config_dir.mkdir()
    _write_hooks_file(
        workdir / ".chrys",
        [_hook_config(id_="p1", event="before_tool_call")],
    )
    (config_dir / "hooks").mkdir(parents=True)
    (config_dir / "hooks" / "hooks.yaml").write_text(
        """\
version: 1
settings:
  shutdown_grace_seconds: 5.0
hooks:
  - id: g1
    event: before_tool_call
    run: { type: command, argv: ["/bin/true"] }
""",
        encoding="utf-8",
    )
    workdir_file = load_workdir_hooks(workdir)
    global_file = load_hooks_dir(config_dir)
    assert workdir_file is not None and global_file is not None
    merged = merge_hooks_files(workdir=workdir_file, global_=global_file)
    assert merged.settings.shutdown_grace_seconds == 5.0


def test_settings_merge_workdir_missing_settings_uses_global(tmp_path: Path) -> None:
    workdir = tmp_path / "workdir"
    config_dir = tmp_path / "config"
    workdir.mkdir()
    config_dir.mkdir()
    _write_hooks_file(
        workdir / ".chrys",
        [_hook_config(id_="p1", event="before_tool_call")],
    )
    (config_dir / "hooks").mkdir(parents=True)
    (config_dir / "hooks" / "hooks.yaml").write_text(
        """\
version: 1
settings:
  shutdown_grace_seconds: 9.0
hooks:
  - id: g1
    event: before_tool_call
    run: { type: command, argv: ["/bin/true"] }
""",
        encoding="utf-8",
    )
    workdir_file = load_workdir_hooks(workdir)
    global_file = load_hooks_dir(config_dir)
    assert workdir_file is not None and global_file is not None
    merged = merge_hooks_files(workdir=workdir_file, global_=global_file)
    assert merged.settings.shutdown_grace_seconds == 9.0


def test_workdir_root_is_primary_cwd(tmp_path: Path) -> None:
    fake_root = tmp_path / "fake_root"
    real_root = tmp_path / "real_root"
    fake_root.mkdir()
    real_root.mkdir()
    hooks_dir = fake_root / ".chrys" / "hooks"
    hooks_dir.mkdir(parents=True)
    (hooks_dir / "hooks.yaml").write_text("version: 1\nhooks: []\n", encoding="utf-8")
    file = load_workdir_hooks(fake_root)
    assert file is not None
    assert file.source == str(hooks_dir / "hooks.yaml")

    assert load_workdir_hooks(real_root) is None


def test_restore_uses_session_primary_cwd(tmp_path: Path) -> None:
    old = tmp_path / "old"
    new = tmp_path / "new"
    old.mkdir()
    new.mkdir()
    (old / ".chrys" / "hooks").mkdir(parents=True)
    (old / ".chrys" / "hooks" / "hooks.yaml").write_text("version: 1\nhooks: []\n", encoding="utf-8")
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.chdir(new)
    try:
        file = load_workdir_hooks(old)
    finally:
        monkeypatch.undo()
    assert file is not None
    assert file.source.startswith(str(old))


def test_enabled_false_honored_cross_file(tmp_path: Path) -> None:
    workdir = tmp_path / "workdir"
    config_dir = tmp_path / "config"
    workdir.mkdir()
    config_dir.mkdir()
    _write_hooks_file(
        workdir / ".chrys",
        [_hook_config(id_="p1", event="before_tool_call")],
    )
    (config_dir / "hooks").mkdir(parents=True)
    (config_dir / "hooks" / "hooks.yaml").write_text(
        """\
version: 1
hooks:
  - id: g1
    event: before_tool_call
    enabled: false
    run: { type: command, argv: ["/bin/true"] }
""",
        encoding="utf-8",
    )
    workdir_file = load_workdir_hooks(workdir)
    global_file = load_hooks_dir(config_dir)
    assert workdir_file is not None and global_file is not None
    merged = merge_hooks_files(workdir=workdir_file, global_=global_file)
    assert [h.id for h in merged.hooks] == ["p1", "g1"]
    assert merged.hooks[1].enabled is False


def test_ordering_preserved(tmp_path: Path) -> None:
    mgr = _build_manager(
        tmp_path,
        workdir_hooks=[_hook_config(id_=f"p{i}", event="before_tool_call") for i in range(3)],
        global_hooks=[_hook_config(id_=f"g{i}", event="before_tool_call") for i in range(5)],
    )
    assert mgr is not None
    assert [h.id for h in mgr.file.hooks] == [
        "p0",
        "p1",
        "p2",
        "g0",
        "g1",
        "g2",
        "g3",
        "g4",
    ]


def test_same_id_warning_logged(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    import logging

    _build_manager(
        tmp_path,
        workdir_hooks=[_hook_config(id_="lint", event="before_tool_call")],
        global_hooks=[_hook_config(id_="lint", event="before_tool_call")],
    )
    with caplog.at_level(logging.WARNING, logger="chrys.service.hooks.loader"):
        workdir_file = load_workdir_hooks(tmp_path / "workdir")
        global_file = load_hooks_dir(tmp_path / "config")
        merge_hooks_files(workdir=workdir_file, global_=global_file)
    assert any("lint" in record.message for record in caplog.records), (
        f"expected warning about 'lint' collision, got: {[r.message for r in caplog.records]}"
    )


def test_modify_flows_workdir_to_global(tmp_path: Path) -> None:
    workdir = tmp_path / "workdir"
    config_dir = tmp_path / "config"
    workdir.mkdir()
    config_dir.mkdir()
    _write_hooks_file(
        workdir / ".chrys",
        [_hook_config(id_="rewrite", event="before_tool_call")],
    )
    (config_dir / "hooks").mkdir(parents=True)
    (config_dir / "hooks" / "hooks.yaml").write_text(
        """\
version: 1
hooks:
  - id: rewrite
    event: before_tool_call
    run: { type: command, argv: ["/bin/true"] }
""",
        encoding="utf-8",
    )
    workdir_file = load_workdir_hooks(workdir)
    global_file = load_hooks_dir(config_dir)
    assert workdir_file is not None and global_file is not None
    merged = merge_hooks_files(workdir=workdir_file, global_=global_file)
    assert [h.id for h in merged.hooks] == ["rewrite", "rewrite"]


def test_manager_takes_only_merged_hooks_file(tmp_path: Path) -> None:
    from chrys.service.hooks.manager import HookManager
    from chrys.service.hooks.schema import MergedHooksFile

    plain = HooksFile(
        hooks=[_hook_config(id_="g1", event="before_tool_call")],
        source="/some/global/hooks.yaml",
    )
    merged = MergedHooksFile.from_single(plain)
    mgr = HookManager(file=merged, hooks_dir=tmp_path / "hooks")
    assert mgr.file is merged
    assert mgr.sources() == ["/some/global/hooks.yaml"]
    assert [h.id for h in mgr.file.hooks] == ["g1"]


def test_from_single_preserves_hooks_and_settings(tmp_path: Path) -> None:
    from chrys.service.hooks.schema import HookSettings, HooksFile, MergedHooksFile

    plain = HooksFile(
        hooks=[_hook_config(id_="h1", event="before_tool_call")],
        source="/x.yaml",
        settings=HookSettings(shutdown_grace_seconds=7.0),
    )
    wrapped = MergedHooksFile.from_single(plain)
    assert [h.id for h in wrapped.hooks] == ["h1"]
    assert wrapped.settings.shutdown_grace_seconds == 7.0
    assert wrapped.sources == ["/x.yaml"]


def test_from_single_as_workdir_flag(tmp_path: Path) -> None:
    from chrys.service.hooks.schema import HooksFile, MergedHooksFile

    plain = HooksFile(
        hooks=[_hook_config(id_="p1", event="before_tool_call")],
        source="/proj.yaml",
    )
    wrapped = MergedHooksFile.from_single(plain, as_workdir=True)
    assert wrapped.workdir is plain
    assert wrapped.global_ is None
    assert wrapped.sources == ["/proj.yaml"]


def test_merged_source_omits_empty_source_segments() -> None:
    wrapped = MergedHooksFile(
        workdir=HooksFile(source=""),
        global_=HooksFile(source="/cfg/hooks.yaml"),
    )
    assert wrapped.source == "/cfg/hooks.yaml"
