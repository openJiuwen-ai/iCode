# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Shared base class for dismissable modal dialogs."""

from __future__ import annotations

from typing import Any, ClassVar, TypeVar

from textual import events
from textual.await_complete import AwaitComplete
from textual.screen import ModalScreen

from chrys.app.tui.behaviors.click_outside_dismiss import ClickOutsideDismissMixin
from chrys.app.tui.behaviors.insert_clipboard import INSERT_CLIPBOARD_BINDINGS, InsertClipboardScreenMixin
from chrys.app.tui.behaviors.right_click_copy import RightClickScreenCopyMixin
from chrys.app.tui.util.liveness import composed_live

DialogResultT = TypeVar("DialogResultT")


class BaseDialog(
    RightClickScreenCopyMixin,
    ClickOutsideDismissMixin,
    InsertClipboardScreenMixin,
    ModalScreen[DialogResultT],
):
    """Base for modal dialogs that may dismiss from a backdrop click.

    Keep this under ``screens/dialogs`` because it subclasses
    :class:`textual.screen.ModalScreen`. Non-dismissable dialogs can either
    pass ``dismiss_on_backdrop=False`` or override
    ``_allow_click_outside_dismiss`` for state-dependent gates.
    """

    # Modal screens end Textual's non-priority binding chain, so the
    # app-level Insert clipboard fallbacks must be repeated here.
    BINDINGS: ClassVar[list] = [*INSERT_CLIPBOARD_BINDINGS]

    def __init__(self, *args: Any, dismiss_on_backdrop: bool = True, **kwargs: Any) -> None:
        self._dismiss_on_backdrop = dismiss_on_backdrop
        self._deferred_dismiss: tuple[object | None] | None = None
        super().__init__(*args, **kwargs)

    def dismiss(self, result: object | None = None) -> AwaitComplete:
        """Dismiss once, running the pre-dismiss hook before first close."""
        if self._chrys_dismiss_await_complete is None:
            self._before_dismiss(result)
        return super().dismiss(result)

    @property
    def dismiss_requested(self) -> bool:
        return self._deferred_dismiss is not None or self._chrys_dismiss_await_complete is not None

    @property
    def _composed_live(self) -> bool:
        """See :func:`composed_live`."""
        return composed_live(self)

    def dismiss_when_topmost(self, result: object | None = None) -> None:
        """Retain completion while covered or mounting; never pop a screen belonging to another operation."""
        if self._chrys_dismiss_await_complete is not None:
            return
        if self._deferred_dismiss is None:
            self._deferred_dismiss = (result,)
        # Removing a dialog that is still composing mounts its widgets without their children.
        if self.is_mounted and self.is_attached and self.app.screen is self:
            (result,) = self._deferred_dismiss
            self._deferred_dismiss = None
            self.dismiss(result)
        else:
            self._on_deferred_dismiss()

    def _on_deferred_dismiss(self) -> None:
        """Hook for hiding completed content while another modal owns the stack."""

    def _on_mount(self, event: events.Mount) -> None:
        if self._deferred_dismiss is not None:
            # The dialog counts as mounted only once its mount handlers have returned.
            self.call_later(self.dismiss_when_topmost)

    def _on_screen_resume(self, event: events.ScreenResume) -> None:
        if self._deferred_dismiss is not None:
            self.dismiss_when_topmost()

    def _allow_click_outside_dismiss(self) -> bool:
        return self._dismiss_on_backdrop

    def _before_dismiss(self, _result: object | None = None) -> None:
        """Hook for subclasses that need cleanup before the modal closes."""
        return
