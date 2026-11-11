# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Locked read-modify-write primitives for chrys' YAML config documents.

Every persisted YAML config under ``~/.chrys/`` goes through
:func:`update_yaml_doc`. There is deliberately **no** bare ``write_yaml_doc``:
a lock-free read followed by a locked whole-file write is only safe when a
single object owns the whole document. Once several panels (or processes)
patch different keys of the same file, the last writer would silently clobber
keys it never read.

Layering the other way round — take the lock in the caller, then call a
function that takes it again — self-deadlocks: ``FileLock`` first grabs a
**non-reentrant** ``threading.Lock`` keyed by lock path
(:func:`chrys.foundation.util.lock._get_path_lock`). Hence one public entry
point that owns the whole critical section, and unlocked helpers stay private.

Reads take the lock too. On Windows an open read handle blocks ``os.replace``,
so an unlocked reader would make a concurrent writer fail rather than merely
observe a stale document.
"""

from __future__ import annotations

import contextlib
import logging
import os
import stat
from collections.abc import Callable, Generator, Iterator
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from chrys.foundation.config.errors import ConfigError
from chrys.foundation.i18n import DisplayPath, MessageRef, msg
from chrys.foundation.platform.files import atomic_write_text, digest_bytes
from chrys.foundation.text.encoding import decode_bytes
from chrys.foundation.util.lock import FileLock

LOCK_TIMEOUT_SECONDS = 10.0

UNTRUSTED_DOC_MAX_BYTES = 1024 * 1024
"""Upper bound for a config document Chrys reads but does not own.

A repository's ``.chrys/settings.yaml`` is attacker-supplied the moment a
repo is cloned, and the plain readers here trust the filesystem: they follow
symlinks and read to end-of-file. A settings document is a handful of short
keys, so anything near this bound is not a settings document.
"""

# ``O_NOFOLLOW`` refuses a symlinked final component at ``open`` time and
# ``O_NONBLOCK`` keeps a FIFO from blocking the open; neither exists on
# Windows, where the ``lstat`` screen below is the only line of defense —
# creating symlinks there requires elevated rights, so the residual
# check-to-open race is accepted.
_UNTRUSTED_OPEN_FLAGS = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)

# ``openat``-style resolution (``dir_fd``) is what makes the parent check and
# the file open one atomic act; POSIX has it, Windows does not.
_OPENAT_AVAILABLE = os.open in os.supports_dir_fd and hasattr(os, "O_DIRECTORY")

logger = logging.getLogger(__name__)


def backup_path_for(path: Path) -> Path:
    """Return the sibling ``.bak`` path for *path*."""
    return path.with_name(path.name + ".bak")


def lock_path_for(path: Path) -> Path:
    """Return the sibling lock path for *path*.

    Derived from the target name (``settings.yaml`` -> ``settings.lock``) and
    never hardcoded: a fixed name would let a process that guards one document
    exclude writers of an unrelated one — and, worse, let two processes guard
    the *same* document with different lock files.
    """
    return path.with_suffix(".lock")


@contextlib.contextmanager
def _read_guard(path: Path, timeout: float) -> Generator[None]:
    """Hold the document's lock for a read, or read without it.

    Reads take the lock as a courtesy to writers, not because the reader needs
    it: on Windows an open handle blocks a concurrent ``os.replace``. Acquiring
    it creates a sidecar, which needs a writable directory — and a config
    directory can be read-only, on a locked-down machine or under an owner the
    user is not. Failing the read there would abort every entry point over a
    document we can read perfectly well, so the courtesy is dropped instead.

    A lock that exists but is *held* is the opposite case and still propagates.
    ``TimeoutError`` is an ``OSError``, so the two would otherwise collapse into
    one: "nobody can take this lock" would be answered by reading behind an
    active writer, which is the single thing the lock is there to prevent.
    """
    lock = FileLock(lock_path_for(path), timeout=timeout)
    try:
        lock.acquire()
    except TimeoutError:
        raise
    except OSError:
        logger.warning("Config lock unavailable; reading %s without it", path, exc_info=True)
        yield
        return
    try:
        yield
    finally:
        lock.release()


def _read_bytes(path: Path) -> bytes | None:
    """The file's bytes, or ``None`` when it is absent or unreadable.

    Bytes rather than text because the digest is taken over exactly what was
    parsed, and a text-mode read has already translated line endings by the
    time it hands anything back — two readers of one file would then disagree
    about its identity.
    """
    try:
        return path.read_bytes()
    except OSError:
        return None


_INVALID_MAPPING = msg(
    "settings.document_invalid_mapping",
    fallback="{path}: expected a YAML mapping within the configuration size limits.",
)
_INVALID_YAML = msg("settings.document_invalid_yaml", fallback="{path}: invalid YAML or text encoding.")
_INVALID_YAML_AT = msg(
    "settings.document_invalid_yaml_at", fallback="{path}: invalid YAML at line {line}, column {column}."
)
_UNREADABLE = msg("settings.document_unreadable", fallback="Cannot read {path}: {detail}")
_LOCK_TIMEOUT = msg("settings.document_lock_timeout", fallback="Timed out waiting for the lock on {path}.")
_INVALID_PROJECT = msg(
    "settings.document_invalid_project", fallback="{path}: unreadable or invalid project YAML document."
)


class ConfigDocumentError(ConfigError, OSError):
    """The primary cannot safely participate in a settings operation."""


def _parse(raw: bytes | None, *, required_at: Path | None = None) -> dict[str, Any] | None:
    """Parse without including document excerpts in errors: they can contain secrets."""
    if raw is None:
        return None

    try:
        data = yaml.safe_load(decode_bytes(raw, errors="strict"))
        if data is None:
            return {}
        if isinstance(data, dict) and _container_tree_within_bounds(data):
            return data
    except yaml.MarkedYAMLError as exc:
        if required_at is not None:
            mark = exc.problem_mark
            path = DisplayPath(required_at)
            detail = (
                _INVALID_YAML_AT.bind(path=path, line=mark.line + 1, column=mark.column + 1)
                if mark
                else _INVALID_YAML.bind(path=path)
            )
            raise ConfigDocumentError(detail) from None
    except yaml.YAMLError, ValueError, RecursionError:
        if required_at is not None:
            raise ConfigDocumentError(_INVALID_YAML.bind(path=DisplayPath(required_at))) from None
    if required_at is not None:
        raise ConfigDocumentError(_INVALID_MAPPING.bind(path=DisplayPath(required_at)))
    return None


_MAX_EXPANDED_CONTAINERS = 100_000
"""Ceiling on the container tree a parsed document may expand to.

The byte caps bound the *text*, not the graph: anchors and aliases let a
few hundred bytes parse into a self-referential structure or one whose
shared branches multiply into millions of tree nodes. Consumers walk the
document as a tree — the flattener recurses into mapping values, and an
invalid raw value is rendered with ``str()``, which walks *every* container
it holds — so the walkable expansion is what has to be bounded, over every
container kind ``safe_load`` can produce, and a document past either bound
is refused whole, like any other document that does not parse.
"""

_MAX_EXPANDED_CONTAINER_DEPTH = 100
"""Ceiling on the expanded nesting depth of a parsed document.

Deep *literal* nesting already dies inside the parser (the ``RecursionError``
catch above), but an alias chain expands deeper than the text ever nested,
and a tree walk's recursion would be the thing that hits the limit.
Settings keys are a handful of segments; one hundred is beyond any document
with an owner.
"""

_MAX_EXPANDED_SCALAR_CHARS = 10_000_000
"""Ceiling on the scalar characters an expanded document may render to.

Bounding the container count bounds the *walk*, not the *output*: one large
string anchored once and aliased through a shared DAG is a single scalar in
the graph, yet every expanded path renders its own copy — a few hundred
kilobytes of text asking ``str()`` for gigabytes while staying under the
container ceiling. Scalars are therefore priced per expanded occurrence, by
size, estimated without rendering them. Ten million characters is far past
any settings document and still harmless to actually render.
"""

_CONTAINER_TYPES = (dict, list, tuple, set, frozenset)
"""Every container ``yaml.safe_load`` can construct.

Tuples come from ``!!omap``/``!!pairs``, sets from ``!!set``; mapping *keys*
can be containers too. All of them are walked by ``str()`` when a value is
rendered for a warning, so all of them count toward the expansion bounds.
"""


def _container_children(node: Any) -> Iterator[Any]:
    if isinstance(node, dict):
        for key, value in node.items():
            yield key
            yield value
    else:
        yield from node


def _scalar_render_cost(value: Any) -> int:
    """Chars *value* contributes to a rendered warning, without rendering it.

    An estimate within a small constant factor is enough: the budget's job is
    to make the render proportional to a fixed number, not to predict it.
    """
    if isinstance(value, (str, bytes)):
        return len(value) + 2
    if isinstance(value, int):  # bools included; ~3 bits per decimal digit
        return value.bit_length() // 3 + 2
    return 32


def _container_tree_within_bounds(doc: dict[str, Any]) -> bool:
    """Whether *doc* expands to a finite container tree inside all bounds.

    Walks each distinct container once (iteratively — the walk must not be
    the recursion that overflows) and measures the tree it would expand to:
    container count, nesting depth, and rendered scalar volume.
    """
    # Post-order over distinct containers: measurements by identity, an
    # ancestor set for cycle detection. A ``(node, True)`` entry pops only
    # after the node's whole subtree was measured, so a ``(node, False)``
    # entry popping while the node is still an ancestor is a true cycle.
    measured: dict[int, tuple[int, int, int]] = {}
    ancestors: set[int] = set()
    stack: list[tuple[Any, bool]] = [(doc, False)]
    while stack:
        node, children_measured = stack.pop()
        node_id = id(node)
        if children_measured:
            ancestors.discard(node_id)
            size = 1
            depth = 1
            chars = 0
            for child in _container_children(node):
                if isinstance(child, _CONTAINER_TYPES):
                    child_size, child_depth, child_chars = measured[id(child)]
                    size += child_size
                    depth = max(depth, child_depth + 1)
                    chars += child_chars
                else:
                    chars += _scalar_render_cost(child)
            if (
                size > _MAX_EXPANDED_CONTAINERS
                or depth > _MAX_EXPANDED_CONTAINER_DEPTH
                or chars > _MAX_EXPANDED_SCALAR_CHARS
            ):
                return False
            measured[node_id] = (size, depth, chars)
            continue
        if node_id in measured:
            continue
        if node_id in ancestors:
            return False
        ancestors.add(node_id)
        stack.append((node, True))
        stack.extend(
            (child, False)
            for child in _container_children(node)
            if isinstance(child, _CONTAINER_TYPES) and id(child) not in measured
        )
    return True


# Owned config documents are few. Remember each independently of sessions so a
# workspace switch can combine the last valid user layer with the new project.
_last_valid: dict[Path, dict[str, Any]] = {}


def _primary(path: Path) -> tuple[dict[str, Any] | None, bytes | None]:
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        _last_valid.pop(path, None)
        return None, None
    except OSError as exc:
        raise ConfigDocumentError(_UNREADABLE.bind(path=DisplayPath(path), detail=exc.strerror or str(exc))) from exc
    doc = _parse(raw, required_at=path)
    if doc is None:
        raise ConfigDocumentError(_INVALID_YAML.bind(path=DisplayPath(path)))
    _last_valid[path] = deepcopy(doc)
    return doc, raw


def _dump(doc: dict[str, Any]) -> str:
    return yaml.safe_dump(doc, sort_keys=True, allow_unicode=True)


def _commit(path: Path, payload: str) -> None:
    """Write *payload* to the primary, then mirror it to the backup.

    Primary first, on purpose: writing the backup first would leave a recovery
    copy of a state that was never committed if the primary write then fails.
    The backup is best-effort — a full disk must not fail an otherwise good
    primary write.
    """
    atomic_write_text(path, payload)
    with contextlib.suppress(OSError):
        atomic_write_text(backup_path_for(path), payload)


@dataclass(frozen=True, slots=True)
class YamlRead:
    """One read of a document, and the identity of what was read."""

    doc: dict[str, Any] | None
    fingerprint: str | None
    """Digest of a successfully parsed primary. Recovery data cannot authorize migration."""
    error: MessageRef | None = None


def read_yaml_document(
    path: Path, *, backup: bool = True, strict: bool = False, lock_timeout: float = LOCK_TIMEOUT_SECONDS
) -> YamlRead:
    """Read without repairing. Ordinary readers recover in memory, strict readers reject."""
    try:
        # A missing directory must not be created just to lock a missing file.
        path.stat()
        with _read_guard(path, lock_timeout):
            doc, raw = _primary(path)
            return YamlRead(doc, digest_bytes(raw))
    except FileNotFoundError:
        _last_valid.pop(path, None)
        return YamlRead(None, None)
    except OSError as exc:
        if isinstance(exc, ConfigDocumentError):
            fault = exc
        elif isinstance(exc, TimeoutError):
            fault = ConfigDocumentError(_LOCK_TIMEOUT.bind(path=DisplayPath(path)))
        else:
            fault = ConfigDocumentError(_UNREADABLE.bind(path=DisplayPath(path), detail=str(exc)))
        if strict:
            if fault is exc:
                raise
            raise fault from exc
        error = str(fault)
        logger.warning("%s", error)
        cached = _last_valid.get(path) if backup else None
        doc = (
            deepcopy(cached) if cached is not None else (_parse(_read_bytes(backup_path_for(path))) if backup else None)
        )
        return YamlRead(doc, None, fault.display)


def read_yaml_doc(
    path: Path,
    *,
    backup: bool = True,
    lock_timeout: float = LOCK_TIMEOUT_SECONDS,
) -> dict[str, Any] | None:
    """Read a document, optionally using remembered content or a backup on failure."""
    return read_yaml_document(path, backup=backup, lock_timeout=lock_timeout).doc


def read_yaml_doc_readonly(path: Path, *, strict: bool = False) -> dict[str, Any] | None:
    """Read the YAML mapping at *path* with no lock, no backup and no repair.

    For documents Chrys does not own — a repository's ``.chrys/settings.yaml``
    lives in someone's working tree, where taking the owned-document lock would create a sidecar.
    Project configuration has no backup or remembered user layer.

    Because the file is also not the *user's*, the read is defensive where the
    owned readers are trusting: only a regular file, never through a symlink,
    and never past :data:`UNTRUSTED_DOC_MAX_BYTES` — opening a cloned
    repository must not hang on a FIFO or feed ``/dev/zero`` to the parser.
    The distrust covers both hops a repository controls: the file itself and
    the directory that holds it — a committed ``.chrys`` symlink must not
    carry the read out of the tree. Ancestors above the parent are the
    caller's trust domain (the project root the user chose to open).

    Missing files return ``None``. For refused or malformed files strict
    reads raise and ordinary reads return ``None``.
    """
    raw = _read_bytes_untrusted(path)
    result = _parse(raw, required_at=path if strict else None)
    if strict and result is None:
        try:
            path.lstat()
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise ConfigDocumentError(
                _UNREADABLE.bind(path=DisplayPath(path), detail=exc.strerror or str(exc))
            ) from exc
        raise ConfigDocumentError(_INVALID_PROJECT.bind(path=DisplayPath(path)))
    return result


def _read_bytes_untrusted(path: Path) -> bytes | None:
    """Bounded, regular-file-only, no-follow read; ``None`` on any refusal."""
    # The parent must itself be a real directory: ``O_NOFOLLOW`` refuses only
    # the final component, and a symlinked (or, on Windows, a reparse-pointed)
    # containing directory would resolve the rest of the path outside the tree
    # before that refusal could bite.
    parent_fd: int | None = None
    try:
        if _OPENAT_AVAILABLE:
            # Bind the final open to the directory that was checked:
            # ``O_DIRECTORY | O_NOFOLLOW`` refuses a symlinked parent in the
            # same call that opens it, and ``dir_fd`` resolves the file inside
            # that very directory — a rename slipped between two path lookups
            # cannot swap the tree out underneath the read.
            parent_fd = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0))
            fd = os.open(path.name, _UNTRUSTED_OPEN_FLAGS, dir_fd=parent_fd)
        else:
            # Windows has no ``openat``, so the screen is two lookups with a
            # window between them — best effort, backed by the ``fstat``
            # re-check on the descriptor below.
            parent = os.lstat(path.parent)
            if not stat.S_ISDIR(parent.st_mode):
                return None
            if getattr(parent, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT:
                return None
            if not stat.S_ISREG(os.lstat(path).st_mode):
                return None
            fd = os.open(path, _UNTRUSTED_OPEN_FLAGS)
    except OSError:
        return None
    finally:
        if parent_fd is not None:
            os.close(parent_fd)
    try:
        described = os.fstat(fd)
        # Re-checked on the open descriptor: the pre-``lstat`` only screens
        # what the path named a moment ago, and it is what let us get here
        # without blocking on a FIFO.
        if not stat.S_ISREG(described.st_mode) or described.st_size > UNTRUSTED_DOC_MAX_BYTES:
            return None
        # Read past the stat by one byte so a file growing under us is caught
        # by size, not trusted because its stat was taken early.
        chunks: list[bytes] = []
        remaining = UNTRUSTED_DOC_MAX_BYTES + 1
        while remaining > 0:
            chunk = os.read(fd, min(remaining, 65536))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        if remaining <= 0:
            return None
        return b"".join(chunks)
    except OSError:
        return None
    finally:
        os.close(fd)


def update_yaml_doc(
    path: Path,
    mutator: Callable[[dict[str, Any]], dict[str, Any]],
    *,
    lock_timeout: float = LOCK_TIMEOUT_SECONDS,
    committed: Callable[[], None] | None = None,
) -> dict[str, Any]:
    """Apply *mutator* to the document at *path* under an exclusive lock.

    The whole read-modify-write happens inside one lock acquisition, so
    concurrent writers merge instead of clobbering: same key is
    last-writer-wins, different keys both survive.

    *mutator* receives the freshly read primary (an empty dict when absent) and returns the document to commit. If
    it raises, the lock is released and **neither** file is touched.

    Returns the committed document.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    with FileLock(lock_path_for(path), timeout=lock_timeout):
        current, _ = _primary(path)
        updated = mutator(dict(current) if current is not None else {})
        payload = _dump(updated)
        _commit(path, payload)
        _last_valid[path] = deepcopy(updated)
        if committed is not None:
            committed()
        return updated
