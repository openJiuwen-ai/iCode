# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Decoded chat images, their cell layout, and their half-block rendering."""

from __future__ import annotations

import base64
import binascii
import logging
import math
from collections.abc import Sequence
from dataclasses import dataclass
from io import BytesIO
from typing import Any, Final, cast

from PIL import Image, ImageOps, UnidentifiedImageError
from rich.color import Color
from rich.segment import Segment
from rich.style import Style

from chrys.foundation.text.images import IMAGE_DECODE_FORMATS

TRANSPARENT_BACKGROUND: Final = (15, 18, 24)
"""What transparent pixels are flattened onto where the terminal cannot blend them."""
MAX_PREVIEW_INPUT_PIXELS: Final = 80_000_000
DEFAULT_MAX_ROWS: Final = 16
_HALF_BLOCK = "▀"
_IMAGE_GAP = 2
_MIN_IMAGE_WIDTH = 8
_MAX_PREVIEW_SOURCE_SIZE = (512, 512)
logger = logging.getLogger(__name__)


@dataclass(frozen=True, eq=False)
class ChatImagePreview:
    """A decoded thumbnail ready for half-block rendering, and the data it came from."""

    image: Image.Image
    media_type: str
    data_uri: str
    """The original image, decoded again at full size for terminals that draw real pixels."""


@dataclass(frozen=True)
class ImageBox:
    """Where one image of a row sits, in cells, and its half-block size in pixels."""

    x: int
    columns: int
    rows: int
    pixel_height: int
    """Half-block pixels tall (two per row); odd when the last row is half padding."""


def extract_image_previews(contents: Sequence[Any] | None) -> list[ChatImagePreview]:
    """Decode image content blocks from a framework/user-message content list."""
    if not contents:
        return []
    previews: list[ChatImagePreview] = []
    for content in contents:
        extracted = _extract_content_data_uri(content)
        if extracted is None:
            continue
        media_type, data_uri = extracted
        if not media_type.startswith("image/"):
            continue
        try:
            data = decode_data_uri(data_uri)
            image = _load_preview_image(data)
        except binascii.Error, Image.DecompressionBombError, OSError, UnidentifiedImageError, ValueError:
            logger.debug("Skipping invalid chat image preview for media type %s", media_type, exc_info=True)
            continue
        previews.append(ChatImagePreview(image=image, media_type=media_type, data_uri=data_uri))
    return previews


def layout_image_row(
    images: Sequence[ChatImagePreview],
    *,
    max_width: int,
    max_rows: int = DEFAULT_MAX_ROWS,
    allow_upscale: bool = False,
) -> tuple[ImageBox, ...]:
    """Lay images side by side within ``max_width`` cells, each at most ``max_rows`` rows tall.

    One column is one thumbnail pixel wide and one row two pixels tall, whatever draws the image, so
    the layout does not change with the terminal's graphics support.
    """
    if not images:
        return ()
    available = max(1, max_width)
    count = len(images)
    gap = _IMAGE_GAP if count > 1 else 0
    per_image_width = max(1, (available - gap * (count - 1)) // count)
    if per_image_width < _MIN_IMAGE_WIDTH and count > 1:
        gap = 1
        per_image_width = max(1, (available - gap * (count - 1)) // count)
    boxes: list[ImageBox] = []
    x = 0
    for preview in images:
        width, height = _fit_size(
            preview.image.size,
            max_width=per_image_width,
            max_height=max(1, max_rows) * 2,
            allow_upscale=allow_upscale,
        )
        boxes.append(ImageBox(x=x, columns=width, rows=math.ceil(height / 2), pixel_height=height))
        x += width + gap
    return tuple(boxes)


def half_block_rows(image: Image.Image, box: ImageBox) -> list[list[Segment]]:
    """Render ``image`` into ``box`` as half-block cells: the upper pixel in the foreground color."""
    fitted = (
        image
        if image.size == (box.columns, box.pixel_height)
        else image.resize((box.columns, box.pixel_height), Image.Resampling.LANCZOS)
    )
    if fitted.height % 2:
        padded = Image.new("RGB", (fitted.width, fitted.height + 1), TRANSPARENT_BACKGROUND)
        padded.paste(fitted, (0, 0))
        fitted = padded
    pixels = fitted.load()
    if pixels is None:
        return []
    rows: list[list[Segment]] = []
    for y in range(0, fitted.height, 2):
        row: list[Segment] = []
        for x in range(fitted.width):
            # Runtime guarantee: preview loading normalizes every image to RGB.
            upper = cast(tuple[int, int, int], pixels[x, y])
            lower = cast(tuple[int, int, int], pixels[x, y + 1])
            row.append(Segment(_HALF_BLOCK, Style(color=Color.from_rgb(*upper), bgcolor=Color.from_rgb(*lower))))
        rows.append(row)
    return rows


def decode_data_uri(uri: str) -> bytes:
    """The bytes of a base64 ``data:`` URI."""
    header, encoded = uri.split(",", 1)
    if ";base64" not in header:
        msg = "image preview data URI is not base64 encoded"
        raise ValueError(msg)
    return base64.b64decode(encoded, validate=True)


def _extract_content_data_uri(content: Any) -> tuple[str, str] | None:
    if isinstance(content, dict):
        media_type = content.get("media_type") or content.get("mime_type") or ""
        if not media_type:
            extra = content.get("additional_properties")
            media_type = extra.get("media_type") if isinstance(extra, dict) else ""
        uri = content.get("uri") or ""
    else:
        # Kernel Content is an external type; rich content fields
        # vary by content kind, so this boundary intentionally uses getattr.
        media_type = getattr(content, "media_type", "") or ""
        uri = getattr(content, "uri", "") or ""
    if not isinstance(media_type, str) or not isinstance(uri, str):
        return None
    if not uri.startswith("data:"):
        return None
    if not media_type:
        media_type = uri[5:].split(",", 1)[0].split(";", 1)[0]
    return media_type, uri


def _load_preview_image(data: bytes) -> Image.Image:
    with Image.open(BytesIO(data), formats=IMAGE_DECODE_FORMATS) as opened:
        if opened.width * opened.height > MAX_PREVIEW_INPUT_PIXELS:
            msg = "image preview input exceeds the supported pixel limit"
            raise ValueError(msg)
        opened.draft("RGB", _MAX_PREVIEW_SOURCE_SIZE)
        image = ImageOps.exif_transpose(opened)
        image.thumbnail(_MAX_PREVIEW_SOURCE_SIZE, Image.Resampling.LANCZOS)
        if image.mode in {"RGBA", "LA", "PA"} or "transparency" in image.info:
            rgba = image.convert("RGBA")
            background = Image.new("RGB", rgba.size, TRANSPARENT_BACKGROUND)
            background.paste(rgba, mask=rgba.getchannel("A"))
            return background
        return image.convert("RGB")


def _fit_size(
    size: tuple[int, int], *, max_width: int, max_height: int, allow_upscale: bool = False
) -> tuple[int, int]:
    width, height = size
    scale = min(max_width / width, max_height / height)
    if not allow_upscale:
        scale = min(scale, 1.0)
    return max(1, int(width * scale)), max(1, int(height * scale))
