# iCode 数据上报（直改源码）方案

- 日期：2026-10-08
- 状态：定稿（两轮评审通过 + 部署形态适配 + pi-acp 参照系校准 + 六项口径决策落定），可启动 M1
- 参考工程①：`d:\Coding\aixcoding\aixcoding-continue`（上报知识库：`d:\Coding\aixcoding\aixcoding-knowledge\data-tracking-analysis`）
- 参考工程②：`d:\Coding\desktop\pi-acp`（agent_studio_new 形态下 pi 引擎的遥测实现，见 §2.2）
- 目标工程：`d:\Tech\Github\huawei\iCode\fork\iCode-fork`（下文 iCode 侧路径均相对 `src\chrys\`）
- 交付形态：**iCode TUI 独立启动** + **agent_studio_new 以 ACP 模式启动 iCode**（headless `run` 共用同一链路，顺带覆盖）

## 一、背景与关键决策

此前评估过"分析 iCode Session 文件"的采集方案，因存在绕不开的问题而放弃（其问题与本方案无关，不做交叉核对）。本方案改为**直接修改 iCode 源代码实现数据上报**。

**部署拓扑**（agent_studio_new 是 ACP 模式下用户可见的 UI，双引擎支持——同一 session 只用一种引擎）：

```
形态 A：用户 → iCode TUI（独立进程，自带审批 UI）
形态 B：用户 → agent_studio_new（Electron UI） --ACP--> iCode（ACP server）
形态 C：用户 → agent_studio_new --ACP--> pi-acp --NDJSON RPC--> pi   ← 不在本方案范围，仅作参照
```

已确认的决策（2026-10-08 全部落定）：

| # | 问题 | 决策 |
|---|---|---|
| 1 | 上报后端与协议 | **完全对齐 csas telemetry 平台**：同一后端，接口路径（`tool-detail/save` 等）与 payload 字段与 aixcoding 保持一致，便于统一聚合 |
| 2 | LLM 调用遥测通道 | **仅搭车（aixcoding 方式）**：iCode 模型请求走与 aixcoding 相同的内部模型网关，把 telemetry 塞请求体（`body.telemetry`），由网关解析落库，零额外请求。**不做** pi-acp 式的 event-log llm_call 独立上报 |
| 3 | 上报范围 | 四类：`tool-detail save+update`、`llm-call telemetry`（搭车）、`ai-code/save`、**输入触发**（单条 `tool-detail/save`，见 §4.4）。`completion-event/*` 因 iCode 无补全不适用；`event-reaction` 本期不做（属 agent_studio_new UI 层将来职责） |
| 4 | 用户身份 | iCode 将改造登录（方案同 aixcoding_continue 的 control-plane），**尚未实施**；本方案预留 userId provider 接口，登录上线后切换数据源 |
| 5 | **event-log 编码事件** | **全不纳入**。用户指示（2026-10-08）：pi-acp 的 event-log（会话/prompt 生命周期、llm_call 独立上报等）是**另外一套不成熟的实现，会被去除**——iCode 不参考、不实现该接口 |
| 6 | **工具参数口径** | **白名单 + 可切换**：默认白名单脱敏（对齐 aixcoding，默认不传参数，仅短参数放行），配置开关可切全量+截断（调试/指定环境用；全量形态对齐 pi-acp 的 value 全量/toolParam 截 2000） |
| 7 | **渠道口径（区分）** | TUI 独立启动：`channelType="cli"`、`channelName="icode-tui"`（headless 为 `"icode-cli"`）；agent_studio_new 形态：`channelType="desktop"`、`channelName/Version` = agent_studio_new 经 ACP prompt `_meta` 传的 ideName/ideVersion——具体枚举值与后端最终确认 |
| 8 | **上报分工架构** | **引擎侧为主**：iCode 引擎侧报全部引擎数据，覆盖形态 A/B 一套代码；agent_studio_new 将来报 UI 交互类（event-reaction 等），两层靠 requestId 经 `_meta` 关联（iCode 的 ACP server 回传，见 §4.5） |

新增代码目录决策：顶层 `src/chrys/aixcoding/` 为 iCode 二开定制包；**与 telemetry 无关的通用基础设施（平台连接配置、统一 HTTP 出口、git 信息、运行上下文）放 `aixcoding/` 顶层**（或其对应子目录），供 telemetry 与将来其他改造共用；**仅数据上报专属代码收敛于 `aixcoding/telemetry/` 子包**（详见 §六）。

## 二、调研结论

### 2.1 aixcoding_continue 上报体系摘要（参考系①：IDE 插件形态）

- **两个通道**：独立 HTTP 上报（reporter → `report/report.ts` 统一出口：token 头 + 全吞错 → 串行队列 → `POST {host}/csas/telemetry/api/v1/*`）；LLM 遥测搭车（`body.telemetry` 随模型请求，网关解析落库）。
- **对上游 continue 的直接修改**：约 10 处、400+ 行（`core.ts`、`llm/index.ts` 13 处注入、`callTool.ts` 7 处、GUI 侧 ~220 行等）。
- **一半触发点在 GUI**。逐项落点：工具 save→§4.2 引擎事件源（已解决）；采纳/拒绝→§4.3 审批即写入重定义（口径待 §8-3 确认）；diff 行数→§4.2 字段映射（SnapshotStore+difflib）；event-reaction→agent_studio_new UI 层将来职责（决策 #8）；输入触发→§4.4 已纳入。
- 值得照抄的工程实践：公共字段收口在统一出口层；per-toolUseId 串行保序；参数上传白名单；上报失败只记日志绝不抛出。

### 2.2 pi-acp 遥测实现摘要（参考系②：agent_studio_new 形态）

pi-acp 是 pi 与 ACP 客户端之间的适配层进程（agent_studio_new spawn 它，它 spawn pi），遥测寄生在消息流翻译层（`src/acp/agent.ts` 处理 ACP 请求、`src/acp/session.ts` 翻译 pi RPC 事件），对 pi 零侵入。

**可借鉴（本方案已吸收）：**

| 要点 | pi-acp 做法 | iCode 吸收方式 |
|---|---|---|
| ID 体系 | 每轮 prompt 一个 requestId+根 spanId（uuid）；**每次 LLM 调用独立 requestId/spanId**（parent=根 span）；工具 `parentSpanId`=发起它的那次 LLM 调用的 span、requestId 优先 llmCallRequestId | §4.1 ID 体系升级为 per-LLM-call 粒度（iCode 引擎内数据支持精确实现，优于原 session 级设计） |
| 输入触发 | `recordInvocation(funcType, name)` → **单条** `tool-detail/save`（saveOnly），funcType：0=skill/1=MCP/2=命令/3=内置；**不用 batch-save 接口** | §4.4 照搬：单条 save + funcType 枚举 |
| channel 口径 | `channelType='desktop'`；tool-detail 的 `channelName/Version`=prompt `_meta` 传的 ideName/ideVersion；`agentName`=functionName（`_meta`） | 决策 #7 直接采用 |
| `_meta` 双向关联 | prompt 响应回传 `_meta['agent-studio.dev/telemetry']`（requestId/spanId）+ 每 session/update 注入；下行读 `_meta` 的 `agent-studio.dev/ide-name/ide-version/function-name` 三个 envelope | §4.5：iCode ACP server 做同样回传（agent_studio_new 已有消费方）与下行读取 |
| 发送机制 | 缓冲批量（batchSize=20/10s）+ tool-detail 即时 save→update promise 链保序 + 吞错 + 缓冲上限 500 丢旧 + 未配置零开销 | 混合采用：tool-detail 即时保序、ai-code 批量缓冲 |
| git 采集 | session 创建时 spawnSync 采一次缓存，失败静默置 null | `git_info.py` 同策略（+TTL 缓存） |
| 工具行数 | `tool-line-counts.ts`：edit patch / write 内容行数统计 | §4.2/§4.3 同需求，数据源 SnapshotStore 更全 |

**不采纳（含用户明确指示）：**

- **event-log 接口体系（save/update）全不采纳**：用户指示 2026-10-08——"那是另外一套不成熟的实现，会被去除"。iCode 不实现 `event-log/save|update`，llm-call 只走搭车通道（决策 #2/#5）。pi-acp 现报的 session_create/load、prompt started/completed/cancel、llm_call（独立 HTTP）、model_change、thinking_change、error 等事件类别均不在 iCode 范围。
- 参数全量上报为默认（iCode 默认白名单，全量仅作开关，决策 #6）。
- `tool-detail/batch-save` 接口（pi-acp 未用，iCode 也用单条 save）。

### 2.3 iCode 架构关键事实（可行性依据）

| # | 事实 | 位置 |
|---|---|---|
| 1 | TUI / ACP / headless `run` 三模式共享进程引导与引擎装配 | `orchestration/startup.py::bootstrap_runtime`、`orchestration/engine/assembly.py::assemble_agent_engine` |
| 2 | EventBus 是前后端唯一事件通道，106+ 事件类型；`stream()` 为异步迭代（不阻塞发布方） | `foundation/events/bus.py:29`、`foundation/events/types.py` |
| 3 | 工具调用统一事件源：`ToolEventMiddleware` 在审批后/执行前发 `InvocationToolCallStart`、执行后发 `InvocationToolCallResult` | `service/agent_middleware/events/tool_events.py:391,689` |
| 4 | ACP 与 TUI 同源：`AcpEventBridge` 投影同一 EventBus；审批 `ApprovalRequest`→`session/request_permission`、`ApprovalResponse` 发布回引擎总线——**agent_studio_new 的批准/拒绝最终也汇入 iCode 进程内同一 EventBus** | `app/acp/bridge.py:212-402`、`app/acp/server.py:740-744,1403-1438,1504-1510` |
| 5 | LLM 客户端唯一组栈点，覆盖全部 provider 与全部调用方 | `service/llm/clients.py::create_client` → `service/llm/instrumented.py::_compose_client_stack:873-891`（工厂调用点 `:988/1051/1123`） |
| 6 | `ChatMiddlewareLayer` 支持构造器 middleware；`ChatContext.options`/`kwargs` 透传到 wire client；但 **`ChatContext` 无 session 字段**，`session_id` 是工厂构造参数（`_prepare_options` 闭包 stamp 进 HTTP 头），middleware 层不可见——须经 `_compose_client_stack` 显式传入（与 `UsageTrackingMiddleware` 构造注入 `on_usage` 的既有惯例一致） | `kernel/middleware.py:182-259,520-531,594-614`、`service/llm/instrumented.py:936-944`、`service/context/middleware/usage.py:57-71` |
| 7 | OpenAI/Anthropic SDK 均支持 `extra_body`（merge 进 body 顶层）；透传点 `openai_chat_completion.py:854-856`（排除集仅 `instructions/tools/conversation_id`） | `service/llm/openai_chat_completion.py:693-699,774-779,841-924` |
| 8 | 系统 side call（judge/标题/last-words）有现成标记 `in_internal_side_call` | `service/llm/instrumented.py:83,540-551` |
| 9 | 文件改动账本：`FileMutationTextSnapshot`(before/after 全文) 等；摘要键进 `InvocationToolCallResult.metadata` | `service/mutations/types.py:290-475`、`tool_events.py:613-622` |
| 10 | 事件基类带 `session_id`；`InvocationEvent.origin` 带 `invocation_id`（主 agent 每 turn 一个） | `foundation/events/types.py:52-94` |
| 11 | **审批归因判据**：`ApprovalDecider.USER vs JUDGE`（judge 批准直接 set_result 不经事件，以 `resolved_by_event` 区分）——"用户采纳 vs 自动采纳"的现成判据；五态 status：`approval_pending/user_approved/user_rejected/auto_approved/bypass_approved` | `service/agent_middleware/control/approval.py:353,374,403,576-582`、`service/session/history.py:1232-1352` |
| 12 | **approval mode 真值源**：`ApprovalModeUpdated` 在每次切换后 + session 就绪时都发布——subscriber 订阅此事件即可重建任意时刻 MANUAL/AUTO/BYPASS | `orchestration/engine/state/controls.py:188`、`orchestration/engine/loader.py:465-467` |
| 13 | **TUI 审批对话框**：仅 Approve/Decline + 可选 reason（无分级）；`modified_args` 扩展点在但无编辑 UI | `app/tui/screens/dialogs/approval/dialog.py:61-62,392-412` |
| 14 | slash skill 引用在**引擎层**解析（`parse_skill_reference`）——输入触发插桩点，TUI/ACP 双形态覆盖（agent_studio_new 会在 UI 层把 slash 改写成 `/skill:<id>` 后发给引擎） | `orchestration/engine/run/input_refs.py:26` |
| 15 | `current_trajectory()` contextvar 含 `session_id/turn_id/actor`，spanId 数据源现成（middleware 执行期绑定待 M1 验证） | `foundation/trajectory/context.py:180` |
| 16 | agent_studio_new 与 ACP agent 间已有通用 `_meta` envelope（`agent-studio.dev/ide-name/ide-version/function-name`，随 prompt 下发；pi-acp 消费中） | pi-acp `src/acp/agent.ts:68-110` |

## 三、总体可行性结论

**可行，且条件优于两个参考系**：

1. 5 处源码注入即覆盖全部交付形态（对比 aixcoding 的 10 处 400+ 行；pi-acp 需独立适配层进程，iCode 引擎内实现省掉最别扭的"向子进程注入遥测上下文"隐藏命令）。
2. tool-detail / ai-code / 输入触发的数据全部可从 EventBus 事件流 + 引擎层解析点获得，**零内核改动**。
3. LLM 搭车通道有现成注入口（构造器 middleware + `extra_body`），一处覆盖全部 provider。
4. **agent_studio_new 形态无额外成本**：审批决策经 ACP 回流引擎 EventBus（事实 #4），channel 身份读 `_meta`（事实 #16），一套代码覆盖形态 A/B。

预计源码修改 5 处、合计 ~27 行（§五），其余全部在 `src/chrys/aixcoding/` 包——追上游版本负担极小。

## 四、四类上报实现设计

### 4.1 llm-call telemetry（搭车通道）+ ID 体系

| 项 | 设计 |
|---|---|
| 注入点 | `_compose_client_stack` 加 `session_id` 形参后改为 `ChatMiddlewareLayer(chat_client, middleware=[AixTelemetryMiddleware(session_id=session_id)])`（3 个工厂调用点把已有 `session_id` 传下去；`ChatContext` 无 session 字段，必须显式传参——详见事实 #6） |
| 请求体 | middleware 在 `context.options` 注入 `extra_body={"telemetry": {...}}`，SDK merge 进 body 顶层；wire 形态用 `CHRYS_DEBUG_LLM_RAW_HTTP_LOG` 实测核对 |
| payload 字段（对齐 aixcoding `getTelemetryData`） | `requestId`、`sessionId`、`spanId`、`parentSpanId`、`eventType:"llm"`、`eventSubType`、`channelType/channelName/channelVersion`、`pluginVersion`(iCode 版本)、`projectName`(cwd basename)、git 五件套（`git_info.py`） |
| **ID 体系（per-LLM-call，对齐 pi-acp 口径）** | 轮次根 span：`uuid5(NAMESPACE, f"{session_id}:{turn_id}")`（确定性：同轮次恒同值，`turn_id` 取 `current_trajectory()`）；**每次主对话 LLM 调用独立 `requestId`(uuid4)**，telemetry 的 `spanId`=轮次根 span、`parentSpanId`=空（或上层）；side call 不参与关联 |
| requestId 关联（工具侧） | middleware 提取 function calls 的 **provider call id**（模型返回的原始 tool_use id）写进程内 registry：`dict[provider_call_id → (requestId, 根spanId)]`——**提取时机分两路**：非流式在 `call_next()` 后直接读 `context.result`(ChatResponse)；**流式（生产默认）下 `call_next()` 返回时 `context.result` 是未消费的 ResponseStream，须走 `stream_result_hooks`/`with_result_hook` 在流终结后提取**（`UsageTrackingMiddleware` 同款模式，`usage.py:96-132`）。时序安全：流终结→提取写 registry→工具才执行→Start 事件发布时 registry 已就绪。工具上报按事件的 `provider_call_id` 字段反查——**精确关联到发起该工具的那次模型调用**（pi-acp 口径），且天然消解 sub-agent 覆盖问题。**id 双体系（已查实，见 §4.2 缺口②）**：middleware 所见响应 id = provider 原始 id（`metadata["call_id"]` 字面量，`loop.py:2007` 写入）；事件 `call_id` 字段 = Chrys 12 位短 id（`_chrys_call_id`）——**两套 id 不同源，事件须补填 `provider_call_id`（§5 #5 必选）方能反查**。fallback：反查不到（取消/边界）时退化为 session 级最新值 |
| eventSubType | 主对话循环 → `agent`；side call（judge/标题/last-words，判 `in_internal_side_call`）→ `system`——对齐 aixcoding "仅主对话通道"门禁语义 |

### 4.2 tool-detail/save + update（独立 HTTP 通道）

| 项 | 设计 |
|---|---|
| 订阅方式 | `bus.stream(InvocationToolCallStart, InvocationToolCallResult, ApprovalModeUpdated, ...)`——不阻塞发布方；tool-detail 即时入串行 HTTP 队列（save→update promise 链保序，对齐 pi-acp/aixcoding 共同语义） |
| 上报时机 | Start（审批后、执行前，`tool_events.py:391`）→ `save` + `update(PENDING=3)`；Result → `update(SUCCESS=1 / FAILED=2)` |
| 字段映射 | `funcId←call_id`、`funcName←tool_name`、`funcType←tool_kind` 映射表（内置→3 / MCP→1 / skill→0，对齐 pi-acp 枚举）、`sessionId←event.session_id`、`spanId←origin.invocation_id`、`requestId←registry（按 provider_call_id 反查，fallback session 级）`、`status/funcErrorMessage`、`executionDurationMs←duration_ms`、`executionStartedAt/finishedAt←timestamp`、写类工具补 `originalLines/addedLines/deletedLines←SnapshotStore difflib`（随终态 update 上报） |
| 参数口径（决策 #6） | **默认白名单**：不传工具参数，仅 `read_file.filepath` 等短参数放行（名单对齐 aixcoding `toolUseReports.ts:25-45` 按 iCode 工具集调整）；**配置可切全量**（value 全量 JSON、toolParam 截 2000 字符，对齐 pi-acp，仅调试/指定环境） |
| 失败分类 | 按 metadata 键还原（`TOOL_ERRORED/TOOL_ERROR_KIND/SHELL_TIMED_OUT`/rejection）；复用 `service/trajectory/tools.py:62-84` 的 `tool_outcome()` 逻辑；`failureType`: timeout/cancelled/error |
| 公共字段收口 | 照抄 aixcoding `buildToolDetailSaveItem`：reporter 只给业务字段，sessionId/userId/git 五件套/gitOwner/gitRepo/channel 三元组/pluginVersion 在统一出口层自动补 |
| 已知缺口 | ① `CancelledError` 不发布 Result（`tool_events.py:509-514`）→ "有 save 无 update"孤儿，见 §8-4；② **id 双体系（已查实）**：事件 `call_id`（Chrys 12 位短 hex，`get_call_id` 读 `_chrys_call_id`）≠ middleware 所见响应 id（provider 原始 id，`metadata["call_id"]` 字面量，`get_provider_call_id` 读它——`hook_dispatch.py:187-202`、`_metadata_keys.py:35`）；且主 agent 路径发布 Start/Result 时 `provider_call_id` 字段未填（`tool_events.py:391-399` 只传 `call_id`，值在 `_process_tool_call:285` 已取得但只进 `_on_start_published` 回调）→ **§5 #5 补填为必选**，registry 关联键统一用 `provider_call_id`；`funcId` 仍用事件 `call_id`（工具自身标识，save/update 关联用，与 requestId 关联键无关） |

### 4.3 ai-code/save

| 项 | 设计 |
|---|---|
| 数据源 | `write_file`/`edit_file`（`_FILE_TOOLS`；shell 隐式写不报）。`args` 即完整生成代码；before/after 全文从 SnapshotStore 取，`difflib` 算 `blocks[{snippet, rangeStart, rangeEnd}]` |
| payload | `reportId`(uuid)、`filepath`(git 相对路径)、`blocks`、`sourceType:"edit"`、`remoteUrl/branch/gitUserName/gitUserEmail` |
| 采纳语义（codeStatus，基于事实 #11/#12 升级） | iCode 是**审批即写入**：`user_approved`（decider=USER，用户看过 diff 预览）→ `5`；`user_rejected` → `4`；`judge 批准/auto_approved（安全只读静默放行）/bypass_approved` → `1`；mode 真值源：订阅 `ApprovalModeUpdated` 流。此映射需产品侧确认（§8-3），机制上 ACP/TUI 同源一处实现 |
| 触发时机 | 工具执行成功（Result 无错误且带 file mutation）后上报，批量缓冲（对齐 pi-acp batchSize 机制） |

### 4.4 输入触发（新增，对齐 pi-acp `recordInvocation`）

| 项 | 设计 |
|---|---|
| 实现 | 引擎层 slash skill 引用解析成功时（`parse_skill_reference` 命中）→ **单条 `tool-detail/save`**（saveOnly，不更新）——不用 batch-save 接口 |
| funcType | `0`（skill）；MCP 工具/内置工具自然走 §4.2 的 save，无需重复 |
| 字段 | `funcName←skill 名`、`sessionId←当前会话`、`requestId←session 级最新`、公共字段自动补 |
| 粒度边界 | 引擎可见的是**最终进入引擎的 skill 引用**（agent_studio_new 在 UI 层改写成 `/skill:<id>` 后下发，TUI 直接透传）；UI 层原始交互细节（敲 `/` 弹候选、选择过程）引擎不可见，属 agent_studio_new 侧数据（已确认此粒度足够） |

### 4.5 ACP `_meta` 双向集成（agent_studio_new 形态适配）

| 方向 | 设计 |
|---|---|
| 下行读 | iCode ACP server 读 prompt `_meta` 的 `agent-studio.dev/ide-name/ide-version/function-name` envelope（agent_studio_new 已下发的通用机制，pi-acp 消费中）→ 填 `channelName/channelVersion/agentName`（决策 #7） |
| 上行回传 | prompt 响应与 session/update 注入 `_meta['agent-studio.dev/telemetry']`（requestId/spanId，key 对齐 pi-acp）——agent_studio_new 将来报 UI 交互类数据时用同一 request_id 关联，两层数据可 join（决策 #8） |
| 挂点 | `app/acp/server.py` prompt 处理 + `bridge.py` 投影处（§5 修改点 #4） |

## 五、源码修改清单（追版本的唯一成本）

| # | 文件 | 修改 | 性质 | 预估 |
|---|---|---|---|---|
| 1 | `service/llm/instrumented.py` | import telemetry 包；`_compose_client_stack` 加 `session_id` 形参并注入 `AixTelemetryMiddleware(session_id=...)`；3 个工厂调用点传参 | 插入 + 既有调用加实参 | ~8 行 |
| 2 | `orchestration/engine/assembly.py` | `assemble_agent_engine` 里调 `subscriber.attach(event_bus, ...)`（per-bus 幂等） | 纯插入 | ~3 行 |
| 3 | `orchestration/engine/run/`（input_refs 调用方） | skill 引用解析命中时调 `reporters.record_invocation(...)`（不改纯函数本体） | 纯插入 | ~3 行 |
| 4 | `app/acp/server.py`（+`bridge.py`） | 读 prompt `_meta` envelope 存 channel context；prompt 响应回传 telemetry `_meta` | 纯插入 | ~8 行 |
| 5 | `service/agent_middleware/events/tool_events.py` | 发布 `InvocationToolCallStart`（L391-399）与 `InvocationToolCallResult`（L689）时补 `provider_call_id=provider_call_id`（值在 L285 已取得）——per-call registry 关联键，**必选** | 既有构造调用加参数 | 2-4 行 |

**合计 5 个文件、~27 行，不修改任何既有逻辑行**——全部为插入调用或给既有调用加参数，不改变上游代码的行为语义。冲突风险分级：#2/#3/#4 纯插入，上游 rebase 几乎不冲突；#1/#5 给既有行加实参，仅当上游恰好重构这几个函数（`_compose_client_stack`、事件发布构造）时产生小冲突，语义简单、解决成本低。上游更新后的破坏点探测靠哨兵测试（§8-7）。对比参考系：aixcoding 对 continue 为 10 处、400+ 行且散布全文件（§2.1）。

## 六、新增代码组织：`src/chrys/aixcoding/` + `telemetry/` 子包

**划分原则：telemetry 专属的才放 `telemetry/` 下；通用基础设施放 `aixcoding/` 顶层，供本次上报与将来登录等其他改造共用。**

```
src/chrys/aixcoding/              # iCode 二开定制顶层包（将来登录等其他改造在此建子目录）
├── __init__.py
├── config.py                     # [通用] 平台连接配置：profile(LOCAL/DEV/PROD)→URL 映射、customServerUrl、
│                                 #   token 来源；独立读 ~/.chrys/aixcoding.yaml + 环境变量
├── http.py                       # [通用] 统一 HTTP 出口：token 请求头、fire-and-forget 全吞错、
│                                 #   串行队列 + 批量缓冲（对齐 aixcoding report.ts/request.ts 与 pi-acp collector）
├── git_info.py                   # [通用] git 五件套 + gitOwner/gitRepo（subprocess git + TTL 缓存；
│                                 #   remote 选取优先级对齐 aixcoding：cnb > origin > 第一个）
├── context.py                    # [通用] 运行上下文：channel 三元组（argv 识别 tui/acp/cli + _meta 注入
│                                 #   ideName 覆盖）、userId provider 接口（当前配置文件实现）
└── telemetry/                    # ★ 仅数据上报专属
    ├── __init__.py
    ├── llm_telemetry.py          # AixTelemetryMiddleware（搭车 + per-call registry）
    ├── subscriber.py             # bus.stream() 订阅 → 分发 reporters；attach() 幂等装配入口
    ├── outcome.py                # 工具失败分类（复用 trajectory tool_outcome 逻辑）
    ├── reporters/
    │   ├── __init__.py
    │   ├── tool_detail.py        # tool-detail/save + update 组装（含 record_invocation 输入触发）
    │   └── ai_code.py            # ai-code/save 组装（blocks diff 计算）
    └── types.py                  # payload 结构、csas 端点常量、枚举
```

- **userId 登录预留**：`context.py` 定义 provider 接口；登录落地时新增 `aixcoding/auth/` 子包注册真实数据源，接口不变。
- **分层约束**：包内只 import `kernel`/`foundation` + 标准库，仅被 §5 各点引用；`tests/architecture/` 依赖方向断言以 Smart Test 验证。
- **配置独立读取是刻意取舍**；telemetry 专属开关（参数白名单/全量切换、`CHRYS_AIXCODING_TELEMETRY_DEBUG`）由 telemetry 包在 `config.py` 之上读取。
- **上报失败只记日志**，无用户可见文案，不涉及 i18n。
- **开发期工具（mock server）放根目录 `aixcoding/telemetry-mock/`**（fork 二开定制专区 `aixcoding/` 内——内部文档/开发工具的统一归属，不进 wheel；2026-10-08 用户确认由顶层 `telemetry-mock/` 收敛至此）：模拟 csas 上报后端，从 agent_studio_new 的 `apps/telemetry-mock`（TS）**重写为 Python 标准库零依赖版**（`http.server` + `sqlite3`；不进 `src/chrys`——hatch wheel 只打包 `src/chrys`，mock 绝不进发行物；不进 pyproject 依赖组）。结构 `server.py / store.py / faults.py / README.md`，**`server.py` 暴露可 import 的 `start_telemetry_mock(options)` factory、`__main__` 为 CLI 入口**（对齐原版 index.ts/server.ts 分离——pytest 可进程内驱动也可子进程启动，M2 自动化复用的前提），`uv run python aixcoding/telemetry-mock/server.py` 启动（loopback-only，默认 :4321，环境变量 `CHRYS_TELEMETRY_MOCK_PORT/TOKEN`）。继承原版能力：**全部 5 个 csas 端点**（tool-detail/save、batch-save、update、ai-code/save、event-reaction/save）+ token 401——**mock 对齐真实后端接口面而非本方案上报范围**（batch-save/event-reaction 虽不在 iCode 上报范围（决策 #3/#5），保留可让 agent_studio_new 侧将来报 event-reaction 时同一 mock 可用；用户决策 2026-10-08：维持 5 端点不砍）；SQLite 每接口一表（`body_json` 原文 + rejected 表）；**观测端点全套 6 个**：`/health`、`/debug/view`（**HTML 观察页**：列表 + sessionId 过滤 + 展开原文 + 2s 自动刷新——"观测能力"的落点，原版即手写内联 HTML/JS 零依赖，Python 版同法）、`/debug/reports`、`/debug/dump`、`/debug/clear`、`/debug/faults`（**运行期切换故障模式**，吞错链路验证依赖）；**故障注入** 5 模式（http500/http503/slow/envelope_reject/drop_body，按 rate/interface 定向）；清理 rev.5 残留列（`turn_content_hash`/`analysis_version`）。选 Python 的决定性理由：**可被 pytest 集成测试复用**（测试内起 mock → 跑引擎 → 断言 SQLite 落库，M2 入库核对自动化）。注意：mock 只覆盖独立 HTTP 通道，**llm-call 搭车通道的报文发往模型网关 mock 收不到**——其开发期观测靠 `CHRYS_DEBUG_LLM_RAW_HTTP_LOG` 与 debug 落盘。`config.py` 的 LOCAL profile URL 指向 mock 即对接。

## 七、配置与用户身份

| 项 | 设计 |
|---|---|
| profile 判定 | `~/.chrys/aixcoding.yaml` 的 `profile` → `AIXCODING_EXTENSION_PROFILE` 环境变量 → 默认 `PROD` |
| URL 映射 | LOCAL `http://localhost:7777` / DEV `http://81.89.182.150/csas` / PROD `http://22.189.54.139/csas`；环境变量可覆盖；`customServerUrl` host 级覆盖 |
| 认证 | `token` 请求头（非 Bearer）；登录前配置文件读，登录后切 control-plane accessToken（provider 接口） |
| userId | 当前 `~/.chrys/aixcoding.yaml` 指定（如工号），登录上线后换 `account.id` |
| 上报开关 | 总开关（默认开，可环境变量关闭；未配置 URL 时零开销——对齐 pi-acp） |
| **参数模式** | `toolParamMode: whitelist | full`（默认 whitelist，决策 #6） |

## 八、风险与待确认项

1. **channelType 枚举值（需后端确认）**：决策 #7 已定方向（cli/icode-tui + desktop/ideName），具体枚举值需后端最终确认（`cli`、`desktop` 是否为合法值；aixcoding 用 `ide`、pi-acp 用 `desktop`——后端字段枚举现状待澄清）。
2. **网关解析 telemetry 需实测**：M1 发请求查库验证（aixcoding 线上该通道已验证无丢失）。
3. **codeStatus 语义映射（需产品侧确认）**：§4.3 的五态映射（user_approved=5 / user_rejected=4 / judge+auto+bypass=1）。注：pi-acp 未上报 codeStatus（接口文档注明"待后端明确语义"）——iCode 是首个填充该字段的引擎形态，映射定稿需产品/后端共同确认。
4. **取消的工具调用**：`CancelledError` 不发 Result → 孤儿 save。对策：订阅中断/turn 结束事件补发 `update(failureType=cancelled)`，或容忍留 PENDING。
5. **middleware session 标识缺口（已闭环）+ 残余验证点**：sessionId 构造传参已定（§4.1）；残余：① `current_trajectory()` 在 middleware 执行期绑定验证（spanId 来源，引擎文档称 "binds it for the duration of a model run"）；② ~~call_id 同源性待验证~~ 已查实**不同源**（事件 `call_id`=Chrys 短 id ≠ middleware 响应 id=provider 原始 id）——§5-#5 补填升为必选、registry 键统一 `provider_call_id`，M1 验证补填后贯通；③ 后端 spanId 口径。
6. **字段超集**：iCode 报文与 aixcoding 的字段差异（无 `userStoryId` 等）——后端容忍度需确认。
7. **上游内部契约漂移**：为依赖的缝配"哨兵测试"（ChatMiddlewareLayer 构造参数、事件字段、metadata 键、mutation 摘要键、`_meta` envelope 名）。
8. ~~sub-agent registry 覆盖~~：**已随 per-LLM-call registry（§4.1）自然消解**——每个 call_id 独立映射，无覆盖竞争；sub-agent 内工具天然关联 sub-agent 那次 LLM 调用的 requestId（语义正确）。保留一条 M1 验证用例确认落库归属即可。

## 九、实施顺序

| 里程碑 | 内容 | 验证 |
|---|---|---|
| M1 | `aixcoding/` 包骨架（config/http/git_info/context + `telemetry/types`）+ `instrumented.py` 注入（含 `session_id` 形参）+ llm-call 搭车 + per-call registry + **`telemetry-mock/`（Python 重写，含故障注入）** | mock 起停/5 端点/观测/故障注入自测通过；TUI/ACP 各发一条模型请求，网关查库核对；`current_trajectory()` middleware 绑定验证；补填 `provider_call_id` 后 registry 贯通验证（含流式链路：`stream_result_hooks` 终结后提取、工具 Start 前 registry 就绪）；带 sub-agent 用例的关联归属；**ACP 形态审批用例（`resolved_by_event` 在 agent_studio_new 回流下为 True，确认 decider=USER）**；后端 spanId/channelType 枚举确认 |
| M2 | subscriber + tool_detail reporter（含输入触发 record_invocation）+ `assembly.py`/`input_refs` 装配 + ACP `_meta` 双向集成 | 成功/异常/超时/审批拒绝四形态 + 时长/错误分类/行数正确；skill 引用触发入库；`_meta` 回传被 agent_studio_new 收到；**mock 上入库核对 + 故障注入下的吞错行为**（后续可自动化为 pytest 集成测试） |
| M3 | ai_code reporter（blocks 计算 + codeStatus 五态映射定稿） | write_file/edit_file 入库核对；审批批准/拒绝两路径 codeStatus 正确 |
| M4 | 登录对接、哨兵测试、文档归档 | Smart Test + 契约测试通过 |

## 附录 A：iCode 侧关键代码位置速查

| 用途 | 位置 |
|---|---|
| LLM 组栈唯一注入点 | `service/llm/instrumented.py:873-891`（`_compose_client_stack`）；工厂调用点 `:988/1051/1123` |
| ChatMiddleware 接口 / options 透传 / ChatContext 无 session 字段 | `kernel/middleware.py:182-259,520-531,594-614` |
| middleware 构造注入惯例 / 流式结果钩子模式 | `service/context/middleware/usage.py:57-71,96-132`（构造注入 `on_usage`；流式 `with_result_hook` 终结后捕获——telemetry 提取 function calls 同款模式） |
| `extra_body` 透传（OpenAI chat） | `service/llm/openai_chat_completion.py:854-856`（排除集不含 `extra_body`；L843-848 为 `n=1` 校验） |
| anthropic 组装/SDK 调用 | `service/llm/anthropic_chat.py:486,501,544,569-583` |
| side call 标记 | `service/llm/instrumented.py:83,540-551`（`in_internal_side_call`） |
| 工具事件发布 | `service/agent_middleware/events/tool_events.py`（Start L391-399、Result L689-700、metadata 合并 L613-643、拒绝 L623-624、取消 L509-514） |
| **id 双体系：provider 原始 id vs Chrys 短 id** | 读写函数 `service/agent_middleware/events/hook_dispatch.py:187-208`（`get_call_id` 读 `_chrys_call_id`、`get_provider_call_id` 读字面量 `"call_id"`、`set_call_id` 写短 id）；key 常量 `service/agent_middleware/_metadata_keys.py:35`；kernel 写入点 `kernel/loop.py:2007`；事件发布未传 provider_call_id：`tool_events.py:285,391-399` |
| 失败 outcome 现成映射 | `service/trajectory/tools.py:62-84`（`tool_outcome`） |
| 事件类型定义 | `foundation/events/types.py`（`InvocationToolCallStart:401`、`Result:468`、`ApprovalRequest:665`、`ApprovalResponse:226`、`ApprovalModeUpdated:711`、`UsageUpdate:824`） |
| 审批中间件 / decider 归因 / 五态 status | `service/agent_middleware/control/approval.py:353,374,403,465,576-582,640-652`；`service/session/history.py:1232-1352` |
| approval mode 真值源 | `orchestration/engine/state/controls.py:156-188`（`ApprovalModeUpdated`）；`orchestration/engine/loader.py:465-467` |
| TUI 审批对话框 / diff 预览 | `app/tui/screens/dialogs/approval/dialog.py:61-62,392-412`、`bodies/file_edit.py:176-206` |
| ACP 事件桥/审批往返（`_meta` 集成挂点） | `app/acp/bridge.py:212-402`、`app/acp/server.py:740-744,1403-1438,1504-1510` |
| slash skill 引用解析（输入触发挂点） | `orchestration/engine/run/input_refs.py:26`（`parse_skill_reference`） |
| mutations 数据结构 | `service/mutations/types.py`（`FileMutation:362`、`FileSnapshot:290`、`FileMutationTextSnapshot:190`） |
| 写类工具清单 | `service/mutations/tool_names.py`（`_FILE_TOOLS`）；`service/tools/builtins/filesystem.py`（`write_file:634`、`edit_file:860`） |
| 引擎装配（EventBus 可达点） | `orchestration/engine/assembly.py:56-236`；bus 创建：`orchestration/session_host.py:326`、`app/tui/app.py:1227` |
| 模型 profile（base_url 可指网关） | `service/profiles/models/schema.py:25-52`；`service/llm/clients.py:250-269` |
| trajectory contextvar（spanId 来源） | `foundation/trajectory/context.py:180`（`current_trajectory()`） |
| wire 请求体实测工具 | `service/llm/raw_http_log.py`（`CHRYS_DEBUG_LLM_RAW_HTTP_LOG`） |

## 附录 B：csas 接口与字段对照（本次四类）

| 接口 | 时机 | 核心字段 → iCode 来源 |
|---|---|---|
| `telemetry/api/v1/tool-detail/save` | 工具 Start（执行前）/ 输入触发命中 | `funcId←call_id`、`funcName←tool_name/skill 名`、`funcType←tool_kind 映射(0/1/3)`、`value←args 白名单(可切全量)`、`sessionId/spanId←事件`、`requestId←per-call registry`、`userId/git*/channel*/pluginVersion/projectName←统一出口补齐` |
| `telemetry/api/v1/tool-detail/update` | Result / 兜底 | `toolUseId←call_id`、`codeStatus←五态映射(§4.3)`、`executionDurationMs←duration_ms`、`executionStartedAt/finishedAt←timestamp`、`originalLines/addedLines/deletedLines←SnapshotStore difflib（写类工具）`、`failureType←outcome(timeout/cancelled/error)`、`toolErrorMessage←result 错误文本` |
| `ai-code/save` | 写类工具成功后 | `reportId←uuid`、`filepath←git 相对路径`、`blocks←SnapshotStore difflib`、`sourceType="edit"`、git 四字段←`git_info` |
| （无独立接口）llm-call | 每次模型请求 | `body.telemetry`：`requestId←每次调用 uuid4`、`sessionId←middleware 构造传参`、`spanId←uuid5(session_id:turn_id)`、`eventType="llm"`、`eventSubType←side_call 判定(agent/system)`、`channel*(决策 #7)/pluginVersion/projectName`、git 五件套←`git_info` |

> 明确排除：`event-log/save|update`（pi-acp 现有但不成熟、将被去除——用户指示 2026-10-08，已记录于 §2.2）；`tool-detail/batch-save`（输入触发用单条 save）；`completion-event/*`（无补全）；`event-reaction/*`（agent_studio_new UI 层将来职责）。

## 附录 C：aixcoding_continue 参考实现位置速查

| 用途 | 位置（`core/aixcoding/` 下） |
|---|---|
| 统一出口（token 头/吞错） | `report/report.ts:11-15,46-95` |
| 串行 HTTP 队列 | `network/request.ts:89-119` |
| per-toolUseId 保序 | `reporters/toolCallReporter.ts:19-40` |
| 公共字段收口 | `reporters/toolUseReporter/common.ts:104-120` |
| 参数白名单 | `reporters/toolUseReporter/toolUseReports.ts:25-45,73-78` |
| telemetry 组装（搭车） | 上游 `core/llm/index.ts:1782-1826`（`getTelemetryData`），注入点 13 处 |
| LLM 报文结构 | `types/tool-use-types.ts:62-103`、`types/report-message-types.ts:88-157` |
| 端点常量 / profile / URL | `types/consts.ts:14-41`、`utils/configUtils.ts:38-117`、`network/webConfigs.ts:15-40,54-85` |
| userId 缓存（control-plane） | `session/SessionInfoCache.ts:20-46` |

## 附录 D：pi-acp 参考实现位置速查（`d:\Coding\desktop\pi-acp`）

| 用途 | 位置 |
|---|---|
| 遥测核心（collector/缓冲/批量/吞错） | `src/acp/telemetry.ts`（597 行；端点 `:419/454/530/534/516`，token 头 `:284`，save→update 链 `:438-457`，缓冲上限 `:57,484-487`） |
| git 采集 | `src/acp/git-context.ts:50-64`（spawnSync 一次采集缓存，失败静默 null） |
| 配置（settings+env 优先级） | `src/acp/pi-settings.ts:93-140`（`PI_ACP_TELEMETRY_URL/TOKEN/DISABLED` + DEFAULT_URL 兜底） |
| 触发层（pi 事件→遥测） | `src/acp/session.ts`（LLM 调用 `turn_start/end` `:1134-1148`、工具 `:894/976/1007`、`recordInvocation` `:1218-1234`、`_meta` 注入 `:570-586`） |
| 轮级 telemetryCtx 生成 | `src/acp/agent.ts:536-542`（requestId+spanId）；`_meta` 回传 `:997-1016`；下行 envelope 校验 `:68-110`（`agent-studio.dev/ide-name` 等三个） |
| 向 pi 注入遥测上下文（iCode 不需要——引擎内直接埋点） | `src/pi-rpc/process.ts:74-124,373-394`（隐藏命令 `/__agent_studio_set_telemetry`） |
| 工具行数统计 | `src/acp/tool-line-counts.ts` |
| channel 口径 | `telemetry.ts:20`（`channelType='desktop'`）、`:408-410`（tool-detail 的 channelName=ideName、agentName=functionName） |

## 修订记录

| 日期 | 修订 |
|---|---|
| 2026-10-08 | 初稿 |
| 2026-10-08 | 评审（有条件通过）后修正：middleware 层 session 标识缺口（`ChatContext` 无 session 字段）→ `_compose_client_stack` 显式传 `session_id` + registry 降 session 级；`extra_body` 透传行号订正为 `openai_chat_completion.py:854-856` |
| 2026-10-08 | 终审通过。spanId 具体化（`uuid5(session_id:turn_id)`，随 registry 带出）；sub-agent 覆盖语义列 §8-8 |
| 2026-10-08 | GUI 触发点落点复查：补 diff 行数三字段；batch-save 范围列 §8-9 待决策 |
| 2026-10-08 | **部署形态适配 + pi-acp 参照系校准 + 六项决策落定（定稿）**：① 新增 agent_studio_new 形态（拓扑见 §一）——审批决策经 ACP 回流引擎 EventBus 已验证，引擎侧上报一套代码覆盖 TUI/ACP（决策 #8 引擎侧为主 + `_meta` 双向关联 §4.5）；② 新增 §2.2 pi-acp 参照系——**用户指示：pi-acp 的 event-log 接口体系是不成熟的实现、将被去除，iCode 不参考不实现（决策 #5）**；③ llm-call 仅搭车（决策 #2）；④ 输入触发纳入：对齐 pi-acp 单条 `tool-detail/save`（saveOnly，funcType=0），挂点 `input_refs.py`（§4.4，原 §8-9 决策闭环）；⑤ ID 体系升级 per-LLM-call（requestId 每次调用独立、工具按 call_id 反查，原 session 级降为 fallback；§8-8 sub-agent 风险随之消解）；⑥ 参数口径白名单+可切换（决策 #6）；⑦ 渠道区分：cli/icode-tui + desktop/ideName（决策 #7）；⑧ codeStatus 升级五态映射（decider USER/JUDGE 判据 + ApprovalModeUpdated 真值源）；⑨ 源码修改清单扩至 4-5 处 ~25 行（+input_refs 挂点 +ACP `_meta` 集成） |
| 2026-10-08 | 第三轮评审（有条件通过）修正：**id 双体系查实**——事件 `call_id`（Chrys 12 位短 id，`_chrys_call_id`，`_metadata_keys.py:35`）≠ middleware 所见响应 id（provider 原始 id，`metadata["call_id"]` 字面量，`hook_dispatch.py:187-202`），原"预期同源"判断错误；per-LLM-call registry 关联键改用 `provider_call_id`，§5 #5 补填事件字段升为必选（`tool_events.py` Start L391/Result L689 发布处传参，值在 L285 已取得）；修改总量 5 处 ~27 行；M1 增补 ACP 审批用例（`resolved_by_event` 回流验证 decider=USER）。其余新增主张（ApprovalDecider 五态判据、ApprovalModeUpdated 真值源、parse_skill_reference 挂点、pi-acp 附录 D 引用）经核验全部属实 |
| 2026-10-08 | §五 补充改动影响评估：逐项"性质"列（纯插入 vs 既有调用加参数）、冲突风险分级（#2/#3/#4 几乎不冲突；#1/#5 小冲突解决成本低）、"不修改任何既有逻辑行"明确表述 |
| 2026-10-08 | 实现层提示（评审④）落入 §4.1：流式（生产默认）下 `call_next()` 返回的 `context.result` 是未消费 ResponseStream，function calls 提取须走 `stream_result_hooks`/`with_result_hook` 在流终结后进行（`UsageTrackingMiddleware` 同款模式）；时序安全（流终结→提取→工具执行→Start 时 registry 就绪）；M1 增加流式链路贯通验证 |
| 2026-10-08 | 新增开发期 mock server 决策（§六 + M1/M2）：从 agent_studio_new `apps/telemetry-mock`（TS，Node≥24 + zod）**重写为 Python 标准库零依赖版**，放仓库顶层 `telemetry-mock/`（不进 `src/chrys`——wheel 打包隔离；不进 scripts——职责区分）；继承 5 端点/SQLite 原文落库/debug 观测/故障注入，清 rev.5 残留列；决定性理由=可复用为 pytest 集成测试；注意 mock 不覆盖 llm-call 搭车通道（其观测靠 raw_http_log/debug 落盘） |
| 2026-10-08 | mock server 细节修正（评审⑤）：① 端点数**维持 5 个**——用户决策：mock 对齐真实后端接口面而非本方案上报范围（batch-save/event-reaction 保留，agent_studio_new 侧将来报 event-reaction 时同一 mock 可用）；② 补 HTML 观察页 `/debug/view`（观测能力核心落点：列表 + 过滤 + 原文展开 + 2s 自动刷新）；③ debug 端点补全 6 个（含 `/health`、`/debug/faults` 运行期故障切换——缺它吞错链路验证断链）；④ 补 factory 结构约定（`start_telemetry_mock` 可 import + `__main__` CLI 分离，pytest 进程内/子进程双驱动） |
| 2026-10-08 | mock server 路径调整（用户确认）：顶层 `telemetry-mock/` → 根目录 `aixcoding/telemetry-mock/`——fork 根建 `aixcoding/` 统一二开专区（`README.md` + `docs/<功能>/` 内部文档 + `telemetry-mock/`，均不进 wheel；进包定制源码仍在 `src/chrys/aixcoding/`，定制测试在 `tests/aixcoding/`）。本文档同日入 fork `aixcoding/docs/telemetry/`（knowledge 原件保留）。另：内部文档禁放仓库 `docs/`——该目录经 `pyproject.toml` force-include 整目录进 wheel 随包分发（供 TUI `/help`） |
| 2026-10-08 | 文档更名：`iCode-数据上报直改源码方案可行性分析.md` → `iCode-数据上报直改源码方案.md`（H1 标题同步）。原因：文档实质已含可行性论证（§二三）+ 方案设计（§四五六七）+ 决策记录（§一/修订记录）+ 实施计划（§九），"可行性分析"仅覆盖前 1/3 且状态已定稿，原名低估其权威性。knowledge 仓库原件保持原名，作为历史归档不动；fork 内此份为随代码演进的活文档（M4 将追加 as-built 实现落地记录）。同目录 `plan.md` 为 M1-M4 任务跟踪清单 |
| 2026-10-09 | **llm-call 搭车 payload 与 aixcoding `getTelemetryData` 差异分析（决策）**：对照 `core/llm/index.ts:1782-1826` 逐字段核对三处差异——① `eventSubType`：iCode 两值 `agent`/`system` 是 aixcoding 六值的合理子集（无补全/chat 模式/批量调用），维持现状；② `functionName`：iCode 缺失（aixcoding 取 `promptBlocks[0]`，即 `#` 提示词模板名；软维度、多数对话为空、值域为自由文本），**决策暂不补**——功能入口统计已由输入触发 `record_invocation`（funcType=0）覆盖，预留补法见实现文档 §6.5；③ `userStoryId`/`userStoryName`：字段超集（§八-6），维持现状。详情见 `aixcoding/docs/telemetry/实现方案/01-llm-call搭车上报实现.md` §六 |
