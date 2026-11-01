# TUI 斜杠命令参考

斜杠命令是在终端用户界面（Terminal User Interface，TUI）输入栏中使用的快捷命令，可用于管理会话、切换配置、查看信息和打开功能窗口等。

## 输入斜杠命令

斜杠命令必须从输入内容的开头开始，命令及其参数写在同一行。

- 正例：`/diff` 会作为命令执行。
- 反例：`请执行 /diff` 不会触发命令。

输入 `/` 后，输入栏上方会显示命令建议列表。继续输入字符可以筛选命令；按 **Tab** 可以补全当前选中的命令。如果命令不显示参数建议列表，补全后可直接按 **Enter** 执行；命令接受参数时，也可以继续输入参数，再按 **Enter** 执行。

以下命令会显示参数建议列表：`/theme`、`/language`、`/approval`、`/buddy`、`/agents`、`/settings` 和 `/man`。按 **Tab** 补全这些命令时，或输入完整命令后按空格键或 **Enter**，都会打开列表。

- 示例：输入 `/approval` 后按空格键，会显示 `Manual`、`Auto` 和 `Bypass`。

参数建议列表出现后，可以使用上下方向键选择并按 **Enter** 执行，也可以用鼠标点击所需项目。

## 查看命令说明

输入 `/man` 可以查看内置斜杠命令的说明：

- `/man`：按空格键或 **Enter** 显示可查看的命令列表，选择命令后打开详细说明。
- `/man <command>`：直接查看指定命令的详细说明，`command` 可以是命令名称或别名，例如 `/man theme`、`/man diff` 或 `/man quit`。

## 命令参考

下表列出 iCode 的内置斜杠命令。

表中：

- `<arg>` 表示必填参数，`[arg]` 表示可选参数。
- `|` 表示可任选其一的值。例如，`[N|all]` 表示可以输入正整数或 `all`。
- `<`、`>`、`[`、`]` 仅用于表示参数，不应作为命令的一部分输入。

| 命令与用法 | 别名 | 说明 | 选项 |
| --- | --- | --- | --- |
| `/new` | — | 开始新会话并保留当前会话。详见[切换到新会话](../guides/daily-use/sessions.md#切换到新会话)。 | — |
| `/clear` | — | 删除当前会话并开始新会话；确认后无法恢复。详见[切换到新会话](../guides/daily-use/sessions.md#切换到新会话)。 | — |
| `/exit` | `/quit` | 退出 iCode 并返回终端。 | — |
| `/resume` | — | 恢复最近的聊天会话，工作流会话会被跳过。详见[恢复已有会话](../guides/daily-use/sessions.md#恢复已有会话)。 | — |
| `/fork` | — | 从当前会话创建一个独立分支。创建后，当前窗口仍停留在原会话，可以选择留在原会话或切换到分支。 | — |
| `/rename [title]` | — | 设置或清除会话标题。详见[修改会话标题](../guides/daily-use/sessions.md#修改会话标题)。 | `title`：直接应用的自定义标题。省略或只输入空白时打开标题编辑器。 |
| `/sessions` | — | 打开会话窗口，以浏览、恢复或删除会话。详见[管理与恢复会话](../guides/daily-use/sessions.md)。 | — |
| `/theme [theme]` | — | 切换界面主题。详见[外观设置](../guides/configuration/settings.md#外观)。 | `theme`：直接切换到指定主题。省略参数时，按空格键或 **Enter** 显示可用主题。 |
| `/language [locale]` | — | 切换界面显示语言。详见[外观设置](../guides/configuration/settings.md#外观)。 | `locale`：直接切换到指定语言。当前支持 `system`（跟随系统）、`en`（英文）和 `zh-Hans`（简体中文）。省略参数时，按空格键或 **Enter** 显示可用值。 |
| `/chdir [path]` | `/cd` | 切换当前会话的工作目录。详见[在会话中切换工作目录](../guides/daily-use/workspaces.md#在会话中切换工作目录)。 | `path`：已存在的目标目录，可以使用绝对路径或相对于当前工作目录的路径。省略时打开目录选择器。 |
| `/copy [N]`<br>`/copy agent [N\|all]`<br>`/copy user [N\|all]`<br>`/copy all` | — | 将对话内容复制到剪贴板。 | 省略参数：复制最近一条智能体消息（智能体在工具调用之间显示的文本也算作单独一条）。<br>`N`：要复制的最近智能体消息数，必须是正整数；`/copy N` 是 `/copy agent N` 的简写。<br>`agent [N\|all]`：复制最近 `N` 条或全部智能体消息；省略数量时复制最近一条。<br>`user [N\|all]`：复制最近 `N` 条或全部用户消息；省略数量时复制最近一条。<br>`all`：复制完整对话记录。 |
| `/fold` | — | 统一切换执行过程块、工具调用组、**思考**和工具调用之间的文字。有展开的内容就全部收起，否则全部展开，包括过程块里的详情；仍在写出的思考和等待你输入的控件除外。 | — |
| `/diff` | — | 打开差异视图，逐行显示当前会话产生的新增、修改和删除文件，便于检查智能体所做的更改。当前工作目录内的文件显示在目录树中，目录外的文件显示在 **External（外部）** 分组中；正在运行的轮次也按此规则显示。 | — |
| `/rollback`<br>`/rollback <N>`<br>`/rollback to <N>` | — | 回滚分为两种：仅回滚对话并保留当前文件变更；同时回滚对话，并恢复 iCode 能够恢复的文件变更。回滚还会停止被丢弃轮次启动的[后台命令](../guides/configuration/tools.md#在后台继续运行的命令)。回滚结果无法撤销。 | 省略参数：打开回滚选择器，默认选择最近的可回滚目标。通过“回滚”窗口左下角的文件还原选项（名称中会显示文件数量），可以决定是否同时恢复文件变更。<br>`N`：立即丢弃最近 `N` 轮，必须是正整数。从一次用户提交开始，到该次智能体运行结束为一轮。<br>`to N`：保留第 1 轮至第 `N` 轮并丢弃之后的轮次；`N` 必须是非负整数，`0` 表示回到会话开始处。<br>`/rollback N` 和 `/rollback to N` 都会立即执行（只有会停止正在运行的后台命令时才会先询问），并恢复被丢弃轮次中 iCode 能够恢复的文件变更。 |
| `/approval [manual\|auto\|bypass]` | — | 切换当前审批模式，所选模式也会保存为下次启动的默认模式（`bypass` 保存为 `auto`）。详见[配置审批模式](../guides/configuration/approval.md)。 | `manual`：需要审批的调用由用户决定。<br>`auto`：由审批裁判模型判断；无法确认安全的调用仍交由用户决定。<br>`bypass`：跳过工具调用审批。<br>省略参数：按空格键或 **Enter** 显示审批模式建议列表。 |
| `/models` | — | 打开模型配置窗口。详见[配置模型](../guides/configuration/models.md)。 | — |
| `/buddy [command]` | — | 管理 Buddy 伙伴。尚未孵化时，只能使用 `hatch`；孵化后可使用其余选项。 | 省略参数：按空格键或 **Enter** 显示可用命令。<br>`hatch`：孵化新伙伴。<br>`info`：显示伙伴信息。<br>`pet`：与伙伴互动并生成回复。<br>`mute`：开启或关闭伙伴通知。<br>`name <new-name>`：将伙伴重命名为 `new-name`。 |
| `/agents [target]` | `/agent`<br>`/config` | 打开智能体配置窗口。详见[配置智能体](../guides/configuration/agents.md#打开智能体配置窗口)。 | 省略参数：按空格键或 **Enter** 显示可用标签页。<br>`target`：直接打开指定标签页。可用值为 `basic`、`instructions`、`tools`、`sub-agents`、`skills`、`mcp`、`memory` 和 `compaction`。<br>参数别名：`subagents` 等同于 `sub-agents`，`skill` 等同于 `skills`。 |
| `/runtime` | `/details` | 打开“运行时详情”窗口，查看当前模型配置和模型 ID、按类别分组的内置工具、上下文管理工具、子智能体工具、按服务器分组的 MCP 工具、按来源目录分组的快捷指令、按来源分组的 Skills、已加载的钩子，以及预配置记忆文件。 | — |
| `/usage` | — | 打开 token 用量窗口，查看所有会话的合计、趋势和速度。详见[查看 token 用量和速度](../guides/daily-use/sessions.md#查看-token-用量和速度)。 | — |
| `/settings [tab]` | — | 打开设置窗口。详见[配置 iCode 设置](../guides/configuration/settings.md#设置窗口)。 | `tab`：直接打开指定标签页。可用值为 `general`、`models`、`safety`、`sessions`、`tools` 和 `notifications`。省略参数时，按空格键或 **Enter** 显示可用标签页。 |
| `/workflow` | — | 打开工作流模式并选择工作流。详见[创建和运行工作流](../guides/running/workflows.md)。 | — |
| `/help` | — | 打开“iCode 用户指南”窗口，查看本文档。也可以按 **F8** 或点击底栏的 `f8 帮助`。 | — |
| `/man [command]` | — | 显示 iCode 命令说明。 | 省略参数：按空格键或 **Enter** 显示可查看的命令。<br>`command`：命令名称或别名；显示指定命令的名称、用法、说明、别名和选项。 |

## 快捷指令

快捷指令是保存为 Markdown 文件的提示词，放在 `~/.chrys/shortcuts`（Windows：`%APPDATA%\chrys\shortcuts`）或项目的 `.chrys/shortcuts` 中。输入 `/` 后，它们列在“快捷指令”分组中；选中一个，或输入 `/名称 参数` 后按 **Tab** 或 **Enter**，提示词会填入输入栏，修改后再发送。如何编写，详见[编写和使用快捷指令](../guides/extensions/shortcuts.md)。

直接输入名称时，依次匹配内置命令、Skill、快捷指令。

## Skill 命令

运行时加载的 Skill 可以通过 `/skill-name` 形式调用。提交 Skill 命令后，iCode 会要求智能体在本轮任务中使用指定 Skill。

可以选择在 Skill 名称后添加任务描述。任务描述会随 Skill 命令一并提交，具体处理方式由 Skill 说明决定。

例如，已加载名为 `review` 的 Skill 时：

- `/review`：调用该 Skill。
- `/review 检查当前更改中的潜在问题`：调用该 Skill 并说明任务。

Skill 命令不属于 iCode 的内置命令。如果 Skill 名称与内置命令相同，内置命令优先。

Skill 的安装和使用方法见[安装和使用 Skills](../guides/extensions/skills.md#使用-skill)。
