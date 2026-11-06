# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Disk profile candidates for one rebuild. Live registries retain their identity."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING

from chrys.foundation.config.errors import ConfigError
from chrys.foundation.i18n import DisplayPath, DisplaySequence, MessageRef, msg
from chrys.foundation.platform.files import publish_file_no_replace, replace_with_retry
from chrys.service.profiles.agents.loader import scan_profile_dir as scan_agents
from chrys.service.profiles.agents.registry import AgentProfileRegistry, legacy_profile_id, resolve_launch_profile
from chrys.service.profiles.agents.serializer import profile_write_lock, save_profile
from chrys.service.profiles.file_scan import profile_fingerprint
from chrys.service.profiles.models.loader import scan_profile_dir as scan_models
from chrys.service.profiles.models.registry import ModelProfileRegistry

if TYPE_CHECKING:
    from chrys.service.profiles.agents.schema import AgentProfile
    from chrys.service.profiles.file_scan import ProfileDirScan
    from chrys.service.profiles.models.schema import ModelProfile

_DIRECTORY_FAILED = msg(
    "profiles.reload_directory_failed", fallback="Cannot list {path}. The profile directories were not reloaded."
)
_STALE = msg("profiles.reload_stale", fallback="Profiles changed while reloading. Run /reload again.")
_INVALID = msg("profiles.reload_invalid", fallback="Skipped invalid or unreadable profile files: {paths}")
_KEPT = msg("profiles.reload_kept", fallback="Kept the last loaded version of these profile files: {paths}")
_NORMALIZE_FAILED = msg("profiles.reload_normalize_failed", fallback="Could not normalize {path}: {reason}")
_FILE_CHANGED = msg(
    "profiles.reload_file_changed", fallback="The profile file changed before it could be normalized: {path}"
)
_FILE_CONFLICT = msg(
    "profiles.reload_file_conflict",
    fallback="Could not register {path} under its profile name. Check for a conflicting filename.",
)
_DISK_CHANGES = msg("profiles.reload_disk_changes", fallback="Profile files already changed: {changes}")
_CHANGED = msg("profiles.reload_changed", fallback="Reloaded agent and model profile definitions.")


class ProfileReload:
    """Capture shared definitions on their owning loop, prepare and normalize off-thread."""

    def __init__(self, agents: AgentProfileRegistry | None, models: ModelProfileRegistry | None) -> None:
        self._live_agents = agents
        self._live_models = models
        self._versions = (agents.revision if agents else None, models.revision if models else None)
        self.agents = agents.reload_base() if agents else None
        self.models = models.reload_base() if models else None
        self._old_agents = {p: agents.get(key) for p, key in agents.sources.items()} if agents else {}
        self._old_models = models.sources.copy() if models else {}
        self._source_successors: dict[tuple[str, str], AgentProfile | None] = {}
        self._agent_files: ProfileDirScan[AgentProfile] | None = None
        self._model_files: ProfileDirScan[ModelProfile] | None = None
        self._rewrites: list[AgentProfile] = []
        self.report: list[MessageRef] = []
        self._old_definitions = (
            {p.name: p for p in agents.list_profiles(include_sub_agent_only=True)} if agents else {},
            {p.id: p for p in models.list_profiles()} if models else {},
        )
        self.completed_changes: list[str] = []

    def check_versions(self) -> None:
        if self._versions != (
            self._live_agents.revision if self._live_agents else None,
            self._live_models.revision if self._live_models else None,
        ):
            raise ConfigError(_STALE.bind())

    def scan(self) -> None:
        kept: list[str] = []
        skipped: list[str] = []
        if self.models is not None:
            files = scan_models(self.models.directory)
            self._model_files = files
            if files.directory_error is not None:
                raise ConfigError(_DIRECTORY_FAILED.bind(path=DisplayPath(self.models.directory)))
            loaded = dict(files.loaded)
            for path in files.fingerprints:
                model = loaded.get(path)
                if model is None:
                    location = f"{path} [{files.invalid[path].location}]"
                    model = self._old_models.get(path)
                    if model is None:
                        skipped.append(location)
                        continue
                    kept.append(location)
                self.models.register(model, source=path)
        if self.agents is not None:
            scan = scan_agents(self.agents.directory)
            self._agent_files = scan
            if scan.directory_error is not None:
                raise ConfigError(_DIRECTORY_FAILED.bind(path=DisplayPath(self.agents.directory)))
            keep_plan = (
                bool(scan.invalid)
                or self.agents.get("Plan") is not None
                or any(p.name == "Plan" for _, p in scan.loaded)
            )
            for path, agent in scan.loaded:
                needs_id = not agent.id
                if needs_id:
                    agent.id = legacy_profile_id(agent.name)
                refs = agent.sub_agents.agents
                drop_plan = not keep_plan and any(ref.profile == "Plan" for ref in refs)
                if drop_plan:
                    agent.sub_agents.agents = [ref for ref in refs if ref.profile != "Plan"]
                if needs_id or drop_plan:
                    self._rewrites.append(agent)
                self.agents.register(agent, source=path)
            for path, issue in scan.invalid.items():
                location = f"{path} [{issue.location}]"
                old_agent = self._old_agents.get(path)
                if old_agent is not None and not any(p.name == old_agent.name for _, p in scan.loaded):
                    self.agents.register(old_agent, source=path)
                    kept.append(location)
                else:
                    skipped.append(location)
            # Preserve the input paths: normalization may rename these files before
            # the launch agent is resolved. An absent tracked source cannot match by name.
            self._source_successors = {
                (old.name, old.id): self.agents.get(self.agents.sources[path]) if path in self.agents.sources else None
                for path, old in self._old_agents.items()
                if old is not None
            }
        for definition, paths in ((_KEPT, kept), (_INVALID, skipped)):
            if paths:
                self.report.append(definition.bind(paths=DisplaySequence(tuple(paths))))
        definitions = (
            {p.name: p for p in self.agents.list_profiles(include_sub_agent_only=True)} if self.agents else {},
            {p.id: p for p in self.models.list_profiles()} if self.models else {},
        )
        if self._old_definitions != definitions:
            self.report.append(_CHANGED.bind())

    def normalize(self) -> None:
        """Recheck disk inputs *after* the build, before applying any agent migrations."""
        if self.models is not None and self._model_files is not None:
            current = scan_models(self.models.directory)
            if current.directory_error or current.fingerprints != self._model_files.fingerprints:
                raise ConfigError(_STALE.bind())
        if self.agents is None or self._agent_files is None:
            return
        directory = self.agents.directory
        current_agents = scan_agents(directory)
        if current_agents.directory_error or current_agents.fingerprints != self._agent_files.fingerprints:
            raise ConfigError(_STALE.bind())
        originals = self._agent_files.loaded
        expected = self._agent_files.fingerprints.copy()

        def failure(path: Path, reason: str = "", *, conflict: bool = False) -> ConfigError:
            source = DisplayPath(path)
            display = (
                _NORMALIZE_FAILED.bind(path=source, reason=reason)
                if reason
                else (_FILE_CONFLICT if conflict else _FILE_CHANGED).bind(path=source)
            )
            return ConfigError(display, hint=self.changes_hint())

        def publish(source: Path, target: Path, replacement: Path | None = None) -> None:
            # Take ownership of the exact directory entry before validating it.
            # An editor can create a new source while it is parked. Only private
            # paths may be unlinked, and neither publication nor recovery overwrites.
            parked = None
            keep_parked = False
            try:
                descriptor, name = tempfile.mkstemp(dir=directory, prefix=f".{source.name}.", suffix=".reload")
                parked = Path(name)
                os.close(descriptor)
                replace_with_retry(source, parked)
                try:
                    if expected.get(source) is None or profile_fingerprint(parked) != expected[source]:
                        raise failure(source)
                    publish_file_no_replace(replacement or parked, target)
                except BaseException:
                    try:
                        publish_file_no_replace(parked, source)
                    except OSError:
                        keep_parked = True
                        self.completed_changes.append(str(parked))
                    raise
            except OSError as exc:
                raise failure(source, exc.strerror or str(exc)) from exc
            finally:
                if parked is not None and not keep_parked:
                    parked.unlink(missing_ok=True)

        def move(source: Path, target: Path) -> None:
            publish(source, target)
            self.completed_changes.append(f"{source.name} → {target.name}")
            expected[target] = expected.pop(source)

        try:
            with profile_write_lock:
                self.check_versions()
                ready = self.agents.canonicalize_profile_files(originals, directory, move=move)
                ready_ids = {id(profile) for _, profile in ready}
                for path, profile in originals:
                    if id(profile) not in ready_ids:
                        raise failure(path, conflict=True)
                for path, _ in originals:
                    self.agents.sources.pop(path, None)
                self.agents.sources.update({path: profile.name for path, profile in ready})
                if self._rewrites:
                    ready_paths = {id(profile): path for path, profile in ready}
                    try:
                        staging_dir = tempfile.TemporaryDirectory(dir=directory, prefix=".icode-reload-")
                    except OSError as exc:
                        raise failure(ready_paths[id(self._rewrites[0])], exc.strerror or str(exc)) from exc
                    with staging_dir as staging:
                        for profile in self._rewrites:
                            path = ready_paths[id(profile)]
                            try:
                                replacement = save_profile(profile, target_dir=Path(staging))
                                digest = profile_fingerprint(replacement)
                                if digest is None:
                                    raise failure(path)
                                publish(path, path, replacement)
                            except (ValueError, OSError) as exc:
                                raise failure(path, str(exc)) from exc
                            expected[path] = digest
                            self.completed_changes.append(path.name)
                if self.completed_changes:
                    final_files = scan_agents(directory)
                    if final_files.directory_error or final_files.fingerprints != expected:
                        raise ConfigError(_STALE.bind(), hint=self.changes_hint())
        except ConfigError as exc:
            exc.display_hint = self.changes_hint()
            raise
        except OSError as exc:
            raise failure(Path(exc.filename) if exc.filename else directory, exc.strerror or str(exc)) from exc

    def changes_hint(self) -> MessageRef | None:
        if self.completed_changes:
            return _DISK_CHANGES.bind(changes=DisplaySequence(tuple(self.completed_changes)))
        return None

    def updated_agent(self, previous: AgentProfile) -> AgentProfile | None:
        if self.agents is None:
            return None
        match = self.agents.get_by_id(previous.id, disambiguating_name=previous.name) if previous.id else None
        # A user override can have its builtin's ID. Renaming the file's agent
        # can reveal that builtin while the user's definition continues under a new ID.
        if match is not None and match.name in self.agents.sources.values():
            return match
        # A builtin and its same-name override can legitimately have different IDs.
        if match is None and self.agents.is_builtin(previous.name):
            match = self.agents.get(previous.name)
        identity = (previous.name, previous.id)
        if identity in self._source_successors:
            return self._source_successors[identity] or match
        return match if previous.id else self.agents.get(previous.name)

    def updated_launch_agent(self, previous_name: str, default_agent: str) -> str:
        if self._live_agents is None or self.agents is None:
            return previous_name
        old = self._live_agents.get(previous_name)
        if old is not None:
            updated = self.updated_agent(old)
            if updated is not None and updated in self.agents.list_profiles():
                return updated.name
            fallback = resolve_launch_profile(self.agents, default_agent)
            return fallback.name if fallback is not None else ""
        return previous_name

    def install(self) -> None:
        if self._live_agents is not None and self.agents is not None:
            self._live_agents.install_reload(self.agents)
        if self._live_models is not None and self.models is not None:
            self._live_models.install_reload(self.models)
