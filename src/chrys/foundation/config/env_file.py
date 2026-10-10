# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Atomic updates for Chrys's process-independent configuration dotenv."""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path

from chrys.foundation.platform.files import atomic_write_text, digest_bytes
from chrys.foundation.text.encoding import decode_bytes
from chrys.foundation.util.lock import FileLock


def config_env_path() -> Path:
    """Return the canonical Chrys configuration dotenv path."""
    from chrys.foundation.platform import get_platform

    return get_platform().config_dir / ".env"


def env_lock_path(path: Path) -> Path:
    """Return the sibling lock path guarding the dotenv file at *path*.

    Shared with the readers that have to prove they read what they are about to
    delete from: two spellings of this name would be two locks over one file,
    which is the same as no lock at all.
    """
    return path.with_name(f"{path.name}.lock")


def _validate_single_line(name: str, value: str) -> None:
    if "\r" in value or "\n" in value:
        msg = f"{name} must not contain newline characters"
        raise ValueError(msg)


def _dotenv_key(line: str) -> str | None:
    """Return the key from a parseable dotenv assignment line."""
    stripped = line.strip()
    if not stripped or stripped.startswith("#") or "=" not in stripped:
        return None
    if stripped.startswith("export "):
        stripped = stripped.removeprefix("export ").lstrip()
    key = stripped.split("=", 1)[0].strip()
    if not key or any(char.isspace() for char in key):
        return None
    return key


def _line_ending(line: str) -> str:
    if line.endswith("\r\n"):
        return "\r\n"
    if line.endswith(("\r", "\n")):
        return line[-1]
    return ""


def _quoted_assignment(key: str, value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'{key}="{escaped}"'


def update_env_file(
    updates: dict[str, str],
    *,
    remove_keys: Iterable[str] = (),
    expect_fingerprint: str | None = None,
) -> bool:
    """Atomically merge and remove dotenv keys while preserving unrelated lines.

    Removal matches the way the OS reads the file, not the way the line is
    spelled: on Windows the environment is case-insensitive, so a lowercase
    ``chrys_theme`` line answers for ``CHRYS_THEME`` and must go when that key
    goes — otherwise the migration leaves behind a spelling that keeps
    overriding the user's document. On POSIX the spellings are distinct keys
    and only the exact one is touched.

    Args:
        expect_fingerprint: Refuse the whole edit unless the file still digests
            to this. Checked *inside* the lock, which is the only place the
            check means anything — a caller comparing beforehand has merely
            moved its own race. Removals are the reason it exists: they delete
            lines chosen from an earlier read of this file, and between the two
            the file may have grown a key that read never saw.

    Returns whether the file was left in the requested state; ``False`` only
    ever means the fingerprint did not match, and nothing was written.
    """
    # Function-scope import: this module is imported by ``settings``, which
    # ``env_layers`` imports in turn.
    from chrys.foundation.config.env_layers import canonical_env_name

    removals = tuple(remove_keys)
    for key, value in updates.items():
        _validate_single_line("dotenv key", key)
        _validate_single_line(f"dotenv value for {key}", value)
    for key in removals:
        _validate_single_line("dotenv key", key)

    path = config_env_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = env_lock_path(path)
    removal_set = {canonical_env_name(key) for key in removals}

    with FileLock(lock_path):
        # One read answers both "is this still the file we were promised" and
        # "what is in it". Digesting the path and then opening it is two reads,
        # and a writer that does not take this lock — any text editor — can
        # land between them: the check passes on the old bytes and the rewrite
        # then strips keys out of the new ones. Digesting what was actually
        # read leaves nothing between the two to lose.
        raw = path.read_bytes() if path.is_file() else None
        if expect_fingerprint is not None and digest_bytes(raw) != expect_fingerprint:
            return False
        existed = raw is not None
        # ``newline=""`` is what the old text-mode read used, i.e. no newline
        # translation, so decoding the bytes reproduces it exactly.
        original = decode_bytes(raw, errors="strict") if raw is not None else ""

        found_updates: set[str] = set()
        rewritten: list[str] = []
        for line in original.splitlines(keepends=True):
            key = _dotenv_key(line)
            if key is not None and canonical_env_name(key) in removal_set:
                continue
            if key in updates:
                found_updates.add(key)
                rewritten.append(f"{_quoted_assignment(key, updates[key])}{_line_ending(line)}")
            else:
                rewritten.append(line)

        payload = "".join(rewritten)
        appended = [key for key in updates if key not in found_updates and canonical_env_name(key) not in removal_set]
        if appended:
            if payload and not payload.endswith(("\r", "\n")):
                payload += "\n"
            payload += "".join(f"{_quoted_assignment(key, updates[key])}\n" for key in appended)

        if not existed or payload != original:
            atomic_write_text(path, payload)
        return True
