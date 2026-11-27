# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Browser session commands retain explicit sandbox startup choices."""

from __future__ import annotations

import pytest

from chrys.app.cli import serve as serve_cli
from tests.app.cli._serve_helpers import FakeServer


@pytest.mark.parametrize("flag", ["--sandbox", "--no-sandbox"])
def test_real_serve_command_carries_sandbox_policy_to_the_session(monkeypatch, flag):
    from chrys.app.cli import app
    from chrys.foundation import guard

    FakeServer.instances.clear()
    monkeypatch.setattr(serve_cli, "_load_server_class", lambda: FakeServer)
    monkeypatch.setattr(serve_cli, "_entrypoint_argv", lambda: ["icode"])
    argv = app._enable_sandbox([flag, "serve", "--sandbox-on-unavailable=warn"])
    serve_cli.run_command(serve_cli.build_parser().parse_args(argv[1:]))
    assert FakeServer.instances[0].command == serve_cli._shell_join(
        ["icode", "__tui_subprocess__", *guard.startup_flags(flag == "--sandbox", "warn")]
    )
