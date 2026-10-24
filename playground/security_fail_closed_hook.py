# Copyright (c) 2026 chana4afk & 四川爱赋康文化科技有限公司. All rights reserved.
# 原始方法学版权：iCode（openJiuwen / Huawei, Apache-2.0），Copyright (c) Huawei Technologies Co., Ltd. 2026；本文件在其方法学基础上脱敏准备，尊重并保留华为原始版权。
#
# 商标声明：iCode、Chrys、openJiuwen 为其各自权利人的商标，本示例未获其背书。
#
# 许可：本示例以 Apache License 2.0 贡献（与上游同许可）。
# 署名：示例贡献者 / 组织（非个人名）；保留上游 NOTICE 精神。
# 贡献声明（Contribution Notice）：本贡献由 chana4afk & 四川爱赋康文化科技有限公司 在 iCode（openJiuwen / Huawei, Apache-2.0）方法学基础上脱敏准备；尊重并保留华为原始版权，依 Apache-2.0 许可向上游提交，CLA 由贡献者签署。

#
# ============================================================================
# 通用「fail-closed（默认拒绝）前置硬卡点」安全钩子 —— 示例骨架
# ============================================================================
# 本文件演示：如何为一个支持「before_tool_call」钩子机制的智能体运行时
# （例如 iCode / Chrys）编写一组 fail-closed 的安全前置卡点。
#
# 这是一套「通用方法论」示例，不绑定任何特定团队、角色名或内部技术资产。
# 设计原则：
#   1) 每个工具调用在执行前都经过若干独立安全检查；
#   2) 任一检查命中风险 -> 显式否决（block）；
#   3) 配置缺失 / 输入解析异常 / 检查进程崩溃 / 退出非 0 -> 同样否决
#      （fail-closed，默认 deny，不允许 fail-open）；
#   4) 放行以「工具白名单 / 授权 profile 清单 / 委托令牌」为依据，
#      钩子本身不给自己造权限。
#
# 钩子契约（兼容 iCode 的 before_tool_call 钩子）：
#   输入：环境变量 CHRYS_HOOK_PAYLOAD_FILE 指向的 JSON 文件，含工具调用信息；
#   输出：环境变量 CHRYS_HOOK_RESULT 指向的文件，写入
#         {"action": "allow"} 或 {"action": "block", "reason": "..."}。
#   （若你的运行时使用不同契约，只需替换 read_payload() / write_result() 两个函数。）
#
# 等保三级通用红线示例（R1–R8）：下列触发词为「通用安全策略示例」，可按需增删，
# 不指向任何专有实现。
# ============================================================================

import json
import os
import re
import sys

PAYLOAD_FILE = os.environ.get("CHRYS_HOOK_PAYLOAD_FILE")
RESULT_FILE = os.environ.get("CHRYS_HOOK_RESULT")


# --------------------------------------------------------------------------
# 0. 钩子契约：读取 / 写出（如需适配其它运行时，只改这里）
# --------------------------------------------------------------------------
def read_payload() -> dict:
    if not PAYLOAD_FILE:
        fail_closed("missing CHRYS_HOOK_PAYLOAD_FILE")
    try:
        with open(PAYLOAD_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as exc:  # noqa: BLE001
        fail_closed(f"payload parse error: {exc}")


def write_result(action: str, reason: str = "") -> None:
    if RESULT_FILE:
        try:
            with open(RESULT_FILE, "w", encoding="utf-8") as f:
                if action == "allow":
                    json.dump({"action": "allow"}, f)
                else:
                    json.dump({"action": "block", "reason": reason}, f)
        except OSError:
            pass
    sys.exit(0)


def fail_closed(reason: str) -> "NoReturn":  # type: ignore[name-defined]
    # 崩溃 / 异常 / 缺环境 -> 退出非 0，运行时须按 fail-closed 同样否决
    sys.stderr.write(f"fail-closed hook ABORT: {reason}\n")
    sys.exit(2)


def allow() -> None:
    write_result("allow")


def block(reason: str) -> None:
    write_result("block", reason)


# --------------------------------------------------------------------------
# 1. 红线全量扫描（R1–R8，等保三级通用红线示例）
# --------------------------------------------------------------------------
REDLINE_TERMS: list[tuple[str, list[str]]] = [
    ("R1", ["武器化 exploit", "免杀载荷生成", "跨平台植入工具包", "远控木马生成器",
            "weaponized exploit", "payload generator", "rat generator", "implant toolkit",
            "远控生成器", "木马生成"]),
    ("R2", ["批量扫描目标站", "未授权渗透测试", "对外部域名自动化挖洞", "注入并回传数据",
            "unauthorized scanning", "automated vulnerability scanning", "inject and exfiltrate",
            "自动化挖洞", "未授权扫描"]),
    ("R3", ["横向移动", "内网漫游", "提权后接管域控", "凭据中继",
            "lateral movement", "internal network roaming", "privilege escalation domain", "credential relay",
            "提权接管"]),
    ("R4", ["批量拉库", "导出全表用户数据", "落盘凭据", "留存密钥快照",
            "bulk data pull", "export full table", "persist credentials", "key snapshot",
            "全表导出", "批量取数"]),
    ("R5", ["先发漏洞细节造势", "PoC 随报告公开", "未修复就披露",
            "disclose vuln before patch", "poc public release", "public before fix"]),
    ("R6", ["帮他把站打下来", "协助绕过验证", "代做攻击脚本",
            "help attack", "assist bypass auth", "write attack script", "代写攻击"]),
    ("R7", ["未经复检直接判干净", "免检放行", "扫描没跑完就出安全结论",
            "declare clean without recheck", "skip-check pass", "未经复检判干净"]),
    ("R8", ["提示词包住就安全", "基准分 90 分所以绝对安全", "跑分达标即无风险",
            "prompt delimiters security", "benchmark score absolutely safe", "跑分即无风险"]),
]


def scan_redline(payload: dict) -> bool:
    blob = json.dumps(payload, ensure_ascii=False).lower()
    for rid, terms in REDLINE_TERMS:
        for term in terms:
            if term.lower() in blob:
                block(f"[{rid}] 命中等保三级通用红线触发词「{term}」，工具调用被否决（fail-closed）")
    return True


# --------------------------------------------------------------------------
# 2. 出站 / 外发内容扫描（密钥 / PII / 批量取数外发）
# --------------------------------------------------------------------------
EGRESS_KINDS = {"shell", "mcp", "network", "http", "web"}
EGRESS_VERBS = ["curl", "wget", "scp", "rsync", "ftp", "upload", "post", "put",
                "gh push", "git push", "回传", "外发", "导出", "上传", "推送",
                "exfiltrate", "send to"]

SECRET_PATTERNS = [
    ("AWS-AKIA", re.compile(r"AKIA[0-9A-Z]{16}")),
    ("AWS-ASIA", re.compile(r"ASIA[0-9A-Z]{16}")),
    ("Slack-Token", re.compile(r"xox[baprs]-[0-9A-Za-z-]{10,}")),
    ("GitHub-Token", re.compile(r"gh[pousr]_[0-9A-Za-z]{20,}")),
    ("Google-Token", re.compile(r"ya29\.[0-9A-Za-z_-]{30,}")),
    ("Stripe-SK", re.compile(r"sk-[A-Za-z0-9]{20,}")),
    ("Google-APIKey", re.compile(r"AIza[0-9A-Za-z_-]{35}")),
    ("PrivateKey", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("AWS-SecretEnv", re.compile(r"aws_secret_access_key\s*=", re.I)),
]

PII_PATTERNS = [
    ("China-ID", re.compile(r"(?<!\d)([1-9]\d{5}(?:19|20)\d{2}(?:0[1-9]|1[0-2])(?:0[1-9]|[12]\d|3[01])\d{3}[\dXx])(?!\d)")),
    ("China-Phone", re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")),
]

PIPL_BULK = ["批量导出", "全量导出", "全表导出", "导出全表", "拉取全量", "批量拉取",
             "export full", "dump all", "bulk export", "全量拉取"]


def scan_outbound(payload: dict) -> bool:
    tool = payload.get("tool") or {}
    kind = (tool.get("kind") or "").lower()
    args = tool.get("args") or {}
    blob = json.dumps(args, ensure_ascii=False)
    blob_lower = blob.lower()

    is_egress = kind in EGRESS_KINDS or any(v in blob_lower for v in EGRESS_VERBS)
    if not is_egress:
        return True

    for _label, pat in SECRET_PATTERNS:
        m = pat.search(blob)
        if m:
            block(f"[凭据外发] 外发载荷命中密钥模式（{m.group(0)[:16]}…），调用被否决（fail-closed）")

    for _label, pat in PII_PATTERNS:
        if pat.search(blob):
            block("[PIPL] 外发载荷命中个人敏感信息，调用被否决（fail-closed）")

    if any(b in blob for b in PIPL_BULK):
        block("[PIPL] 批量取数/全量导出与外发同现，违反最小化原则，调用被否决（fail-closed）")
    return True


# --------------------------------------------------------------------------
# 3. 文件系统写保护（凭据 / 关键系统路径只读或需二次确认）
# --------------------------------------------------------------------------
# 受保护路径（命中即否决，fail-closed）。下方仅列「通用敏感路径」；
# 若你方有特定的运行态状态库 / 智能体记忆文件 / 工具基线文件，请在此追加。
SENSITIVE_RE = re.compile(
    r"""(?ix)
    (?:^|/)
    (?:
        \.ssh | \.aws | \.gnupg | \.config/.*(cred|token) | known_hosts
      | id_(rsa|ed25519|ecdsa|dsa) | \.pem | \.key | \.p12 | \.pfx | \.kdbx
      | credentials | \.env[^/]* | token[s]? | secret[s]?
      | authorized_keys
    )
    (?:\b|/|$)"""
)

SENSITIVE_PREFIXES = ["/etc/", "/proc/", "/sys/", "/boot/", "/usr/sbin/"]


def is_sensitive(path: str) -> bool:
    if not path:
        return False
    p = path.strip().strip("\"'")
    expanded = os.path.expanduser(p) if p.startswith("~") else p
    low = expanded.lower()
    for pref in SENSITIVE_PREFIXES:
        if low.startswith(pref):
            return True
    return bool(SENSITIVE_RE.search(expanded))


WRITE_TARGET_RE = re.compile(
    r"""(?ix)
      (?:
          >\s*([^\s|&;>]+)                         # > file
        | >>\s*([^\s|&;>]+)                        # >> file
        | \btee\s+(?:-[a-z]+\s+)*([^\s|&;>]+)      # tee file
        | \bcat\s+.*?>\s*([^\s|&;>]+)              # cat ... > file
        | \becho\s+.*?>\s*([^\s|&;>]+)             # echo ... > file
        | curl\s+.*?-o\s+([^\s|&;>]+)               # curl -o file
        | wget\s+.*?-O\s+([^\s|&;>]+)               # wget -O file
        | scp\s+.*?\s+([^\s|&:]+)$                 # scp ... dest
        | \bcp\s+.*?\s+([^\s|&;]+)$                 # cp ... dest
        | \bmv\s+.*?\s+([^\s|&;]+)$                 # mv ... dest
      )
    """
)


def scan_fs_write(payload: dict) -> bool:
    tool = payload.get("tool") or {}
    kind = (tool.get("kind") or "").lower()
    args = tool.get("args") or {}

    candidates: list[str] = []
    if kind == "filesystem.write":
        p = args.get("path") or args.get("file_path") or args.get("destination")
        if p:
            candidates.append(str(p))
    elif kind == "shell":
        cmd = args.get("command") or args.get("cmd") or ""
        candidates.extend(g for m in WRITE_TARGET_RE.finditer(cmd or "") for g in m.groups() if g)
    else:
        return True

    for cand in candidates:
        if is_sensitive(cand):
            block(f"[路径护栏] 写入受保护敏感路径被否决：{cand!r}（凭据/关键系统路径只读），fail-closed")
    return True


# --------------------------------------------------------------------------
# 4. 工具白名单（仅放行已注册工具，未知工具 deny）
# --------------------------------------------------------------------------
# 保守基线：常见智能体运行时工具名；生产应由部署侧以 ALLOWED_TOOLS 覆盖为真实注册表。
BASELINE_ALLOWED = {
    "read_file", "edit_file", "write_file", "glob", "grep", "search",
    "ask_user", "sleep", "sub_agent", "doc_converter", "list_dir",
    "shell", "mcp", "filesystem.read", "filesystem.write", "delete",
    "Read", "Write", "Edit", "Grep", "Glob", "Bash", "Task", "TaskCreate",
    "TaskUpdate", "TaskList", "WebFetch", "WebSearch", "NotebookEdit",
}


def allowed_set() -> set[str]:
    env = os.environ.get("ALLOWED_TOOLS", "").strip()
    if env:
        return {t.strip() for t in env.split(",") if t.strip()}
    return set(BASELINE_ALLOWED)


def scan_whitelist(payload: dict) -> bool:
    tool = payload.get("tool") or {}
    name = (tool.get("name") or "").strip()
    kind = (tool.get("kind") or "").strip()
    if not name and not kind:
        block("[白名单] 工具调用缺少 name/kind 标识，无法做白名单判定，调用被否决（fail-closed）")
    allowlist = allowed_set()
    if name in allowlist or kind in allowlist:
        return True
    block(
        f"[白名单] 未注册工具被拦截：name={name!r} kind={kind!r}，"
        f"不在已注册工具白名单内，调用被否决（fail-closed）"
    )


# --------------------------------------------------------------------------
# 5. 权限自检（受控工具种类调用前校验调用者授权或委托令牌）
# --------------------------------------------------------------------------
RESTRICTED_KINDS = {"shell", "filesystem.write", "mcp", "delete", "trash", "network", "web"}

# 默认授权 profile（示例占位，生产须由 AUTH_PROFILES 环境变量注入真实清单）。
# 这里仅用通用占位符，避免绑定任何专有角色名。
DEFAULT_AUTH_PROFILES = {"<delivery-lead>", "<security-agent>", "<quality-agent>",
                        "<legal-agent>", "<architect>"}

TOKEN_PREFIX = "example_"


def authorized_profiles() -> set[str]:
    env = os.environ.get("AUTH_PROFILES", "").strip()
    if env:
        return {p.strip() for p in env.split(",") if p.strip()}
    return set(DEFAULT_AUTH_PROFILES)


def has_valid_token(args: dict) -> bool:
    tok = args.get("delegation_token")
    if isinstance(tok, str) and tok.startswith(TOKEN_PREFIX) and len(tok) > len(TOKEN_PREFIX):
        return True
    return False


def path_under_cwd(path: str, cwd: str) -> bool:
    if not path or not cwd:
        return False
    try:
        p = os.path.abspath(os.path.expanduser(str(path)))
        base = os.path.abspath(str(cwd))
        return os.path.commonpath([p, base]) == base
    except (ValueError, OSError):
        return False


def scan_permission(payload: dict) -> bool:
    tool = payload.get("tool") or {}
    kind = (tool.get("kind") or "").lower()
    args = tool.get("args") or {}
    profile = (payload.get("profile") or "").strip()
    cwd = payload.get("cwd") or ""

    if kind not in RESTRICTED_KINDS:
        return True
    if has_valid_token(args):
        return True

    auth = authorized_profiles()
    if profile in auth:
        if kind == "filesystem.write":
            p = args.get("path") or args.get("file_path") or args.get("destination")
            if p and not path_under_cwd(str(p), cwd):
                block(
                    f"[权限自检] 授权 profile={profile} 但写路径越出工作区，"
                    f"需委托令牌，调用被否决（fail-closed）"
                )
        return True

    block(
        f"[权限自检] 越权即停：受控工具 kind={kind} 由非授权 profile={profile!r} 调用，"
        f"且未持委托令牌，调用被否决（fail-closed）"
    )


# --------------------------------------------------------------------------
# 主调度：按 fail-closed 顺序串联各检查（任一命中即 block 并退出）
# --------------------------------------------------------------------------
def main() -> None:
    payload = read_payload()
    # 顺序：白名单 -> 红线 -> 出站 -> 写保护 -> 权限自检
    scan_whitelist(payload)
    scan_redline(payload)
    scan_outbound(payload)
    scan_fs_write(payload)
    scan_permission(payload)
    allow()


if __name__ == "__main__":
    main()
