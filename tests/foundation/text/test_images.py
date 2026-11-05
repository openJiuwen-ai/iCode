# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""The image formats model APIs read, and the ones Pillow may decode."""

from __future__ import annotations

from pathlib import Path

import pytest

from chrys.foundation.text.images import (
    IMAGE_DECODE_FORMATS,
    MAX_IMAGE_BYTES,
    ImageProcessingError,
    compress_image_data,
    inspect_image_dimensions,
    load_image_file,
    shrink_image_data,
    wire_image_media_type,
)
from tests.support.images import broken_chunk_png, image_bytes, noisy_image_bytes


@pytest.mark.parametrize(
    ("image_format", "declared", "expected"),
    [
        ("PNG", "image/png", "image/png"),
        ("JPEG", "image/png", "image/jpeg"),
        ("GIF", None, "image/gif"),
        ("WEBP", "image/jpeg", "image/webp"),
        ("BMP", "image/png", None),
    ],
)
def test_bytes_name_the_type_whatever_is_declared(
    image_format: str, declared: str | None, expected: str | None
) -> None:
    assert wire_image_media_type(image_bytes(image_format), declared) == expected


def test_bytes_without_a_signature_are_no_image_whatever_is_declared() -> None:
    assert wire_image_media_type(b"image-bytes", "image/png") is None


@pytest.mark.parametrize(
    ("declared", "expected"),
    [
        ("image/png", "image/png"),
        ("IMAGE/JPEG; charset=binary", "image/jpeg"),
        ("image/jpg", "image/jpeg"),
        ("image/bmp", None),
        ("image/svg+xml", None),
        (None, None),
    ],
)
def test_without_bytes_the_declared_type_decides(declared: str | None, expected: str | None) -> None:
    assert wire_image_media_type(None, declared) == expected


@pytest.mark.parametrize("image_format", IMAGE_DECODE_FORMATS)
def test_whitelisted_formats_decode_and_convert(image_format: str) -> None:
    data = image_bytes(image_format, size=(3, 2))

    assert inspect_image_dimensions(data) == (3, 2)
    assert compress_image_data(data).startswith(b"\xff\xd8\xff")


@pytest.mark.parametrize("image_format", ["TIFF", "PPM", "PCX", "SGI", "DDS", "JPEG2000"])
def test_formats_outside_the_whitelist_are_never_decoded(image_format: str) -> None:
    """Pillow reads every one of these; behind an image name they are still refused."""
    data = image_bytes(image_format)

    with pytest.raises(ImageProcessingError, match="could not be read as a supported image"):
        inspect_image_dimensions(data)
    with pytest.raises(ImageProcessingError, match="could not be read as a supported image"):
        compress_image_data(data)


def test_a_caller_names_any_wider_format_set_it_decodes() -> None:
    data = image_bytes("TIFF", size=(3, 2))
    formats = (*IMAGE_DECODE_FORMATS, "TIFF")

    assert inspect_image_dimensions(data, formats=formats) == (3, 2)
    assert compress_image_data(data, formats=formats).startswith(b"\xff\xd8\xff")


def test_an_image_within_the_send_limit_is_sent_as_it_is(tmp_path: Path) -> None:
    source = image_bytes("PNG", size=(640, 480))
    assert len(source) <= MAX_IMAGE_BYTES
    path = tmp_path / "small.png"
    path.write_bytes(source)

    loaded = load_image_file(path)

    assert loaded.data == source
    assert loaded.media_type == "image/png"


def test_an_image_over_the_send_limit_is_sent_as_a_jpeg_within_it(tmp_path: Path) -> None:
    source = noisy_image_bytes("PNG")
    assert len(source) > MAX_IMAGE_BYTES
    path = tmp_path / "large.png"
    path.write_bytes(source)

    loaded = load_image_file(path)

    assert loaded.media_type == "image/jpeg"
    assert loaded.data.startswith(b"\xff\xd8\xff")
    assert loaded.size <= MAX_IMAGE_BYTES
    # Within the limit at a JPEG quality on the ladder, so never scaled down.
    assert (loaded.width, loaded.height) == (640, 480)


def test_compression_fits_the_long_edge_before_anything_else() -> None:
    compressed = compress_image_data(image_bytes("PNG", size=(4000, 1000)))

    assert inspect_image_dimensions(compressed) == (2048, 512)


def test_compression_never_enlarges_an_image() -> None:
    assert inspect_image_dimensions(compress_image_data(image_bytes("PNG", size=(30, 20)))) == (30, 20)


def test_compression_scales_down_what_no_quality_on_the_ladder_fits() -> None:
    compressed = compress_image_data(noisy_image_bytes("PNG", size=(2048, 2048)))

    assert len(compressed) <= MAX_IMAGE_BYTES
    width, height = inspect_image_dimensions(compressed)
    assert width == height
    assert width < 2048


def test_an_image_no_size_fits_names_the_limit() -> None:
    # Under what the headers of even a one-pixel JPEG take.
    with pytest.raises(ImageProcessingError, match="still larger than 300 bytes after compression"):
        compress_image_data(noisy_image_bytes("PNG"), max_bytes=300)


def test_an_image_whose_header_reads_but_whose_data_is_broken_is_unreadable() -> None:
    broken = broken_chunk_png()
    # Pillow reads the header, then raises a SyntaxError decoding the data.
    assert inspect_image_dimensions(broken) == (640, 480)

    with pytest.raises(ImageProcessingError, match="could not be read as a supported image"):
        compress_image_data(broken, max_bytes=64 * 1024)
    assert shrink_image_data(broken, max_bytes=64 * 1024) is None
