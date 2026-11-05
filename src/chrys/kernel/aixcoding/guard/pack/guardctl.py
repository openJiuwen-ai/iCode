# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""安全护栏的开关管理：总开关（默认开）、单条规则开关与写入范围开关。

只依赖标准库，**不需要 uv**：Windows 上有 ``guardctl.cmd`` 免 uv 入口（双击或
``guardctl.cmd status`` 即可，内部借 ``_launcher.ps1`` 现找一个可用解释器），
macOS / Linux 有对应的 ``guardctl.sh``（现找 ``python3`` / ``python``）。

用法（``guardctl.py`` 指下面任一种写法）::

    guardctl.py status              # 看当前开关
    guardctl.py off                 # 关掉整个护栏
    guardctl.py on                  # 打开（默认就是开）
    guardctl.py disable IN-07        # 关掉一条规则
    guardctl.py enable IN-07         # 再打开
    guardctl.py disable 'T-SHELL-03-*'   # 通配符
    guardctl.py list                 # 规则清单与开关
    guardctl.py show IN-07           # 看一条规则的完整定义
    guardctl.py workspace allow      # 不限制写入工作区外（出厂）
    guardctl.py workspace restrict   # 只允许写工作区内
    guardctl.py reset                # 回到出厂状态

Windows 上已安装护栏时::

    & "$env:APPDATA\\chrys\\hooks\\guard\\guardctl.cmd" status
    python "$env:APPDATA\\chrys\\hooks\\guard\\guardctl.py" status

macOS / Linux 上已安装护栏时::

    ~/.chrys/hooks/guard/guardctl.sh status
    python3 ~/.chrys/hooks/guard/guardctl.py status

开关写在 ``guard_state.json``（与 ``rules.json`` 同目录），**不写进
``rules.json``**：``rules.json`` 是随包分发的规则文件，每次启动 iCode 都会被
重新安装覆盖（``chrys.kernel.aixcoding.guard.guard_install``），写进去的开关
下一次启动就没了。状态文件不在安装脚本的 payload 列表里，升级、重装都不会动它。

默认操作的是**已安装的那份**规则（``<config_dir>/hooks/guard/``），因为护栏
运行时读的就是它；没安装时退回脚本同目录的规则文件。``--rules`` /
``--state`` 可以显式指定别的路径。

本脚本只依赖标准库，兼容 Python 3.9+，与主程序零耦合；开关语义直接复用
``_guard_core``，保证"命令行看到的"和"引擎执行的"是同一套判定。仓库内调试可直接跑
``uv run python src/chrys/kernel/aixcoding/guard/pack/guardctl.py status``。
"""

from __future__ import annotations

import argparse
import contextlib
import difflib
import json
import os
import sys
import tempfile
import unicodedata
from fnmatch import fnmatch
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

import _guard_core as guard  # ty: ignore[unresolved-import]  # copied beside this script at install time

RULES_FILE_NAME = "rules.json"
STATE_FILE_NAME = guard.STATE_FILE_NAME
GUARD_HOOK_IDS = ("security-prompt-guard", "security-tool-guard")
PROMPT_SECTION = "提示词层"
TOOL_SECTION = "工具调用前"
WRITE_SCOPE_DETECTORS = ("write_scope", "shell_write_target")
"""受"写入范围"设置影响的检测器。

这两类检测器共用 ``settings.write_scope`` 的判定：限制开启时，写到工作区
以外会被 ``T-FS-01`` / ``T-SHELL-05`` 拦下；限制关闭时它们不会触发。
"""


def _out(message: str = "", *, end: str = "\n") -> None:
    """把一行结果写到标准输出（本机命令行工具的正常输出，不用 ``print``）。"""
    sys.stdout.write(f"{message}{end}")


def _err(message: str = "") -> None:
    """把一行诊断写到标准错误。"""
    sys.stderr.write(f"{message}\n")


# ---------------------------------------------------------------------------
# 路径
# ---------------------------------------------------------------------------


def hooks_dir() -> Path:
    """全局钩子目录（Windows ``%APPDATA%\\chrys\\hooks``，其余 ``~/.chrys/hooks``）。"""
    if sys.platform == "win32":
        appdata = os.environ.get("APPDATA")
        if appdata:
            return Path(appdata) / "chrys" / "hooks"
    return Path.home() / ".chrys" / "hooks"


def default_rules_path() -> tuple[Path, bool]:
    """默认操作的规则文件：已安装副本优先，否则用脚本同目录的那份。

    返回 ``(路径, 是否已安装副本)``——护栏运行时读的是已安装的那份。
    """
    installed = hooks_dir() / "guard" / RULES_FILE_NAME
    if installed.is_file():
        return installed, True
    return Path(__file__).resolve().parent / RULES_FILE_NAME, False


def resolve_paths(args: argparse.Namespace) -> tuple[Path, Path, str]:
    """解析规则文件与状态文件路径。

    返回 ``(规则文件, 状态文件, 来源)``；来源是 ``installed``（已安装副本）、
    ``script``（脚本同目录的副本）或 ``explicit``（命令行显式指定）。
    """
    if args.rules_file:
        rules_path = Path(args.rules_file).expanduser()
        origin = "explicit"
    else:
        rules_path, installed = default_rules_path()
        origin = "installed" if installed else "script"
    state_path = Path(args.state_file).expanduser() if args.state_file else rules_path.parent / STATE_FILE_NAME
    return rules_path, state_path, origin


# ---------------------------------------------------------------------------
# 规则与状态
# ---------------------------------------------------------------------------


class RuleView:
    """一条规则的只读视图（ID / 名称 / 处置 / 出厂开关 / 备注）。"""

    __slots__ = ("disposition", "factory_enabled", "id", "name", "note", "scope_dependent", "section")

    def __init__(self, rule: dict[str, Any], section: str) -> None:
        self.id = str(rule.get("id") or "")
        self.name = str(rule.get("name") or self.id)
        self.disposition = str(rule.get("disposition") or "confirm")
        self.factory_enabled = bool(rule.get("enabled", True))
        self.note = str(rule.get("note") or "")
        self.section = section
        self.scope_dependent = any(
            isinstance(item, dict) and str(item.get("type") or "") in WRITE_SCOPE_DETECTORS
            for item in (rule.get("detect") or [])
        )


def rule_views(rules: dict[str, Any]) -> list[RuleView]:
    """把规则文件里的提示词层与工具层规则摊平成一个列表。"""
    views: list[RuleView] = []
    for key, section in (("prompt_rules", PROMPT_SECTION), ("tool_rules", TOOL_SECTION)):
        views.extend(RuleView(rule, section) for rule in rules.get(key) or [] if isinstance(rule, dict))
    return views


def raw_rule(rules: dict[str, Any], rule_id: str) -> dict[str, Any] | None:
    """从规则文件里取一条规则的原始定义（不做任何覆盖改写）。"""
    for key in ("prompt_rules", "tool_rules"):
        for rule in rules.get(key) or []:
            if isinstance(rule, dict) and str(rule.get("id") or "") == rule_id:
                return rule
    return None


def load_json(path: Path) -> dict[str, Any]:
    """读一个 JSON 对象文件。"""
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise TypeError(f"顶层必须是对象：{path}")
    return data


def load_state(path: Path) -> dict[str, Any]:
    """读状态文件；文件不存在 = 没改过开关 = 空覆盖。"""
    if not path.is_file():
        return {}
    return load_json(path)


def save_state(path: Path, state: dict[str, Any]) -> None:
    """原子写状态文件（同目录临时文件 + ``os.replace``）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    payload_state = {"version": 1, **state}
    payload = json.dumps(payload_state, ensure_ascii=False, indent=2) + "\n"
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(payload)
        os.replace(temporary, path)
    finally:
        with contextlib.suppress(OSError):
            os.unlink(temporary)


def engine_config(
    rules: dict[str, Any], state: dict[str, Any], rules_path: Path, state_path: Path
) -> guard.GuardConfig:
    """构造一个与引擎同源的配置视图，开关语义与运行时完全一致。"""
    config = guard.GuardConfig(rules, path=rules_path, state=state, state_path=state_path)
    guard.apply_state(config)
    return config


def overrides_of(state: dict[str, Any]) -> dict[str, Any]:
    """状态文件里的规则覆盖表。"""
    value = state.get("rules")
    return dict(value) if isinstance(value, dict) else {}


def effective_enabled(view: RuleView, config: guard.GuardConfig) -> bool:
    """一条规则当前是否生效：状态文件的覆盖优先于出厂的 ``enabled``。"""
    override = config.rule_override(view.id)
    return view.factory_enabled if override is None else override


def resolve_rule_ids(views: list[RuleView], candidates: list[str]) -> tuple[list[str], list[str]]:
    """把用户给的规则 ID 或通配符解析成规则 ID 列表。

    返回 ``(命中的 ID, 没命中的输入)``；命中结果去重且保持出现顺序。
    """
    known = [view.id for view in views if view.id]
    matched: list[str] = []
    missing: list[str] = []
    for candidate in candidates:
        wanted = candidate.strip().upper()
        if wanted in known:
            matched.append(wanted)
            continue
        hits = [rule_id for rule_id in known if fnmatch(rule_id, wanted)]
        if hits:
            matched.extend(hits)
        else:
            missing.append(candidate)
    return list(dict.fromkeys(matched)), missing


# ---------------------------------------------------------------------------
# 输出
# ---------------------------------------------------------------------------


def display_width(text: str) -> int:
    """终端显示宽度：全角字符按 2 列算。"""
    return sum(2 if unicodedata.east_asian_width(char) in "WF" else 1 for char in text)


def pad(text: str, width: int) -> str:
    """按显示宽度右补空格。"""
    return text + " " * max(0, width - display_width(text))


def print_table(rows: list[tuple[str, ...]]) -> None:
    """打印一张等宽表格。"""
    if not rows:
        return
    widths = [display_width(cell) for cell in rows[0]]
    for row in rows:
        for index, cell in enumerate(row):
            widths[index] = max(widths[index], display_width(cell))
    for index, row in enumerate(rows):
        _out("  ".join(pad(cell, widths[position]) for position, cell in enumerate(row)).rstrip())
        if index == 0:
            _out("  ".join("-" * width for width in widths))


def hook_install_state() -> str:
    """护栏的两个 hook 是否已经装在钩子配置里。"""
    config_file = hooks_dir() / "hooks.yaml"
    if not config_file.is_file():
        return "未安装（钩子配置文件不存在）"
    try:
        text = config_file.read_text(encoding="utf-8-sig")
    except OSError:
        return "未知（钩子配置读不出来）"
    missing = [hook_id for hook_id in GUARD_HOOK_IDS if hook_id not in text]
    if missing:
        return f"未安装（缺 {'、'.join(missing)}）"
    return "已安装"


# ---------------------------------------------------------------------------
# 子命令
# ---------------------------------------------------------------------------


def cmd_status(
    views: list[RuleView],
    config: guard.GuardConfig,
    rules_path: Path,
    state_path: Path,
    origin: str,
) -> int:
    """打印开关总览。"""
    on = [view for view in views if effective_enabled(view, config)]
    off = [view for view in views if not effective_enabled(view, config)]

    _out(f"安全护栏总开关：{'开' if config.master_enabled else '关'}")
    _out(f"写入范围：{'仅限工作区内' if config.write_scope_restricted else '不限工作区'}")
    _out(f"规则文件：{rules_path}" + ("（未安装，用的是脚本同目录的副本）" if origin == "script" else ""))
    _out(f"开关状态：{state_path}" + ("" if state_path.is_file() else "（未创建，用出厂默认）"))
    _out(f"Hook 安装：{hook_install_state()}")
    _out(f"规则：{len(views)} 条 · 开 {len(on)} · 关 {len(off)}")
    if not config.master_enabled:
        _out("提醒：总开关是关的，护栏当前不拦截任何请求。")
    if not config.write_scope_restricted:
        _out("提醒：写入范围不限工作区，`T-FS-01` / `T-SHELL-05` 不会触发。")
    if off:
        _out("已关闭的规则：")
        print_table([(view.id, view.name, view.disposition) for view in off])
    return 0


def cmd_list(
    views: list[RuleView],
    config: guard.GuardConfig,
    pattern: str,
) -> int:
    """列出规则与开关状态。"""
    wanted = pattern.strip().upper() if pattern else ""
    selected = [view for view in views if not wanted or fnmatch(view.id, wanted)]
    if not selected:
        _err(f"没有匹配 {pattern!r} 的规则。")
        return 1
    rows: list[tuple[str, ...]] = [("状态", "规则 ID", "名称", "处置", "备注")]
    inert = False
    for view in selected:
        effective = effective_enabled(view, config)
        note = view.note
        if view.scope_dependent and not config.write_scope_restricted:
            inert = True
            note = f"{note}；写入范围不限工作区，本规则当前不生效" if note else "写入范围不限工作区，本规则当前不生效"
        rows.append(("开" if effective else "关", view.id, view.name, view.disposition, note))
    print_table(rows)
    if inert:
        _out("注：写入范围不限工作区（出厂行为）。`guardctl.py workspace restrict` 可恢复限制。")
    return 0


def cmd_show(rules: dict[str, Any], views: list[RuleView], config: guard.GuardConfig, rule_id: str) -> int:
    """打印一条规则的完整定义——照着改 ``rules.json`` 用。"""
    wanted = rule_id.strip().upper()
    matched = [view for view in views if fnmatch(view.id, wanted)]
    if not matched:
        _err(f"没有这样的规则：{rule_id}")
        known = [view.id for view in views]
        suggestions = difflib.get_close_matches(wanted, known, n=3, cutoff=0.4)
        if suggestions:
            _err(f"  是不是想写：{'、'.join(suggestions)}")
        return 1
    for view in matched:
        raw = raw_rule(rules, view.id) or {}
        override = config.rule_override(view.id)
        _out(f"# {view.id}  {view.name}（{view.section}）")
        _out(f"# 出厂开关：{'开' if view.factory_enabled else '关'}", end="")
        _out("" if override is None else f" · 现场覆盖：{'开' if override else '关'}")
        _out(f"# 当前生效：{'开' if effective_enabled(view, config) else '关'}")
        _out(json.dumps(raw, ensure_ascii=False, indent=2))
    return 0


def cmd_workspace(state: dict[str, Any], state_path: Path, mode: str) -> int:
    """开关写入范围限制。"""
    restricted = mode == "restrict"
    new_state = dict(state)
    scope = dict(new_state.get("write_scope") or {})
    scope["allow_outside_workspace"] = not restricted
    new_state["write_scope"] = scope
    save_state(state_path, new_state)
    if restricted:
        _out(f"已限制写入范围：只允许写工作区内（{state_path}）")
        _out("`T-FS-01` / `T-SHELL-05` 重新生效。")
    else:
        _out(f"已放开写入范围：写工作区外也放行（{state_path}）")
        _out("`T-FS-01` / `T-SHELL-05` 当前不会触发。")
    return 0


def cmd_master(state: dict[str, Any], state_path: Path, value: bool) -> int:
    """开关总开关。"""
    new_state = dict(state)
    new_state["enabled"] = value
    save_state(state_path, new_state)
    _out(f"总开关已{'打开' if value else '关闭'}：{state_path}")
    if not value:
        _out("护栏不再拦截任何请求；`guardctl.py on` 可恢复。")
    return 0


def cmd_rules(
    views: list[RuleView],
    state: dict[str, Any],
    state_path: Path,
    candidates: list[str],
    value: bool,
) -> int:
    """开关一批规则。"""
    matched, missing = resolve_rule_ids(views, candidates)
    if missing:
        known = [view.id for view in views]
        for name in missing:
            _err(f"没有这样的规则：{name}")
            suggestions = difflib.get_close_matches(name.upper(), known, n=3, cutoff=0.4)
            if suggestions:
                _err(f"  是不是想写：{'、'.join(suggestions)}")
        return 1
    overrides = overrides_of(state)
    for rule_id in matched:
        overrides[rule_id] = value
    new_state = dict(state)
    new_state["rules"] = overrides
    save_state(state_path, new_state)
    _out(f"已{'打开' if value else '关闭'} {len(matched)} 条规则：{'、'.join(matched)}")
    _out(f"状态文件：{state_path}")
    return 0


def cmd_reset(
    views: list[RuleView],
    state: dict[str, Any],
    state_path: Path,
    candidates: list[str],
    master: bool,
) -> int:
    """清除开关覆盖，回到出厂状态。"""
    new_state = dict(state)
    overrides = overrides_of(state)
    if candidates:
        matched, missing = resolve_rule_ids(views, candidates)
        if missing:
            for name in missing:
                _err(f"没有这样的规则：{name}")
            return 1
        for rule_id in matched:
            overrides.pop(rule_id, None)
    else:
        overrides = {}
        # 不指定规则时是“清干净”：写入范围开关也一起回到出厂。
        new_state.pop("write_scope", None)
    if "rules" in new_state or overrides:
        new_state["rules"] = overrides
    if master:
        new_state.pop("enabled", None)
    save_state(state_path, new_state)
    _out("已清除开关覆盖，回到出厂状态。" if (candidates or master) else "已清除全部规则与写入范围开关覆盖。")
    _out(f"状态文件：{state_path}")
    return 0


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    """命令行解析器。"""
    parser = argparse.ArgumentParser(
        prog="guardctl.py",
        description="安全护栏开关管理：总开关（默认开）、单条规则开关与写入范围开关。",
    )
    parser.add_argument(
        "--rules", metavar="PATH", dest="rules_file", help="规则文件路径（默认：已安装副本，其次脚本同目录）"
    )
    parser.add_argument("--state", metavar="PATH", dest="state_file", help="状态文件路径（默认：与规则文件同目录）")
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("status", help="显示总开关与规则开关总览")
    listing = sub.add_parser("list", help="列出规则及其开关状态")
    listing.add_argument("pattern", nargs="?", help="只列出匹配的规则（支持通配符，如 T-SHELL-03-*）")
    showing = sub.add_parser("show", help="打印一条规则的完整定义（照着改 rules.json 用）")
    showing.add_argument("rule_id", metavar="RULE", help="规则 ID 或通配符")
    sub.add_parser("on", help="打开总开关（默认就是开）")
    sub.add_parser("off", help="关闭总开关：整个规则包停用")
    for name, help_text in (("enable", "打开指定规则"), ("disable", "关闭指定规则")):
        toggler = sub.add_parser(name, help=help_text)
        toggler.add_argument("rule_ids", nargs="+", metavar="RULE", help="规则 ID 或通配符（如 T-SHELL-03-*）")
    workspace = sub.add_parser("workspace", help="开关写入范围限制")
    workspace.add_argument(
        "mode",
        choices=("restrict", "allow"),
        help="restrict=只允许写工作区内（T-FS-01 / T-SHELL-05 生效）；allow=不限制（出厂行为）",
    )
    reset = sub.add_parser("reset", help="清除开关覆盖，回到出厂状态")
    reset.add_argument("rule_ids", nargs="*", metavar="RULE", help="只清除这些规则的覆盖（缺省=全部规则）")
    reset.add_argument("--master", action="store_true", help="同时清除总开关覆盖")
    return parser


def main(argv: list[str] | None = None) -> int:
    """执行命令；返回进程退出码。"""
    args = build_parser().parse_args(argv)
    rules_path, state_path, origin = resolve_paths(args)
    try:
        rules = load_json(rules_path)
    except (OSError, TypeError, ValueError) as exc:
        _err(f"读取规则文件失败：{rules_path}（{exc}）")
        return 1
    try:
        state = load_state(state_path)
    except (OSError, TypeError, ValueError) as exc:
        _err(f"读取状态文件失败：{state_path}（{exc}）")
        return 1

    views = rule_views(rules)
    if not views:
        _err(f"规则文件里没有规则：{rules_path}")
        return 1
    config = engine_config(rules, state, rules_path, state_path)

    command = args.command or "status"
    if command == "status":
        return cmd_status(views, config, rules_path, state_path, origin)
    if command == "list":
        return cmd_list(views, config, args.pattern or "")
    if command == "show":
        return cmd_show(rules, views, config, args.rule_id)
    if command == "on":
        return cmd_master(state, state_path, True)
    if command == "off":
        return cmd_master(state, state_path, False)
    if command == "enable":
        return cmd_rules(views, state, state_path, args.rule_ids, True)
    if command == "disable":
        return cmd_rules(views, state, state_path, args.rule_ids, False)
    if command == "workspace":
        return cmd_workspace(state, state_path, args.mode)
    if command == "reset":
        return cmd_reset(views, state, state_path, args.rule_ids, args.master)
    _err(f"未知命令：{command}")
    return 2


def _relax_console_encoding() -> None:
    """控制台编码容错：Windows 中文控制台是 GBK，输出生僻字会抛异常。

    只把编不出来的字符替换掉，不改编码本身。
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            with contextlib.suppress(OSError, ValueError):
                reconfigure(errors="replace")


if __name__ == "__main__":
    _relax_console_encoding()
    raise SystemExit(main())
