# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Reusable modal dialogs."""

from __future__ import annotations

from importlib import import_module
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from chrys.app.tui.screens.dialogs.agent_load import AgentLoadDialog as AgentLoadDialog
    from chrys.app.tui.screens.dialogs.approval import ApprovalDialog as ApprovalDialog
    from chrys.app.tui.screens.dialogs.approval.mode import ApprovalModeScreen as ApprovalModeScreen
    from chrys.app.tui.screens.dialogs.ask_user import AskUserDialog as AskUserDialog
    from chrys.app.tui.screens.dialogs.base import BaseDialog as BaseDialog
    from chrys.app.tui.screens.dialogs.confirm import ConfirmDialog as ConfirmDialog
    from chrys.app.tui.screens.dialogs.connection_test import ConnectionTestDialog as ConnectionTestDialog
    from chrys.app.tui.screens.dialogs.editor import EditorDialog as EditorDialog
    from chrys.app.tui.screens.dialogs.editor import EditorDialogResult as EditorDialogResult
    from chrys.app.tui.screens.dialogs.file_picker import FilePicker as FilePicker
    from chrys.app.tui.screens.dialogs.file_picker import FilePickerMode as FilePickerMode
    from chrys.app.tui.screens.dialogs.image_compression import ImageCompressionDialog as ImageCompressionDialog
    from chrys.app.tui.screens.dialogs.man_page import ManPageDialog as ManPageDialog
    from chrys.app.tui.screens.dialogs.session_in_use import SessionInUseDialog as SessionInUseDialog
    from chrys.app.tui.screens.dialogs.session_title import SessionTitleDialog as SessionTitleDialog
    from chrys.app.tui.screens.dialogs.vision_unsupported import VisionUnsupportedDialog as VisionUnsupportedDialog

_EXPORTS = {
    "AgentLoadDialog": "agent_load",
    "ApprovalDialog": "approval",
    "ApprovalModeScreen": "approval.mode",
    "AskUserDialog": "ask_user",
    "BaseDialog": "base",
    "ConfirmDialog": "confirm",
    "ConnectionTestDialog": "connection_test",
    "EditorDialog": "editor",
    "EditorDialogResult": "editor",
    "FilePicker": "file_picker",
    "FilePickerMode": "file_picker",
    "ImageCompressionDialog": "image_compression",
    "ManPageDialog": "man_page",
    "SessionInUseDialog": "session_in_use",
    "SessionTitleDialog": "session_title",
    "VisionUnsupportedDialog": "vision_unsupported",
}

__all__ = sorted(_EXPORTS)


def __getattr__(name: str) -> Any:
    """Lazily resolve one dialog without importing the others."""
    if name not in _EXPORTS:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    module = import_module(f"{__name__}.{_EXPORTS[name]}")
    value = getattr(module, name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    """Return the default module attributes plus facade exports."""
    return sorted({*globals(), *__all__})
