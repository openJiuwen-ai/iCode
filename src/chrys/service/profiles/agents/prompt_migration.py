# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Recoverable backups before builtin prompt overrides become package-owned."""

from __future__ import annotations

import json
import logging
import tempfile
from copy import deepcopy
from dataclasses import asdict, dataclass
from itertools import count
from pathlib import Path
from typing import TYPE_CHECKING

from chrys.foundation.platform.files import (
    atomic_write_owner_only_text,
    digest_bytes,
    fsync_directory,
    publish_file_no_replace,
)
from chrys.foundation.text.encoding import decode_bytes
from chrys.foundation.text.yaml_io import dump_yaml
from chrys.service.profiles.agents.cloning import CloneIdentity, allocate_identity, copy_profile, name_key
from chrys.service.profiles.agents.loader import AgentProfileLoadError, _parse_profile, scan_profile_dir
from chrys.service.profiles.agents.serializer import builtin_override_dict, profile_directory_lock
from chrys.service.profiles.file_scan import profile_fingerprint, read_profile_bytes, read_profile_mapping

if TYPE_CHECKING:
    from chrys.service.profiles.agents.registry import AgentProfileRegistry
    from chrys.service.profiles.agents.schema import AgentProfile
    from chrys.service.profiles.file_scan import ProfileDirScan

_JOURNAL = ".builtin-prompt-migration.json"
logger = logging.getLogger(__name__)


@dataclass
class _Entry:
    source_file: str
    source_text: str
    source_hash: str
    backup_name: str
    backup_id: str
    backup_text: str
    cleaned_text: str


def _hash(text: str) -> str:
    digest = digest_bytes(text.encode("utf-8"))
    if digest is None:
        raise ValueError("Cannot fingerprint migration data")
    return digest


def needs_prompt_migration(text: str) -> bool:
    data = read_profile_mapping(text)
    compaction = data.get("compaction")
    instructions = data.get("instructions")
    if isinstance(instructions, list):
        instructions = "\n".join(item for item in instructions if isinstance(item, str))
    template = compaction.get("last_words_template") if isinstance(compaction, dict) else None
    return any(isinstance(value, str) and bool(value.strip()) for value in (instructions, template))


def _discard_plan(path: Path) -> None:
    path.unlink(missing_ok=True)
    fsync_directory(path.parent)


def _quarantine_plan(path: Path) -> None:
    """Keep an unreadable plan for recovery without blocking later saves."""
    for index in count(1):
        suffix = ".corrupt" if index == 1 else f".corrupt-{index}"
        destination = path.with_name(path.name + suffix)
        try:
            publish_file_no_replace(path, destination)
        except FileExistsError:
            continue
        path.unlink(missing_ok=True)
        fsync_directory(path.parent)
        logger.warning(
            "Invalid builtin prompt migration journal moved to %s; rebuilding from existing profiles. "
            "Existing -Modified backups are preserved but cannot be verified for reuse; "
            "recovery may create additional copies.",
            destination.name,
        )
        return


def _read_plan(path: Path) -> list[_Entry]:
    if not path.exists():
        return []
    try:
        data = json.loads(read_profile_bytes(path))
        if not isinstance(data, dict):
            raise ValueError("invalid journal document")
        version = data.get("version")
        if type(version) is int and version > 1:
            raise OSError(
                f"Builtin prompt migration journal version {version} requires a newer iCode version; "
                "the journal and original profiles were preserved."
            )
        if type(version) is not int or version != 1:
            raise ValueError("invalid journal version")
        if data.get("completed") is True:
            # Older builds retained completed journals, including credentials.
            _discard_plan(path)
            return []
        entries = [_Entry(**item) for item in data["entries"]]
        source_files: set[str] = set()
        backup_names: set[str] = set()
        backup_ids: set[str] = set()
        for entry in entries:
            if any(not isinstance(value, str) for value in asdict(entry).values()):
                raise ValueError("invalid journal entry")
            for filename in (entry.source_file, f"{entry.backup_name}.yaml"):
                if Path(filename).name != filename or "/" in filename or "\\" in filename:
                    raise ValueError("invalid journal filename")
            original = _parse_profile(path.parent / entry.source_file, entry.source_text)
            backup = _parse_profile(path.parent / f"{entry.backup_name}.yaml", entry.backup_text)
            cleaned = _parse_profile(path.parent / entry.source_file, entry.cleaned_text)
            if (
                entry.source_file != f"{original.name}.yaml"
                or backup.name != entry.backup_name
                or not entry.backup_id
                or backup.id != entry.backup_id
                or backup.name == original.name
                or cleaned.name != original.name
                or (original.id and cleaned.id != original.id)
                or cleaned.instructions
                or cleaned.compaction.last_words_template
                or entry.source_file in source_files
                or entry.backup_name in backup_names
                or entry.backup_id in backup_ids
            ):
                raise ValueError("inconsistent journal entry")
            source_files.add(entry.source_file)
            backup_names.add(entry.backup_name)
            backup_ids.add(entry.backup_id)
        return entries
    except ValueError, KeyError, TypeError, AgentProfileLoadError:
        _quarantine_plan(path)
        return []


def _write_plan(path: Path, entries: list[_Entry]) -> None:
    atomic_write_owner_only_text(path, json.dumps({"version": 1, "entries": [asdict(e) for e in entries]}, indent=2))


def _make_plan(
    registry: AgentProfileRegistry,
    directory: Path,
    scan: ProfileDirScan[AgentProfile],
    sources: dict[str, tuple[str, str]],
    old: list[_Entry],
) -> list[_Entry]:
    occupied = {name_key(p.name) for _, p in scan.loaded}
    occupied.update(name_key(p.display_name or p.name) for _, p in scan.loaded)
    occupied.update(name_key(p.stem) for p in directory.iterdir())
    occupied.update(name_key(name) for name in registry.list_names())
    occupied.update(name_key(p.display_name) for p in registry.list_profiles(include_sub_agent_only=True))
    ids = {p.id for _, p in scan.loaded}
    ids.update(p.id for p in registry.list_profiles(include_sub_agent_only=True))
    reused: dict[str, _Entry] = {}
    names: dict[str, tuple[CloneIdentity, AgentProfile]] = {}
    for entry in old:
        source = directory / entry.source_file
        actual = profile_fingerprint(source)
        backup = directory / f"{entry.backup_name}.yaml"
        backup_unchanged = not (backup.exists() or backup.is_symlink()) or profile_fingerprint(backup) == _hash(
            entry.backup_text
        )
        # A backup renamed/copied by the user still owns its id. Recreating its
        # old filename must not produce a second agent with that same identity.
        backup_unchanged = backup_unchanged and not any(
            profile.id == entry.backup_id and path != backup for path, profile in scan.loaded
        )
        if source in scan.invalid:
            # Keep recovery data for this entry without blocking unrelated saves.
            reused[entry.source_file] = entry
        elif actual in (entry.source_hash, _hash(entry.cleaned_text)):
            sources.setdefault(entry.source_file, (entry.source_text, actual))
            if backup_unchanged:
                reused[entry.source_file] = entry
        if entry.source_file in reused:
            original = _parse_profile(source, entry.source_text)
            preserved = _parse_profile(backup, entry.backup_text)
            identity = CloneIdentity(entry.backup_name, preserved.display_name, entry.backup_id)
            names[original.name] = identity, original
            occupied.update((name_key(identity.name), name_key(identity.display_name)))
            ids.add(identity.id)
    for filename, (source, _) in sorted(sources.items()):
        if filename in reused:
            continue
        profile = _parse_profile(directory / filename, source)
        names[profile.name] = allocate_identity(profile, occupied, ids, backup=True), profile
    keep_plan = bool(scan.invalid) or any(p.name == "Plan" for _, p in scan.loaded) or registry.get("Plan") is not None
    entries = list(reused.values())
    for filename, (source, source_hash) in sorted(sources.items()):
        if filename in reused:
            continue
        profile = _parse_profile(directory / filename, source)
        identity, _ = names[profile.name]
        duplicate = copy_profile(profile, identity, names)
        backup = deepcopy(read_profile_mapping(source))
        backup.update(name=duplicate.name, display_name=duplicate.display_name, id=duplicate.id)
        backup.pop("_builtin_prompts_managed", None)
        sub_agents = backup.get("sub_agents")
        if isinstance(sub_agents, dict) and isinstance(refs := sub_agents.get("agents"), list):
            # The loader skips mappings without a profile and ignores all child
            # refs for ACP profiles. Keep those raw entries in the backup too.
            parsed_refs = (ref for ref in refs if isinstance(ref, dict) and "profile" in ref)
            for ref, copied in zip(parsed_refs, duplicate.sub_agents.agents, strict=False):
                if ref.get("profile") in names:
                    ref.update(
                        profile=copied.profile, tool_name=copied.tool_name, tool_description=copied.tool_description
                    )
        if not profile.id:
            template = registry.get_builtin_template(profile.name)
            if template is not None:
                profile.id = template.id
        if not keep_plan:
            profile.sub_agents.agents = [ref for ref in profile.sub_agents.agents if ref.profile != "Plan"]
        entries.append(
            _Entry(
                filename,
                source,
                source_hash,
                identity.name,
                identity.id,
                dump_yaml(backup),
                dump_yaml(builtin_override_dict(profile)),
            )
        )
    return entries


def _publish_backup(directory: Path, entry: _Entry) -> None:
    destination = directory / f"{entry.backup_name}.yaml"
    if destination.exists():
        if profile_fingerprint(destination) != _hash(entry.backup_text):
            raise OSError(f"Backup {entry.backup_name!r} changed; retry migration to choose a new name.")
        return
    with tempfile.TemporaryDirectory(prefix=".prompt-backup-", dir=directory) as staging:
        staged = Path(staging) / destination.name
        atomic_write_owner_only_text(staged, entry.backup_text)
        publish_file_no_replace(staged, destination)
        fsync_directory(directory)


def migrate_builtin_prompts(registry: AgentProfileRegistry, directory: Path) -> list[str]:
    """Back up nonempty builtin prompt overrides before writers drop their prompts.

    The journal precedes all backup writes. All backups precede any cleanup,
    and every retry verifies both source and backup fingerprints. A modified
    source starts a new plan; an occupied backup name is never overwritten.
    """
    with profile_directory_lock(directory):
        scan = scan_profile_dir(directory)
        if scan.directory_error:
            raise OSError(
                "Cannot read the agent profile directory; original profiles were preserved."
            ) from scan.directory_error
        journal = directory / _JOURNAL
        old = _read_plan(journal)
        sources: dict[str, tuple[str, str]] = {}
        for path, profile in scan.loaded:
            if registry.is_builtin(profile.name):
                raw = read_profile_bytes(path)
                fingerprint = digest_bytes(raw)
                if fingerprint is None or fingerprint != scan.fingerprints[path]:
                    raise OSError("An agent profile changed while preparing its prompt backup. Try again.")
                source = decode_bytes(raw, errors="strict")
                if needs_prompt_migration(source):
                    sources[path.name] = source, fingerprint
        if not sources and not old:
            return []
        names: set[str] = set()
        for path, profile in scan.loaded:
            canonical = directory / f"{profile.name}.yaml"
            if profile.name in names or not canonical.exists() or not path.samefile(canonical):
                raise OSError("Cannot back up builtin prompts until agent filenames and duplicate names are resolved.")
            names.add(profile.name)
        entries = _make_plan(registry, directory, scan, sources, old)
        if entries != old:
            _write_plan(journal, entries)
        ready = []
        for entry in entries:
            if directory / entry.source_file in scan.invalid:
                logger.warning("Prompt backup recovery is pending for unreadable profile %s", entry.source_file)
            else:
                ready.append(entry)
        for entry in ready:
            _publish_backup(directory, entry)
        for entry in ready:
            source = directory / entry.source_file
            actual = profile_fingerprint(source)
            if actual == _hash(entry.cleaned_text):
                continue
            if actual != entry.source_hash:
                raise OSError("An agent profile changed during prompt migration; originals and backups were preserved.")
            atomic_write_owner_only_text(source, entry.cleaned_text)
        if len(ready) == len(entries):
            _discard_plan(journal)
        return [entry.backup_name for entry in ready]
