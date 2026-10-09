# llm-call 搭车上报实现（随大模型上送额外数据）

- 日期：2026-10-09
- 状态：代码已落地（M1），真链路端到端验收项待完成
- 里程碑：M1
- 上游源码改动：1 处（`service/llm/instrumented.py`，方案 §五 #1）
- 关联文档：[iCode-数据上报直改源码方案.md](../iCode-数据上报直改源码方案.md) §4.1、[plan.md](../plan.md) M1

## 一、概述

llm-call 是四类上报中的**搭车通道**：不发起独立 HTTP 请求，而是在每次模型请求前，通过一个 `ChatMiddleware` 把 `telemetry` 字段注入 `context.options["extra_body"]`，wire 客户端（OpenAI/Anthropic SDK）把它 merge 进请求 body 顶层，由内部模型网关解析落库。iCode 侧对回执零感知（方案决策 #2）。

> 与另外三类上报（tool-detail / ai-code / 输入触发）不同，llm-call **不经过** `aixcoding/http.py` 统一 HTTP 出口——那三类的报文发往 csas 后端（mock 可收），本类的报文发往模型网关（mock 收不到，观测靠 `CHRYS_DEBUG_LLM_RAW_HTTP_LOG` 落盘）。

## 二、核心机制

```
模型请求发起
   │
   ▼
AixTelemetryMiddleware.process()          ← ChatMiddleware 栈内、call_next() 之前
   ├─ 判开关（session_id 空/telemetry 关 → 直接透传，零开销）
   ├─ 生成 requestId=uuid4、spanId=uuid5(session_id:turn_id)
   ├─ _inject_telemetry()：向 context.options["extra_body"]["telemetry"] 塞 payload
   ▼
call_next() → wire 客户端
   │  SDK 把 extra_body merge 进请求 body 顶层 → 模型网关解析落库
   ▼
响应返回
   ├─ 非流式：context.result 是 ChatResponse → 直接提取 function_call
   └─ 流式：context.result 是未消费 ResponseStream → with_result_hook 流终结后提取
   ▼
_record_response()：按 provider 原始 call_id 登记 per-call registry
   （供 M2 tool-detail 上报反查 requestId/spanId 关联）
```

## 三、代码位置总览

| 职责 | 文件（相对 `src/chrys/`） | 关键行 |
|---|---|---|
| 注入挂点（组栈唯一入口） | `service/llm/instrumented.py` | `_compose_client_stack` 873-895；工厂调用点 992 / 1056 / 1129 |
| middleware 核心实现 | `aixcoding/telemetry/llm_telemetry.py` | 全文 204 行 |
| `extra_body` 透传（SDK merge） | `service/llm/openai_chat_completion.py` | 841-848 |
| channel 三元组 / pluginVersion | `aixcoding/context.py` | `detect_channel` 40-55、`current_channel` 78-80、`plugin_version` 119-129 |
| git 五件套 | `aixcoding/git_info.py` | `collect_git_info` 49-65、`GitInfo` 26-36 |
| 总开关 / 配置 | `aixcoding/config.py` | `load_settings` 76-110（`telemetry_enabled` 98-100） |

## 四、实现细节

### 4.1 注入挂点（上游源码 #1）

`_compose_client_stack` 是所有 provider（OpenAI Chat / OpenAI Responses / Anthropic）的唯一组栈点。此处新增 `session_id` 形参，并把 `build_telemetry_middleware(session_id)` 作为 `ChatMiddlewareLayer` 的构造器 middleware 传入：

```python
# service/llm/instrumented.py:873-895
def _compose_client_stack(chat_client, *, session_id=None, max_iterations, max_consecutive_errors, tool_result_ceiling_tokens=None):
    # AIxCoding telemetry: llm-call piggyback middleware (chrys/aixcoding/telemetry/llm_telemetry.py).
    from chrys.aixcoding.telemetry.llm_telemetry import build_telemetry_middleware
    knobs = {...}
    return ToolLoopLayer(ChatMiddlewareLayer(chat_client, middleware=build_telemetry_middleware(session_id)), **knobs)
```

三个工厂调用点（`:992` / `:1056` / `:1129`）各自把已有的 `session_id` 补传到 `_compose_client_stack`。

**关键前置（已在 plan.md 记录的附带修复）**：`session_id` 必须从 `create_client` 一路传进 `stack_kwargs`（`service/llm/clients.py`）——否则 instrumented 工厂恒收 `None`、middleware 恒 disabled。此修复与 #1 同批落地。

### 4.2 middleware 实现（新增，核心）

`AixTelemetryMiddleware` 继承 `ChatMiddleware`，在 `process()` 里完成"注入 payload"与"提取 function_call 登记"两件事：

```python
# aixcoding/telemetry/llm_telemetry.py:113-140
async def process(self, context, call_next):
    if not self._enabled():
        await call_next()
        return
    request_id = str(uuid.uuid4())
    span_id = ""
    trajectory = current_trajectory()
    turn_id = trajectory.turn_id if trajectory is not None else None
    if turn_id:
        span_id = root_span_id(self._session_id or "", turn_id)
    try:
        self._inject_telemetry(context, request_id, span_id)
    except Exception:
        logger.warning("AIxCoding telemetry payload inject failed", exc_info=True)
    side_call = in_internal_side_call()
    await call_next()
    if side_call:
        return
    result = context.result
    if isinstance(result, ChatResponse):
        self._record_response(result, request_id, span_id)
    elif isinstance(result, ResponseStream):
        def _record_on_final(response):
            self._record_response(response, request_id, span_id)
            return response
        result.with_result_hook(_record_on_final)
```

注入 payload 的关键三行——把 payload 放进 `extra_body["telemetry"]`：

```python
# aixcoding/telemetry/llm_telemetry.py:188-193
options = dict(context.options or {})
extra_body = options.get("extra_body")
merged = dict(extra_body) if isinstance(extra_body, Mapping) else {}
merged["telemetry"] = payload
options["extra_body"] = merged
context.options = options
```

### 4.3 `extra_body` 透传机制

OpenAI SDK 原生支持 `extra_body`（把顶层 key 原样 merge 进请求 JSON body）。iCode 侧无需显式透传，仅在 `_prepare_options` 校验 `n=1` 时顺带读取它，merge 依赖 SDK 原生能力：

```python
# service/llm/openai_chat_completion.py:841-848
def _prepare_options(self, messages, options):
    # The kernel consumes one conversation, not alternative choices. Check
    # extra_body too: the SDK merges it over named request parameters.
    extra_body = options.get("extra_body")
    for source in (options, extra_body if isinstance(extra_body, Mapping) else {}):
        choice_count = source.get("n")
        ...
```

### 4.4 payload 字段与来源

`_inject_telemetry`（`:152-187`）组装的 `telemetry` payload：

| 字段 | 值 / 来源 |
|---|---|
| `requestId` | `uuid4()`（每次调用独立） |
| `sessionId` | middleware 构造传参（`_compose_client_stack` 显式传入） |
| `eventType` | 固定 `"llm"` |
| `eventSubType` | `in_internal_side_call()` 为真 → `"system"`，否则 `"agent"` |
| `spanId` | `turn_id` 存在时 = `uuid5(NAMESPACE, f"{session_id}:{turn_id}")`（确定性，同轮次恒同值） |
| `channelType` / `channelName` / `channelVersion` | `current_channel()`（argv 识别 + 桌面端 `_meta` 覆盖） |
| `pluginVersion` | `plugin_version()`（`importlib.metadata.version("chrys")`，缓存） |
| `projectName` | workspace 目录名（`SessionEnvironment.cwd`，源码 #6，2026-10-09；缺省回退 `Path.cwd()`） |
| `gitRemote` / `gitBranch` / `gitRevision` / `gitOwner` / `gitRepo` | `collect_git_info(workspace_cwd)`，有值才注入 |

说明：根 span 无父，故不传 `parentSpanId`。**span 模型为"每 turn 一个根 span"的扁平模型**（2026-10-09 源码查证）：sub-agent 的上下文派生链 `with_actor().with_run().with_exchange_facts({})`（`sub_agents.py:76`，上游注释明确 "a sub-agent is single-turn"）**不碰 `turn_id`**——即 sub-agent 的 LLM 调用与主对话**共享同一 spanId**；turn 之间是兄弟关系非父子，无真实父级可填；sub-agent 的归属区分由 `requestId` 承担（per-call registry 精确命中 sub-agent 自己那次 LLM 调用）。将来后端若要求 span 树，需先把 span 模型改造为 per-exchange/per-invocation 层级（重设计，非加字段可解）。`userId` 未纳入（登录未落地，方案决策 #4 预留 provider 接口）。

### 4.5 per-call registry（关联工具上报）

middleware 在响应里从 `function_call` 提取 **provider 原始 `call_id`**（`FunctionCallContent.call_id`，即 `loop.py` 写入 `metadata["call_id"]` 的同一个值），登记进进程内有序字典：

```python
# aixcoding/telemetry/llm_telemetry.py:64-83
def record_call(provider_call_id, request_id, span_id, session_id):
    with _registry_lock:
        _registry[provider_call_id] = (request_id, span_id)   # 上限 4096，超了丢最旧
        if session_id:
            _session_latest[session_id] = (request_id, span_id)

def resolve_call(provider_call_id, session_id=None):
    # per-call 命中优先 → 退化为 session 级最新值
```

**id 双体系（方案 §4.2 缺口②）**：registry 键是 provider 原始 call id，与工具事件的 `call_id`（Chrys 短 id）不同源——因此上游 `tool_events.py` 发布 Start/Result 时补填了 `provider_call_id`（源码 #5），M2 tool-detail 上报据此反查。

### 4.6 流式 / 非流式提取

- **非流式**：`call_next()` 后 `context.result` 已是 `ChatResponse`，直接提取。
- **流式（生产默认）**：`call_next()` 返回时 `context.result` 是未消费的 `ResponseStream`，改走 `with_result_hook(_record_on_final)` 在流终结后提取（`UsageTrackingMiddleware` 同款模式）。时序安全：流终结 → 写 registry → 工具才执行 → Start 事件发布时 registry 已就绪。

### 4.7 side call 处理

judge / 标题 / last-words 等系统内部调用（`in_internal_side_call()` 为真）只搭车（`eventSubType="system"`），**不写 registry、不更新 session 级最新值**——"side call 不参与关联"，对齐 aixcoding 仅主对话通道语义（`process()` 里 `if side_call: return`）。

### 4.8 开关与降级

- `_enabled()`：`session_id` 为空或 `load_settings().telemetry_enabled` 为 False 时直接透传，零开销。
- 总开关来源：`~/.chrys/aixcoding.yaml` 的 `telemetryEnabled` + 环境变量 `AIXCODING_TELEMETRY_DISABLED`（`config.py:98-100`）。
- 装配降级：`build_telemetry_middleware` 捕获异常只记日志返回 `None`（`llm_telemetry.py:98-104`），payload 注入与 function-call 提取各自 try/except 吞错，绝不抛出。

## 五、验证状态

- **单测级（已通过）**：`aixcoding/tests/test_llm_telemetry.py` 12 项——payload 注入 / registry 精确命中→session 级 fallback→上限淘汰 / spanId 确定性 / 流式终结时序 / side call 不关联 / 总开关透传 / `ChatMiddlewareLayer` 端到端。
- **真链路（待验，plan.md M1 验收）**：TUI / ACP 各发一条模型请求 → 网关查库；`CHRYS_DEBUG_LLM_RAW_HTTP_LOG=1` 落盘 `llm_raw_http.jsonl` 核对 `request.body.json.telemetry` 字段；`current_trajectory()` 在 middleware 执行期绑定验证；sub-agent 关联归属。

## 六、与 aixcoding `getTelemetryData` 的字段差异与决策（2026-10-09 记录）

对照参考实现 `aixcoding-continue/core/llm/index.ts:1782-1826` 的 `getTelemetryData`，iCode 当前 llm-call payload 与 aixcoding 存在三处差异。以下记录差异的实际情况与决策，供日后回看。

### 6.1 差异总览

| # | 字段 | aixcoding | iCode | 结论 |
|---|---|---|---|---|
| 1 | `eventSubType` | 六值：`chat`/`inline-chat`/`completion`/`batch`/`system`/`agent` | 两值：`agent`（主对话）/`system`（side call） | 合理子集，维持 |
| 2 | `functionName` | 有（= `promptBlocks[0]`，功能入口名） | **无** | 暂不补（见 §6.3/§6.5） |
| 3 | `userStoryId`/`userStoryName` | 有（用户故事上下文） | 无 | 已知字段超集，维持 |

### 6.2 差异 1：eventSubType（合理子集，维持现状）

iCode 的 `agent`/`system` 是 aixcoding 值域的真子集，交集值语义完全对齐：
- `agent`：主对话（aixcoding 指"非 chat 模式"；iCode 纯 agent 平台无 chat 模式）。
- `system`：非用户直接触发的内部调用（aixcoding=标题生成；iCode=judge/标题/last-words，判 `in_internal_side_call`）。

iCode 缺 `chat`/`completion`/`inline-chat`/`batch` 系因无对应能力：无补全（方案决策 #3，`completion-event` 不适用）、无 chat 模式、无批量调用（embed/rerank）。**结论：维持现状，无需改动。**

### 6.3 差异 2：functionName（调研记录）

**aixcoding 实现链路**（`core/llm/index.ts`）：

```
GUI  streamNormalInput.ts: extractPromptBlocks() 提取 # 模板名 → promptBlocks[]
core index.ts:1286-1290: functionName = promptBlocks[0]
core index.ts:1782-1826: getTelemetryData() → payload.functionName
→ 随 body.telemetry 上送模型网关落库
```

**实际值与触发条件**（调研结论）：
- 值 = 提示词模板 frontmatter 的 `name`，带空格的英文标题（如 `"Write Core Unit Test"`、`"Update LLM Info"`），非枚举、非短标识符。
- 仅当用户输入 `#` 选中一个提示词模板时才产生；普通文本、`/` 斜杠命令、`$` skill 均不产生（`/`、`$` 是 paragraph 内 `slash-command-item`，`extractPromptBlocks` 只扫顶层 `prompt-block`）。
- `/`、`$` 的入口信息走的是另一条"输入触发"通道（`streamResponse.ts` 的 `buildInputTriggeredUsages`），对应 iCode 的 `record_invocation`（单条 `tool-detail/save`，funcType=0）。

**性质**：软维度、自由文本、多数对话为空。后端只能按字符串分组统计，不可能当枚举或硬关联键。

**iCode 对应物**：`current_function_name()`（ACP `_meta` 的 `agent-studio.dev/function-name`），已实现并用于 tool-detail/ai-code 的 `agentName`，仅未写进 llm-call。

### 6.4 差异 3：userStoryId/userStoryName（字段超集，维持现状）

iCode 无"用户故事"（userStory）数据源，属已知字段超集（方案 §八-6，后端容忍度待确认）。**结论：维持现状。**

### 6.5 决策记录

| 项 | 决定 |
|---|---|
| eventSubType | 维持现状（合理子集） |
| userStoryId/userStoryName | 维持现状（无数据源，字段超集） |
| **functionName** | **暂不补** |

**functionName 暂不补的理由**：
1. 它是软维度、多数场景为空（aixcoding 只有 `#` 模板才产生），后端大概率不作为关键 join 键。
2. iCode 的功能入口统计需求已由 `record_invocation`（输入触发单条 save，funcType=0）覆盖，对齐 aixcoding 的 `buildInputTriggeredUsages`。
3. 值域是自由文本（用户自建模板标题），补了也不构成稳定维度。

**将来若需补（预留方案）**：在 `llm_telemetry.py` 的 `_inject_telemetry` 加一行取 `current_function_name()` 写入 `functionName`（与独立通道 `agentName` 同源）；desktop（ACP）形态有值、cli（TUI）形态为 None——如需 cli 形态也覆盖，可顺带在 `input_refs` 挂点（源码 #3 已挂 `record_skill_invocation`）把 skill 名一并写入 function-name 上下文。均为新增代码，不碰上游逻辑。

## 附录：文件速查

| 内容 | 位置 |
|---|---|
| middleware / registry / 组装入口 | `src/chrys/aixcoding/telemetry/llm_telemetry.py` |
| 组栈注入点 | `src/chrys/service/llm/instrumented.py:873-895` |
| `session_id` 传入工厂的修复 | `src/chrys/service/llm/clients.py`（`stack_kwargs` 补 `"session_id"`） |
| `extra_body` 校验点 | `src/chrys/service/llm/openai_chat_completion.py:841-848` |
| side call 判定 | `chrys.kernel.in_internal_side_call`（`kernel`） |
| 轮次根 span 生成 | `llm_telemetry.py:root_span_id`（`uuid5`，命名空间固定） |
| 开关 / profile / token | `src/chrys/aixcoding/config.py` |
| channel / pluginVersion | `src/chrys/aixcoding/context.py` |
| git 五件套 | `src/chrys/aixcoding/git_info.py` |
