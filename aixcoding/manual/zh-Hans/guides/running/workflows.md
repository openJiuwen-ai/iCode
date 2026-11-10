# 创建和运行工作流

工作流（workflow）将任务拆分为多个节点，并通过边定义执行顺序和数据流向。Python 节点用于确定性处理，智能体节点用于需要模型理解和生成的任务；分支、汇合和循环用于组织更复杂的执行流程。

本教程首先通过内置示例介绍工作流的终端用户界面（Terminal User Interface，TUI）和基本操作，再通过六个可独立运行的 Python 示例介绍如何编写工作流。完整参数说明和运行规则见[工作流参考](../../reference/workflows.md)。

## 在 TUI 中运行内置工作流

先[配置可用模型](../../start/getting-started.md#3-配置模型)，并准备一个本地项目目录。接下来运行的内置示例会让智能体读取该目录中的文件，生成项目介绍，并产生模型调用费用。

### 选择工作流

在终端中进入所选项目目录，运行 `icode` 打开 TUI。在输入框输入 `/workflow`，打开工作流选择器，选择来源为内置的 `demo-workflow`。

也可以点击左上角的“应用模式：聊天”，选择“工作流”切换模式，然后再点击“新建会话”并选择工作流。

### 了解工作流界面

选择示例后，可以通过以下页签了解工作流：

- **工作流**：展示执行图，包括节点、连接关系和执行状态。执行图超出面板时，可以按住鼠标拖动来移动视图。运行后，点击图中的节点可以查看该节点的输入、输出，下方的子页签可切换“Markdown”（默认）、“纯文本”（未经排版的原文）、“数据”（结构化数据按字段展示）和“进度消息”（节点运行时报告的进度），只显示有内容的页签；智能体节点还提供“对话记录”，用于查看模型回复和工具调用；智能体的每段文本在模型写完后才出现在其中。对话记录打开时显示最新内容；停留在底部时，新内容到来会随之显示；向上滚动查看之前的内容时，视图停在原处，直到滚回底部。
- **信息**：展示工作流名称、描述、脚本位置、执行环境和节点配置，包括智能体及模型。
- **源代码**：工作流通过 Python 代码定义，此处显示对应的源代码。“工作流”和“源代码”页均为只读；如需修改工作流，需要编辑对应的 Python 源代码文件。
- **输入**：启动后按 Markdown 格式展示本次运行提交的输入内容。点击“复制”可复制提交时的原始内容。
- **输出**：随运行更新节点状态和进度日志，并展示最终输出。

### 运行并观察工作流

工作流中的智能体沿用聊天模式的工具审批方式。运行前可通过右上角的“审批模式：…”调整审批模式，运行中按提示处理审批请求。

1. 在“工作流”页点击“▶ 开始”，打开“启动工作流”对话框。在其中的“运行设置”区域确认工作目录和默认模型。
2. 按需填写希望了解的项目内容，例如 `介绍这个项目的入口和主要模块，用中文回答`；也可以留空，使用示例的默认任务。点击对话框中的“▶ 开始”启动。

启动后，示例会询问阅读深度。可以选择 `deep`，体验多个智能体并行分析；希望减少调用量时可选择 `quick`，由一个智能体快速阅读。

运行期间，可以观察执行图中哪些节点正在运行、已经完成或被跳过。如需停止工作流，点击“■ 取消”并确认。

如果节点因错误进入“等待重试”状态，点击该节点查看错误详情，排查后点击“重试”；也可以关闭节点详情并取消整个运行。手动重试只适用于当前运行中等待重试的节点，不能用来重跑已完成节点或恢复已结束的运行。重试可能重复执行文件写入或外部请求，已发生的修改不会自动撤销。

使用工作流时，终端标签页显示工作流的标题。标题前出现转动的点（例如 `⠋`），表示运行正在进行；`!` 表示有节点在等待重试，`?` 表示工作流或其中的智能体在等你回答问题或批准工具调用。运行结束后，`✓` 表示运行完成，`✗` 表示运行失败；已取消的运行不显示标记。切换回聊天后，标签页重新显示聊天会话。

生成项目介绍后，示例会把草稿放在问题中，请求审阅。回答 `ok` 接受，或填写具体修改意见。示例最多进行两轮撰写，最终输出项目介绍；`deep` 路径还会输出后续阅读建议。

运行结束并产生结果后，点击“■ 取消”右侧的“结果”查看最终输出。工作流有多个输出时，每个输出单独一个页签，页签名为对应的输出节点名称。“输出”页仍保留完整记录，包括节点状态和进度日志。再次点击“▶ 开始”，可填写新的输入并重新运行。

同一工作流会话可以保存多次运行。在“工作流”“信息”等页签上方，点击“运行 1”“运行 2”等运行页签，可切换查看各次记录。退出并重新打开 iCode 后，先通过左上角的应用模式选择器切换到“工作流”。按 `F1` 或点击界面底部的 `f1 会话`，在会话列表中选择工作流会话，可查看历史输入、输出和节点记录。

## 创建第一个工作流

接下来介绍如何编写自己的工作流。先在项目目录下创建 `.chrys/workflows/` 目录（如果尚不存在），后续工作流文件都保存在这个目录下。

新建 `.chrys/workflows/greeting.py`：

```python
from chrys.workflows import WorkflowBuilder, WorkflowValue

wf = WorkflowBuilder(
    "greeting",
    description="Greet the person named in the input."
)


def greet(value: WorkflowValue) -> str:
    text = value.text
    name = text.strip() or "friend"
    return f"Hello, {name}!"


hello = wf.python("hello", greet)
wf.start(hello)
wf.output(hello)
workflow = wf.build()
```

`WorkflowBuilder` 的第一个参数设置工作流标题，可选参数 `description` 描述其用途。标题和描述用于展示工作流信息，不会作为节点输入或智能体指令。

节点是工作流中的执行步骤。`greet()` 定义本例的处理逻辑：接收 `WorkflowValue`，通过 `value.text` 读取输入，返回问候语。`wf.python("hello", greet)` 将该函数注册为名为 `hello` 的 Python 节点。注册时传入函数本身，不加调用括号。

`wf.start(hello)` 指定起点节点，`wf.output(hello)` 指定工作流的输出节点。本例中，`hello` 同时作为起点和输出节点，执行路径如下：

```text
输入 → hello 节点 → 工作流输出
```

最后，`wf.build()` 检查并生成工作流。**文件必须将生成的对象赋给模块顶层的 `workflow` 变量，供 iCode 加载。**

保存文件后无需重启 iCode。点击“新建会话”，打开工作流选择器，选择 `greeting`。

自定义文件首次加载或发生变化时，TUI 会要求检查源码并确认信任：工作流是可执行 Python，加载时就可能执行文件顶层代码。

确认信任后，点击“▶ 开始”，输入 `Alex` 后运行，最终应得到 `Hello, Alex!`；不填输入则得到 `Hello, friend!`。

## 使用其他 Python 环境

默认使用运行 iCode 的 Python 解释器。工作流需要第三方库时，可以通过文件内的脚本元数据指定自备 Python 解释器或虚拟环境；依赖需自行安装，iCode 不会自动安装。配置方式见[工作流参考：执行环境](../../reference/workflows.md#执行环境)。本教程的 Python 示例仅使用标准库，无需额外准备环境。

## 传递数据与询问用户

### 在两个节点之间传递数据

新建 `.chrys/workflows/fruit_list.py`：

```python
from chrys.workflows import NodeContext, WorkflowBuilder, WorkflowValue

wf = WorkflowBuilder("Fruit list")


def parse_items(value: WorkflowValue) -> WorkflowValue:
    text = value.text
    items = [item.strip() for item in text.split(",") if item.strip()]
    return WorkflowValue(text=", ".join(items), data={"items": items})


def describe(value: WorkflowValue, ctx: NodeContext) -> str:
    items = value.data["items"]
    ctx.emit(f"Received {len(items)} items; preparing the list")
    return f"{len(items)} items: {value.text}"


parse = wf.python("parse", parse_items)
report = wf.python("report", describe)
wf.start(parse)
wf.chain(parse, report)
wf.output(report)
workflow = wf.build()
```

本例中，`parse` 节点将输入整理为水果列表，`report` 节点根据列表生成说明。工作流的执行路径如下：

```text
输入 → parse 节点 → report 节点 → 工作流输出
```

`wf.chain(parse, report)` 连接两个节点，将 `parse` 的返回值作为 `report` 的输入。`chain()` 是连续调用 `edge()` 的简写：连接这两个节点时，等价于 `wf.edge(parse, report)`；连接更多节点时，`wf.chain(a, b, c)` 等价于依次调用 `wf.edge(a, b)` 和 `wf.edge(b, c)`。

节点之间通过 `WorkflowValue` 传递数据。它的 `text` 字段存放文本，`data` 字段可选，存放可用 JSON 表示的结构化数据，默认为 `None`。本例中，`parse_items()` 用 `text` 保存逗号分隔的水果名称，用 `data` 保存包含水果列表的字典；`describe()` 通过 `value.data["items"]` 读取列表，计算水果数量。

节点通过 `return` 返回处理结果，返回值决定向下游传递的内容：

- 返回字符串时，iCode 将字符串放入 `WorkflowValue.text`，并将 `data` 设为 `None`。本例中的 `describe()` 只需要输出说明，因此返回字符串。
- 返回 `WorkflowValue` 时，可以同时传递文本和结构化数据，如本例中的 `parse_items()`。

Python 节点函数可以接收一个或两个参数：`parse_items(value)` 接收 `WorkflowValue`；`describe(value, ctx)` 除了接收 `WorkflowValue`，还通过第二个参数接收执行上下文 `NodeContext`。`NodeContext` 提供与当前节点执行相关的操作。本例通过 `ctx.emit()` 向 TUI 报告当前节点的进度。

在 TUI 选择 `fruit_list`，输入 `apple, banana, pear` 并运行。运行完成后，在“输出”页查看工作流的最终输出，结果显示在输出节点名称下方：

```text
report
3 items: apple, banana, pear
```

### 在执行中等待用户回答

除了接收上游节点的数据，节点还可以在执行过程中向用户提问，并根据回答继续处理。

分别替换 `describe()` 函数和 `report = ...` 注册语句，保留中间的 `parse = ...` 及其他代码：

```python
async def describe(value: WorkflowValue, ctx: NodeContext) -> str:
    title = await ctx.ask("请为这份水果清单起一个标题。")
    items = value.data["items"]
    return f"{title}: {len(items)} items: {value.text}"


# 等待用户回答时不设置节点截止时间。
report = wf.python("report", describe, timeout=None)
```

保存修改后，在 TUI 中重新打开该工作流，预览更新后的源码并确认信任，然后启动。运行输入仍填 `apple, banana, pear`。问答窗口出现时输入 `Shopping List`，最终输出应为 `Shopping List: 3 items: apple, banana, pear`。

`ctx.ask()` 向用户提问，等待回答后返回字符串。调用时需要使用 `await`，因此节点函数应使用 `async def` 定义。包含用户问答的工作流需在 TUI 中运行，不支持通过无人值守的 CLI 运行。

问题还可以提供选项，一个对话框也可以同时提出多个问题。此时传入 `Question` 对象而不是字符串。先在文件顶部的 `from chrys.workflows import ...` 一行中加入 `Question`，再次替换 `describe()`：

```python
async def describe(value: WorkflowValue, ctx: NodeContext) -> str:
    title, extras = await ctx.ask(
        [
            Question("请为这份水果清单起一个标题。", header="标题"),
            Question("清单上还要加什么？", header="追加", options=["milk", "bread", "eggs"], multi_select=True),
        ]
    )
    items = value.data["items"] + list(extras.selected)
    return f"{title.text}: {len(items)} items: {', '.join(items)}"
```

传入问题列表时，按顺序为每个问题返回一个 `Answer`：`selected` 是用户选中的选项标签，`text` 是用户输入的内容。使用相同的输入运行。对话框为每个问题显示一个标签页：在“标题”页输入 `Shopping List` 并点击“回答并继续”，在“追加”页勾选 `milk` 和 `eggs` 并点击“回答并检查”，最后点击“提交回答”。输出应为 `Shopping List: 5 items: apple, banana, pear, milk, eggs`。

单选问题、回答包含的内容以及如何把选择结果传给下游，请参阅[工作流参考：`NodeContext.ask`](../../reference/workflows.md#nodecontextask)。

## 定义智能体节点

### 使用 iCode 智能体

新建 `.chrys/workflows/learning_plan.py`：

```python
from chrys.workflows import WorkflowBuilder, WorkflowValue

wf = WorkflowBuilder("Learning plan")


def make_prompt(value: WorkflowValue) -> str:
    text = value.text
    topic = text.strip() or "Python basics"
    return (
        f"Create a three-step learning plan for a beginner studying {topic}. "
        "Use one sentence per step. Answer in English using only this input. Do not call tools."
    )


prepare = wf.python("prepare", make_prompt)
plan = wf.agent("plan", profile="QA")
wf.start(prepare)
wf.chain(prepare, plan)
wf.output(plan)
workflow = wf.build()
```

本例中，`prepare` 节点组织提示词，`plan` 节点调用 QA 智能体生成学习计划。工作流的执行路径如下：

```text
输入 → prepare（Python）→ plan（智能体）→ 工作流输出
```

`wf.agent()` 用于定义智能体节点。在 `wf.agent("plan", profile="QA")` 中，`"plan"` 是节点名称，`profile="QA"` 选择智能体配置。使用以下命令查看可用配置：

```shell
icode agent list
icode model list
```

`profile` 填写 `icode agent list` 输出中的 `Name`，可选参数 `model` 填写 `icode model list` 输出中的 `Name`。需要为该节点指定模型时，替换注册语句，并将 `My model` 换成实际的模型配置名称：

```python
plan = wf.agent("plan", profile="QA", model="My model")
```

`model` 为该节点指定模型，优先于智能体绑定的模型配置。省略时，优先使用智能体绑定的模型配置，否则使用工作流默认模型。

智能体节点只读取上游节点传递的 `WorkflowValue.text` 作为用户消息，不会读取 `data`。如果模型需要使用 `data` 中的信息，应在上游 Python 节点中将相关内容整理到 `text` 中。智能体生成的回答保存在返回的 `WorkflowValue.text` 中，传给下游节点。

在 TUI 中新建会话，选择 `learning_plan` 工作流，输入 `Python file handling` 并运行。可打开 `plan` 节点的“对话记录”查看生成过程。运行完成后，“输出”页会显示一份三步学习计划，具体措辞由模型决定。

#### 补充节点指令

需要为节点补充固定职责或输出要求时，可以使用 `instructions_suffix`。例如，替换 `plan` 的注册语句，让学习计划适合没有编程经验的读者：

```python
plan = wf.agent(
    "plan",
    profile="QA",
    instructions_suffix="Assume the learner has no programming experience. Explain unfamiliar terms briefly.",
)
```

对普通 iCode 智能体，`instructions_suffix` 追加在配置原有的 `instructions` 后，作为系统提示词的一部分；上游传入的 `text` 仍作为用户消息。

### 使用 ACP 智能体

已经[配置外部智能体客户端协议（Agent Client Protocol，ACP）智能体](../extensions/external-acp-agents.md#创建配置并测试连接)时，可用其配置名称替换 `QA`：

```python
plan = wf.agent("plan", profile="My ACP agent")
```

将 `My ACP agent` 替换为 `icode agent list` 中已有的 ACP 配置名称，并确保该配置指定的外部程序可以启动。

ACP 节点默认使用外部智能体配置中的模型，不使用工作流默认模型。

ACP 节点也可以设置 `instructions_suffix`，但它会追加到发送给远端的用户提示末尾，不修改远端智能体的系统提示词。

## 设置超时与重试

Python 节点和智能体节点都可以设置超时和重试：`timeout` 限制节点每次执行的时间，`retry` 设置包含首次执行在内的最多尝试次数及重试间隔。

| 节点 | 默认单次超时 | 默认最多尝试次数（含首次） |
| --- | --- | --- |
| Python | 300 秒 | 1 次，不自动重试 |
| 智能体 | 不限制 | 3 次 |

例如，为前面的学习计划节点设置超时和重试。在 `learning_plan.py` 顶部补充 `Retry` 导入，并替换 `plan` 的注册语句：

```python
from chrys.workflows import Retry

plan = wf.agent(
    "plan",
    profile="QA",
    timeout=120,
    retry=Retry(max_attempts=3, backoff=2),
)
```

这里每次执行最多 120 秒，包含首次执行在内最多尝试三次，重试间隔为两秒。Python 节点也可以在 `wf.python(...)` 中使用相同的 `timeout` 和 `retry` 参数。

设置 `timeout=None` 表示不限制执行时间。等待人工回答或工具审批也会计时，因此前面的问答示例使用了 `timeout=None`。

重试可能重复调用模型、写入文件，或重复外部 ACP 智能体的工具调用。非 ACP 智能体的尝试失败或超时后，重试会接着已有的对话继续执行，已完成的工具调用不会再次执行。已经发生的操作不会自动撤销。并非所有错误都会自动重试，具体规则见[工作流参考：超时与重试](../../reference/workflows.md#超时与重试)。

智能体节点转到后台的 Shell 命令会在该次尝试结束时停止，重试不会沿用它们。详见[在后台继续运行的命令](../configuration/tools.md#在后台继续运行的命令)。

## 定义条件分支

### 使用 switch() 选择一条分支

新建 `.chrys/workflows/route_text.py`：

```python
from chrys.workflows import WorkflowBuilder, WorkflowValue

wf = WorkflowBuilder("Route by length")


def normalize(value: WorkflowValue) -> str:
    text = value.text
    return text.strip()


def is_short(value: WorkflowValue) -> bool:
    return len(value.text) <= 10


def is_medium(value: WorkflowValue) -> bool:
    return len(value.text) <= 20


def short_reply(value: WorkflowValue) -> str:
    text = value.text
    return f"Short text: {text}"


def medium_reply(value: WorkflowValue) -> str:
    text = value.text
    return f"Medium text: {text}"


def long_reply(value: WorkflowValue) -> str:
    text = value.text
    return f"Long text: {len(text)} characters"


check = wf.python("check", normalize)
short = wf.python("short", short_reply)
medium = wf.python("medium", medium_reply)
long = wf.python("long", long_reply)
wf.start(check)
wf.switch(
    check,
    cases=[(is_short, short), (is_medium, medium)],
    default=long,
)
wf.output(short)
wf.output(medium)
wf.output(long)
workflow = wf.build()
```

本例中，`check` 节点先去除输入两端的空白，再根据文本长度选择分支。工作流的执行路径如下：

```text
               ┌─ 长度 ≤ 10      → short  → 输出
输入 → check ──┼─ 10 < 长度 ≤ 20 → medium → 输出
               └─ 长度 > 20      → long   → 输出
```

`wf.switch(check, cases=[...], default=long)` 为 `check` 节点的输出选择一条分支。`cases` 中的每一项由条件函数和目标节点组成，例如 `(is_short, short)`。`switch()` 按声明顺序选择第一个条件函数返回 `True` 的目标；均不满足时选择 `default` 指定的节点。

条件函数接收来源节点输出的 `WorkflowValue`，返回布尔值。本例中的 `is_short()` 和 `is_medium()` 通过 `value.text` 判断文本长度，依次检查，均不满足时进入 `long`。长度不超过 10 的文本同时满足两个条件，但只会进入先声明的 `short` 分支。因此，`medium` 分支实际处理长度大于 10 且不超过 20 的文本。

三个分支节点均通过 `wf.output()` 声明为工作流的输出节点。每次运行只选择一条分支，最终结果只包含该分支的输出，跳过的分支不会产生空结果。

在 TUI 中新建会话，选择 `route_text` 工作流。分别使用以下输入运行，观察执行图中选中的路径与跳过的节点，并在“输出”页查看最终输出：

| 输入 | 最终输出 |
| --- | --- |
| `hello` | `Short text: hello` |
| `hello workflow` | `Medium text: hello workflow` |
| `hello workflow tutorial` | `Long text: 23 characters` |

### 使用 edge() 条件边执行多个分支

`wf.edge(来源节点, 目标节点, when=条件函数)` 连接两个节点。`when` 指定的条件函数接收来源节点输出的 `WorkflowValue`，返回 `True` 时，将来源节点的输出传递给目标节点；省略 `when` 时无条件传递。

各条条件边独立判断，可以选中多条路径。将本例中的 `wf.switch(...)` 调用替换为：

```python
wf.edge(check, short, when=is_short)
wf.edge(check, medium, when=is_medium)
wf.edge(check, long, when=lambda value: len(value.text) > 20)
```

保存修改后，在 TUI 中重新打开该工作流，预览更新后的源码并确认信任，然后输入 `hello` 并运行。输入同时满足 `is_short` 和 `is_medium`，因此两个节点都会执行。“输出”页会分别显示节点名称及其结果：

```text
short
Short text: hello

medium
Medium text: hello
```

## 并行处理并汇总结果

新建 `.chrys/workflows/text_stats.py`：

```python
from chrys.workflows import SourceValue, WorkflowBuilder, WorkflowValue

wf = WorkflowBuilder("Text statistics")


def normalize(value: WorkflowValue) -> str:
    text = value.text
    return text.strip()


def count_chars(value: WorkflowValue) -> WorkflowValue:
    text = value.text
    return WorkflowValue(text=str(len(text)), data={"chars": len(text)})


def count_words(value: WorkflowValue) -> WorkflowValue:
    text = value.text
    count = len(text.split())
    return WorkflowValue(text=str(count), data={"words": count})


def combine_stats(sources: list[SourceValue]) -> str:
    by_node = {source.node_id: source.value.data for source in sources}
    return f"Characters: {by_node['chars']['chars']}; words: {by_node['words']['words']}"


def format_report(value: WorkflowValue) -> str:
    text = value.text
    return f"Done. {text}"


prepare = wf.python("prepare", normalize)
chars = wf.python("chars", count_chars)
words = wf.python("words", count_words)
report = wf.python("report", format_report)
wf.start(prepare)
wf.edge(prepare, chars)
wf.edge(prepare, words)
wf.join([chars, words], report, combine=combine_stats)
wf.output(report)
workflow = wf.build()
```

执行路径如下：

```text
                  ┌→ chars ─┐
输入 → prepare ───┤         ├→ join:report → report → 输出
                  └→ words ─┘
```

`chars` 和 `words` 都在 `prepare` 完成后具备运行条件，彼此无需等待。`wf.join([chars, words], report, combine=combine_stats)` 建立汇合节点，等待两个分支完成，再调用 `combine` 参数指定的合并函数 `combine_stats()`，将其返回值作为 `report` 节点的输入。

合并函数接收 `SourceValue` 列表，通过 `source.node_id` 识别来源，通过 `source.value` 读取结果。本例按来源名称取出字符数和单词数。

合并函数可以返回字符串或 `WorkflowValue`，需要继续传递结构化数据时使用后者。

在 TUI 中新建会话，选择 `text_stats` 工作流，输入 `hello workflow` 并运行，“输出”页应显示：

```text
report
Done. Characters: 14; words: 2
```

需要合并多份文本时，可以省略 `combine`，将原来的 `wf.join(...)` 调用替换为：

```python
wf.join([chars, words], report)
```

本例的两个来源都会产生结果，默认合并按 `[chars, words]` 的声明顺序拼接各来源的 `WorkflowValue.text`，不保留 `data`，顺序与分支完成先后无关。如果将汇合用于条件分支，被跳过的来源不会参与合并；只有一个有效来源时，会原样传递其 `WorkflowValue`，包括 `data`。

保存修改后，在 TUI 中重新打开该工作流，预览更新后的源码并确认信任，再输入 `hello workflow` 并运行。“输出”页会显示：

```text
report
Done. ## chars
14

## words
2
```

其中，`Done.` 由 `format_report()` 添加。默认合并适合将多份智能体分析交给下游总结，无需自行编写合并函数。

## 循环执行直到满足条件

新建 `.chrys/workflows/count_up.py`：

```python
from chrys.workflows import (
    BuilderScope,
    NodeContext,
    NodeHandle,
    WorkflowBuilder,
    WorkflowValue,
)

wf = WorkflowBuilder("Count up to three")


def increment(value: WorkflowValue) -> str:
    text = value.text
    return str(int(text) + 1)


def report_value(value: WorkflowValue, ctx: NodeContext) -> WorkflowValue:
    ctx.emit(f"Current value: {value.text}")
    return value


def reached_target(value: WorkflowValue) -> bool:
    return int(value.text) >= 3


def make_body(scope: BuilderScope) -> tuple[NodeHandle, NodeHandle]:
    increment_node = scope.python("increment", increment)
    report_node = scope.python("report", report_value)
    scope.chain(increment_node, report_node)
    return increment_node, report_node


count = wf.loop(
    "count",
    body=make_body,
    until=reached_target,
    max_iterations=5,
    on_exhausted="fail",
)
wf.start(count)
wf.output(count)
workflow = wf.build()
```

`wf.loop()` 创建循环节点，本例使用的参数含义如下：

| 参数 | 含义 |
| --- | --- |
| `"count"` | 循环节点的名称 |
| `body=make_body` | 构建循环体的函数，声明内部节点及连线，返回入口和出口节点引用 |
| `until=reached_target` | 结束条件函数，接收本轮出口的 `WorkflowValue`；返回 `True` 时结束循环，返回 `False` 且未达到轮数上限时继续下一轮 |
| `max_iterations=5` | 最多执行五轮 |
| `on_exhausted="fail"` | 达到轮数上限仍未满足条件时，`"fail"` 使工作流失败；`"continue"` 则以最后一轮出口的结果作为循环输出，继续执行后续工作流 |

`make_body()` 在构建工作流时执行，通过 `scope` 创建并连接循环体内的节点。返回值 `(increment_node, report_node)` 按顺序指定入口和出口：

- **入口节点 `increment`**：接收每轮输入，并将数字加一。
- **出口节点 `report`**：发送进度消息并返回本轮结果，供结束条件判断使用。

本例的循环节点作为工作流起点，因此第一轮入口节点接收工作流输入。每轮出口节点的结果交给 `until` 指定的 `reached_target()` 判断：数字达到 3 时，结果成为循环节点的输出；否则，在未达到轮数上限时传给下一轮入口节点。

在 TUI 中新建会话，选择 `count_up` 工作流，输入 `0` 并运行。三轮结果依次为 `1`、`2`、`3`，最终输出为 `3`。可打开循环体节点，查看不同轮次的输入、输出和进度消息。再输入 `-10` 运行，观察达到五轮上限后工作流失败的结果。

## 把工作流拆成多个文件

工作流变大后，可以把它移进同名文件夹，拆成多个 Python 文件和资源文件。新建 `.chrys/workflows/greeting_kit/`，放入两个文件。入口文件必须与文件夹同名，即 `greeting_kit.py`：

```python
from pathlib import Path

from chrys.workflows import WorkflowBuilder, WorkflowValue
from phrases import greet

wf = WorkflowBuilder("greeting kit")
TEMPLATE = (Path(__file__).parent / "template.txt").read_text(encoding="utf-8")


def hello(value: WorkflowValue) -> str:
    return greet(TEMPLATE, value.text.strip() or "friend")


node = wf.python("hello", hello)
wf.start(node)
wf.output(node)
workflow = wf.build()
```

同一文件夹中的 `phrases.py`：

```python
def greet(template: str, name: str) -> str:
    return template.format(name=name)
```

再在文件夹中放一个 `template.txt`，内容为 `Hello, {name}!`。

在 TUI 中选择 `greeting_kit`，确认信任后输入 `Alex` 运行，结果为 `Hello, Alex!`。

- 按模块名导入文件夹中的其他文件，如 `from phrases import greet`。相对导入（如 `from .phrases import greet`）不可用。
- 资源文件相对 `__file__` 读取。`open("template.txt")` 这类相对路径相对的是工作区，而不是文件夹。
- 信任确认覆盖文件夹中除以 `.` 开头的名称（如 `.venv`、`.git`）和 Python 在 `__pycache__` 文件夹中保存的编译副本以外的所有文件。修改其中任何文件后，请重新打开工作流进行预览和确认。
- 在工作流选择器中删除工作流文件夹时，只删除其入口文件，其他文件保留。

完整规则见[工作流参考：工作流文件夹](../../reference/workflows.md#工作流文件夹)。

## 从命令行运行

CLI 适合无需人工交互的工作流：不支持 `ctx.ask()` 用户问答，工具审批模式固定为 `bypass`，即跳过工具审批。

### 列举和运行工作流

在项目目录执行以下命令，列举可用的工作流：

```shell
icode workflow list
```

要检查正在编写的工作流，将其文件或文件夹传给 `icode workflow validate`。它会输出 `PASS`，或列出每个问题所在的文件和行，见 [`icode workflow validate`](../../reference/workflows.md#icode-workflow-validate)：

```shell
icode workflow validate .chrys/workflows/greeting.py
```

使用列表中的工作流 ID 运行工作流。将以下命令中的 `WORKFLOW_ID` 替换为实际的 ID：

```shell
icode workflow run WORKFLOW_ID --input "输入文本" --trust
```

`--input` 设置起点节点收到的 `WorkflowValue.text`，省略时为空字符串。初始输入的 `data` 为 `None`，不能通过 CLI 参数直接设置；即使传入 JSON 字符串，也仍是文本，需要工作流自行解析。

输入跨多行时，bash 和 zsh 可用 `$'...'` 引号，以 `\n` 表示换行；PowerShell 中改在双引号内用 `` `n ``，例如 `` --input "第一行`n第二行" ``。见[多行输入](../../reference/workflows.md#多行输入)。

新建或修改工作流文件后，`--trust` 用于确认信任当前源码及运行环境，与在 TUI 中点击“信任”的作用相同。已确认的内容未变化时可以省略；具体检查范围和加载时的行为见[信任确认](../../reference/workflows.md#信任确认)。

要在 TUI 中查看从命令行启动的运行，打开“工作流会话”窗口并勾选“CLI”。

### 保存文本结果

默认输出各输出节点的 `text`，多份结果之间以空行分隔。最终结果写入 stdout（标准输出）。工作流运行期间，进度写入 stderr（标准错误），包括各节点的状态、智能体节点的操作（工具调用及其间写下的说明），以及 `ctx.emit()` 发送的消息。例如：

```text
• Starting workflow review…
Workflow Code review (review) · run 0b337219bb2e · session 890c8ee93562
▸ [plan] running
  [plan] → read   src/app.py
  [plan]   ✓ 0.1s
✓ [plan] completed · 4.2s
▸ [report] running
✓ [report] completed · 2.0s
✓ Workflow completed · 6.5s
```

在终端中，结果里的控制字符会显示为 `�`。可以通过 `>` 将最终结果原样保存到文件，进度仍显示在终端中：

```shell
icode workflow run WORKFLOW_ID --input "输入文本" > result.txt
```

如只需查看警告、错误和最终结果，可添加 `-q` 或 `--quiet`。

### 获取 JSON 结果

添加 `--json` 后，CLI 不显示节点状态和进度消息，而是在运行结束后向 stdout 输出 JSON 格式的运行结果，包括运行状态、输出节点名称，以及各结果的 `text` 和 `data`：

```shell
icode workflow run WORKFLOW_ID --input "输入文本" --json
```

完整 JSON 字段和退出码见[工作流参考](../../reference/workflows.md#命令行)。
