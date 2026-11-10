# 使用 icode run 执行无界面任务

`icode run` 在终端中执行任务，完成后返回智能体的最终回复。它不打开终端用户界面（Terminal User Interface，TUI）；任务运行期间，会逐行显示智能体正在做什么，适合一次性任务、脚本和自动化流程。

本指南介绍 `icode run` 如何指定任务、智能体、模型、工作目录和会话，如何查看或隐藏运行进度，以及如何取得适合程序处理的 JSON 输出。

## 开始前

运行前需要完成以下准备：

- 已安装 iCode，并至少配置一个可用模型。请参阅[开始使用 iCode](../../start/getting-started.md)和[配置模型](../configuration/models.md)。
- 已确定要使用的智能体。内置的 `QA` 智能体在 TUI 中显示为“Q&A Agent”，其指令要求它不修改任何内容，但它可以执行 Shell 命令，且 `icode run` 执行这些命令前不会请求审批；`Code` 智能体可以修改文件和运行命令。自定义智能体的能力取决于其配置。请参阅[配置智能体](../configuration/agents.md)。
- 如任务可能修改文件，先保存未写入磁盘的工作，并在可恢复的工作目录中运行，例如已提交当前变更的 Git 仓库。

> **注意**
>
> `icode run` 始终绕过工具审批。智能体调用 Shell、写入文件或运行 Skill 时不会等待确认。只应在可信的工作目录中运行可信的智能体和任务。

`icode run` 在运行期间不能与人交互；应在启动前将任务要求和所需上下文写入提示词。

## 执行任务

在项目目录中运行命令，并通过 `-a` 或 `--agent` 指定智能体。任务文本含空格时使用引号：

```shell
icode run "概述这个项目的目录结构和主要模块" --agent QA
```

任务运行期间，iCode 会显示运行进度；任务完成后，先显示一行汇总，再显示智能体的最终回复。例如：

```text
• Q&A Agent ready · Example Model · session 8de5057d58ff · ~/projects/demo
I'll start with the top-level layout.
→ shell  ls
  ✓ 0.1s
→ read   README.md
  ✓ 0.0s

✓ Done · 8.2s · 2 tool calls · session 8de5057d58ff
The project has three main parts: ...
```

各行的含义请参阅[查看运行进度](#查看运行进度)。回复中可能改动终端显示的控制字符会显示为 `�`；将输出重定向到文件或其他程序，或添加 `--json` 时，回复保持原样。若任务失败，错误信息写入标准错误（`stderr`），命令以非零状态退出。iCode 能判断模型请求失败的原因时，错误信息会说明原因，下方的 `detail:` 行显示原始错误信息。

需要让智能体修改或验证代码时，可使用 `Code`：

```shell
icode run "修复登录表单的验证错误，并运行相关测试" --agent Code
```

这类任务可能修改工作目录中的文件并执行命令；运行前应确认提示词、智能体和当前目录都符合预期。

智能体转到后台的 Shell 命令（例如开发服务器）会在本次运行结束时停止，iCode 会显示一条警告，说明停止了多少条命令。详见[在后台继续运行的命令](../configuration/tools.md#在后台继续运行的命令)。

运行 `icode run -h` 或 `icode run --help` 可以查看当前版本支持的参数。

## 查看运行进度

进度行写入标准错误（`stderr`），最终回复写入标准输出（`stdout`）。在终端中两者都会显示；使用 `>` 保存回复或将回复交给其他程序时，只有回复会写入：

```shell
icode run "为最近的改动撰写发布说明" --agent QA > notes.md
```

每一行表示一个步骤：

| 行 | 含义 |
| --- | --- |
| `• Q&A Agent ready · …` | 智能体已启动，并显示其模型、会话和工作目录。 |
| 普通文本 | 智能体在步骤之间说明的内容，以及任务失败或被中断时它已写出的内容。 |
| `→ read   README.md` | 智能体调用工具，此处为读取文件。 |
| `✓ 0.1s` 或 `✗ exit 1 · 2.5s` | 工具已完成，或已失败并附原因，以及耗时。 |
| `↳ Explore → grep   TODO` | 子智能体的步骤，以子智能体名称开头。 |
| `↻ … Retrying in 7s (attempt 2/18)` | 模型请求因临时原因失败，将重试。 |
| `Todo 1/3 · → Running tests` | 智能体的待办列表已更新。 |
| `Warning: …` | 值得注意但不会中止任务的情况。 |
| `✓ Done · 8.2s · 2 tool calls · session …` | 任务已完成，显示耗时、智能体调用工具的次数和会话。 |

如只需查看警告、错误和最终回复，可添加 `-q` 或 `--quiet`：

```shell
icode run "概述这个项目" --agent QA --quiet
```

## 指定工作目录

`-C` 或 `--workdir` 指定智能体处理文件和执行命令时使用的工作目录，无需先在终端中切换目录：

```shell
icode run "检查未提交的变更并说明风险" --agent QA --workdir <project-directory>
```

将 `<project-directory>` 替换为实际项目路径。可以使用绝对路径，或相对于运行命令时所在目录的路径；路径必须指向一个已有目录。

## 从文件读取任务

较长、需要版本管理或由脚本生成的任务可以放在文本文件中，再通过 `-t` 或 `--task` 读取：

```shell
icode run --task prompts/review.md --agent Code --workdir <project-directory>
```

`--task` 不要求特定文件后缀。iCode 会读取该文件内容，并将其作为本次任务的提示词。任务文件路径可以是绝对路径，也可以是相对路径；使用相对路径时，iCode 会在 `--workdir` 指定的目录中查找该文件，未指定 `--workdir` 时，则在运行命令时所在的目录中查找。

一次运行只能使用一种提示词来源：直接传入提示词或使用 `--task`。两者不能同时使用。

## 选择智能体和模型

`icode run` 必须通过 `--agent` 指定智能体。运行 `icode agent list` 可以查看可用的智能体配置：

```text
Default  Name  Display Name  ID            Model
   *     Code  Code Agent    b011c0de0001  active
         QA    Q&A Agent     b0119a000005  Example Model
```

上述示例省略了部分行和列，`Example Model` 为虚构的模型配置。

`Name`、`Display Name` 和 `ID` 均可作为 `--agent` 的值。`Model` 为 `active` 表示使用当前生效的模型配置；显示具体名称表示使用已绑定的模型配置。

对于使用 `active` 模型的智能体，可以通过 `-m` 或 `--model` 为本次运行指定模型配置；对于已绑定模型的智能体，`--model` 不生效。

运行 `icode model list` 可以查看可用的模型配置：

```text
Active  ID            Name           Provider  API   Model          Context  Flags
   *    0123456789ab  Example Model  openai    chat  example-model  200k     stream
```

`Active` 列中的 `*` 表示默认生效的模型配置。`ID` 和 `Name` 均可作为 `--model` 的值。

## 取得 JSON 输出

添加 `--json` 后，成功结果以 JSON 对象写入标准输出（`stdout`），错误和警告以 JSON 对象写入标准错误（`stderr`），不显示运行进度。例如：

```shell
icode run "hello" -a QA --json
```

以下是一次运行的实际输出：

```json
{"session_id": "8de5057d-58ff-477e-9158-2f1b8d8e9fc0", "result": "Hello! I'm iCode, a read-only Q&A assistant for this codebase. I can help you with questions about the code, architecture, APIs, conventions, and how things work — with file and line references so you can jump straight to the source.\n\nWhat would you like to know?", "duration": 9.018}
```

其中，`session_id` 是本次任务所属的会话 ID，`result` 是智能体的最终回复，`duration` 是本次任务的耗时秒数。

读取不存在的任务文件时：

```shell
icode run --task missing.md -a QA --json
```

标准错误输出：

```json
{"error": "Task file does not exist: missing.md", "code": "task_file_not_found"}
```

`error` 是错误说明，`code` 是可供程序判断的错误类别。上述错误发生在会话创建前，因此没有 `session_id`；任务开始后发生错误（例如模型请求失败）时，输出还会包含 `session_id`。

## 继续已有会话

需要连续对话时，可使用 `-s` 或 `--session` 恢复已有会话，并基于已有对话继续提交任务。未指定 `-C` 或 `--workdir` 时，恢复后使用该会话保存时的工作目录，而不是执行命令所在的目录；指定 `-C` 或 `--workdir` 会改用指定目录。自动化任务需要确定文件操作位置时，应显式指定 `-C` 或 `--workdir`。如果该会话保存的工作目录已不存在，iCode 会提示错误并停止，此时可使用 `-C` 或 `--workdir` 在另一个目录中继续该会话。

在无界面模式下，可从第一轮 JSON 输出的 `session_id` 字段获取会话 ID：

```shell
icode run "检查当前改动并列出需要补充的测试" --agent Code --json
```

将返回的会话 ID 传给第二轮：

```shell
icode run "根据上一轮的结果，补充缺少的测试" --agent Code -s <session-id> --json
```

其中 `<session-id>` 填写第一轮返回的会话 ID。

通过 `icode run` 创建或继续的会话也会出现在 TUI 的“聊天会话”窗口中，勾选其中的“CLI”即可看到。参阅[恢复已有会话](../daily-use/sessions.md#恢复已有会话)。

## 退出状态

自动化脚本可以根据退出状态判断结果：

| 状态 | 含义 |
| --- | --- |
| `0` | 智能体已正常完成并返回最终回复。 |
| `1` | 配置、任务文件、智能体、模型、会话或任务执行出现错误。具体原因写入标准错误。 |
| `2` | 命令行参数无效，例如缺少 `--agent`，或同时提供或均未提供提示词和 `--task`。用法说明以纯文本写入标准错误，即使指定了 `--json` 也是如此。 |
| `124` | 内部操作超时。`icode run` 不限制任务的总运行时间。 |
| `130` | 运行被中断。 |
