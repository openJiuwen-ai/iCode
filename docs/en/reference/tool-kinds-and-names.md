# Tool kinds and names

iCode uses tool kinds (`tool_kind`) to group tools with similar purposes or sources, and tool names (`tool_name`) to identify individual tools. A kind can contain multiple tools. For example, `filesystem.write` includes `write_file` and `edit_file`.

In the terminal user interface (TUI), enter `/runtime` in the input field, or click the tool count at the right end of the status bar above the input field, to open the "Runtime Details" dialog. The "Tools" tab lists built-in tools grouped by kind, then each extension's tools under its id, then context management tools and sub-agent tools; the "MCP" tab lists MCP tool names grouped by server. Skill tools are not listed in the dialog. An agent can only call tools that are currently loaded.

## Overview of kinds and names

"Built-in" means that the kind can be enabled through the `tools.builtins` field in an agent profile.

| Tool kind | Built-in | Purpose or source | Tool names |
| --- | --- | --- | --- |
| `shell` | Yes | Run shell commands | The name of the current shell; also includes `git_bash` on Windows when Git Bash is detected, and `list_workspace_dirs` in ACP sessions |
| `filesystem.read` | Yes | Read files and view images | `read_file`, `view_image` |
| `filesystem.write` | Yes | Create, overwrite, and edit files | `write_file`, `edit_file` |
| `search` | Yes | Search file contents and filenames | `grep`, `glob` |
| `web_search` | Yes | Search the web for URLs and summaries | `web_search` |
| `web_fetch` | Yes | Read a web page by URL | `web_fetch` |
| `ask_user` | Yes | Ask the user questions during a task | `ask_user` |
| `sleep` | No; every agent has it | Wait | `sleep` |
| `doc_converter` | Yes | Convert PDF and Office documents | `convert_document` |
| `todo` | Yes | Maintain to-do items | `todo_write` |
| `sub_agent` | No | Call sub-agents | Determined by the agent profile |
| `mcp` | No | MCP server tools and on-demand loading controls | Determined by the MCP server and its configuration |
| `extension.<extension id>` | No; enabled with `extensions` | Tools from a [Python extension](../guides/extensions/python-extensions.md) | Determined by the extension |
| `skill` | No | Load skills, read resources, and run scripts | `load_skill`, `read_skill_resource`, `run_skill_script` |
| `context` | No | Compact and query conversation context | `compress_context`, `recall_context`, `list_compressed_contexts` |

Tool availability also depends on the current agent, model capabilities, and how iCode is run. For example, `view_image` is only provided to models that support image input, and the headless CLI mode `icode run` does not provide `ask_user`.

Every agent has `sleep`, whatever its `tools.builtins` lists. A profile that still lists `sleep` loads as before, and iCode leaves it out the next time the profile is saved.

`web_search` and `web_fetch` are not enabled in any agent shipped with iCode. When an agent includes them, either tool is left out while its mode is `off` or if the model's provider runs a web tool of the same name. See [Configure web tools](../guides/configuration/web-tools.md).

## Shell tool names

The main tool in the `shell` kind uses the name of the current shell, such as `zsh`, `bash`, `pwsh`, `powershell`, or `cmd`. On Windows, when Git Bash is detected, iCode also provides a `git_bash` tool alongside the main shell tool. The actual names depend on the operating system and the shells detected by iCode.

ACP sessions also provide `list_workspace_dirs`, which lists the primary working directory and any additional working directories provided by the ACP client.

## Sub-agent tool names

In the `sub_agent` kind, each tool corresponds to a callable sub-agent. Its tool name is determined by [`sub_agents.agents[].tool_name`](./agent-profile.md#sub_agents) in the agent profile. If this field is omitted, the sub-agent's profile name is used by default.

An explicitly set `tool_name` must start with a letter or underscore, followed only by letters, digits, or underscores. When `tool_name` is omitted, the profile name is used as is, so the tool name may contain hyphens or start with a digit. Tool names must be unique within the same parent agent.

## Extension tool names

Each [Python extension](../guides/extensions/python-extensions.md) has its own kind, `extension.<extension id>`, such as `extension.tracker`, and its author chooses its tool names. An extension cannot use the name of an iCode tool, a sub-agent, a tool from one of the agent's MCP servers, or a tool of another extension the same agent uses: if one of its names is taken, the whole extension is left out. An extension tool named like an iCode tool on another system, such as `bash`, is shown as a plain tool call.

In `approval.overrides` and a hook's `match.tool_kind`, `extension.<extension id>` is a valid kind whether or not the extension is installed.

## MCP tool names

The `mcp` kind includes tools provided by MCP servers. iCode first replaces any characters in a tool name other than English letters, digits, underscores, and hyphens with hyphens. If `tool_name_prefix` is not set, this processed name is used directly.

When `tool_name_prefix` is set, iCode also removes leading underscores and hyphens from the processed name, then combines it as `<prefix>_<tool_name>`. If removing these characters leaves an empty name, only the prefix is used. For example, if a server provides `search` or `_search` and the prefix is `github`, the final name is `github_search` in both cases.

When configuring `approval.overrides` or a hook's `match.tool_name`, **use the actual tool name shown in the "MCP" tab of the "Runtime Details" dialog**.

When on-demand loading is enabled, iCode also provides three control tools:

- `<prefix>_list_mcp_tools`
- `<prefix>_load_tool`
- `<prefix>_unload_tool`

If `tool_name_prefix` is not configured, the control tools use the prefix `mcp_` followed by the MCP server name. Every character in the server name other than an ASCII letter or digit is encoded as `-<hex code>-`; for example, server `my-server` gives `mcp_my-2d-server_list_mcp_tools`. Very long server names are shortened and end with a hash. Control tools are not listed in the "MCP" tab of the "Runtime Details" dialog.
