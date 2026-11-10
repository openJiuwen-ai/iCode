# Chat 选项

Chat 选项会给模型配置发出的每个请求加上额外字段。可以用它设置推理强度、开启或关闭思考、开启提示词缓存，或传入网关、自部署服务需要的参数。本指南先介绍填写方法，再列出常见模型服务的设置，并附上各服务的官方文档链接。

提供商文档最后核对日期：**2026-09-30**。下面的示例供按需选用，不应全部一起开启。具体支持情况取决于模型、API 接口和服务端版本；兼容某种协议不代表支持完全相同的选项。

## 添加 Chat 选项

1. 打开“模型配置”窗口（参见[配置模型](./models.md#打开模型配置窗口)），选中一个模型配置。
2. 在“其他选项”区域找到“Chat 选项（额外请求字段）”，点击“+ 添加”新增一行。
3. 在左侧输入框填写字段名，在右侧输入框填写值。
4. 点击“保存”。

保存并关闭窗口后，修改对之后的请求生效。要删除某个选项，点击该行末尾的 ✕，然后保存。

### 值的解析方式

iCode 会尽量把值当作 JSON 解析：

- `0.2` 作为数字发送，`true` 作为布尔值发送。
- `{"type": "adaptive"}` 作为对象发送，`["a", "b"]` 作为列表发送。
- 其他内容都作为文本发送：`high` 发送为 `"high"`。如果要发送看起来像数字或 `true` 的文本，请加上引号，例如 `"1"`。

整个值必须写在一行内。请仔细核对括号、引号和逗号。对象中有错误（例如多了一个结尾逗号）时，会被当作普通文本保存，模型服务随后会拒绝请求。必须填写对象的字段（例如 `extra_body`）会先经过检查：iCode 会显示错误，不会保存。

### 服务专有字段的位置

所选提供商和 API 样式对应的客户端库只接受一组固定的顶层字段名，例如 `temperature`、Chat Completions 的 `reasoning_effort`，以及 Anthropic 的 `thinking`。未知字段可能在请求发出前就导致 `unexpected keyword argument` 错误。服务扩展字段，以及 iCode 内置客户端库尚未接受的新 API 字段，请放进 `extra_body`。OpenAI 兼容服务中的例子包括 `top_k`、`enable_thinking` 和 `chat_template_kwargs`：

| 字段名 | 值 |
| --- | --- |
| `extra_body` | `{"chat_template_kwargs": {"enable_thinking": false}, "top_k": 20}` |

一个模型配置只能有一行 `extra_body`，所以请像上例一样，把所有此类字段写进同一个 JSON 对象。同一个字段既单独成行又出现在 `extra_body` 中时，实际发送的是 `extra_body` 中的值。

`extra_body` 会把字段合并到 HTTP 请求体中，但不能让服务端支持它原本不支持的参数。同样，`top_k` 对 OpenAI 兼容服务是扩展字段，对 Anthropic API 则是原生字段。

### 有专门字段的设置

以下设置不要写在 Chat 选项中：

- **输出长度**：使用“最大输出词元数”字段。保存时会拒绝 `max_tokens` 等行。
- **流式输出**：使用“流式输出”复选框，默认开启，取消勾选即可关闭。不要添加 `stream` 行。
- **模型**：使用“模型”字段。
- **HTTP 请求头**：使用“HTTP 附加请求头”各行，或者添加一行 `extra_headers`，值为 JSON 对象。对于 Anthropic 的 `anthropic-beta` 请求头，“HTTP 附加请求头”会添加到 iCode 的 Beta 标志之后，而 `extra_headers` 行会替换它们，详见 [Anthropic](#anthropic)。
- **Claude 思考设置**：使用配置文件中的 `thinking_block_binding` 和 `auto_interleaved_thinking` 两行，详见 [Claude 思考设置](./models.md#claude-思考设置)。

消息、工具和系统提示词由 iCode 自行构建，因此保存时会拒绝 `messages` 等字段。

### 不在配置中保存密钥

要从环境变量读取值的一部分，用双大括号写上变量名。例如，把网关令牌作为请求头发送：

| 字段名 | 值 |
| --- | --- |
| `extra_headers` | `{"X-Api-Token": "{{MY_GATEWAY_TOKEN}}"}` |

配置文件中保留的是占位符，iCode 在发送请求时才填入实际值。请在启动 iCode 之前设置好该变量；变量未设置时，iCode 会报错并指出变量名。

请求头的值也可以来自命令，例如 `{"X-Api-Token": "{{!tokenprovider gettoken}}"}`，参阅[通过命令获取 API 密钥](./models.md#通过命令获取-api-密钥)。这种写法只能用在 `extra_headers` 中。在 Chat 选项的其他位置使用会报错，因为 iCode 无法替换请求正文里已过期的值。如果要按原样发送这类文本，请转义为 `{{{{!...}}}}`。

### 哪些请求使用 Chat 选项

使用同一模型配置的请求会共用 Chat 选项，包括主智能体、子智能体、工作流中的智能体、会话标题生成、自动审批的判断模型以及上下文压缩。具体操作可能根据自身请求覆盖或过滤部分选项。较高的推理强度也可能增加这些辅助请求的耗时和费用。

## 提示词缓存

在 iCode 默认的本地历史模式下，请求会重新发送当前上下文中的对话。提示词缓存让服务复用未变化的前缀，减少重复处理输入的工作量，通常也能降低输入费用。它与服务端保存对话是两回事。写入缓存可能有额外费用，能否节省费用取决于前缀的复用次数。

OpenAI、DeepSeek、GLM、Kimi 的 Chat Completions 接口和通义千问在支持的模型上提供自动缓存。vLLM 和 SGLang 的前缀缓存由服务端管理，是否生效取决于启动配置和模型支持情况。来源和例外见下文各提供商章节。

**Anthropic 的 Claude API 需要显式设置缓存。** 要自动缓存逐渐增长的对话前缀，请添加这一行：

| 字段名 | 值 |
| --- | --- |
| `extra_body` | `{"cache_control": {"type": "ephemeral"}}` |

如果配置中已有 `extra_body` 行，请把 `"cache_control": {"type": "ephemeral"}` 加进它的对象中。Anthropic 标准的 5 分钟缓存写入费用为基础输入价格的 1.25 倍；读取费用为 0.1 倍或更低，因模型而异。如需保留 1 小时，使用 `{"type": "ephemeral", "ttl": "1h"}`，写入费用为 2 倍。有效期从写入或复用缓存的请求开始时计算，因此生成回答也会消耗其中一部分时间。此外，模型还有最短可缓存前缀的要求。当前限制和计费规则见 [Anthropic 提示词缓存文档](https://platform.claude.com/docs/en/build-with-claude/prompt-caching)。

### “提示词缓存未开启”提醒

保存模型配置时，如果提供商为“Anthropic”、模型 ID 包含 `claude`，且没有任何 `cache_control` 设置，iCode 会显示“提示词缓存未开启”窗口：

- **添加并保存**：把 `"cache_control": {"type": "ephemeral"}` 加入 `extra_body` 行（没有该行时自动新建），然后保存。
- **直接保存**：按原样保存配置。
- 按 **Esc** 或点击窗口外部：返回表单，不保存。

当某次保存让该配置成为完整的 Claude 配置时（新建的配置，或改用“Anthropic”提供商、改成 Claude 模型的配置），iCode 会显示这个提醒。保存本来就是 Claude 配置的配置时，不会再次提醒。

兼容 Anthropic 协议的服务有各自的缓存行为，可能接受、忽略或拒绝 `cache_control`，请查阅对应接口的文档。如果服务拒绝该字段，只从 `extra_body` 中删除 `"cache_control": {...}`，然后保存。此提醒只检查配置内容，无法判断服务端是否实际缓存了请求。

### 检查缓存是否生效

打开侧边栏（按 **Ctrl+G** 显示或隐藏），选择“上下文”标签页。在“词元用量”区域，“缓存命中”一行显示本会话累计报告的缓存读取量。只有请求报告命中时才会增长，不保证每次调用都增长。显示 `-` 不能证明缓存未开启：可能没有命中，也可能服务没有返回缓存用量。

如果“缓存命中”一直显示 `-` 或不再增长：

- 检查模型配置是否包含该服务需要的设置（参见[常见模型服务的设置](#常见模型服务的设置)）。
- 检查模型的最短可缓存前缀、有效期和缓存写入要求。短提示词可能不满足条件，已过期或被清理的条目也无法复用。
- 保持可复用内容稳定。更换模型、工具、推理设置或压缩历史都可能减少缓存复用，但发生变化不一定让此前所有匹配的前缀失效。
- 即使输入相同，请求被路由到不同后端时也可能无法命中。自动缓存不保证每次命中。

## 常见模型服务的设置

以下各节先给出推荐的提供商、API 样式和服务地址，再列出常用设置。字段名和取值范围会随模型变化，请以所用模型的官方文档为准。

### OpenAI

提供商选“OpenAI”，API 样式选“Responses”，服务地址使用默认值。“Chat Completions”也可以使用，但部分较新的模型只在 Responses 下支持工具调用。提示词缓存自动生效。

| 用途 | 字段名 | 值 |
| --- | --- | --- |
| 推理强度（Responses） | `reasoning` | `{"effort": "high"}` |
| 推理摘要（Responses） | `reasoning` | `{"effort": "high", "summary": "auto"}` |
| 推理强度（Chat Completions） | `reasoning_effort` | `high` |
| 回答更简短 | `verbosity` | `low` |
| 更便宜、处理更慢 | `service_tier` | `flex` |
| GPT-5.6 及之后模型的缓存有效期（Responses） | `extra_body` | `{"prompt_cache_options": {"mode": "implicit", "ttl": "30m"}}` |

使用 Responses 时，请用 `reasoning`，不要用 `reasoning_effort`。iCode 会把 `verbosity` 选项转换为 API 的 `text.verbosity`。支持的推理强度、摘要、详细程度和 Flex 服务因模型而异。Flex 还可能返回资源不可用错误，参见 [Flex processing](https://developers.openai.com/api/docs/guides/flex-processing)。

GPT-5.6 及之后的模型使用 `prompt_cache_options`，有效期为 `30m`，自动缓存写入也会计费。较早的模型使用 `prompt_cache_retention`，取值可能为 `in_memory` 或 `24h`，取决于具体模型，两类设置不能混用。请保留 `implicit` 模式：`explicit` 模式需要在内容块上添加缓存断点，Chat 选项表单不会添加这些断点。

使用 Responses 且地址为 OpenAI 官方地址时，iCode 还会把会话 ID 作为 `prompt_cache_key` 发送，同一会话的请求共用一个缓存键；子智能体和工作流中的智能体各用自己的键。要使用自定义的键，请添加 `prompt_cache_key` 行；不想发送，请将其值设为 `null`。

除非需要服务端续接，否则请不要设置 `store`：iCode 默认发送 `false`，这仍然允许提示词缓存。设为 `true` 后，iCode 的上下文压缩会被禁用。

官方文档：[Reasoning](https://developers.openai.com/api/docs/guides/reasoning)、[Prompt caching](https://developers.openai.com/api/docs/guides/prompt-caching)、[Responses API 参考](https://developers.openai.com/api/reference/resources/responses/methods/create)、[Chat Completions API 参考](https://developers.openai.com/api/reference/resources/chat/subresources/completions/methods/create)。

### Anthropic

提供商选“Anthropic”，服务地址为 `https://api.anthropic.com`，不要在末尾加 `/v1`。**请开启提示词缓存**，方法见[提示词缓存](#提示词缓存)。

| 用途 | 字段名 | 值 |
| --- | --- | --- |
| 提示词缓存 | `extra_body` | `{"cache_control": {"type": "ephemeral"}}` |
| 自适应思考并返回可读摘要 | `thinking` | `{"type": "adaptive", "display": "summarized"}` |
| 固定预算的思考（较早的模型） | `thinking` | `{"type": "enabled", "budget_tokens": 8000}` |
| 投入程度（effort） | `output_config` | `{"effort": "medium"}` |
| 其他地址上较早模型的交错思考 | `additional_beta_flags` | `["interleaved-thinking-2025-05-14"]` |

- 只有支持自适应思考的模型才能使用 `adaptive`；较新的模型会拒绝旧的 `enabled` / `budget_tokens` 形式。普通固定预算思考要求“最大输出词元数”大于 `budget_tokens`，手动交错思考的预算规则有所不同。
- `display: "summarized"` 用于请求服务返回可读的思考摘要。较新的 Claude 模型（包括 Opus 5.5 和 Fable 5.1）默认隐藏这部分文字。请在已有的 `thinking` 对象中加入 `"display": "summarized"`；iCode 的“默认展开思考”设置只决定返回的文字在写出时完整展开还是只显示一行，不会向服务请求摘要。如需让接口隐藏摘要，可改为 `"display": "omitted"`；这不会关闭思考，也不会减少思考词元的费用。
- 较新的 Claude 模型会拒绝非默认的 `temperature`、`top_p` 和 `top_k` 值，请不要设置。
- `additional_beta_flags` 和 `betas` 用于给请求增加 Beta 标志，“HTTP 附加请求头”中的 `anthropic-beta` 也一样。iCode 会把它们排在自己的标志之后，合并到同一个 `anthropic-beta` 请求头中发送。`extra_headers` 行中的 `anthropic-beta` 会替换所有这些标志，包括 iCode 自己的标志；此时 iCode 只补上思考设置所需的标志。
- 在 Anthropic 官方地址上使用固定预算的思考时，iCode 会自动添加交错思考的标志，详见 [Claude 思考设置](./models.md#claude-思考设置)。在其他地址上，请按上表自行添加。自适应思考已自动支持交错思考，无需该标志。交错思考的支持情况因模型而异，服务接受某个 Beta 标志并不代表它实际生效。
- 在 Anthropic 官方地址上，Claude Opus 5.5、Fable 5.1 或 Sonnet 5.5 使用自适应思考时，iCode 会请服务略去与对话对不上的早先思考内容，详见 [Claude 思考设置](./models.md#claude-思考设置)。你在 `thinking` 中自己写的 `block_binding` 会原样发送，iCode 会补上它所需的 Beta 标志。

官方文档：[Prompt caching](https://platform.claude.com/docs/en/build-with-claude/prompt-caching)、[Adaptive thinking](https://platform.claude.com/docs/en/build-with-claude/thinking)、[Extended thinking](https://platform.claude.com/docs/en/build-with-claude/extended-thinking)、[Effort](https://platform.claude.com/docs/en/build-with-claude/effort)、[Beta headers](https://platform.claude.com/docs/en/api/beta-headers)。

### DeepSeek

提供商选“DeepSeek (OpenAI)”，API 样式选“Chat Completions”，服务地址使用默认的 `https://api.deepseek.com`。提示词缓存自动生效。

| 用途 | 字段名 | 值 |
| --- | --- | --- |
| 关闭思考 | `extra_body` | `{"thinking": {"type": "disabled"}}` |
| 推理强度 | `reasoning_effort` | `high` |

当前 API 默认开启思考，推理强度支持 `low`、`high` 和 `max`。在思考模式下，`temperature`、`presence_penalty` 和 `frequency_penalty` 会被忽略；`top_p` 仅在 0.95 到 1.0 范围内生效。较早模型 ID 的能力可能不同。

DeepSeek 也支持 Responses API。使用服务端 `web_search` 时必须选 Responses，并添加 `tools` 选项；支持的模型以 DeepSeek 官方文档为准。

| 用途 | 字段名 | 值 |
| --- | --- | --- |
| 服务端联网搜索（Responses） | `tools` | `[{"type": "web_search"}]` |

声明后，模型提供方会接管 `web_search` 这个名称；如果智能体也启用了本地 `web_search`，本地工具会在该次运行中停用并显示提示。服务端搜索不受 iCode 审批和网络设置约束。也可以改用本地 `web_search` 调 DeepSeek Search API，配置见[配置网络工具](./web-tools.md#deepseek-search-api)。

官方文档：[思考模式](https://api-docs.deepseek.com/guides/thinking_mode)、[上下文硬盘缓存](https://api-docs.deepseek.com/guides/kv_cache)、[API 参考](https://api-docs.deepseek.com/api/create-chat-completion)。

### GLM（智谱 / Z.ai）

提供商选“GLM (OpenAI)”。默认服务地址为 `https://open.bigmodel.cn/api/paas/v4`；在中国大陆以外请使用 `https://api.z.ai/api/paas/v4`；使用 GLM Coding Plan 时，请使用 `https://open.bigmodel.cn/api/coding/paas/v4` 或 `https://api.z.ai/api/coding/paas/v4`。提示词缓存自动生效。

| 用途 | 字段名 | 值 |
| --- | --- | --- |
| 关闭思考 | `extra_body` | `{"thinking": {"type": "disabled"}}` |
| 在上下文中保留之前的推理 | `extra_body` | `{"thinking": {"type": "enabled", "clear_thinking": false}}` |
| 推理强度（GLM-5.2 及之后） | `reasoning_effort` | `high` |

GLM-5.3 始终思考，推理强度支持 `low`、`high` 和 `max`，不要给它传入 `thinking.type: disabled`。GLM-5.2 的强度映射不同，更早的模型不支持该强度字段。`clear_thinking: false` 还要求完整保留历史推理，iCode 的 GLM 提供商会保留这些内容。`temperature` 的取值范围为 0 到 1。

官方文档：[Thinking mode](https://docs.z.ai/guides/capabilities/thinking-mode)、[Context caching](https://docs.z.ai/guides/capabilities/cache)、[API 参考](https://docs.z.ai/api-reference/llm/chat-completion)。

### Kimi（月之暗面）

提供商选“OpenAI”，API 样式选“Chat Completions”，服务地址为 `https://api.moonshot.cn/v1`（中国大陆以外为 `https://api.moonshot.ai/v1`）。提示词缓存自动生效。

| 用途 | 字段名 | 值 |
| --- | --- | --- |
| 推理强度（Kimi K3） | `reasoning_effort` | `high` |
| 关闭思考（Kimi K2.6） | `extra_body` | `{"thinking": {"type": "disabled"}}` |
| 延长缓存有效期（Chat Completions） | `extra_body` | `{"prompt_cache_options": {"mode": "implicit", "ttl": "1h"}}` |

Kimi K3 始终思考，强度支持 `low`、`high` 和 `max`，不接受 `thinking` 字段。K2.7 Code 始终思考，但不支持 `reasoning_effort`；K2.6 可以关闭思考，也不支持该强度字段。除非具体模型的文档明确支持，否则请不要设置采样选项；部分 Kimi 模型的固定值限制不代表整个系列都采用相同的采样规则。iCode 只接受省略 `n` 或将其设为 `1`。

Chat Completions 默认使用 5 分钟缓存。1 小时选项的写入价格更高；已有缓存条目过期前，修改 TTL 不会改变其有效期。

Kimi 也提供兼容 Anthropic 协议的接口：提供商选“Anthropic”，服务地址为 `https://api.moonshot.ai/anthropic`。在该接口上，只有请求中要求缓存时才会写入缓存，因此请添加[提示词缓存](#提示词缓存)中的 `cache_control` 设置。

官方文档：[Chat API](https://platform.kimi.ai/docs/api/chat)、[使用思考模型](https://platform.kimi.ai/docs/guide/use-thinking-models)、[上下文缓存](https://platform.kimi.ai/docs/guide/context-caching)、[Anthropic 兼容 API](https://platform.kimi.ai/docs/api/messages)。

### 通义千问（阿里云百炼）

提供商选“OpenAI”，API 样式选“Chat Completions”，服务地址使用百炼控制台中的 OpenAI 兼容地址。目前推荐工作空间专属接口，例如 `https://<WorkspaceId>.cn-beijing.maas.aliyuncs.com/compatible-mode/v1`（北京）或 `https://<WorkspaceId>.ap-southeast-1.maas.aliyuncs.com/compatible-mode/v1`（新加坡），请将 `<WorkspaceId>` 替换为实际工作空间 ID。旧的 `dashscope.aliyuncs.com` 和 `dashscope-intl.aliyuncs.com` 接口仍可使用。API 密钥与接口地域必须匹配，其他地域和 Coding Plan 使用各自的接口。在支持的模型上，隐式缓存自动开启，但不保证命中。

| 用途 | 字段名 | 值 |
| --- | --- | --- |
| 关闭思考 | `extra_body` | `{"enable_thinking": false}` |
| 限定思考预算 | `extra_body` | `{"enable_thinking": true, "thinking_budget": 4096}` |
| 采样 | `extra_body` | `{"top_k": 20}` |
| 允许模型联网搜索 | `extra_body` | `{"enable_search": true}` |

`enable_thinking: false` 适用于混合思考模型，纯思考模型无法关闭思考。预算范围、`top_k` 和 `enable_search` 的支持情况也因模型而异。部分开源千问模型的思考模式要求开启“流式输出”，商业模型则可能同时支持非流式请求。百炼还提供通过消息内容块标记启用的显式缓存，Chat 选项表单不会添加这些标记。

官方文档：[OpenAI 兼容](https://www.alibabacloud.com/help/en/model-studio/compatibility-of-openai-with-dashscope)、[深度思考](https://www.alibabacloud.com/help/en/model-studio/deep-thinking)、[上下文缓存](https://www.alibabacloud.com/help/en/model-studio/context-cache)。

### vLLM

提供商选“OpenAI”，API 样式选“Chat Completions”，服务地址为 `http://<主机>:<端口>/v1`。“API 密钥”填写服务端 `--api-key` 的值；服务端未设置密钥时留空。前缀缓存由服务端配置，请检查 `--enable-prefix-caching` / `--no-enable-prefix-caching` 以及所部署模型的支持情况。

对于需要使用工具的 iCode 智能体，启动服务时请加上 `--enable-auto-tool-choice` 和与模型匹配的 `--tool-call-parser`。推理模型还应配置匹配的 `--reasoning-parser`，以分开显示推理过程和回答。

| 用途 | 字段名 | 值 |
| --- | --- | --- |
| 关闭思考（Qwen3 等） | `extra_body` | `{"chat_template_kwargs": {"enable_thinking": false}}` |
| 推理强度 | `reasoning_effort` | `low` |
| 采样 | `extra_body` | `{"top_k": 20, "min_p": 0.0, "repetition_penalty": 1.05}` |

这些参数取决于模型模板，不是通用开关。有些模型始终思考，有些使用不同的开关。`reasoning_effort` 也不是在每个模型上都提供相同的强度等级。请使用与所安装 vLLM 版本对应的文档。

官方文档：[Tool calling](https://docs.vllm.ai/en/latest/features/tool_calling/)、[Reasoning outputs](https://docs.vllm.ai/en/latest/features/reasoning_outputs/)、[OpenAI-compatible server](https://docs.vllm.ai/en/latest/serving/openai_compatible_server/)、[Server arguments](https://docs.vllm.ai/en/latest/cli/serve/)。

### SGLang

提供商选“OpenAI”，API 样式选“Chat Completions”，服务地址为 `http://<主机>:<端口>/v1`。使用工具时配置工具解析器，使用推理模型时配置推理解析器。普通服务配置默认开启前缀缓存，但 `--disable-radix-cache` 或模型自身的限制可能将其禁用。

| 用途 | 字段名 | 值 |
| --- | --- | --- |
| 关闭思考（混合思考的 Qwen3 模型） | `extra_body` | `{"chat_template_kwargs": {"enable_thinking": false}}` |
| 开启思考（DeepSeek-V3.1） | `extra_body` | `{"chat_template_kwargs": {"thinking": true}}` |
| 推理强度 | `reasoning_effort` | `low` |
| 采样 | `extra_body` | `{"top_k": 20, "min_p": 0.0, "repetition_penalty": 1.05}` |

模板接受的字段和强度等级取决于模型和 SGLang 版本。推理解析器负责分离输出，本身不会开启思考。保持 `separate_reasoning` 开启，才能分开显示推理过程。`reasoning_effort` 应在请求或服务端模板默认值中择一设置，不要两处同时设置。

官方文档：[OpenAI-compatible API](https://docs.sglang.io/docs/basic_usage/openai_api_completions)、[Server arguments](https://docs.sglang.io/docs/advanced_features/server_arguments)、[Tool parser](https://docs.sglang.io/docs/advanced_features/tool_parser)、[Reasoning parser](https://docs.sglang.io/docs/advanced_features/separate_reasoning)。

### LiteLLM 代理

提供商选“OpenAI”，API 样式选“Chat Completions”，服务地址为 `http://<主机>:4000`，API 密钥填写 LiteLLM 虚拟密钥（`sk-...`）。使用 Claude 模型时，也可以选择提供商“Anthropic”并使用相同的服务地址，这样会连接到 LiteLLM 的 Anthropic 格式接口。

| 用途 | 字段名 | 值 |
| --- | --- | --- |
| 为费用统计打标签 | `extra_body` | `{"metadata": {"tags": ["team-a"]}}` |
| 按标签路由 | `extra_body` | `{"tags": ["free"]}` |

标签路由要求代理配置中已有匹配的部署标签，并开启标签过滤；只在请求中传入标签不会自动配置路由。费用报表还取决于代理的统计配置和可用功能。

对于不支持的 OpenAI 参数，代理管理员可以配置 `drop_params`，这些设置随后会被静默省略。它不能通用地修复无效的提供商扩展字段或参数值，此类问题应在模型配置中删除或修正对应字段。

官方文档：[Virtual keys](https://docs.litellm.ai/docs/proxy/user_keys)、[Cost tracking](https://docs.litellm.ai/docs/proxy/cost_tracking)、[Tag routing](https://docs.litellm.ai/docs/proxy/tag_routing)、[Drop unsupported params](https://docs.litellm.ai/docs/completion/drop_params)、[Anthropic `/v1/messages` 接口](https://docs.litellm.ai/docs/anthropic_unified)。

### OpenRouter

提供商选“OpenAI”，API 样式选“Chat Completions”，服务地址为 `https://openrouter.ai/api/v1`。

| 用途 | 字段名 | 值 |
| --- | --- | --- |
| 指定由哪些提供商提供模型 | `extra_body` | `{"provider": {"order": ["anthropic"], "allow_fallbacks": false}}` |
| 推理强度 | `extra_body` | `{"reasoning": {"effort": "high"}}` |
| Claude 模型的提示词缓存 | `extra_body` | `{"cache_control": {"type": "ephemeral"}}` |

需要同时使用时，请写进同一个对象，例如 `{"provider": {"order": ["anthropic"]}, "cache_control": {"type": "ephemeral"}}`。

`provider.order` 设置优先顺序；如果只允许所列提供商处理请求，请保留 `allow_fallbacks: false`。顶层 `cache_control` 可用于 Claude 的自动缓存，但仍需模型和提供商支持。OpenRouter 文档规定的行为可能与直接调用提供商不同。

官方文档：[Quickstart](https://openrouter.ai/docs/quickstart)、[Provider selection](https://openrouter.ai/docs/guides/routing/provider-selection)、[Reasoning tokens](https://openrouter.ai/docs/guides/best-practices/reasoning-tokens)、[Prompt caching](https://openrouter.ai/docs/guides/best-practices/prompt-caching)。

## 添加选项后请求失败时

- **保存时提示“Chat 选项第 N 行：……”**：按提示修改对应的行，然后重新保存。
- **报错中包含 `unexpected keyword argument 'x'`**：把字段 `x` 移入 `extra_body`，方法见[服务专有字段的位置](#服务专有字段的位置)。
- **模型服务提示不支持某个字段**：只删除该字段。如果它位于 `extra_body` 中，只从 JSON 对象里删除这一项，保留其他项，然后保存。
- **报错中提到某个环境变量**：设置该变量后重启 iCode，或者删除对应的 `{{...}}` 占位符。
- **报错说命令失败**：在终端中运行 `{{!...}}` 占位符里的命令查看原因，修好后输入 `/new` 或重启 iCode。参阅[通过命令获取 API 密钥](./models.md#通过命令获取-api-密钥)。
- **找不到明显原因**：逐个删除最近添加的行，直到请求恢复正常，并查阅所用模型的官方文档。
