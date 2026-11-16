# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""A JSON payload whose integer literal is past Python's integer-string conversion limit.

``json.loads`` then raises a bare ``ValueError`` rather than a
``JSONDecodeError``. The limit is configurable, so the payload is derived from
``sys.get_int_max_str_digits()`` instead of a fixed digit count: a larger limit
would otherwise make the payload parse cleanly and the test pass for the wrong
reason. An unlimited interpreter (``0``) has no such payload and skips.
"""

from __future__ import annotations

import sys

import pytest


def oversized_integer_json() -> str:
    """``{"limit": <integer past the conversion limit>}``; skips when conversion is unlimited."""
    limit = sys.get_int_max_str_digits()
    if limit <= 0:
        pytest.skip("integer string conversion is unlimited")
    return '{"limit": ' + "9" * (limit + 10) + "}"
