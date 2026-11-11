# 结构化 Shell Pre-Judge

iCode 可以使用 Tree-sitter 和**固定结构的 YAML 匹配规则**分析 Bash 指令，再进入原有工具审批。功能**默认关闭**，保留原有 Hook、审批流程、DAA（已有授权复用机制）和**执行前** Shell Command Filter 的行为。`enforce` 模式下由等价的内置策略规则保留原 readonly 快捷批准功能。

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

每条规则的 `match` **必须且只能包含一个**受支持字段，不支持任意表达式、正则、通配符或 AND/OR/NOT：

| 字段 | 匹配条件 | 允许效果 |
| --- | --- | --- |
| `argv_exact: [git, status]` | 完整静态参数列表与给定序列完全相等 | ALLOW 或 BLOCK |
| `argv_first: [echo]` | 完整静态参数列表的第一个 token 相等 | **仅 ALLOW** |
| `prefix_first: [rm]` | 可确定的静态前缀的第一个 token 相等，即使后面有动态参数 | **只允许 BLOCK** |

值必须是非空字符串组成的**列表**，即使只有一个 token。`argv_first: [git]` 的 ALLOW 会匹配所有 Git 子命令，包括可能修改数据的操作；对含有高风险参数的命令优先使用 `argv_exact`。不允许将 `argv_first` 用于 BLOCK，因为 `rm "$TARGET"`、`rm *.log` 等动态尾参不会触发阻断；此类规则应使用 `prefix_first`。未知字段、多个匹配字段、空值和非字符串值都会在加载时被拒绝。策略格式版本为 `schema_version: 1`。

### 鉴权语义

BLOCK 优先于 ALLOW。要自动 ALLOW，必须所有可执行 AST 节点匹配 ALLOW、命令 `argv` 完整且语法覆盖完整；否则结构化规则 DEFER，随后会评估**隐式内置 readonly ALLOW 规则**（`__builtin_readonly__`）：它调用原有 `is_safe_readonly_command()` 对**整条命令**判断，保留 Git 等命令专用验证、通配符和已支持的 Shell 方言。用户 YAML BLOCK 优先于内置 readonly 规则。若内置规则不匹配，继续原有审批流程；无需手工将旧 readonly 规则写入 YAML。**静态前缀只能用于 BLOCK**。例如 `echo "rm"` 里的 `rm` 是参数，不是另一个可执行命令。

`mode` 必须加引号，避免 YAML 将 `off` 转成布尔值。`off` 保留旧行为；`shadow` 只记录观察结果；`enforce` 执行明确的 BLOCK 和完全覆盖的 ALLOW。enforce 配置无效时初始化失败；**可解析且明确声明 `shadow`** 的配置若规则无效或缺少依赖，会记录警告并关闭观察。YAML 语法损坏或模式无效时无法可靠确定 `shadow`，配置加载失败。显式 BYPASS 不执行新 BLOCK 规则；无交互的 `icode run` 和 `icode workflow run` 使用 BYPASS。

新 AST Matcher 当前只支持 Bash/Git Bash。zsh、sh、dash、PowerShell、cmd.exe、动态展开、重定向、通配符及多数控制流构造不能通过 AST 规则自动 ALLOW；可靠识别的静态命令仍可触发 BLOCK。不支持的 Shell 方言会记录警告，但内置 readonly 规则仍沿用原有判断行为。匹配采用字面量命令名，不自动规范化可执行文件路径。例如 `prefix_first: [rm]` 不会命中 `/bin/rm -rf x`，而是进入 DEFER（不会自动允许）。像 `command rm`、`env rm`、`time rm` 这样的包装命令按 opaque 处理并 DEFER，不能假设 `prefix_first: [rm]` 会命中包装命令里的 `rm`。本功能不能取代完整的 Shell 安全边界。

**依赖：**启用该功能需要 `tree-sitter==0.25.1` 与 `tree-sitter-bash==0.25.1`。默认 `off` 不会加载 Tree-sitter。依赖锁定和 Linux 离线发行的完整验证仍属于独立发行验收项。

**原有审批流程：**首次 Pre-Judge 在 `before_tool_call` Hook 之后执行。用户在审批窗口主动编辑并批准命令后，会再次运行 Hook，但不会重新进入 Pre-Judge 或 LLM Judge；显式 BYPASS 同样保留原有行为，不运行 YAML BLOCK。这里的 BLOCK 是审批阶段的判定，不是全局不可覆盖的禁止名单。
