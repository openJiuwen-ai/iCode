# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

pytest.importorskip("chrys.service.extensions")

from chrys.service.extensions import bind_extension, discover_extensions, load_extensions

EXPECTED_TOOLS = [
    "browser_open",
    "browser_list",
    "browser_status",
    "browser_resume",
    "browser_navigate",
    "browser_tabs",
    "browser_tab_switch",
    "browser_tab_close",
    "browser_find",
    "browser_snapshot",
    "browser_click",
    "browser_hover",
    "browser_fill",
    "browser_press_key",
    "browser_select_option",
    "browser_wait_for",
    "browser_dialog",
    "browser_downloads",
    "browser_download_cancel",
    "browser_scroll",
    "browser_extract",
    "browser_logs",
    "browser_back",
    "browser_forward",
    "browser_reload",
    "browser_screenshot",
    "browser_close",
    "browser_upload",
    "browser_action_status",
]


def test_real_iCode_binding_exposes_nine_tools_and_hooks(tmp_path: Path) -> None:
    if "browser" not in discover_extensions().sources:
        pytest.skip("icode-browser-use is not installed as a distribution in this environment")

    loaded = asyncio.run(load_extensions(["browser"]))
    assert len(loaded) == 1
    assert loaded[0].extension is not None, loaded[0].problem

    build = SimpleNamespace(
        runtime=SimpleNamespace(cwd=tmp_path, working_dirs=()),
        agent_name="browser-test",
        session_id=None,
        session_dir=tmp_path / "session",
        agent_id=None,
    )
    bound = bind_extension("browser", loaded[0].extension, build)

    assert [tool.name for tool in bound.tools] == EXPECTED_TOOLS
    assert [context.declaration.name for context in bound.contexts] == ["browser_context"]
    assert [hook.declaration.name for hook in bound.before_tool_hooks] == ["browser_guard"]
    assert bound.after_tool_hooks == []
    assert bound.details.tools == EXPECTED_TOOLS
