# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Real image bytes, for tests whose code reads an image's signature rather than its name."""

from __future__ import annotations

from io import BytesIO

from PIL import Image


def image_bytes(image_format: str = "PNG", *, size: tuple[int, int] = (2, 2)) -> bytes:
    """Encode a solid *size* image in any format Pillow writes (``PNG``, ``JPEG``, ``BMP``, ``TIFF``, …)."""
    out = BytesIO()
    Image.new("RGB", size, (200, 40, 40)).save(out, format=image_format)
    return out.getvalue()


def noisy_image_bytes(image_format: str = "PNG", *, size: tuple[int, int] = (640, 480)) -> bytes:
    """Encode an RGB noise *size* image, which no format compresses much: at the default size a PNG is over 800 KB."""
    out = BytesIO()
    Image.merge("RGB", [Image.effect_noise(size, 100) for _ in range(3)]).save(out, format=image_format)
    return out.getvalue()


def broken_chunk_png() -> bytes:
    """A noisy PNG whose header reads, but whose second image-data chunk has a corrupt chunk type."""
    data = bytearray(noisy_image_bytes("PNG"))
    image_data_chunks = 0
    position = 8
    while position < len(data):
        length = int.from_bytes(data[position : position + 4], "big")
        if data[position + 4 : position + 8] == b"IDAT":
            image_data_chunks += 1
            if image_data_chunks == 2:
                data[position + 4 : position + 8] = b"\x00\x01\x02\x03"
                return bytes(data)
        position += 12 + length
    raise AssertionError("The noisy PNG has a single image-data chunk.")
