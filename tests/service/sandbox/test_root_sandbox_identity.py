# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Root with user namespaces off keeps the host uid.

The identity helpers are called directly. Nothing starts bubblewrap or
calls setuid.
"""

from __future__ import annotations

import sys
import types

import pytest

_NOBODY_UID = 65534
_NAMED_UID = 4242
_NAMED_GID = 4243


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


def _policy(*, user_namespace: bool) -> types.SimpleNamespace:
    return types.SimpleNamespace(
        namespace=types.SimpleNamespace(user=user_namespace),
        process=types.SimpleNamespace(run_as_user="sandbox", run_as_group="sandbox"),
    )


def _stub_named_account(monkeypatch: pytest.MonkeyPatch) -> None:
    from jiuwenbox.server.runtime import process as process_module

    account = types.SimpleNamespace(pw_uid=_NAMED_UID)
    group = types.SimpleNamespace(gr_gid=_NAMED_GID)
    monkeypatch.setattr(process_module.pwd, "getpwnam", lambda _name: account)
    monkeypatch.setattr(process_module.grp, "getgrnam", lambda _name: group)


def test_root_without_user_namespace_keeps_host_uid(
    monkeypatch: pytest.MonkeyPatch,
    jiuwenbox_importable: None,
) -> None:
    import os

    from jiuwenbox.server.runtime.process import ProcessRuntime

    monkeypatch.setattr(os, "geteuid", lambda: 0, raising=False)
    monkeypatch.setattr(os, "getegid", lambda: 0, raising=False)
    _stub_named_account(monkeypatch)
    policy = _policy(user_namespace=False)

    uid = ProcessRuntime._resolve_process_uid(policy)
    backing_uid, backing_gid = ProcessRuntime._resolve_backing_identity(policy)

    assert uid == 0
    assert uid != _NOBODY_UID
    assert backing_uid == 0
    assert backing_gid == 0


def test_non_root_keeps_named_account_resolution(
    monkeypatch: pytest.MonkeyPatch,
    jiuwenbox_importable: None,
) -> None:
    import os

    from jiuwenbox.server.runtime.process import ProcessRuntime

    monkeypatch.setattr(os, "geteuid", lambda: 1000, raising=False)
    _stub_named_account(monkeypatch)
    policy = _policy(user_namespace=False)

    assert ProcessRuntime._resolve_process_uid(policy) == _NAMED_UID
    assert ProcessRuntime._resolve_backing_identity(policy) == (_NAMED_UID, _NAMED_GID)
