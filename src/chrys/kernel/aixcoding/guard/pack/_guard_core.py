# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""iCode 安全拦截引擎 —— 提示词层与工具调用前两个 Hook 的唯一公共依赖。

设计约束（决定了这里的写法）：

* Hook 以子进程方式运行，标准输入是 ``DEVNULL``，因此**无法**在 Hook 内做
  交互式确认。所有"处置"只能表达成 ``action: block`` + ``reason``，由 iCode
  把 ``reason`` 推给用户（提示词层走 ``Error(code="hook_blocked")``，工具层
  走被拒绝的工具调用）。这也是 ``settings.confirm_disposition`` 默认取
  ``block`` 的原因：表里的 ``confirm`` 一律先按"打断 + 提示"落地。
* Hook 只看得到**入参**，看不到工具返回值，所以输出侧的扫描不在本层。
* 与主程序零耦合：本模块只用标准库，不 import 任何 ``chrys`` 包；规则与词典
  全部外置在 ``rules.json``，改词典不需要改代码。

对外接口只有四个：:func:`load_config`、:func:`evaluate_prompt`、
:func:`evaluate_tool_call`、:func:`run_hook`。
"""

from __future__ import annotations

import ipaddress
import json
import os
import posixpath
import re
import sys
import tempfile
import time
from fnmatch import fnmatch
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

RULES_FILE_NAME = "rules.json"
"""与本脚本同目录的默认规则文件。"""

RULES_PATH_ENV = "AIXCODING_GUARD_RULES"
"""可用该环境变量指向另一份 ``rules.json``（测试或多套策略切换用）。"""

AUDIT_PATH_ENV = "AIXCODING_GUARD_AUDIT"
"""可用该环境变量覆盖留痕文件路径。"""

STATE_FILE_NAME = "guard_state.json"
"""开关状态文件，与 ``rules.json`` 同目录。

总开关和单条规则的开关都写在这里，**不写进** ``rules.json``：``rules.json``
是随包分发的规则文件，每次启动 iCode 都会被重新安装覆盖（见
``chrys.kernel.aixcoding.guard.guard_install``），写在它里面的开关下一次启动
就没了。状态文件不在安装 payload 列表里，因此升级、重新安装都不会动它。
"""

STATE_PATH_ENV = "AIXCODING_GUARD_STATE"
"""可用该环境变量指向另一份状态文件（测试或多套策略切换用）。"""

MAX_SNIPPET_CHARS = 80
"""命中片段在用户提示里最多展示的字符数。"""

MAX_RULE_REFS_DEPTH = 4
"""``rule_refs`` 递归上限，防止规则互相引用形成环。"""

_ASCII_TOKEN = re.compile(r"[A-Za-z0-9 ._+:/\\@-]+")
"""纯 ASCII 的词典条目按词边界匹配，避免 ``IDA`` 命中 ``validation``。"""

_IPV4_IN_TEXT = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")

_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")

_PATH_ARG_HINTS = ("path", "file", "dir", "target")
_URL_ARG_HINTS = ("url", "uri", "endpoint", "host", "address", "link")


class GuardError(RuntimeError):
    """引擎自身无法继续（配置损坏、正则非法等）时抛出。"""


class DeadlineExceeded(RuntimeError):
    """单次评估超过 ``settings.self_timeout_seconds`` 时抛出。"""


_VALID_DISPOSITIONS = frozenset({"block", "confirm", "notify"})
_SUPPORTED_DETECTORS = frozenset(
    {
        "keyword",
        "path",
        "regex",
        "secret",
        "ip_cidr",
        "extension",
        "write_scope",
        "shell_write_target",
        "url",
        "mcp_server",
        "mcp_args_schema",
        "caller_context",
        "rule_refs",
    }
)


def _as_mapping(value: Any) -> dict[str, Any]:
    """Return *value* as a mapping, or an empty mapping for malformed payloads."""
    return value if isinstance(value, dict) else {}


def _optional_mapping(value: Any, label: str) -> dict[str, Any]:
    """Return a mapping or fail closed when configuration uses the wrong shape."""
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise GuardError(f"{label} 必须是对象")
    return value


def _optional_list(value: Any, label: str) -> list[Any]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise GuardError(f"{label} 必须是数组")
    return value


def _validate_string_list(value: Any, label: str) -> None:
    if value is not None and (not isinstance(value, list) or not all(isinstance(item, str) for item in value)):
        raise GuardError(f"{label} 必须是字符串数组")


def _validate_rule(rule: Any, label: str) -> dict[str, Any]:
    """Reject malformed rules before any evaluation can silently skip them."""
    if not isinstance(rule, dict):
        raise GuardError(f"{label} 必须是对象")
    rule_id = rule.get("id")
    if not isinstance(rule_id, str) or not rule_id.strip():
        raise GuardError(f"{label} 缺少非空字符串 id")
    disposition = rule.get("disposition", "confirm")
    if not isinstance(disposition, str) or disposition.lower() not in _VALID_DISPOSITIONS:
        raise GuardError(f"规则 {rule_id} 的 disposition 非法：{disposition!r}")
    for key in ("enabled", "audit"):
        if key in rule and not isinstance(rule[key], bool):
            raise GuardError(f"规则 {rule_id} 的 {key} 必须是布尔值")
    applies_to = rule.get("applies_to")
    if applies_to is not None:
        if not isinstance(applies_to, dict):
            raise GuardError(f"规则 {rule_id} 的 applies_to 必须是对象")
        for key in ("kinds", "arg_keys", "tool_names"):
            _validate_string_list(applies_to.get(key), f"规则 {rule_id} 的 applies_to.{key}")
    detectors = rule.get("detect", [])
    if not isinstance(detectors, list):
        raise GuardError(f"规则 {rule_id} 的 detect 必须是数组")
    for index, detector in enumerate(detectors):
        if not isinstance(detector, dict):
            raise GuardError(f"规则 {rule_id} 的 detect[{index}] 必须是对象")
        detector_type = detector.get("type")
        if not isinstance(detector_type, str) or detector_type not in _SUPPORTED_DETECTORS:
            raise GuardError(f"规则 {rule_id} 的检测器类型非法：{detector_type!r}")
        if detector_type == "rule_refs":
            _validate_string_list(detector.get("rules"), f"规则 {rule_id} 的 rule_refs.rules")
    return rule


def _validated_rules(value: Any, label: str) -> list[dict[str, Any]]:
    rules = [_validate_rule(rule, f"{label}[{index}]") for index, rule in enumerate(_optional_list(value, label))]
    return rules


def _validate_settings(value: Any) -> dict[str, Any]:
    settings = _optional_mapping(value, "settings")
    confirm_disposition = settings.get("confirm_disposition")
    if confirm_disposition is not None and str(confirm_disposition) not in {"block", "notify"}:
        raise GuardError(f"settings.confirm_disposition 非法：{confirm_disposition!r}")
    for key in ("fail_closed", "audit_on_allow"):
        if key in settings and not isinstance(settings[key], bool):
            raise GuardError(f"settings.{key} 必须是布尔值")
    for key in ("network", "mcp", "write_scope"):
        nested = settings.get(key)
        if nested is not None and not isinstance(nested, dict):
            raise GuardError(f"settings.{key} 必须是对象")
    network = settings.get("network") or {}
    if network.get("default_disposition") is not None:
        default_disposition = str(network["default_disposition"])
        if default_disposition not in _VALID_DISPOSITIONS:
            raise GuardError(f"settings.network.default_disposition 非法：{default_disposition!r}")
    mcp = settings.get("mcp") or {}
    for key in ("allowed_servers", "allowed_profiles", "allowed_workspaces"):
        _validate_string_list(mcp.get(key), f"settings.mcp.{key}")
    return settings


def _validate_dictionaries(value: Any) -> dict[str, Any]:
    dictionaries = _optional_mapping(value, "dictionaries")
    for key, entries in dictionaries.items():
        if not isinstance(key, str) or not isinstance(entries, list):
            raise GuardError("dictionaries 的每个条目必须是以字符串命名的数组")
    return dictionaries


def _state_flag(label: str, value: Any) -> bool | None:
    """把状态文件里的开关值解析成布尔；非布尔值记告警并返回 ``None``。

    状态文件是现场手改的，``"false"``（字符串）在 ``bool()`` 下为真，直接取真值
    会把"关掉护栏"变成"护栏照旧"，属于静默反向。这里只认真布尔。
    """
    if isinstance(value, bool):
        return value
    _log(f"开关状态值必须是布尔值，已忽略：{label}={value!r}")
    return None


# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------


class GuardConfig:
    """``rules.json`` 的内存视图。"""

    __slots__ = ("dictionaries", "path", "prompt_rules", "settings", "source", "state", "state_path", "tool_rules")

    def __init__(
        self,
        data: dict[str, Any],
        *,
        path: Path,
        state: dict[str, Any] | None = None,
        state_path: Path | None = None,
    ) -> None:
        self.source = data
        self.path = path
        self.settings = _validate_settings(data.get("settings"))
        self.dictionaries = _validate_dictionaries(data.get("dictionaries"))
        self.prompt_rules = _validated_rules(data.get("prompt_rules"), "prompt_rules")
        self.tool_rules = _validated_rules(data.get("tool_rules"), "tool_rules")
        rule_ids = [str(rule["id"]) for rule in (*self.prompt_rules, *self.tool_rules)]
        if len(rule_ids) != len(set(rule_ids)):
            raise GuardError("规则 id 必须全局唯一")
        self.state = dict(state or {})
        self.state_path = state_path

    # -- 设置读取 ---------------------------------------------------------

    def setting(self, name: str, default: Any = None) -> Any:
        value = self.settings.get(name)
        return default if value is None else value

    def sub_setting(self, name: str, default: Any = None) -> dict[str, Any]:
        value = self.settings.get(name)
        return dict(value) if isinstance(value, dict) else dict(default or {})

    # -- 词典读取 ---------------------------------------------------------

    def dictionary(self, name: str) -> list[Any]:
        entries = self.dictionaries.get(name)
        return list(entries) if isinstance(entries, list) else []

    def rule(self, rule_id: str) -> dict[str, Any] | None:
        for candidate in (*self.tool_rules, *self.prompt_rules):
            if candidate.get("id") == rule_id:
                return candidate
        return None

    @property
    def max_scan_chars(self) -> int:
        try:
            return max(1024, int(self.setting("max_scan_chars", 262144)))
        except (TypeError, ValueError):
            return 262144

    @property
    def self_timeout_seconds(self) -> float:
        try:
            return max(0.2, float(self.setting("self_timeout_seconds", 4.0)))
        except (TypeError, ValueError):
            return 4.0

    @property
    def fail_closed(self) -> bool:
        return bool(self.setting("fail_closed", True))

    @property
    def master_enabled(self) -> bool:
        """总开关：状态文件优先，其次 ``settings.enabled``，都没有时默认开。

        状态文件里显式写了 ``enabled`` 就以它为准；没写时沿用规则文件里的
        ``settings.enabled``（出厂为 ``true``），再没有就是开。
        """
        if "enabled" in self.state:
            override = _state_flag("enabled", self.state["enabled"])
            if override is not None:
                return override
        return bool(self.setting("enabled", True))

    def rule_override(self, rule_id: str) -> bool | None:
        """状态文件对某条规则的开关覆盖；没有（或非法）覆盖返回 ``None``。"""
        overrides = self.state.get("rules")
        if not isinstance(overrides, dict) or rule_id not in overrides:
            return None
        value = overrides[rule_id]
        if isinstance(value, dict):
            value = value.get("enabled")
        if value is None:
            return None
        return _state_flag(f"rules.{rule_id}", value)

    @property
    def write_scope_restricted(self) -> bool:
        """是否限制写入工作区内（状态文件可覆盖出厂值）。

        出厂默认**不限制**：``settings.write_scope.allow_outside_workspace`` 为
        ``true`` 时，写到工作区以外的路径照常放行（``T-FS-01`` 与 ``T-SHELL-05``
        因此不触发）。现场用状态文件把它改回限制——状态文件优先于出厂值，
        与规则开关同一套覆盖逻辑。
        """
        scope = self.sub_setting("write_scope", {})
        return not bool(scope.get("allow_outside_workspace", True))


def config_home() -> Path:
    """推断 ``<config_dir>``（通常是 ``~/.chrys``）。

    运行期环境变量里没有现成的值，但 payload 文件一定落在
    ``<config_dir>/hooks/tmp/`` 下，据此回溯三层即可；推断失败时退回
    ``~/.chrys``。
    """
    payload = os.environ.get("CHRYS_HOOK_PAYLOAD_FILE")
    if payload:
        try:
            resolved = Path(payload).resolve()
            if resolved.parent.name == "tmp":
                return resolved.parent.parent.parent
        except OSError:
            pass
    return Path.home() / ".chrys"


def expand(value: str) -> str:
    """展开 ``${chrys_home}``（iCode 在 ``run.env`` 里支持的同名模板）。"""
    return value.replace("${chrys_home}", str(config_home()))


def rules_file() -> Path:
    """返回本次要加载的 ``rules.json`` 路径。"""
    override = os.environ.get(RULES_PATH_ENV)
    if override:
        return Path(override)
    return Path(__file__).resolve().parent / RULES_FILE_NAME


def state_file(rules_path: Path) -> Path:
    """返回本次要加载的状态文件路径（默认与规则文件同目录）。"""
    override = os.environ.get(STATE_PATH_ENV)
    if override:
        return Path(override)
    return rules_path.parent / STATE_FILE_NAME


def load_state(path: Path) -> dict[str, Any]:
    """读取开关状态文件。

    文件不存在 = 没改过开关 = 出厂默认（总开关开，规则按 ``rules.json``）。
    文件损坏时按出厂默认处理并告警：宁可多拦一次，也不要因为一个坏文件把
    护栏整个静默关掉。
    """
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        _log(f"开关状态文件不可读，按出厂默认处理（{path}）：{exc}")
        return {}
    if not isinstance(data, dict):
        _log(f"开关状态文件顶层必须是对象，按出厂默认处理（{path}）")
        return {}
    return data


def apply_state(config: GuardConfig) -> None:
    """把状态文件里的开关覆盖写进配置。

    覆盖优先于 ``rules.json``：状态文件是现场改的开关，规则文件是随包分发的
    出厂状态。覆盖分两类：

    * ``rules``——单条规则的 ``enabled``；
    * ``write_scope``——写入范围设置，按键合并，不影响同级的其他键。

    覆盖值一律做类型校验（见 :func:`_state_flag`），非法值忽略并告警——
    不因为一处手改错误把整个开关方向反转。
    """
    scope = config.state.get("write_scope")
    if isinstance(scope, dict):
        merged = dict(config.settings.get("write_scope") or {})
        for key, value in scope.items():
            if key == "allow_outside_workspace":
                flag = _state_flag(f"write_scope.{key}", value)
                if flag is not None:
                    merged[key] = flag
            elif key in {"temp_prefixes", "readonly_globs"} and isinstance(value, list):
                merged[key] = value
            else:
                _log(f"写入范围覆盖项非法，已忽略：{key}={value!r}")
        config.settings["write_scope"] = merged
    overrides = config.state.get("rules")
    if not isinstance(overrides, dict):
        if overrides is not None:
            _log(f"开关状态文件的 rules 必须是对象，已忽略：{type(overrides).__name__}")
        return
    for rule in (*config.prompt_rules, *config.tool_rules):
        override = config.rule_override(str(rule.get("id") or ""))
        if override is not None:
            rule["enabled"] = override


def load_config() -> GuardConfig:
    """读取并解析规则文件与开关状态；任何异常都冒泡给调用方走失败兜底。"""
    path = rules_file()
    raw = path.read_text(encoding="utf-8")
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise GuardError(f"规则文件顶层必须是对象：{path}")
    state_path = state_file(path)
    config = GuardConfig(data, path=path, state=load_state(state_path), state_path=state_path)
    apply_state(config)
    return config


# ---------------------------------------------------------------------------
# 条目 / 模式
# ---------------------------------------------------------------------------


def _entry_parts(entry: Any) -> tuple[str, str, bool]:
    """把词典条目拆成 ``(正则/字面量, 展示名, 是否为正则)``。"""
    if isinstance(entry, str):
        return entry, entry, False
    if isinstance(entry, dict):
        value = entry.get("value")
        text = value if isinstance(value, str) else ""
        name = entry.get("name")
        label = name if isinstance(name, str) and name else text
        return text, label, bool(entry.get("regex"))
    return "", "", False


def _select_entries(entries: list[Any], names: Any) -> list[Any]:
    """按 ``names`` 过滤词典条目；``names`` 缺省或含 ``"*"`` 时不过滤。"""
    if not isinstance(names, list) or not names or "*" in names:
        return entries
    wanted = {str(n) for n in names}
    selected = [e for e in entries if isinstance(e, dict) and str(e.get("name")) in wanted]
    if selected:
        return selected
    # 条目没有名字（纯字符串词典）时退回全量，避免配置写错导致规则静默失效。
    return [e for e in entries if not isinstance(e, dict)] or entries


class Pattern:
    """一条编译好的词典条目。"""

    __slots__ = ("label", "regex", "source")

    def __init__(self, source: str, label: str, regex: re.Pattern[str]) -> None:
        self.source = source
        self.label = label
        self.regex = regex


def compile_literal(text: str, *, label: str) -> Pattern:
    """把字面量编成带词边界的正则（纯 ASCII 时），否则编成普通子串。"""
    if _ASCII_TOKEN.fullmatch(text):
        source = r"(?<![A-Za-z0-9_])" + re.escape(text) + r"(?![A-Za-z0-9_])"
    else:
        source = re.escape(text)
    return Pattern(source, label, re.compile(source, re.IGNORECASE))


def compile_patterns(config: GuardConfig, detector: dict[str, Any], *, mode: str) -> list[Pattern]:
    """把一个词典型检测器展开成编译好的模式列表。

    ``mode``：``literal`` 按词边界匹配字面量（用于工具名关键词），``plain``
    按普通子串匹配字面量（用于路径片段、扩展名等不应受词边界影响的场景），
    ``regex`` 直接把条目当正则。条目显式标了 ``"regex": true`` 时一律按正则。
    """
    name = str(detector.get("dictionary") or "")
    if not name:
        raise GuardError(f"检测器缺少 dictionary：{detector!r}")
    entries = _select_entries(config.dictionary(name), detector.get("names"))
    if not entries:
        raise GuardError(f"词典为空或 names 未命中：{name}")
    patterns: list[Pattern] = []
    for entry in entries:
        value, label, is_regex = _entry_parts(entry)
        if not value:
            continue
        try:
            if mode == "regex" or is_regex:
                patterns.append(Pattern(value, label, re.compile(value, re.IGNORECASE)))
            elif mode == "plain":
                patterns.append(Pattern(re.escape(value), label, re.compile(re.escape(value), re.IGNORECASE)))
            else:
                patterns.append(compile_literal(value, label=label))
        except re.error as exc:
            raise GuardError(f"词典 {name} 中的正则非法：{value!r}（{exc}）") from exc
    return patterns


# ---------------------------------------------------------------------------
# 扫描上下文与命中
# ---------------------------------------------------------------------------


class ScanContext:
    """一次 Hook 评估的输入切片。"""

    __slots__ = ("args", "config", "cwd", "kind", "payload", "profile", "texts", "tool_name")

    def __init__(
        self,
        *,
        config: GuardConfig,
        texts: list[tuple[str, str]],
        args: dict[str, Any],
        payload: dict[str, Any],
    ) -> None:
        tool = _as_mapping(payload.get("tool"))
        self.config = config
        self.texts = texts
        self.args = args
        self.payload = payload
        self.kind = str(tool.get("kind") or "")
        self.tool_name = str(tool.get("name") or "")
        self.cwd = str(payload.get("cwd") or "")
        self.profile = str(payload.get("profile") or "")


class Evidence:
    """一条命中证据，用于用户提示与留痕。"""

    __slots__ = ("label", "snippet", "source")

    def __init__(self, label: str, snippet: str, source: str) -> None:
        self.label = label
        self.snippet = snippet
        self.source = source

    def describe(self) -> str:
        where = f"，来源 {self.source}" if self.source else ""
        return f"命中「{self.label}」{where}：{self.snippet}"

    def to_json(self) -> dict[str, str]:
        return {"label": self.label, "snippet": self.snippet, "source": self.source}


class Hit:
    """一条规则命中。"""

    __slots__ = ("disposition", "evidence", "rule_id", "rule_name")

    def __init__(self, rule: dict[str, Any], disposition: str, evidence: list[Evidence]) -> None:
        self.rule_id = str(rule.get("id") or "?")
        self.rule_name = str(rule.get("name") or self.rule_id)
        self.disposition = disposition
        self.evidence = evidence

    def to_json(self) -> dict[str, Any]:
        return {
            "id": self.rule_id,
            "name": self.rule_name,
            "disposition": self.disposition,
            "evidence": [e.to_json() for e in self.evidence],
        }


def snippet_of(text: str, start: int, end: int) -> str:
    """截取命中位置前后的短片段。"""
    left = max(0, start - 20)
    right = min(len(text), end + 20)
    piece = text[left:right].replace("\n", " ").replace("\r", " ").strip()
    if left > 0:
        piece = "…" + piece
    if right < len(text):
        piece = piece + "…"
    return piece[:MAX_SNIPPET_CHARS]


# ---------------------------------------------------------------------------
# 取值
# ---------------------------------------------------------------------------


def _stringify(value: Any, budget: int) -> str:
    if isinstance(value, str):
        return value[:budget]
    try:
        return json.dumps(value, ensure_ascii=False, default=str)[:budget]
    except (TypeError, ValueError):
        return str(value)[:budget]


def collect_texts(args: dict[str, Any], arg_keys: Any, budget: int) -> list[tuple[str, str]]:
    """按 ``arg_keys`` 收集待扫描文本；未指定时扫描全部参数。"""
    keys = (
        [str(key) for key in arg_keys if key in args] if isinstance(arg_keys, list) and arg_keys else list(args.keys())
    )
    texts: list[tuple[str, str]] = []
    remaining = budget
    for key in keys:
        if remaining <= 0:
            break
        text = _stringify(args.get(key), remaining)
        if not text:
            continue
        remaining -= len(text)
        texts.append((str(key), text))
    return texts


def path_values(args: dict[str, Any]) -> list[str]:
    """取出所有看起来像文件路径的参数值。"""
    found: list[str] = []
    for key, value in args.items():
        lowered = str(key).lower()
        if not any(hint in lowered for hint in _PATH_ARG_HINTS):
            continue
        if isinstance(value, str) and value:
            found.append(value)
        elif isinstance(value, list):
            found.extend(str(v) for v in value if isinstance(v, str) and v)
    return found


def url_values(args: dict[str, Any]) -> list[str]:
    """取出所有看起来像 URL / 主机名的参数值。"""
    found: list[str] = []
    for key, value in args.items():
        lowered = str(key).lower()
        if not any(hint in lowered for hint in _URL_ARG_HINTS):
            continue
        if isinstance(value, str) and value:
            found.append(value)
        elif isinstance(value, list):
            found.extend(str(v) for v in value if isinstance(v, str) and v)
    return found


def split_host_port(raw: str) -> tuple[str, int | None]:
    """把 ``url`` / ``host:port`` / 裸 IP 统一解析成 ``(host, port)``。"""
    text = raw.strip()
    if not text:
        return "", None
    if "://" in text:
        parts = urlsplit(text)
        host = parts.hostname or ""
        try:
            port = parts.port
        except ValueError:
            port = None
        if port is None and parts.scheme == "https":
            port = 443
        elif port is None and parts.scheme == "http":
            port = 80
        return host.lower(), port
    if text.startswith("//"):
        return split_host_port("http:" + text)
    candidate = text.split("/", 1)[0]
    if candidate.startswith("["):
        host, _, rest = candidate[1:].partition("]")
        port_text = rest[1:] if rest.startswith(":") else ""
        return host.lower(), int(port_text) if port_text.isdigit() else None
    host, _, port_text = candidate.partition(":")
    if port_text.isdigit():
        return host.lower(), int(port_text)
    return candidate.lower(), None


def _as_ip(host: str) -> Any:
    """把主机名解析成 IP 对象；不是 IP 字面量时返回 ``None``。"""
    try:
        return ipaddress.ip_address(host)
    except ValueError:
        return None


def _network_or_none(entry: Any) -> Any:
    try:
        return ipaddress.ip_network(str(entry), strict=False)
    except ValueError:
        return None


def _networks(entries: list[Any]) -> list[Any]:
    nets = [_network_or_none(entry) for entry in entries]
    return [net for net in nets if net is not None]


def _allowed_networks(config: GuardConfig) -> list[Any]:
    """``settings.network.allowed_networks``——提示词层 ip_cidr 检测器的白名单。

    出厂只有回环（``127.0.0.0/8``、``::1/128``），所以 ``127.0.0.1`` 放行而RFC1918
    整段仍拦。原先这个设置只有 url 检测器读，ip_cidr 只看 ``internal_networks`` 词典，
    于是提示词层把 ``127.0.0.1`` 也拦了；现在两层读同一份配置，工具层另外叠加
    ``internal_networks``（把私有段当“内网”而非“公网访问”），行为不受这里影响。
    要放行某段私有网，往 ``allowed_networks`` 加即可。
    """
    entries = config.sub_setting("network", {}).get("allowed_networks") or []
    return _networks([str(entry) for entry in entries])


# ---------------------------------------------------------------------------
# 检测器
# ---------------------------------------------------------------------------


def _match_patterns(patterns: list[Pattern], texts: list[tuple[str, str]]) -> list[Evidence]:
    evidence: list[Evidence] = []
    for label, text in texts:
        for pattern in patterns:
            found = pattern.regex.search(text)
            if found is None:
                continue
            evidence.append(Evidence(pattern.label, snippet_of(text, found.start(), found.end()), label))
            if len(evidence) >= 4:
                return evidence
    return evidence


def _detect_patterns(config: GuardConfig, detector: dict[str, Any], ctx: ScanContext, *, mode: str) -> list[Evidence]:
    return _match_patterns(compile_patterns(config, detector, mode=mode), ctx.texts)


def _detect_ip_cidr(config: GuardConfig, detector: dict[str, Any], ctx: ScanContext) -> list[Evidence]:
    networks = _networks(config.dictionary(str(detector.get("dictionary") or "")))
    if not networks:
        return []
    allowed = _allowed_networks(config)
    evidence: list[Evidence] = []
    for label, text in ctx.texts:
        for raw in _IPV4_IN_TEXT.findall(text):
            address = _as_ip(raw)
            if address is None:
                continue
            if any(address.version == net.version and address in net for net in allowed):
                continue
            for network in networks:
                if address.version == network.version and address in network:
                    evidence.append(Evidence(str(network), raw, label))
                    break
    return evidence


def _detect_path(config: GuardConfig, detector: dict[str, Any], ctx: ScanContext) -> list[Evidence]:
    patterns = compile_patterns(config, detector, mode="plain")
    evidence: list[Evidence] = []
    for raw in path_values(ctx.args):
        normalized = raw.replace("/", "\\")
        for pattern in patterns:
            found = pattern.regex.search(normalized) or pattern.regex.search(raw)
            if found is None:
                continue
            evidence.append(Evidence(pattern.label, raw, "path"))
            break
    return evidence


def _detect_extension(config: GuardConfig, detector: dict[str, Any], ctx: ScanContext) -> list[Evidence]:
    patterns = compile_patterns(config, detector, mode="plain")
    evidence: list[Evidence] = []
    for raw in path_values(ctx.args):
        lowered = raw.lower()
        for pattern in patterns:
            if pattern.regex.search(lowered):
                evidence.append(Evidence(pattern.label, raw, "path"))
                break
    return evidence


_WRITE_FLAG_WITH_VALUE = frozenset({"-f", "-d", "-o", "-O"})
_COMMAND_WRAPPERS = frozenset({"sudo", "doas", "nohup", "time", "command", "nice", "ionice"})
_REDIRECT_WRITE = re.compile(r"^(?:\d*>>?|&>>?)\|?$")
_REDIRECT_SKIP = re.compile(r"^(?:\d*<<|<<<|\d*>&|\d*<&|<<)$")
_ENV_ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
_COMMAND_SEPARATORS = frozenset({"|", "||", "&&", ";", "&"})

# 会创建/修改文件、且目标参数位置固定的命令。all=全部位置参数，last=最后一个，
# of= /after:f / after:d = 指定参数形式，inplace=原地编辑的末位操作数。
_SHELL_WRITE_COMMANDS: dict[str, str] = {
    "touch": "all",
    "mkdir": "all",
    "rmdir": "all",
    "tee": "all",
    "cp": "last",
    "mv": "last",
    "install": "last",
    "ln": "last",
    "scp": "last",
    "rsync": "last",
    "zip": "first",
    "dd": "of=",
    "sed": "inplace",
    "tar": "after:f",
    "unzip": "after:d",
    "copy": "last",
    "move": "last",
    "ren": "last",
    "rename": "last",
}

_PS_WRITE_COMMANDS = frozenset(
    {
        "set-content",
        "add-content",
        "out-file",
        "new-item",
        "copy-item",
        "move-item",
        "rename-item",
        "tee-object",
        "export-csv",
        "export-clixml",
        "remove-item",
    }
)
_PS_PATH_FLAGS = frozenset({"-path", "-literalpath", "-destinationpath"})

# 这些命令的参数是脚本路径 / 包名 / 构建目标，不是“直接写文件”的操作数。
# 静态分析跟不进去它们内部写了什么（停机问题），所以只拦它们外面的重定向。
_SCRIPT_RUNNERS = frozenset(
    {
        "python",
        "python2",
        "python3",
        "py",
        "pypy",
        "ipython",
        "node",
        "nodejs",
        "deno",
        "bun",
        "tsx",
        "ts-node",
        "npx",
        "ruby",
        "perl",
        "php",
        "lua",
        "npm",
        "yarn",
        "pnpm",
        "uv",
        "pip",
        "pipenv",
        "poetry",
        "conda",
        "make",
        "cmake",
        "ninja",
        "just",
        "task",
        "tox",
        "nox",
        "pytest",
        "cargo",
        "go",
        "java",
        "javac",
        "mvn",
        "gradle",
        "dotnet",
        "swift",
        "git",
        "docker",
        "podman",
        "kubectl",
        "terraform",
        "ansible",
        "eslint",
        "prettier",
        "tsc",
        "jest",
        "vitest",
        "ruff",
        "mypy",
        "black",
        "bash",
        "sh",
        "zsh",
        "dash",
        "ksh",
        "fish",
        "cmd",
        "cmd.exe",
        "powershell",
        "pwsh",
        "source",
        ".",
        "godot",
        "unity",
        "flutter",
    }
)
# ``bash -c "..."`` 是内联命令而不是脚本文件，对引号里的命令递归一次。
_INLINE_SHELLS = frozenset({"bash", "sh", "zsh", "dash", "ksh"})

_NULL_TARGETS = frozenset({"nul", ".\\nul", "devnull", "/dev/null", "/dev/stdout", "/dev/stderr"})


def _write_scope_evidence(config: GuardConfig, cwd: str, raws: list[str], *, source: str) -> list[Evidence]:
    """写入范围判定：限制开启时只放行工作区内与 ``$TEMP/icode-session-**``，帮助文档只读。

    T-FS-01（文件系统工具的 path 参数）与 T-SHELL-05（shell 命令里解析出的写
    目标）共用这一套判定，两类入口的处置口径保持一致。

    限不限制由 ``settings.write_scope.allow_outside_workspace`` 决定（出厂
    ``true`` = 不限制）；现场可以用状态文件覆盖（``guardctl workspace restrict``）。
    """
    if not config.write_scope_restricted:
        return []
    scope = config.sub_setting("write_scope", {})
    workspace = _resolve_workspace(cwd)
    workspace_scope = _canonical_scope(cwd, None) or workspace
    temp_root = _posix_abs(tempfile.gettempdir()) or ""
    temp_prefixes = [str(p) for p in scope.get("temp_prefixes") or ["icode-session-"]]
    readonly_globs = [str(g) for g in scope.get("readonly_globs") or []]

    evidence: list[Evidence] = []
    for raw in raws:
        target = _resolve_workspace(cwd, raw)
        if target is None:
            evidence.append(Evidence("无法解析的写入目标", raw, source))
            continue
        # Resolve the native filesystem path as well: a workspace-local symlink
        # or junction may point outside, which lexical normpath cannot see.
        target_scope = _canonical_scope(cwd, raw) or target
        if any(fnmatch(target_scope, glob) for glob in readonly_globs):
            evidence.append(Evidence("只读帮助文档", raw, source))
            continue
        if workspace_scope is not None and _is_within(target_scope, workspace_scope):
            continue
        if _is_session_temp(target_scope, temp_root, temp_prefixes):
            continue
        evidence.append(Evidence("工作区外写入", raw, source))
    return evidence


def _detect_write_scope(ctx: ScanContext) -> list[Evidence]:
    """T-FS-01：文件系统工具写入白名单。"""
    return _write_scope_evidence(ctx.config, ctx.cwd, path_values(ctx.args), source="path")


def _detect_shell_write_scope(ctx: ScanContext) -> list[Evidence]:
    """T-SHELL-05：shell 命令写到工作目录外。

    命令文本经 ``arg_keys`` 收进 ``ctx.texts``，所以与 T-SHELL-01 读的是同一份
    入参，只是这里要把重定向与写文件命令的目标参数解析出来再判范围。
    """
    targets: list[str] = []
    for _label, text in ctx.texts:
        targets.extend(_command_write_targets(text))
    return _write_scope_evidence(ctx.config, ctx.cwd, targets, source="command")


def _shell_tokens(command: str) -> list[str]:
    """按 shell / PowerShell 的词法切分：引号内不切分，引号本身从 token 里剥掉。

    只做切分不做展开——``$VAR``、反引号、glob 都原样保留，交给范围判定决定。
    """
    tokens: list[str] = []
    current: list[str] = []
    quote = ""
    for char in command:
        if quote:
            if char == quote:
                quote = ""
            else:
                current.append(char)
            continue
        if char in "\"'":
            quote = char
            continue
        if char == ">":
            # Split an attached redirection (``echo x>file``) into its own
            # operator token.  A ``>`` inside quotes stays part of the word.
            if current:
                tokens.append("".join(current))
                current = []
            if tokens and tokens[-1] == ">":
                tokens[-1] = ">>"
            else:
                tokens.append(">")
            continue
        if char == "|" and tokens and tokens[-1] == ">":
            tokens[-1] = ">|"
            continue
        if char.isspace():
            if current:
                tokens.append("".join(current))
                current = []
            continue
        current.append(char)
    if current:
        tokens.append("".join(current))
    return tokens


def _is_null_target(value: str) -> bool:
    """``/dev/null``、Windows ``nul``、``$null`` 都不是磁盘写入。"""
    lowered = value.strip().strip("\"'").replace("/", "\\").lower().lstrip("\\")
    return lowered in _NULL_TARGETS or lowered in {"dev\\null", "$null", "-", "null"}


def _redirect_targets(tokens: list[str]) -> list[str]:
    """抽出 ``>`` / ``>>`` / ``&>`` 后面的重定向目标，跳过 fd 复制与 here-doc。"""
    targets: list[str] = []
    for index, token in enumerate(tokens):
        if _REDIRECT_SKIP.match(token) or not _REDIRECT_WRITE.match(token):
            continue
        if index + 1 >= len(tokens):
            continue
        candidate = tokens[index + 1]
        if candidate.startswith("&"):
            continue
        targets.append(candidate)
    return targets


def _positional_args(args: list[str]) -> list[str]:
    """位置参数：跳过布尔选项，吃掉 ``-f FILE`` 这类带值选项。"""
    values: list[str] = []
    skip = False
    for argument in args:
        if skip:
            skip = False
            continue
        if argument in _WRITE_FLAG_WITH_VALUE:
            skip = True
            continue
        if argument.startswith("-") and argument != "-":
            continue
        values.append(argument)
    return values


def _flag_value(args: list[str], letter: str) -> list[str]:
    """取短选项 ``-f FILE`` 的取值，同时认 ``-cf FILE`` 这种合并写法。"""
    values: list[str] = []
    for index, token in enumerate(args):
        if not token.startswith("-") or token.startswith("--") or len(token) < 2:
            continue
        cluster = token[1:]
        position = cluster.find(letter)
        if position < 0:
            continue
        rest = cluster[position + 1 :]
        if rest:
            values.append(rest)
        elif index + 1 < len(args):
            values.append(args[index + 1])
    return values


def _powershell_targets(name: str, args: list[str]) -> list[str]:
    """PowerShell 写 cmdlet 的目标。

    Copy/Move 的 ``-Path`` 是源，真正的写入目标是 ``-Destination`` 或第二个
    位置参数；其余 cmdlet 仍按 ``-Path`` / 第一个位置参数判定。
    """
    destination_flags = {"-destination", "-destinationpath"}
    destinations: list[str] = []
    for index, token in enumerate(args[:-1]):
        if token.lower() in destination_flags:
            destinations.extend(args[index + 1].split(","))
    if destinations:
        return destinations
    positional = _positional_args(args)
    if name.lower() in {"copy-item", "move-item"}:
        return positional[1:2]
    targets: list[str] = []
    for index, token in enumerate(args[:-1]):
        if token.lower() in _PS_PATH_FLAGS:
            targets.extend(args[index + 1].split(","))
    if targets:
        return targets
    return positional[:1]


def _writer_targets(name: str, args: list[str]) -> list[str]:
    lowered = name.lower()
    if lowered in _PS_WRITE_COMMANDS:
        return _powershell_targets(name, args)
    mode = _SHELL_WRITE_COMMANDS.get(lowered)
    if mode is None:
        return []
    if mode == "all":
        return _positional_args(args)
    if mode == "first":
        return _positional_args(args)[:1]
    if mode == "last":
        positional = _positional_args(args)
        return positional[-1:]
    if mode == "of=":
        return [a.split("=", 1)[1] for a in args if a.lower().startswith("of=")]
    if mode == "after:f":
        return _flag_value(args, "f")
    if mode == "after:d":
        return _flag_value(args, "d")
    # inplace：只有带 -i / -I 才算写，否则只是输出到标准输出。
    if not any(token in ("-i", "-I", "--in-place") for token in args):
        return []
    positional = _positional_args(args)
    return positional[-1:]


def _split_segments(tokens: list[str]) -> list[list[str]]:
    """按管道与命令连接符切成一个个独立命令段。"""
    segments: list[list[str]] = []
    current: list[str] = []
    for token in tokens:
        if token in _COMMAND_SEPARATORS:
            if current:
                segments.append(current)
            current = []
            continue
        current.append(token)
    if current:
        segments.append(current)
    return segments


def _segment_write_targets(segment: list[str]) -> list[str]:
    """单个命令段里，写文件命令的目标参数。"""
    index = 0
    while index < len(segment):
        token = segment[index].lstrip("&")
        if token.lower() in _COMMAND_WRAPPERS or _ENV_ASSIGNMENT.match(token):
            index += 1
            continue
        break
    body = segment[index:]
    if not body:
        return []
    head = body[0].lstrip("&")
    lowered = head.lower()
    args = body[1:]
    if lowered in _INLINE_SHELLS and "-c" in args:
        position = args.index("-c")
        if position + 1 < len(args):
            return _command_write_targets(args[position + 1])
        return []
    if lowered in _SCRIPT_RUNNERS:
        return []
    return _writer_targets(head, args)


def _command_write_targets(command: str) -> list[str]:
    """从一条命令里抽出所有写入目标（重定向 + 写文件命令的目标参数）。

    分两层：**重定向是 shell 自己做的**，命令头是什么都算，所以对整条命令扫一遍；
    **写文件命令的目标参数**只在命令头本身是写文件工具时才算——``python build.py
    ../x`` 里的 ``../x`` 是脚本的参数而不是 shell 的写入，放行。
    """
    tokens = _shell_tokens(command)
    targets = _redirect_targets(tokens)
    for segment in _split_segments(tokens):
        targets.extend(_segment_write_targets(segment))
    return [t for t in targets if t and not _is_null_target(t)]


_WIN_DRIVE = re.compile(r"^[A-Za-z]:(?:/|$)")


def _posix_abs(text: str) -> str | None:
    """把一段路径文本归一成**OS 无关**的 posix 绝对路径字符串。

    写入范围判定不能依赖宿主机的 ``pathlib`` 语义：同一条命令在 Windows 用户
    机器上执行，Hook 自检却跑在 Linux/macOS CI 上。这里统一处理 ``\\`` → ``/``、
    盘符 ``C:/``（映射成 ``/C:/``）与 UNC 前缀，凡是形如绝对路径的一律认出；
    相对路径返回 ``None``，由调用方决定挂到哪个工作区下。
    """
    text = text.replace("\\", "/")
    if text.startswith("~"):
        return posixpath.normpath(posixpath.expanduser(text))
    if _WIN_DRIVE.match(text):
        text = text[0].upper() + text[1:]
        return posixpath.normpath("/" + text)
    if text.startswith("//"):
        return posixpath.normpath(text)
    if text.startswith("/"):
        return posixpath.normpath(text)
    return None


def _resolve_workspace(cwd: str, raw: str | None = None) -> str | None:
    """解析出用于范围判定的 posix 绝对路径（详见 :func:`_posix_abs`）。"""
    if raw is None:
        base = _posix_abs(cwd) if cwd else None
        if base is None:
            base = _posix_abs(str(Path.cwd()))
        return base
    absolute = _posix_abs(raw)
    if absolute is not None:
        return absolute
    base = _posix_abs(cwd) if cwd else _posix_abs(str(Path.cwd()))
    if base is None:
        return None
    return posixpath.normpath(posixpath.join(base, raw.replace("\\", "/")))


def _native_resolved(cwd: str, raw: str | None) -> Path | None:
    """Resolve a path on the host filesystem; ``None`` when it is not native.

    selftest drives Windows-style paths on every platform, so a drive-letter
    path on POSIX stays lexical instead of being misresolved as relative.
    """
    if sys.platform != "win32":
        if raw is not None and _WIN_DRIVE.match(raw):
            return None
        if raw is None and cwd and _WIN_DRIVE.match(cwd):
            return None
    base = Path(cwd).expanduser() if cwd else Path.cwd()
    candidate = base if raw is None else Path(raw).expanduser()
    if raw is not None and not candidate.is_absolute():
        candidate = base / candidate
    try:
        return candidate.resolve(strict=False)
    except (OSError, RuntimeError, ValueError):
        return None


def _canonical_scope(cwd: str, raw: str | None) -> str | None:
    """Return the native canonical path in the scope-check posix form."""
    resolved = _native_resolved(cwd, raw)
    if resolved is None:
        return None
    return _posix_abs(str(resolved))


def _is_within(target: str, root: str) -> bool:
    return target == root or target.startswith(root + "/")


def _is_session_temp(target: str, temp_root: str, prefixes: list[str]) -> bool:
    if not temp_root or not _is_within(target, temp_root):
        return False
    return any(
        part.startswith(prefix) for prefix in prefixes for part in target.split("/")[-4:] if len(part) > len(prefix)
    )


_LOOPBACK_HOSTNAMES = ("localhost", "localhost.localdomain", "ip6-localhost", "ip6-loopback")


def _is_allowed_hostname(config: GuardConfig, host: str) -> bool:
    """主机名白名单——``allowed_networks`` 只管 IP 字面量，``localhost`` 是名字。

    出厂含回环名与 RFC 6761 的 ``.localhost`` 伪 TLD；配置项是
    ``settings.network.allowed_hosts``，可自行追加。
    """
    entries = config.sub_setting("network", {}).get("allowed_hosts")
    names = [str(item).strip().lower() for item in entries] if entries else list(_LOOPBACK_HOSTNAMES)
    if host in names:
        return True
    return any(host.endswith("." + name) for name in names)


def _detect_url(ctx: ScanContext, mode: str) -> list[Evidence]:
    """网络分类：``blocked`` 命中黑名单/危险端口，``default`` 命中非白名单地址。"""
    if not url_values(ctx.args):
        return []
    blocked_hosts = compile_patterns(ctx.config, {"dictionary": "blocked_hosts", "names": None}, mode="plain")
    dangerous_ports = {int(p) for p in ctx.config.dictionary("dangerous_ports") if isinstance(p, (int, float))}
    allowed = _networks(ctx.config.dictionary("internal_networks"))
    allowed.extend(_allowed_networks(ctx.config))

    evidence: list[Evidence] = []
    for raw in url_values(ctx.args):
        host, port = split_host_port(raw)
        if not host:
            continue
        if mode == "blocked":
            if port is not None and port in dangerous_ports:
                evidence.append(Evidence(f"危险端口 {port}", raw, "url"))
                continue
            for pattern in blocked_hosts:
                if pattern.regex.search(host):
                    evidence.append(Evidence(pattern.label, raw, "url"))
                    break
        elif mode == "default":
            name = host.lower().rstrip(".")
            if _is_allowed_hostname(ctx.config, name):
                continue
            address = _as_ip(name)
            in_network = address is not None and any(address.version == n.version and address in n for n in allowed)
            if not in_network:
                evidence.append(Evidence("非白名单地址", raw, "url"))
    return evidence


def _detect_mcp_server(ctx: ScanContext) -> list[Evidence]:
    allowed = [str(s) for s in ctx.config.sub_setting("mcp", {}).get("allowed_servers") or []]
    if not allowed:
        return []
    candidates = {
        str(ctx.args.get("server") or ""),
        str(ctx.args.get("server_name") or ""),
        ctx.tool_name.split("_", 1)[0] if ctx.tool_name else "",
        ctx.tool_name,
    }
    candidates.discard("")
    for name in candidates:
        for entry in allowed:
            if name == entry or name.startswith(entry + "_") or name.endswith("_" + entry):
                return []
    return [Evidence("MCP Server 不在白名单", ctx.tool_name or "<unknown>", "tool.name")]


def _detect_mcp_args_schema(ctx: ScanContext) -> list[Evidence]:
    """MCP 入参的基本结构校验（完整 JSON Schema 校验需要 MCP 服务端配合）。"""
    if not isinstance(ctx.args, dict):
        return [Evidence("入参不是对象", type(ctx.args).__name__, "args")]
    evidence: list[Evidence] = []
    limit = 100000
    for key, value in ctx.args.items():
        text = _stringify(value, 200000)
        if not isinstance(key, str) or not key:
            evidence.append(Evidence("参数名非法", repr(key), "args"))
            continue
        if len(text) > limit:
            evidence.append(Evidence("参数过长", f"{key}（{len(text)} 字符）", "args"))
        elif _CONTROL_CHARS.search(text):
            evidence.append(Evidence("参数含控制字符", key, "args"))
    return evidence


def _detect_caller_context(ctx: ScanContext) -> list[Evidence]:
    mcp = ctx.config.sub_setting("mcp", {})
    allowed_profiles = [str(p) for p in mcp.get("allowed_profiles") or []]
    if allowed_profiles and ctx.profile not in allowed_profiles:
        return [Evidence("调用方 Profile 不在白名单", ctx.profile or "<empty>", "profile")]
    allowed_workspaces = [str(w) for w in mcp.get("allowed_workspaces") or []]
    if allowed_workspaces:
        workspace = _resolve_workspace(ctx.cwd)
        roots = [_resolve_workspace("", w) for w in allowed_workspaces]
        if workspace is None or not any(r is not None and _is_within(workspace, r) for r in roots):
            return [Evidence("调用方工作目录不在白名单", ctx.cwd or "<empty>", "cwd")]
    return []


def _detect_rule_refs(detector: dict[str, Any], ctx: ScanContext, depth: int) -> list[Evidence]:
    evidence: list[Evidence] = []
    for rule_id in detector.get("rules") or []:
        rule = ctx.config.rule(str(rule_id))
        if rule is None:
            raise GuardError(f"rule_refs 引用了不存在的规则：{rule_id}")
        for item in _run_detectors(rule.get("detect") or [], ctx, depth + 1):
            evidence.append(Evidence(f"{rule_id} {item.label}", item.snippet, item.source))
            if len(evidence) >= 6:
                return evidence
    return evidence


def _run_detectors(detectors: list[Any], ctx: ScanContext, depth: int = 0) -> list[Evidence]:
    evidence: list[Evidence] = []
    if depth > MAX_RULE_REFS_DEPTH:
        raise GuardError("rule_refs 递归过深")
    for detector in detectors:
        if not isinstance(detector, dict):
            raise GuardError(f"检测器必须是对象：{detector!r}")
        kind = str(detector.get("type") or "")
        if kind == "keyword":
            found = _detect_patterns(ctx.config, detector, ctx, mode="literal")
        elif kind == "path":
            found = _detect_patterns(ctx.config, detector, ctx, mode="plain")
        elif kind in ("regex", "secret"):
            found = _detect_patterns(ctx.config, detector, ctx, mode="regex")
        elif kind == "ip_cidr":
            found = _detect_ip_cidr(ctx.config, detector, ctx)
        elif kind == "extension":
            found = _detect_extension(ctx.config, detector, ctx)
        elif kind == "write_scope":
            found = _detect_write_scope(ctx)
        elif kind == "shell_write_target":
            found = _detect_shell_write_scope(ctx)
        elif kind == "url":
            found = _detect_url(ctx, str(detector.get("mode") or "blocked"))
        elif kind == "mcp_server":
            found = _detect_mcp_server(ctx)
        elif kind == "mcp_args_schema":
            found = _detect_mcp_args_schema(ctx)
        elif kind == "caller_context":
            found = _detect_caller_context(ctx)
        elif kind == "rule_refs":
            found = _detect_rule_refs(detector, ctx, depth)
        else:
            raise GuardError(f"未知检测器类型：{kind!r}")
        evidence.extend(found)
        if evidence:
            return evidence
    return evidence


# ---------------------------------------------------------------------------
# 规则求值
# ---------------------------------------------------------------------------


def _rule_enabled(rule: dict[str, Any]) -> bool:
    return bool(rule.get("enabled", True))


def _applies_to(rule: dict[str, Any], ctx: ScanContext) -> bool:
    applies = rule.get("applies_to")
    if not isinstance(applies, dict):
        return True
    kinds = applies.get("kinds")
    if isinstance(kinds, list) and kinds and ctx.kind not in {str(k) for k in kinds}:
        return False
    names = applies.get("tool_names")
    return not (isinstance(names, list) and names and ctx.tool_name not in {str(n) for n in names})


def effective_disposition(rule: dict[str, Any], config: GuardConfig) -> str:
    """把规则里的 ``block`` / ``confirm`` 映射成实际处置。

    ``settings.confirm_disposition`` 是全局开关：默认 ``block``，表里的
    ``confirm`` 一律打断并提示；改成 ``notify`` 则 confirm 类规则只留痕、
    提示词层额外给模型一条 ``system_reminder``。
    """
    disposition = str(rule.get("disposition") or "confirm").lower()
    if disposition not in _VALID_DISPOSITIONS:
        raise GuardError(f"未知的 disposition：{disposition!r}")
    if disposition != "confirm":
        return disposition
    return "block" if str(config.setting("confirm_disposition", "block")) == "block" else "notify"


def _check_deadline(deadline: float | None) -> None:
    if deadline is not None and time.monotonic() > deadline:
        raise DeadlineExceeded("超过 settings.self_timeout_seconds")


def evaluate_rules(
    rules: list[dict[str, Any]],
    ctx: ScanContext,
    *,
    deadline: float | None,
) -> list[Hit]:
    """顺序跑规则，返回全部命中（按规则表顺序）。"""
    hits: list[Hit] = []
    for rule in rules:
        _check_deadline(deadline)
        if not _rule_enabled(rule) or not _applies_to(rule, ctx):
            continue
        evidence = _run_detectors(rule.get("detect") or [], ctx)
        if evidence:
            hits.append(Hit(rule, effective_disposition(rule, ctx.config), evidence))
    return hits


def evaluate_prompt(config: GuardConfig, text: str, *, deadline: float | None = None) -> list[Hit]:
    """提示词层：拿用户输入跑 ``prompt_rules``。"""
    ctx = ScanContext(config=config, texts=[("text", text[: config.max_scan_chars])], args={}, payload={})
    return evaluate_rules(config.prompt_rules, ctx, deadline=deadline)


def evaluate_tool_call(config: GuardConfig, payload: dict[str, Any], *, deadline: float | None = None) -> list[Hit]:
    """工具调用层：拿工具入参跑 ``tool_rules``。"""
    tool = _as_mapping(payload.get("tool"))
    raw_args = _as_mapping(tool.get("args"))
    ctx = ScanContext(config=config, texts=[], args=dict(raw_args), payload=payload)
    hits: list[Hit] = []
    for rule in config.tool_rules:
        _check_deadline(deadline)
        if not _rule_enabled(rule) or not _applies_to(rule, ctx):
            continue
        applies = rule.get("applies_to")
        arg_keys = applies.get("arg_keys") if isinstance(applies, dict) else None
        ctx.texts = collect_texts(dict(raw_args), arg_keys, config.max_scan_chars)
        evidence = _run_detectors(rule.get("detect") or [], ctx)
        if evidence:
            hits.append(Hit(rule, effective_disposition(rule, ctx.config), evidence))
    return hits


# ---------------------------------------------------------------------------
# 用户提示与留痕
# ---------------------------------------------------------------------------

DEFAULT_BLOCK_MESSAGE = "操作不合规，请修改后重试。"


def format_message(config: GuardConfig) -> str:
    """生成给用户看的拦截提示。

    刻意只有一句：用户界面不需要知道命中了哪几条规则、证据是什么——那些明细
    完整写在留痕文件里，需要排查时看那里。文案可用 ``settings.block_message``
    改，不必动代码。
    """
    return str(config.setting("block_message") or DEFAULT_BLOCK_MESSAGE)


def audit_path(config: GuardConfig) -> Path:
    """留痕文件路径。"""
    override = os.environ.get(AUDIT_PATH_ENV)
    if override:
        return Path(override)
    return Path(expand(str(config.setting("audit_log", "${chrys_home}/hooks/guard_audit.jsonl"))))


def audit_records(layer: str, hits: list[Hit], payload: dict[str, Any], *, action: str) -> list[dict[str, Any]]:
    """构造留痕记录。"""
    tool = _as_mapping(payload.get("tool"))
    stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    return [
        {
            "ts": stamp,
            "layer": layer,
            "hook_id": os.environ.get("CHRYS_HOOK_ID", ""),
            "session_id": payload.get("session_id", ""),
            "profile": payload.get("profile", ""),
            "cwd": payload.get("cwd", ""),
            "tool": {"name": tool.get("name", ""), "kind": tool.get("kind", "")},
            "action": action,
            "rules": [hit.to_json() for hit in hits],
        }
    ]


def write_audit(records: list[dict[str, Any]], config: GuardConfig) -> None:
    """追加留痕；写不进去也不能影响拦截本身。"""
    path = audit_path(config)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            for record in records:
                handle.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
    except OSError as exc:
        _log(f"留痕写入失败（{path}）：{exc}")


# ---------------------------------------------------------------------------
# 子进程协议
# ---------------------------------------------------------------------------


def read_payload() -> dict[str, Any]:
    """读取 ``$CHRYS_HOOK_PAYLOAD_FILE``。"""
    path = os.environ.get("CHRYS_HOOK_PAYLOAD_FILE")
    if not path:
        return {}
    with open(path, encoding="utf-8") as handle:
        payload = json.load(handle)
    return payload if isinstance(payload, dict) else {}


def write_decision(decision: dict[str, Any]) -> bool:
    """写入 ``$CHRYS_HOOK_RESULT``；返回是否成功。"""
    path = os.environ.get("CHRYS_HOOK_RESULT")
    if not path:
        return False
    try:
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(decision, handle, ensure_ascii=False)
    except OSError as exc:
        _log(f"决策写入失败（{path}）：{exc}")
        return False
    return True


def _log(message: str) -> None:
    sys.stderr.write(f"[guard] {message}\n")


def _fail_closed(config: GuardConfig | None, layer: str, detail: str) -> int:
    """IN-09 失败兜底：引擎自身异常时按 fail_closed 决定打断还是放行。"""
    fail_closed = True if config is None else config.fail_closed
    _log(f"{layer} 引擎异常：{detail}")
    if fail_closed:
        message = f"{DEFAULT_BLOCK_MESSAGE}\n（原因：策略引擎异常，{detail[:150]}）"
        if write_decision({"action": "block", "reason": message}):
            return 0
        # 连决策文件都写不进去时用非零退出码，让 on_error: block 兜住。
        return 1
    return 0


def run_hook(layer: str) -> int:
    """两个入口脚本共用的主流程。

    返回进程退出码：正常路径恒为 ``0``（放行或写好决策）；只有在决策都写不
    出来时才返回非零，交给 ``hooks.yaml`` 的 ``on_error: block`` 兜底。
    """
    started = time.monotonic()
    config: GuardConfig | None = None
    payload: dict[str, Any] = {}
    try:
        payload = read_payload()
        config = load_config()
        if not config.master_enabled:
            _log("总开关已关闭，本次不检查")
            return 0
        deadline = started + config.self_timeout_seconds
        if layer == "prompt":
            hits = evaluate_prompt(config, str(payload.get("text") or ""), deadline=deadline)
        else:
            hits = evaluate_tool_call(config, payload, deadline=deadline)

        if not hits:
            if config.setting("audit_on_allow", False):
                write_audit(audit_records(layer, [], payload, action="allow"), config)
            return 0

        blocking = [h for h in hits if h.disposition == "block"]
        write_audit(audit_records(layer, hits, payload, action="block" if blocking else "notify"), config)
        if not blocking:
            # confirm_disposition=notify：提示词层把命中规则告诉模型让它自己调整，
            # 工具层只留痕。system_reminder 不直接展示给用户，所以带上规则 ID。
            if layer == "prompt":
                ids = ", ".join(hit.rule_id for hit in hits)
                write_decision({"system_reminder": f"[安全策略] {DEFAULT_BLOCK_MESSAGE}命中规则：{ids}"})
            return 0
        if not write_decision({"action": "block", "reason": format_message(config)}):
            # A rule fired but the result file is unusable. Exit non-zero so
            # ``on_error: block`` stops the call instead of treating it as allow.
            return 1
        return 0
    except (DeadlineExceeded, GuardError) as exc:
        return _fail_closed(config, _layer_title(layer), str(exc))
    except Exception as exc:  # 兜底：任何未预期异常都不得让 Hook 静默通过
        return _fail_closed(config, _layer_title(layer), f"{type(exc).__name__}: {exc}")


def _layer_title(layer: str) -> str:
    return "提示词层" if layer == "prompt" else "工具调用前"


if __name__ == "__main__":  # pragma: no cover - 手工冒烟入口
    raise SystemExit(run_hook(sys.argv[1] if len(sys.argv) > 1 else "tool"))
