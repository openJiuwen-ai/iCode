# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""CORS registration on the vendored sandbox server.

``create_app`` installs one ``CORSMiddleware``. A wildcard origin stays
uncredentialed. The check reads ``user_middleware`` and does not enter the
FastAPI lifespan or start bubblewrap.
"""

from __future__ import annotations

import sys
import types

import pytest
from fastapi.middleware.cors import CORSMiddleware


@pytest.fixture
def jiuwenbox_importable(monkeypatch: pytest.MonkeyPatch) -> None:
    """Let the vendored server import on Windows.

    ``pwd`` and ``grp`` are imported at module scope. The stubs only exist
    so those imports succeed; account lookup is patched per test.
    """
    import os

    if not hasattr(os, "geteuid"):
        monkeypatch.setattr(os, "geteuid", lambda: 0, raising=False)

    def _missing(*_args: object) -> None:
        raise KeyError(_args)

    def _install(name: str) -> None:
        if name in sys.modules:
            return
        try:
            __import__(name)
        except ModuleNotFoundError:
            module = types.ModuleType(name)
            if name == "pwd":
                module.getpwuid = _missing  # type: ignore[attr-defined]
                module.getpwnam = _missing  # type: ignore[attr-defined]
            else:
                module.getgrnam = _missing  # type: ignore[attr-defined]
            monkeypatch.setitem(sys.modules, name, module)

    _install("pwd")
    _install("grp")


def test_cors_middleware_is_a_non_credentialed_wildcard(jiuwenbox_importable: None) -> None:
    """The registered CORS middleware allows any origin and sends no credentials."""
    from jiuwenbox.server.app import create_app

    application = create_app()
    cors = [entry for entry in application.user_middleware if entry.cls is CORSMiddleware]
    assert len(cors) == 1
    assert cors[0].kwargs["allow_credentials"] is False
    assert cors[0].kwargs["allow_origins"] == ["*"]
