# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""The cell layout of a row of chat images, the same whatever draws them."""

from __future__ import annotations

from typing import TYPE_CHECKING

from chrys.app.tui.widgets.chat import image_view
from chrys.app.tui.widgets.chat.image_preview import ImageBox, half_block_rows, layout_image_row
from chrys.app.tui.widgets.chat.image_view import TerminalImages
from tests.support.terminal_images import TerminalImagesApp, image_previews
from tests.support.waiting import wait_for

if TYPE_CHECKING:
    import pytest
    from PIL import Image
    from rich.segment import Segment


def test_images_sit_side_by_side_two_columns_apart_each_within_its_share() -> None:
    previews = image_previews((48, 30), (48, 30))

    # Each gets (42 - 2) // 2 = 20 columns and 6 rows (12 pixels): 48x30 scales by 0.4.
    assert layout_image_row(previews, max_width=42, max_rows=6) == (ImageBox(0, 19, 6, 12), ImageBox(21, 19, 6, 12))


def test_a_narrow_row_closes_the_gap_to_one_column() -> None:
    previews = image_previews((48, 30), (48, 30))

    # Two columns apart, each image would be narrower than eight columns.
    assert layout_image_row(previews, max_width=12, max_rows=6) == (ImageBox(0, 5, 2, 3), ImageBox(6, 5, 2, 3))


def test_small_images_keep_their_size_unless_upscaling_is_allowed() -> None:
    previews = image_previews((4, 2))

    assert layout_image_row(previews, max_width=20, max_rows=100) == (ImageBox(0, 4, 1, 2),)
    assert layout_image_row(previews, max_width=20, max_rows=100, allow_upscale=True) == (ImageBox(0, 20, 5, 10),)


async def test_half_block_rows_are_built_once_for_each_image_and_box(monkeypatch: pytest.MonkeyPatch) -> None:
    """Building them resamples the image and walks its pixels: never once per line or per frame."""
    built: list[ImageBox] = []

    def counting(image: Image.Image, box: ImageBox) -> list[list[Segment]]:
        built.append(box)
        return half_block_rows(image, box)

    monkeypatch.setattr(image_view, "half_block_rows", counting)
    app = TerminalImagesApp(None)
    async with app.run_test(size=(80, 24)) as pilot:
        view = TerminalImages(image_previews((40, 20)))
        await app.mount(view)
        await wait_for(lambda: built, pilot=pilot, description="the image was drawn")
        for _ in range(2):
            for y in range(10):
                view.render_line(y)
        assert built == [ImageBox(0, 40, 10, 20)]

        view.styles.width = 20
        await wait_for(lambda: len(built) == 2, pilot=pilot, description="the narrower box was drawn")
        for y in range(5):
            view.render_line(y)
        assert built == [ImageBox(0, 40, 10, 20), ImageBox(0, 20, 5, 10)]
