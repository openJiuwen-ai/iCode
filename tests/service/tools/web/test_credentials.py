# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Search secrets come only from the process frozen at bootstrap."""

from __future__ import annotations

from collections import UserDict

import pytest

from chrys.foundation.config.process_env import ProcessEnvSnapshot, freeze_process_env
from chrys.foundation.platform import get_platform
from chrys.service.tools.builtins.web import credentials
from chrys.service.tools.builtins.web.credentials import resolve_search_credentials


def test_credentials_use_the_frozen_process_not_live_env(monkeypatch):
    monkeypatch.setenv("Web_Test_Key", "frozen-secret")
    freeze_process_env()
    monkeypatch.setenv("Web_Test_Key", "changed-secret")

    resolved = resolve_search_credentials({"Web_Test_Key"})

    assert resolved.values == {"Web_Test_Key": "frozen-secret"}
    assert "frozen-secret" not in repr(resolved)


def test_only_requested_credentials_are_read(monkeypatch):
    reads: list[str] = []

    class RecordingEnv(UserDict[str, str]):
        def __getitem__(self, name: str) -> str:
            reads.append(name)
            return super().__getitem__(name)

    snapshot = ProcessEnvSnapshot(RecordingEnv({"SEARCH_KEY": "wanted", "UNRELATED_KEY": "other"}))
    monkeypatch.setattr(credentials, "process_env_snapshot", lambda: snapshot)

    assert resolve_search_credentials({"SEARCH_KEY"}).values == {"SEARCH_KEY": "wanted"}
    assert reads == ["SEARCH_KEY"]


@pytest.mark.parametrize("exported", [None, ""])
def test_missing_credentials_do_not_fall_back_to_dotenv(tmp_path, monkeypatch, exported):
    if exported is None:
        monkeypatch.delenv("WEB_TEST_KEY", raising=False)
    else:
        monkeypatch.setenv("WEB_TEST_KEY", exported)
    freeze_process_env()
    monkeypatch.setenv("WEB_TEST_KEY", "late-secret")
    monkeypatch.chdir(tmp_path)
    user_path = get_platform().config_dir / ".env"
    user_path.parent.mkdir(parents=True, exist_ok=True)
    for path in (tmp_path / ".env", user_path):
        path.write_text("WEB_TEST_KEY=file-secret\n", encoding="utf-8")

    with pytest.raises(ValueError, match="Missing web search credential WEB_TEST_KEY"):
        resolve_search_credentials({"WEB_TEST_KEY"})
