# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""browser-use: a stdlib-only browser-automation plugin for iCode.

The package implements an RFC6455 WebSocket client and the Chrome DevTools
Protocol client from the standard library alone, exposed as a
``chrys.extensions`` plugin. It needs no third-party dependency and no driver
download; the host extension API is the only iCode integration.
"""

from __future__ import annotations

__all__ = ["__version__"]

__version__ = "0.3.3"
