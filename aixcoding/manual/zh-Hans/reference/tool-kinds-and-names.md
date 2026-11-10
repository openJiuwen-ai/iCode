# 工具类别和名称

iCode 使用工具类别（`tool_kind`）对用途或来源相近的工具进行分组，使用工具名称（`tool_name`）标识具体工具。同一类别可以包含多个工具，例如 `filesystem.write` 类别包含 `write_file` 和 `edit_file`。

在终端用户界面（Terminal User Interface，TUI）的输入栏中输入 `/runtime`，或点击输入栏上方状态栏右端显示的工具数量，打开“运行时详情”窗口。“工具”标签页列出按类别分组的内置工具、按扩展 ID 分组的扩展工具，以及上下文管理工具和子智能体工具，“MCP”标签页列出按服务器分组的 MCP 工具名称。Skill 工具不会在该窗口中列出。智能体只能调用当前已加载的工具。

## 类别和名称概览

“内置”表示该类别可以通过智能体配置的 `tools.builtins` 字段启用。

| 工具类别 | 内置 | 用途或来源 | 工具名称 |
| --- | --- | --- | --- |
| `shell` | 是 | 执行 Shell 命令 | 当前 Shell 的名称；Windows 上检测到 Git Bash 时还包括 `git_bash`，ACP 会话中还包括 `list_workspace_dirs` |
| `filesystem.read` | 是 | 读取文件和查看图像 | `read_file`、`view_image` |
| `filesystem.write` | 是 | 创建、覆写和编辑文件 | `write_file`、`edit_file` |
| `search` | 是 | 搜索文件内容和文件名 | `grep`、`glob` |
| `web_search` | 是 | 在网络上搜索链接和摘要 | `web_search` |
| `web_fetch` | 是 | 按 URL 读取网页内容 | `web_fetch` |
| `ask_user` | 是 | 在任务中询问用户 | `ask_user` |
| `sleep` | 否，每个智能体都有 | 等待 | `sleep` |
| `doc_converter` | 是 | 转换 PDF 和 Office 文档 | `convert_document` |
| `todo` | 是 | 维护待办事项 | `todo_write` |
| `sub_agent` | 否 | 调用子智能体 | 由智能体配置决定 |
| `mcp` | 否 | MCP 服务器工具和按需加载控制工具 | 由 MCP 服务器及其配置决定 |
| `extension.<扩展 ID>` | 否，通过 `extensions` 启用 | [Python 扩展](../guides/extensions/python-extensions.md)中的工具 | 由扩展决定 |
| `skill` | 否 | 加载 Skill、读取资源和运行脚本 | `load_skill`、`read_skill_resource`、`run_skill_script` |
| `context` | 否 | 压缩和查询对话上下文 | `compress_context`、`recall_context`、`list_compressed_contexts` |

具体工具是否可用还取决于当前智能体、模型能力和运行方式。例如，`view_image` 只会提供给支持图片输入的模型，无界面命令行模式 `icode run` 不提供 `ask_user`。

无论 `tools.builtins` 列出哪些类别，每个智能体都有 `sleep`。仍列出 `sleep` 的配置照常加载，下次保存该配置时 iCode 会将其移除。

iCode 自带的智能体都没有启用 `web_search` 和 `web_fetch`。智能体包含这两个工具时，如果某个工具的模式为 `off`，或模型提供方运行同名的网络工具，对应的本地工具不会提供。参阅[配置网络工具](../guides/configuration/web-tools.md)。

## Shell 工具名称

`shell` 类别中的主要工具使用当前 Shell 的名称，例如 `zsh`、`bash`、`pwsh`、`powershell` 或 `cmd`。在 Windows 上检测到 Git Bash 时，iCode 还会在主要 Shell 工具之外提供 `git_bash` 工具。实际名称取决于操作系统和 iCode 检测到的 Shell。

ACP 会话还会提供 `list_workspace_dirs`，用于列出主工作目录和 ACP 客户端提供的其他工作目录。

## 子智能体工具名称

`sub_agent` 类别中，每个工具对应一个可调用的子智能体。工具名称由智能体配置中的 [`sub_agents.agents[].tool_name`](./agent-profile.md#sub_agents) 决定；省略该字段时，默认使用子智能体的配置名称。

显式设置的 `tool_name` 必须以字母或下划线开头，之后只能包含字母、数字或下划线。省略 `tool_name` 时直接使用配置名称，因此工具名称可能包含连字符或以数字开头。同一父智能体内的工具名称不能重复。

## 扩展中的工具名称

每个 [Python 扩展](../guides/extensions/python-extensions.md)都有自己的类别 `extension.<扩展 ID>`，例如 `extension.tracker`，其中的工具名称由作者决定。扩展不能使用 iCode 工具、子智能体、该智能体的 MCP 服务器提供的工具或同一智能体使用的其他扩展中的工具名称：只要有一个名称已被占用，整个扩展都不会加载。扩展中与其他系统上的 iCode 工具同名的工具（例如 `bash`）会显示为普通工具调用。

在 `approval.overrides` 和 Hooks 的 `match.tool_kind` 中，无论扩展是否已安装，`extension.<扩展 ID>` 都是有效的类别。

## MCP 工具名称

`mcp` 类别包含 MCP 服务器提供的工具。iCode 会先将工具名称中除英文字母、数字、下划线和连字符以外的字符替换为连字符。未设置 `tool_name_prefix` 时，直接使用处理后的名称。

设置 `tool_name_prefix` 后，iCode 还会移除处理后名称开头的下划线和连字符，再按 `<前缀>_<工具名称>` 拼接；名称被移除为空时，只使用前缀。例如，服务器提供 `search` 或 `_search`，前缀为 `github` 时，最终名称均为 `github_search`。

配置 `approval.overrides` 或 Hooks 的 `match.tool_name` 时，应**以“运行时详情”窗口的“MCP”标签页中显示的实际工具名称为准**。

启用按需加载时，iCode 还会提供三个控制工具：

- `<前缀>_list_mcp_tools`
- `<前缀>_load_tool`
- `<前缀>_unload_tool`

未配置 `tool_name_prefix` 时，控制工具以 `mcp_` 加 MCP 服务器名称作为前缀，其中英文字母和数字以外的每个字符都会编码为 `-<十六进制码>-`，例如服务器 `my-server` 对应 `mcp_my-2d-server_list_mcp_tools`；服务器名称过长时会被截短并以哈希值结尾。控制工具不会显示在“运行时详情”窗口的“MCP”标签页中。
