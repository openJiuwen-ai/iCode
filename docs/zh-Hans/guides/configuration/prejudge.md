# 结构化 Shell Pre-Judge

iCode 可以使用 Tree-sitter 和字面量 YAML 规则分析 Bash 指令，再进入原有工具审批。功能**默认关闭**，只返回 **ALLOW** 或 **DEFER**：ALLOW 提供快捷自动审批，DEFER 交给原有审批流程；Pre-Judge 本身不拒绝命令。原有 Hook、审批策略、只读快捷审批、DAA（已有授权复用机制）、已记住的授权和**执行前** Shell Command Filter 的行为均保留。

结构化 ALLOW 仅适用于 iCode 在 Bash 或 Git Bash 中运行的命令；zsh（macOS 默认）和 PowerShell（Windows 默认）始终 DEFER 到普通审批。iCode 使用 `$SHELL` 指定的 shell，因此在 macOS 上请用 `SHELL=/bin/bash icode` 启动；在 Windows 上，只有通过 `git_bash` 工具（安装 Git Bash 后提供）发起的调用才符合条件。

将配置存放在受信任的用户级目录中（macOS/Linux 通常为 `~/.chrys/prejudge.yaml`）。使用该配置目录的所有 Profile 和工作区共享此策略。

```yaml
schema_version: 1
mode: "shadow"
policy_id: example-v1
rules:
  - id: allow-git-status
    effect: allow
    target: command
    match:
      argv_exact: [git, status]
  - id: allow-echo
    effect: allow
    target: command
    match:
      argv_first: [echo]
```

### 匹配规则

每条规则使用 `effect: allow` 和 `target: command`，`match` 只有一个顶层字段，且仅支持下列两种字面量匹配器。DEFER 是无法允许时的默认结果，不是可配置的规则效果。不支持正则表达式、逻辑运算、CLI 参数结构或其他匹配字段：

| 字段 | 匹配条件 | 允许效果 |
| --- | --- | --- |
| `argv_exact: [git, status]` | 完整静态参数列表与给定序列完全相等 | ALLOW |
| `argv_first: [echo]` | 完整静态参数列表的第一个 token 相等 | **仅 ALLOW** |

匹配器必须使用非空字符串**列表**，即使只有一个 token。`argv_first: [git]` 的 ALLOW 会匹配所有参数完整且静态的 Git 子命令，包括可能修改数据的操作；对含有高风险参数的命令优先使用 `argv_exact`。未知字段、多个匹配字段、空值和非字符串值都会在加载时被拒绝。已移除的 `effect: block` 和 `prefix_first` 会明确报错，而非被静默忽略；旧配置中的这些规则需要删除。策略格式版本仍为 `schema_version: 1`。

### 鉴权语义

要由结构化规则 ALLOW，必须所有可执行 AST 节点匹配 ALLOW、命令 `argv` 完整且语法覆盖完整。未匹配、语法损坏、动态展开或不支持的构造均返回 DEFER，继续进入原有审批流程，包括只读快捷审批和敏感数据检查。例如，没有 ALLOW 规则匹配的 `rm x` 会进入正常审批，而不会被 Pre-Judge 拒绝。`ls *.py`、`ls 2>/dev/null`、`sort f` 等原有只读审批仍然有效，配置 `rules: []` 时也是如此。静态前缀不能用于证明 ALLOW；`echo "rm"` 中的 `rm` 是参数，不是另一个可执行命令。

推荐使用 `mode: "off"`、`mode: "shadow"` 或 `mode: "enforce"`。未加引号的 `mode: off`（YAML 将其解析为布尔值 `False`）、`mode: false` 和空文件也会关闭功能，且不会加载 Tree-sitter。`shadow` 记录建议的 ALLOW/DEFER，审批行为保持不变。`enforce` 执行符合条件的 ALLOW。**YAML 语法本身损坏**时，正常审批启动会报包含文件路径的错误，即使原本意图声明 `shadow`。可正常解析的 `shadow` 配置若有无效规则或依赖，会告警并停止观察。无效的 `enforce` 配置会加载失败。诊断包含文件路径、错误类别和适用时的规则序号。BYPASS 跳过配置加载。如果此会话随后切换到 MANUAL/AUTO，届时才加载配置。

AST Matcher 当前只支持 Bash/Git Bash。zsh、sh、dash、PowerShell、cmd.exe、动态展开、重定向、通配符及多数控制流构造均 DEFER。不支持的 Shell 方言会发出警告，说明结构化 ALLOW 不生效，但原有只读快捷审批仍会应用。例如，zsh 的 `ls` 和 PowerShell 的 `Get-ChildItem` 保留原有只读审批行为。匹配采用字面量命令名，不自动规范化可执行文件路径：`rm` 的规则不会匹配 `/bin/rm`。`command`、`env`、`time` 等包装命令也会 DEFER 到原有审批流程。Pre-Judge 是快捷审批层，不是 Shell 安全边界。

### 安装可选解析器

请在运行 iCode 的同一 Python 3.14 环境中安装固定版本。标准 wheel 和离线二进制发行包不包含可选解析器。Off 模式无需解析器。

| 安装方式 | 命令 |
| --- | --- |
| 源码检出 | `uv sync --locked`，然后运行 `uv run icode` |
| uv tool | `uv tool install --reinstall --python 3.14 --with tree-sitter==0.25.1 --with tree-sitter-bash==0.25.1 iCode-TUI` |
| pipx（已安装 iCode-TUI） | `pipx inject iCode-TUI "tree-sitter==0.25.1" "tree-sitter-bash==0.25.1"` |

在相应环境中运行 `python -c "from importlib.metadata import version; print(version('tree-sitter'), version('tree-sitter-bash'))"` 查看版本。源码检出环境使用 `uv run python`。uv tool 安装可通过 `uv tool dir` 找到 `icode-tui` 环境。pipx 安装可用 `pipx runpip iCode-TUI show tree-sitter tree-sitter-bash`。将 wheel 安装到 Python 环境时，请在同一环境中安装解析器。如果离线包不支持安装解析器，请改用可管理的 Python 环境安装。

### 隔离启动

符合条件的 Bash 或 Git Bash 调用由配置的 Shell 以 `--noprofile --norc` 执行原始命令。子进程环境移除 `BASH_ENV`、`ENV` 和全部 `BASH_FUNC_*` 项。iCode 同时检查原始文本和解码参数是否涉及敏感目标。当每条命令都匹配规则时，裸命令名、支持的管道或复合语句均可使用快捷审批。

普通审批和 BYPASS 保留原有启动设置。依赖继承的 Bash 函数或启动脚本的命令，使用结构化快捷审批时可能表现不同。该快捷审批信任系统启动状态、用户配置的环境、`PATH` 和通过它解析到的可执行文件。使用相对查找路径或位于已知工作区内的可执行文件时会 DEFER。请将可信程序和搜索目录放在 Agent 无法修改的位置。继承状态和外部程序可以独立于工具调用发生变化。即使匹配宽泛规则，`open`、`osascript` 和更改执行状态的 Shell 内建命令也会 DEFER。

只要有一个 `PATH` 目录位于工作区内，该启动环境下的命令就无法使用结构化 ALLOW。常见情况包括激活项目的 `.venv`，或以用户主目录作为 iCode 工作区。可在诊断日志中查看 `reason=untrusted_launch_context`。这些调用会继续走普通审批，满足条件时仍可使用原有的只读快捷审批。自定义 Shell 启动参数也可能在执行前触发 DEFER（`reason=unsupported_shell_configuration`）。

iCode 检查原始路径及每一跳链接的文件系统身份。工作区路径的大小写别名和工作区内的链接都会 DEFER，包括指向工作区外的链接。绝对 PATH 项即使不存在，只要已有的上级路径可信，仍可继续判断。PATH 中可信的普通文件不会参与查找。Shell 和显式可执行文件路径必须存在，无法检查的路径仍会 DEFER。iCode 记录已有的目录身份、缺失的后缀和普通文件，并在启动 Shell 前重新检查这些记录及候选可执行文件。任何变化都会使原审批失效。最后一次检查和操作系统启动是两个独立操作，请确保可信目录、链接和可执行文件位于 Agent 无法修改的位置。

`cd` 会改变解析命令和检查敏感目标所用的目录。因此，`cd sub && git status` 这样的复合命令会 DEFER。

### 审批及执行边界

- **Pre-Judge 只返回 ALLOW 或 DEFER。** 本层不产生拒绝或 BLOCK 结果；正常审批和执行检查仍可独立拒绝调用。
- **BYPASS 完全跳过 Structured Pre-Judge，包括 shadow 日志。** `icode run` 与 `icode workflow run` 都使用 BYPASS，`prejudge.yaml` 不改变其审批行为；BYPASS 也不验证这份配置。
- **用户编辑并批准后的命令不会重新鉴权。** 编辑后的命令不会再次经过 Pre-Judge 或 LLM Judge，但仍须经过 `before_tool_call` Hook 和已配置的 Shell Command Filter。
- **Shell Command Filter 在审批之后、实际进程启动之前检查最终命令。** BYPASS 或人工批准都不能覆盖这项检查。过滤器匹配可识别的字面量命令名。
- **审批顺序。** 必须进行的敏感数据检查优先。符合条件的结构化 ALLOW 或原有只读快捷审批可以先于已有授权复用（DAA）、AUTO 审批裁判或人工审批满足 Profile 的 `shell: require`。DEFER 后，原有审批路径仍可能批准调用。参见[配置审批](./approval.md)。

**策略生命周期：** 使用同一 EventBus 的审批中间件共享一份可信策略快照。在 TUI 中，这份快照持续到 iCode 进程退出；`/new`、恢复聊天和切换 Agent 都不会重新加载 `prejudge.yaml`，需要退出并重启 iCode 才能应用修改。主 Agent、Sub-Agent 和 Workflow 使用同一快照。若 iCode 以 BYPASS 开始，在首次切换到 MANUAL/AUTO 时才加载快照。

如果首次切换时策略加载失败，所选的 MANUAL/AUTO 模式仍然生效。TUI 或 ACP 客户端会收到含配置路径的警告。此策略快照停用结构化 ALLOW，原有审批流程及其只读快捷审批继续工作。修复 YAML 或解析器安装后，重启 iCode 即可重试加载。正常启动时，无效的已启用策略会抛出包含路径的错误。

**Shadow 日志。** Shadow 和 enforce 使用相同的资格判断，审批结果保持不变。TUI 用户可在 iCode 配置目录的 `logs/debug.log` 中搜索 `shadow route=`。ACP 的 stderr 不输出 INFO。需要查看 ACP 的 Shadow 判定时，可设置 `CHRYS_OTEL=true`，同时保持遥测端点和敏感数据采集未配置，然后查看会话的 `otel/logs.jsonl`。诊断包含判定、原因、策略及规则 ID、匹配范围和分析摘要。判断失败时只记录模式和异常类型。所有日志级别均省略命令预览和策略内容片段。无界面 CLI 使用 BYPASS。

INFO 日志的 `policy_candidate` 表示 AST 和 YAML 规则在敏感目标及启动检查之前的结果，`route` 则预测是否能使用结构化快捷审批。`decoded_sensitive_target=true` 表示解码参数检查发现了原始文本检查遗漏的敏感目标。在 enforce 模式下，即使没有 YAML 规则匹配，过去自动运行的调用也可能进入普通审批。原有只读快捷审批和已有授权复用也不会自动通过这次调用。Shadow 会记录该发现，实际审批行为保持原样。

Bash 展开在原始命令文本中检查。例如，`grep -F '$DEMO_TOKEN' README.md` 搜索的是字面文本。解码参数中发现的敏感路径仍然需要审批。敏感数据检查也会分析显式传给 `eval` 或已识别 Shell 的 `-c` 参数的源码，包括常见的 `timeout`、`nice` 和 `xargs` 用法。这些命令会从结构化快捷审批 DEFER。

如果 Pre-Judge 无法确定已识别的包装命令可能运行哪个子命令，诊断会记录 `reason=unresolved_carrier`。在 enforce 模式下，即使设置了 `shell: auto` 或规则集为空，调用仍然需要审批。Shadow 会记录相同的发现，审批行为保持不变。

安全检查按可执行文件的基本名称分类，忽略大小写和 `.exe` 后缀。这能覆盖大小写别名，也可能让大小写敏感文件系统上的不同程序触发额外审批。YAML 规则仍按解码后的字面参数匹配，包括大小写和路径。

### 兼容性与当前限制

DEFER 直接复用既有只读检查器，而不再用另一套 YAML 引擎重写它。原有能力和限制均保留，包括各 Shell 的处理方式。这条快捷审批与 `shell_filter: read_only` 预设相互独立，两者语义不同。即使原有快捷审批允许某条命令，AST 层也不会把未展开的 glob 认定为完整的静态 `argv_exact` 参数。兼容性测试覆盖了代表性案例，并不证明所有可能命令都安全。
