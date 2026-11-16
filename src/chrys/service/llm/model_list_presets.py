# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Evidence-backed model-list locations on the configured origin."""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlsplit


@dataclass(frozen=True, slots=True)
class ModelListPreset:
    """An origin/path match and an optional model-list wire format override."""

    host: str
    scheme: str = "https"
    port: int = 443
    path_prefix: str = ""
    list_path: str = ""
    query: str = ""
    list_field: str | None = None
    id_field: str | None = None
    evidence: str = ""


PRESETS: tuple[ModelListPreset, ...] = (
    ModelListPreset(
        host="api.deepseek.com",
        path_prefix="/anthropic",
        list_path="/models",
        evidence="Key-verified 2026-11-09: /anthropic listing 404, /models and conversation 200.",
    ),
    *(
        ModelListPreset(
            host=host,
            path_prefix="/anthropic",
            list_path="/v1/models",
            evidence=(
                "Official documentation + unauthenticated path confirmation (2026-11-09); "
                "not Key-verified: https://platform.kimi.ai/docs/api/list-models "
                "and https://platform.kimi.com/docs/api/list-models."
            ),
        )
        for host in ("api.moonshot.cn", "api.moonshot.ai")
    ),
    *(
        ModelListPreset(
            host=host,
            path_prefix="/apps/anthropic",
            list_path="/compatible-mode/v1/models",
            evidence=(
                "Open-source usage + unauthenticated path confirmation (2026-11-09); "
                "not Key-verified: LobeHub packages/model-runtime/src/providers/qwen/index.ts "
                "uses client.models.list() at /compatible-mode/v1; regional origin documented at "
                "https://www.alibabacloud.com/help/en/model-studio/singapore-regional-access-information."
            ),
        )
        for host in ("dashscope.aliyuncs.com", "dashscope-intl.aliyuncs.com")
    ),
    *(
        ModelListPreset(
            host=host,
            query="type=text&sub_type=chat",
            evidence=(
                "Official documentation + unauthenticated path confirmation (2026-11-09); "
                "filter not Key-verified: https://api-docs.siliconflow.cn/docs/api/models-get "
                "documents type=text and sub_type=chat."
            ),
        )
        for host in ("api.siliconflow.cn", "api.siliconflow.com")
    ),
    ModelListPreset(
        host="open.bigmodel.cn",
        list_path="/api/fine-tuning/model_center/list",
        query="pageSize=100&pageNum=1",
        list_field="rows",
        id_field="modelCode",
        evidence=(
            "Open-source usage + unauthenticated path confirmation (2026-11-09); not Key-verified: "
            "LobeHub packages/model-runtime/src/providers/zhipu/index.ts uses Bearer authentication. "
            "Undocumented console API; its platform-wide catalog may include non-text models."
        ),
    ),
)


def match_preset(base_url: str) -> ModelListPreset | None:
    """Match the exact origin and longest whole-segment path prefix."""
    try:
        url = urlsplit(base_url)
        port = url.port if url.port is not None else {"https": 443, "http": 80}.get(url.scheme)
        matches = [
            preset
            for preset in PRESETS
            if (url.scheme, url.hostname, port) == (preset.scheme, preset.host, preset.port)
            and (
                not preset.path_prefix
                or url.path == preset.path_prefix
                or url.path.startswith(preset.path_prefix.rstrip("/") + "/")
            )
        ]
        return max(matches, key=lambda preset: len(preset.path_prefix)) if matches else None
    except ValueError:
        return None
