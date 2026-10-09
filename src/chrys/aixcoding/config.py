# ruff: noqa: RUF002, RUF003
"""平台连接配置（独立于 iCode 主设置，读 ``<config_dir>/aixcoding.yaml`` + 环境变量）。

刻意独立读取（方案 §六取舍）：不污染上游设置文档与 ENV 层声明；将来登录改造
（control-plane）在同一配置上挂接 token 来源。优先级：环境变量 > yaml > 默认。

- profile 判定：yaml ``profile`` → ``AIXCODING_EXTENSION_PROFILE`` → ``PROD``
  （与 aixcoding-continue 的 configUtils 语义一致，缓存首结果）；
- ``AIXCODING_EXTENSION_BASE_URL``：完整替换 base（对齐 aixcoding 同名环境变量）；
- yaml ``customServerUrl``：仅替换 scheme+host(+port)，保留 profile 路径
  （对齐 aixcoding ``applyCustomServerUrl``）。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import yaml

from chrys.foundation.platform import get_platform

PROFILES = ("LOCAL", "DEV", "PROD")

# base 含路径前缀（对齐 aixcoding baseUrl 语义）；report 基址 = base + REPORT_URL_PATH。
# LOCAL 指向 telemetry-mock（仓库根 aixcoding/telemetry-mock/，默认 127.0.0.1:4321，
# 端点带 /csas 前缀）。
PROFILE_BASE_URLS: dict[str, str] = {
    "LOCAL": "http://127.0.0.1:4321/csas",
    "DEV": "http://81.89.182.150/csas",
    "PROD": "http://22.189.54.139/csas",
}
REPORT_URL_PATH = "telemetry/api/v1"

PROFILE_ENV = "AIXCODING_EXTENSION_PROFILE"
BASE_URL_ENV = "AIXCODING_EXTENSION_BASE_URL"
TOKEN_ENV = "AIXCODING_TOKEN"
TELEMETRY_DISABLED_ENV = "AIXCODING_TELEMETRY_DISABLED"

TOOL_PARAM_MODES = ("whitelist", "full")

_CONFIG_FILE_NAME = "aixcoding.yaml"

_TRUTHY = ("1", "true", "yes")
_FALSY = ("0", "false", "no")


@dataclass(frozen=True)
class AixcodingSettings:
    """进程内共享的平台连接配置快照。"""

    profile: str
    base_url: str
    token: str | None
    user_id: str | None
    tool_param_mode: str
    telemetry_enabled: bool

    @property
    def report_base_url(self) -> str:
        """csas telemetry 上报基址（含 ``/telemetry/api/v1``）。"""
        return f"{self.base_url.rstrip('/')}/{REPORT_URL_PATH}"


_settings_cache: AixcodingSettings | None = None


def clear_settings_cache() -> None:
    """清缓存（测试辅助；下次 ``load_settings`` 重新读取磁盘与环境）。"""
    global _settings_cache
    _settings_cache = None


def load_settings(*, force: bool = False) -> AixcodingSettings:
    global _settings_cache
    if _settings_cache is not None and not force:
        return _settings_cache

    document = get_platform().config_dir / _CONFIG_FILE_NAME
    data = _read_yaml(document)

    profile = _normalize_profile(os.environ.get(PROFILE_ENV) or data.get("profile"))
    base_url = os.environ.get(BASE_URL_ENV) or PROFILE_BASE_URLS[profile]
    if not os.environ.get(BASE_URL_ENV):
        custom = str(data.get("customServerUrl") or "").strip()
        if custom:
            base_url = _apply_host_override(base_url, custom)

    token = os.environ.get(TOKEN_ENV) or _optional_str(data.get("token"))
    user_id = _optional_str(data.get("userId"))

    tool_param_mode = str(data.get("toolParamMode") or TOOL_PARAM_MODES[0]).strip().lower()
    if tool_param_mode not in TOOL_PARAM_MODES:
        tool_param_mode = TOOL_PARAM_MODES[0]

    telemetry_enabled = str(data.get("telemetryEnabled", True)).strip().lower() not in _FALSY
    if os.environ.get(TELEMETRY_DISABLED_ENV, "").strip().lower() in _TRUTHY:
        telemetry_enabled = False

    _settings_cache = AixcodingSettings(
        profile=profile,
        base_url=base_url,
        token=token,
        user_id=user_id,
        tool_param_mode=tool_param_mode,
        telemetry_enabled=telemetry_enabled,
    )
    return _settings_cache


def _optional_str(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _read_yaml(document: Path) -> dict:
    if not document.is_file():
        return {}
    try:
        loaded = yaml.safe_load(document.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        return {}
    return loaded if isinstance(loaded, dict) else {}


def _normalize_profile(value: object) -> str:
    profile = str(value or "").strip().upper()
    return profile if profile in PROFILES else "PROD"


def _apply_host_override(base_url: str, custom_server_url: str) -> str:
    """``customServerUrl`` 只替换 scheme+host(+port)，保留原路径。"""
    replacement = urlsplit(custom_server_url)
    if not replacement.scheme or not replacement.netloc:
        return base_url
    original = urlsplit(base_url)
    return urlunsplit((replacement.scheme, replacement.netloc, original.path, original.query, original.fragment))
