# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Locale-neutral message definitions and display argument types."""

from chrys.foundation.i18n.locale import SUPPORTED_LOCALES, normalize_locale
from chrys.foundation.i18n.localizer import CatalogLoadWarning, CatalogWarningCode, Localizer
from chrys.foundation.i18n.messages import (
    DisplayBlock,
    DisplayJoined,
    DisplayPath,
    DisplaySequence,
    MessageArg,
    MessageDef,
    MessageRef,
    MessageScalar,
    msg,
)

__all__ = [
    "SUPPORTED_LOCALES",
    "CatalogLoadWarning",
    "CatalogWarningCode",
    "DisplayBlock",
    "DisplayJoined",
    "DisplayPath",
    "DisplaySequence",
    "Localizer",
    "MessageArg",
    "MessageDef",
    "MessageRef",
    "MessageScalar",
    "msg",
    "normalize_locale",
]
