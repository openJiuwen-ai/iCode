# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Shared image loading, validation, and compression helpers."""

from __future__ import annotations

import math
import struct
import warnings
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from typing import Any

from chrys.foundation.branding import APP_DISPLAY_NAME

IMAGE_MIME_BY_EXTENSION: dict[str, str] = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
}
# The most bytes an image is sent to a model with. A larger image, or one in a
# format model APIs don't read, is compressed to a JPEG within it: every image
# in the history rides each request, and a gateway in front of a model service
# may reject a request the service itself accepts.
MAX_IMAGE_BYTES = 256 * 1024
# The largest image accepted at all, as bytes and as decoded pixels.
MAX_IMAGE_SOURCE_BYTES = 50 * 1024 * 1024
MAX_IMAGE_PIXELS = 80_000_000
# The largest image an ACP message carries: its base64, under 43 MiB, fits the
# 50 MiB line a JSON-RPC message is read as, leaving room for the rest of it.
MAX_ACP_IMAGE_BYTES = 32 * 1024 * 1024
# The only formats Pillow decodes for an attached or viewed image: the four
# model APIs read plus BMP, which converts. A file in any other format behind
# an image name never reaches the rest of Pillow's decoders.
IMAGE_DECODE_FORMATS: tuple[str, ...] = ("PNG", "JPEG", "GIF", "WEBP", "BMP")

COMPRESSED_IMAGE_MEDIA_TYPE = "image/jpeg"
# What Pillow raises decoding a malformed image: OSError mostly, but a broken
# PNG chunk, for one, is a SyntaxError.
_DECODE_ERRORS: tuple[type[Exception], ...] = (OSError, ValueError, SyntaxError, EOFError, IndexError, struct.error)
# Compression fits an image within this long edge first, never enlarging it,
# then tries these JPEG qualities in turn; an image still too large is scaled
# down further at the same qualities, never encoded at a lower one.
COMPRESSED_IMAGE_MAX_EDGE = 2048
_JPEG_QUALITIES = (88, 82, 76)
_MAX_COMPRESSION_PASSES = 8


@dataclass(frozen=True)
class LoadedImage:
    """Model-ready image bytes with metadata."""

    data: bytes
    media_type: str
    width: int
    height: int

    @property
    def size(self) -> int:
        """Return byte size of the model-ready payload."""
        return len(self.data)


class ImageProcessingError(Exception):
    """Raised when an image cannot be prepared for model input."""


def is_image_media_type(media_type: Any) -> bool:
    """Return True when *media_type* is an image MIME type."""
    if not isinstance(media_type, str):
        return False
    top_level = media_type.split(";", 1)[0].split("/", 1)[0].strip().lower()
    return top_level == "image"


_WIRE_IMAGE_MEDIA_TYPES = frozenset({"image/png", "image/jpeg", "image/gif", "image/webp"})
_IMAGE_MEDIA_TYPE_ALIASES = {"image/jpg": "image/jpeg"}


def wire_image_media_type(data: bytes | None, declared: str | None) -> str | None:
    """The media type model APIs accept for an image: PNG, JPEG, GIF or WebP; None for any other.

    Bytes decide when there are any: their signature names the type whatever
    *declared* says, and bytes without one of these signatures are no image
    a model API reads. Without bytes (an image URL) only *declared* can tell.
    """
    if data is not None:
        if data.startswith(b"\x89PNG\r\n\x1a\n"):
            return "image/png"
        if data.startswith(b"\xff\xd8\xff"):
            return "image/jpeg"
        if data.startswith((b"GIF87a", b"GIF89a")):
            return "image/gif"
        if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
            return "image/webp"
        return None
    if not isinstance(declared, str):
        return None
    media_type = declared.split(";", 1)[0].strip().lower()
    media_type = _IMAGE_MEDIA_TYPE_ALIASES.get(media_type, media_type)
    return media_type if media_type in _WIRE_IMAGE_MEDIA_TYPES else None


def detect_image_media_type(path: str | Path, data: bytes | None = None) -> str | None:
    """Return a supported image media type from magic bytes or extension."""
    if data is not None:
        if data.startswith(b"\x89PNG\r\n\x1a\n"):
            return "image/png"
        if data.startswith(b"\xff\xd8\xff"):
            return "image/jpeg"
        if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
            return "image/webp"
    media_type = IMAGE_MIME_BY_EXTENSION.get(Path(path).suffix.lower())
    if media_type is not None:
        return media_type
    return None


def read_image_source_bytes(path: Path, *, max_bytes: int = MAX_IMAGE_SOURCE_BYTES) -> bytes:
    """Read at most one byte past the source-image cap."""
    with path.open("rb") as file:
        return file.read(max_bytes + 1)


def load_image_file(path: Path, media_type: str | None = None, *, max_bytes: int = MAX_IMAGE_BYTES) -> LoadedImage:
    """Load an image file for model input, compressed when it is over *max_bytes*."""
    try:
        data = read_image_source_bytes(path)
    except ValueError as exc:
        raise ImageProcessingError(f"This image path is invalid: {exc}") from exc
    except OSError as exc:
        raise ImageProcessingError(f"{APP_DISPLAY_NAME} couldn't read this image file: {exc}") from exc

    if len(data) > MAX_IMAGE_SOURCE_BYTES:
        raise ImageProcessingError(format_source_too_large_reason(len(data)))

    detected_media_type = detect_image_media_type(path, data) or media_type
    if detected_media_type is None:
        raise ImageProcessingError("This file is not a supported image. Use PNG, JPEG, or WebP.")

    width, height = inspect_image_dimensions(data)
    # Sent as it is only when the bytes are a format model APIs read; other
    # bytes behind an image name (a renamed BMP) are converted like an
    # oversized image.
    if len(data) <= max_bytes and (wire_media_type := wire_image_media_type(data, None)) is not None:
        return LoadedImage(data=data, media_type=wire_media_type, width=width, height=height)

    compressed = compress_image_data(data, max_bytes=max_bytes)
    compressed_width, compressed_height = inspect_image_dimensions(compressed)
    return LoadedImage(
        data=compressed,
        media_type=COMPRESSED_IMAGE_MEDIA_TYPE,
        width=compressed_width,
        height=compressed_height,
    )


def inspect_image_dimensions(data: bytes, *, formats: tuple[str, ...] = IMAGE_DECODE_FORMATS) -> tuple[int, int]:
    """Decode image metadata, in one of *formats*, and validate dimensions."""
    from PIL import Image, UnidentifiedImageError

    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(BytesIO(data), formats=formats) as opened:
                validate_image_dimensions(opened.size)
                return opened.size
    except (Image.DecompressionBombError, Image.DecompressionBombWarning) as exc:
        raise ImageProcessingError(format_image_too_large_to_process_reason()) from exc
    except (UnidentifiedImageError, OSError, ValueError) as exc:
        raise ImageProcessingError(
            "This file could not be read as a supported image. Export it as PNG, JPEG, or WebP, then try again."
        ) from exc


def compress_image_data(
    data: bytes,
    *,
    max_bytes: int = MAX_IMAGE_BYTES,
    formats: tuple[str, ...] = IMAGE_DECODE_FORMATS,
    max_edge: int | None = COMPRESSED_IMAGE_MAX_EDGE,
) -> bytes:
    """Compress image bytes, in one of *formats*, to a JPEG under *max_bytes*.

    The image is fitted within *max_edge* on its long edge first, as an image
    sent to a model is; None keeps every pixel, as a stored copy does.
    """
    from PIL import Image, ImageOps

    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(BytesIO(data), formats=formats) as opened:
                validate_image_dimensions(opened.size)
                opened = ImageOps.exif_transpose(opened)
                image = _to_jpeg_rgb(opened)
    except (Image.DecompressionBombError, Image.DecompressionBombWarning) as exc:
        raise ImageProcessingError(format_image_too_large_to_process_reason()) from exc
    except _DECODE_ERRORS as exc:
        raise ImageProcessingError(
            "This file could not be read as a supported image. Export it as PNG, JPEG, or WebP, then try again."
        ) from exc

    try:
        compressed = _compress_jpeg_under_limit(image, max_bytes=max_bytes, max_edge=max_edge)
    except (OSError, ValueError) as exc:
        raise ImageProcessingError(
            "This image could not be compressed. Resize it or export a smaller copy, then try again."
        ) from exc
    if compressed is None:
        raise ImageProcessingError(
            f"This image is still larger than {_format_size(max_bytes)} after compression. "
            "Resize it or export a smaller copy, then try again."
        )
    return compressed


def shrink_image_data(data: bytes, *, max_bytes: int) -> LoadedImage | None:
    """The JPEG within *max_bytes* an image already in a conversation shrinks to, or None when none fits."""
    try:
        compressed = compress_image_data(data, max_bytes=max_bytes)
        width, height = inspect_image_dimensions(compressed)
    except ImageProcessingError:
        return None
    return LoadedImage(data=compressed, media_type=COMPRESSED_IMAGE_MEDIA_TYPE, width=width, height=height)


def validate_image_dimensions(size: tuple[int, int]) -> None:
    """Reject images whose decoded pixel count would be excessive."""
    width, height = size
    if width <= 0 or height <= 0:
        raise ImageProcessingError("This image has invalid dimensions and cannot be attached.")
    if width * height > MAX_IMAGE_PIXELS:
        raise ImageProcessingError(format_image_too_large_to_process_reason(width=width, height=height))


def format_image_too_large_to_process_reason(width: int | None = None, height: int | None = None) -> str:
    """Return a friendly reason for images whose decoded dimensions are too large."""
    size_detail = f" ({width:,}x{height:,}; limit {MAX_IMAGE_PIXELS:,} pixels)" if width and height else ""
    return (
        f"This image is too large to process safely{size_detail}. Resize it or export a smaller copy, then try again."
    )


def format_source_too_large_reason(size_bytes: int) -> str:
    """Return a friendly source-size error."""
    return (
        f"This image is {format_mb(size_bytes)}, over the {format_mb(MAX_IMAGE_SOURCE_BYTES)} source limit. "
        "Resize it or export a smaller copy, then try again."
    )


def format_mb(size_bytes: int) -> str:
    """Format bytes as decimal-ish megabytes for user-facing errors."""
    return f"{size_bytes / (1024 * 1024):.1f} MB"


def _format_size(size_bytes: int) -> str:
    """Format bytes as bytes, kilobytes or, from a megabyte, as :func:`format_mb` does."""
    if size_bytes < 1024:
        return f"{size_bytes} bytes"
    if size_bytes < 1024 * 1024:
        return f"{math.ceil(size_bytes / 1024)} KB"
    return format_mb(size_bytes)


def data_uri_payload_info(uri: str) -> tuple[int | None, str]:
    """Return decoded-byte length and hash payload for a data URI without decoding bytes."""
    encoded = uri.split(",", 1)[1] if "," in uri else uri
    compact = "".join(encoded.split())
    padding = compact.count("=")
    byte_length = (len(compact) * 3 // 4) - padding if compact else 0
    return max(byte_length, 0), compact


def hashable_image_identity(uri: str) -> tuple[int | None, str]:
    """Return byte length and stable payload text for an image URI without network access."""
    if uri.startswith("data:"):
        return data_uri_payload_info(uri)
    return None, uri


def _to_jpeg_rgb(image: Any) -> Any:
    """Return an RGB Pillow image suitable for JPEG output."""
    from PIL import Image

    if image.mode == "RGB":
        return image.copy()
    if image.mode in {"RGBA", "LA", "PA"} or "transparency" in image.info:
        rgba = image.convert("RGBA")
        background = Image.new("RGB", rgba.size, (255, 255, 255))
        background.paste(rgba, mask=rgba.getchannel("A"))
        return background
    return image.convert("RGB")


def _fit_long_edge(image: Any, max_edge: int) -> Any:
    """Return *image* scaled down to fit *max_edge* on its long edge, or itself when it already fits."""
    from PIL import Image

    width, height = image.size
    long_edge = max(width, height)
    if long_edge <= max_edge:
        return image
    scale = max_edge / long_edge
    size = (max(1, round(width * scale)), max(1, round(height * scale)))
    return image.resize(size, Image.Resampling.LANCZOS, reducing_gap=3.0)


def _compress_jpeg_under_limit(image: Any, *, max_bytes: int, max_edge: int | None) -> bytes | None:
    """Encode *image* as a JPEG of at most *max_bytes*, or None when no size within the passes gets there."""
    from PIL import Image

    fitted = image if max_edge is None else _fit_long_edge(image, max_edge)
    working = fitted
    for _ in range(_MAX_COMPRESSION_PASSES):
        encoded_sizes: list[int] = []
        for quality in _JPEG_QUALITIES:
            encoded = _encode_jpeg(working, quality=quality)
            if len(encoded) <= max_bytes:
                return encoded
            encoded_sizes.append(len(encoded))

        # Bytes grow about with the pixel count: aim the next size a little under
        # the limit, each side shrinking by at least a tenth and to no less than a third.
        width, height = working.size
        ratio = min(0.90, max(0.35, math.sqrt(max_bytes / min(encoded_sizes)) * 0.96))
        new_size = (max(1, int(width * ratio)), max(1, int(height * ratio)))
        if new_size == working.size:
            return None
        # Each pass resamples the fitted image, not the previous pass's.
        working = fitted.resize(new_size, Image.Resampling.LANCZOS)

    return None


def _encode_jpeg(image: Any, *, quality: int) -> bytes:
    """Encode a Pillow image as optimized progressive JPEG bytes."""
    out = BytesIO()
    image.save(out, format="JPEG", quality=quality, optimize=True, progressive=True)
    return out.getvalue()
