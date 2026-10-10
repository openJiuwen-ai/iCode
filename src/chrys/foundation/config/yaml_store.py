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
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

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
def _read_guard(path: Path, timeout: float) -> Iterator[None]:
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


def _parse(raw: bytes | None) -> dict[str, Any] | None:
    """Decode a YAML mapping, returning ``None`` if undecodable or invalid.

    The shared detector's strict decode raises ``UnicodeError`` (a
    ``ValueError``) instead of replacing damaged bytes. PyYAML only wraps
    *parser* failures in ``YAMLError``: its
    constructors let the underlying exception out, so ``2026-99-99`` raises a
    bare ``ValueError``, an integer past ``sys.get_int_max_str_digits()`` the
    same, and deeply nested flow collections a ``RecursionError``.

    Every one of those is reachable by hand-editing the file — which is exactly
    the case the ``.bak`` fallback exists for, and it only runs if this returns
    ``None`` instead of propagating.
    """
    if raw is None:
        return None
    try:
        text = decode_bytes(raw, errors="strict")
        data = yaml.safe_load(text)
    except yaml.YAMLError, ValueError, RecursionError:
        return None
    if data is None:
        # An empty file, a file of only comments, and an explicit ``null`` all
        # land here, and all three mean "this document holds no settings".
        # Only ``None`` may: ``or {}`` would have swallowed ``false``, ``0``
        # and ``[]`` too, and a truncated file that happens to parse as one of
        # those would then count as a valid empty document — skipping the
        # ``.bak`` fallback and letting the next write mirror the emptiness
        # over the one good copy that was left.
        return {}
    if not isinstance(data, dict) or not _container_tree_within_bounds(data):
        return None
    return data


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


def _read_locked(path: Path, *, backup: bool) -> tuple[dict[str, Any] | None, bytes | None, bool]:
    """Read *path* (caller holds the lock): ``(doc, raw, from_backup)``.

    ``raw`` is the primary's bytes as this read saw them — including when they
    did not parse — so a caller that has to prove what it read digests those
    rather than re-reading the path afterwards.
    """
    raw = _read_bytes(path)
    doc = _parse(raw)
    if doc is not None:
        return doc, raw, False
    if not backup:
        return None, raw, False
    return _parse(_read_bytes(backup_path_for(path))), raw, True


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
    """Digest of the primary as this read left it; ``None`` when absent."""


def read_yaml_document(
    path: Path,
    *,
    backup: bool = True,
    lock_timeout: float = LOCK_TIMEOUT_SECONDS,
) -> YamlRead:
    """Read the YAML mapping at *path* together with the file's fingerprint.

    For callers that later act destructively on the file they read — retiring
    a migrated legacy document, say — and must be able to prove it is still
    the same file. The fingerprint is over the very bytes ``doc`` was parsed
    from, not over a second read of the path: an editor that ignores the
    sidecar lock needs only one write to land between a parse and a re-read,
    and the fingerprint would then certify a file whose contents nobody has
    seen — the next phase retires the newer file and records the older state as
    migrated. Re-entering this function under a caller-held lock self-deadlocks,
    which is why the fingerprint has to come out of the same call.

    A repaired primary is the one case where the two differ legitimately: this
    call rewrites it from the backup, and the fingerprint then describes the
    payload it just wrote, which is what the next reader will find there.
    """
    if not path.exists() and not (backup and backup_path_for(path).exists()):
        return YamlRead(doc=None, fingerprint=None)
    # No ``mkdir`` here: one of those two exists, so its directory does too, and
    # a read must not be what creates the config directory.
    with _read_guard(path, lock_timeout):
        doc, raw, from_backup = _read_locked(path, backup=backup)
        if doc is not None and from_backup:
            try:
                raw = atomic_write_text(path, _dump(doc))
            except OSError:
                # The repair failed, so the primary still holds what was read
                # from it — unparseable, but that is its identity, and the
                # caller's own guard decides what an unreadable one means.
                logger.warning("Could not restore %s from its backup", path, exc_info=True)
        return YamlRead(doc=doc, fingerprint=digest_bytes(raw))


def read_yaml_doc(
    path: Path,
    *,
    backup: bool = True,
    lock_timeout: float = LOCK_TIMEOUT_SECONDS,
) -> dict[str, Any] | None:
    """Read the YAML mapping at *path*, falling back to its backup.

    Returns ``None`` when neither copy yields a mapping, so callers can tell
    "no config yet" from "empty config". When the primary is missing or corrupt
    and the backup parses, the primary is restored from it before returning.

    Nothing is created when neither file exists — a read must not materialise
    the config directory.
    """
    return read_yaml_document(path, backup=backup, lock_timeout=lock_timeout).doc


def read_yaml_doc_readonly(path: Path) -> dict[str, Any] | None:
    """Read the YAML mapping at *path* with no lock, no backup and no repair.

    For documents Chrys does not own — a repository's ``.chrys/settings.yaml``
    lives in someone's working tree, where the other readers' side effects are
    exactly wrong: acquiring the courtesy lock creates a sidecar file in their
    repo, and the backup repair *writes* into it. There is also no Chrys writer
    to be courteous to, because nothing in Chrys ever writes these files.

    Because the file is also not the *user's*, the read is defensive where the
    owned readers are trusting: only a regular file, never through a symlink,
    and never past :data:`UNTRUSTED_DOC_MAX_BYTES` — opening a cloned
    repository must not hang on a FIFO or feed ``/dev/zero`` to the parser.
    The distrust covers both hops a repository controls: the file itself and
    the directory that holds it — a committed ``.chrys`` symlink must not
    carry the read out of the tree. Ancestors above the parent are the
    caller's trust domain (the project root the user chose to open).

    Returns ``None`` when the file is absent, refused, or does not parse as a
    mapping.
    """
    return _parse(_read_bytes_untrusted(path))


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
) -> dict[str, Any]:
    """Apply *mutator* to the document at *path* under an exclusive lock.

    The whole read-modify-write happens inside one lock acquisition, so
    concurrent writers merge instead of clobbering: same key is
    last-writer-wins, different keys both survive.

    *mutator* receives the freshly read document (an empty dict when neither
    the primary nor the backup parses) and returns the document to commit. If
    it raises, the lock is released and **neither** file is touched.

    Returns the committed document.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    with FileLock(lock_path_for(path), timeout=lock_timeout):
        current, _, _ = _read_locked(path, backup=True)
        updated = mutator(dict(current) if current is not None else {})
        payload = _dump(updated)
        _commit(path, payload)
        return updated
