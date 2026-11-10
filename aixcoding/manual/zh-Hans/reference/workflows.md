# 工作流参考

本页供工作流作者查询 Python API、数据与调度规则、执行环境、交互和运行记录。

## 工作流文件

### 文件定义与校验

工作流通过 `.py` 文件定义。下面是将输入原样返回的最小完整示例：

```python
from chrys.workflows import WorkflowBuilder, WorkflowValue

wf = WorkflowBuilder("Echo")


def echo(value: WorkflowValue) -> WorkflowValue:
    return value


node = wf.python("echo", echo)  # 注册节点
wf.start(node)                 # 声明唯一的顶层起点
wf.output(node)                # 至少声明一个输出，可以与起点相同
workflow = wf.build()          # 校验并构建，赋给模块顶层的 workflow 变量
```

模块顶层必须定义名为 `workflow` 的变量，其值为 `WorkflowBuilder` 实例调用 `build()` 返回的 `Workflow` 对象，如上例的 `workflow = wf.build()`。构建器变量名 `wf` 可以自定，入口变量名 `workflow` 固定。不要将 `workflow` 赋值放在函数内或 `if __name__ == "__main__":` 中。

语法错误、顶层代码执行异常、构建校验失败或缺少有效的 `workflow` 对象都会阻止加载。`build()` 的结构校验规则见[`WorkflowBuilder.build()`](#workflowbuilderbuild)。

运行前可用 [`icode workflow validate`](#icode-workflow-validate) 检查工作流，它会指出每个问题所在的文件和行。

### 文件发现

工作流可以是单个 `.py` 文件，也可以是工作流文件夹：文件夹中有一个与文件夹名完全相同的入口文件，例如 `code-review/code-review.py`。工作流 ID 是文件名去掉 `.py` 后的部分，或工作流文件夹的名称，与 `WorkflowBuilder` 的标题无关。iCode 在以下位置查找工作流：

| 位置 | 来源 |
| --- | --- |
| 当前工作目录的 `.chrys/workflows/` | `project` |
| iCode 配置目录下：Windows 为 `%APPDATA%\chrys\workflows\`；Linux、macOS 为 `~/.chrys/workflows/` | `global` |
| 随 iCode 提供的内置示例 | `builtin` |

多个工作流 ID 相同时，按以下顺序取第一个能读取的：

| 优先级 | 工作流 |
| --- | --- |
| 1 | 项目工作流文件夹 |
| 2 | 项目 `.py` 文件 |
| 3 | 全局工作流文件夹 |
| 4 | 全局 `.py` 文件 |
| 5 | 内置工作流 |

项目工作流只从当前工作目录的 `.chrys/workflows/` 发现，不向父目录查找。各位置只发现直接包含的 `.py` 文件和工作流文件夹，忽略以 `.` 或 `_` 开头的名称。没有同名入口文件的文件夹（如 `data/`、`venv/`）不是工作流，会被忽略；工作流文件夹里的子文件夹也不是工作流。内置工作流只有单文件。全局位置保留文件夹名 `sdk` 给 iCode 自用。

用户工作流文件不能是符号链接，需要通过文件所有权检查，源码大小上限为 4 MiB。以下情况的工作流文件夹会被跳过：文件夹本身或其中任何项是符号链接或 Windows 目录联接；其中有普通文件和文件夹以外的项（如命名管道）；文件和文件夹总数超过 1000 个、总大小超过 512 MiB，或嵌套超过 16 层。以 `.` 开头的名称（如 `.venv`、`.git`）和 Python 在 `__pycache__` 文件夹中保存的编译副本（如 `helpers.cpython-312.pyc`）不检查，也不计入上限；其他文件都计入，包括二进制文件和数据文件。无法读取的工作流会被跳过并报告原因，改用下一个同 ID 的工作流。

### 工作流文件夹

把工作流放进文件夹，即可拆成多个文件：

```text
.chrys/workflows/
  code-review/
    code-review.py        # 入口文件：与文件夹同名
    steps.py              # 用 `from steps import ...` 导入
    prompts/summary.md    # 相对 __file__ 读取
```

- 文件夹位于 `sys.path` 最前，与单文件所在目录相同：`import steps`、`from steps import check` 以及子文件夹中的模块（`from helpers.git import diff`，有无 `__init__.py` 均可）都能使用。相对导入（如 `from .steps import check`）不可用，因为入口文件不属于包。
- 其他文件相对 `__file__` 读取，例如 `Path(__file__).parent / "prompts" / "summary.md"`。`open("summary.md")` 这类相对路径相对的是工作区，而不是文件夹。
- 入口文件和文件夹中的其他文件不要与标准库或已安装的模块同名，例如 `json.py`、`pkgutil.py`。Python 可能加载该模块而不是你的文件，也可能用你的文件顶替该模块；工作流用 `multiprocessing` 启动的进程可能无法启动。
- 不要在其他文件中按名称导入入口文件：这会把入口文件作为另一个模块再执行一次。
- iCode 把工作流编译出的 Python 文件存放在配置目录下自己的文件夹中，运行工作流不会在你的文件旁生成 `__pycache__` 文件夹，也不会使用其中已有的你的文件的编译副本。工作流启动的 Python 进程同样如此，除非启动时没有沿用工作流的环境变量，或带有 `-E`、`-I`：这些进程照常使用 `__pycache__` 文件夹，包括确认不检查的编译副本。

把 `review.py` 移到 `review/review.py` 会改变工作流的路径，需要重新确认；为旧文件创建的工作流会话无法再运行它，请新建会话。

### 信任确认

首次加载自定义工作流时，需要确认信任：

- **TUI**：选择工作流后会弹出“信任工作流”对话框，检查源码及所声明的执行环境后，点击“信任”继续；点击“取消”则不加载。
- **CLI**：在 `icode workflow run` 命令中添加 `--trust`。例如，在项目根目录运行 `.chrys/workflows/echo.py`：

```shell
icode workflow run echo --trust --input "Hello"
```

确认会被保存，后续运行可省略 `--trust`。工作流源码、构建出的工作流定义，或所选解释器的路径、版本、平台及 iCode 提供的工作流 SDK 等环境信息发生变化后，需要重新确认。单个 `.py` 文件的源码就是该文件，不检查它导入的其他 Python 文件；工作流文件夹的源码是文件夹中除以 `.` 开头的名称和 Python 在 `__pycache__` 文件夹中保存的编译副本以外的所有文件，新增、修改或删除其中任何文件都需要重新确认。该检查不覆盖已安装依赖包的变化。

文件在预览和启动运行时检查，Python 加载时会重新读取。两者之间发生的修改，以及工作流运行中才读取的文件的修改，无法被发现。修改工作流文件夹中的文件后，请重新打开工作流进行预览和确认。

请在确认信任前检查源码。工作流代码可使用当前用户的权限读写文件或启动程序。确认信任后的预览就会执行模块顶层代码，正式运行时还会重新加载，因此顶层的文件写入、网络请求等操作可能在节点启动前发生，并重复执行。应将业务操作放入节点函数，顶层只保留导入、函数定义和工作流构建。

## Python API

`chrys.workflows` 提供以下 API：

| 名称 | 用途 |
| --- | --- |
| `WorkflowBuilder` | 创建顶层节点、连线、循环、起点和输出 |
| `BuilderScope` | 在循环体内创建节点和连线 |
| `NodeHandle` | 节点引用，由构建方法返回，`node_id` 为节点名称 |
| `Workflow` | `build()` 的结果，赋给顶层 `workflow` 变量 |
| `WorkflowValue` | 节点之间传递的文本与结构化数据 |
| `SourceValue` | 汇合函数收到的来源与结果 |
| `NodeContext` | Python 节点的进度报告与人工问答入口 |
| `Question` | `NodeContext.ask` 的一个问题，可带单选或多选选项 |
| `Option` | `Question` 的一个选项 |
| `Answer` | 用户对一个 `Question` 的回答 |
| `Retry` | 节点重试次数与间隔 |

### `WorkflowBuilder`

创建顶层工作流：

```python
WorkflowBuilder(title: str, *, description: str | None = None)
```

`title` 必须是非空字符串，`description` 是可选说明。两者不作为节点输入或智能体指令。

`WorkflowBuilder` 继承 `BuilderScope`，可直接调用其 [`python()`](#builderscopepython)、[`agent()`](#builderscopeagent)、[`edge()`](#builderscopeedge)、[`chain()`](#builderscopechain)、[`switch()`](#builderscopeswitch) 和 [`join()`](#builderscopejoin) 方法。

#### `WorkflowBuilder.start`

```python
start(node: NodeHandle) -> None
```

声明唯一的顶层起点，只能调用一次。`node` 必须是当前构建器创建的顶层节点引用。

#### `WorkflowBuilder.output`

```python
output(node: NodeHandle) -> None
```

指定收集 `node` 的输出结果，作为工作流的输出。`node` 必须是当前构建器创建的顶层节点引用；可以多次调用以收集不同节点的结果，不能重复声明同一个节点。

将节点声明为输出不会终止后续执行；其下游节点和其他可执行分支仍会继续运行，工作流会等待这些路径结束。结果的收集顺序和跳过行为见[输入调度与输出收集](#输入调度与输出收集)。

#### `WorkflowBuilder.build`

```python
build() -> Workflow
```

检查并冻结工作流，返回 `Workflow`，只能成功构建一次。

必须用 `start()` 指定一个顶层起点，并用 `output()` 指定至少一个输出节点。所有顶层节点都必须能从顶层起点沿连线到达；每个循环体内的节点也都必须能从该循环体的入口沿连线到达。

同一来源节点到同一目标节点不能重复连线，节点不能连向自身，也不能直接在循环体内外的节点之间连线。连线不能形成循环，例如 `A → B → A`。需要重复执行一组节点时，应使用 `loop()` 定义循环，而不是把下游节点连回上游节点。

#### `WorkflowBuilder.loop`

将一组节点封装为循环，重复执行，直到满足结束条件或达到次数上限。返回的 `NodeHandle` 代表整个循环节点，用于连接顶层的上下游节点。

```python
loop(name, body, until, max_iterations, on_exhausted="continue") -> NodeHandle
```

| 参数 | 规则 |
| --- | --- |
| `name` | 循环节点名称，在整个工作流内唯一；不能为空，不能以 `join:` 开头或包含 `->` |
| `body` | 构建循环体的函数，签名为 `(scope: BuilderScope) -> tuple[NodeHandle, NodeHandle]`。`scope` 是构建器传入的循环体作用域，用于创建节点和连线；返回 `(entry, exit)`，分别为循环体的入口和出口节点引用 |
| `until` | 判断是否结束循环的同步函数，签名为 `(value: WorkflowValue) -> bool`。`value` 是本轮出口节点的输出；返回 `True` 表示结束循环，`False` 表示尚未满足结束条件 |
| `max_iterations` | 最多执行的轮数，必填，至少为 1 的整数 |
| `on_exhausted` | 达到次数上限但仍未满足结束条件时的处理方式：`"continue"`（默认）或 `"fail"`，见下文 |

`body` 和 `until` 各接收一个位置参数；上述 `scope`、`value` 仅为示意名称，可自行命名。

**构建循环体**

`body` 在调用 `loop()` 时执行一次。返回的入口和出口必须属于该循环体，也可以是同一节点。

`body` 通过收到的 [`BuilderScope`](#builderscope) 实例创建节点和连线。循环体内不能再创建循环。

**执行与结束规则**

运行时，每轮执行已构建的循环体：

1. 第一轮将循环节点收到的输入传给 `entry`；后续轮次将上一轮 `exit` 的输出传给 `entry`。
2. 等待本轮循环体执行结束，取得 `exit` 的输出。如果本轮没有任何分支向出口传递结果（例如通向出口的唯一连线的 `when` 条件为 `False`），出口节点会被跳过，循环报 `loop_no_value` 错误。
3. 将出口值传给 `until`。返回 `True` 时结束循环，以该值作为循环节点的输出；返回 `False` 且尚未达到次数上限时，开始下一轮。

`until` 在每轮执行后检查，因此循环至少执行一轮。达到 `max_iterations` 且 `until` 仍返回 `False` 时，由 `on_exhausted` 决定结果：

| 值 | 行为 |
| --- | --- |
| `"continue"` | 结束循环，将最后一轮的出口值作为循环节点的输出，继续执行下游节点 |
| `"fail"` | 以 `loop_exhausted` 结束工作流运行 |

### `BuilderScope`

`BuilderScope` 提供创建节点和连线的实例方法，`WorkflowBuilder` 继承这些方法。在工作流顶层，通过 `WorkflowBuilder` 实例创建节点和连线；在循环体内，通过 `loop()` 的 `body` 回调接收的 `BuilderScope` 实例创建节点和连线，无需自行构造该实例。

**节点命名规则**：名称在整个工作流内必须唯一，不能为空，不能以 `join:` 开头或包含 `->`。

#### `BuilderScope.python`

定义一个执行 Python 函数的节点。

```python
python(name, fn, *, timeout=300.0, retry=None) -> NodeHandle
```

| 参数 | 含义 |
| --- | --- |
| `name` | 节点名称 |
| `fn` | 节点执行的 Python 函数，签名和返回值要求见下文 |
| `timeout` | 单次尝试的秒数上限，默认 300；有限正数或 `None`，`None` 表示不设截止时间 |
| `retry` | 重试配置，传入 [`Retry`](#retry) 实例；省略时仅执行一次 |

`fn` 支持同步函数和 `async def` 定义的异步函数。

`fn` 接收一个或两个位置参数：第一个参数是节点的输入值，类型为 [`WorkflowValue`](#workflowvalue)；第二个参数可省略，类型为 [`NodeContext`](#nodecontext)，用于报告进度或询问用户。两种签名示例如下：

```python
def transform(value: WorkflowValue) -> str:
    return value.text.strip()


def report(value: WorkflowValue, ctx: NodeContext) -> WorkflowValue:
    ctx.emit("Preparing result")
    return value
```

`fn` 的位置参数不能有默认值。`fn` 也不能声明 `*args`、`**kwargs` 或必填的关键字专用参数。

`fn` 必须返回字符串或 `WorkflowValue`。返回字符串时，iCode 将其转换为 `WorkflowValue(text=返回的字符串, data=None)`。

Python 节点中的相对文件路径以工作流会话的工作目录为基准。

#### `BuilderScope.agent`

定义一个执行智能体任务的节点。

```python
agent(
    name,
    *,
    profile,
    model=None,
    instructions_suffix=None,
    timeout=None,
    retry=None,
) -> NodeHandle
```

| 参数 | 含义 |
| --- | --- |
| `name` | 节点名称 |
| `profile` | 必填，已有智能体配置的 ID、名称或显示名称；建议使用 ID，通过 `icode agent list` 查看 |
| `model` | 可选，传入 iCode 模型配置的 ID 或名称，可通过 `icode model list` 查看；省略时按下文的模型选择规则确定 |
| `instructions_suffix` | 可选字符串，补充当前节点的指令 |
| `timeout` | 单次尝试的秒数上限，默认不限制；有限正数或 `None` |
| `retry` | 省略时最多三次尝试，间隔为零；不是所有错误都会自动重试 |

##### 智能体配置匹配

`profile` 按以下顺序匹配：

1. ID。
2. 名称精确匹配。
3. 显示名称精确匹配。
4. 名称忽略大小写匹配。
5. 显示名称忽略大小写匹配。

某一步匹配到唯一配置即使用该配置；没有匹配时继续下一步；匹配到多个配置时立即报错。所有步骤均未匹配时，也会报错。

##### 模型选择

`model` 按以下顺序匹配 iCode 模型配置：

1. ID 精确匹配。
2. 名称精确匹配。
3. 名称忽略大小写匹配。

某一步匹配到唯一配置即使用该配置；没有匹配时继续下一步；匹配到多个配置时立即报错。所有步骤均未匹配时，也会报错。

普通 iCode 智能体按以下优先级选择模型：

1. `agent()` 的 `model` 参数。
2. 智能体配置中绑定的模型。
3. 工作流默认模型。TUI 可在“运行设置”中选择；CLI 通过 `icode workflow run <工作流ID> --session <会话ID>` 加载已有工作流会话时，沿用该会话保存的模型选择。未单独选择工作流默认模型时，使用 [iCode 配置的默认模型](settings.md#智能体模型与请求)。

`model` 参数指定的模型不存在或不可用时会报错。智能体绑定的模型不存在时，会继续使用工作流默认模型。没有可用模型时，工作流无法启动。

##### 输入与输出

智能体使用上游传入的 `WorkflowValue.text` 作为任务输入，`WorkflowValue.data` 不会自动加入提示。

节点输出为 `WorkflowValue`，`text` 为智能体的回复文本，`data` 为 `None`。普通 iCode 智能体优先使用最终回复文本；最终回复无文本时，将本次执行过程中输出的文本按顺序拼接，作为节点输出，其中可能包含工具调用前后的过程说明。

##### 智能体配置生效范围

普通 iCode 智能体节点对配置项的支持如下（✓ 支持，✗ 不支持），字段格式和配置规则见[智能体配置文件参考](agent-profile.md)。

| 配置项 | 是否支持 | 节点中的行为 |
| --- | :---: | --- |
| `instructions` | ✓ | 使用配置中的指令，并在末尾追加传给 `agent()` 的 `instructions_suffix` 参数内容 |
| `model` | ✓ | 参与模型选择 |
| `tools.builtins` | ✓ | 加载内置工具；CLI 运行不提供“询问用户”工具 |
| `tools.shell_filter` | ✓ | 应用 Shell 命令过滤配置 |
| `tools.mcp` | ✓ | 加载 MCP 工具，应用允许工具范围、渐进披露和结果限制 |
| `approval` | ✓ | 与聊天模式使用相同的[工具审批规则](../guides/configuration/approval.md) |
| `compaction` | ✓ | 应用上下文压缩配置 |
| `sub_agents` | ✓ | 注册子智能体工具。子智能体失败时立即结束，并将错误返回给节点的模型，不提供“重试”或“中止”选项 |
| `skills` | ✓ | 与聊天模式相同，发现并加载 Skill，提供 `load_skill` 工具 |
| `memory` | ✓ | 加载配置的记忆文件或目录 |

各智能体节点使用独立的上下文。连线只传递节点输出，不会传递完整的对话历史或工具调用记录。

##### 外部 ACP 智能体

`profile` 可以直接引用已配置的 ACP 智能体，即使其标记为“仅用作子智能体”，也可作为工作流节点使用。可通过 [TUI 配置外部 ACP 智能体](../guides/extensions/external-acp-agents.md)，或[创建包含 `acp` 配置的智能体 YAML 文件](agent-profile.md#acp)。

ACP 节点有以下差异：

- 省略 `model` 时使用 ACP 配置中的远端模型设置，不使用工作流默认模型。
- 显式指定 `model` 时，填写 iCode 模型配置的 ID 或名称。iCode 仅使用其中的 `model_id`，通过 ACP 请求切换远端模型；该配置的其他字段不生效。模型未被远端列出，或切换请求因方法不支持、参数无效而被拒绝时，继续使用远端默认模型。
- `instructions_suffix` 附加到发送给外部智能体的用户提示（user prompt）末尾。
- 节点输出由外部智能体配置中的“结果”（[`acp.result_mode`](agent-profile.md#acp)）决定，可选择“最后一个消息片段”或“完整记录”，见[设置结果和超时](../guides/extensions/external-acp-agents.md#设置结果和超时)。
- 工具执行由外部智能体负责。只有外部智能体通过 ACP 发起权限请求的操作，才会进入 iCode 的审批流程。

#### `BuilderScope.edge`

从来源节点连向目标节点，可设置传递结果的条件。

```python
edge(src: NodeHandle, dst: NodeHandle, *, when=None) -> None
```

`src` 是来源节点，`dst` 是目标节点。来源节点完成后，沿连线向目标节点传递输出结果。

省略 `when` 时无条件传递；指定 `when` 时，该同步函数接收来源节点输出的 `WorkflowValue`，返回 `True` 时传递，返回 `False` 时不传递。同一个来源节点可以连接多个目标，各条连线的条件独立判断；多个条件成立时，会向对应的多个目标传递结果。

条件函数只能接收一个无默认值的位置参数。建议仅根据输入判断条件，避免修改外部状态。条件计算抛出异常会阻止来源节点正常完成，不会被视为条件不成立。

`src` 和 `dst` 必须由调用 `edge()` 的实例创建：通过 `WorkflowBuilder` 调用时，两端必须是该工作流的顶层节点；通过循环体的 `BuilderScope` 调用时，两端必须是该循环体内的节点。

#### `BuilderScope.chain`

按顺序连接多个节点。

```python
chain(*nodes: NodeHandle) -> None
```

至少传入两个节点。`chain(a, b, c)` 等价于 `edge(a, b)` 加 `edge(b, c)`。

`nodes` 中的所有节点必须由调用 `chain()` 的实例创建。

#### `BuilderScope.switch`

根据条件为来源节点选择一个下游分支。

```python
switch(
    src: NodeHandle,
    cases: Sequence[tuple[Callable[[WorkflowValue], bool], NodeHandle]],
    default: NodeHandle,
) -> None
```

`src` 是来源节点。`cases` 是按顺序排列的序列，每一项必须是 `(条件函数, 目标节点)` 二元组，目标节点为 `NodeHandle`。`default` 是必填的默认目标节点。

按 `cases` 序列中的顺序依次判断条件，遇到第一个返回 `True` 的条件时选择对应分支，不再判断后续条件；全部不满足时选择默认分支。

条件函数必须是同步函数，只能接收一个无默认值的位置参数，接收来源节点输出的 `WorkflowValue` 并返回布尔值。建议仅根据输入判断条件，避免修改外部状态。条件计算抛出异常会阻止来源节点正常完成，不会被视为条件不成立。

同一个来源节点只能定义一个 `switch()`；`switch()` 和 `edge()` 可以同时使用，互不排斥。

`src`、`cases` 中的目标节点和 `default` 必须由调用 `switch()` 的实例创建。

#### `BuilderScope.join`

汇合多个来源节点的结果，并传给目标节点。

```python
join(
    sources: Sequence[NodeHandle],
    dst: NodeHandle,
    *,
    combine=None,
) -> None
```

`sources` 是非空的 `NodeHandle` 序列，`dst` 是目标节点的 `NodeHandle`；`join()` 建立名为 `join:<目标名称>` 的汇合节点，再连向 `dst`。`dst` 不能再有其他直接入边。

`sources` 中的所有节点和 `dst` 必须由调用 `join()` 的实例创建。

未指定 `combine` 时，使用[默认合并规则](#输入调度与输出收集)。

指定 `combine` 时，该函数必须是同步单参数函数，接收 `list[SourceValue]`，返回字符串或 `WorkflowValue`。列表按 `sources` 的声明顺序排列，而不是完成顺序；被跳过或条件未选中的来源不会出现在列表里。

只要本次至少有一个来源提供输入，就会调用指定的 `combine`，包括只有一个输入的情况。如果没有任何来源提供输入，汇合节点和目标节点都会被跳过，不会调用 `combine`。

### `NodeHandle`

```python
class NodeHandle:
    node_id: str
```

节点引用，用于声明起点、输出和连线。必须使用 `python()`、`agent()` 或 `loop()` 返回的节点引用；直接调用 `NodeHandle("name")` 创建的实例不能用于构建工作流。

唯一的公开属性 `node_id` 为节点名称，不可修改。

### `Workflow`

```python
class Workflow:
    definition: WorkflowDefinition  # 只读属性
```

`WorkflowBuilder.build()` 返回的已构建工作流。将其赋给工作流文件顶层的 `workflow` 变量，供 iCode 加载和运行。

其 `definition` 属性保存构建后的工作流定义，包括标题、描述、起点、输出节点、节点与连线等信息。该 `WorkflowDefinition` 实例由 `build()` 自动创建，无需手动定义。

### `WorkflowValue`

```python
class WorkflowValue:
    text: str
    data: dict | list | str | int | float | bool | None = None
```

节点之间传递的值。`text` 为文本内容；`data` 为结构化数据，默认是 `None`，仅支持以下 JSON 值：

- 字符串（`str`）、整数（`int`）、有限浮点数（`float`）、布尔值（`bool`）和 `None`。
- 列表（`list`）和键为字符串的字典（`dict`）；允许嵌套，但列表元素和字典值也必须符合上述类型要求。

不支持集合（`set`、`frozenset`）、元组（`tuple`）、字节（`bytes`、`bytearray`）等非 JSON 类型，也不支持普通自定义对象、非字符串字典键，以及 `NaN`、正无穷和负无穷。大小限制见[数据与输出限制](#数据与输出限制)。

节点接收的输入由框架传入。Python 节点或 `combine` 返回字符串时，框架会自动将其转换为 `WorkflowValue`；需要返回结构化数据时，可手动创建 `WorkflowValue(text="...", data=...)`，其中 `text` 必填。

### `SourceValue`

```python
class SourceValue:
    node_id: str
    activation_id: str
    value: WorkflowValue
```

汇合函数 `combine` 接收的来源项，由框架创建，无需自行构造。

- `node_id`：来源节点的名称。
- `activation_id`：来源节点在本次工作流运行中的执行标识。循环体节点在不同轮次中具有不同标识。
- `value`：该次执行产出的 `WorkflowValue`。

### `NodeContext`

Python 节点函数可通过第二个位置参数接收 `NodeContext`，用于发送文本消息或询问用户。

每次调用节点函数都会创建新的 `NodeContext` 实例，包括循环中的每一轮和失败后的重试。因此，不应在 `NodeContext` 中保存需要跨调用保留的业务数据；节点之间的数据应通过返回值传递。

#### `NodeContext.emit`

```python
emit(text: str) -> None
```

发送任意文本消息，例如进度或提示。TUI 在“输出”页展示消息；CLI 文本模式将消息写入 stderr，`--json` 模式不显示。消息不影响函数返回的节点输出。

#### `NodeContext.ask`

```python
async ask(prompt: str) -> str
async ask(prompt: Question) -> Answer
async ask(prompt: list[Question] | tuple[Question, ...]) -> tuple[Answer, ...]
```

向用户提问并等待回答。只能在异步节点函数中通过 `await ctx.ask(...)` 调用。返回值取决于传入的参数：

- 传入字符串（不能为空）：提出一个开放式问题，返回用户输入的文本。
- 传入 [`Question`](#question)：可提供选项供用户单选或多选，返回一个 [`Answer`](#answer)。
- 传入包含 1 到 5 个问题的列表或元组：在同一个对话框中一并提问，每个问题一个标签页，按问题顺序返回 `Answer` 元组。只有一个元素的列表也返回元组。

传入其他类型的参数会引发 `TypeError`；传入空列表或超过五个问题会引发 `ValueError`。

TUI 使用与智能体提问工具相同的对话框显示问题。CLI 不支持交互，调用会以 `ask_unavailable` 失败。

等待回答的时间计入节点超时，`ask()` 不使用智能体提问工具的超时设置。如需一直等待用户回答，可在注册 Python 节点时设置 `timeout=None`，取消该节点的超时限制。

[会话目录](../guides/daily-use/sessions.md#查找会话-id-和会话保存位置)下该运行的 `workflows/<run_id>/events.jsonl` 只保存问题和回答的文本摘要（截断至 512 个字符），不保存完整问题。

### `Question`

```python
Question(
    question: str,
    header: str = "",
    options: list[Option | str] | tuple[Option | str, ...] = (),
    multi_select: bool = False,
)
```

`ctx.ask()` 的一个问题。字段名与智能体提问工具一致。

- `question`：问题正文，按 Markdown 渲染，不能为空。除消息大小上限外没有长度限制，因此可以把整篇草稿放进问题供用户审阅。
- `header`：问题标签页上的简短标题，最多 64 个字符。未设置时标签页显示问题序号。只有同时提出多个问题时才显示标签页，因此也才显示标题。
- `options`：最多 8 个选项。字符串是 `Option(label=...)` 的简写。没有选项时，问题只接受输入的回答。
- `multi_select`：为 `True` 时用户可以选择多个选项，此时至少需要一个选项。

选项标签会去除首尾空白，并且在同一问题内不能重复。所有字符串都必须是有效的 Unicode，不能包含未配对的代理字符。类型错误引发 `TypeError`，其他违规引发 `ValueError`；两者都在创建 `Question` 或 `Option` 时抛出，因此回溯信息会指向你的代码。

无论问题是否带有选项，用户都可以自行输入回答，或在选择选项时附加备注。

### `Option`

```python
Option(label: str, description: str = "")
```

- `label`：用户选择的内容，也是 `Answer.selected` 中返回的值，最多 200 个字符。
- `description`：显示在标签下方的一行说明，最多 500 个字符。

### `Answer`

```python
class Answer:
    selected: tuple[str, ...] = ()
    text: str = ""
    answered: bool       # 只读属性
    choice: str | None   # 只读属性
```

用户对一个 `Question` 的回答。

- `selected`：回答中与所提供选项标签一致的值，按选项顺序排列。点击选项与输入与标签完全相同（区分大小写）的文本，得到的回答相同。
- `text`：用户输入的其他内容：与任何标签都不匹配的自定义回答，或随选项一起给出的备注。
- `answered`：仅当用户跳过该问题时为 `False`。
- `choice`：唯一选中的标签；未选中任何选项时为 `None`。选中多个时引发 `ValueError`，多选问题请读取 `selected`。

每个回答都是以下三种形式之一：

| `selected` | `text` | 含义 |
| --- | --- | --- |
| 非空 | 备注，或 `""` | 选择或输入了所提供的选项标签；单选问题最多一个 |
| `()` | 非空 | 与任何标签都不匹配的自定义回答 |
| `()` | `""` | 已跳过 |

对话框的行为：

- 只有一个单选问题时，点击选项会立即提交，因此备注需要在点击前输入。不点击选项而直接提交时，输入的文本就是完整的回答。
- 有多个问题时，点击单选问题的选项会跳到下一个未回答的问题或“提交”标签页。多选问题和输入的回答通过“回答并继续”前进（最后一个问题为“回答并检查”）。“提交”标签页列出所有回答，通过“提交回答”发送；仍有问题未回答时按钮为“仍然提交”。一个问题都没有回答时，需要再按一次确认才会提交。
- 对话框无法用 Esc 关闭。问题会一直等待，直到用户回答，或所属的尝试或运行结束。

`Answer` 是本地结果，不能作为节点输出：节点应返回字符串或 `WorkflowValue`。如需把选择结果传给下游，请同时放入 `text`（智能体节点和默认汇合读取）和 `data`（供 Python 节点使用）：

```python
async def pick_areas(value: WorkflowValue, ctx: NodeContext) -> WorkflowValue:
    answer = await ctx.ask(Question("涉及哪些部分？", options=["API", "Storage", "UI"], multi_select=True))
    text = "\n".join(part for part in (", ".join(answer.selected), answer.text) if part)
    return WorkflowValue(text=text, data={"selected": list(answer.selected), "text": answer.text})
```

### `Retry`

```python
Retry(max_attempts: int, backoff: float = 0.0)
```

`max_attempts` 是包含第一次执行的总尝试次数，至少为 1；`backoff` 是有限、非负的固定秒数间隔。

通过 `python()` 或 `agent()` 的 `retry` 参数设置。是否重试还取决于错误类型，见[超时与重试](#超时与重试)。

## 数据与调度规则

### 数据与输出限制

工作流适合传递任务文本和有限的 JSON 数据。超出下表中的数据限制时会失败，不会自动截断后传给下游。深度、值总数和字符串长度限制适用于 Python 节点及自定义 `combine` 的返回值；返回值无法序列化时也会失败。

| 限制对象 | 上限 | 计算范围 |
| --- | --- | --- |
| 单次跨进程传输的消息大小 | 16 MiB | 按 UTF-8 编码后的整条消息计算，包括传递的值和框架附加的信息 |
| `data` 的嵌套深度 | 64 层 | `data` 本身为第 0 层，每进入一层列表元素或字典值，深度加 1 |
| `data` 中的值的总数 | 200,000 个 | 包括所有嵌套内容；每个列表、字典本身也各计 1 个，字典键名不计入。计数示例见下方 |
| `text` 或 `data` 中的单个字符串值 | 4,194,304 个字符 | 按字符数计算；整个传输消息仍需满足字节数限制 |
| 传给自定义 `combine` 的全部来源数据 | 合计 12 MiB | 合并前的所有来源一起计算，包括各自的 `text`、`data` 和来源标识，按 UTF-8 编码后的 JSON 大小计量 |

下面的 `data` 共计 7 个值：

```python
data = {                          # 外层字典：1 个
    "names": ["Alice", "Bob"],     # 列表本身 + 两个字符串：3 个
    "scores": [90, 80],            # 列表本身 + 两个数字：3 个
}
```

键名 `"names"` 和 `"scores"` 不计入。

进度消息和诊断输出另有限制。下表中的“一次尝试”指节点的一次执行，重试时重新计数。

| 输出方式 | 每次尝试的限制 | 超限后果 |
| --- | --- | --- |
| `ctx.emit()` 发送的消息 | 任意连续 1 秒内最多 200 条；累计最多 10,000 条；消息文本按 UTF-8 编码后累计最多 8 MiB。三项均须满足 | 本次尝试失败 |
| Python 节点直接写入 `sys.stdout` / `sys.stderr` 的内容（如 `print()` 的输出，不含 `ctx.emit()`） | 两者按 UTF-8 编码后合计保留前 64 KiB | 超出部分截断，不会仅因此导致节点失败，也不影响节点返回值 |

CLI 文本模式会将收到的 `ctx.emit()` 消息显示到 stderr，但这些消息仍按 `ctx.emit()` 的独立限额计算，不占用节点的 64 KiB 输出额度。

### 输入调度与输出收集

可以通过多次调用 `wf.edge()`，将多个上游节点连接到同一个节点。节点会等待所有入边都确定是否传递结果，再根据实际收到的结果决定执行或跳过。

除指定了自定义 `combine` 的汇合节点外，各类节点使用以下默认输入合并规则：

| 实际收到结果的来源数量 | 处理方式 |
| --- | --- |
| 零个 | 节点跳过；仅依赖它的后继也会跳过 |
| 一个 | 将该来源的 `WorkflowValue` 原样作为节点输入，保留 `data` |
| 多个 | 按入边声明顺序合并各来源的 `text`，作为节点输入；合并后的 `data=None` |

默认合并多个来源时，文本以来源节点名称作为标题，格式如下：

```text
## first_node
第一份输出

## second_node
第二份输出
```

满足执行条件的分支可以并行调度。当前每次运行最多同时执行四个智能体尝试，超出这一上限的智能体节点需等待名额。

工作流按 `output()` 的声明顺序收集输出节点的结果，只收集已完成且有结果的节点。跳过的输出节点不产生空占位，因此工作流可能完成却没有最终输出。

### 超时与重试

Python 节点和智能体节点的 `timeout` 限制单次尝试的执行时间，包含等待问答或审批的时间，每次重试重新计时。

同一节点的一批出边条件计算（包括 `switch` 判断）共享 30 秒上限。循环结束条件和自定义 `combine` 则各自每次计算最多允许 30 秒。这些计算均不受节点 `timeout` 设置影响。工作流源码加载与构建合计另有 60 秒上限。

节点失败后，是否自动重试取决于错误类型和节点的 `Retry` 配置：

| 情况 | 自动重试条件 |
| --- | --- |
| Python 节点的函数异常或执行超时 | 尚未达到 `Retry.max_attempts` |
| 非 ACP 智能体节点的模型请求遇到临时错误，如连接中断、限流、请求超时 | 先在本次尝试内重试该请求，与聊天模式一致；仍失败时，尚未达到 `Retry.max_attempts` 则重试节点 |
| 非 ACP 智能体节点的模型请求超出模型的上下文窗口 | 与聊天模式一致：开启上下文压缩时，先在本次尝试内压缩上下文并重新发送一次请求，除非报错给出的上限小于模型配置中的值；仍失败时不自动重试节点 |
| 非 ACP 智能体节点达到 `timeout` 上限 | 尚未达到 `Retry.max_attempts` |
| 外部 ACP 智能体节点连接中断、长时间无响应、报错或达到 `timeout` 上限 | 尚未达到 `Retry.max_attempts`。iCode 无法判断外部智能体报告的哪些错误无法恢复，因此都会重试；但智能体无法启动、本次尝试内多次重试连接后仍连不上、配置被拒绝，以及智能体拒绝回答、提前停止或回答为空时不重试 |
| 重试也无法解决的错误，如额度或套餐已用尽、外部智能体需要登录；以及条件或合并计算失败、返回值无法序列化或超限、运行环境不支持问答等 | 不自动重试 |

若重试可能让模型服务商再次运行它自己那边的工具（如服务商运行的 MCP 服务器或 Shell），则不自动重试节点。服务商运行的搜索和代码执行可以安全重复，不影响重试；iCode 自己的文件、Shell 和 MCP 工具也不影响。

智能体节点的模型一旦开始撰写回复，失败的请求就不会在同一次尝试内再次发送：本次尝试失败，节点按本节所述重试。

非 ACP 智能体的尝试失败或超时后，重试会接着已有的对话继续执行：已完成的工具调用不会再次执行，节点详情中新一次尝试的对话记录也接着上一次显示。被失败中断的回复会重新撰写，新一次尝试的对话记录中只显示新的回复。外部 ACP 智能体会以相同输入开启新会话，新一次尝试的对话记录只显示这个会话。若智能体已经完成、之后节点的其他步骤（如出边条件）失败，重试会从输入重新运行智能体。

`Retry(max_attempts=1)` 只关闭节点重试：失败的模型请求仍会在本次尝试内重试，除非模型已开始撰写回复。

TUI 中，节点失败且不再自动重试时（自动重试次数已用尽或不满足自动重试条件），会进入 `awaiting_retry`（等待手动重试）。用户可以查看错误诊断后重试，也可以取消整个运行。手动重试只适用于当前运行中处于这一状态的节点。

CLI 不支持手动重试。节点失败且无法继续自动重试时，整个运行结束；即使已有部分输出，整个工作流仍视为失败。

重试 Python 节点会重新执行整个函数，包括函数完成后在出边条件计算时失败的情况。循环节点自身在结束条件或出边条件计算时失败，手动重试只重新计算失败的条件，保留已经完成的循环进度。

重试可能重复文件写入、外部请求等操作，应确保这些操作可安全重复执行。超时或取消不会撤销已经发生的操作，也不保证立即停止正在运行的同步 Python 函数。

## 执行环境

工作流默认使用运行 iCode 的 Python 解释器，也可在工作流文件中指定已有的虚拟环境或 Python 可执行文件，支持 Python 3.9 及以上版本。

下面的例子使用项目根目录下已有的 `.venv`，工作流文件位于项目 `.chrys/workflows/`：

```python
# /// script
# [tool.chrys]
# python = "../../.venv"
# ///
```

保留示例中的 `#` 前缀和 `# ///` 标记，iCode 会读取这段注释中的配置。`[tool.chrys]` 是 iCode 专用配置区，`python` 的相对路径以工作流文件所在目录为基准。工作流文件夹的基准就是文件夹本身：项目根目录下的 `.venv` 写作 `../../../.venv`，文件夹中的写作 `.venv`。

`python` 也可直接填写 Python 可执行文件的路径，如 `"/opt/homebrew/bin/python3.12"`。iCode 不会通过 `PATH` 查找命令。使用 uv 创建的环境时，指向其 `.venv`。

iCode 启动解释器时会把环境变量 `PYTHONPYCACHEPREFIX` 设为自己存放编译文件的文件夹。用 `-E` 或 `-I` 启动 Python 的包装脚本会忽略该变量，工作流将无法加载。

iCode 不会自动安装依赖，请提前在所选环境中安装工作流所需的第三方包。`chrys.workflows` 由 iCode 在运行时提供，无需另外安装。

脚本元数据还支持以下两个顶层字段，写在 `[tool.chrys]` 之前：

| 字段 | 规则 |
| --- | --- |
| `requires-python` | 可选的 Python 版本约束字符串，例如 `">=3.10,<3.13"`。iCode 检查所选解释器是否满足约束，不满足时拒绝加载；不会自动寻找或安装其他版本。仅支持以逗号分隔、由 `~=`、`==`、`!=`、`<=`、`>=`、`<`、`>` 与纯数字发布版本组成的子句：`.*` 只能与 `==`、`!=` 搭配，`~=` 至少需要两段版本号。其他写法同样会拒绝加载 |
| `dependencies` | 可选的依赖字符串数组。非空时必须同时指定 `[tool.chrys] python`，否则拒绝加载，即使默认环境已经安装这些包。指定自备解释器后，iCode 不安装这些依赖，也不校验已安装版本是否符合声明 |

例如，以下声明要求使用 Python 3.10 至 3.12 的项目虚拟环境，并由作者预先安装 `requests`：

```python
# /// script
# requires-python = ">=3.10,<3.13"
# dependencies = ["requests"]
# [tool.chrys]
# python = "../../.venv"
# ///
```

## 会话、运行与记录

工作流会话独立于聊天会话，用于保存同一工作流在同一工作区中的多次运行。每次启动运行都会生成新的 `run_id`，使用本次提供的输入，不会自动带入历史对话。

### 会话设置

TUI 的“启动工作流”对话框提供“运行设置”。工作目录能否更改取决于会话是否已有运行，以及工作流的来源：

| 会话与工作流来源 | 工作目录规则 |
| --- | --- |
| 尚未运行的新会话，使用内置或全局工作流 | 可以在运行设置中更改工作目录 |
| 尚未运行的新会话，使用项目工作流 | 固定为发现该工作流的项目目录，即包含 `.chrys/workflows/` 的目录 |
| 已有运行的会话 | 工作区固定；更换目录或工作流需新建会话 |

包含 iCode 智能体节点时，运行设置还提供默认模型选择。

工作流会话不单独保存审批模式，而是与聊天模式共用 TUI 当前的审批模式，之后重新打开的会话同样如此。CLI 运行始终使用 `bypass`。

### 运行结果与恢复

重新打开会话后，可以查看历史或启动新运行，但不会从上次中断的节点续跑。已取消或中断的运行需要重新开始；当前运行中等待手动重试的节点按[超时与重试](#超时与重试)处理。

`icode workflow run --json` 输出的结果对象中，`outcome` 字段表示运行结果。本地记录也保存该字段，位于[会话目录](../guides/daily-use/sessions.md#查找会话-id-和会话保存位置)下 `workflows/<run_id>/events.jsonl` 的运行结束事件中。

进程异常终止、来不及收尾的记录，会在恢复会话时补记 `outcome=orphaned`，在工作流运行界面中显示为“进程退出时未完成”。

| 运行结果 `outcome` | 含义 |
| --- | --- |
| `completed` | 运行完成 |
| `node_failed` | 节点失败 |
| `loop_exhausted` | 循环按 `on_exhausted="fail"` 耗尽次数 |
| `cancelled` | 运行取消；`reason=deadline_exceeded` 表示运行总超时 |
| `worker_lost` | 执行工作流的进程丢失 |
| `storage_failed` | 运行记录无法写入 |
| `orphaned` | 恢复时发现原进程终止，运行未正常收尾 |

工具调用产生的文件修改按运行归属记录，但任意 Python 代码或外部程序的副作用不应假定都可追踪或撤销。取消、节点重试和再次运行均不自动回滚文件。

### 查看与导出记录

TUI 中通过运行页签切换同一会话的运行记录；退出后，切换到工作流模式，再从会话列表重新打开。节点详情保留循环中的不同轮次和重试的不同尝试。查看历史不会执行工作流源码。

[会话目录](../guides/daily-use/sessions.md#查找会话-id-和会话保存位置)下的 `workflows/<run_id>/` 保存源码快照、图定义、输入、最终输出、运行事件和节点记录；智能体对话记录也归属于该运行。这些文件用于保留记录，不支持工作流续跑。

运行和节点的用量、耗时可用于检查开销；ACP 用量取决于远端是否报告。需要导出分析时使用：

```shell
icode trajectory export --session <session-id> --format json --out workflow.json
icode trajectory export --session <session-id> --format perfetto --out workflow.perfetto.json
```

将 `<session-id>` 换成工作流会话 ID。工作流会话的分析导出仅支持 JSON 和 Perfetto 格式。

## 生命周期 Hooks

工作流使用已配置的外部 Hooks，配置文件格式见[Hooks 参考](hooks.md)。一个工作流会话可以包含多次运行。`session_*` 事件表示会话首次投入运行、恢复后开始运行或结束使用；`workflow_run_*` 事件表示每次运行的开始和结束。节点执行不会单独触发下表中的会话事件。

运行须先通过源码、执行环境和智能体配置等启动检查，才会触发会话开始或恢复事件，以及运行开始事件。未通过检查的启动请求不触发这些事件。

| 事件 | 触发时机 | 额外字段 |
| --- | --- | --- |
| `session_start` | 新建会话首次开始运行时，在 `workflow_run_start` 之前 | 无 |
| `session_restored` | 从保存记录加载的会话，在本次启动 iCode 后首次开始新运行时，在 `workflow_run_start` 之前 | `restored_session_id`：恢复的会话 ID |
| `session_end` | 本次启动 iCode 后已开始过运行的会话被删除、iCode 正常关闭或 CLI 运行命令正常结束时 | 无 |
| `workflow_run_start` | 每次运行通过启动检查后、执行节点前 | `run_id`、`input_text` |
| `workflow_run_end` | 每次运行完成收尾、确定最终结果后，包括失败和取消 | `run_id`、`outcome`、`reason` |

每次启动 iCode 后，同一会话仅在首次运行时触发 `session_start` 或 `session_restored`；每次被接受的运行各触发一次 `workflow_run_start` 和 `workflow_run_end`，节点重试不会重复触发。进程崩溃或被强制终止时，无法保证触发结束事件。

上述事件均包含 `session_kind: "workflow"`、`workflow_id`、`session_id` 和 `cwd`；`profile` 为空字符串，因为工作流会话不绑定单个智能体。其他通用字段见[Hooks 基础字段](hooks.md#基础字段)。

`workflow_run_start` 和 `workflow_run_end` 用于通知和记录。配置为阻塞式钩子时，iCode 会等待钩子结束，但不会采用其返回的操作决定来阻止运行或授予工具权限；`action: block` 会被忽略，钩子失败时的 `on_error: block` 按警告处理。

## 命令行

### `icode workflow list`

按[文件发现](#文件发现)规则列出工作流，不执行工作流代码。自定义工作流的标题取自此前保存的信任确认记录。不带命令运行 `icode workflow` 效果相同。

```shell
icode workflow list [--json]
```

默认以文本模式显示 `ID`、`Source`、`Title`、`Path`，未知标题显示为 `-`；工作流文件夹的 `Path` 是其入口文件。添加 `--json` 后输出一个对象，其 `workflows` 数组每项包含 `id`、`source`、`layout`（单个 `.py` 文件为 `file`，工作流文件夹为 `package`）、`title`、`path`（入口文件）；未知标题为空字符串。两种输出模式下，跳过文件的警告均写入 stderr。使用 `-h` / `--help` 查看帮助。

### `icode workflow validate`

在运行前检查工作流。该命令按运行时的方式加载工作流，像编译器一样指出每个问题所在的文件、行和列。

```shell
icode workflow validate <path> [--json]
```

`<path>` 是工作流 `.py` 文件或[工作流文件夹](#工作流文件夹)，相对于当前目录，不必位于 iCode 查找工作流的位置。对于文件夹，`code-review`、`code-review/` 和 `code-review/code-review.py` 都检查该文件夹。

校验会执行工作流的模块顶层代码，与在 TUI 中加载时相同，但不运行任何节点，也不调用模型。校验不询问也不记录信任确认，因此工作流是新的或已修改时，之后运行 `icode workflow run` 仍需 `--trust`。

检查按以下顺序进行，在第一个失败的阶段停止：

| 阶段 | 检查内容 |
| --- | --- |
| `resolve` | 路径指向一个工作流：`.py` 文件，或包含与文件夹同名入口文件的文件夹；不是链接，且名称会被 iCode 加载 |
| `read` | 每个文件都可读取且未超出大小上限，入口文件为 UTF-8 编码 |
| `metadata` | `# /// script` 块（如有）有效，见[执行环境](#执行环境) |
| `environment` | 工作流指定的 Python 解释器存在且能启动 |
| `load` | 文件夹中每个 `.py` 文件都能编译，顶层代码运行成功，`build()` 成功 |
| `graph` | 构建出的图有效；结构可疑时给出警告 |
| `bindings` | 每个智能体节点的智能体配置和模型在本设备上可用 |

#### 文本报告

每个问题显示为 `文件:行:列: error: 消息 [代码]`，下方是该行源码并标出位置。`note:` 行说明代码是如何执行到此处的，`help:` 行给出修改建议。加载期间工作流打印的内容显示在 `captured output (load):` 之下。最后一行是结果：

```text
./.chrys/workflows/code-review/steps.py:2:10: error: NameError: name 'summarise' is not defined [load_error]
    2 | PROMPT = summarise("diff")
      |          ^~~~~~~~~
  note: imported from ./.chrys/workflows/code-review/code-review.py:3
captured output (load):
  | loading steps
FAIL code-review (package): 1 error
```

通过校验时只输出一行，例如 `PASS code-review (package) · 1 node · 0 edges`。警告列在其上方，不会导致校验失败。只有工作流使用 Python 3.11 及以上版本时才显示列号。

#### JSON 报告

添加 `--json` 后，stdout 输出一个 JSON 对象。所有字段始终存在，未知的值为 `null`。

| 字段 | 类型与含义 |
| --- | --- |
| `version` | 整数：报告格式版本，当前为 `1` |
| `status` | 字符串：`pass` 或 `fail` |
| `target` | 对象：所检查的对象，包括 `path`、`layout`（`file` 或 `package`）、`workflow_id`、`entry`（入口文件）、`package_dir`、`source_digest` 和 `files`（文件数） |
| `stages` | 对象数组：按顺序列出各阶段的 `name` 和 `status`（`pass`、`fail` 或 `skipped`） |
| `diagnostics` | 对象数组：发现的问题，结构见下文 |
| `diagnostics_truncated` | 布尔值：部分加载问题被省略 |
| `sites_truncated` | 布尔值：工作流节点过多，无法报告每个节点的声明位置，图和绑定问题可能没有行号 |
| `workflow` | 对象或 `null`：工作流加载成功后，包含 `title`、`node_count`、`edge_count` 和 `outputs`（输出节点 ID） |
| `output` | 对象：加载期间打印的 `text`，以及是否被截断（`truncated`） |

`diagnostics` 中每项包含：

| 字段 | 类型与含义 |
| --- | --- |
| `severity` | 字符串：`error` 或 `warning` |
| `code` | 字符串：问题代码，见下表 |
| `stage` | 字符串：发现问题的阶段 |
| `message` | 字符串：问题描述 |
| `file` | 字符串或 `null`：文件的绝对路径 |
| `line`、`column`、`end_line`、`end_column` | 整数或 `null`：在文件中的位置，从 1 开始 |
| `node` | 字符串或 `null`：问题涉及的节点 ID，或写作 `source->target` 的边 |
| `source_line` | 字符串或 `null`：`line` 行的文本 |
| `notes` | 对象数组，每项包含 `message`、`file`、`line`：代码是如何执行到此处的，由内向外 |
| `hint` | 字符串或 `null`：修改建议 |
| `traceback` | 字符串或 `null`：加载失败时的 Python traceback，只保留你自己文件中的帧（不含标准库、已安装的包和 iCode 自身） |

| 代码 | 阶段 | 含义 |
| --- | --- | --- |
| `path_not_found` | `resolve` | 路径不存在。若有工作流使用该 ID，提示中会给出其路径 |
| `path_is_link` | `resolve` | 工作流或文件夹的入口文件是链接 |
| `path_not_workflow` | `resolve` | 路径不指向单个工作流：不是 `.py` 文件或文件夹，是存放工作流的目录（或包含它的项目文件夹、`.chrys` 文件夹）或工作流文件夹中的文件，文件夹的入口文件不是普通文件，或与所在文件夹列出的拼写不同 |
| `name_ignored` | `resolve` | iCode 从不加载该名称，例如以 `.` 或 `_` 开头的名称 |
| `name_reserved` | `resolve` | `sdk` 在全局工作流文件夹中是保留名称 |
| `entry_missing` | `resolve` | 文件夹中没有与其同名的入口文件 |
| `shadowed`（警告） | `resolve` | 另一个同 ID 的工作流优先被找到，运行时会使用那一个 |
| `source_too_large`、`package_too_large` | `read` | 入口文件或文件夹超出大小上限 |
| `source_unreadable`、`package_unreadable` | `read` | 文件无法读取 |
| `package_link`、`package_unsupported_file` | `read` | 文件夹中有链接，或有普通文件和文件夹以外的项 |
| `source_not_utf8` | `read` | 入口文件不是 UTF-8 编码 |
| `metadata_invalid` | `metadata` | `# /// script` 块无效 |
| `environment_invalid` | `environment` | Python 环境不可用 |
| `syntax_error` | `load` | `.py` 文件无法编译 |
| `load_error` | `load` | 顶层代码抛出异常 |
| `sdk_validation_error` | `load` | `WorkflowBuilder` 拒绝了某个声明或 `build()` |
| `missing_workflow` | `load` | 没有由 `build()` 构建的模块级 `workflow` |
| `load_timeout` | `load` | 加载未在时限内完成 |
| `worker_failed` | `load` | 加载工作流的进程出错 |
| `manifest_invalid` | `graph` | 构建出的图无效 |
| `loop_exit_all_conditional`（警告） | `graph` | 循环的出口节点只能经条件边到达，某次迭代可能没有结果值，导致运行失败 |
| `agent_profile_missing` | `bindings` | 智能体节点指定的智能体配置不可用 |
| `model_unresolvable` | `bindings` | 智能体节点没有可用的模型 |
| `internal_error` | 任意 | iCode 自身在所示阶段出错 |

#### 退出码

| 退出码 | 含义 |
| --- | --- |
| `0` | PASS，可能带有警告 |
| `1` | FAIL |
| `2` | 参数解析错误 |
| `130` | CLI 捕获到键盘中断 |

### `icode workflow run`

运行指定工作流，完成后输出结果。

#### 参数

```shell
icode workflow run <workflow-id> [--input TEXT] [-s SESSION] [--trust] [--timeout SECONDS] [--json] [-q]
```

| 参数 | 默认值与用途 |
| --- | --- |
| `<workflow-id>` | 文件名去掉 `.py` 的部分，或工作流文件夹的名称，可由 `icode workflow list` 查询 |
| `--input TEXT` | 默认空字符串，传给起点的 `WorkflowValue.text`（见[多行输入](#多行输入)） |
| `-s` / `--session` | 加载已有工作流会话，为该会话绑定的工作流发起新运行；不能续跑旧运行。该会话必须是至少已有一次运行的工作流会话；`<workflow-id>` 必须与会话绑定的工作流一致，否则运行会被拒绝，错误代码为 `spec_changed` |
| `--trust` | 信任当前自定义源码及环境；内置工作流无需此选项 |
| `--timeout SECONDS` | 默认无总时限；必须为有限正数，覆盖预览、工作流加载和执行；不包含会话恢复、部分初始化及清理时间，超时后仍等待清理完成 |
| `--json` | 使用 JSON 输出 |
| `-q` / `--quiet` | 不在 stderr 显示进度；仍显示警告、错误、加载输出和节点外输出，以及最终输出 |
| `-h` / `--help` | 查看帮助 |

#### 多行输入

`--input` 中换行的写法取决于 shell。bash 和 zsh 中使用 `$'...'` 引号，用 `\n` 表示换行：

```shell
icode workflow run demo-workflow --input $'interactive: false\ndepth: deep\nhow are errors handled?'
```

PowerShell 中在双引号内用 `` `n `` 表示换行：

```powershell
icode workflow run demo-workflow --input "interactive: false`ndepth: deep`nhow are errors handled?"
```

PowerShell 不认识 `$'...'`，会把文本原样作为一行传入，其中的 `\n` 是两个普通字符。

#### 文本输出

不加 `--json` 时使用文本模式，将最终输出按声明顺序拼接到 stdout，多份结果之间空一行。进度逐行写入 stderr，每个步骤一行：节点状态、智能体节点的工具调用及其间写下的说明、`ctx.emit()` 进度、循环迭代，以及运行成功时最后的 `✓ Workflow completed` 行。加载输出、节点外输出等运行诊断也写入 stderr。`--quiet` 隐藏进度，但保留警告、错误，以及加载输出和节点外输出。

#### JSON 输出

JSON 模式不向 stderr 输出节点状态、`ctx.emit()` 进度或循环迭代消息；警告和错误仍写入 stderr。生成运行结果后，向 stdout 输出一个单行 JSON 对象。下表列出该对象的全部顶层字段；`diagnostics` 和 `outputs` 的内部结构分别在后文说明。

| 字段 | 类型与含义 |
| --- | --- |
| `session_id` | 字符串或 `null`：会话标识；没有可用标识时为 `null` |
| `run_id` | 字符串：本次运行标识 |
| `outcome` | 字符串：运行结果，见[运行结果与恢复](#运行结果与恢复) |
| `reason` | 字符串：结果原因代码，无原因时为空字符串 |
| `node_id` | 字符串：相关失败节点，无相关节点时为空字符串 |
| `error` | 字符串：错误说明，无错误时为空字符串 |
| `duration` | 数值：CLI 统计的耗时秒数，含准备过程 |
| `diagnostics` | 对象或 `null`：运行诊断；无可用诊断时可为 `null` |
| `outputs` | 对象数组：最终输出，按输出声明顺序排列；没有结果时为空数组 |

`diagnostics` 为对象时，结构如下，不包含节点诊断文件中的输出。示例仅展示该字段的值；为便于阅读添加了换行和注释，实际输出是无注释的单行 JSON。

```jsonc
{
  "load": {
    // 字符串：加载工作流文件时捕获的输出，例如文件顶层的 print()
    "text": "加载时的输出\n",
    // 布尔值：加载输出是否因超过捕获上限而截断
    "truncated": false
  },
  "native": {
    // 字符串：无法关联到具体节点执行尝试的输出，
    // 例如子进程或原生扩展直接写出的内容；超过捕获上限时只保留末尾部分
    "text": "",
    // 整数：因捕获上限而丢弃的字节数
    "dropped_bytes": 0
  }
}
```

诊断收集或读取失败时，对象中会包含字符串字段 `error`，并可能缺少 `load` 或 `native`。例如读取失败时：

```jsonc
{
  "error": "诊断读取失败的具体原因"
}
```

`outputs` 的每个元素都是一个对象，包含以下字段：

| 字段 | 类型与含义 |
| --- | --- |
| `node_id` | 字符串：输出节点的 ID |
| `activation_id` | 字符串：产生该输出的节点激活标识 |
| `text` | 字符串：节点返回的 `WorkflowValue.text` |
| `data` | JSON 值：节点返回的 `WorkflowValue.data`，可以是对象、数组、字符串、数值、布尔值或 `null`；内部结构由工作流定义，没有固定字段 |

#### 节点诊断

文本和 JSON 模式均会保存节点诊断记录。节点内 `print()` 的输出只写入节点诊断记录，不输出到 CLI 的 stderr，也不包含在结果 JSON 的 `diagnostics` 中。

节点诊断记录位于[会话目录](../guides/daily-use/sessions.md#查找会话-id-和会话保存位置)下的 `workflows/<run_id>/nodes/`，文件名通常为 `<activation_id>.<attempt>.diagnostics.<hash>.json`，分别对应节点激活标识、执行尝试编号，以及记录标识的 8 位哈希（用于在文件名净化后仍保持唯一）。其中 `phases[].stdout.text` 保存捕获的文本，`phases[].stdout.truncated` 表示是否因捕获限制而截断；没有诊断内容时不生成文件。也可在 TUI 节点详情的“输出”页签中查看对应执行尝试的诊断。

#### 错误处理与退出码

文本和 JSON 模式使用相同的退出码。运行失败或取消时，文本模式向 stderr 输出错误文本，JSON 模式向 stderr 输出错误 JSON；参数解析错误在两种模式下均以文本形式写入 stderr。

失败或取消且已经生成运行结果时，stdout 仍可能包含输出：文本模式输出已收集的最终结果文本，JSON 模式输出结果对象。若在生成运行结果前失败，例如找不到工作流或尚未确认信任，则不保证产生 stdout 结果。脚本应检查退出码，不能仅凭 stdout 有内容或 JSON 中的 `outputs` 非空判断成功。

| 退出码 | 含义 |
| --- | --- |
| `0` | 完成 |
| `1` | 运行失败、被拒绝或普通取消等错误 |
| `2` | 参数解析错误 |
| `124` | 工作流运行超时 |
| `130` | CLI 捕获到键盘中断 |
