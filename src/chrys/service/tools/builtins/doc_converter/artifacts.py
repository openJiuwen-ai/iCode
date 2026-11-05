# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Bounded session artifacts for images embedded in converted documents."""

from __future__ import annotations

import hashlib
import logging
import os
import re
import stat
import threading
from dataclasses import dataclass
from pathlib import Path

from chrys.foundation.platform import get_platform
from chrys.foundation.platform.files import (
    atomic_write_owner_only_bytes,
    secure_open_owner_only_binary,
    secure_unlink_owner_verified,
)
from chrys.foundation.text.images import (
    MAX_IMAGE_SOURCE_BYTES,
    ImageProcessingError,
    compress_image_data,
    detect_image_media_type,
    inspect_image_dimensions,
)
from chrys.service.tools.builtins.doc_converter.parsers.base import DOCUMENT_IMAGE_DECODE_FORMATS, VisualOccurrence
from chrys.service.tools.session_artifacts import (
    DOCUMENT_IMAGE_ARTIFACT_MAX_FILE_BYTES,
    DOCUMENT_IMAGE_ARTIFACT_MAX_FILES,
    DOCUMENT_IMAGE_ARTIFACT_MAX_TOTAL_BYTES,
    DocumentImageArtifactLimitError,
    iter_document_image_artifacts,
    make_document_image_artifact_handle,
)

logger = logging.getLogger(__name__)

MAX_IMAGE_OCCURRENCES = 128
MAX_UNIQUE_IMAGES = 128
MAX_STORED_IMAGE_BYTES = DOCUMENT_IMAGE_ARTIFACT_MAX_FILE_BYTES
MAX_TOTAL_IMAGE_BYTES = 512 * 1024 * 1024
MAX_SESSION_IMAGE_FILES = DOCUMENT_IMAGE_ARTIFACT_MAX_FILES
MAX_SESSION_IMAGE_BYTES = DOCUMENT_IMAGE_ARTIFACT_MAX_TOTAL_BYTES
MAX_SOURCE_STEM_BYTES = 48
MAX_ARTIFACT_BASENAME_BYTES = 128

_INVALID_FILENAME_RE = re.compile(r'[<>:"/\\|?*\x00-\x1f\ud800-\udfff]')
_PATH_LOCKS: dict[str, threading.Lock] = {}
_PATH_LOCKS_GUARD = threading.Lock()


@dataclass(frozen=True, slots=True)
class _SavedImage:
    """One normalized image artifact reused by repeated occurrences."""

    reference: str
    source_name: str


@dataclass(slots=True)
class _PendingImage:
    """In-process claims on one not-yet-committed content artifact."""

    claims: int = 1
    committed: bool = False


class _SessionImageQuotaExceeded(Exception):
    """Raised when publishing a new digest would exceed the session cap."""


_PENDING_IMAGES: dict[str, _PendingImage] = {}


def bounded_artifact_stem(name: str) -> str:
    """Return a strict-UTF-8 artifact stem no longer than 48 bytes."""
    sanitized = _INVALID_FILENAME_RE.sub("_", name).strip(" .") or "document"
    bounded = _truncate_utf8(sanitized, MAX_SOURCE_STEM_BYTES).strip(" .")
    return bounded or "document"


def artifact_basename_within_limit(filename: str) -> bool:
    """Return whether a generated artifact basename obeys the byte contract."""
    return len(filename.encode("utf-8")) <= MAX_ARTIFACT_BASENAME_BYTES


def canonical_document_artifact_root(artifact_root: Path) -> Path:
    """Canonicalize the trusted session parent without following its artifact child."""
    canonical_session_dir = Path(os.path.realpath(os.path.abspath(os.fspath(artifact_root.parent))))
    return canonical_session_dir / artifact_root.name


def prepare_document_artifact_root(artifact_root: Path) -> Path:
    """Create and verify a link-free document artifact root."""
    canonical_root = canonical_document_artifact_root(artifact_root)
    canonical_root.mkdir(mode=0o700, parents=True, exist_ok=True)
    absolute = os.path.abspath(os.fspath(canonical_root))
    if os.path.normcase(os.path.realpath(absolute)) != os.path.normcase(absolute):
        raise OSError("session document directory must not be redirected")
    info = canonical_root.lstat()
    if not stat.S_ISDIR(info.st_mode):
        raise OSError("session document artifact root is not a directory")
    if not get_platform().is_windows:
        os.chmod(canonical_root, 0o700)
    return canonical_root


def _truncate_utf8(text: str, max_bytes: int) -> str:
    encoded = text.encode("utf-8")
    if len(encoded) <= max_bytes:
        return text
    return encoded[:max_bytes].decode("utf-8", errors="ignore")


def _path_lock(path: Path) -> threading.Lock:
    key = os.path.normcase(os.path.abspath(os.fspath(path)))
    with _PATH_LOCKS_GUARD:
        lock = _PATH_LOCKS.get(key)
        if lock is None:
            lock = threading.Lock()
            _PATH_LOCKS[key] = lock
        return lock


class DocumentImageSink:
    """Normalize and persist one conversion's embedded images under its session."""

    def __init__(self, artifact_root: Path, *, source_stem: str) -> None:
        self._artifact_root = canonical_document_artifact_root(artifact_root)
        # Preserve the constructor contract while making image identity
        # session-wide and content-based. Markdown naming still uses this
        # shared stem normalizer.
        self._source_stem = bounded_artifact_stem(source_stem)
        self._occurrence_count = 0
        self._unprocessed_count = 0
        self._candidate_failure_count = 0
        self._artifact_failure_count = 0
        self._linked_image_count = 0
        self._storage_limit_count = 0
        self._artifact_root_failure_count = 0
        self._saved_by_digest: dict[str, _SavedImage] = {}
        self._reference_paths: dict[str, Path] = {}
        self._claimed_paths: set[Path] = set()
        self._total_stored_bytes = 0
        self._root_ready = False
        self._root_failed = False

    def try_reserve_occurrence(self) -> bool:
        """Reserve one explicit candidate retrieval within the per-document limit."""
        if self._occurrence_count >= MAX_IMAGE_OCCURRENCES:
            return False
        self._occurrence_count += 1
        return True

    def record_unprocessed_occurrences(self, count: int) -> None:
        """Record candidates not retrieved after the occurrence cap."""
        self._unprocessed_count += max(count, 0)

    def record_candidate_failure(self) -> None:
        """Record an embedded image that could not be read or normalized."""
        self._candidate_failure_count += 1

    def record_linked_image(self) -> None:
        """Record a linked-only image that Chrys deliberately did not download."""
        self._linked_image_count += 1

    @property
    def warnings(self) -> tuple[str, ...]:
        """Return deterministic, bounded warning summaries."""
        warnings: list[str] = []
        if self._unprocessed_count:
            warnings.append(
                f"Skipped {self._unprocessed_count} image candidate(s) after the "
                f"{MAX_IMAGE_OCCURRENCES}-candidate retrieval limit."
            )
        if self._linked_image_count:
            warnings.append(
                f"Skipped {self._linked_image_count} linked image(s) because no embedded image data was available."
            )
        if self._candidate_failure_count:
            warnings.append(
                f"Skipped {self._candidate_failure_count} image candidate(s) that could not be decoded or normalized."
            )
        if self._artifact_failure_count:
            warnings.append(
                f"Skipped {self._artifact_failure_count} image candidate(s) that could not be persisted or referenced "
                "as session artifacts."
            )
        if self._storage_limit_count:
            warnings.append(
                f"Skipped {self._storage_limit_count} image candidate(s) because document or session image storage "
                "limits were reached."
            )
        if self._artifact_root_failure_count:
            warnings.append(
                "Image extraction could not use the session artifact directory; document text was preserved."
            )
        return tuple(warnings)

    def save_image(
        self,
        data: bytes,
        *,
        location: str,
        ordinal: int,
        source_name: str,
    ) -> VisualOccurrence | None:
        """Normalize, deduplicate, and persist one embedded image."""
        try:
            normalized, suffix = self._normalize_image(data)
        except ImageProcessingError:
            self.record_candidate_failure()
            return None

        digest = hashlib.sha256(normalized).hexdigest()
        saved = self._saved_by_digest.get(digest)
        if saved is not None:
            return VisualOccurrence(
                location=location,
                ordinal=ordinal,
                reference=saved.reference,
                source_name=source_name or saved.source_name,
            )

        if len(self._saved_by_digest) >= MAX_UNIQUE_IMAGES:
            self._storage_limit_count += 1
            return None
        if self._total_stored_bytes + len(normalized) > MAX_TOTAL_IMAGE_BYTES:
            self._storage_limit_count += 1
            return None
        if not self._ensure_artifact_root():
            return None

        try:
            filename = self._image_filename(digest, suffix)
            path = self._artifact_root / filename
            if path not in self._claimed_paths and self._acquire_content_artifact(path, normalized):
                self._claimed_paths.add(path)
            reference = self._model_reference(path)
        except _SessionImageQuotaExceeded:
            self._storage_limit_count += 1
            return None
        except OSError, ValueError:
            logger.warning("Failed to persist a converted-document image for %s", self._source_stem, exc_info=True)
            self._artifact_failure_count += 1
            return None

        saved = _SavedImage(
            reference=reference,
            source_name=source_name,
        )
        self._saved_by_digest[digest] = saved
        self._reference_paths[reference] = path
        self._total_stored_bytes += len(normalized)
        return VisualOccurrence(
            location=location,
            ordinal=ordinal,
            reference=reference,
            source_name=source_name,
        )

    def commit_occurrences(self, occurrences: tuple[VisualOccurrence, ...]) -> None:
        """Commit returned content artifacts and discard true pending orphans."""
        committed = {
            path for occurrence in occurrences if (path := self._reference_paths.get(occurrence.reference)) is not None
        }
        self._release_claims(committed)

    def abort(self) -> None:
        """Release this conversion's claims and discard uncommitted artifacts."""
        self._release_claims(set())

    def _normalize_image(self, data: bytes) -> tuple[bytes, str]:
        if len(data) > MAX_IMAGE_SOURCE_BYTES:
            raise ImageProcessingError("embedded image exceeds the source-size limit")
        media_type = detect_image_media_type("embedded", data)
        if media_type is not None and len(data) <= MAX_STORED_IMAGE_BYTES:
            inspect_image_dimensions(data)
            suffix = {
                "image/png": ".png",
                "image/jpeg": ".jpg",
                "image/webp": ".webp",
            }[media_type]
            return data, suffix
        # Every pixel kept: viewing the stored copy fits it to what a model is sent.
        normalized = compress_image_data(
            data, max_bytes=MAX_STORED_IMAGE_BYTES, formats=DOCUMENT_IMAGE_DECODE_FORMATS, max_edge=None
        )
        if len(normalized) > MAX_STORED_IMAGE_BYTES:
            raise ImageProcessingError("normalized embedded image exceeds the storage limit")
        return normalized, ".jpg"

    def _ensure_artifact_root(self) -> bool:
        if self._root_ready:
            return True
        if self._root_failed:
            return False
        try:
            self._artifact_root = prepare_document_artifact_root(self._artifact_root)
        except OSError:
            logger.warning("Failed to prepare the converted-document artifact root", exc_info=True)
            self._artifact_root_failure_count += 1
            self._root_failed = True
            return False
        self._root_ready = True
        return True

    @staticmethod
    def _image_filename(digest: str, suffix: str) -> str:
        filename = f"image-{digest}{suffix}"
        if not artifact_basename_within_limit(filename):
            raise ValueError("generated document image filename exceeds the byte limit")
        return filename

    def _acquire_content_artifact(self, path: Path, payload: bytes) -> bool:
        """Reuse or publish an immutable digest artifact and return whether it is pending."""
        with _path_lock(self._artifact_root):
            key = os.path.normcase(os.path.abspath(os.fspath(path)))
            try:
                info = path.lstat()
            except FileNotFoundError:
                session_image_count, session_image_bytes = self._session_image_usage()
                if (
                    session_image_count >= MAX_SESSION_IMAGE_FILES
                    or session_image_bytes + len(payload) > MAX_SESSION_IMAGE_BYTES
                ):
                    raise _SessionImageQuotaExceeded from None
                atomic_write_owner_only_bytes(path, payload)
                _PENDING_IMAGES[key] = _PendingImage()
                return True

            if not stat.S_ISREG(info.st_mode):
                raise OSError("document image content path is not a regular file")
            with secure_open_owner_only_binary(path) as existing:
                existing_payload = existing.read(MAX_STORED_IMAGE_BYTES + 1)
            if existing_payload != payload:
                raise OSError("document image content digest does not match its stored artifact")

            pending = _PENDING_IMAGES.get(key)
            if pending is None:
                return False
            pending.claims += 1
            return True

    def _session_image_usage(self) -> tuple[int, int]:
        """Return stored image count and bytes while the artifact-root lock is held."""
        count = 0
        total = 0
        try:
            artifacts = iter_document_image_artifacts(
                self._artifact_root,
                max_files=MAX_SESSION_IMAGE_FILES,
                max_file_bytes=MAX_STORED_IMAGE_BYTES,
                max_total_bytes=MAX_SESSION_IMAGE_BYTES,
            )
            for _path, size in artifacts:
                count += 1
                total += size
        except DocumentImageArtifactLimitError:
            raise _SessionImageQuotaExceeded from None
        return count, total

    def _release_claims(self, committed: set[Path]) -> None:
        with _path_lock(self._artifact_root):
            for path in self._claimed_paths:
                key = os.path.normcase(os.path.abspath(os.fspath(path)))
                pending = _PENDING_IMAGES.get(key)
                if pending is None:
                    continue
                if path in committed:
                    pending.committed = True
                pending.claims -= 1
                if pending.claims:
                    continue
                _PENDING_IMAGES.pop(key, None)
                if not pending.committed:
                    self._discard_paths({path})
        self._claimed_paths.clear()

    @staticmethod
    def _model_reference(path: Path) -> str:
        # Keep this value strict-UTF-8 so output normalization is an identity;
        # commit_occurrences() relies on the normalized reference matching its
        # _reference_paths key exactly. Handles also remain fork-local after a
        # parent session is deleted, unlike absolute parent-session paths.
        return make_document_image_artifact_handle(path.name)

    @staticmethod
    def _discard_paths(paths: set[Path]) -> None:
        for path in paths:
            if path.exists() and not secure_unlink_owner_verified(path):
                logger.warning("Failed to discard unreferenced converted-document image %s", path)
