# 结构化 Shell Pre-Judge

iCode 可以使用 Tree-sitter 和字面量 YAML 规则分析 Bash 指令，再进入原有工具审批。功能**默认关闭**，只返回 **ALLOW** 或 **DEFER**：ALLOW 提供快捷自动审批，DEFER 交给原有审批流程；Pre-Judge 本身不拒绝命令。原有 Hook、审批策略、只读快捷审批、DAA（已有授权复用机制）、已记住的授权和**执行前** Shell Command Filter 的行为均保留。

将配置存放在受信任的用户级目录中（macOS/Linux 通常为 `~/.chrys/prejudge.yaml`），不会从项目工作目录加载策略。

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

推荐使用 `mode: "off"`、`mode: "shadow"` 或 `mode: "enforce"`。未加引号的 `mode: off`（YAML 将其解析为布尔值 `False`）、`mode: false` 和空文件也会关闭功能，且不会加载 Tree-sitter。`shadow` 只记录建议的 ALLOW/DEFER，不执行快捷审批；`enforce` 执行符合条件的 ALLOW。**YAML 语法本身损坏**时，正常审批启动会报包含文件路径的错误，即使原本意图声明 `shadow`。可正常解析且明确声明 `shadow` 的配置，若规则或依赖无效，会记录含文件路径（和适用时规则 ID）的警告并停止观察；无效的 `enforce` 规则会报包含文件路径和规则 ID 的错误。BYPASS 不会加载或验证 `prejudge.yaml`；如果此会话随后切换到 MANUAL/AUTO，届时才加载配置。

AST Matcher 当前只支持 Bash/Git Bash。zsh、sh、dash、PowerShell、cmd.exe、动态展开、重定向、通配符及多数控制流构造均 DEFER。不支持的 Shell 方言会发出警告，说明结构化 ALLOW 不生效，但原有只读快捷审批仍会应用。例如，zsh 的 `ls` 和 PowerShell 的 `Get-ChildItem` 保留原有只读审批行为。匹配采用字面量命令名，不自动规范化可执行文件路径：`rm` 的规则不会匹配 `/bin/rm`。`command`、`env`、`time` 等包装命令也会 DEFER 到原有审批流程。Pre-Judge 是快捷审批层，不是 Shell 安全边界。

**依赖：**启用该功能需要 `tree-sitter==0.25.1` 与 `tree-sitter-bash==0.25.1`。默认 `off` 不会加载 Tree-sitter。依赖锁定和 Linux 离线发行的完整验证仍属于独立发行验收项。

### 审批及执行边界

- **Pre-Judge 只返回 ALLOW 或 DEFER。** 本层不产生拒绝或 BLOCK 结果；正常审批和执行检查仍可独立拒绝调用。
- **BYPASS 完全跳过 Structured Pre-Judge，包括 shadow 日志。** `icode run` 与 `icode workflow run` 都使用 BYPASS，`prejudge.yaml` 不改变其审批行为；BYPASS 也不验证这份配置。
- **用户编辑并批准后的命令不会重新鉴权。** 编辑后的命令不会再次经过 Pre-Judge 或 LLM Judge，但仍须经过 `before_tool_call` Hook 和已配置的 Shell Command Filter。
- **Shell Command Filter 在人工审批之后、实际进程启动之前检查最终命令。** BYPASS 或人工批准都不能覆盖这项检查。但该过滤器仅匹配可识别的字面量命令名，**不是沙箱**：黑名单 `[rm]` 不能拦截所有 `rm` 调用方式，例如 `$'rm'`、`/bin/rm`、`env rm`。
- **DEFER 不保证弹出人工审批窗口。** Profile override、已有授权复用及 AUTO 模式的 LLM Judge 仍可能批准命令并执行。

**策略生命周期：** 使用同一 EventBus 的审批中间件共享一份可信策略快照。在 TUI 中，这份快照持续到 iCode 进程退出；`/new`、恢复聊天和切换 Agent 都不会重新加载 `prejudge.yaml`，需要退出并重启 iCode 才能应用修改。主 Agent、Sub-Agent 和 Workflow 使用同一快照。若 iCode 以 BYPASS 开始，在首次切换到 MANUAL/AUTO 时才加载快照。

如果首次切换时策略加载失败，所选的 MANUAL/AUTO 模式仍然生效。系统记录错误，并在该 EventBus 生命周期内停用结构化 ALLOW 和只读快捷审批，由原有审批流程决定后续调用。修复配置或解析器安装后，需要重启 iCode 才能重试加载。正常启动时，无效的已启用策略仍会抛出包含路径的错误。

**Shadow 日志：** INFO 日志包括判定、原因、Policy/Rule ID、匹配 span，以及转义换行、最多 512 个字符的命令文本。命令可能含有敏感 Token；应限制日志访问，并避免在命令中放入明文凭据。

### 兼容性与当前限制

DEFER 直接复用既有只读检查器，而不再用另一套 YAML 引擎重写它。原有能力和限制均保留，包括各 Shell 的处理方式。这条快捷审批与 `shell_filter: read_only` 预设相互独立，两者语义不同。即使原有快捷审批允许某条命令，AST 层也不会把未展开的 glob 认定为完整的静态 `argv_exact` 参数。兼容性测试覆盖了代表性案例，并不证明所有可能命令都安全。
