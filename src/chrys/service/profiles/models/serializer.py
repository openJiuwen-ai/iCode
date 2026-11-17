# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Model profile serializer — convert ModelProfile dataclasses to YAML-ready dicts."""

from __future__ import annotations

import logging
from dataclasses import MISSING, fields
from typing import TYPE_CHECKING, Any

from chrys.foundation.platform.files import atomic_write_owner_only_text
from chrys.foundation.text.yaml_io import dump_yaml
from chrys.foundation.trajectory.keys import ensure_owner_only_directory
from chrys.service.profiles.agents.loader import is_filename_safe_profile_name

if TYPE_CHECKING:
    from pathlib import Path

    from chrys.service.profiles.models.schema import ModelProfile

logger = logging.getLogger(__name__)


def profile_to_dict(profile: ModelProfile) -> dict[str, Any]:
    """Serialize a ModelProfile to a dict suitable for ``yaml.dump()``.

    Fields at their default value are omitted (except ``id`` and ``name``
    which are always emitted).
    """
    result: dict[str, Any] = {}
    always_emit = {"id", "name"}
    for f in fields(profile):
        val = getattr(profile, f.name)
        if f.name not in always_emit:
            if f.default is not MISSING and val == f.default:
                continue
            if f.default_factory is not MISSING:
                try:
                    if val == f.default_factory():
                        continue
                except TypeError:
                    pass
        result[f.name] = val
    return result


def _user_profiles_dir() -> Path:
    """Return the user model profiles directory."""
    from chrys.foundation.platform import get_platform

    return get_platform().config_dir / "models"


def save_profile(profile: ModelProfile) -> Path:
    """Serialize *profile* to YAML and write to ``~/.chrys/models/{id}.yaml``.

    The file holds the provider API key, so it is written atomically and
    owner-only: 0600 on POSIX, an owner-only DACL on Windows. The ``models/``
    directory is created (or tightened) owner-only where the OS has POSIX
    modes, and a symbolic link in its place is refused on every platform.

    Raises:
        ValueError: If ``profile.id`` is not filename-safe. The loader scans
            only the top level of the directory, so an id carrying a path
            separator would be written where nothing ever reads it back.

    Returns the path written.
    """
    if not is_filename_safe_profile_name(profile.id):
        msg = f"Model profile id {profile.id!r} is not filename-safe; use a bare name, not a path"
        raise ValueError(msg)
    target_dir = _user_profiles_dir()
    ensure_owner_only_directory(target_dir)
    path = target_dir / f"{profile.id}.yaml"
    data = profile_to_dict(profile)
    text = dump_yaml(data)
    atomic_write_owner_only_text(path, text)
    logger.debug("Saved model profile to %s", path)
    return path


def delete_profile(profile_id: str) -> bool:
    """Delete the model profile YAML file for *profile_id*.

    Returns True if the file existed and was removed, False otherwise.
    """
    path = _user_profiles_dir() / f"{profile_id}.yaml"
    if path.is_file():
        path.unlink()
        logger.debug("Deleted model profile %s", path)
        return True
    return False
