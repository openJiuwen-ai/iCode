# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""YAML / JSON loader for ``hooks.yaml`` (or ``hooks.json``).

The two formats parse to the same intermediate dict, so we sniff by
extension only (no auto-format-detection by content — easier to debug,
and config files almost always have the right extension).

Validation is fail-fast: a malformed file raises
:class:`HooksConfigError` with a path and a line-pointer-ish message
("``hooks[0].execution.mode``").  Tests rely on the exact path strings,
so don't reformat them casually.  The one exception is a well-formed hook
whose ``match`` regex does not compile: that hook alone is left out and
recorded in :attr:`HooksFile.skipped_hooks`, and the rest of the file loads.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import asdict, fields, replace
from pathlib import Path
from typing import Any, cast

import yaml

from chrys.foundation.branding import APP_DISPLAY_NAME
from chrys.foundation.text.encoding import decode_bytes
from chrys.foundation.tool_kinds import strip_legacy_kind_prefix
from chrys.foundation.trajectory.ids import OPAQUE_ID_MAX_LENGTH, is_valid_opaque_id
from chrys.service.hooks.events import HookEvent
from chrys.service.hooks.matcher import REGEX_COMPILE_ERRORS
from chrys.service.hooks.schema import (
    HookArgMatch,
    HookConfig,
    HookExecution,
    HookMatch,
    HookRun,
    HookSettings,
    HooksFile,
    MergedHooksFile,
    SkippedHook,
)

logger = logging.getLogger(__name__)


class HooksConfigError(ValueError):
    """Raised on any structural / semantic problem in the hooks config."""


# ---------------------------------------------------------------------------
# Entry points
# ---------------------------------------------------------------------------


def load_hooks_file(path: Path) -> HooksFile:
    """Load *path* and return the parsed :class:`HooksFile`.

    Args:
        path: Absolute path to ``hooks.yaml`` or ``hooks.json``.

    Raises:
        FileNotFoundError: *path* does not exist.
        HooksConfigError: The file is structurally invalid.
    """
    if not path.exists():
        msg = f"Hooks config file not found: {path}"
        raise FileNotFoundError(msg)

    try:
        text = decode_bytes(path.read_bytes(), errors="strict")
    except UnicodeError as exc:
        raise HooksConfigError(f"Cannot decode hooks config file {path}: {exc}") from exc
    suffix = path.suffix.lower()
    try:
        raw = (json.loads(text) if text.strip() else {}) if suffix == ".json" else (yaml.safe_load(text) or {})
    except (yaml.YAMLError, json.JSONDecodeError) as exc:
        msg = f"Failed to parse {path}: {exc}"
        raise HooksConfigError(msg) from exc

    file = parse_hooks_dict(raw)
    _normalize_script_paths(file, base_dir=path.parent)
    file.source = str(path)
    return file


def load_hooks_dir(config_dir: Path) -> HooksFile:
    """Load ``<config_dir>/hooks/hooks.{yaml,json}`` if present.

    Returns an empty :class:`HooksFile` when no config is found.  Runtime
    callers should treat ``source == ""`` and ``hooks == []`` as "hooks are
    disabled" so the no-config path does not create outbox/log/tmp
    directories.
    """
    hooks_dir = config_dir / "hooks"
    for name in ("hooks.yaml", "hooks.yml", "hooks.json"):
        path = hooks_dir / name
        if path.exists():
            return load_hooks_file(path)
    return HooksFile()


def load_hooks_project(project_root: Path) -> HooksFile | None:
    """Load ``<project_root>/.chrys/hooks/hooks.{yaml,yml,json}`` if present.

    Mirrors :func:`load_hooks_dir` for the project tree. Returns ``None``
    when no project file is found. Per-file parse and IO errors raise
    so the caller (the engine start path) can decide whether to warn-
    and-continue or fail hard.

    The file-naming precedence matches :func:`load_hooks_dir`:
    ``hooks.yaml`` > ``hooks.yml`` > ``hooks.json``.
    """
    hooks_dir = project_root / ".chrys" / "hooks"
    for name in ("hooks.yaml", "hooks.yml", "hooks.json"):
        path = hooks_dir / name
        if path.exists():
            return load_hooks_file(path)
    return None


def merge_hooks_files(
    project: HooksFile | None,
    global_: HooksFile | None,
) -> MergedHooksFile:
    """Combine a project-level and a global-level :class:`HooksFile`.

    Rules:

    - ``hooks`` ordering: project entries first (in project YAML order),
      then global entries (in global YAML order). No id-based dedup
      across files; a same-id collision is allowed and both hooks run
      (project first), with a ``WARNING`` logged at load time.
    - ``settings``: per-field merge. A field set in the project file
      wins; otherwise the global field; otherwise the dataclass default.
    - ``sources``: list of included source paths in load order.
    - Either input may be ``None`` (file not found). The result is still
      a valid :class:`MergedHooksFile`.

    The "first block short-circuits" rule applies to the combined
    ``hooks`` list as a single chain, so a project block ends the
    chain before any global hook runs.
    """
    if _same_source_file(project, global_):
        project = None

    settings = _merge_settings(project, global_)
    hooks: list[HookConfig] = []
    if project is not None:
        hooks.extend(project.hooks)
    if global_ is not None:
        hooks.extend(global_.hooks)
    sources: list[str] = []
    if project is not None and project.source:
        sources.append(project.source)
    if global_ is not None and global_.source:
        sources.append(global_.source)
    _warn_on_id_collisions(project, global_)
    return MergedHooksFile(
        project=project,
        global_=global_,
        settings=settings,
        hooks=hooks,
        sources=sources,
    )


def _same_source_file(project: HooksFile | None, global_: HooksFile | None) -> bool:
    """Return True when project/global inputs came from the same hooks file."""
    if project is None or global_ is None or not project.source or not global_.source:
        return False
    try:
        # By identity: on a caseless disk two spellings of one file differ in case.
        return Path(project.source).samefile(global_.source)
    except OSError:
        return Path(project.source).resolve() == Path(global_.source).resolve()


def _merge_settings(project: HooksFile | None, global_: HooksFile | None) -> HookSettings:
    """Per-field merge: project wins, then global, then defaults.

    Only settings explicitly present in each source file participate in the
    merge; implicit dataclass defaults from an omitted ``settings`` block must
    not overwrite values from another source.
    """
    base = HookSettings()
    if global_ is not None:
        base = _apply_explicit_settings(base, global_.settings, global_.settings_overrides)
    if project is not None:
        base = _apply_explicit_settings(base, project.settings, project.settings_overrides)
    return base


def _apply_explicit_settings(base: HookSettings, settings: HookSettings, explicit_fields: set[str]) -> HookSettings:
    """Copy only explicitly configured settings fields onto *base*."""
    if not explicit_fields:
        return base
    setting_values = asdict(settings)
    return replace(base, **{name: setting_values[name] for name in explicit_fields})


def _warn_on_id_collisions(
    project: HooksFile | None,
    global_: HooksFile | None,
) -> None:
    """Emit a WARNING for each id that appears in both files.

    Same-id hooks are kept (the layered semantic requires both to run);
    this warning is purely informational. To suppress a global hook,
    set ``enabled: false`` in the global file.
    """
    if project is None or global_ is None:
        return
    project_ids = {h.id for h in project.hooks if h.id}
    global_ids = {h.id for h in global_.hooks if h.id}
    collisions = project_ids & global_ids
    if not collisions:
        return
    for hook_id in sorted(collisions):
        logger.warning(
            "Hook id %r is defined in both project (%s) and global (%s) "
            "hooks configs. Both will run, project first. To suppress the "
            "global hook, set 'enabled: false' in the global file.",
            hook_id,
            project.source,
            global_.source,
        )


def _normalize_script_paths(file: HooksFile, *, base_dir: Path) -> None:
    """Resolve relative ``type: script`` paths against the hook file directory."""
    for hook in file.hooks:
        if hook.run.type != "script":
            continue
        script_path = Path(hook.run.path)
        if script_path.is_absolute():
            continue
        hook.run.path = str((base_dir / script_path).resolve())


# ---------------------------------------------------------------------------
# Dict → dataclass
# ---------------------------------------------------------------------------


def parse_hooks_dict(raw: Any) -> HooksFile:
    """Convert a raw parsed mapping into a :class:`HooksFile`.

    Exposed separately from :func:`load_hooks_file` so callers (and
    tests) can drive the loader without writing temp files.
    """
    if raw is None:
        return HooksFile()
    if not isinstance(raw, dict):
        msg = f"Hooks config root must be a mapping, got {type(raw).__name__}"
        raise HooksConfigError(msg)

    valid_root = {"version", "settings", "hooks"}
    extra_root = cast("set[str]", set(raw) - valid_root)
    if extra_root:
        msg = f"Unknown keys in hooks config root: {sorted(extra_root)}"
        raise HooksConfigError(msg)

    version = raw.get("version", 1)
    if version != 1:
        msg = f"Unsupported hooks config version: {version!r} (this build supports version 1)"
        raise HooksConfigError(msg)

    raw_settings = raw.get("settings")
    settings = _parse_settings(raw_settings)
    hooks, skipped_hooks = _parse_hooks_list(raw.get("hooks", []))
    settings_overrides = set() if raw_settings is None else set(raw_settings)
    return HooksFile(
        version=version,
        settings=settings,
        settings_overrides=settings_overrides,
        hooks=hooks,
        skipped_hooks=skipped_hooks,
    )


def _parse_settings(raw: Any) -> HookSettings:
    if raw is None:
        return HookSettings()
    if not isinstance(raw, dict):
        msg = f"'settings' must be a mapping, got {type(raw).__name__}"
        raise HooksConfigError(msg)
    valid = {f.name for f in fields(HookSettings)}
    extra = cast("set[str]", set(raw) - valid)
    if extra:
        msg = f"Unknown keys in 'settings': {sorted(extra)}"
        raise HooksConfigError(msg)

    defaults = HookSettings()
    shutdown_grace = _float_field(
        raw.get("shutdown_grace_seconds", defaults.shutdown_grace_seconds),
        "settings.shutdown_grace_seconds",
    )
    if shutdown_grace < 0:
        msg = f"settings.shutdown_grace_seconds must be >= 0, got {shutdown_grace}"
        raise HooksConfigError(msg)

    max_parallel = _int_field(raw.get("max_parallel_hooks", defaults.max_parallel_hooks), "settings.max_parallel_hooks")
    if max_parallel <= 0:
        msg = f"settings.max_parallel_hooks must be > 0, got {max_parallel}"
        raise HooksConfigError(msg)

    retry_age = _float_field(
        raw.get("outbox_retry_age_seconds", defaults.outbox_retry_age_seconds), "settings.outbox_retry_age_seconds"
    )
    if retry_age < 0:
        msg = f"settings.outbox_retry_age_seconds must be >= 0, got {retry_age}"
        raise HooksConfigError(msg)

    max_retries = _int_field(raw.get("outbox_max_retries", defaults.outbox_max_retries), "settings.outbox_max_retries")
    if max_retries < 0:
        msg = f"settings.outbox_max_retries must be >= 0, got {max_retries}"
        raise HooksConfigError(msg)

    return HookSettings(
        shutdown_grace_seconds=shutdown_grace,
        max_parallel_hooks=max_parallel,
        outbox_retry_age_seconds=retry_age,
        outbox_max_retries=max_retries,
    )


def _parse_hooks_list(raw: Any) -> tuple[list[HookConfig], list[SkippedHook]]:
    if not isinstance(raw, list):
        msg = f"'hooks' must be a list, got {type(raw).__name__}"
        raise HooksConfigError(msg)
    out: list[HookConfig] = []
    skipped: list[SkippedHook] = []
    seen_ids: set[str] = set()
    for i, item in enumerate(raw):
        try:
            cfg = _parse_one_hook(item)
        except HooksConfigError as exc:
            msg = f"hooks[{i}]: {exc}"
            raise HooksConfigError(msg) from exc
        if cfg.id in seen_ids:
            msg = f"hooks[{i}]: duplicate id '{cfg.id}'"
            raise HooksConfigError(msg)
        seen_ids.add(cfg.id)
        reason = _invalid_match_regex(cfg.match) if cfg.enabled else None
        if reason is not None:
            skipped.append(SkippedHook(id=cfg.id, reason=reason))
            continue
        out.append(cfg)
    return out, skipped


def _invalid_match_regex(match: HookMatch) -> str | None:
    """Why *match* can never match: a regex that does not compile, or None.

    The matcher treats such a condition as a miss, which would silently
    switch off a blocking hook; the loader skips the hook and reports it
    instead.
    """
    for name, arg in match.args.items():
        if arg.regex is None:
            continue
        try:
            re.compile(arg.regex)
        except REGEX_COMPILE_ERRORS as exc:
            return f"match.args.{name}.regex is not a valid regular expression: {exc}"
    return None


def _parse_one_hook(raw: Any) -> HookConfig:
    if not isinstance(raw, dict):
        msg = f"hook entry must be a mapping, got {type(raw).__name__}"
        raise HooksConfigError(msg)

    valid_top = {"id", "event", "run", "execution", "match", "enabled", "description"}
    extra = cast("set[str]", set(raw) - valid_top)
    if extra:
        msg = f"unknown keys: {sorted(extra)}"
        raise HooksConfigError(msg)

    hook_id = raw.get("id")
    if not isinstance(hook_id, str) or not hook_id.strip():
        msg = "'id' is required and must be a non-empty string"
        raise HooksConfigError(msg)
    hook_id = hook_id.strip()
    if not is_valid_opaque_id(hook_id):
        msg = f"'id' must be printable and no longer than {OPAQUE_ID_MAX_LENGTH} characters"
        raise HooksConfigError(msg)

    event_raw = raw.get("event")
    if not isinstance(event_raw, str):
        msg = "'event' is required and must be a string"
        raise HooksConfigError(msg)
    try:
        event = HookEvent(event_raw)
    except ValueError as exc:
        valid_events = ", ".join(sorted(e.value for e in HookEvent))
        msg = f"unknown event '{event_raw}'.  Valid: {valid_events}"
        raise HooksConfigError(msg) from exc

    run = _parse_run(raw.get("run"))
    execution = _parse_execution(raw.get("execution"))
    match = _parse_match(raw.get("match"))

    _validate_run_execution_combo(execution, hook_id=hook_id)
    _warn_on_blocking_durable_hook(hook_id=hook_id, execution=execution)
    _warn_on_broad_before_tool_hook(hook_id=hook_id, event=event, match=match)

    enabled = raw.get("enabled", True)
    if not isinstance(enabled, bool):
        msg = f"'enabled' must be a boolean, got {type(enabled).__name__}"
        raise HooksConfigError(msg)

    description = "" if raw.get("description") is None else raw.get("description", "")
    if not isinstance(description, str):
        msg = f"'description' must be a string, got {type(description).__name__}"
        raise HooksConfigError(msg)

    return HookConfig(
        id=hook_id,
        event=event,
        run=run,
        execution=execution,
        match=match,
        enabled=enabled,
        description=description,
    )


def _parse_run(raw: Any) -> HookRun:
    if raw is None:
        msg = "'run' is required"
        raise HooksConfigError(msg)
    if not isinstance(raw, dict):
        msg = f"'run' must be a mapping, got {type(raw).__name__}"
        raise HooksConfigError(msg)

    valid = {f.name for f in fields(HookRun)}
    extra = cast("set[str]", set(raw) - valid)
    if extra:
        msg = f"run: unknown keys: {sorted(extra)}"
        raise HooksConfigError(msg)

    run_type = raw.get("type")
    if run_type not in ("script", "command", "shell"):
        msg = f"run.type must be 'script', 'command', or 'shell', got {run_type!r}"
        raise HooksConfigError(msg)

    path = "" if raw.get("path") is None else raw.get("path", "")
    if not isinstance(path, str):
        msg = f"run.path must be a string, got {type(path).__name__}"
        raise HooksConfigError(msg)
    argv = [] if raw.get("argv") is None else raw.get("argv", [])
    shell = "" if raw.get("shell") is None else raw.get("shell", "")
    if not isinstance(shell, str):
        msg = f"run.shell must be a string, got {type(shell).__name__}"
        raise HooksConfigError(msg)

    if run_type == "script" and not path:
        msg = "run.path is required when run.type == 'script'"
        raise HooksConfigError(msg)
    if run_type == "command":
        if not isinstance(argv, list) or not argv:
            msg = "run.argv must be a non-empty list when run.type == 'command'"
            raise HooksConfigError(msg)
        if not all(isinstance(a, str) for a in argv):
            msg = "run.argv entries must all be strings"
            raise HooksConfigError(msg)
    if run_type == "shell" and not shell:
        msg = "run.shell is required when run.type == 'shell'"
        raise HooksConfigError(msg)

    args = [] if raw.get("args") is None else raw.get("args", [])
    if not isinstance(args, list) or not all(isinstance(a, str) for a in args):
        msg = "run.args must be a list of strings"
        raise HooksConfigError(msg)

    env = {} if raw.get("env") is None else raw.get("env", {})
    if not isinstance(env, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in env.items()):
        msg = "run.env must be a string→string mapping"
        raise HooksConfigError(msg)

    cwd = "" if raw.get("cwd") is None else raw.get("cwd", "")
    if not isinstance(cwd, str):
        msg = f"run.cwd must be a string, got {type(cwd).__name__}"
        raise HooksConfigError(msg)

    return HookRun(type=run_type, path=path, argv=list(argv), shell=shell, args=list(args), env=dict(env), cwd=cwd)


def _parse_execution(raw: Any) -> HookExecution:
    if raw is None:
        return HookExecution()
    if not isinstance(raw, dict):
        msg = f"'execution' must be a mapping, got {type(raw).__name__}"
        raise HooksConfigError(msg)
    valid = {f.name for f in fields(HookExecution)}
    extra = cast("set[str]", set(raw) - valid)
    if extra:
        msg = f"execution: unknown keys: {sorted(extra)}"
        raise HooksConfigError(msg)

    mode = raw.get("mode", "fire_and_forget")
    if mode not in ("blocking", "async", "fire_and_forget"):
        msg = f"execution.mode must be 'blocking' / 'async' / 'fire_and_forget', got {mode!r}"
        raise HooksConfigError(msg)

    delivery = raw.get("delivery", "best_effort")
    if delivery not in ("best_effort", "durable"):
        msg = f"execution.delivery must be 'best_effort' / 'durable', got {delivery!r}"
        raise HooksConfigError(msg)

    on_error = raw.get("on_error", "warn")
    if on_error not in ("block", "warn", "ignore"):
        msg = f"execution.on_error must be 'block' / 'warn' / 'ignore', got {on_error!r}"
        raise HooksConfigError(msg)

    timeout = _float_field(raw.get("timeout_seconds", HookExecution.timeout_seconds), "execution.timeout_seconds")
    if timeout <= 0:
        msg = f"execution.timeout_seconds must be > 0, got {timeout}"
        raise HooksConfigError(msg)

    detach = raw.get("detach", False)
    if not isinstance(detach, bool):
        msg = f"execution.detach must be a boolean, got {type(detach).__name__}"
        raise HooksConfigError(msg)

    return HookExecution(
        mode=mode,
        detach=detach,
        delivery=delivery,
        timeout_seconds=timeout,
        on_error=on_error,
    )


def _parse_match(raw: Any) -> HookMatch:
    if raw is None:
        return HookMatch()
    if not isinstance(raw, dict):
        msg = f"'match' must be a mapping, got {type(raw).__name__}"
        raise HooksConfigError(msg)
    valid = {f.name for f in fields(HookMatch)}
    extra = cast("set[str]", set(raw) - valid)
    if extra:
        msg = f"match: unknown keys: {sorted(extra)}"
        raise HooksConfigError(msg)

    profile = raw.get("profile")
    if profile is not None and not isinstance(profile, str):
        msg = f"match.profile must be a string, got {type(profile).__name__}"
        raise HooksConfigError(msg)

    profiles = [] if raw.get("profiles") is None else raw.get("profiles", [])
    if not isinstance(profiles, list) or not all(isinstance(p, str) for p in profiles):
        msg = "match.profiles must be a list of strings"
        raise HooksConfigError(msg)

    tool_kind = raw.get("tool_kind")
    if tool_kind is not None and not isinstance(tool_kind, str):
        msg = f"match.tool_kind must be a string, got {type(tool_kind).__name__}"
        raise HooksConfigError(msg)
    if tool_kind is not None:
        # Kind-only field (no ``kind.tool_name`` compounds — tool_name is a
        # separate matcher); legacy pre-P1 values carried a ``chrys.`` prefix.
        tool_kind = strip_legacy_kind_prefix(tool_kind, source="match.tool_kind")

    tool_name = raw.get("tool_name")
    if tool_name is not None and not isinstance(tool_name, str):
        msg = f"match.tool_name must be a string, got {type(tool_name).__name__}"
        raise HooksConfigError(msg)

    args_raw = {} if raw.get("args") is None else raw.get("args", {})
    if not isinstance(args_raw, dict):
        msg = f"match.args must be a mapping, got {type(args_raw).__name__}"
        raise HooksConfigError(msg)

    args: dict[str, HookArgMatch] = {}
    for name, val in args_raw.items():
        if not isinstance(name, str):
            msg = f"match.args keys must be strings, got {type(name).__name__}"
            raise HooksConfigError(msg)
        if not isinstance(val, dict):
            msg = f"match.args.{name} must be a mapping with equals/contains/regex"
            raise HooksConfigError(msg)
        valid_arg = {"equals", "contains", "regex"}
        extra_arg = cast("set[str]", set(val) - valid_arg)
        if extra_arg:
            msg = f"match.args.{name}: unknown keys: {sorted(extra_arg)}"
            raise HooksConfigError(msg)
        equals = _optional_string_field(val.get("equals"), f"match.args.{name}.equals")
        contains = _optional_string_field(val.get("contains"), f"match.args.{name}.contains")
        regex = _optional_string_field(val.get("regex"), f"match.args.{name}.regex")
        args[name] = HookArgMatch(equals=equals, contains=contains, regex=regex)

    return HookMatch(
        profile=profile,
        profiles=list(profiles),
        tool_kind=tool_kind,
        tool_name=tool_name,
        args=args,
    )


def _validate_run_execution_combo(execution: HookExecution, *, hook_id: str) -> None:
    """Reject contradictory combinations.

    Today: ``detach: true`` only makes sense with ``fire_and_forget`` —
    blocking/async modes need stdio pipes to await output, which a
    detached child can't keep open.
    """
    if execution.detach and execution.mode != "fire_and_forget":
        msg = (
            f"hook '{hook_id}': execution.detach=true requires execution.mode='fire_and_forget' "
            f"(got mode={execution.mode!r}); a detached child has no stdio for {APP_DISPLAY_NAME} to await"
        )
        raise HooksConfigError(msg)


def _warn_on_broad_before_tool_hook(*, hook_id: str, event: HookEvent, match: HookMatch) -> None:
    if event != HookEvent.BEFORE_TOOL_CALL:
        return
    if (
        match.profile is not None
        or match.profiles
        or match.tool_kind is not None
        or match.tool_name is not None
        or match.args
    ):
        return
    logger.warning(
        "Hook %r is a before_tool_call hook with no match filters; it will run for every tool call",
        hook_id,
    )


def _warn_on_blocking_durable_hook(*, hook_id: str, execution: HookExecution) -> None:
    if execution.mode != "blocking" or execution.delivery != "durable":
        return
    logger.warning(
        "Hook %r uses execution.delivery='durable' with mode='blocking'; blocking hooks are awaited inline and do "
        "not use the durable outbox",
        hook_id,
    )


def _float_field(value: Any, path: str) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        msg = f"{path} must be a number, got {value!r}"
        raise HooksConfigError(msg)
    return float(value)


def _int_field(value: Any, path: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        msg = f"{path} must be an integer, got {value!r}"
        raise HooksConfigError(msg)
    return value


def _optional_string_field(value: Any, path: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        msg = f"{path} must be a string, got {type(value).__name__}"
        raise HooksConfigError(msg)
    return value
