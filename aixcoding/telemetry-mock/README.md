# telemetry-mock：csas 上报后端 mock（Python 标准库零依赖）

模拟 csas telemetry 上报后端，供 iCode 数据上报（`src/chrys/aixcoding/`）开发联调与
pytest 集成测试复用。从 agent_studio_new 的 TS 版 `apps/telemetry-mock` 重写，
行为契约对齐；**不进 wheel**（hatch 只打包 `src/chrys`），不进 pyproject 依赖组。

- 方案依据：`aixcoding/docs/telemetry/iCode-数据上报直改源码方案.md` §六
- 相对 TS 版的差异（按方案决策）：清理 rev.5 残留列（`turn_content_hash` /
  `analysis_version` / `latest_update_json`）及其版本头幂等机制——mock 的职责是
  观测收到的报文，**全部裸插入**；校验失败 problem 文本为自定格式（非 zod 原文）。

## 启动

```bash
uv run python aixcoding/telemetry-mock/server.py            # 默认 127.0.0.1:4321，:memory:
uv run python aixcoding/telemetry-mock/server.py --port 5000 --db mock.db --require-token secret
```

参数：`--port` / `--db` / `--require-token` / `--quiet`；
环境变量：`CHRYS_TELEMETRY_MOCK_PORT`（默认 4321）、`CHRYS_TELEMETRY_MOCK_TOKEN`。
仅允许绑定 loopback。`src/chrys/aixcoding/config.py` 的 LOCAL profile 默认指向
`http://127.0.0.1:4321/csas`。

pytest 内复用（factory，进程内起停）：

```python
from server import start_telemetry_mock

server = start_telemetry_mock(port=0, database_path=":memory:", require_token="t")
# server.origin / server.database_path / server.store / server.close()
```

（`aixcoding/tests/conftest.py` 已把本目录加入 `sys.path`，可直接 `import server`。）

## csas 端点（`/csas/telemetry/api/v1/` 前缀，POST）

| 端点 | 必填 | 说明 |
|---|---|---|
| `tool-detail/save` | `funcType`(非负 int)、`funcName`(1..256) | 单条工具上报（`productName` 真实契约可选——aixcoding-continue 不下发，2026-10-09 放宽） |
| `tool-detail/batch-save` | 顶层数组 ≥1；元素 `funcType` + `funcName`(1..256) | 批量 |
| `tool-detail/update` | `funcId`(1..512)、`codeStatus`(非负 int) | 执行状态回写 |
| `ai-code/save` | `reportId`(1..256)、`sourceType`(1..64)、`blocks` ≤64（元素必填 `rangeStart`/`rangeEnd`） | AI 生成代码 |
| `event-reaction/save` | 无（任意 JSON 宽收） | 预留 |

- 鉴权：启动设 `--require-token` 时，请求头 `token`（裸名，非 Bearer）须精确相等，否则 `401`。
- 成功 envelope 两种：ai-code 用 `{"code":200,"message":"success","data":{"reportId":...}}`；
  其余用 `{"success":true,"message":"保存成功","code":200,"timestamp":<ms>,"result":null,"e":null}`。
- 校验失败：落 `rejected_reports` 表后回 400（`e:"invalid_request"`，`message` 为 problem 文本）。
- 请求体上限 8 MiB（超限 413 并断连）；非 JSON 400。
- 未知字段放行（passthrough）。

## 观测端点

| 端点 | 功能 |
|---|---|
| `GET /health` | 存活检查 |
| `GET /debug/view`（及 `/`） | HTML 观察页：6 区块列表、过滤、点击展开原文、2s 自动刷新、清空 |
| `GET /debug/reports?interface=&sessionId=&funcId=&limit=` | 查询落库记录（`interface` 必填，含 `rejected`；`limit` 1..500 默认 100） |
| `GET /debug/dump` | 全表导出（六键，每表至多 500 条） |
| `POST /debug/clear` | 清空全部表 |
| `POST /debug/faults` | 运行期切换故障注入，返回当前配置 |

## 故障注入

`POST /debug/faults`，body：

```json
{"mode": "http500", "rate": 1.0, "slowMs": 2000, "interfaces": ["tool-detail/save"]}
```

| 模式 | 行为 |
|---|---|
| `http500` / `http503` | **先落库**再回 `500/503 {"error":"<mode>"}` |
| `slow` | 延迟 `slowMs` 后正常落库响应 |
| `envelope_reject` | **先落库**再回 HTTP 200 业务失败 envelope（`e:"BusinessException"`；ai-code 为 `code:500`） |
| `drop_body` | 不落库，直接断连（客户端收到连接重置/EOF） |

`rate`（0..1，确定性伪随机按毫秒抽样）、`interfaces`（完整路径或短名定向）可选；
`{"mode":"none"}` 复位。http500/503/envelope_reject/slow 均落库——验证客户端吞错链路时
可核对“报文已收到”。

## 存储

SQLite（默认 `:memory:`，`--db` 指文件）：每接口一表 + `rejected_reports`，
均保存 `body_json` 原文与关键提取列（`session_id` / `func_id` / `code_status` /
`item_count` / `block_count` 等）。表名见 `store.py::INTERFACE_TABLES`。

## 注意

- mock 只覆盖独立 HTTP 通道；llm-call 搭车通道（`body.telemetry` 随模型请求发往
  模型网关）mock 收不到——其观测靠 `CHRYS_DEBUG_LLM_RAW_HTTP_LOG` 与 debug 落盘。
- 不支持 chunked 请求体（客户端请带 `Content-Length`；httpx/urllib 默认满足）。
