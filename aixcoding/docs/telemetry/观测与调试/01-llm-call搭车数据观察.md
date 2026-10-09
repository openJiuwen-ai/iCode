# llm-call 搭车数据观察（随大模型上送）

- 日期：2026-10-09
- 关联实现：[../实现方案/01-llm-call搭车上报实现.md](../实现方案/01-llm-call搭车上报实现.md)

## 一、通道特点

llm-call 的 `telemetry` 塞在**模型请求体** `body.telemetry` 里、发往**模型网关**（搭车通道），不经过 csas 后端——所以 `telemetry-mock` 收不到。观察它必须看真实模型请求的 HTTP 报文。

## 二、观察手段：`CHRYS_DEBUG_LLM_RAW_HTTP_LOG`

`service/llm/raw_http_log.py` 在 provider SDK 的 httpx 客户端上挂了 request/response hook，启用后把**完整原始 HTTP 请求体**（含 `telemetry`）落盘到 session 目录的 `llm_raw_http.jsonl`。

**关键：不需要真实大模型返回结果。** request hook 在请求发出的那一刻就落盘，响应是否成功/失败无关紧要。只需 iCode 能走到"发起一次模型请求"（配一个可用的模型 profile，哪怕是会失败的 endpoint 或真实内网网关）。

## 三、操作步骤（Windows）

```cmd
:: 1. 启动前设置环境变量（apply=RESTART，必须在启动 iCode 前设好）
set CHRYS_DEBUG_LLM_RAW_HTTP_LOG=1

:: 2. 在 git 仓库目录下，headless 发一条请求（-a 指定 agent，必填）
uv run icode run "解释一下这个仓库是做什么的" -a Code
```

## 四、落盘位置与格式

- 位置：`%APPDATA%\chrys\sessions\<session_short_id>\llm_raw_http.jsonl`（Windows；macOS/Linux 为 `~/.chrys/sessions/...`）
- 格式：JSONL；每条 `event=request` 的记录含 `request.body.json`（解析后的请求体），`telemetry` 就在 `request.body.json.telemetry`。

定位文件：

```powershell
Get-ChildItem "$env:APPDATA\chrys\sessions" -Recurse -Filter llm_raw_http.jsonl | Select-Object FullName
```

## 五、核对字段

```powershell
# 列出所有 session 日志里含 telemetry 的行
Get-Content "$env:APPDATA\chrys\sessions\*\llm_raw_http.jsonl" | Select-String -Pattern '"telemetry"'
```

`telemetry` 应含（有值才出现）：`requestId` / `sessionId` / `spanId` / `eventType`(=`"llm"`) / `eventSubType`(=`"agent"`|`"system"`) / `channelType` / `channelName` / `pluginVersion` / `projectName` / git 五件套（`gitRemote`/`gitBranch`/`gitRevision`/`gitOwner`/`gitRepo`）。

## 六、注意事项

- **RESTART**：`CHRYS_DEBUG_LLM_RAW_HTTP_LOG` 是 RESTART 级设置，必须在进程启动前设置，运行中改不生效。
- **不脱敏**：日志含 API key、完整 prompt、工具参数、模型响应，勿外发。
- **`spanId` 非空**即同时验证了 `current_trajectory()` 在 middleware 执行期绑定（方案 §8-5①）。

## 七、单测级对照（不依赖真实模型）

`aixcoding/tests/test_llm_telemetry.py`（12 项）直接构造 middleware，断言 `context.options["extra_body"]["telemetry"]` 的内容——但只验证 middleware 注入，**不覆盖**"SDK 把 `extra_body` merge 进请求 body 顶层"这一步；wire 级贯通靠本文件的方式验证。
