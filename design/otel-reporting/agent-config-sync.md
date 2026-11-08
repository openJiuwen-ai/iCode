# agent-config/1.0 三仓同步契约（iCode 特供版 · 客户端侧）

> 状态：生效中。权威规范：**`iCode-teams` 仓库 `docs/specs/data-platform/01-fact-flow-and-storage.md` §4.6**（本地检出：`C:\Users\wty\Desktop\LingXi\iCode-teams`）。
> 本文档不是第二份规范：它把权威规范中**客户端（生产方）必须遵守的部分**映射到本仓库的实现，并列出当前实现与规范的**差异清单**。三者（teams 插件、本客户端、data-server）的任何一侧改动 §4.6 相关行为时，必须同批对齐另外两侧，并更新本文的差异清单。

## 1. 目标与边界

环境复现分两份事实、两种时间语义，**分别写入**：

| 事实 | 时间语义 | 生产方职责 |
|---|---|---|
| 仓库初始状态镜像 + `.gitignore` 忽略清单 | `session_start` 冻结（首采后不重写） | `workspace.manifest` / `workspace.file` 帧（`service/reporting/workspace.py`，已实现） |
| agent 有效配置（本契约） | **采集时刻**（`capturedAt` 记录；可能在会话前，也可能后补） | `agent.env` 帧（`service/reporting/agent_config.py` + `service.py`） |

`session.meta` 帧**不得**携带 `environment{agentConfigSha256, agentConfigSize, workspaceKind}`：`agent.env` 是会话行环境三列的**唯一写入者**（§3 差异 3）。

## 2. 载荷（内容寻址 blob，即 `agent-config/1.0`）

规范化 JSON（键按字典序、无多余空白），`contentHash = sha256(规范化字节)`，与 blob 的内容寻址 sha 同源。**`capturedAt` 不进载荷**（带时间戳会破坏同配置跨会话共享）。

| 字段 | 要求 |
|---|---|
| `schema` | 恒为 `agent-config/1.0` |
| `appVersion` | 客户端版本 |
| `platform` | `<os>-<arch>`：`win32→windows`、`darwin→macos`、其余 `linux`；arch 归一 `x64/x86_64→amd64`、`aarch64/arm64→arm64`、`x86/ia32→ia32` |
| `profile` | `{name, yaml}` **恒在**；`name` 取 profile 文档的 `name:`、缺失回退会话的 profile 名；未命中即 `{name: <请求名>, yaml: ""}` 且 `sections.profile = 'absent'` |
| `model` | `{profileId, provider, modelId, params}` 四键恒在；`params` 来自模型 profile **除 `name`/`id` 外**的字段（`provider`/`model_id` 若文档里写了也留在 params） |
| `skills` | `[{name, source, digest}]`；`source ∈ config\|linked\|""`；`digest = sha256(SKILL.md 字节)`，不可读即 `""`（**不得用名字摘要冒充内容摘要**） |
| `mcp` | `{servers:[{name, transport?, connectionDigest?}]}`；`name` 恒在，派生字段无该事实时**省略**；**来源＝profile 文档的 `tools.mcp[]`**；stdio＝`sha256(规范化 JSON([command, ...args]))`，http＝`sha256(规范化 JSON([url]))` |
| `hooks` | `{count, events}`；`count`＝hook **条目数**，`events`＝去重后 `hooks[].event`（**单数**字段名）集合 |
| `settingsDigest` | hooks 文档 `settings` 段脱敏后的摘要；**无显式 settings 时省略该键**（不用 `sha256({})` 冒充） |
| `sections` | `{profile, model, skills, mcp, hooks}` 各取 `captured\|absent`（"确实没有"与"不知道"必须分开） |
| `redacted[]` | 被替换的键名（去重排序）；只有键名 |
| `truncated` | 恒为布尔；本客户端允许按既有降级策略置 `true`（teams 端恒 `false`，消费方必须容忍 `true`） |

脱敏：**与权威规范同一集合**——凭据分段（`key/token/secret/password/credential/authorization/bearer/private/cookie`）∪ 显式清单（`api_key`/`apikey`/`api-key`/`access_token`/`refresh_token`/`secret_key`/`private_key` 及 `_`/`-` 后缀变体）∪ **按分段命中的连接键**（`command/cmd/args/arguments/env/headers/httpheaders/extraheaders/url/uri/baseurl/endpoint/address/proxy`，`http_headers`/`base_url`/`proxy_url` 由 `headers`/`url` 分段命中）命中的键，**值替换为 `<redacted>`（键名保留）**，键名记入 `redacted[]`。**JSON 字符串形态的嵌套配置**（`chat_options`）：解析后按同一规则脱敏再规范化序列化，解析失败整值替换并记入 `redacted[]`。

profile 正文**规范化序列化**（键排序、去注释、统一缩进）：同一配置的不同书写方式必须得到同一 `contentHash`。

## 3. 本仓库与规范的差异清单（同步项）

状态：**第 1–7 项已在本仓库实现**（`dev/wty/built-in-upload`）；回归用例见 §4。仍保留的生产方差异列在表后。

| # | 位置 | 状态 |
|---|---|---|
| 1 | `service/reporting/service.py`（`_send_agent_environment`） | ✅ `mcp_servers` 取自 profile 文档 `tools.mcp[]` 的 `{name, transport, connectionDigest}`（摘要先于脱敏计算）；`hooks_count`/`hook_events` 取自生效 hooks 文档的条目数与 `hooks[].event`；模型标识取自会话运行时事实（`SessionReady`/`AgentRuntimeUpdated` 的 `runtime_details.model`） |
| 2 | `service/reporting/agent_config.py` | ✅ `digest = sha256(SKILL.md 字节)`（不可读为空串）；`source ∈ config\|linked\|""`（config＝`<config_dir>/skills`，linked＝`~/.agents/skills`）；`count` = 条目数 |
| 3 | `service/reporting/meta.py` | ✅ `session/1.0` 不再携带 `environment{...}`；`MetaEnvironment` 移除 |
| 4 | `service/reporting/service.py` | ✅ 帧信封携带 `workspaceKind`/`capturedAt`（`frames.FrameHeader`，仅 `agent.env` 帧）；变更检测为 contentHash **或** workspaceKind 变化即重发（unit_id 不含分类）；服务端对该重报回 `duplicate` 但把信封事实覆写进会话行三列（data-server `_refresh_agent_env_envelope`，仅当内容仍是当前锚点、`capturedAt` 不倒退） |
| 5 | `service/reporting/agent_config.py::platform_string` | ✅ arch 归一 `amd64/arm64/ia32` |
| 6 | `service/reporting/agent_config.py` | ✅ 输出 `sections{profile, model, skills, mcp, hooks}`，`captured\|absent`；事实缺席（如 restore 后未收到 runtime 事实）记 `absent` |
| 7 | `service/reporting/agent_config.py` | ✅ 分段脱敏保持；`tools.mcp` 的 command/args/url/env/headers 明文不出本机（结构级脱敏，流式写法同样命中），回归用例已签 |
| 8 | `service/reporting/agent_config.py`（载荷格式与 teams 生产方对齐，2026-11-08） | ✅ 脱敏集合补齐 `cookie` 段、显式清单与 `_`/`-` 后缀变体、连接键的 `arguments`/`httpheaders`/`extraheaders`/`baseurl` 分段；`chat_options` 等 JSON 字符串形态解析后脱敏（解析失败整值替换并记入 `redacted[]`）；`model.params` 只排除 `name`/`id`；`settingsDigest` 无显式 settings 时省略；`profile.name` 取文档 `name:`；`mcp.servers[]` 的 `transport`/`connectionDigest` 无该事实时省略（别名 `cmd`/`type`/`uri` 同样识别）；`sections.mcp` 以"声明了 `tools.mcp`"为准。用例：`test_redaction_matches_the_shared_rule_set_for_compact_and_nested_spellings`、`test_mcp_digests_cover_stdio_commands_and_http_urls`、`test_the_real_layout_reaches_the_payload_without_plaintext` |

**仍保留的生产方差异**（跨仓黄金对比时的白名单候选，非缺陷）：

- `truncated` 可为 `true`（本客户端允许降级；teams 恒 `false`，规范要求消费方容忍）；
- `skills[].source` 为空串时涵盖本仓特有的项目技能（`<cwd>/.agents/skills`）与内联 profile 技能——规范只认 `config|linked` 两类来源，空串是"本契约不为它命名位置"；
- `skills[].name` 取运行时事实（会话实际加载的技能名），teams 侧取 frontmatter `name:` 回退目录名；
- `appVersion` 恒在（本进程总有版本号），teams 侧取不到会话 `meta.app_version` 时省略该键；
- MCP 条目的补充来源：teams 生产方还会读 `<configRoot>/mcp/*.yaml`（存在的旧安装），本客户端只报运行时真正加载的 profile `tools.mcp[]`——同一安装下 teams 可能多出条目；
- `settingsDigest` 覆盖生效 hooks 文档（project+global 合并、仅显式字段）的 `settings` 段；teams 侧文档形态不同时摘要自然不同；
- 正文序列化器不同（`yaml.safe_dump(sort_keys=True)` vs teams 的 YAML 序列化器）：同一配置的 `contentHash` 不要求跨生产方相等，只要求各自逐字节稳定（规范 01 §4.6 规则 4）。

## 4. 验收（用例已落在 `tests/service/reporting/`）

1. **真实布局非空**：`test_agent_config.py::test_the_real_layout_reaches_the_payload_without_plaintext`（mcp/hooks 非空、sections 全 captured）；
2. **明文不出本机**：同上（载荷字节断言 + `redacted[]` 键名），以及 `test_agent_config.py::test_redaction_matches_the_shared_rule_set_for_compact_and_nested_spellings`（`cookie`/`apikey`/后缀写法/连接键分段/`chat_options` 的 JSON 字符串形态）；
3. **排版不敏感**：`test_agent_config.py::test_the_same_profile_written_two_ways_yields_the_same_payload`（注释/键序/风格不同 → 同一 contentHash）；
4. **信封**：`test_service.py::test_the_lifecycle_events_queue_the_documented_units`（`agent.env` 帧 `workspaceKind`/`capturedAt`；载荷不含）与 `test_meta.py::test_the_meta_document_carries_no_environment_columns`；
5. **形状与 teams 生产方一致**：`test_agent_config.py::test_mcp_digests_cover_stdio_commands_and_http_urls`（派生字段省略而非填空、`cmd`/`type` 别名、`sections.mcp` 的声明口径）与第 2 项（同一脱敏集合）；
6. **golden 跨仓一致性**（design §15.7 的开口项）：**客户端侧已与 data-server 对表（2026-11-07）**——用本仓真实配置布局产出的帧与载荷直打 data-server 的 ingest：帧解码与 `unit_id` 逐字一致、载荷入库（样本成为对方仓库的形状守卫 `tests/fixtures/agent_env_icode_agent_config_1_0.json`）、三列（含微秒级 `capturedAt`）落地、同内容重报的分类覆写生效、manifest 协商字段双边一致、旧式 `session.meta`+`environment` 被拒。与 teams 仓的对比：**逐字节不要求相等**（序列化器不同），字段级口径已按 §3-8 对齐，剩余差异即上方白名单；需重跑的开口项是"同一真实配置经两个生产方后字段逐项相等"的样本对表——样本由 teams 侧产出后再补。
