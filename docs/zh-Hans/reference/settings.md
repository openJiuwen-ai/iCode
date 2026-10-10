# iCode 设置文件参考

`settings.yaml` 保存 iCode 的界面偏好、默认智能体、会话和工具设置。本文按用途列出常用设置键，并说明文件位置与格式、值的写法、多个来源并存时的优先级，以及项目文件可以覆盖的范围。

在终端用户界面（Terminal User Interface，TUI）中修改设置的步骤和生效时间，见[配置 iCode 设置](../guides/configuration/settings.md)。

## 文件位置与格式

| 配置来源 | macOS / Linux | Windows | 用途 |
| --- | --- | --- | --- |
| 用户设置 | `~/.chrys/settings.yaml` | `%APPDATA%\chrys\settings.yaml` | 保存个人设置；TUI 设置窗口写入此文件 |
| 项目设置 | 工作目录下的 `.chrys/settings.yaml` | 同左 | 覆盖该工作目录的部分设置，默认不加载 |

iCode 仅在当前会话的工作目录下查找 `.chrys/settings.yaml`，不向父目录或子目录查找。

文件使用 YAML 格式。键名以点分隔表示嵌套层级，例如 `llm.retry.max_transient` 在文件中写为：

```yaml
llm:
  retry:
    max_transient: 3
```

手动编辑设置文件后，保存并重新启动 iCode 使修改生效。

## 设置键

表中“无”表示没有对应环境变量，“未指定”表示该键没有保存值（未填写或留空）。通用的取值写法与无效值处理见[值的写法与无效值](#值的写法与无效值)。

### 界面与输入

| YAML 键 | 环境变量 | 默认值 | 类型、取值与效果 |
| --- | --- | --- | --- |
| `ui.theme` | `CHRYS_THEME` | `chrys` | 字符串；使用 TUI 主题列表（按 **F9** 打开）中已有的主题名称。已停用的名称 `chrys-dark` 按 `chrys` 读取，其他未知名称回到默认主题 |
| `ui.locale` | `CHRYS_LOCALE` | `system` | 字符串；`system` 跟随系统，也可设为 `en` 或 `zh-Hans`。不支持的语言使用英语；不改变模型回复语言 |
| `ui.editor.keymap` | `CHRYS_EDITOR_KEYMAP` | `standard` | 字符串；`standard`、`emacs`、`vim`，选择输入编辑器的按键模式 |
| `ui.chat.tool_groups_expanded` | 无 | `false` | 布尔值；新出现的工具组是否默认展开详情 |
| `ui.chat.file_snapshot_inline_chars` | `CHRYS_TUI_FILE_SNAPSHOT_INLINE_CHARS` | `131072` | 整数；控制文件编辑卡片直接载入的改动前后内容上限，按两者字节数合计判断。不超过上限时随卡片载入，标题显示具体的增删行数；超过上限且有可用快照引用时，展开差异才读取，标题不显示具体行数。`0` 或负数将上限设为零；没有可用快照引用时仍可能保留内联内容。此设置只控制卡片的内容载入方式；是否备份文件内容由 `mutations.snapshot.max_file_mb` 和 `mutations.snapshot.skip_binary` 决定 |
| `history.prompt.enabled` | `CHRYS_HISTORY_DISABLE` | `true` | 布尔值；跨会话保存和读取输入历史，关闭不会删除已有记录。环境变量含义相反：只有精确的 `1` 禁用历史，其他值不禁用 |
| `app.update_check` | `CHRYS_UPDATE_CHECK` | `true` | 布尔值；TUI 每天启动时最多查询一次 iCode 的最新版本，有新版本时在欢迎页显示新版本号和升级 iCode 的命令。查询请求不包含你的任何个人信息或数据。`icode run`、`icode acp` 和 `icode serve` 不会查询 |
| `workspace.mru_max_entries` | `CHRYS_WORKSPACE_MRU_MAX_ENTRIES` | `20` | 整数；最多保留多少个最近使用的工作目录，上限 `100`，`0` 或负数禁用记录 |

### 智能体、模型与请求

模型用途键填写已有模型配置的 ID 或唯一名称；Buddy 伙伴不选择模型配置，其键填写模型服务端的模型名称（`model_id`）。

| YAML 键 | 环境变量 | 默认值 | 类型、取值与效果 |
| --- | --- | --- | --- |
| `agent.default_profile` | `CHRYS_DEFAULT_AGENT` | `Code` | 字符串；下次启动 TUI 时的默认智能体，不切换当前会话；显式 `--agent` 优先 |
| `model.profile.active` | `CHRYS_MODEL_PROFILE` | 未指定 | 字符串；选择已有模型配置，作为智能体未指定有效 `model.profile_id` 时的后备模型。显式 `--model` 优先于此键，但仍作为后备模型；ACP 会话中显式切换模型则优先于智能体指定的模型 |
| `model.role.approval_judge` | `CHRYS_MODEL_PROFILE_APPROVAL_JUDGE` | 未指定 | 字符串；自动审批使用的模型配置，未指定时使用当前模型 |
| `model.role.session_title` | `CHRYS_MODEL_PROFILE_SESSION_TITLE` | 未指定 | 字符串；自动生成会话标题使用的模型配置，未指定时使用当前模型 |
| `model.role.buddy_model_id` | `CHRYS_PET_MODEL` | 未指定 | 字符串；Buddy 伙伴使用的模型，沿用当前模型配置的连接；未指定时使用当前模型 |
| `llm.retry.max_transient` | `CHRYS_MAX_TRANSIENT_RETRIES` | `null` | 整数或 `null`；`null` 使用当前启动入口的默认次数：TUI（含 `icode serve`）和 `icode acp` 为 `10` 次，`icode run` 和 `icode workflow run` 为 `18` 次。每次重试前的等待逐次变长，最长 10 分钟。`0` 禁用瞬时错误自动重试，负数无效，上限 `50` |

瞬时错误重试用于临时网络故障、请求超时、限流等可恢复错误。提高次数也会增加最终失败前的等待时间和请求次数；该数字不等于整个任务的模型请求总数。

### 审批与项目信任

`approval` 相关键控制智能体请求执行操作时的审批行为；三种模式的差别与配置步骤见[配置审批模式](../guides/configuration/approval.md)。

| YAML 键 | 环境变量 | 默认值 | 类型、取值与效果 |
| --- | --- | --- | --- |
| `approval.default_mode` | `CHRYS_DEFAULT_APPROVAL_MODE` | `manual` | 字符串；`manual` 手动审批、`auto` 自动审批、`bypass` 跳过审批。设置默认模式不会切换当前会话 |
| `approval.timeout_seconds` | `CHRYS_APPROVAL_TIMEOUT_SECONDS` | `0` | 整数；TUI 和 ACP 等待人工审批的秒数，`0` 表示不超时，正数表示等待时限，负数调整为 `0`。超时后拒绝本次工具调用。重启 iCode 后生效 |
| `ui.approval.defer_while_judging` | 无 | `true` | 布尔值；自动模式下，TUI 是否只在审批裁判模型标记调用或评估失败时才弹出审批对话框。`false` 表示评估期间立即弹出。ACP 服务器和 `icode run` 不受影响 |
| `project.config_enabled` | 无 | `false` | 布尔值；是否加载各工作目录的项目设置，必须在用户设置中启用 |
| `project.hooks_enabled` | 无 | `false` | 布尔值；是否加载工作目录 `.chrys/hooks` 中的项目 Hooks，必须在用户设置中启用，不影响用户级 Hooks |
| `project.skills_enabled` | 无 | `false` | 布尔值；是否加载工作目录 `.agents/skills` 中的项目 Skills，必须在用户设置中启用；智能体自身的“从工作文件夹加载 Skills”选项也需保持开启 |

手动在 YAML 或环境变量中将默认审批模式设为 `bypass`，下次按此默认值启动时就会跳过审批。在 TUI 中通过 `/approval` 切换到 `bypass` 时，iCode 会将默认模式保存为 `auto`；TUI 设置窗口不提供 `bypass`。

项目设置、项目 Hooks 和项目 Skills 都随你打开的仓库而来，因此默认都不加载，并且分别开启：开启其中一项不会开启其他项。工作目录中有未开启的项时，iCode 会弹出提示，说明需要开启哪个设置。Hooks 是 iCode 在特定事件时运行的外部命令，编写与配置见[配置和编写 Hooks](../guides/extensions/hooks.md)；Skills 见[安装和使用 Skills](../guides/extensions/skills.md)。

### 会话与文件恢复

| YAML 键 | 环境变量 | 默认值 | 类型、取值与效果 |
| --- | --- | --- | --- |
| `session.title.auto` | `CHRYS_SESSION_TITLE_AUTO` | `true` | 布尔值；通过额外模型请求自动生成会话标题。手动设置标题后，不再自动更新该会话标题 |
| `storage.session_root_dir` | `CHRYS_SESSION_ROOT_DIR` | 未指定 | 路径字符串；默认使用用户配置目录，实际会话位于根目录下的 `sessions` 子目录；支持 `~`，建议使用绝对路径 |
| `rollback.snapshots_keep` | `CHRYS_ROLLBACK_SNAPSHOTS_KEEP` | `20` | 整数；每个会话保留的回滚快照数，最小 `1`；超出后删除较早快照 |
| `mutations.snapshot.max_file_mb` | `CHRYS_MUTATION_SNAPSHOT_MAX_FILE_MB` | `50` | 整数；单个文件备份的大小上限，单位 MiB。`0` 或负数取消大小限制；超限文件仍记录为有改动，但不备份内容，无法据此显示差异或恢复文件 |
| `mutations.snapshot.skip_binary` | `CHRYS_MUTATION_SNAPSHOT_SKIP_BINARY` | `true` | 布尔值；是否跳过二进制文件内容备份。设为 `false` 时仍受单文件大小上限约束 |

改变会话存储根目录不会自动迁移旧会话；指定目录不可用时会提示并回退到默认位置。迁移步骤见[迁移会话存储位置](../guides/daily-use/sessions.md#迁移会话存储位置)。

### 工具与工作目录变更

| YAML 键 | 环境变量 | 默认值 | 类型、取值与效果 |
| --- | --- | --- | --- |
| `tools.search.respect_gitignore` | 无 | `true` | 布尔值；grep 和 glob 搜索时应用 `.gitignore` 规则。设为 `false` 时仍会跳过隐藏路径，`.ignore` 等其他忽略文件也仍然生效 |
| `tools.ask_user.inline` | `CHRYS_ASK_USER_INLINE` | `false` | 布尔值；TUI 中将智能体提问显示在对话中，而不是弹出窗口 |
| `tools.ask_user.timeout_seconds` | `CHRYS_ASK_USER_TIMEOUT_SECONDS` | `900` | 整数或 `null`；等待回答的秒数，超时后向智能体返回超时结果；`0`、负数或用户 YAML 的 `null` 表示无限等待 |
| `tools.result.ceiling_tokens` | `CHRYS_TOOL_RESULT_CEILING_TOKENS` | `64000` | 整数；单条工具结果交给模型前的最终长度上限，单位 token。`0` 关闭此限制；正数最小 `2000`，负数无效 |
| `workspace.change_notice.enabled` | `CHRYS_WORKSPACE_CHANGE_NOTICE` | `true` | 布尔值；开始处理新提示词时，向智能体提供自上次提交提示词以来的工作目录文件变更摘要，包含智能体造成的改动和外部改动 |
| `workspace.change_notice.max_entries` | `CHRYS_WORKSPACE_CHANGE_NOTICE_MAX_ENTRIES` | `50` | 整数；变更摘要的最大条目数，范围 `1`–`100` |
| `mutations.parallel_implicit_tools` | `CHRYS_PARALLEL_IMPLICIT_TOOLS` | `true` | 布尔值；允许同一会话中的 Shell、Skill 脚本等可能改动文件的工具并行运行。设为 `false` 更容易判断文件改动来自哪次工具调用 |
| `mutations.coordination.enabled` | `CHRYS_MUTATION_COORDINATION` | `true` | 布尔值；协助区分共享工作目录的不同 iCode 会话造成的文件改动 |

工具自身还可能有独立的输出限制，例如 MCP 返回结果和 Skill 资源的限制。将 `tools.result.ceiling_tokens` 设为 `0` 不会关闭这些限制，也不能恢复已被工具截断的内容。此外，Shell 命令和 Skill 脚本的每一路输出最多保留 32 MiB，与这些 token 限制无关。

### 网络工具

以下键用于配置 `web_search` 和 `web_fetch` 工具。这两个工具默认关闭，还需要加入智能体才能使用。这些键没有对应环境变量，只能在用户设置中配置，项目设置不能配置。TUI 的“设置”窗口在“工具”标签页中提供常用的键，其余的键需在用户 `settings.yaml` 中设置。工具如何使用这些值，参阅[配置网络工具](../guides/configuration/web-tools.md)。

来源列表的每一项都是确切的来源（协议、主机和可选端口，例如 `https://docs.example.com`），不能包含路径、查询参数、凭据或通配符。列表可以写成 YAML 列表，也可以写成 JSON 数组字符串；TUI 保存时使用 JSON 字符串。空字符串表示空列表。两个模式键中，`true` 或不加引号的 `on` 对网络搜索表示 `auto`、对网页读取表示 `on`；`false` 或不加引号的 `off` 表示 `off`。这些值按工具使用的同一套规则检查，无效值会被报告（并说明错在哪里）然后忽略。

| YAML 键 | 默认值 | 类型、取值与效果 |
| --- | --- | --- |
| `tools.web_search.mode` | `auto` | 字符串；`auto` 或 `off`。`auto` 时，包含 `web_search` 且未配置搜索提供方的智能体会将搜索请求发送到 Exa 公开端点 `https://mcp.exa.ai/mcp`；`off` 关闭网络搜索。智能体配置文件中的 `tools.web_search.mode` 优先 |
| `tools.web_search.num_results` | `8` | 整数，范围 `1`–`20`；智能体未指定数量时每次搜索返回的结果数。智能体配置文件中的值优先 |
| `tools.web_search.timeout_seconds` | `30` | 整数，范围 `1`–`120`；整个搜索调用的截止时间（秒），包括等待空闲连接的时间。智能体配置文件中的值优先 |
| `tools.web_search.custom_endpoints` | `[]` | 对象列表，每项形如 `{"url": "...", "credential_env_names": ["..."]}`；授权一个 `custom_http` 搜索端点及其可以读取的环境变量。同一 URL 只能出现一次 |
| `tools.web_search.private_origins` | `[]` | 来源列表；即使解析到私有或其他非公网地址也允许访问的搜索端点 |
| `tools.web_search.http_origins` | `[]` | 来源列表；允许通过明文 `http://` 访问的搜索端点 |
| `tools.web_fetch.mode` | `on` | 字符串；`on` 或 `off`。`on` 模式下，包含 `web_fetch` 的智能体可以使用该工具；`off` 模式下不提供。智能体配置文件中的 `tools.web_fetch.mode` 优先 |
| `tools.web_fetch.max_tokens` | `16000` | 整数，范围 `1`–`64000`；读取的页面最多返回的词元数。智能体可以要求更少，但不能更多。智能体配置文件中的值优先 |
| `tools.web_fetch.timeout_seconds` | `60` | 整数，范围 `1`–`120`；整个网页读取调用的截止时间（秒）。智能体配置文件中的值优先 |
| `tools.web_fetch.private_origins` | `[]` | 来源列表；即使解析到私有或其他非公网地址，`web_fetch` 也可以读取的来源 |
| `tools.web_fetch.http_origins` | `[]` | 来源列表；`web_fetch` 可以通过明文 `http://` 读取的来源 |
| `tools.web_fetch.allowed_origins` | `[]` | 来源列表；非空时，`web_fetch` 只从这些来源读取 |
| `tools.web_fetch.denied_origins` | `[]` | 来源列表；`web_fetch` 从不读取的来源，优先于其他所有列表 |
| `tools.web_egress.proxy_url` | 未设置 | 字符串；仅供网络工具使用的 `http://` 或 `https://` 代理，写成不含凭据和路径的来源，例如 `http://proxy.example.com:8080`。网络工具忽略 `HTTP_PROXY`、`HTTPS_PROXY` 等变量 |
| `tools.web_egress.proxy_dns` | `local` | 字符串；`local` 或 `remote`。设置代理后，`local` 在本机解析主机名，只连接检查过的公网地址；`remote` 把主机名交给代理，由代理决定最终地址。未设置 `proxy_url` 时不起作用。参阅[使用 fake-IP 代理](../guides/configuration/web-tools.md#使用-fake-ip-代理) |

这些值中的主机名可以包含下划线。

### 上下文与轨迹分析

以下设置没有对应环境变量。

| YAML 键 | 默认值 | 类型、取值与效果 |
| --- | --- | --- |
| `context.warn_threshold_pct` | `0.5` | 数值；范围 `0`–`1`，表示上下文窗口的占用比例。用量达到阈值时向智能体提示一次上下文用量较高，回落到阈值以下后再次达到才会再提示；默认阈值为 50%；不改变自动压缩的触发条件 |
| `trajectory.verify_commands` | 内置常见测试、检查命令列表 | 字符串；轨迹分析用来识别验证操作的命令词，以逗号分隔，例如 `"pytest,ruff,npm test"`。自定义值替换整份列表；只影响分析归类，不执行这些命令 |

### 通知

以下键均为布尔值，默认均为 `true`，没有对应环境变量，用于 TUI 的事件通知。

| YAML 键 | 效果 |
| --- | --- |
| `notifications.enabled` | 事件通知总开关 |
| `notifications.delivery.desktop` | 发送桌面弹窗 |
| `notifications.delivery.sound` | 播放声音 |
| `notifications.suppress_when_focused` | iCode 获得焦点时暂停通知 |
| `notifications.events.approval_required` | 需要审批时通知 |
| `notifications.events.ask_user` | 智能体需要输入时通知 |
| `notifications.events.turn_complete` | 智能体完成任务时通知 |
| `notifications.events.turn_error` | 智能体出错时通知 |

发送通知需要总开关、对应事件开关和至少一种发送方式开启，且未因获得焦点而暂停。关闭总开关不会清除其他选择。

### 诊断与遥测

**原始 HTTP 日志可能包含明文 API 密钥、完整提示词和模型回复。** 仅在明确数据去向和访问权限后启用，排查结束后关闭不再需要的诊断选项并重启 iCode。

| YAML 键 | 环境变量 | 默认值 | 类型、取值与效果 |
| --- | --- | --- | --- |
| `log.raw_http_capture` | `CHRYS_DEBUG_LLM_RAW_HTTP_LOG` | `false` | 布尔值；将模型原始 HTTP 请求和响应写入当前会话目录的 `llm_raw_http.jsonl` |
| `otel.enabled` | `CHRYS_OTEL` | `false` | 布尔值；启用 OpenTelemetry 导出 |
| `otel.endpoint` | `CHRYS_OTEL_ENDPOINT` | 未指定 | 字符串；遥测收集器端点。未配置任何端点时仅在本地保存追踪和日志；标准 OTLP 端点环境变量优先于此设置。配置端点后停止本地保存，发送失败时不回退 |
| `otel.sensitive_data` | `CHRYS_OTEL_SENSITIVE_DATA` | `false` | 布尔值；额外记录提示词、模型回复、工具参数和工具结果，可能包含私密信息；仅在明确数据去向和访问权限后启用 |

数据保存位置、收集器连接、认证和指标查询见 [OpenTelemetry 参考](./opentelemetry.md)。

## 来源优先级

每个键独立按以下优先级取值，使用靠前的有效值。项目设置还必须符合[项目级设置](#项目级设置)的限制。

| 优先级 | 来源 |
| --- | --- |
| 1 | 当前会话中的明确选择、命令行参数或入口固定行为 |
| 2 | 启动进程时已有的环境变量 |
| 3 | 旧版用户 `.env` 中尚未迁移的设置（macOS / Linux：`~/.chrys/.env`；Windows：`%APPDATA%\chrys\.env`） |
| 4 | 已启用且允许应用的项目设置 |
| 5 | 用户设置 |
| 6 | 内置默认值 |

启动时，iCode 会尝试将旧版用户 `.env` 中的 iCode 设置迁入用户 `settings.yaml`，并清理已迁移的设置行。合并时 `settings.yaml` 中已有的同名键优先保留，不会被覆盖；尚未完成迁移的设置行仍按上表第 3 级来源生效。迁移失败时会提示，并在下次启动时重试。模型服务的 API 密钥等环境变量保留在 `.env` 中。

工作目录 `.env` 中的 iCode 设置不生效。项目设置应写入 `.chrys/settings.yaml`。

## 值的写法与无效值

下面说明向设置文件与环境变量填值的通用规则；单个键的上下限、`null` 的含义等约束，见[设置键](#设置键)各键行的“类型、取值与效果”列。

- 布尔值在 YAML 中写 `true` 或 `false`。对应的环境变量接受 `1`、`true`、`yes`、`on` 和 `0`、`false`、`no`、`off`，不区分大小写。个别环境变量的含义与名称相反或只接受特定拼写，已在对应键行注明。
- 整数直接写数字。标明上下限的键，超界值会被调整到边界；明确禁止负数的键会拒绝负值。`0` 或负数的特殊含义（禁用、取消限制等）以各键行说明为准。
- 未填写的键和空字符串通常不提供覆盖。要撤销某个文件中的设置，删除该键；删除后仍可能使用其他来源的值（见[来源优先级](#来源优先级)）。
- 类型列为“整数或 `null`”的键可写 `null`，其含义已在对应键行说明；用户设置与项目文件对 `null` 的处理不同，见[项目级设置](#项目级设置)。
- 未知键会被报告且不生效。多数无效值会被忽略并报告，取值继续向较低优先级查找——例如，环境变量中的值无效而用户设置中已有有效值时，使用用户设置的值。`approval.default_mode`、`tools.result.ceiling_tokens`、`log.raw_http_capture`、`otel.sensitive_data`、`mutations.trace.fsatrace_path` 五个键不同：一旦在查找中遇到无效值，就直接采用内置默认值并停止查找，不回退到较低优先级。例如，无法识别的审批模式会采用默认值 `manual`。

## 项目级设置

项目设置默认不加载。先在用户 `settings.yaml` 中启用，或在 TUI 的“设置 → 安全”中打开“加载项目设置”：

```yaml
project:
  config_enabled: true
```

启用后适用于所打开的工作目录。项目文件只能配置下表中的键。

有覆盖方向限制的键只接受表中“允许的覆盖方向”内的项目值，比较时以用户 YAML 中的值为基准；用户未设置时以内置默认值为基准。`llm.retry.max_transient` 在用户未设置或设为 `null` 时，按当前启动入口的默认值比较。没有方向限制的键直接采用项目值。环境变量不参与比较，但仍优先于项目设置。项目文件写入 `null` 不提供覆盖。

| 项目可配置的键 | 允许的覆盖方向 |
| --- | --- |
| `llm.retry.max_transient` | 只能减少或保持重试次数 |
| `tools.result.ceiling_tokens` | 只能降低或保持结果长度上限；`0` 表示无限制，是最宽松的值，仅当用户设置本身就是 `0` 时项目才能写 `0` |
| `session.title.auto` | 可以关闭；不能重新开启用户已关闭的自动标题 |
| `mutations.parallel_implicit_tools` | 可以关闭；不能重新开启用户已关闭的并行执行 |
| `mutations.coordination.enabled` | 可以开启；不能关闭用户已开启的跨会话改动区分 |
| `workspace.change_notice.enabled` | 可以开启；不能关闭用户已开启的变更通知 |
| `workspace.change_notice.max_entries` | 可在 `1`–`100` 范围内覆盖 |
| `context.warn_threshold_pct` | 可在 `0`–`1` 范围内覆盖 |
| `trajectory.verify_commands` | 可替换验证命令列表 |

网络工具相关的键（`tools.web_search.*`、`tools.web_fetch.*` 和 `tools.web_egress.*`）不在此表中，因此项目无法开启网络搜索，也无法扩大网络工具可连接的范围。

例如，在工作目录的 `.chrys/settings.yaml` 中写入：

```yaml
llm:
  retry:
    max_transient: 3
```

启用项目设置并重新启动后，若用户未设置重试次数，则使用项目的 `3` 次；若用户 YAML 已设为 `1`，项目中的 `3` 会被拒绝，仍使用 `1`。启动环境中的有效值始终优先。

iCode 会提示并忽略项目中不允许的键、超出覆盖限制的值和未知键，继续应用其他有效设置。

## 启动入口的差异

| 入口 | 与设置文件有关的行为 |
| --- | --- |
| `icode`（TUI） | 使用界面、输入、通知设置；默认智能体取自 `agent.default_profile` |
| `icode run` | 固定跳过审批，不等待用户回答；默认审批模式和提问超时设置不改变这些行为 |
| `icode acp` | 智能体客户端协议（Agent Client Protocol，ACP）服务；初始智能体和审批模式由 `--agent`、`--approval` 决定，默认分别为 `Code`、`manual`；提问默认无限等待，由 `--ask-user-timeout` 指定超时 |
| `icode serve` | 在浏览器中承载 TUI；配置文件和环境变量来自运行 iCode 服务的机器 |

## 设置未生效时

1. 确认设置文件的位置正确，保存后已重新启动 iCode。
2. 检查是否存在更高优先级的来源；删除 YAML 中的键不会清除其他来源的值。
3. 对项目设置，确认用户已启用加载，且键名和覆盖方向符合限制。
4. 查看 iCode 启动时报告的消息：无效值或未知键的处理规则见[值的写法与无效值](#值的写法与无效值)；目录相关提示对照[文件位置与格式](#文件位置与格式)检查。
