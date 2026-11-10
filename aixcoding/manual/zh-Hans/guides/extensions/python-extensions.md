# Python 扩展

扩展是扩充智能体能力的 Python 模块。它的每个部分都是一个 `async def` 函数：

- **工具**：供模型调用。模型能看到它的名称、说明和参数；模型调用该工具时，iCode 会运行你的函数，并把返回值交给模型。
- **上下文**：iCode 在每轮开始时向它询问智能体应当知道的文本，例如当前分支对应的工单。参阅[每轮为智能体提供上下文](#每轮为智能体提供上下文)。
- **工具钩子**：它能看到智能体发起的每个工具调用，可以在调用运行前拒绝或修改它，也可以在调用结束后修改模型读到的结果。参阅[检查或修改工具调用](#检查或修改工具调用)。

扩展在 iCode 内部以你的权限运行：你能读取和修改的内容，它都能读取和修改；智能体传给其工具的内容，它也都能看到。带有工具钩子的扩展能看到智能体发起的每个工具调用及其结果。请只安装你信任的扩展，就像对待任何 Python 包一样。

## 扩展、MCP 服务器和 Skills

| 使用 | 适用场景 |
| --- | --- |
| Python 扩展 | 工具就是几个需要在你的电脑上运行的 Python 函数，例如读取项目 `pyproject.toml` 中的版本号，或用你自己的令牌搜索团队的问题跟踪系统。 |
| [MCP 服务器](./mcp.md) | 工具已经有现成的 MCP 服务器、用其他语言编写，或者也需要在其他应用中使用。 |
| [Skill](./skills.md) | 智能体需要的是在任务需要时使用的说明、参考文件或脚本，而不是一个带参数调用的工具。 |

## 快速上手

1. 将以下文件保存为 `~/.chrys/extensions/hello.py`（Windows 为 `%APPDATA%\chrys\extensions\hello.py`）：

   ```python
   """Greetings for trying out extensions."""

   from typing import Annotated

   from chrys.extensions import Extension

   extension = Extension()


   @extension.tool(requires_approval=False)
   async def hello_greet(name: Annotated[str, "Who to greet"]) -> str:
       """Greet someone by name."""
       return f"Hello, {name}!"
   ```

2. 启动 iCode，输入 `/agents tools`，在“扩展”下勾选 `hello`，然后点击“保存”。
3. 对智能体说：“用 hello_greet 向小明问好。”对话中会显示这次工具调用及其参数和结果。

扩展不一定是单个文件：较大的扩展可以是包含多个模块的文件夹。参阅[安装扩展](#安装扩展)。

## 安装扩展

扩展有两种安装方式：

- **作为文件或文件夹安装**：将扩展复制到 iCode 配置目录的 `extensions` 文件夹中（macOS 和 Linux 为 `~/.chrys/extensions/`，Windows 为 `%APPDATA%\chrys\extensions\`）。扩展可以是单个文件 `<扩展 ID>.py`，也可以是文件夹 `<扩展 ID>/`：其中的 `__init__.py` 定义扩展，旁边还可以放其他模块（参阅[将扩展拆分为多个文件](#将扩展拆分为多个文件)）：

  ```text
  extensions/
  ├── hello.py             # 扩展“hello”
  └── tracker/             # 扩展“tracker”
      ├── __init__.py
      └── client.py
  ```

  以 `_` 或 `.` 开头的名称会被忽略。以这种方式安装的扩展只能导入它自己的模块，以及 iCode 自身运行环境中已有的模块。

- **作为 Python 包安装**，适用于作者已发布为包的扩展。将它安装到 iCode 所在的运行环境中。如果你用 uv 安装了 iCode，用 `--with` 指定扩展的包名：

  ```bash
  uv tool install iCode-TUI --with icode-pyproject
  ```

  uv 只保留最近一次命令中指定的包，因此每次都要列出你使用的所有扩展。以包的方式安装时，扩展需要的库也会一并安装。iCode 离线安装包无法添加额外的包，请改用文件方式安装扩展。

iCode 只在这两处查找扩展，不会从项目文件夹中加载。

扩展的 ID 即其文件名或文件夹名，或其所在包为它指定的名称。ID 只能使用小写字母、数字和 `_`，必须以字母开头，并且不能与 iCode 某个内置工具类别的 ID（例如 `shell`、`todo` 或 `sleep`）相同。如果两处安装了 ID 相同的扩展，两份都不会加载：请删除其中一份。

## 为智能体启用扩展

1. 在输入栏中输入 `/agents tools`，打开当前智能体的“工具”标签页。如需修改其他智能体，可在左侧列表中选择。
2. 在“扩展”下勾选该智能体要使用的扩展。每个扩展都会显示其说明，下一行是它的安装位置。安装了两份的扩展会列出，但无法勾选。
3. 点击“保存”。

在智能体配置中，这一选择对应顶层的 `extensions` 列表，参阅 [extensions](../../reference/agent-profile.md#extensions)。扩展未安装时仍会保留在配置中，因此可以先启用再安装。每个智能体有各自的列表：子智能体或工作流中的智能体节点，只有在其自身的配置列出某个扩展时才会使用它。

iCode 会在首次启动使用某扩展的智能体时读取该扩展的代码；在“工具”标签页中列出扩展不会运行其中任何代码。修改或更新扩展后，请重启 iCode。

如果扩展无法加载，智能体会在不包含它的情况下启动，iCode 会显示一条警告说明原因。要查看实际加载了哪些内容，输入 `/runtime` 并打开“工具”标签页：每个扩展的工具、上下文和工具钩子列在该扩展的 ID 下，未能加载的扩展列在“不可用的扩展”下。参阅[排查问题](#排查问题)。

## 审批扩展中的工具

扩展中的工具在运行前会请求审批，即使智能体审批配置的 `default` 为 `auto` 也是如此，除非扩展作者将某个工具标记为可以直接运行（通常是只读取内容的工具）。智能体配置中的审批规则优先。扩展中每个工具的工具类别都是 `extension.<扩展 ID>`，因此可以这样写：

```yaml
approval:
  overrides:
    extension.tracker: auto
    extension.tracker.tracker_create_issue: require
```

按照这些规则，`tracker` 扩展中的工具都会直接运行，只有 `tracker_create_issue` 会先请求审批。

Hooks 也以同样方式匹配扩展中的工具，例如 `match.tool_kind: extension.tracker`。参阅[配置审批模式](../configuration/approval.md)和[工具类别和名称](../../reference/tool-kinds-and-names.md#扩展中的工具名称)。

> `icode run` 从不请求审批：所有工具都会直接运行，扩展中的工具也一样。参阅[使用 icode run 执行无界面任务](../running/icode-run.md)。

## 编写扩展

扩展是一个包含名为 `extension` 的 `Extension` 的模块。每个工具都是一个 `async def` 函数，通过 `@extension.tool` 加入扩展。下面这个扩展保存为 `pyproject.py`，为智能体提供一个从 `pyproject.toml` 读取项目版本号的工具：

```python
"""Read details from the project's pyproject.toml."""

import tomllib
from typing import Annotated

from chrys.extensions import Extension, ToolContext, ToolError

extension = Extension()


@extension.tool(requires_approval=False)
async def pyproject_version(
    ctx: ToolContext,
    path: Annotated[str, "Path to pyproject.toml, relative to the working directory"] = "pyproject.toml",
) -> str:
    """Read the project's version from pyproject.toml."""
    try:
        data = tomllib.loads((ctx.cwd / path).read_text(encoding="utf-8"))
        return data["project"]["version"]
    except (OSError, tomllib.TOMLDecodeError, KeyError) as exc:
        raise ToolError("pyproject.toml is missing or names no project version.", code="no_version") from exc
```

模块文档字符串的第一行是“工具”标签页中显示的说明。单独写 `@extension.tool` 即使用全部默认值，也可以传入下面的选项。对于每个工具：

- **名称**：函数名，或 `@extension.tool(name="...")`。模型通过这个名称调用工具。名称可以使用字母、数字、`_` 和 `-`，最多 64 个字符，并且不能与 iCode 的工具、子智能体、该智能体的 MCP 服务器提供的工具或该智能体使用的其他扩展中的工具同名。以扩展 ID 作为每个名称的开头，可以避免冲突。
- **说明**：函数的文档字符串，或 `@extension.tool(description="...")`。模型根据它决定何时使用该工具，因此请写清工具做什么、在什么情况下有用。
- **参数**：每个参数都需要类型。写成 `Annotated[类型, "说明"]` 可以为模型提供该值的说明。可用的类型有 `str`、`int`、`float`、`bool`、`None`、`Any`、`Literal[...]`、`list[...]`、`dict[str, ...]`、`str | None` 这样的联合类型，以及表示对象的 `TypedDict` 类；工具收到的是模型发送的内容，均为普通 Python 值。有默认值的参数是可选参数。参数必须有名称：不支持 `*args`、`**kwargs` 和仅限位置的参数。
- **上下文**：一个类型为 `ToolContext` 的参数（名称不限）告诉工具它在何处、为谁运行。模型看不到这个参数。
- **结果**：返回字符串；没有输出时返回 `None`；也可以返回 JSON 能表示的值，例如由字符串、数字、布尔值和 `None` 组成的 `dict` 或 `list`，模型会以 JSON 形式收到。其他值（例如日期）请先转换为字符串。超过 8,000 个 token 的结果会被截短，完整文本保存在会话目录中，并告知模型保存位置。用 `@extension.tool(max_result_tokens=...)` 可以设置其他上限，设为 `0` 则关闭这一上限；所有工具结果共用的上限 [`tools.result.ceiling_tokens`](../../reference/settings.md)（默认 64,000 个 token）仍然有效。
- **错误**：`raise ToolError("message", code="...")` 告诉模型出了什么问题；code 会在会话记录中标记这次失败。其他任何异常只会告诉模型工具执行失败，因此异常消息中的路径、地址和令牌不会进入对话。
- **审批**：除非写了 `@extension.tool(requires_approval=False)`，工具在运行前都会请求审批；写了之后，由智能体的审批设置决定，与 iCode 自身的只读工具相同。只对不做任何修改的工具使用它。

iCode 在导入扩展时会检查每个工具：函数不是 `async def`、参数没有类型或其类型是模型无法发送的、两个工具同名，都会导致扩展无法加载，警告中会说明问题。

### 不要让 iCode 卡住

- 工具运行在 iCode 自身的事件循环上：任何会阻塞的操作（例如较慢的文件操作、使用同步库发送的网络请求或耗时的计算）在运行期间都会让 iCode 卡住。请改用 `await asyncio.to_thread(...)` 运行这些操作。
- 模型可以一次调用多个工具，这些调用会同时运行。如果对你的工具的多个调用共用某些状态（例如同一个文件），请用 `asyncio.Lock` 保护。
- 用户中断智能体时，iCode 会取消仍在运行的调用，正在进行的 `await` 会抛出 `asyncio.CancelledError`。请让它继续向上抛出，并在 `finally` 块中做清理。

### 工具可以从上下文中读取的内容

| `ToolContext` 字段 | 值 |
| --- | --- |
| `cwd` | 当前[工作目录](../daily-use/workspaces.md)；以它为基准解析相对路径 |
| `workspace_dirs` | 工作目录，以及其后的各个附加工作目录 |
| `agent_name` | 调用该工具的智能体名称 |
| `session_id` | 会话 ID，或 `None` |
| `data_dir` | 存放扩展自身文件的文件夹，跨会话保留：iCode 配置目录中的 `extension-data/<扩展 ID>` |
| `session_data_dir` | 存放扩展在本会话中文件的文件夹，位于会话目录内；会话没有目录时为 `None`。复刻会话时会复制这个文件夹，因此请保存相对于它的路径 |

iCode 会在工具首次使用这两个文件夹时创建它们，且只有你可以读取。上下文只在传给它的那次调用中有效：不要留到之后的调用中使用。工具只能通过其结果改变 iCode 的状态；它修改的其他内容都是文件和服务，就像你运行的任何程序一样。

### 示例：搜索团队的问题跟踪系统

这个扩展 `tracker.py` 用于搜索团队的问题跟踪系统（issue tracker）。它从环境变量读取令牌，在不阻塞 iCode 的情况下调用该系统的 Web API，并把失败转换为模型能据此处理的消息。它返回一个对象列表，模型会以 JSON 形式收到。使用时请换成你所用系统的地址和字段名：

```python
"""Search issues in the team's issue tracker."""

import asyncio
import json
import os
import urllib.error
import urllib.parse
import urllib.request
from typing import Annotated, Literal

from chrys.extensions import Extension, ToolError

extension = Extension()

API = "https://tracker.example.com/api"


def _get(path: str) -> dict:
    token = os.environ.get("TRACKER_TOKEN")
    if not token:
        raise ToolError("TRACKER_TOKEN is not set; set it before starting iCode.", code="missing_token")
    request = urllib.request.Request(API + path, headers={"Authorization": f"Bearer {token}"})
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            return json.load(response)
    except urllib.error.HTTPError as exc:
        raise ToolError(f"The tracker answered HTTP {exc.code}.", code="http_error") from exc
    except OSError as exc:
        raise ToolError("The tracker cannot be reached.", code="unreachable") from exc


@extension.tool(requires_approval=False)
async def tracker_search(
    query: Annotated[str, "Words to look for in issue titles"],
    status: Annotated[Literal["open", "closed", "any"], "Which issues to include"] = "open",
) -> list[dict[str, str]]:
    """Search the team's issue tracker; returns each matching issue's id, title, and status."""
    data = await asyncio.to_thread(_get, "/issues?" + urllib.parse.urlencode({"q": query, "status": status}))
    return [{"id": i["id"], "title": i["title"], "status": i["status"]} for i in data["issues"]]
```

### 每轮为智能体提供上下文

用 `@extension.context` 添加一个 `async def` 函数，就能在每轮开始时告诉智能体一些信息，例如当前分支对应的工单，或团队正在值守的服务。它接收一个 `TurnContext`，返回文本；没有要说的内容时返回 `None`：

```python
"""Tell the agent which ticket the current branch is for."""

import asyncio
import re
import subprocess

from chrys.extensions import Extension, TurnContext

extension = Extension()


def _current_branch(cwd) -> str:
    result = subprocess.run(
        ["git", "branch", "--show-current"],
        cwd=cwd,
        capture_output=True,
        text=True,
        stdin=subprocess.DEVNULL,  # never read iCode's own input
    )
    return result.stdout.strip()


@extension.context
async def ticket(ctx: TurnContext) -> str | None:
    """Name the ticket in a branch such as ABC-123-fix-login."""
    branch = await asyncio.to_thread(_current_branch, ctx.cwd)
    match = re.match(r"[A-Z]+-\d+", branch)
    if match is None:
        return None
    return f"The current branch {branch} is for ticket {match[0]}. Mention the ticket in commit messages."
```

- **何时询问**：每轮开始时，即你发送消息、子智能体收到任务，或工作流步骤运行智能体时。智能体的所有上下文会同时被询问，这一轮会等待它们完成，因此请让它们尽快返回，并用 `await asyncio.to_thread(...)` 运行会阻塞的操作。重试某一轮，或工作流步骤重试其智能体时，沿用上下文第一次给出的文本；工作流步骤每次重新运行都会重新询问。工作流步骤的 `timeout` 从上下文全部返回后才开始计时。
- **能读取什么**：`ctx.prompt` 是这一轮要做的事：你的消息、子智能体的任务或步骤的提示词。其他字段与 [`ToolContext`](#工具可以从上下文中读取的内容) 相同。
- **智能体看到什么**：随这一轮的消息一起看到这段文本。只有当文本发生变化，或对话经过压缩、智能体已看不到它时，iCode 才会再次发送。返回 `None` 或空白文本会告诉智能体之前的文本不再适用，关闭该扩展也是如此。超过 4,000 个 token 的文本会被截短。
- **名称**：函数名，或 `@extension.context(name="...")`。名称可以使用字母、数字、`_` 和 `-`，最多 64 个字符，且在扩展内唯一。已保存的会话会记录这个名称，因此重命名上下文相当于新建一个上下文。
- **失败**：抛出异常、运行时间超过超时时间（默认 10 秒，可用 `@extension.context(timeout=...)` 设置）或返回的不是文本的上下文，在这一轮不提供任何内容，效果与返回 `None` 相同。它第一次失败时，iCode 会显示一条警告，说明是哪个上下文以及出了什么错。

### 检查或修改工具调用

工具钩子能看到智能体发起的每个工具调用，iCode 自身的工具、MCP 工具、子智能体和扩展工具都包括在内。子智能体自己发起的工具调用，只会经过它自己的配置启用的扩展中的钩子。用 `@extension.before_tool` 添加一个 `async def` 函数，可以在调用运行前检查它；用 `@extension.after_tool` 添加，可以在调用结束后修改模型读到的结果。这个扩展保存为 `icode_guard.py`：

```python
"""Keep .env files unchanged and tokens out of tool results."""

import re
from pathlib import PurePath

from chrys.extensions import Extension, ToolCall, ToolError, ToolResult

extension = Extension()

TOKEN = re.compile(r"ghp_[A-Za-z0-9]{36}")


@extension.before_tool
async def protect_env(call: ToolCall) -> None:
    if call.kind == "filesystem.write" and PurePath(call.arguments.get("path", "")).name == ".env":
        raise ToolError("Changing .env files is not allowed; ask the user to change them.")


@extension.after_tool
async def hide_tokens(call: ToolCall, result: ToolResult) -> str | None:
    if TOKEN.search(result.text):
        return TOKEN.sub("[token removed]", result.text)
    return None
```

- **调用前**：钩子接收一个 `ToolCall`：工具的 `name`、它的 [`kind`](../../reference/tool-kinds-and-names.md)、以 JSON 值表示的调用参数 `arguments`、调用的 `call_id`，以及与工具的 `ToolContext` 一样带有你的扩展文件夹的 `context`。抛出 `ToolError` 即拒绝该调用：模型读到 `Error: <消息>`。返回一个 dict，调用就会改用这些参数运行；请返回全部参数，而不只是你修改的那些。返回 `None` 则按原样运行。你配置的[钩子](./hooks.md)和审批看到的是你的钩子留下的参数。如果你在审批时修改了调用的参数，调用前钩子会针对你修改后的参数再运行一次。
- **调用后**：钩子接收该 `ToolCall` 和一个 `ToolResult`：`text` 是模型读到的内容；`failed` 表示调用是否未成功；`rejected` 表示调用是否被你、某个钩子或某个扩展拒绝；`duration_ms` 是耗时。返回文本即替换模型读到的内容，结果中的图片和其他附件会保留；返回 `None` 则保持不变。如果工具抛出了异常，无论钩子返回什么，模型读到的都是该错误。
- **顺序**：钩子依次运行，先按智能体启用扩展的顺序，再按各扩展中声明的顺序，每个钩子看到的都是前一个钩子留下的内容。它们在你于 `hooks.yaml` 中配置的钩子之前运行。
- **失败**：调用前钩子如果抛出 `ToolError` 以外的异常、运行时间超过超时时间（默认 10 秒，可用 `@extension.before_tool(timeout=...)` 设置），或返回的不是 dict，就会拒绝该调用。调用后钩子如果抛出异常、运行超时或返回的不是文本，结果会被替换为一条说明 iCode 已隐去该结果的提示。这两种消息都会说明是哪个扩展的哪个钩子；错误本身不会展示给模型，在 TUI 中 iCode 会把它记录在配置目录的 `logs/debug.log` 中。
- **钩子看不到的调用**：由模型服务商自行运行的工具（例如服务商自带的网页搜索）、参数与工具不符的调用，以及[外部 ACP 智能体](./external-acp-agents.md)的工具调用。如果你在调用后钩子运行期间中断了调用，该调用保留工具返回的结果。

同一个模型回复中的多个调用会同时运行，因此针对不同调用的钩子也可能同时运行。

### 将扩展拆分为多个文件

以文件夹 `<扩展 ID>/` 的方式安装扩展。其中的 `__init__.py` 定义 `extension` 和工具，其文档字符串的第一行是“工具”标签页中显示的说明。用相对导入引用文件夹中的其他模块：

```python
# tracker/client.py
def search_issues(query: str) -> list[dict[str, str]]:
    ...  # the request to the tracker, as in the example above
```

```python
# tracker/__init__.py
"""Search issues in the team's issue tracker."""

import asyncio
from typing import Annotated

from chrys.extensions import Extension

from .client import search_issues

extension = Extension()


@extension.tool(requires_approval=False)
async def tracker_search(query: Annotated[str, "Words to look for in issue titles"]) -> list[dict[str, str]]:
    """Search the team's issue tracker; returns each matching issue's id, title, and status."""
    return await asyncio.to_thread(search_issues, query)
```

该文件夹不在 Python 的导入路径中，因此 `import client` 和 `from client import ...` 都找不到这个模块：请写成 `from .client import ...`。

### 测试扩展

工具仍是普通的异步函数，因此测试可以用测试上下文直接调用它们。`chrys.extensions` 随 iCode 提供；要运行测试，请在开发环境中安装 `iCode-TUI`：

```python
import asyncio

from chrys.extensions import ToolContext

from icode_pyproject import pyproject_version


def test_reads_the_version(tmp_path):
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "demo"\nversion = "1.2.0"\n', encoding="utf-8")
    ctx = ToolContext.for_testing(tmp_path)
    assert asyncio.run(pyproject_version(ctx)) == "1.2.0"
```

`ToolContext.for_testing(cwd, data_dir=..., session_data_dir=..., agent_name=..., session_id=...)` 只为工具提供你传入的文件夹：未传入 `data_dir` 时访问 `ctx.data_dir` 会报错；未传入 `session_data_dir` 时 `ctx.session_data_dir` 为 `None`。

上下文和工具钩子同样是普通的异步函数。用 `TurnContext.for_testing(tmp_path, prompt="...")` 调用上下文；用你构造的 `ToolCall` 和 `ToolResult` 调用钩子：

```python
import asyncio

import pytest
from chrys.extensions import ToolCall, ToolContext, ToolError

from icode_guard import protect_env


def test_refuses_to_change_env_files(tmp_path):
    call = ToolCall(
        name="write_file",
        kind="filesystem.write",
        call_id="call-1",
        arguments={"path": ".env", "content": "KEY=1"},
        context=ToolContext.for_testing(tmp_path),
    )
    with pytest.raises(ToolError):
        asyncio.run(protect_env(call))
```

### 将扩展发布为包

在包的 `pyproject.toml` 中，于 `chrys.extensions` 入口点组下声明扩展。入口的名称即扩展 ID，其值指明模块及其中的 `extension`。这个包把扩展的代码放在 `pyproject.toml` 旁边的 `icode_pyproject/__init__.py` 中：

```toml
[project]
name = "icode-pyproject"
version = "0.1.0"
description = "Read details from the project's pyproject.toml"
requires-python = ">=3.14"
dependencies = []

[project.entry-points."chrys.extensions"]
pyproject = "icode_pyproject:extension"

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[dependency-groups]
dev = ["iCode-TUI", "pytest"]
```

“工具”标签页中显示的是包的 `description`。iCode 运行在 Python 3.14 或更高版本上。在 `dependencies` 中列出扩展需要的库，但不要列出 `iCode-TUI`：扩展运行在已经安装好的 iCode 中，因此只把 `iCode-TUI` 作为开发依赖。`uv build` 会把包构建到 `dist/` 中，供发布使用；在你自己的代码目录中，`uv sync` 会安装这个包及其 `dev` 依赖组，测试因此可以导入它。包安装到 iCode 的运行环境后，该扩展就会在“工具”标签页中列出。

## 排查问题

| 现象 | 检查什么 |
| --- | --- |
| “工具”标签页中没有该扩展 | 文件位于配置目录的 `extensions` 文件夹中（而不是项目文件夹中），名称以 `.py` 结尾或是包含 `__init__.py` 的文件夹，且名称不以 `_` 或 `.` 开头。以包的方式安装时，包必须装在 iCode 所在的运行环境中。 |
| “扩展”下显示“已跳过 …” | ID 不符合上面的 ID 规则，或与某个内置工具类别重名。请重命名文件或文件夹。 |
| 警告说智能体启动时未包含该扩展 | 警告会说明原因：扩展未安装、安装了两份、导入失败（消息中会给出错误），或其中某个工具的名称已被其他工具使用。 |
| 调用结果为“Error: Function failed.” | 工具抛出了 `ToolError` 以外的异常。在 TUI 中，iCode 会把它记录在配置目录的 `logs/debug.log` 中。对于需要让模型知道的失败，请抛出 `ToolError`。 |
| 警告说智能体缺少某个上下文 | 该上下文抛出了异常、运行超时，或返回的不是文本。警告会说明原因；这一轮智能体没有该上下文。 |
| 调用结果为“Extension … before_tool hook … failed …, so the call did not run.” | 该钩子抛出了 `ToolError` 以外的异常、运行超时，或返回的不是参数 dict。消息会说明原因；在 TUI 中，配置目录的 `logs/debug.log` 中有具体错误。 |
| 结果显示为“[Extension … after_tool hook … failed …, so iCode withheld this result.]” | 该钩子抛出了异常、运行超时，或返回的不是文本。在 TUI 中，配置目录的 `logs/debug.log` 中有具体错误。 |
| 修改扩展后没有效果 | 重启 iCode：iCode 运行期间，每个扩展只导入一次。 |
