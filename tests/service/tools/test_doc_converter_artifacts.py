# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tests for doc_converter artifact writing: DocumentImageSink limits/concurrency and the markdown writer."""

from __future__ import annotations

import hashlib
import os
import struct
import threading
from io import BytesIO
from pathlib import Path
from typing import IO
from unittest.mock import patch

import pytest

from chrys.foundation.platform import get_platform
from chrys.foundation.text.images import COMPRESSED_IMAGE_MAX_EDGE, inspect_image_dimensions
from chrys.service.tools.builtins.doc_converter import (
    _write_unique_markdown,
)
from chrys.service.tools.builtins.doc_converter.artifacts import (
    MAX_ARTIFACT_BASENAME_BYTES,
    MAX_IMAGE_OCCURRENCES,
    DocumentImageSink,
)
from chrys.service.tools.builtins.doc_converter.parsers.base import VisualOccurrence
from chrys.service.tools.session_artifacts import (
    resolve_document_markdown_artifact_handle,
)
from tests.service.tools._doc_converter_fakes import (
    _png_bytes,
    _session_artifact_path,
)
from tests.support.images import image_bytes


def test_document_image_sink_deduplicates_and_commits_returned_occurrences(tmp_path: Path) -> None:
    sink = DocumentImageSink(tmp_path / "session" / "doc_converter", source_stem="report")
    image = _png_bytes()
    assert sink.try_reserve_occurrence()
    first = sink.save_image(image, location="Page 1", ordinal=1, source_name="first.png")
    assert sink.try_reserve_occurrence()
    second = sink.save_image(image, location="Page 2", ordinal=1, source_name="second.png")

    assert first is not None
    assert second is not None
    assert first.reference == second.reference
    files = list((tmp_path / "session" / "doc_converter").glob("*.png"))
    assert len(files) == 1

    sink.commit_occurrences((first, second))

    assert files[0].exists()


def test_document_image_sink_reuses_committed_content_and_cleans_new_abort(tmp_path: Path) -> None:
    root = tmp_path / "session" / "doc_converter"
    first_sink = DocumentImageSink(root, source_stem="report")
    assert first_sink.try_reserve_occurrence()
    existing = first_sink.save_image(_png_bytes(), location="Page 1", ordinal=1, source_name="existing.png")
    assert existing is not None
    first_sink.commit_occurrences((existing,))

    second_sink = DocumentImageSink(root, source_stem="report")
    assert second_sink.try_reserve_occurrence()
    second_copy = second_sink.save_image(_png_bytes(), location="Page 1", ordinal=1, source_name="copy.png")
    assert second_sink.try_reserve_occurrence()
    created = second_sink.save_image(_png_bytes((0, 0, 255)), location="Page 2", ordinal=1, source_name="new.png")
    assert second_copy is not None
    assert created is not None
    assert second_copy.reference == existing.reference

    second_sink.abort()

    assert _session_artifact_path(root.parent, existing.reference).exists()
    assert not _session_artifact_path(root.parent, created.reference).exists()


def test_aborting_creator_cannot_delete_image_committed_by_concurrent_conversion(tmp_path: Path) -> None:
    root = tmp_path / "session" / "doc_converter"
    creator = DocumentImageSink(root, source_stem="report")
    consumer = DocumentImageSink(root, source_stem="report")
    image = _png_bytes()
    assert creator.try_reserve_occurrence()
    created = creator.save_image(image, location="Page 1", ordinal=1, source_name="logo.png")
    assert consumer.try_reserve_occurrence()
    committed = consumer.save_image(image, location="Page 1", ordinal=1, source_name="logo.png")
    assert created is not None
    assert committed is not None
    assert created.reference == committed.reference

    consumer.commit_occurrences((committed,))
    creator.abort()

    assert _session_artifact_path(root.parent, committed.reference).exists()


def test_content_addressed_path_mismatch_is_reported_as_persistence_failure(tmp_path: Path) -> None:
    root = tmp_path / "session" / "doc_converter"
    creator = DocumentImageSink(root, source_stem="report")
    assert creator.try_reserve_occurrence()
    committed = creator.save_image(_png_bytes(), location="Page 1", ordinal=1, source_name="logo.png")
    assert committed is not None
    creator.commit_occurrences((committed,))
    artifact = _session_artifact_path(root.parent, committed.reference)
    artifact.write_bytes(_png_bytes((0, 0, 255)))

    collider = DocumentImageSink(root, source_stem="report")
    assert collider.try_reserve_occurrence()
    rejected = collider.save_image(_png_bytes(), location="Page 1", ordinal=1, source_name="logo.png")

    assert rejected is None
    assert collider.warnings == (
        "Skipped 1 image candidate(s) that could not be persisted or referenced as session artifacts.",
    )
    assert "decoded or normalized" not in collider.warnings[0]
    assert _session_artifact_path(root.parent, committed.reference).exists()


def test_image_handle_failure_is_reported_as_persistence_failure_and_cleans_orphan(tmp_path: Path) -> None:
    root = tmp_path / "session" / "doc_converter"
    sink = DocumentImageSink(root, source_stem="report")
    assert sink.try_reserve_occurrence()

    with patch.object(DocumentImageSink, "_model_reference", side_effect=ValueError("invalid handle")):
        occurrence = sink.save_image(_png_bytes(), location="Page 1", ordinal=1, source_name="logo.png")

    assert occurrence is None
    assert sink.warnings == (
        "Skipped 1 image candidate(s) that could not be persisted or referenced as session artifacts.",
    )
    assert len(list(root.glob("*.png"))) == 1

    sink.commit_occurrences(())

    assert list(root.glob("*.png")) == []


def test_document_image_sink_unique_limit_still_allows_repeated_digest(tmp_path: Path) -> None:
    sink = DocumentImageSink(tmp_path / "session" / "doc_converter", source_stem="report")
    first: VisualOccurrence | None = None
    with patch("chrys.service.tools.builtins.doc_converter.artifacts.MAX_UNIQUE_IMAGES", 2):
        for index in range(2):
            assert sink.try_reserve_occurrence()
            occurrence = sink.save_image(
                _png_bytes((index, 0, 0)),
                location="Page 1",
                ordinal=index + 1,
                source_name=f"{index}.png",
            )
            assert occurrence is not None
            first = first or occurrence

        assert sink.try_reserve_occurrence()
        rejected = sink.save_image(
            _png_bytes((2, 0, 0)),
            location="Page 1",
            ordinal=3,
            source_name="over.png",
        )
        assert rejected is None
        assert first is not None
        assert sink.try_reserve_occurrence()
        repeated = sink.save_image(
            _png_bytes((0, 0, 0)),
            location="Page 2",
            ordinal=1,
            source_name="repeat.png",
        )

    assert repeated is not None
    assert repeated.reference == first.reference
    assert len(list((tmp_path / "session" / "doc_converter").iterdir())) == 2


def test_document_image_sink_enforces_occurrence_limit_with_bounded_warning(tmp_path: Path) -> None:
    sink = DocumentImageSink(tmp_path / "session" / "doc_converter", source_stem="report")

    assert all(sink.try_reserve_occurrence() for _ in range(MAX_IMAGE_OCCURRENCES))
    assert sink.try_reserve_occurrence() is False
    sink.record_unprocessed_occurrences(72)

    assert len(sink.warnings) == 1
    assert "72 image candidate(s)" in sink.warnings[0]


def test_document_image_sink_enforces_total_normalized_byte_limit(tmp_path: Path) -> None:
    sink = DocumentImageSink(tmp_path / "session" / "doc_converter", source_stem="report")

    with (
        patch(
            "chrys.service.tools.builtins.doc_converter.artifacts.MAX_TOTAL_IMAGE_BYTES",
            10,
        ),
        patch.object(
            DocumentImageSink,
            "_normalize_image",
            side_effect=[(b"first!", ".png"), (b"second", ".png")],
        ),
    ):
        assert sink.try_reserve_occurrence()
        first = sink.save_image(b"source-1", location="Page 1", ordinal=1, source_name="one.png")
        assert sink.try_reserve_occurrence()
        second = sink.save_image(b"source-2", location="Page 1", ordinal=2, source_name="two.png")

    assert first is not None
    assert second is None
    assert any("storage limits" in warning for warning in sink.warnings)


def test_document_image_sink_enforces_session_quota_and_still_reuses_digest(tmp_path: Path) -> None:
    root = tmp_path / "session" / "doc_converter"
    first_bytes = _png_bytes()
    second_bytes = _png_bytes((0, 0, 255))

    with patch(
        "chrys.service.tools.builtins.doc_converter.artifacts.MAX_SESSION_IMAGE_BYTES",
        len(first_bytes),
    ):
        first_sink = DocumentImageSink(root, source_stem="first")
        assert first_sink.try_reserve_occurrence()
        first = first_sink.save_image(first_bytes, location="Page 1", ordinal=1, source_name="first.png")
        assert first is not None
        first_sink.commit_occurrences((first,))

        second_sink = DocumentImageSink(root, source_stem="second")
        assert second_sink.try_reserve_occurrence()
        rejected = second_sink.save_image(
            second_bytes,
            location="Page 1",
            ordinal=1,
            source_name="second.png",
        )
        assert rejected is None
        assert any("session image storage limits" in warning for warning in second_sink.warnings)

        repeated_sink = DocumentImageSink(root, source_stem="repeat")
        assert repeated_sink.try_reserve_occurrence()
        repeated = repeated_sink.save_image(
            first_bytes,
            location="Page 1",
            ordinal=1,
            source_name="repeat.png",
        )

    assert repeated is not None
    assert repeated.reference == first.reference
    assert len(list(root.glob("*.png"))) == 1


def test_document_image_sink_enforces_session_file_count_with_streaming_scan(tmp_path: Path) -> None:
    root = tmp_path / "session" / "doc_converter"
    root.mkdir(parents=True)
    (root / "image-existing-one.png").write_bytes(b"one")
    (root / "image-existing-two.jpg").write_bytes(b"two")
    (root / "report.md").write_text("ignored", encoding="utf-8")
    sink = DocumentImageSink(root, source_stem="report")

    with (
        patch(
            "chrys.service.tools.builtins.doc_converter.artifacts.MAX_SESSION_IMAGE_FILES",
            2,
        ),
        patch.object(Path, "iterdir", side_effect=AssertionError("quota scan must stream")),
    ):
        assert sink.try_reserve_occurrence()
        occurrence = sink.save_image(
            _png_bytes(),
            location="Page 1",
            ordinal=1,
            source_name="new.png",
        )

    assert occurrence is None
    assert any("session image storage limits" in warning for warning in sink.warnings)
    assert sorted(path.name for path in root.glob("*.png")) == ["image-existing-one.png"]


def test_concurrent_image_sinks_cannot_oversubscribe_session_quota(tmp_path: Path) -> None:
    root = tmp_path / "session" / "doc_converter"
    payloads = (_png_bytes(), _png_bytes((0, 0, 255)))
    barrier = threading.Barrier(2)
    results: list[tuple[VisualOccurrence | None, tuple[str, ...]]] = []
    results_lock = threading.Lock()

    def save(payload: bytes) -> None:
        sink = DocumentImageSink(root, source_stem="report")
        assert sink.try_reserve_occurrence()
        barrier.wait()
        occurrence = sink.save_image(payload, location="Page 1", ordinal=1, source_name="image.png")
        if occurrence is not None:
            sink.commit_occurrences((occurrence,))
        with results_lock:
            results.append((occurrence, sink.warnings))

    with patch(
        "chrys.service.tools.builtins.doc_converter.artifacts.MAX_SESSION_IMAGE_BYTES",
        max(map(len, payloads)),
    ):
        threads = [threading.Thread(target=save, args=(payload,)) for payload in payloads]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

    assert len(results) == 2
    assert sum(occurrence is not None for occurrence, _warnings in results) == 1
    assert any("session image storage limits" in warning for _occurrence, warnings in results for warning in warnings)
    assert len(list(root.glob("*.png"))) == 1


def test_document_image_sink_skips_normalized_image_over_per_file_limit(tmp_path: Path) -> None:
    sink = DocumentImageSink(tmp_path / "session" / "doc_converter", source_stem="report")

    with (
        patch("chrys.service.tools.builtins.doc_converter.artifacts.MAX_STORED_IMAGE_BYTES", 8),
        patch(
            "chrys.service.tools.builtins.doc_converter.artifacts.compress_image_data",
            return_value=b"x" * 9,
        ),
    ):
        assert sink.try_reserve_occurrence()
        occurrence = sink.save_image(b"unsupported-raster", location="Page 1", ordinal=1, source_name="huge.bmp")

    assert occurrence is None
    assert len(sink.warnings) == 1
    assert "could not be decoded or normalized" in sink.warnings[0]
    assert not (tmp_path / "session" / "doc_converter").exists()


@pytest.mark.parametrize("image_format", ["TIFF", "JPEG2000"])
def test_document_image_sink_converts_scanned_page_formats(tmp_path: Path, image_format: str) -> None:
    """pypdf hands scanned PDF pages over as TIFF or JPEG 2000."""
    root = tmp_path / "session" / "doc_converter"
    sink = DocumentImageSink(root, source_stem="report")

    assert sink.try_reserve_occurrence()
    occurrence = sink.save_image(image_bytes(image_format), location="Page 1", ordinal=1, source_name="scan")

    assert occurrence is not None
    assert [path.suffix for path in root.iterdir()] == [".jpg"]


def test_document_image_sink_keeps_every_pixel_of_a_converted_image(tmp_path: Path) -> None:
    # Wider than an image sent to a model is: that fitting happens when the copy is viewed.
    size = (COMPRESSED_IMAGE_MAX_EDGE * 2, 500)
    root = tmp_path / "session" / "doc_converter"
    sink = DocumentImageSink(root, source_stem="report")

    assert sink.try_reserve_occurrence()
    assert sink.save_image(image_bytes("TIFF", size=size), location="Page 1", ordinal=1, source_name="scan")

    (stored,) = root.iterdir()
    assert inspect_image_dimensions(stored.read_bytes()) == size


def _emf_header() -> bytes:
    """An enhanced metafile's header record, which Pillow identifies as WMF (rasterized through GDI on Windows)."""
    return struct.pack(
        "<II4i4i4sIIIHHIIIiiii",
        *(1, 88, 0, 0, 15, 9, 0, 0, 400, 240, b" EMF", 0x10000, 88, 1, 1, 0, 0, 0, 0, 1024, 768, 270, 203),
    )


@pytest.mark.parametrize(
    ("data", "pillow_format"),
    [pytest.param(image_bytes("PCX"), "PCX", id="pcx"), pytest.param(_emf_header(), "WMF", id="emf")],
)
def test_document_image_sink_skips_formats_pillow_may_not_decode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, data: bytes, pillow_format: str
) -> None:
    from PIL import Image

    real_open = Image.open
    assert real_open(BytesIO(data), formats=(pillow_format,)).format == pillow_format
    # Off Windows Pillow cannot rasterize a metafile at all, so the skip alone
    # proves nothing there: every open must also leave the format out.
    allowed: list[tuple[str, ...] | None] = []

    def recording_open(
        fp: str | IO[bytes], mode: str = "r", formats: list[str] | tuple[str, ...] | None = None
    ) -> Image.Image:
        allowed.append(None if formats is None else tuple(formats))
        return real_open(fp, mode, formats)

    monkeypatch.setattr(Image, "open", recording_open)
    sink = DocumentImageSink(tmp_path / "session" / "doc_converter", source_stem="report")

    assert sink.try_reserve_occurrence()
    occurrence = sink.save_image(data, location="Page 1", ordinal=1, source_name="clip")

    assert allowed
    assert all(formats is not None and pillow_format not in formats for formats in allowed)
    assert occurrence is None
    assert len(sink.warnings) == 1
    assert "could not be decoded or normalized" in sink.warnings[0]


def test_document_image_and_markdown_names_are_utf8_byte_bounded(tmp_path: Path) -> None:
    long_stem = "报告" * 100
    sink = DocumentImageSink(tmp_path / "session" / "doc_converter", source_stem=long_stem)
    assert sink.try_reserve_occurrence()
    occurrence = sink.save_image(_png_bytes(), location="Page 1", ordinal=1, source_name="内部名.png")

    assert occurrence is not None
    image_name = _session_artifact_path(tmp_path / "session", occurrence.reference).name
    assert len(image_name.encode("utf-8")) <= MAX_ARTIFACT_BASENAME_BYTES
    assert image_name == f"image-{hashlib.sha256(_png_bytes()).hexdigest()}.png"
    assert long_stem not in image_name
    assert "内部名" not in image_name


@pytest.mark.skipif(get_platform().is_windows, reason="symlink creation is not generally available on Windows")
def test_document_image_sink_rejects_redirected_artifact_root(tmp_path: Path) -> None:
    session_dir = tmp_path / "session"
    outside = tmp_path / "outside"
    session_dir.mkdir()
    outside.mkdir()
    (session_dir / "doc_converter").symlink_to(outside, target_is_directory=True)
    sink = DocumentImageSink(session_dir / "doc_converter", source_stem="report")
    assert sink.try_reserve_occurrence()

    occurrence = sink.save_image(_png_bytes(), location="Page 1", ordinal=1, source_name="pixel.png")

    assert occurrence is None
    assert list(outside.iterdir()) == []
    assert any("artifact directory" in warning for warning in sink.warnings)


@pytest.mark.skipif(get_platform().is_windows, reason="symlink creation is not generally available on Windows")
def test_markdown_writer_canonicalizes_trusted_session_parent_symlink(tmp_path: Path) -> None:
    real_root = tmp_path / "real"
    real_root.mkdir()
    alias_root = tmp_path / "alias"
    alias_root.symlink_to(real_root, target_is_directory=True)
    aliased_session_dir = alias_root / "session"

    saved = _write_unique_markdown(
        os.fspath(aliased_session_dir / "doc_converter"),
        "report",
        "# Report",
    )

    expected = real_root / "session" / "doc_converter" / "report.md"
    assert saved.handle == "chrys-session-document:report.md"
    assert saved.path == os.fspath(expected)
    assert expected.read_text(encoding="utf-8") == "# Report"
    assert resolve_document_markdown_artifact_handle(saved.handle, aliased_session_dir) == os.fspath(expected)


@pytest.mark.skipif(get_platform().is_windows, reason="symlink creation is not generally available on Windows")
def test_markdown_writer_rejects_redirected_artifact_child(tmp_path: Path) -> None:
    session_dir = tmp_path / "session"
    outside = tmp_path / "outside"
    session_dir.mkdir()
    outside.mkdir()
    (session_dir / "doc_converter").symlink_to(outside, target_is_directory=True)

    with pytest.raises(OSError, match="must not be redirected"):
        _write_unique_markdown(os.fspath(session_dir / "doc_converter"), "report", "# Report")

    assert list(outside.iterdir()) == []


@pytest.mark.skipif(get_platform().is_windows, reason="symlink creation is not generally available on Windows")
def test_document_image_sink_rejects_redirected_existing_image(tmp_path: Path) -> None:
    root = tmp_path / "session" / "doc_converter"
    first_sink = DocumentImageSink(root, source_stem="report")
    assert first_sink.try_reserve_occurrence()
    first = first_sink.save_image(_png_bytes(), location="Page 1", ordinal=1, source_name="pixel.png")
    assert first is not None
    first_sink.abort()

    outside = tmp_path / "outside.png"
    outside.write_bytes(b"outside")
    artifact = _session_artifact_path(tmp_path / "session", first.reference)
    artifact.symlink_to(outside)
    second_sink = DocumentImageSink(root, source_stem="report")
    assert second_sink.try_reserve_occurrence()

    occurrence = second_sink.save_image(_png_bytes(), location="Page 1", ordinal=1, source_name="pixel.png")

    assert occurrence is None
    assert outside.read_bytes() == b"outside"
    assert second_sink.warnings == (
        "Skipped 1 image candidate(s) that could not be persisted or referenced as session artifacts.",
    )


def test_concurrent_same_name_image_writes_do_not_overwrite_different_bytes(tmp_path: Path) -> None:
    root = tmp_path / "session" / "doc_converter"
    barrier = threading.Barrier(2)
    results: list[VisualOccurrence] = []
    result_lock = threading.Lock()

    def save(color: tuple[int, int, int]) -> None:
        sink = DocumentImageSink(root, source_stem="report")
        assert sink.try_reserve_occurrence()
        barrier.wait()
        occurrence = sink.save_image(_png_bytes(color), location="Page 1", ordinal=1, source_name="same.png")
        assert occurrence is not None
        sink.commit_occurrences((occurrence,))
        with result_lock:
            results.append(occurrence)

    threads = [
        threading.Thread(target=save, args=((255, 0, 0),)),
        threading.Thread(target=save, args=((0, 0, 255),)),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert len(results) == 2
    assert results[0].reference != results[1].reference
    stored = sorted(root.glob("*.png"))
    assert len(stored) == 2
    assert {path.read_bytes() for path in stored} == {_png_bytes((255, 0, 0)), _png_bytes((0, 0, 255))}
