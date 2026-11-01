# 配置和编写 Hooks

钩子（Hooks）用于在指定事件发生时自动运行本地脚本或命令。例如，可以在一轮任务结束后记录结果、在工具运行前检查参数，或在用户提交消息时为模型补充要求。

本指南通过一个简单的示例，介绍如何编写和配置钩子，并提供几个可以按需修改的常用示例。完整的事件列表、配置字段、默认值以及脚本的输入输出格式，请参阅 [Hooks 配置参考](../../reference/hooks.md)。

> **安全提示**
>
> 钩子直接使用当前用户的权限运行，不经过工具审批，也不受安全沙箱限制。启用项目自带的钩子前，请检查其配置文件以及运行的脚本和命令。
>
> 钩子可能接收对话内容、工具参数和工具运行结果。若钩子会将这些数据发送到外部服务，请注意检查其中是否包含敏感信息。

## 创建第一个钩子

下面将在 iCode 当前会话的工作目录中创建一个项目钩子。它只对当前工作目录生效，并会在每一轮结束后将轮次状态追加到该目录下的 `hook-events.log`。

### 1. 创建配置

创建 `.chrys/hooks/hooks.yaml`：

```yaml
hooks:
  - id: record-turn
    event: after_turn
    run:
      type: script
      path: scripts/record_turn.py
```

`id` 是钩子的稳定标识，在同一个配置文件中不能重复；这里将它命名为 `record-turn`。

这项配置在 `after_turn` 事件发生时运行 `scripts/record_turn.py`。相对脚本路径以钩子配置文件所在目录为基准，因此这里指向下一步创建的 `.chrys/hooks/scripts/record_turn.py`。

### 2. 创建脚本

创建 `.chrys/hooks/scripts/record_turn.py`：

```python
import json
import os
from pathlib import Path

with open(os.environ["CHRYS_HOOK_PAYLOAD_FILE"], encoding="utf-8") as payload_file:
    payload = json.load(payload_file)

with Path("hook-events.log").open("a", encoding="utf-8") as log_file:
    log_file.write(f"turn={payload['turn']} status={payload['status']}\n")
```

iCode 通过 `CHRYS_HOOK_PAYLOAD_FILE` 环境变量提供本次事件的 JSON 输入文件。`after_turn` 事件的输入包含 `turn` 和 `status`，分别表示轮次编号和结束状态。脚本默认在 iCode 当前会话的工作目录中运行，所以 `hook-events.log` 也会写到工作目录。

### 3. 加载并验证

在工作目录中打开终端，运行以下命令检查配置：

```shell
icode hook validate
```

该命令检查项目和全局钩子配置文件，不会运行任何钩子。它逐条列出问题及其所在文件和行号，最后一行在没有错误时为 `PASS`，否则为 `FAIL` 和错误数量。按提示修改后再次运行，直到输出类似下面的一行：

```text
PASS 1 file · 1 hook
```

项目钩子默认不加载。在终端用户界面（Terminal User Interface，TUI）中按 **F10** 打开“设置”，在“安全管控”-“项目信任”区域勾选“加载项目钩子”。如果已经勾选，切换会话或重启 iCode，使磁盘上的配置生效。

运行 `icode hook list`，查看在此目录中启动的会话会加载哪些钩子。`Project hooks` 一行末尾应为 `(loaded)`，下方列出 `record-turn`。

在 TUI 中输入 `/runtime`，打开“钩子”标签页，应能看到 `record-turn`。提交一条消息并等待当前轮结束后，检查工作目录中的 `hook-events.log`。文件应出现类似内容：

```text
turn=1 status=ok
```

## 示例：按工具参数阻止操作

需要在工具运行前检查或拒绝操作时，使用 `before_tool_call` 事件和阻塞式执行。下面的示例阻止文件写入工具修改名为 `.env` 的文件。

先把以下条目加入 `.chrys/hooks/hooks.yaml` 的 `hooks` 列表：

```yaml
  - id: protect-env
    event: before_tool_call
    match:
      tool_kind: filesystem.write
      args:
        path:
          regex: "(^|[\\\\/])\\.env$"
    run:
      type: script
      path: scripts/protect_env.py
    execution:
      mode: blocking
      timeout_seconds: 5
      on_error: block
```

这段配置只匹配具有 `path` 参数，并且路径以 `.env` 结尾的 `filesystem.write` 工具调用。阻塞式钩子会在 iCode 继续处理调用前完成。

然后创建 `.chrys/hooks/scripts/protect_env.py`：

```python
import json
import os

with open(os.environ["CHRYS_HOOK_RESULT"], "w", encoding="utf-8") as result_file:
    json.dump(
        {"action": "block", "reason": "不允许通过文件写入工具修改 .env"},
        result_file,
        ensure_ascii=False,
    )
```

脚本通过 `CHRYS_HOOK_RESULT` 指定的结果文件返回 `action: block` 和拒绝原因。脚本打印到标准输出或标准错误的内容不作为操作决定读取，iCode 对两者各最多保留 256 KiB（开头和结尾）。

`on_error: block` 表示脚本无法启动、超时或返回非零退出码时也拒绝工具调用，适合需要失败时保持限制的检查。这个钩子只会阻止匹配的 `filesystem.write` 调用；Shell 命令不受影响。

切换会话或重启 iCode 后，先创建一个专门用于验证的 `hook-demo/.env`，写入已知内容，例如 `HOOK_TEST=unchanged`。然后要求智能体仅使用 `write_file` 或 `edit_file` 尝试修改一次，被拒绝后停止，不使用 Shell 或其他方式。检查该工具调用是否在执行前被拒绝，并显示脚本提供的原因；再确认文件内容保持不变。验证后可以删除 `hook-demo/.env`。

## 示例：操作前请你确认

钩子也可以不自己拒绝操作，而是把决定交给你。下面的示例在任何提到 `prod` 的 Shell 命令运行前询问你。

把以下条目加入 `.chrys/hooks/hooks.yaml` 的 `hooks` 列表：

```yaml
  - id: confirm-prod
    event: before_tool_call
    match:
      tool_kind: shell
      args:
        command:
          contains: prod
    run:
      type: script
      path: scripts/confirm_prod.py
    execution:
      mode: blocking
      timeout_seconds: 5
```

然后创建 `.chrys/hooks/scripts/confirm_prod.py`：

```python
import json
import os

with open(os.environ["CHRYS_HOOK_RESULT"], "w", encoding="utf-8") as result_file:
    json.dump(
        {"action": "ask", "reason": "此命令提到了 prod，是否运行？"},
        result_file,
        ensure_ascii=False,
    )
```

智能体运行匹配的命令时，iCode 会显示钩子给出的原因和命令并询问你。**继续**则命令照常运行，**取消**则拒绝它。你考虑的时间不计入 `timeout_seconds`。完整规则见[请你确认](../../reference/hooks.md#请你确认)。

## 示例：为模型添加系统提醒

`user_prompt_submit` 钩子可以在用户提交消息时，为模型补充要求或提示。下面的示例只对 Code 智能体生效，并提醒它在修改 Python 文件后运行项目测试。

先把以下条目加入 `.chrys/hooks/hooks.yaml` 的 `hooks` 列表：

```yaml
  - id: remind-python-tests
    event: user_prompt_submit
    match:
      profile: Code
    run:
      type: script
      path: scripts/remind_python_tests.py
    execution:
      mode: blocking
      timeout_seconds: 5
```

然后创建 `.chrys/hooks/scripts/remind_python_tests.py`：

```python
import json
import os

with open(os.environ["CHRYS_HOOK_RESULT"], "w", encoding="utf-8") as result_file:
    json.dump(
        {"system_reminder": "修改 Python 文件后，运行与改动相关的测试。"},
        result_file,
        ensure_ascii=False,
    )
```

切换会话或重启 iCode，使配置生效。系统提醒只有在阻塞式钩子成功结束后才会生效。iCode 会用 `<system-reminder>` 标签包住提醒内容，随用户提交的消息一起发送给模型。这段提醒不会显示在 TUI 对话中，也不会写入会话数据。

如需确认 `user_prompt_submit` 钩子是否生效，或查看系统提醒发送给模型时的实际内容，可以检查模型请求的原始 HTTP 日志。该日志包含未脱敏的 API 密钥和完整对话内容，应仅在排查问题时临时启用。在 TUI 中按 **F10**，进入“安全管控”-“诊断”，开启“捕获模型原始 HTTP 请求”，然后重启 iCode。使用 Code 智能体提交一条消息后，在当前会话目录的 `llm_raw_http.jsonl` 中搜索提醒正文“修改 Python 文件后”。当前会话目录的位置参阅[查找会话 ID 和会话保存位置](../daily-use/sessions.md#查找会话-id-和会话保存位置)。验证完成后，应关闭“捕获模型原始 HTTP 请求”并再次重启 iCode。

如果只想在部分任务中添加提醒，可以让脚本读取 `CHRYS_HOOK_PAYLOAD_FILE` 中的 `text`（用户刚提交的消息），并只在消息符合条件时向 `CHRYS_HOOK_RESULT` 写入 `system_reminder`。

## 选择运行方式

多数钩子只需根据“当前操作是否必须等待结果”选择执行方式：

| 需求 | 建议模式 | 行为 |
| --- | --- | --- |
| 在操作继续前检查、拒绝或修改操作 | `blocking` | iCode 等待钩子结束；部分事件会采用结果文件中的操作决定 |
| 不阻塞当前操作，但希望 iCode 在当前轮或会话结束时等待一段时间 | `async` | iCode 立即继续，之后在适用的事件和等待上限内等待钩子完成 |
| 普通通知或允许被退出打断的记录任务 | `fire_and_forget` | iCode 立即继续，结束当前轮或会话时也不等待；这是默认模式 |

异步钩子（`async`）不会延迟触发它的事件或当前操作。当前轮或会话结束时，如果相应的异步钩子仍在运行，iCode 会等待它完成后再结束当前轮或会话。`user_interrupt` 触发的异步钩子不会被等待。会话结束时的等待上限可在[全局设置](../../reference/hooks.md#全局设置)中配置。需要在 iCode 退出后重试未完成的任务时，还应配置 `delivery: durable`。

## 查看运行过的钩子

在对话中，右下角会显示 iCode 正在等待的钩子。点击它会打开侧栏的“钩子”标签页（按 **Ctrl+G** 显示或隐藏侧栏）；右下角显示着某个钩子时，还会在上面打开这个钩子的详情，并且列表会滚动到这一行并标出它。

“钩子”标签页列出 iCode 启动以来所有对话会话中运行过的钩子（工作流运行的钩子不在其中），按开始顺序编号，最新的在最下面：包括正在运行和排队等待的，以及最近结束的 100 次。每行显示编号、表示运行情况的图标（例如 `✗` 表示运行失败，`⊘` 表示阻止了操作，`?` 表示正在等你回答）、钩子名称，最右侧是运行时长。运行详情中会说明它由什么触发、结果如何。点击“清除已结束”可从列表中移除已结束的运行，正在运行的会保留。清除后的运行无法再显示，新的运行接着原来的编号往下数。结束的运行超过 100 次后，列表顶部会提示只保留最近 100 次。

点击一行可查看详情。“输入”显示 iCode 交给钩子的 payload 以及钩子的运行方式；“输出”显示结果文件、iCode 采纳了哪些内容（或为什么没有采纳），以及钩子输出到 stdout 和 stderr 的内容。点击某个方框即可复制其中的内容，按 **c** 复制整个标签页；很长的内容只显示一部分，但复制时仍会复制全部内容。每次启动 iCode 时列表为空；要查看 iCode 加载了哪些钩子，输入 `/runtime` 并打开“钩子”标签页。

## 使用全局钩子

前面的示例都放在当前工作目录的 `.chrys/hooks` 中，只对该工作目录生效。需要在所有工作目录使用同一个钩子时，可以改用全局配置：

| 平台 | 全局配置文件 |
| --- | --- |
| macOS 和 Linux | `~/.chrys/hooks/hooks.yaml` |
| Windows | `%APPDATA%\chrys\hooks\hooks.yaml` |

项目配置和全局配置可以同时加载。两处钩子的执行顺序、同名 ID 和 `settings` 合并规则见[多个钩子的执行顺序与配置合并](../../reference/hooks.md#多个钩子的执行顺序与配置合并)。
