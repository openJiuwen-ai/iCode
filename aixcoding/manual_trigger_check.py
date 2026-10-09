# 手动触发工具上报的本地验证脚本（无需 LLM）。
#
# 模拟引擎侧发布 InvocationToolCallStart/Result（与 tool_events.py 同款事件），
# 走真实链路：EventBus → subscriber → ToolDetailReporter → TelemetryHttpClient
# 串行队列 → mock server（http://127.0.0.1:4321/csas）。
#
# 用法（先启动 mock：`uv run python aixcoding/telemetry-mock/server.py`）：
#
#     AIXCODING_EXTENSION_PROFILE=LOCAL uv run python aixcoding/manual_trigger_check.py
#
# 报文按 e65c05b 作者原版口径组装（update 用 funcId/funcErrorMessage、save 不带
# codeStatus、agentName 仅 desktop _meta 下行 functionName 时携带、渠道 argv 识别
# + ACP _meta desktop 覆盖），save 与 update 均可直接落 mock 正式表。核对方式：
#
#     curl -s "http://127.0.0.1:4321/debug/reports?interface=tool-detail/save"   | python3 -m json.tool
#     # 预期 count=3：3 条 save（read_file/write_file/run_command，codeStatus=0）
#     curl -s "http://127.0.0.1:4321/debug/reports?interface=tool-detail/update" | python3 -m json.tool
#     # 预期 count=5：3 条 PENDING(codeStatus=3) + 1 条 SUCCESS(1) + 1 条 USER_REJECTED(4)
#     curl -s "http://127.0.0.1:4321/debug/reports?interface=rejected" | python3 -m json.tool
#     # 预期 count=0
#     curl -s -X POST http://127.0.0.1:4321/debug/clear   # 重置
#
# 真实入口（需模型 profile）走同样链路：
#     AIXCODING_EXTENSION_PROFILE=LOCAL uv run icode run "读取 README.md 的前 20 行"
from __future__ import annotations

import asyncio
import logging
import sys

logging.basicConfig(level=logging.INFO, stream=sys.stdout)

from chrys.foundation.events.bus import EventBus  # noqa: E402
from chrys.foundation.events.types import InvocationToolCallResult, InvocationToolCallStart  # noqa: E402
from chrys.foundation.models.invocations import InvocationOrigin  # noqa: E402
from chrys.aixcoding.telemetry import subscriber  # noqa: E402

SESSION = "sess-e2e-manual-0001"
TURN = "inv-e2e-manual-0001"


async def main() -> None:
    origin = InvocationOrigin(kind="turn", session_id=SESSION, invocation_id=TURN, parent=None)
    bus = EventBus()
    subscriber.attach(bus)
    await asyncio.sleep(0.2)  # 等 create_task 的订阅注册完成

    cases = [
        ("call-e2e-001", "read_file", {"path": "/tmp/e2e.txt"}),
        ("call-e2e-002", "write_file", {"path": "/tmp/e2e_out.txt", "content": "hello\nworld\n"}),
        ("call-e2e-003", "run_command", {"command": "echo hi"}),
    ]
    for call_id, tool, args in cases:
        await bus.publish(
            InvocationToolCallStart(
                origin=origin,
                tool_name=tool,
                tool_kind="builtin",
                args=args,
                call_id=call_id,
                session_id=SESSION,
            )
        )
    # 成功与拒绝两条终态；run_command 不发 Result（模拟取消容忍路径）
    await bus.publish(
        InvocationToolCallResult(
            origin=origin,
            tool_name="read_file",
            call_id="call-e2e-001",
            result="file content...",
            duration_ms=11,
            metadata={},
            session_id=SESSION,
        )
    )
    await bus.publish(
        InvocationToolCallResult(
            origin=origin,
            tool_name="write_file",
            call_id="call-e2e-002",
            result="Error: permission denied",
            duration_ms=20,
            metadata={"approval": "user_rejected"},
            session_id=SESSION,
        )
    )
    client = subscriber._shared_client()
    await client.wait_drained(timeout=10)
    print("E2E-OK: 8 reports submitted (3 save + 5 update), serial queue drained")


asyncio.run(main())
