# ai-code 上报实现

- 日期：2026-10-09
- 状态：代码已落地（M3），真链路端到端验收项待完成
- 里程碑：M3
- 上游源码改动：无（全部在 `src/chrys/aixcoding/` 新增包内）
- 关联文档：[iCode-数据上报直改源码方案.md](../iCode-数据上报直改源码方案.md) §4.3、[plan.md](../plan.md) M3、[02-tool-detail上报实现.md](02-tool-detail上报实现.md)

## 一、概述

ai-code 上报记录"AI 生成的代码块"——`write_file` / `edit_file` 执行成功后，把生成内容按 diff 切成 blocks，经批量缓冲 POST 到 `ai-code/save`。数据源是工具执行结果的 SnapshotStore 全文快照（`metadata["file_snapshot"]` 的 before/after），不重复读取磁盘。

> 归属关系：ai-code 与 tool-detail 共享同一 EventBus 订阅链路（`subscriber.attach()` 里同一 `_on_start`/`_on_result` 同时分发两个 reporter，见 02 文档 §4.1），但 ai-code 走 **批量缓冲**（`BatchBuffer`）而非即时串行队列。

## 二、核心机制

```
写类工具（write_file / edit_file）执行成功
   │  InvocationToolCallResult（metadata 含 file_snapshot before/after）
   ▼
AiCodeReporter.on_result
   ├─ 判定：写类工具？未报错？有 after_text？有 path？
   ├─ ai_code_blocks(before, after) → blocks（整文件单 block）
   ├─ ApprovalTracker.adopted_code_status → codeStatus（五态）
   ├─ 组装 payload（reportId/filepath/blocks/sourceType/requestId/git 四字段）
   ▼
BatchBuffer.add()  ← 满 20 条或 10s flush，超 500 丢旧
   ▼
POST {report_base_url}/ai-code/save
```

## 三、代码位置总览

| 职责 | 文件（相对 `src/chrys/`） | 关键行 |
|---|---|---|
| reporter 组装 | `aixcoding/telemetry/reporters/ai_code.py` | 全文 184 行 |
| blocks 计算 | 同上 `ai_code_blocks` | 50-60 |
| 审批采纳追踪 | 同上 `ApprovalTracker` | 76-109 |
| 批量缓冲 | `aixcoding/http.py` | `BatchBuffer` 103-165 |
| 订阅分发（双 reporter） | `aixcoding/telemetry/subscriber.py` | 60-83、132-141 |
| codeStatus 枚举 | `aixcoding/telemetry/types.py` | 30-35 |

## 四、实现细节

### 4.1 触发条件

`FILE_WRITE_TOOLS = frozenset({"write_file", "edit_file"})`（对齐 service 层 `_FILE_TOOLS`；shell 隐式写不报）。`on_result` 里同时满足才上报：写类工具、`on_start` 时记录过 args、未报错（`errored=False`）、`after_text` 非空、`path` 非空：

```python
# aixcoding/telemetry/reporters/ai_code.py:123-140
def on_result(self, event, *, code_status, errored):
    args = self._args_by_call.pop(event.call_id, None)
    if args is None or event.tool_name not in FILE_WRITE_TOOLS or errored:
        return
    snapshot = event.metadata.get("file_snapshot")
    before_text = getattr(snapshot, "before_text", None)
    after_text = getattr(snapshot, "after_text", None)
    if not isinstance(after_text, str) or not after_text:
        return
    path = args.get("path")
    if not isinstance(path, str) or not path:
        return
```

### 4.2 blocks 计算

`ai_code_blocks` 对齐 pi-acp `aiCodeBlocks` 语义：**整文件一个 block**，`rangeStart` 定位旧文中新文本首行，找不到回退 1：

```python
# aixcoding/telemetry/reporters/ai_code.py:50-60
def ai_code_blocks(before_text, after_text):
    if not after_text:
        return []
    new_line_count = len(_split_lines(after_text))
    range_start = 1
    if before_text is not None:
        index = before_text.find(after_text)
        if index >= 0:
            range_start = before_text[:index].count("\n") + 1
    return [{"snippet": after_text, "rangeStart": range_start, "rangeEnd": range_start + new_line_count - 1}]
```

### 4.3 codeStatus 采纳判定（五态映射）

`ApprovalTracker` 从审批事件流维护四张有界记忆表，`adopted_code_status` 判定写类工具的采纳语义：

```python
# aixcoding/telemetry/reporters/ai_code.py:100-109
def adopted_code_status(self, call_id, session_id, *, rejected):
    if rejected:
        return CodeStatus.USER_REJECTED       # 4
    for request_id, approved in self._approved.items():
        if approved and self._request_call_id.get(request_id) == call_id:
            if self._request_mode.get(request_id) == _MODE_MANUAL:
                return CodeStatus.USER_APPROVED  # 5（manual 下用户批准）
            return CodeStatus.SUCCESS            # 1（judge/auto/bypass）
    return CodeStatus.SUCCESS                    # 1（无审批）
```

- 事件源：`ApprovalModeUpdated`（mode 真值源）→ `_modes`；`ApprovalRequest` → `_request_mode`/`_request_call_id`；`ApprovalResponse` → `_approved`（均由 `subscriber.py:85-107` 订阅分发）。
- **注意**：拒绝时工具不执行、无 mutation，本 reporter 不触发，`4` 实际落在 tool-detail update（M2，见 02 文档 §4.5）。
- **待定稿**：五态映射（§8-3）未获产品/后端确认，当前按方案 §4.3 值实现。已知偏差：`ApprovalResponse` 事件流不区分 user/judge 代答，以"请求时 mode=manual"近似用户批准。

### 4.4 payload 组装

```python
# aixcoding/telemetry/reporters/ai_code.py:145-174
payload = {
    "reportId": str(uuid.uuid4()),
    "filepath": _relative_filepath(path),        # git 相对路径，越界回退绝对路径
    "blocks": ai_code_blocks(before_text, after_text),
    "sourceType": "edit",
    "sessionId": event.session_id,
    "codeStatus": code_status,
    "spanId": event.origin.invocation_id,
}
request = resolve_call(event.provider_call_id, event.session_id)
if request is not None:
    payload["requestId"] = request[0]            # per-call registry 反查
...
payload.update({
    "remoteUrl": _git_field("git_remote"),
    "branch": _git_field("git_branch"),
    "gitUserName": _git_field("git_user_name"),  # git config user.name（M3 扩展）
    "gitUserEmail": _git_field("git_user_email"),
})
payload.update(common_fields())
self._add(payload)
```

- `_relative_filepath`：以 cwd 为基准算相对路径（`Path.resolve().relative_to`），越界回退原文。
- git 四字段 `remoteUrl/branch/gitUserName/gitUserEmail` 单独取（`git_user_name`/`git_user_email` 为 M3 对 `GitInfo` 的扩展，来自 `git config`）。
- `common_fields()` 复用与 tool-detail 同一收口（见 02 文档 §4.8）。

### 4.5 批量缓冲

`AiCodeReporter` 挂在 `BatchBuffer`（满 20 条或 10s flush，超 500 丢旧，对齐 pi-acp batchSize 机制）：

```python
# aixcoding/telemetry/subscriber.py:132-141
def default_ai_code_reporter():
    global _ai_code
    if _ai_code is None:
        from chrys.aixcoding.http import BatchBuffer
        from chrys.aixcoding.telemetry.reporters.ai_code import AiCodeReporter
        from chrys.aixcoding.telemetry.types import AI_CODE_SAVE
        _ai_code = AiCodeReporter(BatchBuffer(_shared_client(), AI_CODE_SAVE).add)
    return _ai_code
```

`BatchBuffer.add()` 惰性启动定时 flush（`_ensure_timer`），同步构造期无循环也能安全 add（`http.py:127-143`）；`flush()` 逐条 submit 到同一串行出口。

## 五、验证状态

- **单测级（已通过）**：`aixcoding/tests/test_ai_code.py` 13 项（blocks 四形态/采纳语义四路径/reporter 触发与跳过/订阅端到端）。
- **真链路（待验，plan.md M3 验收）**：write_file / edit_file mock SQLite 入库核对；审批批准/拒绝两路径 codeStatus 正确（5/4；judge+auto+bypass=1）。
- **前置待办**：codeStatus 五态映射产品/后端定稿（§8-3）。

## 附录：文件速查

| 内容 | 位置 |
|---|---|
| reporter / blocks / ApprovalTracker | `src/chrys/aixcoding/telemetry/reporters/ai_code.py` |
| 批量缓冲 | `src/chrys/aixcoding/http.py:103-165` |
| 双 reporter 分发 + 审批事件订阅 | `src/chrys/aixcoding/telemetry/subscriber.py` |
| codeStatus 枚举 | `src/chrys/aixcoding/telemetry/types.py:30-35` |
| git 四字段扩展 | `src/chrys/aixcoding/git_info.py`（`GitInfo.git_user_name/git_user_email`） |
| 公共字段收口 | `src/chrys/aixcoding/telemetry/reporters/__init__.py` |
