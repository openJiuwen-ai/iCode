# iCode 特供版：事实数据上报（report/1.0）—— 客户端设计

状态：设计稿（未实施）。
范围：本仓库（icode-zju fork）客户端侧。
配套文档：[server-acceptance.md](./server-acceptance.md)（服务端需求与验收测试）。

> 修订记录（第二版，取代 2026-11 初稿）
> - 取消 OTLP：传输改为 HTTPS 批量，帧格式与本地队列同构（§4.1）。
> - 取消"每轮全量快照"：改为流式分块 + 只传变化块（§5.3）。
> - 语义下沉：窗口切分、索引抽取、代际派生全部由服务端在完整流上完成，客户端不解析任何 teams 语义（§5.2、§6）。
> - 取消 `control` 记录族与 prefix digest 游标推断：由 manifest 端点与帧头 `reset` 标志替代（§5.6、§5.8）。
> - 工作区镜像（IGWS）进入 v1 且默认开启（决策 D5，理由见 §13）。
> - 目录名 `otel-reporting/` 为历史名称，内容已不含 OTLP。

---

## 1. 背景、约束与目标

### 1.1 硬约束

1. **客户端只有 iCode 进程本身**：不存在 teams 那样的外置观察者进程/插件。采集、入队、发送、重试必须在 iCode 生命周期内完成。
2. **服务端是独立服务**：与 teams 数据平台严格分开部署，不共享进程、不共享数据库连接、不依赖 teams 的服务端组件。
3. **落盘数据与 iCode teams 版同构**：OBS 内容寻址键布局、对象种类、PG 表结构与行键、事件索引白名单字段、IGWS 容器字节格式保持一致，使下游消费端无需改动即可消费本通道的数据（判定口径见决策 D1）。

### 1.2 优先级（本设计的裁决顺序）

1. **数据完整性**：不丢、不重复、可逐字节重建。
2. **用户使用体验**：不阻塞事件循环、不阻塞 turn、不制造可感知卡顿、内存与磁盘有界。
3. **与 teams 同构**：在满足 1、2 的前提下追求；为此可以放弃"与 teams 对同一输入产出逐字节相同的窗口对象"。

任何与上述顺序冲突的设计选择都按下位处理，例如：为保证与 teams 分窗逐字节一致而引入轮询驱动的窗口边界、或为省一次读盘而在 hot path 上做哈希，都不接受。

### 1.3 决策记录

| 编号 | 决策 | 取值 | 影响 |
|---|---|---|---|
| D1 | 同构口径 | **表级 + 对象级同构**：OBS 键布局、PG 表/行键/白名单字段一致；分窗规则为确定性规则，不保证与 teams 对同一输入 sha 相同 | 下游按行内记录的对象 sha 取对象，不受影响；需要"重切窗比对"的下游需另行确认（差异清单见服务端文档 §5） |
| D2 | 同构语义位置 | **服务端统一派生**：客户端只搬原始字节与原始事件行 | 客户端约 1.2k 行；窗口边界不受采集时机影响；规则修复不需发版 |
| D3 | 传输 | **HTTPS 批量（httpx）+ 自定义帧** | 无 base64 膨胀；可实现缺块协商；无 OTel SDK 私有依赖 |
| D4 | 新鲜度 | **每轮保存后立即上报 + turn 活跃期最长 60 s 补报** | 崩溃最多丢 60 s 的轨迹行；常驻唤醒可控 |
| D5 | 工作区与环境 | **进入 v1，默认开启**：仓库脏树（逐文件块 + manifest，服务端拼 IGWS）+ `agent.env`；忽略文件只报清单 | 同时支撑接受率与**数据集复现**；是唯一读取用户源码的通路，需显式告知 |

### 1.4 非目标（v1）

- git 侧事实：`adoption-evidence/1.0`、`commit/2.0`、reflog 结算（需要 commit 观察，见 §13 开放问题）；
- 回滚正文单元：`session-rollback/1.0`、`session-superseded/1.0`、回滚 code-change diff 正文；
- `localAssociations.put` / `similarityCalibration.put`（双工申报）；
- 任何形态的服务端下发策略（单向 push）。

## 2. 设计原则

1. **字节即真相**：只上报本地文件与事件行的原始字节，不做语义重编码。服务端拼接 + sha256 校验即可逐字节重建。
2. **客户端无 teams 语义**：客户端不解析事件行、不切窗口、不判完整性、不推断代际。它只知道"哪些字节还没送到"。唯一的索引型载荷是 `session.meta`，其值来自进程内已有值（§5.3.2），不解析 `session.json`。
3. **事件循环零工作**：所有磁盘 IO、哈希、编码、网络都在专用工作线程；EventBus 处理器只置位唤醒。
4. **有界资源**：内存按块（256 KiB）、磁盘按字节上限、线程数固定为 1。
5. **幂等由内容派生**：单元键只从 `kind + session + scope + payload sha256` 派生，不掺入进程/运行实例标识；重开、换机、重装后自然衔接。
6. **失败不影响主流程**：上报子系统的任何异常都被吞掉并计入本地诊断计数，绝不影响 turn 执行、会话保存或 TUI 渲染。

## 3. 总体架构

```
trajectory writer ──▶ trajectory/events.jsonl ──┐
                                                │  ┌─────────────────────────────────┐
session store ──────▶ sessions/<id>/session.json ┼─▶│ service/reporting（内容采集）     │
                                                │  │  TrajectoryTailer（offset 续读）  │
git（只读命令）────▶ IGWS 镜像 / 仓库身份 ──────┘  │  SnapshotTap（分块增量）          │
                                                   │  WorkspaceCapture（IGWS）          │
                                                   │  RepositoryIdentity                │
                                                   │  SessionMetaSignal（保存钩子）      │
                                                   └───────────────┬─────────────────┘
                                                                   │ 帧（header + payload）
                                                   ┌───────────────▼─────────────────┐
                                                   │ foundation/reporting（纯传输）    │
                                                   │  DurableWAL（追加文件 + ack 偏移） │
                                                   │  ReportPump（单线程，HTTPS 批量）  │
                                                   │  ManifestReconciler               │
                                                   └───────────────┬─────────────────┘
                                                                   │ POST /v1/batch
                                                                   ▼
                                                            独立上报服务
```

分层（符合仓库 layering DAG）：

| 模块 | 层 | 职责 |
|---|---|---|
| `foundation/reporting/`（新包） | foundation | 纯传输，内容无关：帧编解码、`DurableWAL`、`ReportPump`、`ManifestReconciler`、上限与丢弃证据 |
| `service/reporting/`（新包） | service | 内容采集：tailer、snapshot tap、workspace capture、repository identity、会话生命周期绑定、meta 组装 |
| `orchestration/startup.py` | orchestration | bootstrap 新增一步（enabled 时在首帧之后启动，见 §5.1） |
| `service/session/persistence.py` | service | 新增一个**仅传原始值**的保存钩子（§5.3.2） |

## 4. 上报单元（wire contract）

### 4.1 传输与帧格式

- 协议：HTTPS，`httpx`（复用仓库既有的客户端所有权模式与代理/TLS 处理）。
- 端点：
  - `POST /v1/batch` —— 批量上报单元；
  - `POST /v1/session/{session_id}/manifest` —— 缺块协商（双向上报，见 §5.8）。
- 请求体 = 若干**帧**顺序拼接（`Content-Type: application/vnd.chrys.report.frames`；大于 32 KiB 时 `Content-Encoding: gzip`，zlib/stdlib）：

```
frame := u32BE(header_len) | header_json(UTF-8) | payload(payload_len bytes)
```

`header_json` 字段：

| 字段 | 说明 |
|---|---|
| `v` | 帧版本，当前 `1` |
| `kind` | 单元类型，见 §4.2 |
| `unit_id` | `sha256(kind \| session_id \| scope \| payload_sha256)` 的 64 位十六进制；服务端幂等键 |
| `session_id` | 会话 UUID；与会话无关的单元为空串 |
| `scope` | 幂等作用域，如 `lines:1201-1450`、`snapshot:3`、`wsfile:<sha>:0` |
| `payload_sha256` | 载荷 sha256 |
| `payload_len` | 载荷字节数 |
| `created_at` | RFC3339 UTC，仅诊断 |
| `reset` | 仅 `trajectory.lines`：本批为客户端偏移失效后的重读（§5.2） |

**WAL 里存的就是这些帧，逐字节相同**：`DurableWAL` 追加的字节流与 `POST /v1/batch` 的请求体是同一格式，Pump 只需按字节上限切出连续帧，不做重编码。

### 4.2 单元类型

| kind | payload | scope | 用途 |
|---|---|---|---|
| `trajectory.lines` | 原始事件行按 `\n` 连接（每行以 `\n` 结尾，与文件字节一致）；≤1 MiB | `lines:<first>-<last>` | 轨迹行原文，服务端切窗与重建的唯一素材 |
| `snapshot.chunk` | `session.json` 原始字节切片（256 KiB/块） | `chunk:<content_hash>:<index>` | 快照字节 |
| `snapshot.manifest` | JSON：`{content_hash,size,chunk_size,chunks:[sha...]}` | `manifest:<content_hash>` | 快照清单与完整性锚点 |
| `session.meta` | JSON：`session/1.0` 白名单（§4.3） | `meta:<content_hash>` | `sessions` 行 |
| `workspace.manifest` | JSON：IGWS manifest（`workspace-state/1.0`：三段条目含 `path/status/mode/content{offset,length,sha256}`、`omitted[]`、`ignored{count,truncated,entries[]}`、`capture`、`repository`） | `ws:<manifest_sha256>` | `workspace_states` 行；服务端据此拼装 IGWS 容器 |
| `workspace.file` | 单文件内容切片（≤4 MiB/文件，1 MiB/块） | `wsfile:<sha256>:<index>` | 服务端按 manifest 顺序拼成 IGWS 容器；内容寻址天然去重 |
| `agent.env` | JSON：`agent-config/1.0` 脱敏快照（§4.4） | `agentenv:<sha256>` | 环境复现（agent 有效配置） |
| `repo.identity` | JSON：`repository/1.0`（§4.3） | `repo:<repository_identity>` | `repository_registry` 行 |

说明：

- **IGWS 容器由服务端拼装**：manifest 的每个条目已带 `content{offset,length,sha256}`，服务端按 manifest 顺序拼接 `workspace.file` 得到容器字节并计算 sha256（与 teams 的容器格式逐字节一致）。客户端不再产出整块容器，因此"只改了一个文件"时只上传该文件的内容块。
- **忽略文件只报清单**：`ignored.entries` 仅路径（目录折叠、≤200 条、超出 `truncated`），正文永不采集。
- 单文件内容块与快照块共用同一套内容寻址与幂等规则：相同 sha 的文件在跨会话/跨轮之间不会重传。
- **没有 `control` 记录族**。原设计的 `session.open/resume/retail/watermark/eom/heartbeat/generation.reset` 全部取消：关联由 `session_id` + 内容哈希 + manifest 推导；代际由服务端从事件行派生（服务端文档 §3.4）；liveness 由到达时间推导。
- `trajectory.lines` 的载荷是文件里的**原样字节**，因此服务端重建 `events.jsonl` 只是拼接。
- 单批上限：未压缩 ≤ 8 MiB，压缩后 ≤ 2 MiB；单帧载荷 ≤ 1 MiB。

### 4.3 白名单载荷

`session.meta`（`session/1.0`）：`title`、`agentProfile`、`modelProvider`、`modelId`、`appVersion`、`parentSessionId`、`primaryCwdDigest`（sha256 前 32 hex）、`createdAt`、`updatedAt`、`schemaVersion`、`turnCount`、`messageCount`、`totalTokens`、`repositoryIdentities`（≤16，单调累积）、`environment{agentConfigSha256, agentConfigSize, workspaceKind}`（`workspaceKind ∈ git | non_git | no_repo`）。

`repo.identity`（`repository/1.0`）：`repositoryIdentity`、`remote{host,path,urlKind}`、`remoteDigest`、`observedAt`。

上述字段的取值来源是进程内已有值（§5.3.2），不解析 `session.json`。

### 4.4 `agent.env`（`agent-config/1.0`）

采集范围决策：**只到"仓库脏树 + agent 配置"**；不采运行时（环境变量、依赖安装状态）。

| 字段 | 内容 |
|---|---|
| `schema` / `appVersion` / `platform` | `agent-config/1.0`、iCode 版本、`<os>-<arch>`（只到平台串） |
| `profile{name,yaml}` | 生效 profile 的正文（凭据类键剔除） |
| `model{profileId,provider,modelId,params}` | 模型与影响生成结果的参数（temperature/topP/maxTokens 等） |
| `tools{enabled[],kinds{}}` | 工具集与类型 |
| `skills[{name,source,digest}]` | 技能清单（正文不入此单元） |
| `mcp{servers[{name,transport,commandDigest}]}` | MCP 服务器清单（命令只留摘要） |
| `hooks{count,events[]}` | hooks 事件集（命令正文与路径不入此单元） |
| `settingsDigest` | 与 agent 行为相关设置的摘要 |
| `redacted[]` | 被剔除的键名清单（审计用） |

来源是**内存中已加载的配置**（profile、技能注册表、MCP 配置、模型选项、hooks），不重新读盘、不解析 `session.json`；脱敏在序列化时一次完成。上限 256 KiB，超限则截断到安全子集并标 `truncated=true`。

## 5. 客户端模块

### 5.1 生命周期绑定

- `bootstrap_runtime` 新增一步：`report.enabled` 为真时**只登记加载器**；真正的导入、WAL 打开、pump 线程启动放在首帧之后的后台任务（TUI 走 `call_after_refresh` 等价时机，headless/ACP 走首个事件循环空转点）。关闭时零导入、零线程。
- 启动**不做全量对账**：不遍历会话目录、不哈希任何已有文件。对账发生在会话被打开时（§5.8）。
- 事件订阅（只订阅，不发布）：

| 事件 | 动作 |
|---|---|
| `SessionReady` / `SessionRestored`（`types.py:1392/1480`） | 绑定会话目录；启动 manifest 对账；发送 `repo.identity` 与 `agent.env`（每会话一次）；**不采集工作区** |
| `SessionSaved`（`types.py:1500`） | 唤醒 tailer、快照 tap、工作区结果采集（post） |
| `TurnStarted`（`types.py:473`） | 工作区签名检查：未变则沿用上一个静止快照作为本轮 pre，变化才重采 |
| `ExecutionChanged`（`types.py:76`）最终释放 | 唤醒 tailer（turn 收尾的兜底触发） |
| `SessionDeleted`（`types.py:1601`） | 解绑；WAL 内已入队单元**不撤回**（删除 ≠ 撤回，与 teams 一致） |

- 进程关闭：pump 走 `foundation/util/once_close.py` 的 `OnceClose` + `finish_close`，排空上限 5 s（含一次在途请求的超时）；超时后放弃未发送单元但**保留 WAL**，下次启动续传。

### 5.2 TrajectoryTailer

- 每会话持久偏移（`cursors/<session>.json`：`{offset,last_sequence,size}`）。客户端只读取行内的 `sequence` 字段（自己的信封契约，用于游标与重放定位），不解码任何业务字段。
- 读完窗口：从 `offset` 读到最后一个 `\n`（torn tail 永不外发）；复用 `foundation/trajectory/reader.py::iter_complete_lines` 的行切分，不另写解析。
- 空闲判定：先比对 `(st_size, st_mtime_ns)`，未变直接返回，不读文件。
- 截断/重建：`size < offset` → 偏移归零，本批置 `reset=true`；服务端据此区分"重读"与"文件重建"（服务端文档 §3.3）。
- 单飞：同一会话同一时刻只允许一个读取任务（三路触发会重叠）。
- 预算：单轮读取 ≤ 4 MiB、单个 `trajectory.lines` 帧载荷 ≤ 1 MiB、每会话每轮 ≤ 1 次读取；全部在 `asyncio.to_thread` 中执行。
- 触发：`SessionSaved` + turn 活跃期 60 s 定时器 + `ExecutionChanged` 释放；定时器在会话不活跃时暂停（ACP 单进程多会话共用一个调度器，不按会话起线程）。

### 5.3 快照采集

#### 5.3.1 SnapshotTap

- 触发：`SessionSaved`。
- 流式读取：以 256 KiB 为单位读 `session.json`，逐块 `sha256`；与该会话上次的块哈希列表比对，**只把变化的块写入 WAL**（`snapshot.chunk`），最后写入 `snapshot.manifest`（完整清单）。
- 稳定性：保存是 tmp+rename 的原子替换（`service/state/store.py`），读到的一定是一致版本；`session.recovery.json` 与 `.bak` 忽略。
- 未变化即跳过：`(size, mtime_ns)` 相同则整轮跳过（不读、不哈希）。
- 内存：单块 256 KiB；不把整个文件读入内存，不做全量 base64。
- 在 worker 线程执行；单会话单飞。

#### 5.3.2 session.meta 的来源（唯一新增的保存钩子）

`session.json` 是本进程内部 schema，服务端不应解析它。因此在保存成功后，由 `service/session/persistence.py` 调用一次 `reporting.publish_session_meta(...)`，参数是从当前作用域**已有的值**构造的不可变 `ReportMeta`（title、profile、model、计数、token、parent_session_id、时间戳等），不做额外读盘、不做 JSON 解析、不 `await`。

约束：

- 该调用放在 `SessionSaved` 发布**之前**、且整体包在 `try/except Exception` 中，异常只计数不上抛；
- 传入的是基元与不可变对象，不传引擎/存储/会话对象；
- `report.enabled` 为假时该调用是空函数（内联 return），零成本。

### 5.4 WorkspaceCapture（v1，默认开）

两个用途：**接受率**（turn 前后对照）与**数据集复现**（任务前的本地环境）。采集范围按决策 D5 限定为**仓库脏树 + agent 配置**；不采运行时（环境变量、依赖安装状态），不采忽略文件正文。

- 触发：`TurnStarted`（本轮基线）、`SessionSaved`（本轮结果）。**取消会话激活时的采集**——其结果必被首个 `TurnStarted` 覆盖，且"打开了会话但没跑任务"不需要复现数据。
- **优先复用静止快照**：`pre(turn N)` 默认取 turn N-1 结束时的快照（那一刻没有写入者，最干净）；`TurnStarted` 只算签名，未变则直接引用，不重采。
- **签名门控**：`sha256(HEAD sha + git status --porcelain=v2 -z 输出)`；与上次相同则**不发送任何单元**（下游按"无新快照 = 状态未变"处理），也不读文件内容。
- 内容采集（git 走 `foundation/platform/process.py` 的异步托管子进程；读盘、哈希、编码在 `to_thread`）：
  - `git rev-parse HEAD`、`git status --porcelain=v2 -z`、`git diff --cached --raw -z`、`git diff --raw -z`、`git ls-files --others --exclude-standard -z`、`cat-file --batch`（仅脏文件）；
  - **逐文件读取**：`stat → read → stat` 比对，期间被改则重读一次，仍不稳定置 `raced=true`（沿用 teams 的 `raced` 语义），供下游判定该条目是否可信；
  - **忽略文件清单**：`git ls-files --others --ignored --exclude-standard --directory -z`，只取路径、目录折叠、≤200 条，超出置 `truncated`；正文永不采集；
  - 产出 `workspace.manifest`（三段条目、每项 `path/status/mode/size/sha256`）+ 每个变化文件一个 `workspace.file` 流。**不产出整块容器**：服务端按 manifest 拼装（§4.2 说明）。
- 限额与降级（沿用 teams 常量 + 本设计新增墙钟）：
  - 单文件 ≤ 4 MiB（超限记 `omitted(size-cap)`）、单仓库 ≤ 32 MiB（`total-cap`，`complete=false`）、每段 ≤ 10000 条（`entry-cap`）；
  - 单次采集墙钟 ≤ 10 s，超时 → 放弃本次并记 `remoteKind=timeout`（`complete=false`）；
  - 上一次签名检查耗时 > 500 ms 时，本轮基线捕获降级为"仅 turn 收尾一次"；
  - 非 git 目录 / 无 git 可执行 → **不产出任何工作区单元**，在 `session.meta.workspaceKind` 标 `non_git | no_repo`，之后不再尝试。
- 隐私：IGWS 含未提交文件正文，`agent.env` 含 profile 正文（均已脱敏）。默认开启的依据是决策 D5；首次运行时在诊断面显示一次说明，设置项可关闭（§9）。

### 5.5 RepositoryIdentity

会话激活时一次：`git remote get-url origin`（无 origin 且恰有一个 remote 用该 remote；多 remote 无 origin → 不上报并记 `ambiguous_remotes`）+ `sha256(realpath(git common dir))` 作为身份。带超时，失败静默。

### 5.6 DurableWAL

目录：`<config_dir>/reporting/`

```
reporting/
  wal.log            # 追加帧流（与 /v1/batch 请求体同格式）
  wal.ack            # 已确认的字节偏移（单行 ASCII）
  cursors/<sid>.json # 轨迹偏移、快照块哈希、工作区签名
  dropped.log        # 显式丢弃证据（溢出/被拒/多次失败）
```

- 写入：帧追加到 `wal.log`；每批（≤1 MiB 或 200 ms）一次 `flush` + `fsync`，不是每帧一次。
- 确认：按 §5.7 的推进条件更新 `wal.ack`。
- 恢复：启动时从 `wal.ack` 顺序解析帧；尾部不完整帧截断；`sending` 状态不需要单独目录（请求是幂等的，重发即可）。
- 上限：`report.queue_bytes_max`（默认 256 MB）。超限时从**最旧未确认帧**开始丢弃，逐帧写入 `dropped.log`（`unit_id`、`kind`、`reason`、`bytes`），并计入诊断计数。
- 压缩：`wal.ack` 之后的前缀在达到 8 MB 时被重写到新文件（两文件轮换），避免无限增长。

### 5.7 ReportPump

- **专用线程**（`threading.Thread`，不是 asyncio 任务）：读 WAL 中下一段连续帧（按字节上限 8 MiB / 压缩后 2 MiB），gzip（>32 KiB），POST。
- 单飞：在途请求恒为 1；握手/认证失败不阻塞采集（WAL 吸收）。
- 超时 10 s；失败退避 2 s 起、×2、封顶 300 s、带 0.8–1.2 抖动；连续失败 12 次不清空 WAL（只在字节上限溢出时按 §5.6 丢弃）。
- 响应处理：
  - `200`：逐单元处理 `accepted` / `duplicates` / `rejected[{unit_id,code}]` / `deferred`；`code` 属于终态（如 `payload_too_large`、`unsupported_kind`）→ 记 `dropped.log`；`deferred` 的帧保留在 WAL、按退避重发；`retry_after_ms>0` → 退避。
  - `400`（整批格式错）→ 该批整体丢弃并留证（说明客户端有 bug，不重试）。
  - `401/403` → 暂停发送并进入退避（诊断面提示"凭据无效"），WAL 继续增长到上限。
  - `413`（批过大）→ 以更小的帧数上限重新切批（不是重发同一请求）。
  - `429` → 按 `Retry-After`。
  - `5xx` / 网络错误 → 退避重试。
- **`wal.ack` 推进条件**：本批每一帧都得到 `accepted` / `duplicates` / 终态 `rejected` 才推进到本批末尾；只要有一帧处于 `deferred` 或未决（5xx、超时、限流），`wal.ack` 停在该帧之前，整段重发（重发幂等）。
- 关闭：见 §5.1。

### 5.8 ManifestReconciler

- 时机：会话激活时；连续失败后恢复时（不轮询）。
- 请求体：本会话已知的 `snapshot{content_hash,chunk_hashes}`、`workspace{manifest_sha256,file_shas}`。
- 响应：`missing_chunk_hashes`、`missing_file_shas`、`acked_last_sequence`、`known_snapshot`。
- 动作：只把缺口重新入队（客户端块的哈希可从文件重算，客户端游标丢失也能恢复）；`acked_last_sequence` 与本地游标的 `last_sequence` 比较，落后则从本地偏移继续 tail，游标丢失则从 0 重读并置 `reset=true`（轨迹行体积远小于快照，按内容幂等重放是可接受的代价）。
- 这一机制取代原设计的 `control.retail`：换机/重装/游标丢失时，重放量从"全量"降为"缺口"。

## 6. 与服务端的职责边界

| 关注点 | 客户端 | 服务端 |
|---|---|---|
| 文件/事件行字节 | 原样搬运 | 拼接 + sha256 校验 |
| 窗口切分 | 不做 | 唯一实现（服务端文档 §3.4） |
| 索引白名单抽取 | 不做 | 唯一实现 |
| generation 派生 | 只在截断时置 `reset` | 由内容与事件行判定 |
| 完整性判定 | 不做 | 由序列连续性与 `trajectory.gap` 行判定 |
| 缺块协商 | 发起 | 应答 |
| 幂等键 | 生成（内容派生） | 去重 |
| 同构产物 | manifest + 逐文件内容（服务端无仓库，只能由客户端产出） | 拼装 IGWS 容器与其余全部 |

## 7. 资源预算与不阻塞约束

| 项 | 预算 | 实现约束 |
|---|---|---|
| 事件循环停顿 | 0 ms | 所有 IO/哈希/编码在 worker 线程；处理器只 `set()` 一个 `threading.Event` |
| 常驻线程 | 1 个（pump）+ 1 个按需（采集，`asyncio.to_thread` 复用默认池） | — |
| 常驻内存 | < 4 MB | 块级处理；不整文件读入 |
| 单次分配峰值 | ≤ 1 MiB（帧）+ 256 KiB（块） | 批次切分按字节 |
| 磁盘 | `queue_bytes_max` 默认 256 MB，诊断面可见 | WAL 上限 + 丢弃证据 |
| 启动 | `enabled=false` → 0；`enabled=true` → 首帧之后，增量 < 100 ms | 惰性导入 + 后台任务 |
| 定时器 | 1 个 60 s 调度器（所有会话） | 会话不活跃即暂停 |
| git 子进程 | 每次 ≤ 10 s，签名未变不启动内容采集 | 托管 subprocess + 超时 + `stdin=DEVNULL` |
| 工作区传输 | 单轮只传变化文件 | manifest + 逐文件内容块；未变文件按 sha 去重，不重传整块容器 |

硬性禁止：

- 不在 EventBus 处理器里做任何磁盘/网络/哈希操作（`publish()` 是内联 `await`，见 `foundation/events/bus.py:56-65`）；
- 不在 asyncio 任务里做同步网络调用；
- 不在 turn 收尾路径上同步等待上报队列（WAL 满即丢弃并留证，不等服务端）；
- 不对 `session.json` 做整文件读入 + 全量哈希 + 全量编码。

## 8. 可靠性语义

- **at-least-once**：WAL 落盘（`fsync`）后才算"已接收"；服务端 2xx 前不推进 `wal.ack`。
- **精确一次效果**：`unit_id` 内容派生，服务端按 `(tenant, unit_id)` 去重。
- **乱序容忍**：批次内顺序保留，跨批可乱序；服务端按键排序。
- **缺块可自愈**：manifest 协商；服务端保留未完成组装直到超时。
- **分叉/旧副本**：同一 `session_id` 的 `session.meta` 以 `updated_at` 新者为准（服务端规则），旧快照对象照常落 OBS；同 `sequence` 不同内容 → 服务端记 `failed` 证据，不覆盖既有行。
- **多写入者**：不支持同一会话被两个活跃进程同时写入（共享/同步盘）。客户端检测到 `size < offset` 时按 `reset` 处理，服务端按内容冲突留证。
- **删除不撤回**：`SessionDeleted` 只解绑采集，已入队单元照常发送（与 teams 一致；合规口径见 §13）。

## 9. 配置项（settings group `report`）

| key | 环境变量 | 默认 | 说明 |
|---|---|---|---|
| `report.enabled` | `CHRYS_REPORT` | `false`（特供版构建预置 `true`） | 总开关，重启生效 |
| `report.endpoint` | `CHRYS_REPORT_ENDPOINT` | `""` | 服务端基址，如 `https://report.example/api` |
| `report.token` | `CHRYS_REPORT_TOKEN` | `""` | `Authorization: Bearer`；建议只从环境/系统凭据读取，不写 settings.yaml |
| `report.upload_timeout_ms` | `CHRYS_REPORT_TIMEOUT_MS` | `10000` | 单次请求超时 |
| `report.batch_max_bytes` | — | `8388608` | 单批未压缩上限 |
| `report.poll_interval_ms` | `CHRYS_REPORT_POLL_INTERVAL_MS` | `60000` | 轨迹补报周期（turn 活跃期） |
| `report.queue_bytes_max` | `CHRYS_REPORT_QUEUE_BYTES_MAX` | `268435456` | WAL 字节上限 |
| `report.workspace_capture` | `CHRYS_REPORT_WORKSPACE_CAPTURE` | `true` | 仓库脏树采集独立开关（含文件正文与忽略文件清单） |
| `report.agent_config` | `CHRYS_REPORT_AGENT_CONFIG` | `true` | `agent.env` 采集独立开关（profile 正文等，已脱敏） |
| `report.diagnostics` | — | `true` | 诊断面显示队列水位/最近错误/丢弃计数 |

新增设置项需要 i18n 标签（`msg()` 流水线）与 `spec.py`/`settings.py` 登记；`report.*` 与 `otel.*` 完全独立，不受 `TELEMETRY_GATE` 门控。

## 10. 复用清单（不再重造）

| 能力 | 直接复用 |
|---|---|
| 完整行切分 / torn tail / 行解码 | `foundation/trajectory/reader.py`（`iter_complete_lines`、`decode_event_line`） |
| 事件行预算与编码常量 | `foundation/trajectory/envelope.py`（`LINE_BUDGET_BYTES`、`SCHEMA_VERSION`） |
| 崩溃扫描/截断语义（仅用于校验） | `foundation/trajectory/recovery.py` |
| 原子写 | `foundation/platform/files.py` |
| 子进程托管与超时 | `foundation/platform/process.py`（`managed_subprocess`、`spawn_managed_stdio_process`） |
| 关闭语义 | `foundation/util/once_close.py`（`OnceClose`、`finish_close`） |
| HTTP 客户端所有权 | 复用 `service/llm/clients.py` 的创建/移交/关闭模式（不改 LLM 代码） |
| 会话目录与短 id | `resolve_sessions_dir()`、`session_short_id()` |
| 事件与设置基础设施 | `foundation/events/bus.py`、`foundation/config/{spec,settings}.py` |

## 11. 侵入性审计（改动点清单）

**新增**
1. `foundation/reporting/`：帧、WAL、Pump、Manifest、配置读取。
2. `service/reporting/`：tailer、snapshot tap、workspace capture、repository identity、生命周期绑定。

**改动（三处，均很小）**
3. `orchestration/startup.py::bootstrap_runtime`：新增一步（enabled 时登记后台启动），需同步 `tests/architecture/test_entrypoint_bootstrap.py` 的 guard。
4. `service/session/persistence.py`：保存成功后一次 `try/except` 包裹的 meta 钩子调用（§5.3.2）。
5. 设置项 + i18n 标签 + 诊断面入口。

**零改动**：轨迹写入器、会话写入器、引擎、turn 执行路径、`foundation/observability/*`、sub-agent/工作流构建路径。

## 12. 验收对应

客户端侧验收（与 server-acceptance.md 的编号对应）：T1（帧与批次）、T4（增量快照）、T5（manifest 补缺）、T6（幂等重发）、T14（工作区镜像与增量）、T18（体验预算）、T19（启动成本）、T22（`agent.env` 脱敏与幂等）、T23（pre 快照复用与 `raced`）、T24（忽略文件清单与非 git 标记）。

## 13. 工作区、环境复现与接受率（D5）

采集范围（决策）：**仓库脏树 + agent 有效配置**；不采运行时（环境变量、依赖安装状态），不采忽略文件正文（只报路径清单）。

同一份数据服务两个用途：

1. **接受率**：turn 前后的两份工作区快照 + `repository identity`。下游 diff 得到 agent 的改动面，再按 `repository` 的 host/path 从远端取最终提交，做行级存活比对。
2. **数据集复现**：turn 前的快照就是"agent 看到的世界"；`agent.env` 补上仓库之外决定行为的部分（profile 正文、技能、MCP、模型参数、hooks 事件集、OS/arch/iCode 版本）。两者缺一不可——只有工作区，复现不出同一份配置下的同一次运行；只有配置，复现不出代码上下文。

数据形态：

- 工作区按**逐文件内容块 + manifest** 上报，服务端拼装 IGWS 容器（§4.2）。因此"只改了一个文件"只传该文件；未变文件按 sha 去重，跨会话/跨轮都不重传。
- `agent.env` 是脱敏后的有效配置快照（§4.4），每会话一份，与工作区快照相互独立。

已知边界（写进文档而不是留给下游猜）：

- 忽略文件只有清单（路径、目录折叠、≤200 条、超出标 `truncated`），内容不采——`.env` 类密钥不外流；
- 非 git 目录不采集，用 `session.meta.workspaceKind` 标记为 `non_git | no_repo`，数据集构建器据此过滤；
- 超限、读取失败、采集期竞态都显式落 `omitted[]` / `raced`，不静默（`complete=false`）；
- **接受率的提交侧事实不在本通道**：服务端/下游需要按 `repository` 从远端拉取提交内容，否则只能回答"改了什么"，不能回答"落地了多少"（开放问题 3）。

可选增强（P2）：`file.change` 单元——在写/编辑工具的调用点直接取 `(path, 修改前字节, 修改后字节)`，只报 iCode 实际改过的文件。对接受率更精确（行级、不受用户自身未提交改动污染），但对复现不是必需；与上述两套并存，共用 blob/WAL/幂等设施。

## 14. 分阶段实施

- **P0 传输与采集核心**：帧/WAL/Pump/Manifest + TrajectoryTailer + SnapshotTap + `session.meta` 钩子 + repository 身份 + 设置与诊断面。对应 T1/T2/T3/T4/T5/T6/T7/T8/T9/T16/T17/T18/T19。
- **P1 工作区与环境**：WorkspaceCapture（逐文件内容块 + manifest，服务端拼装 IGWS）+ `agent.env` + 签名门控与预算。对应 T14/T15/T22/T23/T24。（P1 交付前 `report.workspace_capture` / `report.agent_config` 键存在但不生效。）
- **P2 可选增强**：`file.change` 单元（更精确的接受率）、失败重试的可观测性增强、压缩策略调优。
- **P3（另行确认）**：git 侧事实（adoption/commit/code-change）与回滚正文单元。

## 15. 开放问题

1. **凭据与租户**：token 由部署注入还是用户登录获取？token 与 `user_id` 的映射放在服务端配置还是身份服务？
2. **保留策略**：OBS 对象生命周期、PG 分区与 TTL 的决策人（影响 §容量模型）。
3. **接受率的提交侧事实**：commit 内容由谁拉取（服务端定时从远端 `git fetch`，还是由另一个通道上报）？这决定 `workspace_states` 是否足以支撑报表。
4. **删除会话不撤回**是否符合合规口径（当前与 teams 一致：删除 ≠ 撤回）。
5. **采集告知**：工作区（未提交源码）与 `agent.env`（profile 正文）首次开启时是否需要一次明确确认（当前设计：诊断面提示一次 + 两个独立开关）。
6. **下游是否要求分窗 sha 与 teams 一致**（当前决策 D1 为"否"；若为"是"，需追加确定性验证并确认 teams 分窗不依赖轮询时机）。
7. **teams 侧字段对齐**：teams 文档已定义 `agent-config/1.0`、`meta.environment` 与 schema v10 两列；两端字段名与脱敏白名单必须逐项一致，实现时需加跨仓一致性测试。

## 附录 A：teams 端口来源对照（本版仍然有效的部分）

| 目标 | teams 源 |
|---|---|
| 事件行白名单 | spec `01-fact-flow-and-storage.md` §5.2 |
| session meta 白名单 | `src/client/capture.ts:455-471` |
| IGWS 容器字节格式与限额 | `src/client/workspace-state.ts:29-94,404-419`；`src/contract.ts:1404-1408` |
| repository 规则 | `src/client/repository-discovery.ts`；spec `01` §6.9 |
| 单元 schema / 幂等 / 服务端校验语义 | `src/service/ingest.ts:26-35,170-404`；`src/contract.ts` |
| PG 表结构 | `src/service/schema.ts`（SCHEMA_VERSION=9） |
| OBS 键布局与 registry 语义 | spec `02-server-design.md` §5.4 |

**本版不再移植**：`src/client/trajectory.ts` 的游标/代际/prefix digest（改为服务端从事件行派生）、`store.ts`/`transport.ts` 的队列状态机（改为 WAL + ack 偏移）。
