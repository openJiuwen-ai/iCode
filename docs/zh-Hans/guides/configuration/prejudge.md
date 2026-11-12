# 结构化 Shell Pre-Judge

iCode 可以使用 Tree-sitter 和**固定结构的 YAML 匹配规则**分析 Bash 指令，再进入原有工具审批。功能**默认关闭**，不替换旧 regex/readonly Pre-Judge、DAA、Hook、授权复用或后置 Shell Command Filter。

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

BLOCK 优先于 ALLOW。要自动 ALLOW，必须所有可执行 AST 节点匹配 ALLOW、命令 `argv` 完整且语法覆盖完整；否则 DEFER，走常规审批（`enforce` 不再使用旧 readonly 快捷批准）。**静态前缀只能用于 BLOCK**。例如 `echo "rm"` 里的 `rm` 是参数，不是另一个可执行命令。

`mode` 必须加引号，避免 YAML 将 `off` 转成布尔值。`off` 保留旧行为；`shadow` 只记录观察结果；`enforce` 执行明确的 BLOCK 和完全覆盖的 ALLOW。enforce 配置无效时初始化失败；shadow 配置错误会记录警告并停止观察。显式 BYPASS 不执行新 BLOCK 规则；无交互的 `icode run` 和 `icode workflow run` 使用 BYPASS。

当前只支持 Bash/Git Bash。zsh、sh、dash、PowerShell、cmd.exe、动态展开、重定向、通配符及多数控制流构造不允许自动 ALLOW；内部可靠识别的静态命令仍有机会触发 BLOCK。匹配采用字面量命令名，不自动规范化可执行文件路径。例如 `prefix_first: [rm]` 不会命中 `/bin/rm -rf x`，而是进入 DEFER（不会自动允许）。像 `command rm`、`env rm`、`time rm` 这样的包装命令按 opaque 处理并 DEFER，不能假设 `prefix_first: [rm]` 会命中包装命令里的 `rm`。本功能不能取代完整的 Shell 安全边界。

**依赖：**启用该功能需要 `tree-sitter==0.25.1` 与 `tree-sitter-bash==0.25.1`。默认 `off` 不会加载 Tree-sitter。依赖锁定和 Linux 离线发行的完整验证仍属于独立发行验收项。
