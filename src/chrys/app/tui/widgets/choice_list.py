# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""An option list whose Enter chooses the row the moves typed before it leave highlighted."""

from __future__ import annotations

from typing import ClassVar

from textual.binding import Binding, BindingType
from textual.widgets import OptionList


class ChoiceList(OptionList):
    """Enter runs the screen's ``choose`` action, as a binding of this list.

    Textual runs a focused widget's bindings, the list's moves among them, only once the key has
    climbed to the App, while a screen's key handler sees each key on its way up. Read there, an
    Enter typed while the loop was busy would act before the moves typed ahead of it had run.
    """

    BINDINGS: ClassVar[list[BindingType]] = [Binding("enter", "screen.choose", show=False)]
