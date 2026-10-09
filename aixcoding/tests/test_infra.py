# ruff: noqa: RUF002, RUF003, S101
"""src/chrys/aixcoding 基础设施测试（config / http / git_info / context / telemetry.types）。

M1 验收项"LOCAL profile 指向 mock 可对接"在 ``test_local_profile_end_to_end``
落地：起 mock → 环境变量指向 mock 端口 → http client 上报 → mock 落库核对。
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest
from server import start_telemetry_mock

from chrys.aixcoding import config, context, git_info
from chrys.aixcoding.http import BatchBuffer, TelemetryHttpClient
from chrys.aixcoding.telemetry import types as telemetry_types
from chrys.foundation.platform import get_platform

CSAS = "/csas/telemetry/api/v1"


@pytest.fixture(autouse=True)
def _fresh_settings() -> None:
    config.clear_settings_cache()
    context.clear_desktop_channel()
    git_info.clear_git_info_cache()


@pytest.fixture
def mock():
    instance = start_telemetry_mock(port=0, quiet=True)
    yield instance
    instance.close()


def _port(instance) -> int:
    return int(instance.origin.rsplit(":", 1)[1])


# -- config -------------------------------------------------------------------


def test_config_defaults_no_file_no_env() -> None:
    settings = config.load_settings(force=True)
    assert settings.profile == "PROD"
    assert settings.base_url == "http://22.189.54.139/csas"
    assert settings.report_base_url == "http://22.189.54.139/csas/telemetry/api/v1"
    assert settings.tool_param_mode == "whitelist"
    assert settings.telemetry_enabled is True
    assert settings.token is None


def test_config_local_profile_points_to_mock() -> None:
    assert config.PROFILE_BASE_URLS["LOCAL"] == "http://127.0.0.1:4321/csas"


def test_config_env_profile_and_base_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AIXCODING_EXTENSION_PROFILE", "dev")
    monkeypatch.setenv("AIXCODING_EXTENSION_BASE_URL", "http://10.0.0.9:9999/prefix")
    settings = config.load_settings(force=True)
    assert settings.profile == "DEV"
    assert settings.base_url == "http://10.0.0.9:9999/prefix"
    assert settings.report_base_url == "http://10.0.0.9:9999/prefix/telemetry/api/v1"


def test_config_yaml_overrides_and_host_level_custom_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AIXCODING_EXTENSION_PROFILE", raising=False)
    monkeypatch.delenv("AIXCODING_EXTENSION_BASE_URL", raising=False)
    document = get_platform().config_dir / "aixcoding.yaml"
    document.parent.mkdir(parents=True, exist_ok=True)
    document.write_text(
        "profile: LOCAL\n"
        "customServerUrl: http://127.0.0.1:5555\n"
        "token: yaml-token\n"
        "userId: '60001'\n"
        "toolParamMode: full\n"
        "telemetryEnabled: false\n",
        encoding="utf-8",
    )
    settings = config.load_settings(force=True)
    # customServerUrl 仅替换 host，保留 LOCAL 的 /csas 路径
    assert settings.base_url == "http://127.0.0.1:5555/csas"
    assert settings.token == "yaml-token"
    assert settings.user_id == "60001"
    assert settings.tool_param_mode == "full"
    assert settings.telemetry_enabled is False


def test_config_invalid_profile_falls_back_to_prod(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AIXCODING_EXTENSION_PROFILE", "moonbase")
    assert config.load_settings(force=True).profile == "PROD"


# -- http（端到端对接 mock，M1 验收） ------------------------------------------


async def test_local_profile_end_to_end(
    mock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AIXCODING_EXTENSION_PROFILE", "LOCAL")
    monkeypatch.setenv("AIXCODING_EXTENSION_BASE_URL", f"http://127.0.0.1:{_port(mock)}/csas")
    settings = config.load_settings(force=True)
    assert settings.report_base_url == f"http://127.0.0.1:{_port(mock)}/csas/telemetry/api/v1"

    client = TelemetryHttpClient(settings.report_base_url, token=None)
    try:
        client.submit(
            telemetry_types.TOOL_DETAIL_SAVE,
            {"productName": "iCode", "funcType": 3, "funcName": "read_file", "funcId": "f-1", "sessionId": "s-1"},
        )
        await client.wait_drained()
    finally:
        await client.stop()

    rows = mock.store.query("tool-detail/save")
    assert len(rows) == 1
    assert rows[0]["func_name"] == "read_file"


async def test_http_client_preserves_submit_order(mock) -> None:
    client = TelemetryHttpClient(f"http://127.0.0.1:{_port(mock)}/csas/telemetry/api/v1")
    try:
        for index in range(5):
            client.submit(
                telemetry_types.TOOL_DETAIL_SAVE,
                {
                    "productName": "iCode",
                    "funcType": 3,
                    "funcName": "read_file",
                    "funcId": f"f-{index}",
                    "sessionId": f"s-{index}",
                },
            )
        await client.wait_drained()
    finally:
        await client.stop()
    rows = mock.store.query("tool-detail/save", limit=10)
    assert [row["func_id"] for row in reversed(rows)] == [f"f-{index}" for index in range(5)]


async def test_http_client_swallows_unreachable_server() -> None:
    client = TelemetryHttpClient("http://127.0.0.1:1/csas/telemetry/api/v1", timeout=0.5)
    try:
        client.submit(telemetry_types.TOOL_DETAIL_SAVE, {"anything": True})
        await client.wait_drained()  # 不得抛出：上报失败只记日志
    finally:
        await client.stop()


async def test_batch_buffer_flush_and_cap(mock) -> None:
    client = TelemetryHttpClient(f"http://127.0.0.1:{_port(mock)}/csas/telemetry/api/v1")
    try:
        buffer = BatchBuffer(client, telemetry_types.AI_CODE_SAVE, flush_size=3, max_pending=10)
        for index in range(3):  # 满 flush_size 立即 flush
            buffer.add({"reportId": f"r-{index}", "sourceType": "edit", "blocks": []})
        await client.wait_drained()
        assert buffer.pending_count == 0
        assert len(mock.store.query("ai-code/save")) == 3

        capped = BatchBuffer(client, telemetry_types.AI_CODE_SAVE, flush_size=100, max_pending=4)
        for index in range(6):  # flush_size 不触发，靠 max_pending 丢旧
            capped.add({"reportId": f"b-{index}", "sourceType": "edit", "blocks": []})
        assert capped.pending_count == 4
        await capped.stop()  # stop 时 flush 余量
        await client.wait_drained()
    finally:
        await client.stop()
    total = len(mock.store.query("ai-code/save"))
    assert total == 3 + 4  # 首轮 3 条 + 丢旧后余 4 条


# -- git_info -------------------------------------------------------------------


def test_git_info_collects_five_fields(git_repo: Path) -> None:
    git = shutil.which("git")
    assert git is not None  # git_repo fixture 已验证过 git 可用
    env = {**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull}
    subprocess.run(  # noqa: S603
        [git, "remote", "add", "origin", "https://git.example.com/acme/widget.git"],
        cwd=git_repo,
        check=True,
        capture_output=True,
        stdin=subprocess.DEVNULL,
        env=env,
    )
    info = git_info.collect_git_info(git_repo, force=True)
    assert info is not None
    assert info.git_branch  # 默认分支名不定（master/main），只断言非空
    assert info.git_revision and len(info.git_revision) == 40
    assert info.git_remote == "https://git.example.com/acme/widget.git"
    assert info.git_owner == "acme"
    assert info.git_repo == "widget"


def test_git_info_no_repo_returns_none(tmp_path: Path) -> None:
    assert git_info.collect_git_info(tmp_path, force=True) is None


def test_git_info_parses_scp_like_url() -> None:
    assert git_info._parse_owner_repo("git@gitcode.com:team/project.git") == ("team", "project")
    assert git_info._parse_owner_repo("https://host.cn/a/b/c.git") == ("b", "c")
    assert git_info._parse_owner_repo("weird-url") == (None, None)


def test_git_info_cache_hits_within_ttl(tmp_path: Path) -> None:
    counter = {"count": 0}
    original = git_info._collect

    def counting_collect(cwd: Path):
        counter["count"] += 1
        return original(cwd)

    git_info._collect = counting_collect
    try:
        git_info.collect_git_info(tmp_path, ttl_seconds=60)
        git_info.collect_git_info(tmp_path, ttl_seconds=60)
        assert counter["count"] == 1
    finally:
        git_info._collect = original


# -- context / types -------------------------------------------------------------


def test_detect_channel_by_argv() -> None:
    assert context.detect_channel(["icode"]).channel_name == "icode-tui"
    assert context.detect_channel(["icode", "-s", "x"]).channel_name == "icode-tui"  # 选项跳过
    assert context.detect_channel(["icode", "run", "do it"]).channel_name == "icode-cli"
    assert context.detect_channel(["icode", "acp"]).channel_name == "icode-acp"
    assert context.detect_channel(["icode", "serve"]).channel_name == "icode-tui"
    assert context.detect_channel(["icode"]).channel_type == "cli"


def test_desktop_channel_override() -> None:
    context.set_desktop_channel("Agent Studio", "1.2.3")
    channel = context.current_channel()
    assert channel.channel_type == "desktop"
    assert channel.channel_name == "Agent Studio"
    assert channel.channel_version == "1.2.3"
    context.clear_desktop_channel()
    assert context.current_channel().channel_type == "cli"


def test_user_id_provider_reads_config() -> None:
    document = get_platform().config_dir / "aixcoding.yaml"
    document.parent.mkdir(parents=True, exist_ok=True)
    document.write_text("userId: '60002'\n", encoding="utf-8")
    provider = context.ConfigUserIdProvider()
    assert provider() == "60002"


def test_telemetry_type_constants_match_contract() -> None:
    assert telemetry_types.TOOL_DETAIL_SAVE == "tool-detail/save"
    assert telemetry_types.TOOL_DETAIL_UPDATE == "tool-detail/update"
    assert telemetry_types.AI_CODE_SAVE == "ai-code/save"
    assert int(telemetry_types.FuncType.SKILL) == 0
    assert int(telemetry_types.FuncType.MCP) == 1
    assert int(telemetry_types.FuncType.BUILTIN) == 3
    assert int(telemetry_types.CodeStatus.USER_APPROVED) == 5
    assert int(telemetry_types.CodeStatus.PENDING) == 3
    assert telemetry_types.FailureType.TIMEOUT == "timeout"
