# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""AIxCoding TUI update-detection mock server (local debug tool, never shipped).

Simulates ``GET /csas/api/v2/releases/check`` — the release-check contract the
TUI update-detection feature will call at startup. The real backend does not
exist yet; this mock freezes the wire contract so client work and manual runs
can proceed: strict query validation
(unknown/missing/invalid → ``400 invalid_app_release_query``), v2 manifest
answers with ``ETag``/``304`` support, ``204`` for "no applicable release",
plus fault modes (``error`` / ``garbage`` / slow) covering the client's
silent-failure paths.

Public API lives in :mod:`aixcoding.mock.update.server`; the canonical
entry points are :func:`aixcoding.mock.update.server.create_server`
(tests, ephemeral port) and ``python aixcoding/mock/update/server.py``
(defaults: loopback :7778; ``--port`` overrides it, every other knob is
steered at runtime via ``POST /debug/config``). The current product has no
internal update consumer; product wiring belongs to the future update batch.

Layout::

    aixcoding/mock/
        update/               # this subpackage
            __init__.py      # this file
            server.py        # HTTP server, strict validation, modes, debug endpoints
            README.md        # endpoints, modes, /debug/config usage

    aixcoding/tests/mock/
        test_update_server.py   # contract tests (stdlib urllib, no fixtures needed)

The TUI client and its end-to-end tests migrate with the update feature;
until then, the contract tests in ``aixcoding/tests/mock/`` guard this mock.
"""

from __future__ import annotations
