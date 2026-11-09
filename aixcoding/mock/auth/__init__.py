# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""AIxCoding device-code auth mock server (local debug tool, never shipped).

Simulates the intranet auth service behind ``82.187.34.98`` (PROD) /
``81.89.182.150`` (DEV), which is unreachable off the bank network: the
device-code endpoints (``/auth/device/code``, ``/auth/device/token``), the
``/user/info`` endpoint, and a bundled local verification page that decides
pending grants in ``manual`` mode. Wire quirks of the real backend (the
``result`` vs ``data`` envelope split, ``success: true`` while pending, the
``success``-less unknown-token body) are reproduced byte-for-byte so the
client must parse the envelope, never the HTTP status.

Public API lives in :mod:`aixcoding.mock.auth.server`; the canonical
entry points are :func:`aixcoding.mock.auth.server.create_server`
(tests) and ``python aixcoding/mock/auth/server.py`` (CLI, port 7777).
Point iCode at it with ``AIXCODING_ENVIRONMENT=local``.

Layout::

    aixcoding/mock/
        auth/                 # this subpackage
            __init__.py      # this file
            server.py        # HTTP server, grant state machine, verify page
            README.md        # endpoints, modes, usage

Minimal wire-contract tests live in ``aixcoding/tests/mock/test_auth_server.py``
(device-code envelope, poll states, user-info quirks); end-to-end behaviour
tests exercise this mock through the real ``AuthClient`` and arrive with the
auth feature migration.
"""

from __future__ import annotations
