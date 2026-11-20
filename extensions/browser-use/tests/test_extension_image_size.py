# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

from __future__ import annotations

from icode_browser_use.extension import _image_size


def test_image_size_reads_png_header() -> None:
    header = b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\x0dIHDR" + (640).to_bytes(4, "big") + (480).to_bytes(4, "big")
    assert _image_size(header) == (640, 480)


def test_image_size_rejects_unknown_data() -> None:
    assert _image_size(b"not-an-image") is None
