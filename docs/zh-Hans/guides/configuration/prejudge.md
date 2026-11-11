# 结构化 Shell Pre-Judge

iCode 可以使用 Tree-sitter 和**固定结构的 YAML 匹配规则**分析 Bash 指令，再进入原有工具审批。功能**默认关闭**，保留原有 Hook、审批流程、DAA（已有授权复用机制）和**执行前** Shell Command Filter 的行为。`enforce` 模式下将原 readonly 规则转写为内置 YAML 数据，并通过新的 AST Parser 和 Policy Engine 执行，不调用旧验证器。

将配置存放在受信任的用户级目录中（macOS/Linux 通常为 `~/.chrys/prejudge.yaml`），不会从项目工作目录加载策略。

```yaml
schema_version: 1
mode: "shadow"
policy_id: example-v1
rules:
  - id: block-rm
    effect: block
    target: command
    match:
      prefix_first: [rm]
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

每条规则的 `match` 只有一个顶层字段。除原有字面量 Matcher 外，现支持有界的结构化条件组合（并非通用表达式语言）：

| 字段 | 匹配条件 | 允许效果 |
| --- | --- | --- |
| `argv_exact: [git, status]` | 完整静态参数列表与给定序列完全相等 | ALLOW 或 BLOCK |
| `argv_first: [echo]` | 完整静态参数列表的第一个 token 相等 | **仅 ALLOW** |
| `prefix_first: [rm]` | 可确定的静态前缀首 token 相等 | **只允许 BLOCK** |
| `command_in: [ls, cat]` | 命令名称属于给定集合 | 仅 ALLOW |
| `args_none_of: [--delete]` | 参数中不含危险 token | 仅 ALLOW |
| `all: [{command_in: [ls]}, {args_none_of: [--delete]}]` | 对已证明静态的 argv 组合判断 | 仅 ALLOW |
| `cli: {command: git, subcommand: status, ...}` | 声明式匹配子命令、允许选项及必要标志 | 仅 ALLOW |

字面量 token-list Matcher 必须使用非空字符串**列表**，即使只有一个 token；新增结构化 Matcher 的字段与嵌套条件也经过严格校验，仅在完整静态 argv 已证明时用于用户 ALLOW。`argv_first: [git]` 的 ALLOW 会匹配所有 Git 子命令，包括可能修改数据的操作；对含有高风险参数的命令优先使用 `argv_exact`。不允许将 `argv_first` 用于 BLOCK，因为 `rm "$TARGET"`、`rm *.log` 等动态尾参不会触发阻断；此类规则应使用 `prefix_first`。未知字段、多个匹配字段、空值和非字符串值都会在加载时被拒绝。策略格式版本为 `schema_version: 1`。

### 鉴权语义

BLOCK 优先于 ALLOW。要自动 ALLOW，必须所有可执行 AST 节点匹配 ALLOW、命令 `argv` 完整且语法覆盖完整；否则结构化规则 DEFER，随后会评估 `prejudge_v2/readonly_policy.yaml` 内的**内置 YAML ALLOW 规则**，由**新 AST Parser 与结构化 Matcher**完成判断；生产路径不调用旧 `is_safe_readonly_command()`。内置规则覆盖普通只读命令、危险参数守卫，以及部分 Git、pip、ruff 等命令的选项限制。用户 YAML BLOCK 优先；遇到尚未支持或无法证明安全的语法则 DEFER，进入原有审批流程。用户不需要手动复制内置规则。**静态前缀只能用于 BLOCK**。例如 `echo "rm"` 里的 `rm` 是参数，不是另一个可执行命令。

`mode` 必须加引号，避免 YAML 将 `off` 转成布尔值。`off` 保留旧行为；`shadow` 只记录观察结果；`enforce` 执行明确的 BLOCK 和完全覆盖的 ALLOW。enforce 配置无效时初始化失败；**可解析且明确声明 `shadow`** 的配置若规则无效或缺少依赖，会记录警告并关闭观察。YAML 语法损坏或模式无效时无法可靠确定 `shadow`，配置加载失败。显式 BYPASS 不执行新 BLOCK 规则；无交互的 `icode run` 和 `icode workflow run` 使用 BYPASS。

新 AST Matcher 当前只支持 Bash/Git Bash。zsh、sh、dash、PowerShell、cmd.exe、动态展开、重定向、通配符及多数控制流构造不能通过 AST 规则自动 ALLOW；可靠识别的静态命令仍可触发 BLOCK。不支持的 Shell 方言会记录警告并由新 AST 策略 DEFER；旧 readonly Checker 不再作为兜底调用。匹配采用字面量命令名，不自动规范化可执行文件路径。例如 `prefix_first: [rm]` 不会命中 `/bin/rm -rf x`，而是进入 DEFER（不会自动允许）。像 `command rm`、`env rm`、`time rm` 这样的包装命令按 opaque 处理并 DEFER，不能假设 `prefix_first: [rm]` 会命中包装命令里的 `rm`。本功能不能取代完整的 Shell 安全边界。

**依赖：**启用该功能需要 `tree-sitter==0.25.1` 与 `tree-sitter-bash==0.25.1`。默认 `off` 不会加载 Tree-sitter。依赖锁定和 Linux 离线发行的完整验证仍属于独立发行验收项。

**原有审批流程：**首次 Pre-Judge 在 `before_tool_call` Hook 之后执行。用户在审批窗口主动编辑并批准命令后，会再次运行 Hook，但不会重新进入 Pre-Judge 或 LLM Judge；显式 BYPASS 同样保留原有行为，不运行 YAML BLOCK。这里的 BLOCK 是审批阶段的判定，不是全局不可覆盖的禁止名单。

### 兼容性与当前限制

内置 YAML Policy **重新实现**了部分原 readonly 自动批准能力，但目前尚未达到完整行为等价。安全文件描述符重定向、某些环境变量前缀、复杂的 `find -exec`/`xargs`/`sed`/`awk`、POSIX `for` 循环和非 Bash 方言等，可能从旧版自动 ALLOW 变为 DEFER；这类命令仍走原有审批流程，绝不会因为模型不能确定就自动批准。旧 Checker 只可在测试中作为独立对照，不参与新实现运行。完整历史测试集通过前，不应宣称与旧版完全等价。`argv_exact` 不会把未展开的 glob 当成确定的完整 argv；内置只读规则可以在受控范围内将字面 glob 作为符号参数判断。
