# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Model profile serializer — convert ModelProfile dataclasses to YAML-ready dicts."""

from __future__ import annotations

import logging
from dataclasses import MISSING, fields
from typing import TYPE_CHECKING, Any

from chrys.foundation.text.yaml_io import dump_yaml
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


def _require_filename_safe_id(profile_id: str) -> None:
    """Refuse ids that would resolve ``<id>.yaml`` outside the models directory."""
    if not is_filename_safe_profile_name(profile_id):
        msg = f"Model profile id {profile_id!r} is not filename-safe; use a bare id, not a path"
        raise ValueError(msg)


def save_profile(profile: ModelProfile) -> Path:
    """Serialize *profile* to YAML and write to ``~/.chrys/models/{id}.yaml``.

    Returns the path written.

    Raises:
        ValueError: If ``profile.id`` is not filename-safe.
    """
    _require_filename_safe_id(profile.id)
    target_dir = _user_profiles_dir()
    target_dir.mkdir(parents=True, exist_ok=True)
    path = target_dir / f"{profile.id}.yaml"
    data = profile_to_dict(profile)
    text = dump_yaml(data)
    path.write_text(text, encoding="utf-8")
    logger.debug("Saved model profile to %s", path)
    return path


def delete_profile(profile_id: str) -> bool:
    """Delete the model profile YAML file for *profile_id*.

    Returns True if the file existed and was removed, False otherwise.

    Raises:
        ValueError: If ``profile_id`` is not filename-safe.
    """
    _require_filename_safe_id(profile_id)
    path = _user_profiles_dir() / f"{profile_id}.yaml"
    if path.is_file():
        path.unlink()
        logger.debug("Deleted model profile %s", path)
        return True
    return False
