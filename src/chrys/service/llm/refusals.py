# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""The failure of a refused response that still asks for tool calls, shared by every wire protocol.

It sits outside the protocol packages: each imports its own provider SDK, so
the Anthropic client taking it from the Chat Completions package would load
the whole OpenAI SDK with it.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING

from chrys.foundation.errors import ProviderResponseError

if TYPE_CHECKING:
    from chrys.kernel import Content, UsageDetails


def refused_calls_error(
    observed_contents: Sequence[Content] = (), *, usage_details: UsageDetails | None = None
) -> ProviderResponseError:
    """The failure of a response that refused or was filtered, yet asks for tool calls.

    The calls are never run: the response's own verdict is that it should
    not go on, and that verdict stands even when the response also failed
    or was cut off. *observed_contents* is the hosted work the response
    showed but never yielded.
    """
    return ProviderResponseError(
        "content_filter",
        "The response was refused or filtered, so the tool calls it requested were not run.",
        retryable=False,
        invalidates_continuation_token=True,
        observed_contents=tuple(observed_contents),
        usage_details=usage_details,
    )
