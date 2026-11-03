# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Workflow nodes omit the main agent's always-on documentation guidance."""

from __future__ import annotations

from pathlib import Path

import pytest

from chrys.service.llm.mock import MockChatClient, MockResponse
from tests.orchestration.workflows._hosting import (
    PROFILE,
    confirm,
    make_host,
    make_profile,
    make_project,
    patch_runtime,
    run,
    write_workflow,
)


async def test_workflow_node_does_not_inject_product_documentation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "index.yaml").write_text("locales: [en]\ntopics: []\n", encoding="utf-8")
    monkeypatch.setenv("CHRYS_DOCS_ROOT", str(docs))
    project = make_project(tmp_path)
    client = MockChatClient(responses=[MockResponse(text="reviewed")])
    patch_runtime(monkeypatch, [MockChatClient(responses=[]), client], builtin_tools=True)
    write_workflow(
        project,
        "review",
        (
            "from chrys.workflows import WorkflowBuilder\n"
            "wf = WorkflowBuilder('review')\n"
            f"review = wf.agent('review', profile={PROFILE!r})\n"
            "wf.start(review)\nwf.output(review)\nworkflow = wf.build()\n"
        ).encode(),
    )
    host = make_host(tmp_path, project=project, profiles=[make_profile(builtins=["filesystem.read"])])
    try:
        await confirm(host, "review")
        result, _stream = await run(host, "review", input_text="Review the project.")
        assert result.outcome.value == "completed"
        assert len(client.call_history) == 1
        _messages, options = client.call_history[0]
        # AIxCoding branding: prompt documentation title absent
        assert "AIxCoding documentation" not in options["instructions"]
        assert "Topic index:" not in options["instructions"]
        assert "read_file" in {tool.name for tool in options["tools"]}
    finally:
        await host.shutdown()
