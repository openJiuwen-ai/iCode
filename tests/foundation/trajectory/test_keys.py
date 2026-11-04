# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

from __future__ import annotations

import asyncio
import errno
import os
import stat
from pathlib import Path
from unittest.mock import Mock

import pytest

from chrys.foundation.platform import get_platform, key_files
from chrys.foundation.platform.files import SecureFileError
from chrys.foundation.trajectory import keys as keys_module
from chrys.foundation.trajectory.fingerprint import (
    DOMAIN_TOOL_ARGUMENTS,
    DOMAIN_TOOL_CONTENT,
    FINGERPRINT_KEY_BYTES,
    canonical_json_bytes,
    fingerprint_json,
    fingerprint_text,
    keyed_fingerprint,
)
from chrys.foundation.trajectory.keys import (
    TRAJECTORY_KEY_FILE_NAME,
    ensure_owner_only_directory,
    load_or_create_fingerprint_key,
)
from tests.support.secure_files import plant_owner_only_bytes
from tests.support.symlinks import symlink_or_skip


def test_load_or_create_key_is_stable_owner_only_and_32_bytes(tmp_path: Path) -> None:
    config_dir = tmp_path / "config"
    key = load_or_create_fingerprint_key(config_dir)
    assert len(key) == FINGERPRINT_KEY_BYTES
    key_path = config_dir / TRAJECTORY_KEY_FILE_NAME
    assert key_path.read_bytes() == key
    if not get_platform().is_windows:
        assert stat.S_IMODE(key_path.stat().st_mode) == 0o600
    assert load_or_create_fingerprint_key(config_dir) == key
    # No temp files linger.
    assert [p.name for p in config_dir.iterdir() if p.name.endswith(".tmp")] == []


def test_existing_key_is_never_overwritten(tmp_path: Path) -> None:
    config_dir = tmp_path / "config"
    first = load_or_create_fingerprint_key(config_dir)
    second = load_or_create_fingerprint_key(config_dir)
    assert first == second


def test_invalid_existing_key_is_rejected_not_repaired(tmp_path: Path) -> None:
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    key_path = config_dir / TRAJECTORY_KEY_FILE_NAME
    fd = os.open(key_path, os.O_WRONLY | os.O_CREAT, 0o600)
    os.write(fd, b"short")
    os.close(fd)
    with pytest.raises(SecureFileError):
        load_or_create_fingerprint_key(config_dir)
    assert key_path.read_bytes() == b"short"


@pytest.mark.parametrize("publication", ["native", "no_hard_links", "directory"])
async def test_concurrent_creators_agree_on_one_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, publication: str
) -> None:
    if publication == "no_hard_links":
        monkeypatch.setattr(os, "link", Mock(side_effect=OSError(errno.ENOTSUP, "Hard links unavailable")))
    elif publication == "directory":
        monkeypatch.setattr(
            key_files, "publish_file_no_replace", Mock(side_effect=OSError(errno.ENOTSUP, "No atomic file publish"))
        )
    config_dir = tmp_path / "config"
    keys = await asyncio.gather(*(asyncio.to_thread(load_or_create_fingerprint_key, config_dir) for _ in range(8)))
    assert len(set(keys)) == 1
    assert len(keys[0]) == FINGERPRINT_KEY_BYTES
    key_path = config_dir / TRAJECTORY_KEY_FILE_NAME
    assert key_path.is_dir() is (publication == "directory")
    assert key_files.read_owner_only_key(key_path, max_bytes=33) == keys[0]
    assert list(config_dir.glob("*.tmp")) == []


@pytest.mark.parametrize("winner", [b"w" * FINGERPRINT_KEY_BYTES, b"incomplete"])
def test_key_publication_without_hard_links_validates_a_racing_creator(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, winner: bytes
) -> None:
    from chrys.foundation.platform.files import secure_open_owner_only

    publish = keys_module.publish_owner_only_key

    def racing_publish(source: Path, target: Path) -> None:
        fd = secure_open_owner_only(target, write=True, create=True)
        with os.fdopen(fd, "wb") as file:
            file.write(winner)
        publish(source, target)

    monkeypatch.setattr(os, "link", Mock(side_effect=OSError(errno.ENOTSUP, "Hard links unavailable")))
    monkeypatch.setattr(keys_module, "publish_owner_only_key", racing_publish)

    if len(winner) == FINGERPRINT_KEY_BYTES:
        assert load_or_create_fingerprint_key(tmp_path) == winner
    else:
        with pytest.raises(SecureFileError, match="invalid length"):
            load_or_create_fingerprint_key(tmp_path)
    assert (tmp_path / TRAJECTORY_KEY_FILE_NAME).read_bytes() == winner
    assert list(tmp_path.glob("*.tmp")) == []


def test_directory_key_survives_restart_on_a_filesystem_with_atomic_publication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with monkeypatch.context() as patch:
        patch.setattr(
            key_files, "publish_file_no_replace", Mock(side_effect=OSError(errno.ENOTSUP, "No atomic file publish"))
        )
        original = load_or_create_fingerprint_key(tmp_path)
    assert (tmp_path / TRAJECTORY_KEY_FILE_NAME).is_dir()
    assert load_or_create_fingerprint_key(tmp_path) == original


@pytest.mark.parametrize("debris", [b"", b"partial", b"x" * FINGERPRINT_KEY_BYTES])
def test_incomplete_staging_directory_is_never_taken_as_the_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, debris: bytes
) -> None:
    interrupted = tmp_path / f".{TRAJECTORY_KEY_FILE_NAME}.interrupted.tmp"
    interrupted.mkdir()
    plant_owner_only_bytes(interrupted / "key", debris)
    monkeypatch.setattr(
        key_files, "publish_file_no_replace", Mock(side_effect=OSError(errno.ENOTSUP, "No atomic file publish"))
    )

    key = load_or_create_fingerprint_key(tmp_path)

    assert len(key) == FINGERPRINT_KEY_BYTES
    assert load_or_create_fingerprint_key(tmp_path) == key
    assert (interrupted / "key").read_bytes() == debris


@pytest.mark.parametrize("payload", [None, b"", b"short"])
def test_invalid_existing_directory_key_is_rejected_not_repaired(tmp_path: Path, payload: bytes | None) -> None:
    key_path = tmp_path / TRAJECTORY_KEY_FILE_NAME
    key_path.mkdir()
    if payload is not None:
        plant_owner_only_bytes(key_path / "key", payload)
    with pytest.raises(SecureFileError):
        load_or_create_fingerprint_key(tmp_path)
    assert key_path.is_dir()
    if payload is None:
        assert list(key_path.iterdir()) == []
    else:
        assert (key_path / "key").read_bytes() == payload


def test_ensure_owner_only_directory_creates_and_tightens(tmp_path: Path) -> None:
    target = tmp_path / "session" / "trajectory"
    ensure_owner_only_directory(target)
    assert target.is_dir()
    if get_platform().is_windows:
        return
    assert stat.S_IMODE(target.stat().st_mode) == 0o700
    target.chmod(0o755)
    ensure_owner_only_directory(target)
    assert stat.S_IMODE(target.stat().st_mode) == 0o700


def test_ensure_owner_only_directory_rejects_a_file(tmp_path: Path) -> None:
    target = tmp_path / "not-a-dir"
    target.write_bytes(b"")
    with pytest.raises(OSError):
        ensure_owner_only_directory(target)


@pytest.mark.parametrize("link_target", ["directory", "file", "missing"])
def test_ensure_owner_only_directory_names_a_link_as_insecure(tmp_path: Path, link_target: str) -> None:
    if link_target == "directory" and get_platform().is_windows:
        pytest.skip("the Windows branch neither opens nor tightens the directory")
    elsewhere = tmp_path / "elsewhere"
    if link_target == "directory":
        elsewhere.mkdir()
    elif link_target == "file":
        elsewhere.write_bytes(b"")
    target = tmp_path / "trajectory"
    symlink_or_skip(target, elsewhere, target_is_directory=link_target != "file")
    with pytest.raises(SecureFileError, match="symbolic link"):
        ensure_owner_only_directory(target)


def test_fingerprints_are_keyed_and_domain_separated() -> None:
    key_a = b"a" * 32
    key_b = b"b" * 32
    data = b"same-input"
    assert keyed_fingerprint(key_a, DOMAIN_TOOL_ARGUMENTS, data) != keyed_fingerprint(
        key_b, DOMAIN_TOOL_ARGUMENTS, data
    )
    assert keyed_fingerprint(key_a, DOMAIN_TOOL_ARGUMENTS, data) != keyed_fingerprint(key_a, DOMAIN_TOOL_CONTENT, data)
    assert keyed_fingerprint(key_a, DOMAIN_TOOL_ARGUMENTS, data) == keyed_fingerprint(
        key_a, DOMAIN_TOOL_ARGUMENTS, data
    )
    assert len(keyed_fingerprint(key_a, DOMAIN_TOOL_ARGUMENTS, data)) == 64
    with pytest.raises(ValueError):
        keyed_fingerprint(b"", DOMAIN_TOOL_ARGUMENTS, data)
    with pytest.raises(ValueError):
        keyed_fingerprint(key_a, "bad\x00domain", data)


def test_canonical_json_is_order_independent_and_injective_for_surrogates() -> None:
    assert canonical_json_bytes({"b": 1, "a": [1, 2]}) == canonical_json_bytes({"a": [1, 2], "b": 1})
    assert canonical_json_bytes({"s": "\udcff"}) != canonical_json_bytes({"s": "\\udcff"})
    key = b"k" * 32
    assert fingerprint_json(key, DOMAIN_TOOL_ARGUMENTS, {"x": 1}) == fingerprint_json(
        key, DOMAIN_TOOL_ARGUMENTS, {"x": 1}
    )
    assert fingerprint_text(key, DOMAIN_TOOL_CONTENT, "hi\udcff") == fingerprint_text(
        key, DOMAIN_TOOL_CONTENT, "hi\udcff"
    )
    # ...and the escape a user typed is not the surrogate it spells.
    assert fingerprint_text(key, DOMAIN_TOOL_CONTENT, "hi\udcff") != fingerprint_text(
        key, DOMAIN_TOOL_CONTENT, "hi\\udcff"
    )
