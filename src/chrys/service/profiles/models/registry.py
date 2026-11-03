# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""ModelProfileRegistry — register, load, and manage model profiles."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING

from chrys.service.profiles.models.loader import scan_profile_dir

if TYPE_CHECKING:
    from chrys.service.profiles.file_scan import ProfileDirScan
    from chrys.service.profiles.models.schema import ModelProfile

logger = logging.getLogger(__name__)


def _default_user_dir() -> Path:
    from chrys.foundation.platform import get_platform

    return get_platform().config_dir / "models"


class ModelProfileRegistry:
    """Central registry for all available model profiles.

    Manages user-defined model profiles stored in ``~/.chrys/models/``.
    Unlike ``AgentProfileRegistry``, there are no built-in profiles —
    users create and manage all profiles themselves.
    """

    def __init__(self) -> None:
        self._profiles: dict[str, ModelProfile] = {}
        self._user_dir: Path | None = None
        self.sources: dict[Path, ModelProfile] = {}
        self.revision = 0

    @property
    def directory(self) -> Path:
        return self._user_dir or _default_user_dir()

    def reload_base(self) -> ModelProfileRegistry:
        view = ModelProfileRegistry()
        view._user_dir = self._user_dir
        disk_ids = {p.id for p in self.sources.values()}
        view._profiles = {key: p for key, p in self._profiles.items() if key not in disk_ids}
        return view

    def install_reload(self, view: ModelProfileRegistry) -> None:
        self._profiles = view._profiles
        self.sources = view.sources
        self.revision += 1

    def register(self, profile: ModelProfile, *, source: Path | None = None) -> None:
        """Register a model profile."""
        self._profiles[profile.id] = profile
        if source is not None:
            self.sources[source] = profile
        elif self._user_dir is not None:
            self.sources[self._user_dir / f"{profile.id}.yaml"] = profile
        self.revision += 1

    def get(self, profile_id: str) -> ModelProfile | None:
        """Get a registered model profile by ID."""
        return self._profiles.get(profile_id)

    def list_profiles(self) -> list[ModelProfile]:
        """List all registered model profiles."""
        return list(self._profiles.values())

    def list_ids(self) -> list[str]:
        """List IDs of all registered model profiles."""
        return list(self._profiles.keys())

    def read_profiles(self, directory: Path | None = None) -> tuple[Path, ProfileDirScan[ModelProfile]]:
        """Read profile files without mutating this registry.

        Keeping acquisition separate lets async frontends move disk I/O to a
        worker thread and install the result back on their owning event loop.
        """
        target = directory or _default_user_dir()
        return target, scan_profile_dir(target)

    def install_profiles(self, directory: Path, profiles: ProfileDirScan[ModelProfile]) -> int:
        """Install profiles already read from *directory* into this registry."""
        self._user_dir = directory
        # Case-folded key → first-seen id.  Name comparison is case-
        # insensitive to match the uniqueness rule enforced by the UI
        # (avoids the user creating two profiles that only differ in
        # case and then being unable to tell them apart in listings).
        seen_names: dict[str, str] = {}
        for path, profile in profiles.loaded:
            key = profile.name.casefold()
            if key in seen_names:
                logger.warning(
                    "Duplicate model profile name %r found (ids: %s, %s) — "
                    "rename one via the model configuration screen to avoid ambiguity.",
                    profile.name,
                    seen_names[key],
                    profile.id,
                )
            else:
                seen_names[key] = profile.id
            self.register(profile, source=path)
        return len(profiles.loaded)

    def load_profiles(self, directory: Path | None = None) -> int:
        """Load model profiles from a directory.

        Args:
            directory: Path to scan. Defaults to ``~/.chrys/models/``.

        Returns the number of profiles loaded.  Emits a warning if two
        loaded profiles share a display name — ids disambiguate them in
        the registry, but name collisions make the UI ambiguous.
        """
        target, profiles = self.read_profiles(directory)
        return self.install_profiles(target, profiles)

    def remove(self, profile_id: str) -> bool:
        """Remove a profile from the registry."""
        if self._profiles.pop(profile_id, None) is None:
            return False
        self.sources = {path: p for path, p in self.sources.items() if p.id != profile_id}
        self.revision += 1
        return True

    def load_all(self, user_dir: Path | None = None) -> int:
        """Load all user profiles. Returns total loaded."""
        return self.load_profiles(user_dir)
