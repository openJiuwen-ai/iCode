# iCode 安全拦截 Hook 规则包

把《拦截规则一览表.xlsx》的**提示词层**和**工具调用前**两层规则，做成两个 iCode
阻塞式 Hook。流式输出层（STREAM_CHUNK）本次不实现。

* 与主程序零耦合：不 import 任何 `chrys` 包，只用标准库，不改动 `src/chrys` 任何文件。
* 规则与词典全部外置在 `rules.json`，改词典不需要改代码，下次 Hook 触发即生效。
* 命中即**打断 + 一句话提示**：Hook 写 `{"action": "block", "reason": ...}`，
  iCode 把 `reason` 推给用户。提示固定为 `操作不合规，请修改后重试。`——命中了哪几条
  规则、证据是什么不往界面里塞，那些明细完整写在留痕里。

## 目录内容

| 文件 | 作用 |
| --- | --- |
| `aixcoding/tools/guard_install_merge.py` | 特殊环境手工安装：复制 payload 到 `hooks/guard/`，把两个 hook **合并**进已有 `hooks.yaml`（正常升级由启动自装器完成） |
| `hooks.yaml` | Hook 配置片段（安装脚本从这里取 `hooks` 列表） |
| `_prompt_guard.py` | `user_prompt_submit` 入口（规则 IN-01 ~ IN-09） |
| `_tool_guard.py` | `before_tool_call` 入口（规则 T-*） |
| `_guard_core.py` | 共享引擎：加载规则、匹配、留痕、写决策 |
| `rules.json` | **唯一需要维护的文件**：词典 + 规则 + 全局设置 |
| `aixcoding/tests/guard/guard_pack_selftest.py` | 标准库自检（70 条，每条规则一个正例，含若干"日常开发不应被拦"的负例） |
| [OPERATIONS.md](OPERATIONS.md) | 使用与运维手册：拦截清单、配置、排查、**版本升级迁移** |
| [PORTING.md](PORTING.md) | **新拉一份 iCode 源码怎么复现拦截**：文件放哪、hook 配什么、怎么生效 |
| [MANUAL_TESTS.md](MANUAL_TESTS.md) | 90 条手工测试用例与预期结果（预期均为实测） |

> 日常使用、排查与版本升级迁移见 [OPERATIONS.md](OPERATIONS.md)。

## 安装

**用合并安装脚本**：多数机器的 `<config_dir>/hooks/hooks.yaml` 里已经存在其他 hook
（遥测、审计等），直接覆盖会把它们全部弄丢。脚本的做法是把 4 个文件复制到
`hooks/guard/` 子目录，再把两个 hook **追加**到已有的 `hooks` 列表末尾：

```bash
uv run python -m aixcoding.tools.guard_install_merge  # 特殊环境手工安装（幂等）
uv run aixcoding-cli hook validate                    # 期望 PASS
uv run aixcoding-cli hook list                        # 期望看到两个 security-* 条目
```

写入前自动备份为 `hooks.yaml.pre-guard`；回退时把它拷回 `hooks.yaml` 即可。
只查看安装状态而不改文件：

```bash
uv run python -m aixcoding.tools.guard_install_merge --check
```

### 解释器策略：执行时现找 python

iCode 选 hook 解释器的顺序是 `uv` → `python3`/`python` → 自身运行时，它**不过滤**
Windows 应用商店的 0 字节转发存根（`…\Microsoft\WindowsApps\python.exe`）。选中那个存根，
hook 就起不来（`exit_code=9009`），而 `on_error: block` 会把那一轮直接拦下——表现就是**每一句
话都被自己的安全策略拦掉**。

所以安装脚本把两个 hook 的 `run` 写成 **PowerShell 启动器**（`type: command`）：hook 每次执行
时现找一个真实的 python——先 PATH（跳过 0 字节存根），再打包版 iCode 首次运行自动解压的内嵌
解释器 `%LOCALAPPDATA%\pyapp\data\chrys\<hash>\<version>\python\python.exe`：

```yaml
run:
  type: command
  argv:
    - 'C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe'
    - '-NoProfile'
    - '-NonInteractive'
    - '-EncodedCommand'
    - '<找 python 并跑脚本的启动器，完整内容见仓库 hooks.yaml>'
  env:
    GUARD_SCRIPT: _prompt_guard.py
```

配置因此**完全可移植**：换一台电脑把整个 `%APPDATA%\chrys\hooks\` 目录（含 `guard/`）拷过去
就能用，不需要那台机器预装 Python，也不需要重跑安装脚本。找不到解释器时启动器报错退出，
配合 `on_error: block` 保持失败关闭（fail-closed）。

想固定用一个解释器（比如项目 `.venv`）：

```bash
uv run python -m aixcoding.tools.guard_install_merge --python C:\path\to\python.exe
```

Linux / macOS 没有 PowerShell：安装脚本会自动把 `run` 改回 `type: script`（POSIX 没有商店
存根问题）；手动拷贝模板时才需要自己改。

手动安装（仅适用于 `hooks.yaml` 里没有其他 hook 的情况）：把
`_prompt_guard.py`、`_tool_guard.py`、`_guard_core.py`、`rules.json` 放到
`<config_dir>/hooks/guard/`，并把 `hooks.yaml` 的 `hooks` 列表追加到配置里。全局目录是
`~/.chrys/hooks/`（Windows 为 `%APPDATA%\chrys\hooks\`）。模板本身就是启动器形态，Windows 上
直接可用；Linux / macOS 上请把两条 hook 的 `run` 改回 `type: script` + `path: guard/*.py`。

项目级安装：把同样 4 个文件放到 `<workspace>/.chrys/hooks/`，并在设置里打开
`project.hooks_enabled`（项目文件默认不加载）。注意启动器按 `%APPDATA%` 找脚本，项目级
hook 请把 `run` 改回 `type: script` 或自行配置路径。

装完后需要**重启 iCode 或切换会话**才会重新加载钩子配置（只改脚本不需要）。

要求：脚本只依赖标准库，兼容 Python 3.9+；解释器由启动器在 hook 执行时现找（见上）。

## 自检

```bash
uv run python -m aixcoding.tests.guard.guard_pack_selftest -v
```

60 个用例：每条规则各一个正例，外加 `ls -la` / `git status` / `uv run pytest ...` / 读写工作区内 `.py`、访问 `127.0.0.1` 等**必须放行**的负例，
最后 4 个用例按 iCode 的真实子进程协议（`CHRYS_HOOK_PAYLOAD_FILE` /
`CHRYS_HOOK_RESULT`）跑一遍两个入口脚本，另有一组用例盯安装脚本的解释器探测与启动器改写。

改完 `rules.json` 先跑一遍自检，确认没有把正常会话打死。

## 规则映射

### 提示词层（`user_prompt_submit`）

| 规则表行 | 规则 ID | 处置 |
| --- | --- | --- |
| 反编译工具 | `IN-01` | confirm |
| 攻击工具 | `IN-02` | confirm |
| SQL 注入 payload | `IN-03` | confirm |
| XSS payload | `IN-04` | confirm |
| 路径穿越 / LFI | `IN-05` | confirm |
| 内网资产 | `IN-06` | confirm |
| 同义语义簇 | `IN-07` | confirm |
| 合规类 | `IN-08` | confirm + 强制留痕 |
| 失败兜底 | `IN-09` | block |

`IN-09` 没有检测项：它由两层机制落地——脚本内部异常时走 `_fail_closed()`，
子进程超时 / 崩溃由 `hooks.yaml` 的 `timeout_seconds: 5` + `on_error: block` 兜住。

### 工具调用层（`before_tool_call`）

| 规则表行 | 规则 ID | 处置 |
| --- | --- | --- |
| 危险命令（rm -rf / mkfs / dd / sudo / 反弹 shell / 覆写系统文件 …） | `T-SHELL-01` | block |
| Zsh 攻击（等号扩展 / Null byte / 零宽字符 / glob / 进程替换 / autoload） | `T-SHELL-02-1` ~ `-6` | confirm / block |
| 命令链接 / 反引号 / 嵌套 / here-doc / base64 -d | `T-SHELL-03-1` ~ `-5` | confirm |
| Shell 内建（eval / source / exec / trap） | `T-SHELL-04` | confirm |
| USB 审计取证 | `T-PS-01` | block |
| 逆向工具 | `T-PS-02` | block |
| 反调试 API | `T-PS-03` | confirm |
| Python 逆向库 / 字节码自省 / 反编译器 | `T-PY-01` ~ `-03` | block |
| 写入白名单 | `T-FS-01` | block |
| 文件路径黑名单 | `T-FS-02` | block |
| 扩展名黑名单（拒打开） | `T-FS-03` | block |
| 网络黑名单（内网资产 / 业务系统 / 危险端口） | `T-NET-01` | block |
| 网络默认处置（非白名单地址） | `T-NET-02` | confirm |
| MCP Server 白名单 / Schema / 调用方上下文 / 沙箱静态扫描 | `T-MCP-01` ~ `-04` | block |
| 敏感信息进入调用参数 | `T-OUT-01` | block |
| 高熵长串 | `T-OUT-02` | confirm（**默认关闭**） |

网络两条规则的求值顺序：**黑名单先判**（命中即 block），命中内网子网白名单
（`127/8`、`::1`、`10/8`、`172.16/12`、`192.168/16`）放行，其余地址走"默认处置"。

MCP 的 5 道校验里，**sha256 签名校验没有在 Hook 层实现**：Hook 只能看到 iCode 转发
出来的工具名和入参，拿不到 MCP 服务端的制品摘要。这一项需要 MCP 服务端或网关侧配合，
本包不提供假的替代实现。其余 4 项都在 `T-MCP-01` ~ `T-MCP-04` 中。

## 调优

### 提示文案

用户看到的只有一句：**操作不合规，请修改后重试。**

命中了哪些规则（`IN-01`、`T-SHELL-01` …）与命中证据不进界面，完整写在
`settings.audit_log` 指向的留痕文件里，需要排查时看那里。文案在 `rules.json`
的 `settings.block_message` 里改，不用动代码：

```json
"block_message": "操作不合规，请修改后重试。"
```

工具层被拒时，模型会收到 `Error: 操作不合规，请修改后重试。`，因此它能知道要改，
但同样看不到规则明细。

### `confirm` 为什么是"打断"

iCode 以子进程方式运行 Hook，子进程的 `stdin` 是 `DEVNULL`，**Hook 里弹不出确认框**。
所以规则表里的 `confirm` 目前统一按"打断 + 中文提示"落地，用户看到提示后自行调整请求
或联系管理员放行。

想改成"只提示不打断"，改 `rules.json` 里的一个开关即可：

```json
"confirm_disposition": "notify"
```

`block`（默认）= 打断；`notify` = 记录留痕，提示词层额外给模型注入一条
`<system-reminder>`，工具层静默放行。

### 高频误报的处理顺序

1. 先把对应规则的 `"enabled": false` 关掉；
2. 或把它的 `"disposition"` 改成 `"notify"`（配合上面的全局开关）；
3. 或直接改 `dictionaries` 里的词条。

出厂配置里最容易误伤的几条，README 与 `rules.json` 的 `note` 字段都已标注：

* `IN-07` 同义语义簇：`拒绝` / `禁用` / `拦截` 在日常对话里很常见。
* `T-SHELL-03-1` 命令链接：`&&`、`;`、`||` 会命中几乎所有复合命令。
* `T-SHELL-04` Shell 内建：`source` / `exec` 在构建脚本里偶尔合法。
* `T-NET-02` 网络默认处置：任何**非内网**地址都要确认，公网访问默认全中。
* `T-FS-01` 写入白名单：写工作区以外的路径（含用户目录）都会被拦。
* `T-FS-02` 路径黑名单：`crowdstrike` / `falcon` / `dlp` 等词若出现在项目目录名里会误伤。
* `T-FS-03` 扩展名黑名单：`grep`/`glob` 不受影响，但 `read_file` 读 `.jar` / `.so` 会中。

`T-OUT-02`（高熵长串）**默认关闭**：正常代码里的 sha256、base64 资源会大量命中。

### `rules.json` 结构

```jsonc
{
  "settings":  { /* 全局开关：留痕路径、confirm 处置、失败兜底、扫描上限、写入/网络/MCP 白名单 */ },
  "dictionaries": { /* 所有词条：关键词、正则、端口、CIDR、扩展名…… */ },
  "prompt_rules": [ /* IN-* */ ],
  "tool_rules":   [ /* T-* */ ]
}
```

单条规则：

```jsonc
{
  "id": "T-SHELL-01",
  "name": "危险命令",              // 出现在用户提示里
  "layer": "Bash",                // 分组标签，仅用于阅读
  "disposition": "block",         // block | confirm
  "enabled": true,
  "audit": true,                  // 命中写留痕
  "applies_to": { "kinds": ["shell"], "arg_keys": ["command", "reason"] },
  "detect": [{ "type": "regex", "dictionary": "shell_dangerous" }],
  "note": "给维护者看的说明"
}
```

`applies_to` 省略 = 对所有工具生效；`arg_keys` 省略 = 扫描全部入参。
检测器类型：`keyword`（按词边界匹配词条）、`regex`、`secret`、`path`、
`extension`、`ip_cidr`、`url`、`write_scope`、`mcp_server`、`mcp_args_schema`、
`caller_context`、`rule_refs`（复用其他规则的检测项）。

词典条目既可以是字符串，也可以是 `{"name": "...", "value": "...", "regex": true}`；
用 `{"type": "regex", "dictionary": "X", "names": ["a", "b"]}` 可以只取词典里的某几条。

### 环境变量（调试用）

| 变量 | 作用 |
| --- | --- |
| `AIXCODING_GUARD_RULES` | 指向另一份 `rules.json` |
| `AIXCODING_GUARD_AUDIT` | 覆盖留痕文件路径 |

### 留痕

命中（block 与 notify）都会追加一行 JSON 到 `settings.audit_log`（默认
`<config_dir>/hooks/guard_audit.jsonl`），字段：`ts` / `layer` / `session_id` /
`profile` / `cwd` / `tool` / `action` / `rules[]`（含每条规则的命中证据）。
写留痕失败只会打 stderr，不影响拦截结果。

## 已知边界

* **流式输出层未实现**：规则表里的 `STREAM_CHUNK` 一层（PE 结构、端点安全产品、
  考试答案、USB 管控绕过等）本次不做。它需要 iCode 侧提供可改写 / 可中断的输出通道；
  当前 Hook 拿不到流式输出，也无法与用户实时交互。
* **只看得到入参**：Hook 触发时工具还没执行，`T-OUT-*` 只能拦"把密钥写进命令 /
  写进文件"这类入参行为，工具返回结果里的敏感信息属于流式输出层的职责。
* **不校验 MCP 制品签名**：见上文。
* **内网 IP 检测只覆盖 IPv4 字面量**（IPv6 内网字面量不参与 `IN-06`）。
* **`url` 参数解析**：只从名字里含 `url` / `uri` / `endpoint` / `host` / `address` /
  `link` 的入参取目标地址；自定义命名的 MCP 工具若用别的参数名，网络规则会漏。

## 自定义示例

给自研项目加一条"禁止操作生产库"的规则：

```jsonc
// dictionaries
"blocked_hosts": [
  "22.122.49.168",
  "prod-db.internal",              // 新增
  { "regex": true, "value": "\\.(?:local|internal|corp|intra|lan)$" }
]
```

改完跑 `uv run python -m aixcoding.tests.guard.guard_pack_selftest`，再 `uv run aixcoding-cli hook validate`。
