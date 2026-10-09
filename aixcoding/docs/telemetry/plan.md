# 数据上报实施计划（M1-M4）

- 日期：2026-10-08
- 决策依据：[iCode-数据上报直改源码方案.md](iCode-数据上报直改源码方案.md)（下称"方案"）——全部设计决策、代码位置、字段映射以方案为准，本文只跟踪任务与状态，不复述
- 验证命令：`uv run python scripts/chrys_test.py --smart --paths <本次改动文件>`（源码与 `tests/` 改动后必跑）
- ⚠ 流程约定：**"源码修改清单"（本文档章节）中每一项，动手前必须先向用户列出"文件 + 改动 + 行数"并获得确认**；`src/chrys/aixcoding/`、`tests/aixcoding/`、`aixcoding/`（根目录专区）内新增文件无需确认

## 状态总览

| 里程碑 | 目标（方案 §九） | 状态 |
|---|---|---|
| M1 | aixcoding 包骨架 + llm-call 搭车 + per-call registry + telemetry-mock | **进行中**（代码 2026-10-08 全落；**2026-10-09 真链路验收大半通过**：spanId 绑定/registry 贯通含流式/LOCAL→mock 对接/raw log 核对；余：TUI/ACP→真实网关查库（§8-2）、sub-agent 用例、ACP 审批用例） |
| M2 | subscriber + tool_detail（含输入触发）+ 装配 + ACP `_meta` 双向 | **进行中**（代码 2026-10-08 全落；**2026-10-09 真链路验收**：四形态之三（成功/异常/审批拒绝）+ 时长/错误分类/行数 + workspace 贯通（源码 #6）；余：超时形态、skill 引用触发、故障注入、`_meta` 回传联调） |
| M3 | ai_code reporter + codeStatus 五态映射 | **进行中**（代码 2026-10-08 全落；**2026-10-09**：write_file 真链路入库首验通过 + ai_code.py 三处潜伏 bug 修复（tuple/spanId/git 基）；余：edit_file 补验、审批批准两路径 codeStatus、§8-3 定稿） |
| M4 | 登录对接 + 哨兵测试 + 实现落地记录 | 未开始 |

## 外部确认项（方案 §八，与开发并行推进，不阻塞启动）

- [ ] channelType 枚举值——后端（§8-1，M1 期间推进）
- [ ] 网关解析 telemetry 实测（§8-2，M1 内执行）
- [ ] codeStatus 五态映射——产品/后端（§8-3，M3 前定稿）
- [ ] 后端 spanId 口径（§8-5③）
- [ ] 字段超集容忍度——后端（§8-6）

## 源码修改清单（追版本唯一成本；⚠ 每项动手前需用户逐项确认）

按实施里程碑排序（M1 = #1、#5；M2 = #2、#3、#4），编号见方案 §五。

| # | 文件 | 改动（方案 §五） | 里程碑 | 状态 |
|---|---|---|---|---|
| 1 | `service/llm/instrumented.py` | `_compose_client_stack` 加 `session_id` 形参 + 注入 `AixTelemetryMiddleware`；3 个工厂调用点传参（~8 行） | M1 | 未开始 |
| 5 | `service/agent_middleware/events/tool_events.py` | Start（L391）/Result（L689）发布补 `provider_call_id` 参数（2-4 行；registry 贯通必选，值在 L285 已取得） | M1 | 未开始 |
| 2 | `orchestration/engine/assembly.py` | `assemble_agent_engine` 里调 `subscriber.attach(event_bus, ...)`，per-bus 幂等（~3 行） | M2 | 未开始 |
| 3 | `orchestration/engine/run/`（input_refs 调用方） | skill 引用解析命中时调 `recorders.record_invocation(...)`，不改纯函数本体（~3 行） | M2 | 未开始 |
| 4 | `app/acp/server.py`（+ `bridge.py`） | 读 prompt `_meta` envelope 存 channel context；响应回传 telemetry `_meta`（~8 行） | M2 | 未开始 |
| 6 | `foundation/events/types.py` + `tool_events.py`（2 处）+ `sub_agent_events.py`（4 处）+ `instrumented.py`（`_compose_client_stack`+3 工厂）+ `clients.py`（`create_client`+`stack_kwargs`）+ `build/builder.py`（`create_client` 调用传 `runtime.cwd`） | 工具事件与 llm 搭车 middleware 携带**会话工作区**（`workspace_cwd`=`SessionEnvironment.cwd`，workspace 优先/启动目录兜底）——projectName/git 五件套/fileName 相对化的取值基（用户确认 2026-10-09，完整级别） | M2 | 已实施 2026-10-09（Smart Test 14869 过；`test_openai_chat_stream_assembly.py` 替身签名同步 +1 行，同 #1 附带先例） |

## M1

### 新增文件（无需逐项确认）

- [x] `src/chrys/aixcoding/__init__.py`
- [x] `src/chrys/aixcoding/config.py`——profile/URL/token（方案 §七）
- [x] `src/chrys/aixcoding/http.py`——统一出口：吞错、串行队列、批量缓冲
- [x] `src/chrys/aixcoding/git_info.py`——git 五件套 + TTL 缓存
- [x] `src/chrys/aixcoding/context.py`——channel 三元组 + userId provider 接口
- [x] `src/chrys/aixcoding/telemetry/__init__.py`
- [x] `src/chrys/aixcoding/telemetry/types.py`——payload 结构、csas 端点常量、枚举
- [x] `aixcoding/telemetry-mock/server.py`——`start_telemetry_mock(options)` factory + `__main__` CLI
- [x] `aixcoding/telemetry-mock/store.py`——SQLite 原文落库（每接口一表 + rejected 表）
- [x] `aixcoding/telemetry-mock/faults.py`——5 模式故障注入（http500/http503/slow/envelope_reject/drop_body）
- [x] `aixcoding/telemetry-mock/README.md`
- [x] `aixcoding/tests/test_telemetry_mock.py`——起停 / 5 端点 / 观测端点 / 故障注入（factory import 驱动，无需改 chrys_test.py）
- [x] `aixcoding/tests/test_infra.py`——config/http/git_info/context/types 测试（含 LOCAL profile 对接 mock 端到端）
- [x] `aixcoding/tests/test_llm_telemetry.py`——payload 注入/registry 语义（精确命中→session 级 fallback→上限淘汰）/spanId 确定性/流式终结时序/side call 不关联/总开关透传/ChatMiddlewareLayer 端到端（12 项）
- [x] `src/chrys/aixcoding/telemetry/llm_telemetry.py`——`AixTelemetryMiddleware`（搭车 + per-call registry，方案 §4.1）+ `build_telemetry_middleware` 装配入口（吞装配异常降级）+ `resolve_call` 反查 API（M2 subscriber 用）

> 测试目录（2026-10-08 用户确认调整）：定制测试放仓库根 **`aixcoding/tests/`**（专区自包含、不侵入上游 `tests/` 白名单），不继承 `tests/conftest.py`——专区 conftest 自建 config_dir 隔离 / mock sys.path 注入 / `git_repo` fixture。pytest `testpaths` 只含 `tests/`，须显式运行：`uv run --extra all pytest aixcoding/tests`；Smart Test 与上游 CI 不自动收集（架构守卫除外——`src/chrys/aixcoding/**` 改动仍触发 `tests/architecture` watch）。

### 源码修改

- [x] 源码 #1（已确认 2026-10-08，同日实施）：`_compose_client_stack` 加 `session_id` 形参 + 注入 `build_telemetry_middleware(session_id)`；3 个工厂调用点补传参（合计 11 行，惰性 import，装配异常在 aixcoding 侧吞掉降级）。**附带产物**：上游测试 `tests/service/llm/test_openai_chat_stream_assembly.py` 的 `_checked_stack` 替身签名同步 `session_id` 形参（+1 行，用户确认 2026-10-08）
- [x] 源码 #5（已确认 2026-10-08，同日实施）：Start/Result 两处发布补 `provider_call_id=provider_call_id`（2 行；`_process_tool_call` 形参作用域内直接可用）
- [x] **附带修复（已确认 2026-10-08，同日实施）**：`service/llm/clients.py` 的 `stack_kwargs` 补 `"session_id"`（+1 行，置于 mock 分支之后——`MockChatClient.__init__` 不收该参）。真链路方案推演时发现：`create_client` 的调用方均正确传 `session_id`，但它只进了请求头（`_build_default_headers`）、不进 `stack_kwargs` → instrumented 工厂恒收 `None` → middleware 真实运行恒 disabled（单测直构 middleware 故未暴露）。Smart Test 12224 项通过；另 2 项失败为环境噪音（CodeBuddy safe-delete shim 对 Windows `nul` 设备名 `unlink` 误伤 `test_file_scanner.py` 两用例，断言本身已过、仅清理阶段被拦截，与改动无关）
- [x] **架构注册（已确认 2026-10-08，同日实施）**：`tests/architecture/test_layering.py` 加 `AIXCODING = "aixcoding"` 常量 + `TIER_ORDER` 条目 `tier=1`（仅许 import kernel/foundation，恰与方案 §六分层约束一致；service/orchestration/app 向下引用合法），共 6 行。测试移入 `aixcoding/tests/` 后 `test_test_layout.py` 白名单改动不再需要（tests/ 树零侵入）。Smart Test 495 项全绿验证通过。

### 验收（方案 §九 M1 行展开）

- [x] mock 自测通过（含 `/debug/view` 观察页、`/debug/faults` 运行期切换）——41 项测试全绿（test_telemetry_mock.py 25 项 + test_infra.py 16 项）；CLI 直跑冒烟通过
- [ ] TUI / ACP 各发一条模型请求 → 网关查库核对（搭车通道，§8-2 实测）
- [x] `current_trajectory()` 在 middleware 执行期绑定验证（spanId 来源，§8-5①）——**2026-10-09 CLI headless 实测通过**（`CHRYS_DEBUG_LLM_RAW_HTTP_LOG=1` + `uv run icode run "<prompt>" -a Code` → `llm_raw_http.jsonl` 落盘核对：15 字段全齐、spanId 非空、telemetry 已 merge 进 body 顶层（wire 级贯通）；步骤见 `观测与调试/01-llm-call搭车数据观察.md`）
- [x] `provider_call_id` 补填后 registry 贯通：含流式链路——**2026-10-09 真链路实测通过**（deepseek 流式：同 turn 3 轮 LLM 调用各得独立 requestId、并行 2 个 glob 共享同一 requestId=同响应双 function call、spanId 与 `llm_raw_http.jsonl` telemetry.spanId 同值；session ca4a2be7）
- [ ] sub-agent 用例：工具关联到 sub-agent 那次 LLM 调用的 requestId
- [ ] ACP 形态审批用例：agent_studio_new 回流下 `resolved_by_event`=True，decider=USER
- [x] LOCAL profile 指向 mock 可对接（`http://127.0.0.1:4321`）——**2026-10-09 实测通过**：① mock CLI 起停 + `/debug/view` 观察页可用（展开状态跨自动刷新已修）；② `AIXCODING_EXTENSION_PROFILE=LOCAL` 端到端证明（mock 实收报文，强于配置核对）；③ raw log 落盘核对完成（含 spanId 非空验证 §8-5①）。搭车报文发往模型网关、mock 收不到——其观测靠 raw log（文档 01）

## M2

### 新增文件

- [x] `src/chrys/aixcoding/telemetry/subscriber.py`——`attach()` 幂等装配 + 分发（**实现偏差**：方案写 `bus.stream()`，但 `assemble_agent_engine` 在 TUI 路径是无事件循环的同步上下文，stream 消费循环无法同步启动——改用 `bus.subscribe()` 回调式，handler 仅入队+轻计算、事件零丢失语义不变；注册双分支：loop 内 `create_task` / 无 loop `asyncio.run` 一次性）+ `record_skill_invocation`（源码 #3 调用点）+ 进程级共享 reporter（串行队列单例，save→update 天然保序）
- [x] `src/chrys/aixcoding/telemetry/outcome.py`——工具终态分类：直接复用 foundation `tool_result_metadata_*` 判定（无需抄写 service 层 `tool_outcome`）；M2 三值映射：成功=1 / 失败=2（timeout|error）/ 拒绝=4，五态定稿待 §8-3
- [x] `src/chrys/aixcoding/telemetry/reporters/__init__.py`——公共字段收口 `common_fields()`（channel/git 五件套/pluginVersion/projectName/userId）
- [x] `src/chrys/aixcoding/telemetry/reporters/tool_detail.py`——save（含白名单 value：`read_file`→filepath、`load_skill`→skill_name；full 模式全量 JSON 截 2000）+ update（PENDING→终态）+ difflib 行数（`metadata["file_snapshot"]` 的 before/after_text）+ `record_invocation`（输入触发单条 save）
- [x] `src/chrys/aixcoding/telemetry/acp_meta.py`——ACP `_meta` envelope 解析/组装（键与校验对齐 pi-acp：`agent-studio.dev/ide-name|ide-version|telemetry`，schemaVersion=1）
- [x] `aixcoding/tests/test_tool_detail.py`——分类/映射/白名单/行数/报文/保序/装配幂等/开关（16 项）
- [x] 附带：`context.py` 公共化 `plugin_version()`（llm_telemetry 私有实现改为复用）

### 源码修改

- [x] 源码 #2（已确认 2026-10-08，同日实施）：`assembly.py` `bus = event_bus` 后插 import + `subscriber.attach(bus)`（3 行，函数内 import）
- [x] 源码 #3（已确认 2026-10-08，同日实施）：`runner.py` + `active_injection.py` 两处 `_skill_reference_reminder` 命中后插 `record_skill_invocation(reference.skill.name, self._session.session_id)`（各 3 行）；**`retry.py` 第三处同形方法不挂**（重试是同一文本重放，避免双计）
- [x] 源码 #4（已确认 2026-10-08，同日实施）：`acp/server.py` prompt() 开头读 `_meta`（`read_ide_channel_meta(kwargs)`，原 `_ = kwargs` 占位随移除）+ EndTurn/Cancelled 两处 `PromptResponse` 补 `_meta=telemetry_response_meta(session_id)`（合计 +5 行）；**`bridge.py` 的 session/update 流式 `_meta` 暂不做**（prompt 响应回传即可验收，需要流式关联再补）

### 验收

- [x] 成功 / 异常 / 超时 / 审批拒绝四形态入库正确（mock SQLite 核对）——**2026-10-09 真链路验三**：成功（read_file/glob/write_file codeStatus=1）、异常（读不存在文件 codeStatus=2 + failureType=error + funcErrorMessage 全路径）、审批拒绝（pwsh 被拒 codeStatus=4 + "Tool execution was rejected by user."，session b6ffbc04）；**超时形态未真链路构造**（单测级已覆盖， tolerated）
- [x] 时长、错误分类、写类工具行数（difflib）正确——**2026-10-09 真链路实测通过**（executionDurationMs 合理：read_file 11-13ms / glob 647-658ms；failureType error/rejected 正确；创建行数 0/178/0 精确符合三态规则）
- [ ] skill 引用触发入库（单条 save，funcType=0）——单测级已验证，真链路待验
- [ ] `_meta` 回传被 agent_studio_new 收到——待验（agent_studio_new 侧联调）
- [ ] 故障注入下上报吞错（后续可自动化为 pytest 集成测试：起 mock → 跑引擎 → 断言落库）
- [x] 取消的工具调用孤儿 save 对策落地或明确容忍（§8-4）——**明确容忍**：`CancelledError` 不发 Result → save 停 PENDING，不补发（reporter 注释与方案 §8-4 记录在案）

## M3

### 新增文件

- [x] `src/chrys/aixcoding/telemetry/reporters/ai_code.py`——blocks difflib 计算（整文件单 block，rangeStart 定位旧文新文本首行/回退 1，**对齐 pi-acp ``aiCodeBlocks`` 语义**）+ `ApprovalTracker`（codeStatus 采纳判定：订阅 mode/approval 事件，manual 下批准=5、auto 下 judge=1、无审批=1、拒绝=4）+ `AiCodeReporter`（BatchBuffer 批量：满 20 条或 10s flush）
- [x] `aixcoding/tests/test_ai_code.py`——blocks 四形态/采纳语义四路径/reporter 触发与跳过/订阅端到端（13 项）
- [x] 附带：`git_info.py` 的 `GitInfo` 加 `git_user_name`/`git_user_email`（git config）；`http.py` 的 `BatchBuffer.add` 惰性启动定时 flush（TUI 同步构造期无循环可用）

### 前置

- [ ] codeStatus 五态映射经产品/后端确认定稿（§8-3）——**未定稿，按方案 §4.3 值实现**（拒绝=4 / manual 批准=5 / judge+auto+bypass+无审批=1，附在 ai-code 报文 `codeStatus` 字段；拒绝时工具不执行无 mutation，4 落在 tool-detail update（M2））；已知偏差：ApprovalResponse 事件流不区分 user/judge 代答，以"请求时 mode=manual"近似用户批准

### 验收

- [x] write_file / edit_file 入库核对（mock SQLite）——**2026-10-09 真链路 write_file 通过**（ai_code_saves 首条：reportId/filepath 工作区相对路径/blocks 178 行块，session b6ffbc04；当日顺带修复 ai_code.py 三处潜伏 bug：tuple getattr/spanId 错位/git cwd 基）；edit_file 路径待真链路补验
- [ ] 审批批准 / 拒绝两路径 codeStatus 正确（5 / 4；judge+auto+bypass=1）——**部分真链路验证（2026-10-09）**：拒绝路径 tool-detail codeStatus=4 已实测；批准=5 / judge+auto=1 的 ai-code codeStatus 路径待 ACP/审批场景补验

## 评审处理记录（2026-10-08，外部 AI 评审 5 项）

1. **function-name/agentName 缺失（已补）**：方案 §4.5 的三 envelope 少实现了 `agent-studio.dev/function-name`——`acp_meta.py` 补键与读取（独立于 ide-name，无则清除旧值），`context.py` 加 `set_current_function_name`/`current_function_name`（进程级最近值近似），tool-detail save（含 record_invocation）与 ai-code payload 补 `agentName` 字段；测试 +3（envelope 读取/agentName/响应 meta roundtrip）
2. **`except A, B:` 裸形式（不改）**：项目 `ruff format` 以 py314 为目标强制 PEP 758 裸形式——M2/M3 两次实测 format 主动把带括号写法改写为裸形式，加括号会被 format gate 打回；语义等价（解析为元组），维持工具规范形态
3. **无界内存增长（已修）**：`reporters/__init__.py` 加 `remember_bounded`（容量 1024 丢最旧），`ApprovalTracker` 四表、`ToolDetailReporter._started_at`、`AiCodeReporter._args_by_call` 全部接入——取消等无终态残留同样被淘汰；测试 +1
4. **channel/function_name 进程级全局覆盖（记录在案）**：iCode ACP 常驻多 session 时最后下发者赢——单 agent_studio_new 客户端场景 ideName/functionName 恒定无实际差异；`context.py` 的 `set_desktop_channel` docstring 已注明近似语义与 per-session 化路径；若将来多客户端并发接入需改为 session 级 channel context
5. **验证边界（确认）**：真链路验收项未勾选状态与 plan.md 记录一致，属已知未完成项而非缺陷

## M4

- [ ] userId 登录对接：`aixcoding/auth/` 子包注册真实数据源，provider 接口不变（方案 §4 决策 #4）
- [ ] 哨兵测试（§8-7）：ChatMiddlewareLayer 构造参数、事件字段、metadata 键、mutation 摘要键、`_meta` envelope 名；另加 `foundation.platform.process._windows_hidden_subprocess_kwargs`（`git_info.py:18` 引用的 foundation 下划线私有成员——现有 kernel-private 守卫只盯 `chrys.kernel._` 前缀不覆盖 foundation，上游改签名时会静默断，2026-10-08 评审指出）
- [ ] **实现落地记录**：在方案文档末尾追加 as-built 章节（实际改动文件清单 + 验证结果）——独立 design.md 暂不建，仅当 telemetry 子包演化为多模块复杂系统时再拆
- [ ] Smart Test + 契约测试通过

## 使用方式

- 每次开发会话从本文件开始：看状态总览 → 当前里程碑找未完成任务
- 完成即勾选；状态变化同步"状态总览"表；源码修改项获用户确认后在对应行注明"已确认（日期）"
