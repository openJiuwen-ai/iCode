# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Encode ACP payloads exactly as the SDK puts them on the wire.

The SDK dumps a params model in Python mode (``acp.utils.serialize_params``),
a response model in JSON mode, and passes dict payloads through untouched,
then writes the result with ``json.dumps``. A value JSON cannot encode fails
that write, and inside a prompt the failed send fails the whole turn. Test
doubles that stand in for the connection run every payload through these
helpers so a test fails at the send that would have failed in production.

A failure is raised and also recorded, because production code catches
``Exception`` around some sends (a permission request that fails reads as a
rejection) and would swallow the ``AssertionError``. ``tests/app/acp/conftest.py``
fails any test that leaves a recorded failure unclaimed; a test that expects
one claims it with :func:`take_acp_wire_failures`.

The check is stricter than the SDK in two ways: it rejects NaN and infinity,
which ``json.dumps`` writes as bare tokens that JSON parsers refuse, and lone
surrogates (an undecodable byte in a path), which it writes as ``\\udcXX``
escapes that strict parsers, iCode's own ACP client among them, refuse. Each
payload is written as the SDK writes it and read back with that client's
parser, so a surrogate pair, which the escapes carry whole, passes.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from acp import RequestError
from acp.utils import serialize_params
from pydantic import BaseModel

from chrys.service.acp_client.protocol import parse_protocol_json

_recorded_failures: list[str] = []


def acp_outgoing_json(payload: BaseModel | Mapping[str, Any] | None) -> Any:
    """Return the JSON the client decodes from a notification or request *payload*."""
    return _encode(serialize_params(payload) if isinstance(payload, BaseModel) else payload)


def acp_response_json(result: object) -> Any:
    """Return the JSON the client decodes from a handler's *result*."""
    if isinstance(result, BaseModel):
        result = result.model_dump(mode="json", by_alias=True, exclude_none=True, exclude_unset=True)
    return _encode(result)


def acp_error_json(error: RequestError) -> Any:
    """Return the JSON the client decodes from a handler's raised *error*."""
    return _encode(error.to_error_obj())


def take_acp_wire_failures() -> list[str]:
    """Return the encode failures recorded since the last call, and forget them."""
    failures = list(_recorded_failures)
    _recorded_failures.clear()
    return failures


def _encode(value: object) -> Any:
    try:
        text = json.dumps(value, separators=(",", ":"), allow_nan=False)
        return parse_protocol_json(text.encode("ascii"))
    except (TypeError, ValueError) as exc:
        # json adds "when serializing dict item 'x'" notes naming the path.
        where = " ".join(getattr(exc, "__notes__", ()))
        failure = f"ACP payload cannot be sent as JSON: {exc} {where}".rstrip()
        _recorded_failures.append(failure)
        raise AssertionError(failure) from exc
