# Chat Options

Chat Options add fields to every request a model profile sends. Use them to set reasoning effort, turn thinking on or off, turn on prompt caching, or pass settings that a gateway or self-hosted server needs. This guide explains how to fill them in, then lists settings for common model services, with links to each service's own documentation.

Provider documentation last checked: **2026-09-30**. Examples are alternatives, not a list to enable together. Support depends on the exact model, API endpoint, and server version; a compatible protocol does not guarantee identical options.

## Add a Chat Option

1. Open the "Model Configuration" window (see [Configure models](./models.md#open-the-model-configuration-window)) and select a model profile.
2. In the "Extra Options" section, find "Chat Options". Click "+ Add" for a new row.
3. Enter the field name in the left box and its value in the right box.
4. Click "Save".

Changes apply to later requests after you save and close the window. To remove an option, click ✕ at the end of its row, then save.

### How values are read

iCode reads each value as JSON when it can:

- `0.2` is sent as a number and `true` as a boolean.
- `{"type": "adaptive"}` is sent as an object and `["a", "b"]` as a list.
- Anything else is sent as text: `high` is sent as `"high"`. To send text that looks like a number or `true`, put it in quotes, for example `"1"`.

Keep the whole value on one line. Check braces, quotes, and commas carefully. An object with a typo, such as a trailing comma, is saved as plain text, and the model service then rejects the request. Fields that must hold an object, such as `extra_body`, are checked instead: iCode shows an error and does not save.

### Where service-specific fields go

The client library for the selected provider and API style accepts a fixed set of field names at the top level, such as `temperature`, `reasoning_effort` for Chat Completions, or `thinking` for Anthropic. An unknown keyword can fail before the request reaches the server, with `unexpected keyword argument`. Put service extensions, or new API fields that iCode's bundled client library does not yet accept, inside `extra_body`. Examples for OpenAI-compatible servers include `top_k`, `enable_thinking`, and `chat_template_kwargs`:

| Key | Value |
| --- | --- |
| `extra_body` | `{"chat_template_kwargs": {"enable_thinking": false}, "top_k": 20}` |

A profile can have only one `extra_body` row, so put every such field into the same JSON object, as in the example above. When a field appears both as its own row and inside `extra_body`, the value in `extra_body` is the one sent.

`extra_body` merges fields into the HTTP request body; it does not make an unsupported field work on the server. Likewise, `top_k` is an extension for OpenAI-compatible servers but a native field on Anthropic's API.

### Settings that have their own fields

Some settings don't belong in Chat Options:

- **Output length**: use the "Max Output Tokens" field. Rows such as `max_tokens` are refused at save.
- **Streaming**: use the "Streaming" checkbox, which is on by default; uncheck it to turn streaming off. Don't add a `stream` row.
- **Model**: use the "Model" field.
- **HTTP headers**: use the "HTTP Extra Headers" rows, or an `extra_headers` row whose value is a JSON object. For Anthropic's `anthropic-beta` header, "HTTP Extra Headers" add to iCode's beta flags, while an `extra_headers` row replaces them; see [Anthropic](#anthropic).
- **Claude thinking settings**: use the `thinking_block_binding` and `auto_interleaved_thinking` lines of the profile file; see [Claude thinking settings](./models.md#claude-thinking-settings).

iCode builds the messages, tools, and system prompt itself, so fields such as `messages` are refused at save.

### Keep secrets out of the profile

To read part of a value from an environment variable, write the variable name in double braces. For example, to send a gateway token as a header:

| Key | Value |
| --- | --- |
| `extra_headers` | `{"X-Api-Token": "{{MY_GATEWAY_TOKEN}}"}` |

The profile file keeps the placeholder, and iCode fills in the value when it sends a request. Set the variable before starting iCode. If it isn't set, iCode reports an error naming the variable.

A header can also get its value from a command, such as `{"X-Api-Token": "{{!tokenprovider gettoken}}"}`; see [Get an API key from a command](./models.md#get-an-api-key-from-a-command). This works only inside `extra_headers`. Anywhere else in Chat Options it is an error, because iCode can't replace an expired value in the body of a request. To send such text as written, escape it as `{{{{!...}}}}`.

### Which requests use Chat Options

Chat Options are shared by requests made with the profile, including the main agent, sub-agents, workflow agents, session titles, the automatic approval judge, and context compaction. Individual operations may override or filter options for their own request. Higher reasoning effort can also increase the latency and cost of these auxiliary requests.

## Prompt caching

In iCode's default local-history mode, requests resend the conversation currently in context. Prompt caching lets the service reuse an unchanged prefix, reducing repeated input processing and usually its price. It is separate from storing a conversation on the service. Cache writes may cost extra, so savings depend on how often the prefix is reused.

OpenAI, DeepSeek, GLM, Kimi's Chat Completions API, and Qwen offer automatic caching on supported models. vLLM and SGLang manage prefix caching on the server; their launch settings and model support determine whether it is active. See the provider sections below for sources and exceptions.

**Anthropic's Claude API requires a cache setting.** To enable automatic caching of the growing conversation prefix, add this row:

| Key | Value |
| --- | --- |
| `extra_body` | `{"cache_control": {"type": "ephemeral"}}` |

If the profile already has an `extra_body` row, add `"cache_control": {"type": "ephemeral"}` to its object instead. Anthropic's standard 5-minute writes cost 1.25× the base input rate; reads cost 0.1× or less, depending on the model. For a 1-hour entry, use `{"type": "ephemeral", "ttl": "1h"}`; writes cost 2×. The TTL runs from the start of a request that writes or reuses the entry, so generation time consumes part of it. A model-specific minimum prefix length also applies. See [Anthropic prompt caching](https://platform.claude.com/docs/en/build-with-claude/prompt-caching) for current limits and pricing rules.

### The "Prompt Caching Is Off" reminder

When you save a profile with the "Anthropic" provider, a model ID containing `claude`, and no `cache_control` setting, iCode shows the "Prompt Caching Is Off" window:

- **Add and Save** adds `"cache_control": {"type": "ephemeral"}` to the `extra_body` row, creating the row if needed, then saves.
- **Save as Is** saves the profile unchanged.
- Press **Esc** or click outside the window to return to the form without saving.

iCode shows the reminder on the save that makes the profile a complete Claude profile: a new profile, or one you changed to the Anthropic provider or to a Claude model. Saving a profile that is already a Claude profile doesn't show it again.

Anthropic-compatible services have their own cache behavior: they may honor, ignore, or reject `cache_control`. Check the endpoint's documentation. If it rejects the field, remove only `"cache_control": {...}` from `extra_body` and save. The reminder checks the profile's settings, not whether the server actually cached a request.

### Check that caching works

Open the sidebar (**Ctrl+G** shows or hides it) and select the "Context" tab. In the "Token Usage" section, the "Cached" row shows the session's accumulated reported cache reads. It grows when a request reports a hit, not necessarily on every call. `-` does not prove caching is disabled: the service may report no hits or omit cache usage.

If "Cached" stays at `-` or stops growing:

- Check that the profile has the setting the service needs (see [Settings for common model services](#settings-for-common-model-services)).
- Check the model's minimum cacheable prefix length, retention period, and cache-write requirements. Short prompts may not qualify, and expired or evicted entries cannot be reused.
- Keep reusable content stable. Changing the model, tools, reasoning settings, or compacting history can reduce cache reuse; a change need not invalidate every earlier matching prefix.
- A request routed to a different backend may miss even with identical input. Automatic caching does not guarantee a hit.

## Settings for common model services

Each section below lists the recommended provider, API style, and base URL, then some commonly used settings. Field names and allowed values change with each model, so check the linked documentation for the model you use.

### OpenAI

Provider "OpenAI", API style "Responses", with the default base URL. "Chat Completions" works too, but some newer models support tool calls only on Responses. Prompt caching is automatic.

| What you want | Key | Value |
| --- | --- | --- |
| Reasoning effort (Responses) | `reasoning` | `{"effort": "high"}` |
| Reasoning summaries (Responses) | `reasoning` | `{"effort": "high", "summary": "auto"}` |
| Reasoning effort (Chat Completions) | `reasoning_effort` | `high` |
| Shorter answers | `verbosity` | `low` |
| Cheaper, slower processing | `service_tier` | `flex` |
| Cache lifetime on GPT-5.6 and later (Responses) | `extra_body` | `{"prompt_cache_options": {"mode": "implicit", "ttl": "30m"}}` |

On Responses, use `reasoning` rather than `reasoning_effort`. iCode translates its `verbosity` option to the API's `text.verbosity`. Allowed effort levels, summaries, verbosity, and Flex availability vary by model. Flex can also return resource-unavailable errors; see [Flex processing](https://developers.openai.com/api/docs/guides/flex-processing).

GPT-5.6 and later use `prompt_cache_options` with a `30m` TTL; automatic cache writes are billable. Earlier models use `prompt_cache_retention`, with values such as `in_memory` or `24h` depending on the model. These settings are not interchangeable. Keep `mode` implicit: explicit mode requires content-block breakpoints, which the Chat Options form does not add.

On Responses with OpenAI's own address, iCode also sends the session ID as `prompt_cache_key`, so requests from one session share a cache key; sub-agents and workflow agents use their own. To use a key of your own, add a `prompt_cache_key` row. To send none, set its value to `null`.

Leave `store` unset unless you want service-side continuation: iCode sends `false` by default, which still allows prompt caching. Setting it to `true` disables iCode's context compaction.

Documentation: [Reasoning](https://developers.openai.com/api/docs/guides/reasoning), [Prompt caching](https://developers.openai.com/api/docs/guides/prompt-caching), [Responses API reference](https://developers.openai.com/api/reference/resources/responses/methods/create), [Chat Completions API reference](https://developers.openai.com/api/reference/resources/chat/subresources/completions/methods/create).

### Anthropic

Provider "Anthropic", base URL `https://api.anthropic.com`. Don't add `/v1` to the base URL. **Add prompt caching**, as described in [Prompt caching](#prompt-caching).

| What you want | Key | Value |
| --- | --- | --- |
| Prompt caching | `extra_body` | `{"cache_control": {"type": "ephemeral"}}` |
| Adaptive thinking with readable summaries | `thinking` | `{"type": "adaptive", "display": "summarized"}` |
| Thinking with a fixed budget (older models) | `thinking` | `{"type": "enabled", "budget_tokens": 8000}` |
| Effort | `output_config` | `{"effort": "medium"}` |
| Interleaved thinking on older models at another address | `additional_beta_flags` | `["interleaved-thinking-2025-05-14"]` |

- Use `adaptive` only on models that support it; recent models reject the older `enabled`/`budget_tokens` form. For ordinary fixed-budget thinking, set "Max Output Tokens" higher than `budget_tokens` (manual interleaved thinking has different budget rules).
- `display: "summarized"` asks the service to return a readable summary of the model's thinking. Newer Claude models, including Opus 5.5 and Fable 5.1, hide this text by default. Add `"display": "summarized"` to your existing `thinking` object to request it; iCode's "Expand thinking by default" setting only chooses whether returned text shows expanded or on one line while it streams, and does not request it from the service. To hide the summary at the API level, use `"display": "omitted"`; this leaves thinking enabled and does not reduce its token charges.
- Newer Claude models reject non-default `temperature`, `top_p`, and `top_k` values, so leave them unset.
- `additional_beta_flags` and `betas` add beta flags to the request, as does an `anthropic-beta` header in "HTTP Extra Headers". iCode sends them all, after its own flags, in one `anthropic-beta` header. An `anthropic-beta` key in an `extra_headers` row replaces all of these flags, iCode's own included; iCode then adds only the flags the thinking settings need.
- With fixed-budget thinking at Anthropic's own address, iCode adds the interleaved-thinking flag itself; see [Claude thinking settings](./models.md#claude-thinking-settings). At another address, add it yourself as in the table above. Adaptive thinking already interleaves and needs no flag. Interleaving depends on the exact model, and accepting a beta flag does not mean it takes effect.
- With Claude Opus 5.5, Fable 5.1 or Sonnet 5.5 and adaptive thinking at Anthropic's own address, iCode asks the service to leave out earlier thinking that no longer matches the conversation; see [Claude thinking settings](./models.md#claude-thinking-settings). A `block_binding` you write into `thinking` yourself is sent as written, and iCode adds the beta flag it needs.

Documentation: [Prompt caching](https://platform.claude.com/docs/en/build-with-claude/prompt-caching), [Adaptive thinking](https://platform.claude.com/docs/en/build-with-claude/thinking), [Extended thinking](https://platform.claude.com/docs/en/build-with-claude/extended-thinking), [Effort](https://platform.claude.com/docs/en/build-with-claude/effort), [Beta headers](https://platform.claude.com/docs/en/api/beta-headers).

### DeepSeek

Provider "DeepSeek (OpenAI)", API style "Chat Completions", with the default base URL `https://api.deepseek.com`. Prompt caching is automatic.

| What you want | Key | Value |
| --- | --- | --- |
| Turn thinking off | `extra_body` | `{"thinking": {"type": "disabled"}}` |
| Reasoning effort | `reasoning_effort` | `high` |

The current API enables thinking by default and accepts `low`, `high`, or `max` effort. In thinking mode, `temperature`, `presence_penalty`, and `frequency_penalty` are ignored; `top_p` is effective only from 0.95 to 1.0. Older model IDs may have different capabilities.

DeepSeek also supports the Responses API. Server-side `web_search` requires Responses and a `tools` row; see DeepSeek's documentation for supported models.

| What you want | Key | Value |
| --- | --- | --- |
| Server-side web search (Responses) | `tools` | `[{"type": "web_search"}]` |

The declaration makes the provider own the `web_search` name; if the agent also enables the local `web_search`, iCode turns the local tool off for that run and shows a notice. Provider-run search is not covered by iCode approval or network settings. You can also use the local `web_search` with DeepSeek's Search API; see [Configure web tools](./web-tools.md#deepseek-search-api).

Documentation: [Thinking mode](https://api-docs.deepseek.com/guides/thinking_mode), [Context caching](https://api-docs.deepseek.com/guides/kv_cache), [API reference](https://api-docs.deepseek.com/api/create-chat-completion).

### GLM (Zhipu / Z.ai)

Provider "GLM (OpenAI)". The default base URL is `https://open.bigmodel.cn/api/paas/v4`; outside mainland China use `https://api.z.ai/api/paas/v4`, and for the GLM Coding Plan use `https://open.bigmodel.cn/api/coding/paas/v4` or `https://api.z.ai/api/coding/paas/v4`. Prompt caching is automatic.

| What you want | Key | Value |
| --- | --- | --- |
| Turn thinking off | `extra_body` | `{"thinking": {"type": "disabled"}}` |
| Keep earlier reasoning in context | `extra_body` | `{"thinking": {"type": "enabled", "clear_thinking": false}}` |
| Reasoning effort (GLM-5.2 and later) | `reasoning_effort` | `high` |

GLM-5.3 always thinks and accepts `low`, `high`, or `max` effort; do not send `thinking.type: disabled` to it. GLM-5.2 has different effort mappings, and earlier models do not support this effort field. `clear_thinking: false` also requires intact historical reasoning, which iCode's GLM provider preserves. `temperature` ranges from 0 to 1.

Documentation: [Thinking mode](https://docs.z.ai/guides/capabilities/thinking-mode), [Context caching](https://docs.z.ai/guides/capabilities/cache), [API reference](https://docs.z.ai/api-reference/llm/chat-completion).

### Kimi (Moonshot AI)

Provider "OpenAI", API style "Chat Completions", base URL `https://api.moonshot.ai/v1` (in mainland China, `https://api.moonshot.cn/v1`). Prompt caching is automatic.

| What you want | Key | Value |
| --- | --- | --- |
| Reasoning effort (Kimi K3) | `reasoning_effort` | `high` |
| Turn thinking off (Kimi K2.6) | `extra_body` | `{"thinking": {"type": "disabled"}}` |
| Longer cache lifetime (Chat Completions) | `extra_body` | `{"prompt_cache_options": {"mode": "implicit", "ttl": "1h"}}` |

Kimi K3 always thinks, accepts `low`, `high`, or `max` effort, and does not accept a `thinking` field. K2.7 Code always thinks but does not support `reasoning_effort`; K2.6 can disable thinking but also does not support that effort field. Leave sampling options unset unless the exact model documents them; fixed-value restrictions on some Kimi models are not a universal sampling contract. iCode only accepts `n` omitted or set to `1`.

Chat Completions uses a 5-minute cache by default. The 1-hour option has a higher cache-write price; changing TTL does not change the lifetime of an existing entry before it expires.

Kimi also offers an Anthropic-protocol endpoint: provider "Anthropic", base URL `https://api.moonshot.ai/anthropic`. On it, requests write to the cache only when they ask for caching, so add the `cache_control` setting from [Prompt caching](#prompt-caching).

Documentation: [Chat API](https://platform.kimi.ai/docs/api/chat), [Thinking models](https://platform.kimi.ai/docs/guide/use-thinking-models), [Context caching](https://platform.kimi.ai/docs/guide/context-caching), [Anthropic-compatible API](https://platform.kimi.ai/docs/api/messages).

### Qwen (Alibaba Cloud Model Studio)

Provider "OpenAI", API style "Chat Completions", and the OpenAI-compatible base URL from your Model Studio console. The current recommendation is a workspace-specific endpoint, such as `https://<WorkspaceId>.cn-beijing.maas.aliyuncs.com/compatible-mode/v1` (Beijing) or `https://<WorkspaceId>.ap-southeast-1.maas.aliyuncs.com/compatible-mode/v1` (Singapore); replace `<WorkspaceId>` with your workspace ID. The older `dashscope.aliyuncs.com` and `dashscope-intl.aliyuncs.com` endpoints remain available. API keys and endpoints must match the region; other regions and Coding Plan have their own endpoints. Implicit caching is automatic on supported models, with no guaranteed hit.

| What you want | Key | Value |
| --- | --- | --- |
| Turn thinking off | `extra_body` | `{"enable_thinking": false}` |
| Thinking with a budget | `extra_body` | `{"enable_thinking": true, "thinking_budget": 4096}` |
| Sampling | `extra_body` | `{"top_k": 20}` |
| Let the model search the web | `extra_body` | `{"enable_search": true}` |

`enable_thinking: false` applies to hybrid thinking models; thinking-only models cannot disable it. Budget limits, `top_k`, and `enable_search` support also vary by model. Some open-source Qwen models require "Streaming" for thinking, while commercial models can also support non-streaming requests. Model Studio also offers explicit caching via message-block markers; the Chat Options form does not add those markers.

Documentation: [OpenAI compatibility](https://www.alibabacloud.com/help/en/model-studio/compatibility-of-openai-with-dashscope), [Deep thinking](https://www.alibabacloud.com/help/en/model-studio/deep-thinking), [Context cache](https://www.alibabacloud.com/help/en/model-studio/context-cache).

### vLLM

Provider "OpenAI", API style "Chat Completions", base URL `http://<host>:<port>/v1`. For "API Key", enter the server's `--api-key` value, or leave it blank if the server doesn't use one. Prefix caching is a server setting: check `--enable-prefix-caching` / `--no-enable-prefix-caching` and support for the deployed model.

For iCode agents that use tools, start the server with `--enable-auto-tool-choice` and the `--tool-call-parser` that matches your model. Add the matching `--reasoning-parser` for reasoning models to separate reasoning from the answer.

| What you want | Key | Value |
| --- | --- | --- |
| Turn thinking off (Qwen3 and similar) | `extra_body` | `{"chat_template_kwargs": {"enable_thinking": false}}` |
| Reasoning effort | `reasoning_effort` | `low` |
| Sampling | `extra_body` | `{"top_k": 20, "min_p": 0.0, "repetition_penalty": 1.05}` |

These are model-template options, not universal switches. Some models always think; others use a different toggle. `reasoning_effort` does not provide the same effort levels on every model. Match the documentation to your installed vLLM version.

Documentation: [Tool calling](https://docs.vllm.ai/en/latest/features/tool_calling/), [Reasoning outputs](https://docs.vllm.ai/en/latest/features/reasoning_outputs/), [OpenAI-compatible server](https://docs.vllm.ai/en/latest/serving/openai_compatible_server/), [Server arguments](https://docs.vllm.ai/en/latest/cli/serve/).

### SGLang

Provider "OpenAI", API style "Chat Completions", base URL `http://<host>:<port>/v1`. Configure the tool parser for tool use and the reasoning parser for reasoning models. Prefix caching is enabled by default in ordinary server configurations, but `--disable-radix-cache` or model-specific constraints can disable it.

| What you want | Key | Value |
| --- | --- | --- |
| Turn thinking off (hybrid Qwen3 models) | `extra_body` | `{"chat_template_kwargs": {"enable_thinking": false}}` |
| Turn thinking on (DeepSeek-V3.1) | `extra_body` | `{"chat_template_kwargs": {"thinking": true}}` |
| Reasoning effort | `reasoning_effort` | `low` |
| Sampling | `extra_body` | `{"top_k": 20, "min_p": 0.0, "repetition_penalty": 1.05}` |

The template's accepted keys and effort levels depend on the model and SGLang version. A reasoning parser separates output; it does not itself enable thinking. Keep `separate_reasoning` enabled to display reasoning separately. Configure `reasoning_effort` in either the request or the server's template defaults, not both.

Documentation: [OpenAI-compatible API](https://docs.sglang.io/docs/basic_usage/openai_api_completions), [Server arguments](https://docs.sglang.io/docs/advanced_features/server_arguments), [Tool parser](https://docs.sglang.io/docs/advanced_features/tool_parser), [Reasoning parser](https://docs.sglang.io/docs/advanced_features/separate_reasoning).

### LiteLLM proxy

Provider "OpenAI", API style "Chat Completions", base URL `http://<host>:4000`, and a LiteLLM virtual key (`sk-...`) as the API key. For Claude models, you can also use provider "Anthropic" with the same base URL, which reaches LiteLLM's Anthropic-format endpoint.

| What you want | Key | Value |
| --- | --- | --- |
| Tag spend for cost tracking | `extra_body` | `{"metadata": {"tags": ["team-a"]}}` |
| Route by tag | `extra_body` | `{"tags": ["free"]}` |

Tag routing requires matching deployment tags and enabled tag filtering in the proxy; a request tag alone does not configure routing. Spend reporting also depends on the proxy's tracking setup and available features.

For unsupported OpenAI parameters, the proxy administrator can configure `drop_params`; those settings are then silently omitted. It is not a general fix for an invalid provider-specific field or value. Remove or correct those fields in the profile.

Documentation: [Virtual keys](https://docs.litellm.ai/docs/proxy/user_keys), [Cost tracking](https://docs.litellm.ai/docs/proxy/cost_tracking), [Tag routing](https://docs.litellm.ai/docs/proxy/tag_routing), [Dropping unsupported parameters](https://docs.litellm.ai/docs/completion/drop_params), [Anthropic `/v1/messages` endpoint](https://docs.litellm.ai/docs/anthropic_unified).

### OpenRouter

Provider "OpenAI", API style "Chat Completions", base URL `https://openrouter.ai/api/v1`.

| What you want | Key | Value |
| --- | --- | --- |
| Choose which providers serve the model | `extra_body` | `{"provider": {"order": ["anthropic"], "allow_fallbacks": false}}` |
| Reasoning effort | `extra_body` | `{"reasoning": {"effort": "high"}}` |
| Prompt caching for Claude models | `extra_body` | `{"cache_control": {"type": "ephemeral"}}` |

To combine these, put them in one object, for example `{"provider": {"order": ["anthropic"]}, "cache_control": {"type": "ephemeral"}}`.

`provider.order` sets preference; retain `allow_fallbacks: false` if only the listed providers should serve the request. Top-level `cache_control` is supported for Claude automatic caching, but model and provider support still matters. OpenRouter's documented behavior can differ from calling that provider directly.

Documentation: [Quickstart](https://openrouter.ai/docs/quickstart), [Provider selection](https://openrouter.ai/docs/guides/routing/provider-selection), [Reasoning tokens](https://openrouter.ai/docs/guides/best-practices/reasoning-tokens), [Prompt caching](https://openrouter.ai/docs/guides/best-practices/prompt-caching).

## If requests fail after you add an option

- **Saving shows `Chat Options row N: ...`**: fix the row the message names, then save again.
- **The error contains `unexpected keyword argument 'x'`**: move the field `x` into `extra_body`, as described in [Where service-specific fields go](#where-service-specific-fields-go).
- **The model service says it doesn't support a field**: remove only that field. If it is inside `extra_body`, delete just that member from the JSON object and keep the others. Then save.
- **The error names an environment variable**: set that variable and restart iCode, or remove the `{{...}}` placeholder.
- **The error says a command failed**: run the command from your `{{!...}}` placeholder in a terminal to see why. Once it works, type `/new` or restart iCode. See [Get an API key from a command](./models.md#get-an-api-key-from-a-command).
- **Nothing obvious**: remove recently added rows one at a time until requests work again, and check the service's documentation for the model you use.
