# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""AgentProfileRegistry — register, load, and switch agent profiles."""

from __future__ import annotations

import contextlib
import logging
import unicodedata
import uuid
from collections.abc import Iterable
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Final

from chrys.foundation.config.settings import DEFAULT_AGENT_PROFILE
from chrys.service.profiles.agents.loader import (
    AgentProfileLoadError,
    load_profile_from_yaml,
    scan_profile_dir,
)

if TYPE_CHECKING:
    from chrys.service.profiles.agents.loader import ProfileDirScan
    from chrys.service.profiles.agents.schema import AgentProfile

logger = logging.getLogger(__name__)

# Stable hardcoded ids for built-in profiles.  Mirrors what is stamped into
# the YAML files under ``service/profiles/agents/builtins/``.  Used by the legacy-id
# migration so a user's shadow profile (e.g. ``~/.chrys/agents/Code.yaml``)
# inherits the same id as the builtin it overrides — preserving the
# "this is still the Code agent" identity for future sync/distribution.
_BUILTIN_IDS: dict[str, str] = {
    "Code": "b011c0de0001",
    "Explore": "b011e80e0002",
    "General": "b0119e4e0004",
    "QA": "b0119a000005",
}
BUILTIN_PROFILE_NAMES: Final = frozenset(_BUILTIN_IDS)

# The removed builtin ``Plan`` sub-agent, whose references user profiles may still carry.
_RETIRED_PLAN_PROFILE = "Plan"


@dataclass
class AgentProfileRegistrySnapshot:
    """Restorable snapshot of registry-owned in-memory state.

    Transactional saves currently mutate only ``profiles``, but the full
    registry metadata is captured so future changes stay rollback-safe.
    """

    profiles: dict[str, AgentProfile]
    builtin_profiles: dict[str, AgentProfile]
    user_dir: Path | None


def _default_user_dir() -> Path:
    from chrys.foundation.platform import get_platform

    return get_platform().config_dir / "agents"


_MAX_QUARANTINE_SUFFIX = 100


def _free_sibling(path: Path, stem: str, suffix: str) -> Path | None:
    """Return a free ``<stem>.<suffix>[-N]`` sibling of *path*, or None if all are taken."""
    base = path.with_name(f"{stem}{suffix}")
    if not base.exists():
        return base
    for n in range(2, _MAX_QUARANTINE_SUFFIX + 1):
        candidate = path.with_name(f"{stem}-{n}{suffix}")
        if not candidate.exists():
            return candidate
    return None


def _canonical_caseless(value: str) -> str:
    """Key for the Unicode canonical caseless match (``NFD(casefold(NFD(x)))``).

    Case folding alone is not enough on either side: the input must be
    normalized so decomposed and precomposed spellings fold alike, and the
    folded result must be normalized again because some foldings emit
    sequences that are canonically equivalent but not byte-identical
    (U+0390 folds to U+03B9 U+0308 U+0301, while U+0399 U+0308 U+0301
    normalizes to U+03AA U+0301 and folds to U+03CA U+0301).
    """
    return unicodedata.normalize("NFD", unicodedata.normalize("NFD", value).casefold())


def _is_same_filename_spelling(a: str, b: str) -> bool:
    """True when *a* and *b* differ only in case or Unicode normalization form.

    macOS filesystems resolve ``Café`` typed as NFC and stored as NFD to the
    same entry (HFS+ even rewrites every name to NFD), and case-insensitive
    ones fold ``code``/``Code``; such a directory listing is still the file
    that ``<name>.yaml`` addresses.
    """
    return _canonical_caseless(a) == _canonical_caseless(b)


def _quarantine_path(path: Path) -> Path | None:
    """Return a free ``<file>.conflict[-N]`` sibling for *path* (outside the ``*.yaml``/``*.yml`` scan)."""
    return _free_sibling(path, f"{path.name}.conflict", "")


def _parking_path(path: Path) -> Path | None:
    """Return a free ``<stem>.moving[-N].yaml`` sibling used to break a rename cycle mid-migration.

    The parked file stays inside the ``*.yaml`` scan on purpose: should the
    process die between the two renames, the next load picks it up as an
    ordinary non-canonical file and finishes the move.
    """
    return _free_sibling(path, f"{path.stem}.moving", ".yaml")


def _profile_id_matches(profiles: Iterable[AgentProfile], profile_id: str) -> list[AgentProfile]:
    """Return every profile carrying the non-empty stable id."""
    if not profile_id:
        return []
    return [profile for profile in profiles if profile.id == profile_id]


def _warn_ambiguous_profile_id(profile_id: str, matches: Iterable[AgentProfile]) -> None:
    names = ", ".join(repr(name) for name in sorted({profile.name for profile in matches}))
    logger.warning(
        "Agent profile id %r is ambiguous across profiles %s; refusing to resolve by id",
        profile_id,
        names,
    )


def resolve_profile_selector(profiles: Iterable[AgentProfile], selector: str) -> AgentProfile | None:
    """Resolve a profile by id, name, or display name from *profiles*."""
    raw = selector.strip()
    if not raw:
        return None
    candidates = list(profiles)
    id_matches = _profile_id_matches(candidates, raw)
    if len(id_matches) == 1:
        return id_matches[0]
    if len(id_matches) > 1:
        _warn_ambiguous_profile_id(raw, id_matches)
        return None

    exact_name_matches = [profile for profile in candidates if profile.name == raw]
    if len(exact_name_matches) == 1:
        return exact_name_matches[0]
    if len(exact_name_matches) > 1:
        logger.warning("Agent profile name %r is ambiguous; use a profile id instead", raw)
        return None

    exact_display_matches = [profile for profile in candidates if profile.display_name == raw]
    if len(exact_display_matches) == 1:
        return exact_display_matches[0]
    if len(exact_display_matches) > 1:
        logger.warning("Agent profile display name %r is ambiguous; use a profile id instead", raw)
        return None

    folded = raw.casefold()
    folded_name_matches = [profile for profile in candidates if profile.name.casefold() == folded]
    if len(folded_name_matches) == 1:
        return folded_name_matches[0]
    if len(folded_name_matches) > 1:
        logger.warning("Agent profile name %r is ambiguous ignoring case; use a profile id instead", raw)
        return None

    folded_display_matches = [
        profile for profile in candidates if profile.display_name and profile.display_name.casefold() == folded
    ]
    if len(folded_display_matches) == 1:
        return folded_display_matches[0]
    if len(folded_display_matches) > 1:
        logger.warning("Agent profile display name %r is ambiguous ignoring case; use a profile id instead", raw)
    return None


class AgentProfileRegistry:
    """Central registry for all available agent profiles.

    Manages built-in profiles, user-defined profiles, and profile switching.
    """

    def __init__(self) -> None:
        self._profiles: dict[str, AgentProfile] = {}
        self._builtin_profiles: dict[str, AgentProfile] = {}
        self._user_dir: Path | None = None

    def register(self, profile: AgentProfile) -> None:
        """Register an agent profile definition."""
        conflicts = _profile_id_matches(
            (candidate for name, candidate in self._profiles.items() if name != profile.name),
            profile.id,
        )
        if conflicts:
            names = ", ".join(repr(name) for name in sorted({candidate.name for candidate in conflicts}))
            logger.warning(
                "Duplicate agent profile id %r for profile %r conflicts with registered profiles %s; "
                "id lookups will fail closed",
                profile.id,
                profile.name,
                names,
            )
        self._profiles[profile.name] = profile

    def snapshot(self) -> AgentProfileRegistrySnapshot:
        """Return a deep snapshot of registry state for transactional callers."""
        return AgentProfileRegistrySnapshot(
            profiles=deepcopy(self._profiles),
            builtin_profiles=deepcopy(self._builtin_profiles),
            user_dir=self._user_dir,
        )

    def restore(self, snapshot: AgentProfileRegistrySnapshot) -> None:
        """Restore a snapshot created by :meth:`snapshot`."""
        self._profiles = deepcopy(snapshot.profiles)
        self._builtin_profiles = deepcopy(snapshot.builtin_profiles)
        self._user_dir = snapshot.user_dir

    def get(self, name: str) -> AgentProfile | None:
        """Get a registered agent profile by name."""
        return self._profiles.get(name)

    def get_by_id(self, profile_id: str, *, disambiguating_name: str = "") -> AgentProfile | None:
        """Get a profile by stable id, using an exact name only to break an id tie.

        A missing id never falls back to the name: that would let a deleted
        profile silently adopt a different agent whose name was later reused.
        """
        matches = _profile_id_matches(self._profiles.values(), profile_id)
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1 and disambiguating_name:
            named_matches = [profile for profile in matches if profile.name == disambiguating_name]
            if len(named_matches) == 1:
                logger.warning(
                    "Agent profile id %r is ambiguous; resolving by exact saved profile name %r",
                    profile_id,
                    disambiguating_name,
                )
                return named_matches[0]
        if len(matches) > 1:
            _warn_ambiguous_profile_id(profile_id, matches)
        return None

    def resolve_selector(self, selector: str) -> AgentProfile | None:
        """Resolve a profile by id, name, or display name.

        Selector precedence is id, exact name, exact display name, case-insensitive
        name, then case-insensitive display name. Ambiguous display/name matches
        return ``None`` so callers can ask the user to disambiguate with an id.
        """
        return resolve_profile_selector(self._profiles.values(), selector)

    def list_names(self) -> list[str]:
        """List names of all registered agent profiles."""
        return list(self._profiles.keys())

    def list_profiles(self, *, include_sub_agent_only: bool = False) -> list[AgentProfile]:
        """List registered agent profiles.

        Args:
            include_sub_agent_only: If False (default), excludes profiles marked
                ``sub_agent_only=True``.  Pass True to include all profiles.
        """
        profiles = list(self._profiles.values())
        if not include_sub_agent_only:
            profiles = [p for p in profiles if not p.sub_agent_only]
        return profiles

    def load_builtins(self) -> int:
        """Load all built-in agent profile definitions from service/profiles/agents/builtins/*.yaml.

        Returns the number of profiles loaded.
        """
        builtins_dir = Path(__file__).parent / "builtins"
        if not builtins_dir.exists():
            return 0

        loaded = 0
        # Case-folded key → original filename.  Names are case-insensitive
        # because the filesystem is (macOS/Windows), so "Code.yaml" and
        # "code.yaml" would clobber each other on disk.
        seen: dict[str, str] = {}
        for path in sorted(builtins_dir.glob("*.yaml")):
            try:
                profile = load_profile_from_yaml(path)
                key = profile.name.casefold()
                if key in seen:
                    logger.warning(
                        "Duplicate agent profile name %r in builtins (%s shadows %s) — later file wins.",
                        profile.name,
                        path.name,
                        seen[key],
                    )
                seen[key] = path.name
                if not profile.id:
                    # Builtin YAMLs must carry a hardcoded ``id``.  Refuse to
                    # silently auto-assign one because that would defeat the
                    # whole point of stable identifiers across installations.
                    logger.error(
                        "Builtin agent profile %r at %s is missing required 'id' field.",
                        profile.name,
                        path.name,
                    )
                self._builtin_profiles[profile.name] = deepcopy(profile)
                self.register(profile)
                loaded += 1
            except AgentProfileLoadError as e:
                logger.warning("Failed to load builtin agent profile %s: %s", path.name, e)
        return loaded

    def load_user_profiles(self, directory: Path | None = None) -> int:
        """Load user-defined agent profiles from a directory.

        Args:
            directory: Path to scan. Defaults to ``~/.chrys/agents/``.

        Returns the number of profiles loaded.  Warns once per name if
        two YAML files in the directory share the same ``name`` — names
        are registry keys, so later files silently shadow earlier ones.
        Files stored under a filename other than ``<name>.yaml`` are moved
        to that canonical path so later saves and deletes address them.
        """
        target = directory or _default_user_dir()
        scan = scan_profile_dir(target)
        loaded = self._canonicalize_profile_files(scan.loaded, target)
        # Case-folded names: the registry key is the raw name, but
        # filesystem casing varies, so treat "Code" == "code" for
        # warning purposes.
        seen: set[str] = set()
        registered = 0
        for p in loaded:
            key = p.name.casefold()
            if key in seen:
                logger.warning(
                    "Duplicate agent profile name %r found in %s — "
                    "rename one via the agent configuration screen to avoid shadowing.",
                    p.name,
                    target,
                )
            else:
                seen.add(key)
            self._migrate_legacy_id_if_needed(p, target)
            self.register(p)
            registered += 1
        self._drop_retired_plan_refs(loaded, scan, target)
        return registered

    def _canonicalize_profile_files(
        self, loaded: list[tuple[Path, AgentProfile]], user_dir: Path
    ) -> list[AgentProfile]:
        """Move every loaded profile file to ``<name>.yaml``; return the profiles that may be registered.

        Every write path (save, delete, rename, built-in reset) addresses a
        user profile by ``<name>.yaml``, while the loader accepts any
        ``*.yaml``/``*.yml`` file and takes the name from its contents.  A
        profile stored under another filename would therefore look editable
        but keep reappearing from the untouched file on the next load.  The
        registry only ever registers profiles whose file sits at the
        canonical path, so:

        - a file already at ``<name>.yaml`` (or a case-only alias of it on a
          case-insensitive filesystem) is registered as is;
        - a file elsewhere is moved to ``<name>.yaml`` first;
        - a file whose canonical path is taken by a *different* file that
          keeps that name is quarantined as ``<file>.conflict`` (outside the
          ``*.yaml``/``*.yml`` scan, contents untouched) and not registered —
          leaving it in place would let it resurface as soon as the canonical
          file is deleted;
        - a file that cannot be moved is not registered either, since every
          editor operation would silently miss it.

        Moves are planned over the whole directory before anything is
        quarantined: a canonical path occupied by another file that is itself
        about to move (``A.yaml`` defining ``B`` while ``B.yaml`` defines
        ``A``) is a pending target, not a conflict, so both files end up
        under their own names.  A file that should have moved away but could
        not (permissions, no free quarantine name) keeps blocking its path;
        files waiting on that path are left untouched and skipped rather than
        quarantined, so the next load can finish the migration once the
        blocker is fixed.
        """
        registrable: list[AgentProfile] = []
        # Canonical paths owned by files that stay put — the only true conflicts.
        claimed: set[Path] = set()
        # Paths still occupied by files that failed to move aside this load.
        blocked: set[Path] = set()
        pending: list[tuple[Path, AgentProfile]] = []
        for path, profile in loaded:
            canonical = user_dir / f"{profile.name}.yaml"
            if path.name == canonical.name:
                claimed.add(canonical)
                registrable.append(profile)
            else:
                pending.append((path, profile))

        while pending:
            pending_paths = {path for path, _profile in pending}
            waiting: list[tuple[Path, AgentProfile]] = []
            for path, profile in pending:
                canonical = user_dir / f"{profile.name}.yaml"
                try:
                    if canonical in claimed:
                        if not self._quarantine_conflicting_profile(profile, path, canonical):
                            blocked.add(path)
                        continue
                    if canonical in blocked:
                        # The occupant wanted to leave but could not; it is not a
                        # rival for the name, so leave this file alone as well.
                        self._warn_blocked_profile(profile, path, canonical)
                        blocked.add(path)
                        continue
                    if canonical.exists():
                        if canonical in pending_paths:
                            # Occupied by a file that is itself moving away; retry once it has.
                            waiting.append((path, profile))
                            continue
                        if _is_same_filename_spelling(path.name, canonical.name) and canonical.samefile(path):
                            # Case- or normalization-only difference on a filesystem that
                            # folds those: name-based lookups already resolve to this file.
                            # Any other alias (e.g. a hard link) would resurface after a delete.
                            claimed.add(canonical)
                            registrable.append(profile)
                            continue
                        if not self._quarantine_conflicting_profile(profile, path, canonical):
                            blocked.add(path)
                        continue
                    path.rename(canonical)
                except OSError as exc:
                    self._warn_unmovable_profile(profile, path, canonical, exc)
                    blocked.add(path)
                    continue
                logger.info("Moved agent profile %r from %s to canonical path %s.", profile.name, path.name, canonical)
                claimed.add(canonical)
                registrable.append(profile)
            if len(waiting) == len(pending):
                # Every remaining file waits on another remaining file, so the
                # targets form a cycle.  Park one file that is itself a target
                # under a temporary name; the file waiting on it can then move
                # and the cycle unwinds over the following passes.
                targets = {user_dir / f"{profile.name}.yaml" for _path, profile in waiting}
                index = next(i for i, (path, _profile) in enumerate(waiting) if path in targets)
                path, profile = waiting[index]
                parked = _parking_path(path)
                try:
                    if parked is None:
                        raise OSError("no free temporary name")
                    path.rename(parked)
                except OSError as exc:
                    self._warn_unmovable_profile(profile, path, user_dir / f"{profile.name}.yaml", exc)
                    blocked.add(path)
                    waiting.pop(index)
                else:
                    waiting[index] = (parked, profile)
            pending = waiting
        return registrable

    @staticmethod
    def _warn_unmovable_profile(profile: AgentProfile, path: Path, canonical: Path, exc: OSError) -> None:
        logger.warning(
            "Ignoring agent profile %r in %s: it could not be moved to %s (%s). "
            "Rename the file yourself or fix the directory permissions to use it.",
            profile.name,
            path,
            canonical.name,
            exc,
        )

    @staticmethod
    def _warn_blocked_profile(profile: AgentProfile, path: Path, canonical: Path) -> None:
        logger.warning(
            "Ignoring agent profile %r in %s for now: %s is still occupied by a file that could not be "
            "moved aside. It will be moved into place on the next load once that file is fixed.",
            profile.name,
            path,
            canonical.name,
        )

    @staticmethod
    def _quarantine_conflicting_profile(profile: AgentProfile, path: Path, canonical: Path) -> bool:
        """Move a duplicate source out of the scan as ``<file>.conflict``; contents are untouched.

        Returns whether *path* was actually vacated; a file that stays behind
        keeps blocking that path for anything waiting to move there.
        """
        quarantine = _quarantine_path(path)
        if quarantine is not None:
            try:
                path.rename(quarantine)
            except OSError as exc:
                logger.warning(
                    "Ignoring agent profile %r in %s because %s already defines that name; the duplicate "
                    "could not be moved aside (%s) — rename or remove it to keep it from resurfacing.",
                    profile.name,
                    path,
                    canonical.name,
                    exc,
                )
                return False
            logger.warning(
                "Agent profile %r in %s duplicates %s; moved it to %s so the canonical file stays the only "
                "source. Rename it back under a different name if you still need it.",
                profile.name,
                path.name,
                canonical.name,
                quarantine.name,
            )
            return True
        logger.warning(
            "Ignoring agent profile %r in %s because %s already defines that name and no free quarantine "
            "name was available; rename or remove the extra file.",
            profile.name,
            path,
            canonical.name,
        )
        return False

    # =========================================================================
    # BEGIN legacy-id migration
    # ---------------------------------------------------------------------
    # User profile YAMLs written before the ``id`` field existed don't have
    # one.  We mint a UUID-hex (12 chars, matching ModelProfile) and rewrite
    # the file in place so the id sticks across runs.
    #
    # TODO(remove-after-migration): once user profiles in the wild all carry
    # an ``id:`` field, delete this method and its single caller in
    # ``load_user_profiles``.  The schema default of ``id: ""`` plus the
    # builtin sanity check in ``load_builtins`` is enough on its own.
    # =========================================================================
    def _migrate_legacy_id_if_needed(self, profile: AgentProfile, user_dir: Path) -> None:
        """Assign an id and rewrite the YAML if the loaded profile lacks one.

        If the profile's name matches a built-in (e.g. a user's customized
        ``Code.yaml`` that shadows the builtin), reuse the builtin's
        hardcoded id so the two stay linked for sync/distribution.
        Otherwise mint a fresh UUID-hex.  Callers guarantee the profile's
        file already sits at ``<name>.yaml`` (see
        :meth:`_canonicalize_profile_filename`), so the rewrite updates the
        file that was loaded rather than creating a second one.

        A failed rewrite restores the empty legacy id in memory.  Exposing an
        unpersisted generated id would let sessions record an identity that
        changes on the next process start.
        """
        if profile.id:
            return
        from chrys.service.profiles.agents.serializer import save_profile

        builtin_id = _BUILTIN_IDS.get(profile.name)
        migrated_id = builtin_id or uuid.uuid4().hex[:12]
        profile.id = migrated_id
        source = "builtin" if builtin_id else "generated"
        try:
            save_profile(profile, target_dir=user_dir)
        except (OSError, ValueError) as exc:
            profile.id = ""
            logger.warning(
                "Could not persist migrated id %s for legacy profile %r in %s: %s",
                migrated_id,
                profile.name,
                user_dir,
                exc,
            )
            return
        logger.info(
            "Migrated legacy agent profile %r: assigned %s id %s and rewrote %s.",
            profile.name,
            source,
            profile.id,
            user_dir / f"{profile.name}.yaml",
        )

    # =========================================================================
    # END legacy-id migration
    # =========================================================================

    # =========================================================================
    # BEGIN retired Plan sub-agent migration
    # ---------------------------------------------------------------------
    # The builtin ``Plan`` sub-agent was removed.  User profiles saved while it
    # shipped — every ``Code.yaml`` customized from the agent configuration
    # screen among them — still list it under ``sub_agents``, where every
    # build would now skip it as unknown.  Those references are dropped and
    # the file is rewritten once.
    #
    # TODO(remove-after-migration): once user profiles in the wild no longer
    # reference ``Plan``, delete this method, its single caller in
    # ``load_user_profiles`` and ``_RETIRED_PLAN_PROFILE``.
    # =========================================================================
    def _drop_retired_plan_refs(self, profiles: Iterable[AgentProfile], scan: ProfileDirScan, user_dir: Path) -> None:
        """Remove user profiles' sub-agent references to the retired builtin ``Plan`` and rewrite their YAML.

        A ``Plan`` profile of the user's own keeps every reference valid, so
        nothing is touched while one exists: registered, or parsed but kept
        out of the registry by a file move that failed.  A profile file that
        failed to parse might be that ``Plan``, so the migration waits until
        the directory loads cleanly.  A failed rewrite still drops the
        references in memory and is retried on the next load.
        """
        referencing = [
            profile
            for profile in profiles
            if any(ref.profile == _RETIRED_PLAN_PROFILE for ref in profile.sub_agents.agents)
        ]
        if not referencing:
            return
        if self.get(_RETIRED_PLAN_PROFILE) is not None or any(
            parsed.name == _RETIRED_PLAN_PROFILE for _path, parsed in scan.loaded
        ):
            return
        if scan.invalid:
            logger.warning(
                "Keeping references to the removed Plan sub-agent in %s for now: %s could not be loaded and "
                "may define a Plan profile of your own. Fix or remove it to finish the migration.",
                ", ".join(repr(profile.name) for profile in referencing),
                ", ".join(path.name for path in scan.invalid),
            )
            return
        from chrys.service.profiles.agents.serializer import save_profile

        for profile in referencing:
            profile.sub_agents.agents = [
                ref for ref in profile.sub_agents.agents if ref.profile != _RETIRED_PLAN_PROFILE
            ]
            try:
                path = save_profile(profile, target_dir=user_dir)
            except (OSError, ValueError) as exc:
                logger.warning(
                    "Dropped agent profile %r's references to the removed Plan sub-agent for this run but could "
                    "not rewrite its file in %s: %s",
                    profile.name,
                    user_dir,
                    exc,
                )
                continue
            logger.info(
                "Dropped agent profile %r's references to the removed Plan sub-agent and rewrote %s.",
                profile.name,
                path,
            )

    # =========================================================================
    # END retired Plan sub-agent migration
    # =========================================================================

    def remove(self, name: str, *, cascade: bool = True) -> bool:
        """Remove a profile from the registry.

        Args:
            name: Registry name to remove.
            cascade: When True, remove sub-agent references to ``name`` from
                other profiles. Transactional rename paths pass False after
                retargeting references themselves.
        """
        existed = self._profiles.pop(name, None) is not None
        if existed and cascade:
            self._remove_sub_agent_refs(name)
        return existed

    def is_builtin(self, name: str) -> bool:
        """Check whether a profile was loaded from the builtins directory."""
        return name in self._builtin_profiles

    def resolve_builtin_filename(self, name: str) -> str | None:
        """Resolve a name that could address a builtin's shadow on a caseless filesystem.

        This is a deletion guard, not profile identity: registered names and
        reset templates retain exact spelling, including distinct user names.
        """
        return next(
            (builtin_name for builtin_name in self._builtin_profiles if _is_same_filename_spelling(name, builtin_name)),
            None,
        )

    def get_builtin_template(self, name: str) -> AgentProfile | None:
        """Return an isolated copy of the pristine built-in profile named *name*."""
        profile = self._builtin_profiles.get(name)
        return deepcopy(profile) if profile is not None else None

    def build_builtin_reset(
        self,
        name: str,
        *,
        preserve_from: AgentProfile | None = None,
    ) -> AgentProfile:
        """Build a pristine built-in profile while retaining user-owned integrations."""
        template = self.get_builtin_template(name)
        if template is None:
            msg = f"Agent profile is not built-in: {name}"
            raise ValueError(msg)
        current = preserve_from or self._profiles.get(name)
        if current is None:
            msg = f"Agent profile not found: {name}"
            raise ValueError(msg)
        template.skills = deepcopy(current.skills)
        template.tools.mcp = deepcopy(current.tools.mcp)
        template.memory = deepcopy(current.memory)
        return template

    def profiles_referencing(self, name: str) -> list[AgentProfile]:
        """Return other profiles whose sub-agent references a removal would change."""
        return [
            profile
            for profile in self._profiles.values()
            if profile.name != name and any(ref.profile == name for ref in profile.sub_agents.agents)
        ]

    def _remove_sub_agent_refs(self, name: str) -> None:
        """Remove all sub-agent references to *name* from other profiles and persist changes."""
        from chrys.service.profiles.agents.serializer import save_profile

        for profile in self.profiles_referencing(name):
            profile.sub_agents.agents = [a for a in profile.sub_agents.agents if a.profile != name]
            if not self.is_builtin(profile.name):
                save_profile(profile)

    def load_all(self, user_dir: Path | None = None) -> int:
        """Load builtins + user profiles. Returns total loaded."""
        target = user_dir or _default_user_dir()
        self._user_dir = target
        with contextlib.suppress(OSError):
            (target / ".hidden.yaml").unlink(missing_ok=True)
        count = self.load_builtins()
        count += self.load_user_profiles(target)
        return count


def resolve_launch_profile(registry: AgentProfileRegistry, preferred_name: str) -> AgentProfile | None:
    """Resolve a main agent for a fresh start, falling back through Code to the first visible profile.

    The TUI's launch profile, and the agent a new session starts on once the
    one it named no longer resolves.
    """
    requested = preferred_name.strip() or DEFAULT_AGENT_PROFILE
    # Match the TUI picker: hidden and sub-agent-only profiles are not valid main launch profiles.
    profiles = registry.list_profiles()
    by_name = {profile.name: profile for profile in profiles}

    profile = resolve_profile_selector(profiles, requested)
    if profile is not None:
        return profile

    if requested != DEFAULT_AGENT_PROFILE:
        fallback = by_name.get(DEFAULT_AGENT_PROFILE)
        if fallback is not None:
            logger.warning(
                "Default agent profile %r is not usable as a launch profile, falling back to %r.",
                requested,
                DEFAULT_AGENT_PROFILE,
            )
            return fallback

    if profiles:
        logger.warning(
            "Default agent profile %r is not usable as a launch profile, falling back to %r.",
            requested,
            profiles[0].name,
        )
        return profiles[0]
    return None
