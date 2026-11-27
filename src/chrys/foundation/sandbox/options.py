# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Pure option validation for the initial two backend schemas; no engine loading."""

from __future__ import annotations

from collections.abc import Mapping
from urllib.parse import urlsplit

from chrys.foundation.sandbox.models import JiuwenboxOptions, ProcessOptions, SandboxError


def parse_options(backend: str, options: Mapping[str, object]) -> ProcessOptions | JiuwenboxOptions:
    """Validate selected options without contacting or discovering a backend.

    The schema list is explicit. It is not a production adapter registry and
    must never cause an arbitrary module from configuration to be imported.
    """
    if backend == "process":
        if options:
            raise SandboxError("invalid_options: process has no new-schema options in this draft")
        return ProcessOptions()
    if backend != "jiuwenbox":
        raise SandboxError("unknown_backend: selected profile has no registered schema")
    if options.keys() - {"network", "url", "token"}:
        raise SandboxError("invalid_options: unknown jiuwenbox option")
    network = options.get("network", "guarded")
    if not isinstance(network, str) or network not in {"guarded", "host", "offline"}:
        raise SandboxError("invalid_options: network must be guarded, host or offline")
    url = options.get("url", "")
    token = options.get("token", "")
    if not isinstance(url, str) or not isinstance(token, str):
        raise SandboxError("invalid_options: url and token must be strings")
    if url:
        try:
            parts = urlsplit(url)
            port = parts.port
            valid = (
                parts.scheme in {"http", "https"}
                and bool(parts.hostname)
                and parts.username is None
                and parts.password is None
                and not parts.query
                and not parts.fragment
                and parts.path in {"", "/"}
                and (port is None or port > 0)
                and not any(char.isspace() or ord(char) < 32 for char in url)
            )
        except ValueError:
            valid = False
        if not valid:
            raise SandboxError("invalid_options: url must be an HTTP(S) origin without credentials")
    return JiuwenboxOptions(network=network, url=url, token=token)
