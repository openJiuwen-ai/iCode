# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Install the bundled security guard hooks into the global hooks directory.

The rule pack lives in ``chrys/kernel/aixcoding/guard/pack/`` and ships inside
the wheel and the PyApp distribution, so a user never copies files by hand.
Installation writes to the place the runtime actually reads
(:func:`chrys.foundation.platform.get_platform` -> ``config_dir / "hooks"``):

* Windows: ``%APPDATA%\\chrys\\hooks\\``
* macOS / Linux: ``~/.chrys/hooks/``

Two machine shapes are handled:

* **Fresh install** — no ``hooks.yaml`` yet: the shipped template is written
  verbatim.
* **Existing configuration** — the entries this installer owns are replaced in
  place (matched by id), and the settings keys the template ships are
  overwritten with the shipped values so every upgrade carries them;
  unrelated hooks, other settings and comments are kept.  A configuration
  that cannot be parsed is backed up and replaced: a
  guard that silently fails to load is worse than a hand-written hook being
  reset, and the backup keeps that hook recoverable.

Every write goes through a same-directory temporary file plus ``os.replace``,
so a crash mid-install cannot leave a half-written ``hooks.yaml`` behind.
"""

from __future__ import annotations

import contextlib
import os
import re
import shutil
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

import yaml

from chrys.foundation import platform as platform_api

GUARD_ASSET_DIR = Path(__file__).resolve().parent / "pack"
"""Directory holding the shipped rule pack in source checkouts, wheels and PyApp builds."""


def resolve_asset_dir() -> Path:
    """Return the single shipped rule-pack directory."""
    return GUARD_ASSET_DIR


GUARD_CONFIG_NAME = "hooks.yaml"
"""Name of the hooks configuration this installer owns entries in."""

GUARD_TEMPLATE_NAME = "hooks.yaml"
"""Shipped template; its ``path:`` values already point at ``guard/``."""

GUARD_SUBDIR = "guard"
"""Scripts and the rule dictionary go here, beside ``hooks.yaml``."""

GUARD_PAYLOAD_FILES = ("_prompt_guard.py", "_tool_guard.py", "_guard_core.py", "rules.json", "_launcher.ps1")
"""Files a hook subprocess needs at run time.

These files live in ``pack/`` beside this module — the single source of truth
shipped inside the wheel through ``packages = ["src/chrys"]``. ``_launcher.ps1``
is the Windows interpreter launcher the shipped ``hooks.yaml`` template
invokes; on POSIX it is copied but unused.
Legacy installs (before 0.31.0) shipped the guard scripts without the leading
underscore; :func:`_copy_payload` removes those leftovers.
"""

LEGACY_PAYLOAD_FILES = ("prompt_guard.py", "tool_guard.py", "guard_core.py")
"""Pre-0.31.0 payload names; removed from installs so stale scripts never linger."""

MANAGED_HOOK_IDS = ("security-prompt-guard", "security-tool-guard")
"""Hook ids this installer owns; re-installing replaces exactly these."""

BACKUP_SUFFIX = ".pre-guard"

_BOM = b"\xef\xbb\xbf"
_TOP_LEVEL_KEY = re.compile(r"^(?P<key>[A-Za-z_][\w-]*)\s*:(?P<rest>.*)$")
_BLOCK_SEQUENCE = re.compile(r"^(?P<indent>[ ]*)-(?P<rest>\s.*|$)")


@dataclass(frozen=True)
class GuardInstallReport:
    """What :func:`install_guard_hooks` did, for the caller's status output."""

    hooks_dir: Path
    """Directory the rule pack was written to."""

    hooks_file: Path
    """``hooks.yaml`` that now lists the guard hooks."""

    merged_into_existing: bool
    """``True`` when another configuration was merged instead of overwritten."""

    backup: Path | None
    """Backup of the previous configuration, when one was made."""

    replaced_unparsable_config: bool = False
    """``True`` when the previous configuration could not be parsed."""


def hooks_dir_for(config_dir: Path) -> Path:
    """Return ``<config_dir>/hooks`` — the directory the runtime loads hooks from."""
    return config_dir / "hooks"


def install_guard_hooks(*, hooks_dir: Path | None = None, asset_dir: Path | None = None) -> GuardInstallReport:
    """Copy the rule pack into *hooks_dir* and merge its hook entries.

    ``hooks_dir`` defaults to the platform hooks directory; ``asset_dir`` is
    overridable for tests.  Files are copied before the configuration is
    written, so the configuration never points at scripts that are not there.
    Idempotent: re-running on an up-to-date install rewrites the same content.
    """
    source_dir = resolve_asset_dir() if asset_dir is None else asset_dir
    missing = missing_guard_assets(source_dir)
    if missing:
        raise FileNotFoundError(f"Guard hooks assets are missing from this build: {', '.join(map(str, missing))}")

    target_dir = hooks_dir_for(platform_api.get_platform().config_dir) if hooks_dir is None else hooks_dir
    template_text = (source_dir / GUARD_TEMPLATE_NAME).read_text(encoding="utf-8")
    template_text = _posix_template(template_text)
    managed_entries = parse_managed_entries(template_text)
    template_settings = yaml.safe_load(template_text).get("settings") or {}

    _copy_payload(Path(source_dir), target_dir)

    hooks_file = target_dir / GUARD_CONFIG_NAME
    if not hooks_file.exists():
        _write_text_atomic(hooks_file, template_literal_text(template_text), backup=False)
        return GuardInstallReport(target_dir, hooks_file, merged_into_existing=False, backup=None)

    raw = hooks_file.read_bytes()
    had_bom = raw.startswith(_BOM)
    text = raw.decode("utf-8-sig")
    try:
        merged_text = sync_template_settings(merge_guard_entries(text, managed_entries), template_settings)
    except yaml.YAMLError, ValueError:
        return _replace_unparsable(hooks_file, template_text, had_bom)
    try:
        merged_document = yaml.safe_load(merged_text)
    except yaml.YAMLError:
        return _replace_unparsable(hooks_file, template_text, had_bom)
    if not isinstance(merged_document, dict):
        return _replace_unparsable(hooks_file, template_text, had_bom)

    _write_text_atomic(hooks_file, merged_text, backup=True, had_bom=had_bom)
    return GuardInstallReport(
        target_dir,
        hooks_file,
        merged_into_existing=True,
        backup=hooks_file.with_name(f"{hooks_file.name}{BACKUP_SUFFIX}"),
    )


def template_literal_text(template_text: str) -> str:
    """Return the template exactly as a fresh install should write it."""
    return template_text.replace("\r\n", "\n").rstrip("\n") + "\n"


_LAUNCHER_SCRIPT_RE = re.compile(r"^\s*GUARD_SCRIPT:\s*(\S+)", re.MULTILINE)


def _posix_template(template_text: str) -> str:
    """Rewrite launcher-form ``run`` blocks back to ``type: script`` on POSIX.

    The shipped template invokes ``guard/_launcher.ps1`` through PowerShell so
    Windows machines without a usable PATH python still run the guard (the
    launcher finds one at execution time). POSIX has no Store-stub problem, and
    the hooks runner there has no PowerShell to invoke, so the engine writes
    the plain script form instead.
    """
    if sys.platform == "win32" or "_launcher.ps1" not in template_text:
        return template_text
    lines = template_text.splitlines(keepends=True)
    rewritten: list[str] = []
    index = 0
    while index < len(lines):
        line = lines[index]
        indent = len(line) - len(line.lstrip())
        if line.strip() == "run:" and "_launcher.ps1" in "".join(lines[index : index + 30]):
            block_end = index + 1
            while block_end < len(lines):
                following = lines[block_end]
                if following.strip() and len(following) - len(following.lstrip()) <= indent:
                    break
                block_end += 1
            match = _LAUNCHER_SCRIPT_RE.search("".join(lines[index:block_end]))
            if match is not None:
                script = match.group(1).replace("\\", "/")
                pad = " " * indent
                rewritten.append(f"{pad}run:\n{pad}  type: script\n{pad}  path: {script}\n")
                index = block_end
                continue
        rewritten.append(line)
        index += 1
    return "".join(rewritten)


def parse_managed_entries(template_text: str) -> list[str]:
    """Return the template's ``hooks:`` entries as text blocks, comments included."""
    document = yaml.safe_load(template_text)
    if not isinstance(document, dict) or not isinstance(document.get("hooks"), list):
        raise ValueError(f"{GUARD_TEMPLATE_NAME} has no hooks list")
    blocks = _hook_blocks(template_text)
    template_ids = [str(entry.get("id", "")) for entry in document["hooks"] if isinstance(entry, dict)]
    if len(blocks) != len(template_ids):
        raise ValueError(f"{GUARD_TEMPLATE_NAME}: {len(template_ids)} hooks but {len(blocks)} YAML blocks")
    return blocks


def merge_guard_entries(config_text: str, managed_entries: list[str]) -> str:
    """Replace our entries in *config_text*, keeping every other entry and setting.

    Raises ``yaml.YAMLError`` when the file cannot be parsed; the caller then
    treats it as unparsable instead of writing half of it back.
    """
    document = yaml.safe_load(config_text)
    if document is None:
        document = {}
    if not isinstance(document, dict):
        raise ValueError("hooks configuration must be a mapping")
    existing_entries = document.get("hooks")
    if existing_entries is not None and not isinstance(existing_entries, list):
        raise ValueError("hooks must be a list")

    newline = "\r\n" if "\r\n" in config_text else "\n"
    blocks = _hook_blocks(config_text)
    if _needs_structured_hook_merge(existing_entries, blocks):
        kept_entries = [
            entry
            for entry in existing_entries or []
            if not (isinstance(entry, dict) and str(entry.get("id", "")) in MANAGED_HOOK_IDS)
        ]
        rendered = _render_structured_hooks(kept_entries) + "".join(
            block.replace("\r\n", "\n") for block in managed_entries
        )
    else:
        kept_blocks = [block for block in blocks if _hook_id(block) not in MANAGED_HOOK_IDS]
        rendered = "".join(block.replace("\r\n", "\n") for block in (*kept_blocks, *managed_entries))
    span = _hook_section_span(config_text)
    if span is None:
        return template_literal_text(config_text + newline + ("hooks:\n" + rendered).replace("\n", newline))
    if not rendered:
        rendered = " []\n"
    start, end = span
    body = ("hooks:\n" + rendered).replace("\n", newline)
    return template_literal_text(config_text[:start] + body + config_text[end:])


def _needs_structured_hook_merge(existing_entries: object, blocks: list[str]) -> bool:
    """Whether the hooks list uses a valid YAML shape that block surgery cannot preserve.

    Flow-style sequences (``hooks: [{id: telemetry, ...}]``) are legal input but
    have no per-entry block to edit.  Fall back to a data-preserving render
    instead of treating the empty block list as "no hooks" and dropping them.
    """
    if not existing_entries:
        return False
    entries = existing_entries if isinstance(existing_entries, list) else []
    return len(blocks) != len(entries) or any(_hook_id(block) == "" for block in blocks)


def _render_structured_hooks(entries: list[object]) -> str:
    """Render non-managed hooks as a block sequence indented under ``hooks:``."""
    if not entries:
        return ""
    dumped = yaml.safe_dump(entries, allow_unicode=True, default_flow_style=False, sort_keys=False, width=4096)
    return "".join(f"  {line}\n" for line in dumped.rstrip("\n").splitlines())


def _replace_unparsable(hooks_file: Path, template_text: str, had_bom: bool) -> GuardInstallReport:
    backup = hooks_file.with_name(f"{hooks_file.name}{BACKUP_SUFFIX}")
    shutil.copyfile(hooks_file, backup)
    _write_text_atomic(hooks_file, template_literal_text(template_text), backup=False, had_bom=had_bom)
    return GuardInstallReport(
        hooks_file.parent,
        hooks_file,
        merged_into_existing=False,
        backup=backup,
        replaced_unparsable_config=True,
    )


# ---------------------------------------------------------------------------
# YAML section surgery
# ---------------------------------------------------------------------------


def _hook_section_span(text: str) -> tuple[int, int] | None:
    """Return the character span of the top-level ``hooks:`` section."""
    return _top_level_span(text, "hooks")


def _top_level_span(text: str, key: str) -> tuple[int, int] | None:
    """Return the character span of one top-level mapping section."""
    offset = 0
    start: int | None = None
    for line in text.splitlines(keepends=True):
        match = _TOP_LEVEL_KEY.match(line.rstrip("\r\n"))
        if match is not None:
            if start is not None:
                return start, offset
            if match.group("key") == key:
                start = offset
        offset += len(line)
    if start is None:
        return None
    return start, len(text)


_SETTINGS_ENTRY = re.compile(r"^(?P<indent>\s+)(?P<key>[A-Za-z_][\w-]*)\s*:(?P<value>[^\n#]*)(?P<comment>#.*)?$")
"""One child ``key: value`` line of the ``settings:`` section, comment kept separate."""


def sync_template_settings(config_text: str, template_settings: dict[str, object]) -> str:
    """Overwrite the settings keys the template ships with the shipped values.

    The rule pack is the source of truth for these keys, so every upgrade
    carries their current values into existing configurations — a machine that
    installed an older pack never keeps the stale ``shutdown_grace_seconds``
    it was seeded with.  Keys the template does not ship, and comments, are
    kept.  A configuration without a ``settings:`` section gains one before
    the ``hooks:`` section.
    """
    if not template_settings:
        return config_text
    newline = "\r\n" if "\r\n" in config_text else "\n"
    span = _top_level_span(config_text, "settings")
    if span is None:
        block = (
            "settings:"
            + newline
            + "".join(f"  {key}: {_yaml_scalar(value)}{newline}" for key, value in template_settings.items())
            + newline
        )
        hooks_span = _top_level_span(config_text, "hooks")
        if hooks_span is None:
            return template_literal_text(config_text + block)
        start = hooks_span[0]
        return template_literal_text(config_text[:start] + block + config_text[start:])

    section_lines = config_text[span[0] : span[1]].splitlines(keepends=True)
    child_matches = [
        match
        for line in section_lines[1:]
        if (stripped := line.rstrip("\r\n"))[:1] in (" ", "\t") and (match := _SETTINGS_ENTRY.match(stripped))
    ]
    if not child_matches:
        document = yaml.safe_load(config_text)
        existing_settings = document.get("settings") if isinstance(document, dict) else None
        if existing_settings is None or isinstance(existing_settings, dict):
            merged_settings = {**(existing_settings or {}), **template_settings}
            return template_literal_text(
                config_text[: span[0]] + _render_mapping("settings", merged_settings) + config_text[span[1] :]
            )

    rewritten: list[str] = []
    missing = dict(template_settings)
    for line in section_lines:
        stripped = line.rstrip("\r\n")
        match = _SETTINGS_ENTRY.match(stripped) if stripped[:1] in (" ", "\t") else None
        if match is not None and match.group("key") in missing:
            key = match.group("key")
            comment = match.group("comment")
            rewritten.append(
                f"{match.group('indent')}{key}: {_yaml_scalar(missing.pop(key))}{f' {comment}' if comment else ''}{newline}"
            )
        else:
            rewritten.append(line)
    for key, value in missing.items():
        rewritten.append(f"  {key}: {_yaml_scalar(value)}{newline}")
    return template_literal_text(config_text[: span[0]] + "".join(rewritten) + config_text[span[1] :])


def _render_mapping(name: str, values: dict[object, object]) -> str:
    """Render one top-level mapping block, preserving every key and value."""
    dumped = yaml.safe_dump(values, allow_unicode=True, default_flow_style=False, sort_keys=False, width=4096)
    return f"{name}:\n" + "".join(f"  {line}\n" for line in dumped.rstrip("\n").splitlines())


def _yaml_scalar(value: object) -> str:
    """Render one settings value the way YAML writes it (plain scalars for ints).

    ``safe_dump`` of a bare scalar appends the document-end marker ``...``
    (``"30\\n...\\n"``); leaving it in would turn the following line into a
    document boundary and make the file unparsable.
    """
    dumped = yaml.safe_dump(value, default_flow_style=True, width=4096)
    return dumped.removesuffix("...\n").strip()


def _hook_blocks(text: str) -> list[str]:
    """Split the ``hooks:`` section into one text block per list entry.

    Each block starts at a list-item line and runs until the next item (or the
    end of the section), so both the item's keys and its nested mappings stay
    with it.  Comment lines between a block and the next item stay attached to
    that next item, preserving the documented layout of the file.
    """
    span = _hook_section_span(text)
    if span is None:
        return []
    lines = text[span[0] : span[1]].splitlines(keepends=True)[1:]
    item_indent = next(
        (len(sequence.group("indent")) for line in lines if (sequence := _BLOCK_SEQUENCE.match(line)) is not None),
        None,
    )
    if item_indent is None:
        return []
    blocks: list[str] = []
    current: list[str] = []
    comments: list[str] = []
    for line in lines:
        sequence = _BLOCK_SEQUENCE.match(line)
        if sequence is not None and len(sequence.group("indent")) == item_indent:
            if current:
                blocks.append("".join(current))
            current = [*comments, line]
            comments = []
        elif line.strip().startswith("#") or not line.strip():
            comments.append(line)
        elif current:
            current.extend(comments)
            current.append(line)
            comments = []
    if current:
        blocks.append("".join(current))
    return blocks


def _hook_id(block: str) -> str:
    """Return the ``id:`` of one hook block, or ``""`` when it has none."""
    parsed = yaml.safe_load(block)
    if isinstance(parsed, list) and parsed and isinstance(parsed[0], dict):
        return str(parsed[0].get("id", ""))
    return ""


# ---------------------------------------------------------------------------
# Filesystem helpers
# ---------------------------------------------------------------------------


def missing_guard_assets(asset_dir: Path | None = None) -> list[Path]:
    """Return shipped files absent from this build."""
    root = resolve_asset_dir() if asset_dir is None else asset_dir
    expected = (root / GUARD_TEMPLATE_NAME, *(root / name for name in GUARD_PAYLOAD_FILES))
    return [path for path in expected if not path.is_file()]


def _copy_payload(source_dir: Path, target_dir: Path) -> None:
    """Copy the scripts and dictionary into ``<hooks_dir>/guard/`` atomically."""
    guard_dir = target_dir / GUARD_SUBDIR
    guard_dir.mkdir(parents=True, exist_ok=True)
    for name in GUARD_PAYLOAD_FILES:
        _copy_file_atomic(source_dir / name, guard_dir / name)
    for name in LEGACY_PAYLOAD_FILES:
        with contextlib.suppress(OSError):
            (guard_dir / name).unlink(missing_ok=True)


def _copy_file_atomic(source: Path, destination: Path) -> None:
    """Copy *source* over *destination* without exposing a partial file."""
    fd, temporary_name = tempfile.mkstemp(prefix=f".{destination.name}.", suffix=".tmp", dir=destination.parent)
    temporary_path = Path(temporary_name)
    try:
        os.close(fd)
        shutil.copyfile(source, temporary_path)
        os.replace(temporary_path, destination)
    finally:
        with contextlib.suppress(OSError):
            temporary_path.unlink(missing_ok=True)


def _write_text_atomic(path: Path, text: str, *, backup: bool, had_bom: bool = False) -> None:
    """Write *text* (UTF-8, optional BOM) to *path* atomically, backing up first."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if backup and path.exists():
        shutil.copyfile(path, path.with_name(f"{path.name}{BACKUP_SUFFIX}"))
    payload = text.encode("utf-8")
    if had_bom:
        payload = _BOM + payload
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
        _apply_default_permissions(temporary_path)
        os.replace(temporary_path, path)
    finally:
        if temporary_path.exists():
            with contextlib.suppress(OSError):
                temporary_path.unlink()


def _apply_default_permissions(path: Path) -> None:
    """Keep the configuration owner-only where the platform supports it."""
    if sys.platform == "win32":
        return
    with contextlib.suppress(OSError, AttributeError):
        os.chmod(path, 0o600)


def installed_guard_hooks(hooks_dir: Path | None = None) -> tuple[bool, list[str]]:
    """Return ``(config_lists_guards, missing_files)`` for a hooks directory."""
    target_dir = hooks_dir_for(platform_api.get_platform().config_dir) if hooks_dir is None else hooks_dir
    hooks_file = target_dir / GUARD_CONFIG_NAME
    listed = False
    with contextlib.suppress(OSError, yaml.YAMLError):
        document = yaml.safe_load(hooks_file.read_text(encoding="utf-8-sig")) if hooks_file.is_file() else None
        if isinstance(document, dict) and isinstance(document.get("hooks"), list):
            ids = {str(entry.get("id", "")) for entry in document["hooks"] if isinstance(entry, dict)}
            listed = all(hook_id in ids for hook_id in MANAGED_HOOK_IDS)
    missing = [
        str(target_dir / GUARD_SUBDIR / name)
        for name in GUARD_PAYLOAD_FILES
        if not guard_file_present(target_dir, name)
    ]
    return listed, missing


def guard_file_present(target_dir: Path, name: str) -> bool:
    """Return whether one installed rule-pack file exists under ``guard/``."""
    return (target_dir / GUARD_SUBDIR / name).is_file()


__all__ = [
    "GUARD_ASSET_DIR",
    "GUARD_PAYLOAD_FILES",
    "GUARD_SUBDIR",
    "MANAGED_HOOK_IDS",
    "GuardInstallReport",
    "hooks_dir_for",
    "install_guard_hooks",
    "installed_guard_hooks",
    "merge_guard_entries",
    "missing_guard_assets",
    "parse_managed_entries",
    "sync_template_settings",
    "template_literal_text",
]
