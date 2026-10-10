# Hooks 配置参考

钩子（Hooks）让 iCode 在指定时机自动运行本地脚本或命令。本页用于查询完整配置字段、默认值、事件数据和执行规则。第一次使用 Hooks 时，建议先阅读[配置和编写 Hooks](../guides/extensions/hooks.md)，通过可运行的示例创建和验证钩子。

> **安全提示**
>
> 钩子直接使用当前用户的权限运行，不经过工具审批，也不受安全沙箱限制。启用项目自带的钩子前，请检查其配置文件以及运行的脚本和命令。
>
> 钩子可能接收对话内容、工具参数和工具运行结果。若钩子会将这些数据发送到外部服务，请注意检查其中是否包含敏感信息。

## 术语

本页将使用以下中文名称：

| 中文名称 | 配置值或对象 | 含义 |
| --- | --- | --- |
| 一轮 | `turn` | iCode 对一次用户请求的处理过程，从开始处理请求，到智能体停止生成内容和调用工具为止。用户中断或运行失败也会结束当前轮；运行期间提交的补充消息仍属于当前轮。 |
| 事件 | `event` | 触发钩子的时机，例如一轮结束或工具运行前。 |
| 阻塞式钩子 | `execution.mode: blocking` | iCode 等待钩子结束后再继续；部分事件会采用钩子返回的操作决定。 |
| 异步钩子 | `execution.mode: async` | iCode 立即继续当前操作，但通常会在当前轮或会话结束前等待钩子完成。 |
| 即发即弃钩子 | `execution.mode: fire_and_forget` | iCode 立即继续，在当前轮或会话结束时也不等待钩子；这是默认模式。 |
| 分离运行 | `execution.detach: true` | 让已经启动的即发即弃钩子在 iCode 退出后继续运行。 |
| 持久投递 | `execution.delivery: durable` | 记录未完成的非阻塞钩子任务，以便 iCode 以后重试；同一任务可能重复运行，脚本应能安全地重复执行。 |
| 输入文件 | `CHRYS_HOOK_PAYLOAD_FILE` | iCode 为本次事件生成的 JSON 文件，包含事件和上下文数据。 |
| 结果文件 | `CHRYS_HOOK_RESULT` | 钩子写入操作决定或补充信息的 JSON 文件。只有符合条件的阻塞式钩子能借此改变 iCode 的行为。 |

## 配置文件位置

iCode 可以分别从全局配置目录和当前工作目录加载一个钩子配置文件：

| 范围 | macOS 和 Linux | Windows |
| --- | --- | --- |
| 全局 | `~/.chrys/hooks/hooks.yaml` | `%APPDATA%\chrys\hooks\hooks.yaml` |
| 项目 | `<working-directory>/.chrys/hooks/hooks.yaml` | `<working-directory>\.chrys\hooks\hooks.yaml` |

两处都可以改用 `hooks.yml` 或 `hooks.json`。同一目录存在多个候选文件时，iCode 按 `hooks.yaml`、`hooks.yml`、`hooks.json` 的顺序只加载第一个。

钩子配置文件建议使用 UTF-8。iCode 也会自动检测 GBK/GB18030 等常见旧编码，以及带 BOM 的 UTF-16/UTF-32。

文件无法按检测到的编码解码时，iCode 会报告配置警告并禁用该文件中的钩子，不会替换命令或其他配置值中的字符。

`<working-directory>` 是会话的工作目录。对于项目配置，iCode 只检查 `<working-directory>/.chrys/hooks/`，不会在工作目录的父目录或子目录中查找。

## 加载配置文件

项目钩子随你打开的仓库而来，因此默认不加载。如需加载，在终端用户界面（Terminal User Interface，TUI）中按 **F10** 打开“设置”，在“安全”-“项目信任”区域勾选“加载项目钩子”（`project.hooks_enabled`）。工作目录中有未加载的项目钩子时，iCode 会弹出提示。全局钩子始终加载。

在磁盘上编辑钩子配置文件不会实时生效。修改后，需要切换会话或重启 iCode，让 iCode 重新加载配置。仅修改钩子脚本则不需要重新加载；脚本会在下次触发钩子时使用新内容。

在 TUI 中输入 `/runtime`，打开“钩子”标签页，可以查看当前加载的项目钩子和全局钩子。

## 配置文件结构

钩子配置文件支持以下顶层字段：

| 字段 | 类型 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `version` | 整数 | `1` | 配置格式版本。目前只支持 `1`。 |
| `settings` | 对象 | 各字段的默认值 | 当前配置文件中所有钩子共用的并发、退出等待和持久投递重试设置。 |
| `hooks` | 列表 | `[]` | 钩子条目列表。 |

`version` 和 `settings` 均可省略；省略时，iCode 使用表中列出的默认值。

`hooks` 用于定义钩子；`settings` 用于设置当前配置文件中所有钩子共用的执行参数。

### 全局设置

需要调整并发数量、退出等待时间或持久投递的重试行为时，可以配置 `settings`：

| 字段 | 类型 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `shutdown_grace_seconds` | 数值（>= `0`） | `5.0` | 当前会话结束时（见[会话事件](#会话事件)中的 `session_end`），最多等待异步钩子运行的时间。超时后仍未结束的钩子会被取消。 |
| `max_parallel_hooks` | 整数（>= `1`） | `4` | 最多同时运行的钩子数。达到上限后，新触发的钩子会等待已有钩子结束。启用分离运行的钩子不受此限制。 |
| `outbox_retry_age_seconds` | 数值（>= `0`） | `60.0` | 仅用于持久投递钩子。iCode 启动后，会重试因上次退出或崩溃而未记录为完成的钩子任务。此设置指定重试前至少等待的秒数，从任务上次开始执行时算起；任务从未执行过时，则从创建时算起。 |
| `outbox_max_retries` | 整数（>= `0`） | `3` | 仅用于持久投递钩子。每个钩子任务最多启动的次数，包括首次执行。使用默认值 `3` 时，中断的任务最多重试两次；设为 `1` 时不重试；设为 `0` 时，从未启动的任务也会被记录为失败。 |

同时存在项目和全局配置时，`settings` 按字段合并：项目文件中的值优先，其次使用全局文件中的值，两个文件均未设置时使用默认值。

### 定义钩子

`hooks` 列表中的每一项定义一个钩子，支持以下字段：

| 字段 | 类型 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `id` | 字符串 | 必填 | 钩子的稳定标识。首尾空白会被删除，删除后长度必须为 `1` 到 `512` 个字符。同一文件内不能重复。 |
| `event` | 字符串 | 必填 | 何时运行钩子，可用值见[事件](#事件)。 |
| `run` | 对象 | 必填 | 要启动的脚本、命令或 Shell 片段。 |
| `execution` | 对象 | 见[执行方式](#执行方式) | iCode 是否等待钩子、运行时限和失败处理方式。 |
| `match` | 对象 | 无筛选条件 | 限制哪些智能体或工具调用会触发该钩子。 |
| `enabled` | 布尔值 | `true` | 设为 `false` 可停用该钩子而不删除配置。 |
| `description` | 字符串 | `""` | 该钩子的说明，便于识别；不改变运行行为。 |

`id` 可以包含中文、空格和特殊符号，但不能包含换行符、制表符或其他无法正常显示的字符。`id` 包含空格或特殊符号时，应使用引号包住整个值。项目配置和全局配置可以使用相同的 `id`，两条钩子都会运行。

## 事件

通过 `event` 指定钩子在哪种事件发生时运行。

所有事件都提供[基础字段](#基础字段)，部分事件还会提供各自的额外字段，具体见下文。

### 会话事件

| 事件 | 运行时机 | 额外字段 |
| --- | --- | --- |
| `session_start` | 智能体准备就绪后。启动 iCode、新建会话或将当前会话回滚到开始处时会触发；切换智能体或模型不会触发。 | 无 |
| `session_restored` | 已保存的会话恢复后。启动 iCode 后立即恢复会话时，`session_start` 和 `session_restored` 都可能触发。 | `restored_session_id`：本次恢复的已保存会话 ID |
| `session_end` | 当前会话结束时：退出 iCode、切换会话、新建会话、删除或清空当前会话，或将当前会话回滚到开始处。 | 无 |

### 轮次事件

| 事件 | 运行时机 | 额外字段 |
| --- | --- | --- |
| `before_turn` | 一轮开始前。 | `turn`：从 `1` 开始的轮次编号<br>`user_text`：当前轮使用的用户文本<br>`is_retry`：是否正在重试同一轮 |
| `after_turn` | 一轮结束后，包括正常完成、用户中断和运行失败。 | `turn`：从 `1` 开始的轮次编号<br>`status`：`ok`、`interrupted` 或 `failed`<br>`failed`：本轮是否未正常完成，运行失败或被中断时均为 `true` |

### 用户操作事件

| 事件 | 运行时机 | 额外字段 |
| --- | --- | --- |
| `user_prompt_submit` | 用户提交开始新一轮的消息，或在智能体运行期间提交补充消息时。 | `text`：用户刚提交的消息文本<br>`injected`：`true` 表示运行期间提交的补充消息，`false` 表示开始新一轮的消息 |
| `user_interrupt` | 用户中断当前轮后。 | 无 |

### 工具事件

`tool` 和 `result` 对象的字段见[工具事件字段](#工具事件字段)。

| 事件 | 运行时机 | 额外字段 |
| --- | --- | --- |
| `before_tool_call` | 工具进入审批和执行流程前。如果用户在审批时编辑了参数，该钩子会在工具运行前使用编辑后的参数再次运行。 | `tool`：工具名称、类别、调用 ID 和参数 |
| `after_tool_call` | 工具调用返回后。工具正常返回结果、返回失败结果、因审批未通过而未执行，或被 `before_tool_call` 钩子拒绝时都会触发。 | `tool`：工具调用<br>`result`：运行结果 |
| `tool_error` | 工具调用抛出运行错误后。同一次调用会先触发 `after_tool_call`，再触发 `tool_error`。 | `tool`：工具调用<br>`result`：运行结果，其中 `result.error` 为 `true` |

### 审批事件

审批事件用于通知或记录工具审批的人工等待状态，仅覆盖 iCode 处理的工具审批，不覆盖外部 ACP 智能体转发的审批。字段格式见[审批事件字段](#审批事件字段)。

| 事件 | 运行时机 | 额外字段 |
| --- | --- | --- |
| `approval_requested` | 工具审批进入人工等待时。 | `request_id`、`caller_name`、`tool` |
| `approval_resolved` | 人工同意或拒绝审批后，iCode 继续处理审批结果前；等待被中断时不触发，不表示工具已经执行或执行成功。 | 与 `approval_requested` 相同，另有 `approved` |

在自动审批模式（`auto`）下，iCode 先完成自动审查，仅在仍需人工决定时触发审批事件。自动审查直接通过、审批被绕过，或进入等待前已经获得审批决定时，均不触发。

这两个事件的钩子只能观察审批状态，不能通过结果文件批准、拒绝或修改审批决定。

### 子智能体事件

| 事件 | 运行时机 | 额外字段 |
| --- | --- | --- |
| `sub_agent_start` | 子智能体调用开始时。 | `sub_agent`：显示名称 `name`、工具名称 `tool_name`、调用标识 `invocation_id`，以及父级调用标识 `parent_call_id` 和 `parent_provider_call_id` |
| `sub_agent_end` | 子智能体调用结束时。 | `sub_agent`：与 `sub_agent_start` 相同<br>`status`：`ok`、`failed` 或 `cancelled`<br>`result_summary`：最长 500 个字符的结果摘要 |

### 上下文压缩事件

| 事件 | 运行时机 | 额外字段 |
| --- | --- | --- |
| `pre_compact` | 上下文即将压缩时。 | `trigger`：压缩阶段或触发原因，取值见下表<br>`usage_pct`：压缩前的上下文使用比例，以 `1` 表示 100%<br>`tokens_before`：压缩前的 token 数<br>`sub_agent`：仅在子智能体压缩时提供，包含显示名称 `name` 和工具名称 `tool_name` |

`trigger` 的取值如下：

| 值 | 含义 |
| --- | --- |
| `phase1` | 缩减较早的工具结果。 |
| `phase2` | 移除较早的工具调用及其结果。 |
| `phase3` | 用摘要代替已完成的较早轮次。 |
| `phase4` | 生成用于续接当前任务的 Last Words，并缩减当前轮内容。 |
| `force` | 强制压缩。 |

### 工作流事件

| 事件 | 运行时机 | 额外字段 |
| --- | --- | --- |
| `workflow_run_start` | 工作流运行通过启动检查后、执行节点前。 | `run_id`、`input_text` |
| `workflow_run_end` | 工作流运行结束后，包括失败和取消。 | `run_id`、`outcome`、`reason` |

这两个事件只在工作流会话中触发，用于通知和记录；`action: block` 会被忽略。会话事件在工作流会话中也会触发，但触发时机不同。详见工作流参考中的[生命周期 Hooks](./workflows.md#生命周期-hooks)。

### 阻塞式钩子的影响

只有以下事件会采用阻塞式钩子在结果文件中返回的操作决定：

| 事件 | 影响 |
| --- | --- |
| `user_prompt_submit` | 拒绝消息或添加系统提醒。 |
| `before_tool_call` | 拒绝工具调用或修改参数，但不能批准调用或绕过审批。 |
| `after_tool_call` | 向工具结果追加上下文，但不能撤销已经发生的调用。 |
| `tool_error` | 向工具错误追加上下文，但不能撤销已经发生的调用。 |

### 异步钩子的等待时机

异步钩子触发后立即运行，当前操作会继续执行。如果钩子尚未完成，iCode 会在以下时机等待：

- 当前轮结束时，等待本轮触发的异步钩子；`user_interrupt` 触发的钩子除外。
- 当前会话结束时，等待会话事件触发的异步钩子。`session_end` 在会话结束阶段触发，因此会立即进入等待。

会话结束时的等待受 `shutdown_grace_seconds` 限制。每个异步钩子本身还受 `timeout_seconds` 限制。

## 匹配条件

`event` 决定钩子监听哪种事件，`match` 进一步限定哪些事件会触发钩子。省略 `match` 或设置为 `{}` 时，不增加筛选条件；设置多个条件时，事件必须同时满足这些条件。

| 字段 | 类型 | 适用事件 | 匹配要求 |
| --- | --- | --- | --- |
| `profile` | 字符串 | 所有事件 | 事件所属的智能体名称（基础字段 `profile`）等于指定值。 |
| `profiles` | 字符串列表 | 所有事件 | 事件所属的智能体名称是列表中的一项；空列表不限制智能体名称。 |
| `tool_kind` | 字符串 | 工具、审批事件 | 工具类别等于指定值。 |
| `tool_name` | 字符串 | 工具、审批事件 | 工具名称等于指定值。 |
| `args` | 对象 | 工具、审批事件 | 工具参数满足指定条件，见[工具参数匹配](#工具参数匹配)。 |

`profile` 和 `profiles` 通常只需设置一个；同时设置时，两者都必须满足。对于子智能体事件，以及子智能体内部的工具、审批和上下文压缩事件，智能体名称是子智能体的配置名称，而不是主智能体的名称。

其他事件不提供 `tool` 对象，无法满足工具类别、名称或具体参数的筛选条件。工具类别的可用值及对应工具名称见[工具类别和名称](./tool-kinds-and-names.md)。

### 工具参数匹配

`args` 以工具实际定义的参数名为键，每个参数下配置比较条件。例如，以下片段要求工具的 `path` 参数同时包含 `src/`，并以 `.py` 结尾：

```yaml
match:
  args:
    path:
      contains: "src/"
      regex: '\.py$'
```

| 运算符 | 匹配要求 | 示例 |
| --- | --- | --- |
| `equals` | 参数的完整字符串与指定值相同。 | `equals: "README.md"` |
| `contains` | 参数的字符串包含指定片段。 | `contains: "src/"` |
| `regex` | 参数的字符串中存在符合 Python 正则表达式的部分。 | `regex: '\.py$'` |

匹配时遵循以下规则：

- 指定多个参数，或为同一参数设置多个运算符时，所有条件都必须满足。`args: {}` 不增加参数筛选条件。
- 参数缺失或值为 `null` 时不匹配。`path: {}` 只检查 `path` 存在且不为 `null`，不比较具体值。
- 条件值必须是字符串。在 YAML 中，数字和布尔值需要加引号：未加引号的值（如 `equals: 10` 或 `equals: true`）属于配置错误，会导致该文件中的所有钩子被停用。
- 非字符串参数会先用 Python 的 `str()` 转换再比较，因此 JSON 的 `true` 和 `false` 会变成 `True` 和 `False`（应写 `equals: "True"` 进行匹配），列表和对象使用 Python 的表示形式。
- 正则表达式建议使用 YAML 单引号字符串；使用双引号时，反斜杠需要转义。正则表达式无效的钩子会在会话启动时被跳过，并显示一条注明该钩子和所在文件的警告；该文件中的其他钩子仍会运行。

## 运行配置

`run` 指定钩子要启动的脚本或命令。先通过 `type` 选择一种运行方式：

| `type` | 适用情况 | 必填字段 |
| --- | --- | --- |
| `script` | 运行已有脚本，并由 iCode 根据文件扩展名选择运行程序。 | `path` |
| `command` | 直接启动一个可执行文件，不需要 Shell 解释。 | `argv` |
| `shell` | 运行包含重定向、管道、变量展开等 Shell 语法的命令字符串。 | `shell` |

只填写所选类型对应的 `path`、`argv` 或 `shell`。三种类型都可以[设置环境变量和工作目录](#设置环境变量和工作目录)。

### 运行脚本

使用 `script` 运行磁盘上的脚本文件。例如，运行钩子配置文件所在目录下的 `scripts/check.py` 并传入 `--strict` 参数：

```yaml
run:
  type: script
  path: scripts/check.py
  args: ["--strict"]
```

| 字段 | 类型 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `type` | `script` | 必填 | 固定填写 `script`。 |
| `path` | 字符串 | 必填 | 脚本文件路径。可以使用绝对路径；相对路径以当前钩子配置文件所在目录为基准。 |
| `args` | 字符串列表 | `[]` | 传给脚本的参数，按顺序追加在脚本路径后。 |

iCode 根据文件扩展名选择运行程序：

| 扩展名 | 运行程序 |
| --- | --- |
| `.py` | 按下文说明选择 `uv` 或 Python |
| `.ps1` | `pwsh` 或 `powershell` |
| `.sh`、`.bash`、`.zsh` | `bash` 或 `sh`；Windows 还会查找 Git Bash |
| `.js`、`.mjs` | `node` |
| `.ts` | `npx tsx` |
| `.rb` | `ruby` |
| `.pl` | `perl` |
| 其他 | 按 Python 脚本运行 |

对于 `.py` 文件，iCode 会先在用户的 `PATH` 中依次查找 `uv`、`python3` 和 `python`。找到 `uv` 时，通过 `uv run` 运行脚本；找到 Python 时，直接使用该解释器。如果都未找到，则使用 iCode 自带运行环境中的 `uv` 或 Python。

运行其他类型的脚本前，需安装表中对应的运行程序，并确保 iCode 可以通过 `PATH` 找到该程序。

### 运行命令

使用 `command` 直接启动可执行文件。例如，运行 `git status --short`：

```yaml
run:
  type: command
  argv: ["git", "status", "--short"]
```

iCode 不会通过 Shell 解释参数，因此重定向、管道、变量展开和命令替换不会生效。

| 字段 | 类型 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `type` | `command` | 必填 | 固定填写 `command`。 |
| `argv` | 字符串列表 | 必填 | 要启动的可执行文件及其固定参数。列表不能为空，第一项是可执行文件。 |
| `args` | 字符串列表 | `[]` | 追加到 `argv` 末尾的参数。 |

每个参数应作为一个独立的列表元素填写，不需要添加 Shell 转义。可以将全部参数写入 `argv`，也可以将末尾参数写入 `args`；两种写法的运行结果相同。不依赖 Shell 语法时，优先使用 `command`，可以避免不同 Shell 的引用和转义差异。

### 运行 Shell 片段

使用 `shell` 运行一个完整的 Shell 命令字符串。例如，下面的配置把一行文字追加到工作目录的 `hook-events.log`：

```yaml
run:
  type: shell
  shell: "echo hook triggered >> hook-events.log"
```

| 字段 | 类型 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `type` | `shell` | 必填 | 固定填写 `shell`。 |
| `shell` | 字符串 | 必填 | 交给 Shell 解释的完整命令字符串。 |

在 macOS 和 Linux 上，iCode 使用环境变量 `$SHELL` 指定的 Shell；未设置时使用 `/bin/sh`。在 Windows 上使用命令提示符（CMD）。不同 Shell 支持的语法可能不同。

整个命令由 Shell 解释。不要把不可信内容直接拼接到 `shell` 字符串中，否则可能造成命令注入。`shell` 类型不使用 `args`；需要传入额外内容时，应直接写入 `shell` 字符串，或改用 `script`、`command`。

### 设置环境变量和工作目录

`env` 和 `cwd` 适用于 `script`、`command` 和 `shell` 三种类型。

| 字段 | 类型 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `env` | 字符串到字符串的对象 | `{}` | 添加到钩子进程的环境变量；名称相同时，覆盖 iCode 传递的值。 |
| `cwd` | 字符串 | `""` | 钩子进程的工作目录；省略或留空时使用当前会话的工作目录。 |

例如，为脚本设置环境变量，并在当前会话的工作目录中运行：

```yaml
run:
  type: script
  path: scripts/check.py
  env:
    CHECK_LEVEL: strict
    SESSION_NAME: "${session_id}"
  cwd: "${workspace_cwd}"
```

脚本路径和运行工作目录使用不同的路径基准：

| 配置 | 路径基准 |
| --- | --- |
| `path` 使用相对路径 | 钩子配置文件所在目录 |
| `cwd` 省略或留空 | 当前会话的工作目录 |
| `cwd` 使用相对路径 | iCode 进程的当前工作目录，可能与会话工作目录不同 |

设置 `cwd` 时，应使用绝对路径或 `${workspace_cwd}`、`${chrys_home}` 模板，例如用 `${chrys_home}/hooks` 指定 iCode 配置目录下的 `hooks` 目录；目标目录必须存在。

`env` 的值和 `cwd` 支持以下模板：

| 模板 | 值 |
| --- | --- |
| `${workspace_cwd}` | 当前会话的工作目录 |
| `${chrys_home}` | iCode 配置目录；通常为 `~/.chrys` 或 `%APPDATA%\chrys` |
| `${session_id}` | 当前会话 ID |
| `${profile}` | 事件所属的智能体名称，与基础字段 `profile` 相同 |

iCode 会按字面量替换 `env` 值和 `cwd` 中出现的上述模板。其他变量或表达式（如 `~`、`$VAR`、`${VAR}` 和 `${VAR:-default}`）不会由 iCode 展开。

## 执行方式

`execution` 控制 iCode 是否等待钩子、钩子能否在 iCode 退出后继续运行、未完成的任务是否重试，以及超时和失败的处理方式。所有字段都可以省略。多数通知和记录脚本使用默认值即可：当前操作不等待钩子，钩子最多运行 30 秒，失败时记录警告。

配置时，先选择 `mode`，再按需要设置其他字段。

### 选择运行模式

先根据钩子是否必须在当前操作继续前完成，选择 `mode`：

| `mode` | 等待方式 | 适用情况 | 是否可能影响当前操作 |
| --- | --- | --- | --- |
| `blocking` | 先等待钩子结束，再继续处理当前操作；多个 `blocking` 钩子按配置顺序运行 | 操作继续前必须完成的检查，或需要拒绝、修改操作 | 部分事件可以，见[结果文件](#结果文件) |
| `async` | 当前操作立即继续；后续等待规则见[异步钩子的等待时机](#异步钩子的等待时机) | 不应延迟当前操作，但需要在本轮或当前会话结束前完成的通知和记录 | 否 |
| `fire_and_forget`（默认） | 当前操作立即继续；本轮或当前会话结束时不等待钩子 | 允许尚未完成的通知和记录被 iCode 退出打断 | 否 |

### 设置运行时限和失败处理

| 字段 | 类型或可用值 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `timeout_seconds` | 大于 `0` 的数值 | `30.0` | 钩子的最长运行时间，单位为秒；启用 `detach` 时忽略。 |
| `on_error` | `block`、`warn` 或 `ignore` | `warn` | 钩子失败后的处理方式，具体行为见下表。 |

`on_error` 决定钩子无法启动、运行超时或返回非零退出码时，iCode 如何继续：

| 值 | 行为 |
| --- | --- |
| `block` | 对 `user_prompt_submit` 或 `before_tool_call` 的阻塞式钩子，拒绝当前操作并向用户显示错误；其他事件或非阻塞模式按 `warn` 处理。 |
| `warn` | 记录警告并继续当前操作。 |
| `ignore` | 仅以调试级别记录失败并继续当前操作。钩子超时或被停止时仍会记录警告。 |

阻塞式钩子失败时，iCode 不采用脚本返回的操作决定，只应用 `on_error`。

例如，需要在工具运行前完成检查，并在脚本无法启动、超时或返回非零退出码时拒绝调用，可以设置：

```yaml
execution:
  mode: blocking
  timeout_seconds: 5
  on_error: block
```

### 退出后继续运行

`detach` 是布尔值，默认为 `false`。需要已启动的钩子在 iCode 退出后继续运行时，可以设为 `true`，但只能用于 `mode: fire_and_forget`。

启用 `detach` 后，钩子不受 `max_parallel_hooks` 和 `timeout_seconds` 限制。标准输出和标准错误保存在 `~/.chrys/hooks/logs`（macOS 和 Linux）或 `%APPDATA%\chrys\hooks\logs`（Windows）。

### 重试未完成的任务

`delivery` 控制 iCode 是否在以后启动时重试未完成的任务：

| 值 | 行为 |
| --- | --- |
| `best_effort`（默认） | 不重试。 |
| `durable` | 记录待完成的任务，在 iCode 启动时重试符合条件的未完成任务。仅适用于 `async` 和 `fire_and_forget`。 |

已经记录为失败的任务不会自动重试，例如脚本返回非零退出码或运行超时。同一任务可能重复运行，因此脚本必须能安全地重复执行。

重试条件由[全局设置](#全局设置)控制：`outbox_retry_age_seconds` 指定重试前至少等待多久，`outbox_max_retries` 限制每个任务最多启动多少次。为 `blocking` 设置 `delivery: durable` 不会自动重试，还会产生警告。

`detach` 让已启动的进程在 iCode 退出后继续运行，`delivery` 让 iCode 在以后启动时恢复未完成的任务，两者可以同时设置。例如：

```yaml
execution:
  mode: fire_and_forget
  detach: true
  delivery: durable
```

## 脚本输入与输出

脚本通过环境变量找到本次钩子的输入和结果文件：

| 环境变量 | 说明 |
| --- | --- |
| `CHRYS_HOOK_ID` | 当前条目的 `id`。 |
| `CHRYS_HOOK_EVENT` | 当前事件名称。 |
| `CHRYS_HOOK_PAYLOAD_FILE` | 包含本次事件信息的 UTF-8 JSON 文件路径。 |
| `CHRYS_HOOK_RESULT` | 钩子可以写入的 UTF-8 JSON 结果文件路径；文件初始为空。 |
| `PYTHONUTF8` | 固定为 `1`。 |
| `PYTHONIOENCODING` | 固定为 `utf-8`。 |

每次运行钩子时，iCode 都会提供输入文件和结果文件。两个文件会在钩子结束后删除，不应保存其路径供以后使用。

脚本从输入文件读取事件信息，需要返回操作决定时写入[结果文件](#结果文件)。标准输出和标准错误不作为操作决定读取；iCode 对两者各最多保留 256 KiB（开头和结尾）。启用 `detach` 的钩子写入日志文件的输出不受此限制。

### 基础字段

每个输入文件都包含以下字段：

| 字段 | 含义 |
| --- | --- |
| `schema` | 输入数据的格式版本，当前为 `1`。 |
| `event` | 本次触发的事件名称。 |
| `timestamp` | 事件时间，使用 UTC。 |
| `session_id` | 当前会话的 ID。 |
| `profile` | 当前智能体的名称，例如 `Code`。对于子智能体事件，以及子智能体内部的工具、审批和上下文压缩事件，此字段是子智能体的配置名称。 |
| `cwd` | 当前会话的工作目录。`run.cwd` 设置的是钩子进程的工作目录，不会改变此字段。 |

例如，一次工具调用前的输入包含以下基础字段：

```json
{
  "schema": 1,
  "event": "before_tool_call",
  "timestamp": "2026-05-14T15:00:00Z",
  "session_id": "abc-123",
  "profile": "Code",
  "cwd": "/path/to/working-directory"
}
```

不同事件还会包含各自的额外字段，具体见[事件](#事件)章节。

### 工具事件字段

以下对象只展示工具事件新增的字段：

```json
{
  "tool": {
    "name": "edit_file",
    "kind": "filesystem.write",
    "call_id": "abcdef123456",
    "args": {
      "path": "src/example.py",
      "old_string": "old",
      "new_string": "new"
    }
  },
  "result": {
    "text": "OK",
    "duration_ms": 42,
    "error": false,
    "failed": false,
    "approval_rejected": false
  }
}
```

`before_tool_call` 没有 `result`。`after_tool_call` 和 `tool_error` 包含 `result`，其中的状态字段含义如下：

| 字段 | 出现条件与含义 |
| --- | --- |
| `error` | 工具调用抛出异常时为 `true`；正常返回失败结果或调用被拒绝时为 `false`。 |
| `failed` | 调用被判定为失败时为 `true`，包括抛出异常、返回可识别的失败结果（例如 Shell 命令返回非零退出码），或调用被拒绝。 |
| `approval_rejected` | 始终存在；调用被用户或钩子拒绝时为 `true`，否则为 `false`。 |
| `rejection_source` | 仅在调用被拒绝时出现；`user` 表示用户拒绝，`hook` 表示钩子拒绝。 |
| `hook_denied` | 仅在调用被拒绝时出现；钩子拒绝时为 `true`，用户拒绝时为 `false`。 |

`error: true` 时，`failed` 也为 `true`，但 `failed: true` 不一定表示抛出了异常。`tool_error` 只在抛出异常时触发；需要检查各类失败时，应监听 `after_tool_call` 并检查 `result.failed`。

### 审批事件字段

以下对象展示 `approval_resolved` 在基础字段之外提供的内容：

```json
{
  "request_id": "approval-123",
  "caller_name": "Code",
  "tool": {
    "name": "edit_file",
    "kind": "filesystem.write",
    "call_id": "call-456",
    "args": {
      "path": "src/example.py",
      "old_string": "old",
      "new_string": "new"
    }
  },
  "approved": true
}
```

| 字段 | 含义 |
| --- | --- |
| `request_id` | 本次审批请求的标识；同一次等待的开始和结束事件使用相同值。 |
| `caller_name` | 发起工具调用的智能体显示名称；未配置显示名称时使用配置名称。 |
| `tool` | 进入人工等待时的工具名称、类别、调用 ID 和参数。人工审批时编辑的参数不会更新此处的 `args`，因此该字段不一定是最终执行参数。 |
| `approved` | 仅在 `approval_resolved` 中出现；`true` 表示同意，`false` 表示拒绝。 |

审批事件的基础字段 `profile` 是发起调用的智能体配置名称，可能与 `caller_name` 不同。

## 结果文件

向 `CHRYS_HOOK_RESULT` 指定的文件写入 UTF-8 JSON 对象，即可返回操作决定。文件最大为 1 MiB。

只有阻塞式钩子成功返回退出码 `0` 时，iCode 才会按下表所列的事件和条件采用操作决定。异步和即发即弃钩子的结果文件不会改变 iCode 行为。钩子无法启动、运行超时或返回非零退出码时，结果文件会被忽略，并按[失败处理配置](#设置运行时限和失败处理)中的 `on_error` 处理。

结果文件支持以下 JSON 字段：

| 字段 | 类型 | 生效事件与条件 | 说明 |
| --- | --- | --- | --- |
| `action` | `allow`、`block` 或 `modify` | `user_prompt_submit`、`before_tool_call` | 默认为 `allow`。两种事件都接受 `block`；只有 `before_tool_call` 接受 `modify`。 |
| `reason` | 字符串 | `user_prompt_submit`、`before_tool_call`，且 `action: block` | 向用户显示的拒绝原因；工具调用被拒绝时，也会提供给模型。 |
| `args_override` | 对象 | `before_tool_call` 且 `action: modify` | 将其中的顶层字段加入现有 `tool.args`。同名字段会替换原值；如果值是对象，该对象也会被整体替换。 |
| `system_reminder` | 字符串 | `user_prompt_submit` | 作为系统提醒加入当前轮的每一次模型调用。 |
| `extra_context` | 字符串 | `after_tool_call`、`tool_error` | 追加到工具结果文本，不能撤销已经发生的调用。 |

结果文件按以下规则读取：

- 空文件或未写入结果等同于 `{"action": "allow"}`。
- JSON 无效、顶层不是对象或文件超过 1 MiB 时，iCode 会记录警告并忽略结果，按无决定处理。
- 未知字段会被忽略。

多个钩子的决定如何组合，见[多个钩子的执行顺序与配置合并](#多个钩子的执行顺序与配置合并)。

## 多个钩子的执行顺序与配置合并

### 执行顺序

同时加载项目配置和全局配置时，执行模式决定钩子在哪个阶段运行，配置来源决定每个阶段内的顺序。对于同一次事件，iCode 按以下两个阶段处理钩子：

1. 依次检查并运行匹配的阻塞式钩子：先检查项目钩子，再检查全局钩子，各自保持配置文件中的定义顺序。
2. 如果没有钩子拒绝操作，iCode 再启动匹配的异步和即发即弃钩子：先按项目配置中的定义顺序启动，再按全局配置中的定义顺序启动。这些非阻塞钩子可能并发运行，因此完成顺序不一定与启动顺序相同。

阻塞式钩子返回的操作决定按以下规则组合：

| 操作决定 | 多个阻塞式钩子返回时的组合方式 |
| --- | --- |
| `args_override` | 立即修改工具参数。后续钩子使用修改后的参数进行匹配和检查；多个钩子修改同一参数时，后运行的钩子覆盖先运行的钩子。 |
| `system_reminder`、`extra_context` | 按钩子的执行顺序累积。 |
| `action: block` | 对 `user_prompt_submit` 和 `before_tool_call`，第一个拒绝决定会停止当前事件的剩余钩子，包括尚未启动的非阻塞钩子。 |

对于 `user_prompt_submit` 和 `before_tool_call`，阻塞式钩子失败且配置了 `on_error: block` 时，同样会拒绝当前操作并停止当前事件的剩余钩子。

因此，项目钩子修改工具参数后，后续全局钩子会看到修改后的参数；项目钩子拒绝操作后，当前事件的全局钩子不会运行。

### 同名 ID 不会覆盖钩子

项目配置和全局配置可以定义相同的 `id`。两个钩子都会保留，iCode 同时会记录重名警告。

同一配置文件内不能出现重复的 `id`，否则该配置文件加载失败。`id` 只用于标识钩子，不是覆盖键；在项目配置中定义同名钩子，不能覆盖或停用全局钩子。需要停用全局钩子时，应编辑全局配置，将该钩子的 `enabled` 设为 `false`，或者删除该钩子定义。

### 设置合并与加载失败

项目配置和全局配置中的 `settings` 按字段合并：项目配置中明确设置的值优先，其次使用全局配置中的值；两处都没有设置时使用默认值。

任一配置文件无效时，iCode 会显示警告，并停用该文件定义的所有钩子；另一处有效配置文件仍然生效。未找到配置文件不会产生警告。
