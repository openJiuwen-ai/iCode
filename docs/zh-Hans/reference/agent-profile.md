# 智能体配置文件参考

智能体配置文件（Agent Profile）是保存在本机的 YAML 文件，用于定义智能体的名称、指令、模型、工具、审批策略、Skills、记忆、上下文压缩和子智能体。本页列出 YAML 文件的加载与修改规则，以及可用字段、默认值和约束。

如需通过终端用户界面（Terminal User Interface，TUI）配置智能体，参阅[配置智能体](../guides/configuration/agents.md)。需要手工编辑 YAML 时，以本页为准。

## 用户智能体配置目录

用户智能体配置文件保存在以下目录：

| 平台 | 目录 |
| --- | --- |
| macOS、Linux | `~/.chrys/agents/` |
| Windows | `%APPDATA%\chrys\agents\` |

iCode 只加载目录中扩展名为 `.yaml` 或 `.yml` 的非隐藏文件，其他文件会被忽略。无效配置不会阻止其他配置加载，但会被跳过，并在启动日志中记录警告。

建议使用 UTF-8。iCode 也会自动检测 GBK/GB18030 等常见旧编码，以及带 BOM 的 UTF-16/UTF-32。无法按检测到的编码解码的文件会被视为无效文件，不会用 `�` 替换字节。iCode 保存或重写配置时使用 UTF-8。

iCode 在启动时加载这些文件。手工新增、修改或删除配置文件后，更改会在下次启动 iCode 时生效。

## 配置文件命名和规范化

每个配置必须包含 `name`。`name` 用作配置文件名，因此必须是合法的跨平台文件名，不能包含路径分隔符、冒号、控制字符或 `* ? " < > |` 中的任一字符，也不能使用 `.`、`..` 或 Windows 保留设备名。加载时会移除 `name` 两端的空白。

加载用户配置时，iCode 会根据配置内容规范化文件：

- 文件名不是 `<name>.yaml` 时，iCode 会将其重命名为 `<name>.yaml`。这也会将 `.yml` 扩展名改为 `.yaml`。
- 配置缺少 `id` 时，iCode 会分配稳定 ID，并重写整个文件。重写后不会保留原有注释、字段顺序和格式，还会移除无法识别的键和取默认值的字段（`approval` 除外，它始终会写入）。
- `sub_agents.agents` 引用了已从 iCode 中移除的内置智能体时，iCode 会删除这些条目，并以同样方式重写文件。如果你有同名的自有配置，则保留这些引用；目录中有配置文件加载失败时，此项修改会推迟。
- 目标文件名已被其他配置占用时，该配置不会加载。iCode 会尽可能将冲突文件重命名为带 `.conflict` 标记的文件。

上述规范化过程可能重命名或重写文件。需要保留原文件时，请在加载前备份。

发生冲突时，检查 `<name>.yaml` 和带 `.conflict` 标记的文件，并保留需要的配置。如需同时保留两者，将其中一个文件改为新的独立配置：修改 `name`，删除原有的 `id`，并将文件扩展名恢复为 `.yaml`。重启后，iCode 会为该配置分配新 ID。

受文件系统限制时，iCode 可能无法添加 `.conflict` 标记。此时冲突文件会保留原名，但仍不会加载。

## 覆盖内置智能体

内置智能体的原始配置随 iCode 安装。通过 TUI 修改并保存内置智能体后，iCode 会在[用户智能体配置目录](#用户智能体配置目录)中生成同名配置文件。例如，修改并保存名称为 `Code` 的内置智能体后会生成 `Code.yaml`。

内置智能体的 `name` 包括 `Code`、`QA`、`Explore` 和 `General`。在用户智能体配置目录下手工创建的配置文件使用上述任一 `name` 时，也会覆盖对应的内置智能体。覆盖配置会完整替代内置配置，不会继承或合并内置配置中的其他字段。删除对应的用户配置文件并重启 iCode，即可恢复使用内置配置。

## 基础配置示例

配置必须包含 `name` 字段，用于标识智能体配置。建议同时填写 `display_name`、`description` 和 `instructions`，以便识别配置和明确智能体行为。以下示例定义了一个用于代码审查的智能体：

```yaml
name: Reviewer
display_name: 代码审查
description: 检查代码质量和潜在缺陷
instructions: |
  阅读相关代码和测试。
  优先报告会影响用户的缺陷，并给出文件位置。
tools:
  builtins:
    - filesystem.read
    - search
```

示例省略了 `id`。iCode 首次加载配置时会分配稳定 ID 并回写文件。

将上述配置保存为[用户智能体配置目录](#用户智能体配置目录)中的 `Reviewer.yaml`。运行以下命令，确认配置能够加载：

```bash
icode agents
```

列表中出现“代码审查”，说明配置已加载。如果未出现，请检查启动时显示的 YAML 解析或字段校验警告，并确认文件位于用户智能体配置目录中。

## 顶层字段说明

| 字段 | 类型 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `name` | 字符串 | 必填 | 配置名称，也是规范文件名的主体。子智能体引用使用此值。 |
| `id` | 字符串 | 自动分配 | 稳定标识符。省略或留空时，iCode 会自动分配一个 12 位十六进制 ID：与内置智能体同名时沿用其 ID，否则生成新的 ID。也可以手动填写唯一 ID；纯数字 ID 需要加引号，例如 `id: "123"`，否则会被读取为数字，导致配置加载失败。复制已有配置文件并在其基础上修改以创建新的配置时，建议删除 `id` 字段，由 iCode 自动分配新的 ID。 |
| `display_name` | 字符串 | 空 | 显示名称。 |
| `description` | 字符串 | 空 | 说明智能体用途，也作为子智能体未填写工具描述时的默认描述。 |
| `sub_agent_only` | 布尔值 | `false` | 为 `true` 时不能选作主智能体，只能被其他智能体调用。外部 ACP 智能体会被强制设为 `true`。 |
| `instructions` | 字符串或字符串列表 | 空 | 提供给内置类型智能体的主要行为指令。写成列表时，各项按每项一行合成一段文本。外部 ACP 配置会忽略此字段。 |
| `model` | 对象 | `{}` | 绑定模型配置，见 [model](#model)。 |
| `tools` | 对象 | `{}` | 配置内置工具、MCP 和 Shell 过滤，见 [tools](#tools)。 |
| `approval` | 对象 | 见下文 | 配置哪些工具调用需要审批，见 [approval](#approval)。 |
| `skills` | 对象 | 见下文 | 配置 Skill 来源和内嵌 Skill，见 [skills](#skills)。 |
| `memory` | 对象 | `{}` | 配置自动加载的参考文件，见 [memory](#memory)。 |
| `compaction` | 对象 | 见下文 | 配置上下文压缩，见 [compaction](#compaction)。 |
| `sub_agents` | 对象 | 见下文 | 配置可调用的子智能体，见 [sub_agents](#sub_agents)。 |
| `acp` | 对象 | 未启用 | 字段一旦出现，该配置即为外部智能体客户端协议（Agent Client Protocol，ACP）智能体，见 [acp](#acp)。空对象 `acp: {}` 或空值（`acp:`、`acp: null`）也会启用此类型，但没有 `command` 时无法启动。 |

## model

`model` 用于将智能体绑定到指定的模型配置。

```yaml
model:
  profile_id: 0123456789ab
```

| 字段 | 类型 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `profile_id` | 字符串 | 空 | 要绑定的模型配置 ID。 |

省略 `profile_id`、将其留空，或者填写的 ID 在已加载的模型配置中找不到时，主智能体使用会话中当前生效的模型配置，子智能体继承父智能体实际使用的模型配置。ID 找不到时，iCode 还会记录警告。

运行 `icode models` 可以在 `ID` 列查看模型配置的稳定 ID。模型配置方法参阅[配置模型](../guides/configuration/models.md)。

## tools

```yaml
tools:
  builtins:
    - filesystem.read
    - search
    - shell
  mcp: []
```

| 字段 | 类型 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `builtins` | 字符串列表 | `[]` | 要启用的内置工具类别，可用值见[类别和名称概览](./tool-kinds-and-names.md#类别和名称概览)中“内置”为“是”的行。内置智能体都不包含 `web_search` 和 `web_fetch`。 |
| `mcp` | 对象列表 | `[]` | 模型上下文协议（Model Context Protocol，MCP）服务器配置，见 [tools.mcp](#toolsmcp)。 |
| `shell_filter` | 字符串或对象 | 未设置（不进行 Shell 过滤） | 限制 Shell 工具可以执行的命令，见 [tools.shell_filter](#toolsshell_filter)。 |
| `web_search` | 对象 | 未设置（使用用户设置） | `web_search` 工具的模式和搜索提供方，见 [tools.web_search](#toolsweb_search)。 |
| `web_fetch` | 对象 | 未设置（使用用户设置） | `web_fetch` 工具的模式和限制，见 [tools.web_fetch](#toolsweb_fetch)。 |

### tools.mcp

每个列表条目配置一个服务器。`name` 和 `transport` 必填；同一智能体配置内的 `name` 不能重复，且重复判断不区分大小写。HTTP 连接需要 `url`，STDIO 连接需要 `command`。

STDIO 配置示例：

```yaml
tools:
  mcp:
    - name: local-server
      transport: stdio
      command: python
      args: ["-m", "example_mcp_server"]
      env:
        API_KEY: "{{MCP_API_KEY}}"
        LOG_LEVEL: info
```

HTTP 配置示例：

```yaml
tools:
  mcp:
    - name: remote-server
      transport: http
      url: https://example.com/mcp
      headers:
        Authorization: "Bearer {{MCP_API_TOKEN}}"
```

`env` 和 `headers` 都是键值映射：左侧填写变量名或请求头名称，右侧填写对应的值。值可以直接填写，也可以用 `{{ENV_VAR}}` 引用启动 iCode 前已设置的环境变量。`env` 中引用的变量缺失或为空时，服务器连接失败；`headers` 仅在 `resolve_header_templates` 为 `true` 时解析变量并应用这一规则。

| 字段 | 类型 | 默认值 | 适用范围 | 说明 |
| --- | --- | --- | --- | --- |
| `name` | 字符串 | 必填 | 通用 | 此配置中的服务器名称。 |
| `transport` | `http` 或 `stdio` | 必填 | 通用 | 连接方式。 |
| `enabled` | 布尔值 | `true` | 通用 | 是否连接并提供服务器能力。 |
| `description` | 字符串 | 空 | 通用 | 供用户识别配置的备注，不作为服务器说明发送给智能体。 |
| `tool_name_prefix` | 字符串 | 空 | 通用 | 给 MCP 工具的名称添加 `<前缀>_`。前缀只能包含字母、数字、下划线和连字符，必须以字母或数字开头和结尾，不能有首尾空白。添加前缀后的完整工具名不能超过 64 个字符。 |
| `allowed_tools` | 字符串列表或 `null` | `null` | 通用 | `null` 表示允许全部服务器工具，`[]` 表示不允许任何工具，列表表示只允许指定工具。 |
| `use_progressive_disclosure` | 布尔值 | `false` | 通用 | 为 `true` 时按需加载允许的工具。`allowed_tools` 为 `[]` 时不能设为 `true`。 |
| `always_load` | 字符串列表 | `[]` | 通用 | 按需加载时最初可见的工具。`allowed_tools` 为列表时，这些名称也必须在该列表中；未启用按需加载时必须为空。 |
| `load_prompts` | 布尔值 | `true` | 通用 | 是否将服务器提示词模板作为工具加载。 |
| `expose_instructions` | 布尔值 | `true` | 通用 | 是否将服务器使用说明加入模型上下文。 |
| `timeout` | 正整数或 `null` | `null` | 通用 | 单次 MCP 请求（包括工具调用）的超时秒数。`null` 使用默认值 30 秒。必须大于 `0`。 |
| `max_tool_result_tokens` | 整数或 `null` | `null` | 通用 | 单次远程工具文本结果上限。`null` 使用默认上限 `8000` 个词元，`0` 仅取消此项上限，正整数至少为 `100`；其他值会导致整个智能体配置被跳过。 |
| `command` | 字符串 | 空 | STDIO | 启动服务器的可执行文件名称或路径。YAML 中不要在此字段拼接参数。 |
| `args` | 字符串列表 | `[]` | STDIO | 传给服务器进程的参数。 |
| `env` | 字符串对象 | `{}` | STDIO | 传给服务器进程的环境变量。HTTP 配置会忽略此字段。 |
| `encoding` | 字符串或 `null` | `null` | STDIO | 服务器标准输入输出编码；通常留空。 |
| `url` | 字符串 | 空 | HTTP | 以 `http://` 或 `https://` 开头的服务器 URL。 |
| `headers` | 字符串对象 | `{}` | HTTP | HTTP 请求头。 |
| `resolve_header_templates` | 布尔值 | `true` | HTTP | 是否解析请求头值中的 `{{ENV_VAR}}`。设为 `false` 时按字面发送。 |
| `verify_ssl` | 布尔值 | `true` | HTTP | 是否验证 HTTPS 证书。关闭会降低连接安全性。 |
| `bypass_proxy` | 布尔值 | `false` | HTTP | 是否绕过环境变量配置的 HTTP/HTTPS 代理，直接连接服务器。 |
| `terminate_on_close` | 布尔值或 `null` | `null` | HTTP | 关闭连接时是否请求终止远程会话；`null` 使用默认值 `true`。 |

`allowed_tools` 和 `always_load` 填写服务器提供的原始工具名称。`tool_name_prefix` 会改变智能体调用工具时使用的名称；`approval.overrides` 也使用这个名称。名称生成规则见 [MCP 工具名称](./tool-kinds-and-names.md#mcp-工具名称)。如果填写的某个名称可能指两个工具（例如前缀为 `gh` 时，服务器的 `search` 工具会变成 `gh_search`，恰好是另一个工具的原始名称），智能体会加载失败。错误信息会指出这个名称，并为它匹配到的每个工具给出一个只对应该工具的名称：本例中服务器的 `search` 写 `search`，服务器的 `gh_search` 写 `gh_gh_search`。把这个名称换成你想让智能体使用的那些工具对应的名称。如果错误信息说没有名称能单独对应某个工具，请换一个 `tool_name_prefix`。

启用按需加载时，iCode 还会添加用于查看、加载和卸载 MCP 工具的控制工具，名称规则见 [MCP 工具名称](./tool-kinds-and-names.md#mcp-工具名称)。此时，`tool_name_prefix` 不能超过 49 个字符。

配置方法、连接测试和调用验证参阅[连接 MCP 服务器](../guides/extensions/mcp.md)。

### tools.shell_filter

`shell_filter` 只对 `shell` 类别中的工具生效。省略此字段或设置为 `unrestricted` 时，iCode 不过滤 Shell 命令。

Shell 工具调用先经过[审批](#approval)，放行后再由 `shell_filter` 检查具体命令；两项检查都通过后，命令才会执行。若命令被过滤器阻止，iCode 不会启动 Shell 进程，而是返回“命令被阻止”的工具错误。

`shell_filter` 可以使用预设，也可以自定义允许或阻止的命令。使用预设时，可以直接填写预设名称，也可以在对象中通过 `preset` 选择；两种写法效果相同：

```yaml
tools:
  shell_filter: read_only
```

```yaml
tools:
  shell_filter:
    preset: read_only
```

可用的预设如下：

| 值 | 行为 |
| --- | --- |
| `read_only` | 只允许内置命令名单中的命令，并阻止未被引号包围的重定向和命令替换。例如：`ls`、`cat`、`rg`、`git`、`python`、`curl` 和 PowerShell 的 `Get-Content` 等。不检查命令参数、子命令或脚本内容，因此不保证命令只读，也不是安全沙箱。 |
| `unrestricted` | 不进行 Shell 过滤。通常直接省略 `shell_filter` 即可。 |

只有上表中的名称会启用预设。无法识别的预设不会生效：使用标量写法时不进行 Shell 过滤；使用对象写法时，iCode 会尝试使用同一对象中的自定义规则，若 `commands` 为空，同样不进行 Shell 过滤。

需要自行指定允许或阻止的命令时，使用对象形式。例如：

```yaml
tools:
  shell_filter:
    mode: whitelist
    commands: [git, rg]
    allow_redirections: false
    allow_subshells: false
```

对象形式支持以下字段：

| 字段 | 类型 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `preset` | `read_only`、`unrestricted` 或空 | 空 | 选择预设。设置为可用预设后，忽略其他自定义过滤字段。 |
| `mode` | `whitelist` 或 `blacklist` | `whitelist` | `whitelist` 只允许 `commands` 中的命令；`blacklist` 阻止 `commands` 中的命令，允许其他命令。 |
| `commands` | 字符串列表 | `[]` | 命令名名单。`whitelist` 模式下为允许名单，`blacklist` 模式下为阻止名单；必须至少包含一个命令，自定义过滤才会生效。为空时不进行 Shell 过滤。 |
| `allow_redirections` | 布尔值 | `true` | 是否允许未被引号包围的 `>`、`>>` 和 `<`。 |
| `allow_subshells` | 布尔值 | `true` | 是否允许未被引号包围的 `$()` 和反引号命令替换。 |

在自定义规则中，`commands` 填写可执行文件名，例如 `[git, rg]`。名称区分大小写；使用绝对路径时需要填写完整路径。对于由 `|`、`&&`、`||` 或 `;` 等连接的多个命令，iCode 会分别检查每一段，所有命令名都符合规则后才会执行。以上示例允许 `rg foo .`；`rg foo . | less` 会被阻止，因为管道右侧的 `less` 不在允许名单中。

### tools.web_search

`web_search` 字段配置 `web_search` 工具，仅在 `builtins` 包含 `web_search` 时生效。省略的字段使用[网络工具用户设置](./settings.md#网络工具)。配置步骤和各提供方的行为参阅[配置网络工具](../guides/configuration/web-tools.md)。

```yaml
tools:
  builtins: [filesystem.read, search, web_search]
  web_search:
    mode: auto
    fallback_chain: [primary, backup]
    num_results: 8
    timeout_seconds: 30
    providers:
      primary:
        type: tavily
        api_key_env: TAVILY_API_KEY
      backup:
        type: duckduckgo_html
```

| 字段 | 类型 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `mode` | `auto`、`provider`、`off` 或布尔值 | 用户设置 `tools.web_search.mode`（`auto`） | `auto`：`provider`、`fallback_chain` 和 `providers` 均省略时使用 Exa 公开端点；配置了 `providers` 时必须设置 `fallback_chain`。`provider`：只使用 `provider` 指定的一个提供方，不能设置 `fallback_chain`。`off`：关闭网络搜索。`true` 表示 `auto`，`false` 表示 `off`。其他值会导致整个智能体配置被跳过。 |
| `provider` | 字符串 | 未设置 | `provider` 模式使用的提供方在 `providers` 中的 ID，不能与 `fallback_chain` 同时使用。 |
| `fallback_chain` | 字符串列表 | 未设置 | `auto` 模式下按顺序尝试的提供方 ID（均需在 `providers` 中），最多 5 个，不能重复。 |
| `providers` | 对象 | 未设置 | 以自定义 ID 为键的提供方配置。ID 以小写字母开头，只能包含小写字母、数字、`_` 和 `-`，最长 64 个字符。 |
| `num_results` | 整数 | 用户设置 `tools.web_search.num_results`（`8`） | 智能体未指定数量时每次搜索返回的结果数，范围 `1`–`20`。 |
| `timeout_seconds` | 整数 | 用户设置 `tools.web_search.timeout_seconds`（`30`） | 整个搜索调用的截止时间（秒），范围 `1`–`120`。 |

数值超出范围或 `providers` 条目格式错误时，同样会导致该配置被跳过。与模式不符的组合（例如 `provider` 模式未设置 `provider`，或选用了 `providers` 中不存在的提供方）则会让智能体不包含网络工具，iCode 会显示一条写明原因的警告。

`providers` 中的每一项支持以下字段：

| 字段 | 适用于 | 说明 |
| --- | --- | --- |
| `type` | 全部 | 必填。无需密钥的服务为 `exa_mcp`、`bing_html` 或 `duckduckgo_html`；需要密钥的搜索 API 为 `tavily`、`brave` 或 `exa`；自己的端点为 `custom_http`。 |
| `api_key_env` | `tavily`、`brave`、`exa` | 必填。保存 API 密钥的环境变量名。这些类型不接受其他字段。 |
| `preset` | `custom_http` | `searxng` 或 `serpapi`，为对应服务自动填写请求和响应映射；`serpapi` 还会设置 `endpoint` 和 `auth`。自己填写的字段优先。 |
| `endpoint` | `custom_http` | 搜索端点 URL。还需在用户设置 `tools.web_search.custom_endpoints` 中授权该端点及其读取的每个环境变量。 |
| `method` | `custom_http` | `GET`（默认）或 `POST`。 |
| `auth` | `custom_http` | 对象，包含 `location`（`header`、`query` 或默认的 `none`）、`name`（请求头或查询参数名）、`key_env`（保存密钥的环境变量）和可选的 `prefix`（例如 `"Bearer "`）。`location` 为 `header` 时，`name` 必须是 `Authorization`、`X-API-Key` 或 `X-Subscription-Token`。 |
| `headers` | `custom_http` | 只能包含 `Accept` 和 `Content-Type`，且内容类型必须为 `application/json`。密钥应通过 `auth` 传递，不能写在请求头中。 |
| `request` | `custom_http` | `query_params`，以及 `POST` 时的 `json`。写成 `{$input: query}`、`{$input: limit}`、`{$input: allowed_domains}` 或 `{$input: blocked_domains}` 的值会替换为对应的搜索参数，`{$env: NAME}` 会替换为环境变量。 |
| `response` | `custom_http` | 指向响应内容的 JSON Pointer：`results_pointer` 和 `url_pointer`（未使用预设时必填），以及可选的 `title_pointer` 和 `snippet_pointer`。 |

`exa_mcp`、`bing_html` 和 `duckduckgo_html` 只接受 `type` 字段。

### tools.web_fetch

`web_fetch` 字段配置 `web_fetch` 工具，仅在 `builtins` 包含 `web_fetch` 时生效。省略的字段使用[网络工具用户设置](./settings.md#网络工具)。

```yaml
tools:
  builtins: [filesystem.read, search, web_search, web_fetch]
  web_fetch:
    mode: on
    max_tokens: 16000
    timeout_seconds: 60
```

| 字段 | 类型 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `mode` | `on`、`off` 或布尔值 | 用户设置 `tools.web_fetch.mode`（`on`） | `on` 时 `web_fetch` 可用；`off` 时不提供。`true` 表示 `on`，`false` 表示 `off`。 |
| `max_tokens` | 整数 | 用户设置 `tools.web_fetch.max_tokens`（`16000`） | 读取的页面最多返回的词元数，范围 `1`–`64000`。智能体可以要求更少，但不能更多。 |
| `timeout_seconds` | 整数 | 用户设置 `tools.web_fetch.timeout_seconds`（`60`） | 整个网页读取调用的截止时间（秒），范围 `1`–`120`。 |

网络访问（代理、来源列表和自定义端点授权）只能在用户设置中配置，不能写在智能体配置文件中。参阅设置文件参考中的[网络工具](./settings.md#网络工具)。

## approval

`approval` 用于设置工具调用的**初始审批级别**。它与会话的**审批模式**不同：审批级别决定工具调用是否需要进入审批流程，审批模式决定进入审批流程后的处理方式。

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `default` | `auto`、`require` 或 `skip` | 未匹配覆盖规则时使用的审批级别。 |
| `overrides` | 对象 | 按工具类别或工具名称设置审批级别。 |

未配置 `approval` 时，iCode 使用以下默认配置：

```yaml
approval:
  default: auto
  overrides:
    shell: require
    filesystem.write: require
    todo: skip
```

### 审批级别

审批级别可使用以下值：

| 值 | 行为 |
| --- | --- |
| `require`     | 工具调用初始需要审批。 |
| `auto`、`skip` | 工具调用初始无需审批。当前版本中两者行为相同。 |

一次工具调用的处理流程如下：

1. iCode 根据 `default` 和 `overrides` 确定初始审批级别。
2. iCode 根据安全规则调整结果：

   * 访问敏感目标的 Shell 命令和文件读写会调整为需要审批；
   * 已知安全的只读 Shell 命令、工作目录 Git 仓库内的非敏感文件写入等操作可以直接执行。
3. 如果调用仍需审批，则由当前会话的审批模式决定处理方式，包括用户确认、审批裁判模型判断或绕过审批。具体行为参阅[配置审批模式](../guides/configuration/approval.md)。

### 覆盖规则

工具类别和名称的区别、可用值及动态名称规则见[工具类别和名称](./tool-kinds-and-names.md)。

`overrides` 的键可以是：

* 工具类别，例如 `shell`；
* 工具名称，例如 `write_file`；
* 工具类别和工具名称组合，例如 `filesystem.write.write_file`。

与工具类别相同的键始终表示该类别。如需指定名称与某个类别相同的工具（例如名为 `search` 的 MCP 工具），请使用工具类别和工具名称组合，例如 `mcp.search`。`web_search` 和 `web_fetch` 例外：这两个键同时匹配同名的任何工具（例如名为 `web_search` 的 MCP 工具），因此为此类工具编写的覆盖规则仍然有效。

同一工具调用匹配多条规则时，按以下优先级选择：

1. 工具类别和工具名称；
2. 工具名称；
3. 工具类别；
4. `default`。

规则在 YAML 中的顺序不影响匹配结果。例如：

```yaml
approval:
  default: auto
  overrides:
    filesystem.write: require
    filesystem.write.write_file: auto
```

在此示例中：

* `write_file` 匹配 `filesystem.write.write_file`，审批级别为 `auto`；
* 其他文件写入工具匹配 `filesystem.write`，审批级别为 `require`；
* 其他工具使用 `default` 的 `auto`。

省略 `overrides` 时，iCode 使用默认覆盖项 `shell: require`、`filesystem.write: require` 和 `todo: skip`。显式设置 `overrides: {}` 会清空这些默认覆盖项，未匹配其他规则的工具均使用 `default`。

Skill 脚本是例外：`run_skill_script` 默认需要审批。如需修改该行为，需要在 `overrides` 中显式配置 `skill`、`run_skill_script` 或 `skill.run_skill_script` 的审批级别。

网络工具也是例外：`web_search` 和 `web_fetch` 类别的工具默认需要审批，即使 `default` 为 `auto`。该规则在 `overrides` 中的规则之后、`default` 之前生效。如需修改，在 `overrides` 中显式配置该类别或工具名称的审批级别，例如 `web_search: auto`。

## skills

```yaml
skills:
  paths:
    - ./team-skills
  script_timeout: 300
  script_extensions: [.py, .sh, .ps1]
  auto_load_user_agents_skills: true
  auto_load_cwd_agents_skills: true
  inline:
    - name: release-notes
      description: 根据提交记录起草面向用户的发布说明
      instructions: |
        读取变更并按新增、修复和兼容性变化分类。
      resources:
        - name: style-guide
          description: 发布说明的写作规则
          content: |
            使用简短标题，只描述用户可观察的变化。
```

| 字段 | 类型 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `paths` | 字符串列表 | `[]` | 额外的 Skill 搜索根目录。iCode 在每个目录及其最多两层子目录中查找 `SKILL.md`；找到后不再搜索该 Skill 目录的内部。相对路径按当前[工作目录](../guides/daily-use/workspaces.md)解析；路径开头的 `~` 由 iCode 展开为当前用户主目录。 |
| `inline` | 对象列表 | `[]` | 直接写在配置中的 Skill，见下文。 |
| `script_timeout` | 正整数 | `300` | Skill 脚本最长运行秒数。 |
| `script_extensions` | 字符串列表 | `[.py, .sh, .ps1]` | 允许作为 Skill 脚本运行的扩展名；所需解释器必须已经安装。 |
| `auto_load_user_agents_skills` | 布尔值 | `true` | 是否加载 Agent Skills 用户级共享目录。 |
| `auto_load_cwd_agents_skills` | 布尔值 | `true` | 是否加载当前工作目录的 `.agents/skills`；还需在设置中开启“加载项目 Skills”（`project.skills_enabled`）。切换工作目录后重新加载。 |

iCode 用户 Skill 目录始终加载；Agent Skills 用户级共享目录默认加载；当前工作目录 Skill 目录在设置中开启“加载项目 Skills”后加载。两者可分别通过两个 `auto_load_*` 字段关闭。用户级 Skill 目录路径见[Skill 安装位置](../guides/extensions/skills.md#skill-安装位置)。

多个来源出现同名 Skill 时，优先级从高到低为：`paths` 中靠前的目录、iCode 用户 Skill 目录、Agent Skills 用户级共享目录、当前工作目录 Skill 目录、`inline` 中靠前的定义。同一个搜索根目录中存在多个同名 Skill 时，发现顺序不确定；为确保加载指定版本，请只保留其中一个。

> **注意**：Skill 脚本作为本地进程运行，不受安全沙箱隔离。将目录加入 `paths` 或启用自动加载来源前，请检查其中的 `SKILL.md`、脚本和其他相关文件，只加载可信 Skill。允许某种脚本扩展名不会自动安装对应解释器。

### skills.inline

| 字段 | 类型 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `name` | 字符串 | 必填 | 1～64 个字符，可使用小写字母、数字和连字符；连字符不能连续出现，也不能位于开头或结尾。 |
| `description` | 字符串 | 必填 | 非空，最长 1024 个字符。用于帮助智能体判断何时加载 Skill。 |
| `instructions` | 字符串或字符串列表 | 空 | Skill 加载后提供给智能体的操作说明。写成列表时，各项按每项一行合成一段文本。 |
| `resources` | 对象列表 | `[]` | 随 Skill 加载的内嵌文本资源，见下表。 |

| `resources` 子字段 | 类型 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `name` | 字符串 | 必填 | 资源标识，不是文件路径；同一 Skill 内不要使用仅大小写不同的重复名称。 |
| `description` | 字符串 | 空 | 资源用途说明。 |
| `content` | 字符串 | 空 | 文本资源内容。内容为空的资源会被忽略。 |

内嵌 Skill 不支持文件资源或脚本。需要引用文件或运行脚本时，应创建包含 `SKILL.md` 的目录 Skill，并通过 `skills.paths` 或上述自动加载的 Skill 目录加载。

Skill 的安装步骤、目录规范、加载验证和使用方式详见[安装和使用 Skills](../guides/extensions/skills.md)。

## memory

```yaml
memory:
  files:
    - AGENTS.md
    - docs/project-context.md
  folders:
    - docs/reference
```

| 字段 | 类型 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `files` | 字符串列表 | `[]` | 单个 `.md` 或 `.txt` 文件。 |
| `folders` | 字符串列表 | `[]` | 扫描其中 `.md` 和 `.txt` 文件的目录。 |

路径可以是绝对路径，也可以相对于当前工作目录；相对路径不能指向工作目录以外的位置。主智能体和工作流中的智能体节点会加载记忆；子智能体不会加载自身或父智能体的记忆。目录扫描深度、文件数和总词元上限参阅[配置记忆](../guides/configuration/memory.md)。

## compaction

`compaction` 用于配置 iCode 压缩当前任务时生成的续接信息（Last Words）；这些设置不影响较早工具结果或已完成对话轮次的压缩摘要。

```yaml
compaction:
  last_words_template: |
    优先保留复现步骤、关键日志和仍待验证的假设。
  last_words_max_output_tokens: 20000
  phase4_side_call_token_budget: -1
```

| 字段 | 类型 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `last_words_template` | 字符串 | 空 | 附加到 iCode 固定 Last Words 要求后的补充说明，用于强调当前任务中需要保留的信息，不会替换固定格式。 |
| `last_words_max_output_tokens` | 整数 | `20000` | Last Words 的最大输出词元数；运行时不会超过模型配置的输出上限。 |
| `phase4_side_call_token_budget` | 整数或 `null` | `-1` | 从用户提交消息到智能体完成本次回复期间，生成 Last Words 的额外模型请求可使用的估算输入词元总预算。`-1` 或 `null` 表示不限预算，`0` 表示不发起 Last Words 请求，正整数设置累计上限；不能小于 `-1`。 |

`last_words_max_output_tokens` 限制输出，`phase4_side_call_token_budget` 限制累计输入。后者主要用于严格控制额外模型用量，通常保留默认值。预算不足时，iCode 会保留当前任务内容；如果上下文仍然过大，本次任务可能无法继续。

`compaction` 不提供压缩触发阈值设置；iCode 会根据当前模型的上下文窗口、最大输出词元数和安全余量自动确定何时压缩。

压缩流程和可见行为参阅[配置上下文压缩](../guides/configuration/compaction.md)。

## sub_agents

```yaml
sub_agents:
  max_total_concurrency: 3
  agents:
    - profile: Explore
      tool_name: explore
      tool_description: 搜索并分析相关代码
      max_concurrency: 3
```

| 字段 | 类型 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `max_total_concurrency` | 正整数 | `3` | 所有子智能体调用合计的最大并发数。 |
| `agents` | 对象列表 | `[]` | 可供当前智能体调用的配置。 |
| `agents[].profile` | 字符串 | 必填 | 已加载的智能体 `name`。不能引用自身，同一配置不能重复引用。缺少 `profile` 的条目会被忽略。 |
| `agents[].tool_name` | 字符串 | 子智能体 `name` | 暴露给父智能体的工具名称。显式填写的值必须以字母或下划线开头，之后只能包含字母、数字或下划线。省略时直接使用子智能体的 `name`。最终生效的名称在同一父智能体内不能重名。 |
| `agents[].tool_description` | 字符串 | 子智能体 `description` | 帮助父智能体判断何时调用。 |
| `agents[].max_concurrency` | 正整数 | `3` | 此子智能体的最大并发调用数。 |

TUI 的“智能体配置”窗口保存时会检查并发数和 `tool_name` 格式；加载手工编辑的 YAML 文件时不做这些检查。并发数为 `0` 或更小时，相应子智能体的每次调用都会失败。

非 ACP 子智能体内部的工具调用使用父智能体的 `approval` 配置；子智能体自身的 `approval` 配置不影响这些调用。

非 ACP 子智能体向父智能体返回最终回复，不包含其在工具调用之间输出的文本。最终回复无文本时，返回其输出的全部文本，按顺序拼接。ACP 子智能体返回的内容由 [`result_mode`](#acp) 决定。

配置方法和调用验证参阅[配置智能体](../guides/configuration/agents.md#为智能体配置子智能体)。

## acp

顶层出现 `acp` 时，该配置会成为外部 ACP 智能体，并被强制设为仅用作子智能体。任务指令、模型和工具由外部进程决定。

外部 ACP 智能体不使用当前配置中的 `instructions`、`model.profile_id`、`tools`、`skills`、`memory`、`compaction`、`approval` 和 `sub_agents`。这些设置不会影响外部智能体的行为。

```yaml
name: ExternalReviewer
display_name: 外部审查智能体
description: 通过 ACP 调用外部审查程序
acp:
  command: example-agent
  args: [acp]
  env:
    API_KEY: "{{EXAMPLE_API_KEY}}"
  result_mode: last_segment
```

| 字段 | 类型 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `command` | 字符串 | 空 | 可执行文件名称或路径。启动外部智能体时必填；空值可以被加载，但无法启动。不能包含 NUL，也不要把参数拼入此字段。 |
| `args` | 字符串列表 | `[]` | 逐项传给进程的参数；不能包含 NUL。 |
| `env` | 字符串对象 | `{}` | 传给进程的环境变量。名称须符合环境变量命名规则，忽略大小写后不得重复；`CHRYS_ACP_SUBAGENT_DEPTH` 保留给 iCode。 |
| `cwd` | 字符串 | 空 | 外部智能体的工作目录；留空时使用 iCode 当前会话的工作目录，相对路径也以该会话目录为基准解析。 |
| `allow_external_cwd` | 布尔值 | `false` | 是否允许外部智能体在当前会话的工作目录及其子目录之外运行。默认为 `false`，指定范围外的 `cwd` 会报错并拒绝启动，不会回退到默认目录。此选项只控制启动目录校验，不限制外部智能体的文件访问权限。 |
| `session_mode` | 字符串 | 空 | 请求外部智能体使用的会话模式 ID。 |
| `model_id` | 字符串 | 空 | 尽力请求外部智能体使用的模型 ID。 |
| `config_options` | 对象 | `{}` | 外部智能体配置项；键必须是非空字符串，值只能是字符串或布尔值。 |
| `best_effort_options` | 布尔值 | `false` | 是否忽略外部智能体不支持的会话模式和配置项并继续连接。 |
| `result_mode` | `last_segment` 或 `transcript` | `last_segment` | 向父智能体返回最后一个消息片段，或完整消息记录。 |
| `handshake_timeout_seconds` | 数字 | `30.0` | 启动、初始化和打开会话的超时秒数，必须大于 `0`。YAML 中无效值会回退到默认值并记录警告。 |
| `idle_timeout_seconds` | 数字 | `600.0` | 调用期间无活动的超时秒数；`0` 表示不限时，不能为负数。YAML 中无效值会回退到默认值并记录警告。 |

**`allow_external_cwd` 的目录范围**：如果 iCode 会话由 ACP 客户端创建，且客户端提供了附加工作目录，那么即使 `allow_external_cwd` 为 `false`，外部智能体也可以在这些目录及其子目录中运行。

`command`、`args`、`env` 的值和 `cwd` 可以使用 `{{ENV_VAR}}` 引用 iCode 进程的环境变量。缺失或为空的变量会导致启动失败。连接测试、工作目录安全边界和配置项应用方式参阅[配置外部 ACP 智能体](../guides/extensions/external-acp-agents.md)。
