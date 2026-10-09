# tool-detail 上报实现（save/update + 输入触发）

- 日期：2026-10-09
- 状态：代码已落地（M2），真链路端到端验收项待完成
- 里程碑：M2
- 上游源码改动：4 处（`assembly.py` #2、`runner.py`/`active_injection.py` #3、`acp/server.py` #4、`tool_events.py` #5）
- 关联文档：[iCode-数据上报直改源码方案.md](../iCode-数据上报直改源码方案.md) §4.2/§4.4/§4.5、[plan.md](../plan.md) M2、[01-llm-call搭车上报实现.md](01-llm-call搭车上报实现.md)

## 一、概述

tool-detail 是**独立 HTTP 通道**（区别于 llm-call 的搭车通道）：从 EventBus 订阅工具事件，组装 `tool-detail/save` + `tool-detail/update` 报文，经 `aixcoding/http.py` 统一串行 HTTP 出口 POST 到 csas 后端。三类触发：

| 触发 | 时机 | 上报 |
|---|---|---|
| 工具调用 Start | 审批后、执行前 | `save` + `update(PENDING=3)` |
| 工具调用 Result | 执行结束 | `update(成功=1 / 失败=2 / 拒绝=4)` |
| 输入触发 | slash skill 引用命中 | 单条 `save`（funcType=0，saveOnly 不更新） |

## 二、核心机制

```
EventBus 事件流（InvocationToolCallStart / Result / Approval*）
   │  subscriber.attach() 用 bus.subscribe() 回调式注册（源码 #2）
   ▼
ToolDetailReporter.on_start / on_result / record_invocation
   │  resolve_call() 反查 requestId（per-call registry，见 01 文档）
   │  common_fields() 补公共字段
   ▼
TelemetryHttpClient.submit()  ← 单 worker 串行队列，fire-and-forget 吞错
   │  save→update 天然保序（同一队列 FIFO）
   ▼
POST {report_base_url}/tool-detail/save | /update
```

## 三、代码位置总览

| 职责 | 文件（相对 `src/chrys/`） | 关键行 |
|---|---|---|
| 订阅装配入口（源码 #2） | `orchestration/engine/assembly.py` | 76-80 |
| 事件分发 + 进程级单例 | `aixcoding/telemetry/subscriber.py` | 全文 190 行 |
| save/update/输入触发组装 | `aixcoding/telemetry/reporters/tool_detail.py` | 全文 187 行 |
| 失败分类 | `aixcoding/telemetry/outcome.py` | 全文 48 行 |
| 公共字段收口 | `aixcoding/telemetry/reporters/__init__.py` | 25-61 |
| 串行 HTTP 出口 / 批量缓冲 | `aixcoding/http.py` | `TelemetryHttpClient` 25-100、`BatchBuffer` 103-165 |
| 端点/枚举契约 | `aixcoding/telemetry/types.py` | 全文 72 行 |
| provider_call_id 补填（源码 #5） | `service/agent_middleware/events/tool_events.py` | 398-399、696-697 |
| 输入触发挂点（源码 #3） | `orchestration/engine/run/runner.py` 585-588、`active_injection.py` 422-425 | — |
| ACP `_meta` 集成（源码 #4） | `app/acp/server.py` 333-337,383,390 + `aixcoding/telemetry/acp_meta.py` | — |

## 四、实现细节

### 4.1 订阅装配（源码 #2）

在引擎装配点 `bus = event_bus` 之后挂载 subscriber（per-bus 幂等，开关关闭时直接返回）：

```python
# orchestration/engine/assembly.py:76-80
bus = event_bus
# AIxCoding telemetry: tool-detail/ai-code reporting on this bus (idempotent per bus).
from chrys.aixcoding.telemetry import subscriber

subscriber.attach(bus)
```

`subscriber.attach()` 订阅 5 类事件并分发：

```python
# aixcoding/telemetry/subscriber.py:103-107
_register(bus, InvocationToolCallStart, _on_start)
_register(bus, InvocationToolCallResult, _on_result)
_register(bus, ApprovalModeUpdated, _on_approval_mode)
_register(bus, ApprovalRequest, _on_approval_request)
_register(bus, ApprovalResponse, _on_approval_response)
```

**实现偏差（相对方案）**：方案写 `bus.stream()` 订阅，但 `assemble_agent_engine` 在 TUI 路径是**无事件循环的同步上下文**（Textual `run()` 之前构造引擎），stream 消费循环无法同步启动。改用 `bus.subscribe()` 回调式注册：handler 仅做 payload 组装 + 串行队列入队（毫秒级），事件零丢失语义不变。`_register()` 双分支处理：事件循环内 `create_task`、无循环时 `asyncio.run` 一次性完成（`subscriber.py:169-180`）。

### 4.2 事件来源与 provider_call_id 补填（源码 #5）

工具事件由 `tool_events.py` 发布，`provider_call_id` 是 per-call registry 的关联键（与事件的 Chrys 短 `call_id` 不同源，见 01 文档 §4.5）。上游在发布处补填该字段：

```python
# service/agent_middleware/events/tool_events.py:395-400（Start）
tool_kind=tool_kind,
args=args,
call_id=call_id,
# AIxCoding telemetry: provider call id keys the per-call registry.
provider_call_id=provider_call_id,
session_id=self._session_id,
```

Result 发布处同构（`:696-697`）。值 `provider_call_id = get_provider_call_id(context)` 在 `:285` 已取得，形参作用域内直接可用。

### 4.3 save 组装

`on_start`（审批后、执行前）组装 `tool-detail/save`：

```python
# aixcoding/telemetry/reporters/tool_detail.py:106-140
def on_start(self, event):
    remember_bounded(self._started_at, event.call_id, event.timestamp)
    request = resolve_call(event.provider_call_id, event.session_id)
    payload = {
        "funcId": event.provider_call_id or event.call_id,  # provider id 优先（csas 语义），空值 fallback Chrys 短 id
        "funcName": event.tool_name,
        "funcType": func_type_for_kind(event.tool_kind),  # skill=0/MCP=1/内置=3
        "sessionId": event.session_id,
    }
    if request is not None:
        payload["requestId"] = request[0]   # per-call registry 反查
        if request[1]:
            payload["spanId"] = request[1]  # 提问周期链路 span（=llm-call 搭车 telemetry.spanId）
    value = pick_value(event.tool_name, event.args, full_mode=...)
    if value is not None:
        payload["value"] = value
    ...
    payload.update(common_fields())
    self._submit(TOOL_DETAIL_SAVE, payload)
    self._submit(TOOL_DETAIL_UPDATE, {"funcId": event.call_id, "funcName": event.tool_name,
        "codeStatus": CodeStatus.PENDING, "executionStartedAt": _iso(event.timestamp)})
```

- **funcType 映射**（`func_type_for_kind`）：`KIND_SKILL→0`、`KIND_MCP→1`、其余→内置 `3`。
- **参数口径**（决策 #6，`pick_value`）：默认白名单——仅 `read_file→path`（input_model 真实参数名，2026-10-09 修正：原写 `filepath` 真链路取不到值）、`load_skill→skill_name` 放行；`toolParamMode: full` 时输出 args 全量 JSON 截 2000 字符。

### 4.4 update 组装

`on_result` 组装 `tool-detail/update`（终态）：

```python
# aixcoding/telemetry/reporters/tool_detail.py:142-161
def on_result(self, event):
    started = self._started_at.pop(event.call_id, None)
    classification = classify_result_metadata(event.metadata)
    payload = {
        "funcId": event.call_id,
        "funcName": event.tool_name,
        "codeStatus": classification.code_status,   # 成功1/失败2/拒绝4
        "executionDurationMs": event.duration_ms,
    }
    if started is not None:
        payload["executionStartedAt"] = _iso(started)
    if event.timestamp is not None:
        payload["executionFinishedAt"] = _iso(event.timestamp)
    if classification.failure_type is not None:
        payload["failureType"] = classification.failure_type
    if classification.rejected or classification.code_status != CodeStatus.SUCCESS:
        error_text = _error_text(event)
        if error_text:
            payload["funcErrorMessage"] = error_text
    payload.update(line_counts(event.metadata))
    self._submit(TOOL_DETAIL_UPDATE, payload)
```

写类工具行数（`line_counts`，随终态 update 上报）——`metadata["file_snapshot"]` 是 **tuple
`(before_text, after_text)`**（`mutations/pipeline.py:89`，`tool_events.py:616` 原样挂入；
2026-10-09 真链路发现原先按对象属性取恒空，单测用 SimpleNamespace 掩盖了类型不符，已修）。
行数规则（**2026-10-09 用户定稿**，按 `metadata["file_mutation_op"]` 分派，缺省按空侧推断）：
**创建**（op=create）：`originalLines=0`、`addedLines=新文件行数`、`deletedLines=0`；
**删除**（op=delete）：`originalLines=0`、`addedLines=0`、`deletedLines=被删文件行数`；
**修改**（op=modify）：difflib 差异，`originalLines=before 行数`。
save 侧：写类工具（`write_file`/`edit_file`，对齐 iCode `_FILE_TOOLS`）从参数 `path`
取值附 **`fileName`**（csas 契约字段，aixcoding-continue save 填 `fileName`：
`toolCallReporter.ts:70`；2026-10-09 真链路联调补齐）。**fileName 口径**（同日用户定稿，
对齐 aixcoding `getRelativePathOfFile` 语义）：文件在当前工程目录（cwd）内时取
**相对路径**（含文件名，POSIX 分隔符，`relative_file_name()`）；工程外或解析失败
保留绝对路径。

**键名对齐与超集字段（2026-10-09 真链路联调修正）**：update 报文键名对齐
aixcoding-continue `toolCallReporter.ts` 的真实上报——关联键 `funcId`（键名对齐；
**值改用 provider 原始 call id**——csas 语义 = "模型调用工具时生成的 toolUseId"
（`tool-use-types.ts:75-76`），与 session.json 消息历史/模型网关记录同 id 可直接
核对；id 双体系见方案 §4.2：`metadata["call_id"]`（provider）≠
`metadata["_chrys_call_id"]`（内部 12hex）；provider id 为空时 fallback Chrys
短 id）、错误信息 `funcErrorMessage`（非 `toolErrorMessage`）、
并附 `funcName`；save 的 `spanId` 语义修正——csas 契约为**提问周期链路 span**
（aixcoding-continue 取 `SessionContext.getCurrentSpanId()`，与 llm-call 搭车
telemetry 的 `spanId` 同值），改由 registry 反查的根 span 填充（原先误填
`origin.invocation_id`），`parentSpanId` 真实上报从不填充、不再下发（查证
依据见实现文档 01 §4.4：span 为"每 turn 一根"的扁平模型，sub-agent 与主对话
同值，无真实父级可填）；输入触发
save 同步对齐 aixcoding `InputTriggeredUsage` 语义——`requestId` 留空（不误挂
上一轮请求），仅以 `spanId` 关联。save 的 `productName` 在真实契约中可选且 aixcoding-continue 不下发
（本实现同样不下发，mock 校验已同步放宽）。**超集字段**：`executionDurationMs` /
`executionStartedAt` / `executionFinishedAt` / `failureType` 为 iCode 额外下发
（aixcoding-continue 的 update 只发 funcId/funcName/codeStatus/funcErrorMessage/
三行数 7 字段）——**决策（2026-10-09）：暂保留**供时长与错误分类观测（M2 验收核对
项），真实后端对未知字段的容忍度待方案 §8-6 外部确认，若后端严格拒收再收敛。

### 4.5 失败分类（outcome.py）

`classify_result_metadata` 复用 foundation 的结构化判定（不依赖 service 层，满足分层约束）：

```python
# aixcoding/telemetry/outcome.py:36-47
def classify_result_metadata(metadata):
    if tool_result_metadata_is_rejected(metadata):
        return ToolOutcomeClassification(CodeStatus.USER_REJECTED, rejected=True)
    timed_out = metadata.get(PROCESS_TIMED_OUT_METADATA_KEY) is True or bool(metadata.get(SHELL_TIMED_OUT_METADATA_KEY))
    errored = metadata.get(TOOL_ERRORED_METADATA_KEY) is True
    structured_failed = tool_result_metadata_failure_state(metadata) is True
    if timed_out:
        return ToolOutcomeClassification(CodeStatus.FAILED, FailureType.TIMEOUT)
    if errored or structured_failed:
        return ToolOutcomeClassification(CodeStatus.FAILED, FailureType.ERROR)
    return ToolOutcomeClassification(CodeStatus.SUCCESS)
```

映射：拒绝→`4`；超时→`2`+`timeout`；错误→`2`+`error`；否则 `1`。**已知边界**：`CancelledError` 不发 Result → save 停在 PENDING，M2 明确容忍（方案 §8-4）。

### 4.6 输入触发（源码 #3）

引擎层 slash skill 引用解析命中时（`parse_skill_reference` 返回非 None）调 `record_skill_invocation`——不改纯函数本体，在调用方插入：

```python
# orchestration/engine/run/runner.py:581-589（active_injection.py:418-426 同构）
reference = parse_skill_reference(text, skill_details or ...)
if reference is None:
    return None
# AIxCoding telemetry: input-trigger report for a resolved slash skill reference.
from chrys.aixcoding.telemetry.subscriber import record_skill_invocation

record_skill_invocation(reference.skill.name, self._session.session_id)
return format_skill_reference_reminder(reference)
```

对应 reporter 组装单条 save（saveOnly，不更新）：

```python
# aixcoding/telemetry/reporters/tool_detail.py:163-181
def record_invocation(self, skill_name, session_id):
    payload = {
        "funcName": skill_name,
        "funcType": FuncType.SKILL,   # 0
        "sessionId": session_id,
    }
    request = resolve_call("", session_id)   # session 级最新（无 provider_call_id）
    if request is not None:
        payload["requestId"] = request[0]
    ...
    payload.update(common_fields())
    self._submit(TOOL_DETAIL_SAVE, payload)
```

注：`retry.py` 第三处同形方法不挂（重试是同一文本重放，避免双计）。

### 4.7 ACP `_meta` 集成（源码 #4）

agent_studio_new 形态下，channel 身份与功能入口从 prompt `_meta` envelope 读取（下行），响应用 telemetry `_meta` 回传 requestId/spanId（上行）：

```python
# app/acp/server.py:333-337
# AIxCoding telemetry: desktop channel from the prompt _meta envelope,
# plus telemetry ids returned to the client on the response.
from chrys.aixcoding.telemetry.acp_meta import read_ide_channel_meta, telemetry_response_meta

read_ide_channel_meta(kwargs)
```

```python
# app/acp/server.py:383 / :390（EndTurn / Cancelled 两处）
_meta=telemetry_response_meta(session_id),
```

`acp_meta.py` 负责 envelope 校验与组装：下行读 `agent-studio.dev/ide-name` / `ide-version` / `function-name`（`schemaVersion==1` + 非空短字符串 + 无控制字符），命中后 `set_desktop_channel` 覆盖为 `desktop`/ideName、`set_current_function_name` 记录功能入口（tool-detail 报文的 `agentName` 字段来源）；上行 `telemetry_response_meta` 取 registry 的 session 级最新主对话调用回传 `agent-studio.dev/telemetry`。

### 4.8 保序与吞错（http.py）

- `TelemetryHttpClient.submit()` 仅入队（fire-and-forget），单 worker 逐条 POST，`save→update` 天然保序；上报失败只记日志绝不抛出（`http.py:37-44,73-100`）。
- `trust_env=False`：内网端点直连，不受本机代理环境变量干扰。
- `common_fields()`（`reporters/__init__.py:25-61`）统一补 `pluginVersion`/`userId`/channel 三元组/`projectName`/git 五件套，reporter 只给业务字段。

## 五、验证状态

- **单测级（已通过）**：`aixcoding/tests/test_tool_detail.py` 16 项（分类/映射/白名单/行数/报文/保序/装配幂等/开关）。
- **真链路（待验，plan.md M2 验收）**：成功/异常/超时/审批拒绝四形态 mock SQLite 入库核对；skill 引用触发入库；`_meta` 回传被 agent_studio_new 收到；故障注入下吞错行为（后续可自动化 pytest 集成测试：起 mock → 跑引擎 → 断言落库）。

## 附录：文件速查

| 内容 | 位置 |
|---|---|
| save/update/输入触发组装 | `src/chrys/aixcoding/telemetry/reporters/tool_detail.py` |
| 订阅装配 / 进程级单例 / 输入触发入口 | `src/chrys/aixcoding/telemetry/subscriber.py` |
| 失败分类 | `src/chrys/aixcoding/telemetry/outcome.py` |
| 公共字段收口 | `src/chrys/aixcoding/telemetry/reporters/__init__.py` |
| 端点/枚举/failureType | `src/chrys/aixcoding/telemetry/types.py` |
| 串行队列 / 批量缓冲 | `src/chrys/aixcoding/http.py` |
| 装配挂点 | `src/chrys/orchestration/engine/assembly.py:76-80` |
| 输入触发挂点 | `src/chrys/orchestration/engine/run/runner.py:585-588`、`active_injection.py:422-425` |
| ACP `_meta` 挂点 | `src/chrys/app/acp/server.py:333-337,383,390` |
| provider_call_id 补填 | `src/chrys/service/agent_middleware/events/tool_events.py:398-399,696-697` |
