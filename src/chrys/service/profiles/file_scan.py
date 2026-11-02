# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Read a profile directory without changing its files or a live registry."""

from __future__ import annotations

import errno
import os
import stat
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from yaml.constructor import ConstructorError

from chrys.foundation.platform.files import digest_bytes
from chrys.foundation.text.encoding import decode_bytes


class _ProfileLoader(yaml.SafeLoader):
    """Keep the source position when a safe scalar constructor rejects its value."""

    def construct_object(self, node: yaml.Node, deep: bool = False) -> Any:
        try:
            return super().construct_object(node, deep)
        except (AttributeError, LookupError, TypeError, ValueError, OverflowError) as exc:
            raise ConstructorError(problem="Invalid YAML value", problem_mark=node.start_mark) from exc


def read_profile_mapping(text: str) -> dict[str, Any]:
    loader = _ProfileLoader(text)
    try:
        node = loader.get_single_node()
        data = loader.construct_document(node) if node is not None else None
        if not isinstance(data, dict):
            mark = node.start_mark if node is not None else loader.get_mark()
            raise ConstructorError(problem="Profile YAML must be a mapping", problem_mark=mark)
        return data
    finally:
        loader.dispose()


class ProfileLoadError(Exception):
    """Caller-facing diagnostics may contain raw fields. Only log a safe reason."""

    def __init__(self, message: str, *, field: str | None = None, log_reason: str | None = None) -> None:
        self.field = field
        self.log_reason = log_reason
        super().__init__(message)


@dataclass(frozen=True)
class ProfileFileIssue:
    # The toast uses language-neutral YAML coordinates, a schema field, or an OS error code.
    location: str
    reason: str


@dataclass
class ProfileDirScan[P]:
    loaded: list[tuple[Path, P]] = field(default_factory=list)
    invalid: dict[Path, ProfileFileIssue] = field(default_factory=dict)
    fingerprints: dict[Path, str | None] = field(default_factory=dict)
    directory_error: OSError | None = None


def read_profile_bytes(path: Path) -> bytes:
    # A FIFO replacing a profile must never block the TUI's reload worker.
    def open_profile(name: str, flags: int) -> int:
        if sys.platform != "win32":
            flags |= os.O_NONBLOCK
        return os.open(name, flags)

    with open(path, "rb", opener=open_profile) as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise OSError("Not a regular profile file")
        return stream.read()


def profile_fingerprint(path: Path) -> str | None:
    try:
        return digest_bytes(read_profile_bytes(path))
    except OSError:
        return None


def scan_profiles[P](
    directory: Path,
    parse: Callable[[Path, str], P],
    load_error: type[ProfileLoadError],
    *,
    skip_hidden: bool,
) -> ProfileDirScan[P]:
    """Keep the fingerprint of the bytes parsed, including malformed documents.

    A genuinely absent directory is empty. A listing failure cannot establish
    deletion, including a failure after the filesystem returned some entries.
    """
    scan = ProfileDirScan[P]()
    try:
        paths = list(directory.iterdir())
    except FileNotFoundError as exc:
        try:
            directory.lstat()
        except FileNotFoundError:
            return scan
        except OSError as stat_error:
            scan.directory_error = stat_error
            return scan
        scan.directory_error = exc
        return scan
    except OSError as exc:
        scan.directory_error = exc
        return scan

    for pattern in ("*.yaml", "*.yml"):
        for path in sorted(p for p in paths if p.match(pattern) and not (skip_hidden and p.name.startswith("."))):
            scan.fingerprints[path] = None
            try:
                raw = read_profile_bytes(path)
                scan.fingerprints[path] = digest_bytes(raw)
                profile = parse(path, decode_bytes(raw, errors="strict"))
            except (OSError, UnicodeError, RecursionError, OverflowError, load_error) as exc:
                # A YAML exception may echo credentials from the offending line.
                cause = exc.__cause__
                if isinstance(cause, yaml.MarkedYAMLError) and cause.problem_mark is not None:
                    mark = cause.problem_mark
                    issue = ProfileFileIssue(
                        f"YAML {mark.line + 1}:{mark.column + 1}",
                        f"Invalid YAML at line {mark.line + 1}, column {mark.column + 1}",
                    )
                elif isinstance(exc, ProfileLoadError) and exc.field:
                    issue = ProfileFileIssue(exc.field, exc.log_reason or f"Invalid or missing field {exc.field}")
                elif isinstance(exc, OSError):
                    code = errno.errorcode.get(exc.errno, type(exc).__name__)
                    issue = ProfileFileIssue(code, exc.strerror or code)
                elif isinstance(exc, UnicodeError):
                    issue = ProfileFileIssue("encoding", "Invalid document encoding")
                else:
                    issue = ProfileFileIssue("YAML", "Invalid YAML document")
                scan.invalid[path] = issue
            else:
                scan.loaded.append((path, profile))
    return scan
