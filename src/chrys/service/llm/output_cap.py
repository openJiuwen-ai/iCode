# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""The output cap in any of its chat-option spellings, as the one name a request builder renames.

A user or a profile may set the cap as ``max_tokens``, ``max_output_tokens``
or ``max_completion_tokens``. Each wire protocol sends it under the name its
own variant names, and each request builder gets there by renaming the
canonical ``max_tokens``; a cap left under another spelling would reach the
SDK call as an unknown keyword and fail the whole call.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any, Final

from chrys.kernel.client import OUTPUT_CAP_OPTION_ALIASES

logger = logging.getLogger(__name__)

CANONICAL_OUTPUT_CAP_OPTION: Final = "max_tokens"
"""The spelling every wire protocol's request builder renames to its own field."""


def fold_output_cap_options(options: Mapping[str, Any]) -> dict[str, Any]:
    """*options* with every output-cap spelling folded onto :data:`CANONICAL_OUTPUT_CAP_OPTION`.

    The canonical alias wins when more than one spelling is supplied, the
    conflict order ``OUTPUT_CAP_OPTION_ALIASES`` declares; the others are
    removed so no unknowable field reaches the SDK call. A cap spelled only
    differently keeps its value, which is what the caller asked to send.
    """
    folded = dict(options)
    supplied = [alias for alias in OUTPUT_CAP_OPTION_ALIASES if folded.get(alias) is not None]
    if len(supplied) < 2 and (not supplied or supplied[0] == CANONICAL_OUTPUT_CAP_OPTION):
        return folded
    cap = folded[supplied[0]]
    for alias in supplied:
        del folded[alias]
    folded[CANONICAL_OUTPUT_CAP_OPTION] = cap
    if len(supplied) > 1:
        logger.warning(
            "Several output-cap options were supplied (%s); sending %r under %r",
            ", ".join(supplied),
            cap,
            CANONICAL_OUTPUT_CAP_OPTION,
        )
    return folded
