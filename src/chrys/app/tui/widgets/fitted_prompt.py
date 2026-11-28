# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Width-aware prompt visual shared by workspace widgets."""

from __future__ import annotations

from typing import TYPE_CHECKING

from textual.content import Content
from textual.visual import Visual

if TYPE_CHECKING:
    from collections.abc import Callable

    from rich.text import Text
    from textual.css.styles import RulesMap
    from textual.strip import Strip
    from textual.style import Style
    from textual.visual import RenderOptions


class FittedPrompt(Visual):
    """Measure and draw a label at the same width, including its first layout."""

    def __init__(self, build: Callable[[int], Text]) -> None:
        self._build = build

    def _content(self, width: int) -> Content:
        return Content.from_rich_text(self._build(max(1, width)))

    def get_optimal_width(self, rules: RulesMap, container_width: int) -> int:
        return self._content(container_width).get_optimal_width(rules, container_width)

    def get_height(self, rules: RulesMap, width: int) -> int:
        return self._content(width).get_height(rules, width)

    def render_strips(self, width: int, height: int | None, style: Style, options: RenderOptions) -> list[Strip]:
        if width <= 0 or height == 0:
            return []
        return self._content(width).render_strips(width, height, style, options)
