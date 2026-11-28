# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Duplicate-id handling tests for ModelProfileRegistry."""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from chrys.service.profiles.file_scan import ProfileDirScan
from chrys.service.profiles.models.registry import ModelProfileRegistry
from chrys.service.profiles.models.schema import ModelProfile

_LOGGER = "chrys.service.profiles.models.registry"


def _scan(*entries: tuple[str, str]) -> ProfileDirScan[ModelProfile]:
    """Build a scan result from (filename, profile id) pairs."""
    scan = ProfileDirScan[ModelProfile]()
    for filename, profile_id in entries:
        path = Path("/profiles") / filename
        scan.loaded.append((path, ModelProfile(id=profile_id, name=filename)))
    return scan


def test_duplicate_id_logs_warning(caplog: pytest.LogCaptureFixture) -> None:
    """Two files sharing an id should warn instead of silently overwriting."""
    registry = ModelProfileRegistry()
    scan = _scan(("a.yaml", "dup"), ("b.yaml", "dup"))
    with caplog.at_level(logging.WARNING, logger=_LOGGER):
        count = registry.install_profiles(Path("/profiles"), scan)
    assert count == 2
    assert registry.list_ids() == ["dup"]
    assert "Duplicate model profile id 'dup'" in caplog.text
    assert "a.yaml" in caplog.text
    assert "b.yaml" in caplog.text


def test_distinct_ids_do_not_warn(caplog: pytest.LogCaptureFixture) -> None:
    """Profiles with unique ids load without a duplicate-id warning."""
    registry = ModelProfileRegistry()
    scan = _scan(("a.yaml", "one"), ("b.yaml", "two"))
    with caplog.at_level(logging.WARNING, logger=_LOGGER):
        count = registry.install_profiles(Path("/profiles"), scan)
    assert count == 2
    assert sorted(registry.list_ids()) == ["one", "two"]
    assert "Duplicate model profile id" not in caplog.text
