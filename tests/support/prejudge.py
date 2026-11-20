# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Require the real grammar wheels for parser-dependent tests in CI."""

from __future__ import annotations

import importlib
import os

import pytest


def require_grammars(*modules: str) -> None:
    for name in modules:
        if os.environ.get("CI"):
            importlib.import_module(name)
        else:
            pytest.importorskip(name)
