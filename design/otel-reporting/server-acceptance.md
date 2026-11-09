# 服务端需求与验收测试：事实数据上报（report/1.0）

状态：设计稿（未实施）。
配套文档：[client-design.md](./client-design.md)（客户端设计）。
本文档是**服务端实现方的需求规格 + 验收测试文档**：所有验收用例以给定/当/则表述，可直接转为集成测试。

> 修订记录（第二版）
> - 入口从 OTLP Logs 接收端改为自有 HTTPS 接口（`/v1/batch`、`/v1/session/{id}/manifest`）。
> - 服务端成为**同构语义的唯一实现方**：窗口切分、索引抽取、代际派生都在这里完成（决策 D2）。
> - 新增缺块协商端点与确定性分窗规则；删除 `control` 记录族相关需求。
> - 与 teams 的差异显式列为 §5，供下游确认。

---

## 1. 角色与部署形态

一个独立的**接收 + 重组 + 入库**服务，与 teams 数据平台严格分开（独立进程、独立连接、独立部署单元），但**落盘形态与 teams 同构**：

```
iCode 客户端（iCode 进程内，report/1.0）
   └─▶ HTTPS /v1/batch（帧流，gzip）
         ├─ 帧解析与幂等（ingest_receipts）
         ├─ 字节组装：snapshot / workspace blob → sha256 校验 → OBS
         ├─ 同构派生器：按会话有序行流切窗、抽索引、派生代际
         └─ PostgreSQL（teams schema v9 在范围内表）
   ◀── 200 + accepted/duplicates/rejected         （ACK-after-commit）
   └─▶ HTTPS /v1/session/{id}/manifest            （缺块协商）
```

允许且鼓励复用 teams 的**纯函数**（`checked*` 校验、白名单抽取、schema DDL），但**不依赖** teams 的服务端组件、命令通道、数据库或部署。

## 2. 接口契约

### 2.1 通用

- 传输：HTTPS；`httpx` 之外的任何客户端均应可用（协议自包含）。
- 认证：`Authorization: Bearer <token>`。缺失/无效 → `401`；tenant 与 `user_id` 由 token 决定，**客户端自报的任何身份字段一律忽略**。
- 请求体：若干帧顺序拼接（见客户端设计 §4.1），`Content-Type: application/vnd.chrys.report.frames`；`Content-Encoding: gzip` 时必须解压。
- 限额：请求体解压前 ≤ 16 MiB、解压后 ≤ 16 MiB（客户端单批未压缩上限 8 MiB）；单帧载荷 ≤ 1 MiB；单批帧数 ≤ 4096；超限 → `413`（整批拒绝，客户端应重新切批而不是重试同一请求）。
- 并发闸门：服务端**在读取请求体之前**限制同时处理的请求数（默认 64）；超限 → `503` + `Retry-After`，无任何副作用。
- 超时：服务端处理单批 ≤ 8 s，短于客户端 10 s 超时（避免客户端先超时导致的重复处理）；超时 → `503` + `Retry-After`。
- 幂等：同一批的重复到达（超时重发、并发到达）必须等价于一次成功。

### 2.2 `POST /v1/batch`

请求：帧流。响应 `200`：

```json
{
  "accepted":   ["<unit_id>", ...],
  "duplicates": ["<unit_id>", ...],
  "rejected":   [{"unit_id": "...", "code": "payload_sha_mismatch", "message": "..."}],
  "deferred":   ["<unit_id>", ...],
  "retry_after_ms": 0
}
```

- `accepted`：**该单元的全部副作用已持久提交**（OBS 对象已写入并登记 `verified`，或 PG 事务已提交，或已进入隔离区）。
- `duplicates`：幂等键已存在，本次无副作用。
- `rejected`：终态拒绝（客户端应丢弃并留证）。`code` 至少包含：`payload_sha_mismatch`、`payload_too_large`、`unsupported_kind`、`unsupported_version`、`schema_invalid`、`sequence_conflict`、`missing_reference`。
- `deferred`：**无副作用，需稍后重发**。用于前置条件未满足但可自愈的情形（例如 `session.meta` 引用的快照块尚未到齐）。客户端必须保留该帧并用退避重发；服务端不得把 `deferred` 当作终态记录。
- `retry_after_ms > 0`：整批限流，客户端按此退避。
- 部分成功必须如实分区：四个数组互斥且并集覆盖本批全部 `unit_id`；不得把未提交的单元放进 `accepted`。

**ACK 语义（关键）**：`200` 返回时，响应里 `accepted` 列出的每个单元必须已持久提交。崩溃注入点见 T7。

状态码：`400` 帧格式错误（终态，客户端丢弃整批并留证）；`401/403`；`413`；`429`（带 `Retry-After`）；`5xx`（可重试）。

### 2.3 `POST /v1/session/{session_id}/manifest`

请求：

```json
{
  "snapshot":  {"content_hash": "<sha>", "chunk_hashes": ["<sha>", ...]},
  "workspace": {"manifest_sha256": "<sha>", "file_shas": ["<sha>", ...]}
}
```

响应 `200`：

```json
{
  "missing_chunk_hashes": ["<sha>", ...],
  "missing_file_shas": ["<sha>", ...],
  "acked_last_sequence": 1200,
  "known_snapshot": true
}
```

- 只读，无副作用。
- `acked_last_sequence` 是该会话**已确认提交**的最大 sequence（用于客户端校正本地偏移），不是"收到过"的最大 sequence。
- 未鉴权的会话 id 一律按"不存在"处理（不泄漏存在性）。

## 3. 处理规则

### 3.1 帧解析与幂等

1. 逐帧解析；`unit_id` 必须可由 `(kind, session_id, scope, payload_sha256)` 重算并匹配——不匹配按 `schema_invalid` 拒绝（防止客户端 bug 造成键碰撞）。
2. 幂等表 `ingest_receipts`：`(tenant, unit_id)` 唯一；先写入 `pending` 行（事务内），副作用完成置 `committed`，终态拒绝置 `rejected`。
3. 幂等判定的并发安全：同一 `unit_id` 的并发到达由唯一约束 + 行锁串行化，后到者返回 `duplicates`。
4. 未知 `kind` / 帧版本 > 1：原样写入隔离区对象 + 收据 `unsupported`，响应 `rejected`；不解析内容。

### 3.2 字节块组装（`snapshot.chunk` / `workspace.file`）

1. **快照分块是内容寻址对象**：`snapshot.chunk` 的载荷本身即一块内容（`scope` = `chunk:<content_hash>:<index>`），按 `payload_sha256` 直接写 OBS 并登记 `object_registry`（channel `snapshot-chunk`）——不落本地暂存，也不要求"索引 0..max 全齐"。增量上报因此天然成立：客户端只发变化块，未变化的块沿用已登记对象。
2. **清单驱动拼装**：`snapshot.manifest`（`scope` = `manifest:<content_hash>`）声明有序分块清单 `chunks[]` 与 `size` / `chunk_size`（条目数与 `size` 自洽）。分块全部 `verified` → 按序取回、拼接、`sha256 == content_hash` → 写整份快照对象（channel `session-snapshot`）并登记 `verified`；缺块时登记缺块行（`state='missing'` + `missing_hashes`），此后每到一个分块都重试拼装，缺口补齐即自动转正。
3. 校验失败 → 不登记；拼装幂等，任何时刻（含进程重启后）只要分块对象在库即可重拼；`missing_reference` 的引用方（`session.meta` / `workspace.manifest`）**不得**提交为"完整"。
4. **工作区分片走本地暂存**：`workspace.file` 的分片是整文件的字节区间（`wsfile:<sha256>:<index>`），不是独立对象 → 按块序落暂存目录（内存只留元数据），按序拼接校验后写 OBS。预算：单会话 ≤ 64 MiB、单租户 ≤ 4 GiB（200 并发会话 × 约 20 MiB 未完成组装）；暂存目录单独挂盘并按此额度监控。**超预算不淘汰**：该分片返回 `deferred`（无副作用），客户端按退避重发——淘汰已收下的分片等于静默丢数据。
5. **缺块终态与恢复路径**：组装超过 `assembly_ttl`（默认 24 h）仍有缺口 → 置 `missing` 终态，写 `object_registry(state='missing', missing_hashes=...)` 并释放分片字节，引用它的 `sessions` 行标记 `body_state='missing'`；缺口治愈后状态可推进为 `verified`（状态单向，不允许回退）。恢复不依赖进程内存：快照由"分块对象 + 清单"重拼；工作区把已 `committed` 的分片/清单帧的重放视为幂等重新物化（响应仍是 `duplicates`），或由 manifest 端点会话级协商（`missing_chunk_hashes` / `missing_file_shas`）驱动客户端只补缺口；孤儿暂存目录按 mtime 回收。
6. **工作区容器拼装（`workspace.manifest`）**：manifest 每个条目带 `content{offset,length,sha256}`（删除与 gitlink 条目为 `null`）。服务端按 manifest 顺序拼接已 `verified` 的单文件内容 → 得到 IGWS 容器字节（`"IGWS" | version u16 | flags u16 | manifestLength u32 | manifest JSON | 内容流`）→ 计算容器 sha256 → 写 OBS → `workspace_states` 行引用该 sha。拼装是**确定性纯函数**（同一 manifest + 同一内容集合 → 同一字节），与 teams 客户端产出的容器同构。任一条目缺块 → 该 manifest 返回 `deferred`。
7. **`agent.env`**：单帧，载荷为脱敏 JSON（≤256 KiB，`agent-config/1.0`）。`sha256(payload)` 即 OBS 键；登记对象 `verified` 后随锚点写会话行环境三列（`agent_config_sha256` / `workspace_kind` / `agent_config_captured_at`，本单元是唯一写入者）；正文不入 PG。信封：`workspaceKind` 三值枚举（`git|non_git|no_repo`）、`capturedAt` 必填 RFC3339——客户端**采集时刻**，消费方用它区分"会话前采集"与"后补"。载荷校验：JSON 可解析、`schema` 匹配、`redacted[]` 键名、`sections{profile,model,skills,mcp,hooks}` 各取 `captured|absent`、`truncated` 布尔——均必填，未知成员宽松忽略、缺失按显式失败；超长按 `schema_invalid` 拒绝（不截断）。同会话换新配置 → 三列推进，旧引用对象置 `superseded`（OBS 字节保留）。**分类变化不改变载荷**，客户端重发同一 `contentHash`、只换信封 → 仍是 `duplicates`，但信封的 `workspaceKind`/`capturedAt` 覆写进三列（不新建对象、不汰换）；仅当内容仍是当前锚点、且 `capturedAt` 不倒退时生效（旧 blob 的乱序重放不改任何事实）。

### 3.3 轨迹行缓冲（`trajectory.lines`）

1. 载荷按 `\n` 拆行，每行解析顶层 `sequence`、`event_type`、`branch_id`、`schema_version`。
2. 以 `(session_id, sequence)` 为行的位置键；同一 `sequence` 相同内容 → 去重；不同内容 → `sequence_conflict` 证据（不覆盖已有行）。
3. 按会话维护有序缓冲（内存 ≤ 64 MiB，超出落盘），直到 §3.4 的切窗边界。
4. 不完整行（末尾无 `\n`）→ 拒收该批（客户端不得发送 torn line；服务端不做"善意补全"）。
5. `reset=true` 的批次：表示客户端偏移失效后的重读。按行内容判定——
   - 所有 `(sequence, content)` 均已存在 → 纯去重，代际不变；
   - 存在同 `sequence` 不同内容 → 视为文件重建：**代际 +1**，后续行写入新代际。

### 3.4 同构派生器（唯一实现）

#### 3.4.1 代际（generation）

- 会话首个单元 → `generation = 0`。
- 行内 `branch_id` 发生变化（回滚）→ `generation += 1`；切换点前切窗（窗口不跨代际）。
- 文件重建（§3.3-5）→ `generation += 1`。
- 代际是**行的键组成部分**，不是文件切分：`trajectory_events` 行键 `(tenant, session_id, generation, sequence)`。

#### 3.4.2 切窗边界（确定性）

按顺序在下列任一条件处切窗（切在**行边界**）：

1. 代际变化点之前；
2. 遇到 `turn.finished` 行（该行属于本窗口）；
3. 窗口内索引行 > 200；
4. 窗口 payload 字节数 > 4 MiB；
5. 会话空闲 janitor（默认 1 h 无新行）——只对"已停止上报的会话的最后一个窗口"生效，边界取当时最后一个完整行。

窗口对象 = 窗口内各行原样字节按 `\n` 连接（含尾 `\n`）；`sha256(窗口字节) = payload_sha256`，对象写 OBS。

#### 3.4.3 索引抽取（白名单）

事件行（顶层 + 有界 payload 抽取）：`event_id / sequence / event_type / turn_id / operation_id / actor_kind / actor_role / occurred_at / monotonic_ns / branch_id`；payload 仅抽 `turn_number`、`duration_ms / end_reason`、`tool_name / tool_kind`、`outcome`、`usage.normalized.{input,output}_total`、子代理名/状态、回滚分支字段（`old_branch_id / new_branch_id / superseded_from_sequence / superseded_to_sequence / target_turn_id`）。白名单与 teams spec `01-fact-flow-and-storage.md` §5.2 逐项一致，**只在本服务内维护一份**。

`session_turns`：取本窗口内 `turn.started` / `turn.finished` 行，按 `(session_id, generation, turn_number)` upsert；不回放全量历史。`turn_id` 是身份，`turn_number` 允许重复。

完整性：窗口内序号连续，或被 `trajectory.gap` 行覆盖 → `complete`；存在已知空洞 → `gapped`；两者都不成立（如窗口未闭合）→ 不写 `event_batches` 行。

#### 3.4.4 `session.meta` 与 `sessions` 行

- `session.meta` 载荷是 `session/1.0` 白名单（客户端产出），服务端不解析 `session.json`。
- `sessions` 行（PK = tenant + session_id）就地更新：`meta` 列与 `snapshot_sha256` 锚点取"最新"快照 —— 以 `meta.updated_at` 判定，`observed_at` 平手裁决，**与到达顺序无关**；落后值不回退并记 `stale_report` 证据。
- 锚点提交前置条件：引用的 `content_hash` 已在 `object_registry` 为 `verified`，否则该 `session.meta` 单元返回 `deferred`（无副作用），等块齐后由客户端重发。
- `session/1.0` 不携带环境引用：收到仍带 `environment` 的载荷按未知字段显式拒绝（不做兼容读取）。环境三列只由 `agent.env` 写——`agent.env` 先到时落 `session_anchors` 占位，`session.meta` 建行时从锚点 coalesce，之后**不写也不清除**；`session.meta` 不再因 agent-config 未转正而 `deferred`。

#### 3.4.5 关联证据

`session_id` + `snapshot.content_hash`（命中 `object_registry`）即"该流衔接该归档"的证据；服务端在每个 `session.meta` 上记录 `linked | unlinked` 与 `observed_at`、`user_id`，供追溯。**无需**客户端上报 control 记录。

### 3.5 乱序、并发与分叉

1. 记录可乱序到达（跨批、跨连接）；组装与入表均按键排序，不依赖到达序。
2. 同一会话的写入按 `(session_id)` 串行化（行锁或单会话队列），保证切窗确定性。
3. 多写入者（同一 `session_id` 被两个活跃客户端上报）：同 `sequence` 不同内容 → 先到者保留，后到者记 `sequence_conflict`；`session.meta` 按 §3.4.4 的确定性规则推进，不因冲突而回退。

## 4. 存储

### 4.1 OBS

- 键：`t/<tenant>/o/<sha256[0:2]>/<sha256>`；对象不可变、内容去重。
- 在范围内对象：`session.json` 快照、快照分块（内容寻址，跨快照与跨会话去重）、事件窗口 payload、IGWS 工作区镜像。
- **ETag 落列**：每次写对象都把存储返回的 ETag 一并写进该单元的锚点列——`object_registry.etag`（快照分块、整份快照、工作区文件、工作区容器、事件窗口、`agent.env`）、`event_batches.payload_etag`、`workspace_states.body_etag`。本地存储实现给出内容摘要（md5 十六进制），OBS 取单段上传响应的 `ETag` 并去掉引号；存储没有返回 ETag 时该列留空，不伪造。用途：下游核对"库内锚点 ↔ 对象"是否一致，不必重新下载对象。
- 生命周期：**必须**配置保留策略（见 §6.3），不得按"永不删除"部署生产环境。

### 4.2 PostgreSQL（teams schema v9 在范围内表 + v10 两列，列级一致）

| 表 | 写入来源 |
|---|---|
| `sessions` | `session.meta`（meta 列、snapshot 锚点、`body_state`）；`agent.env`（环境三列：`agent_config_sha256`、`workspace_kind`、`agent_config_captured_at`，唯一写入者） |
| `session_turns` | 派生器：窗口内 turn 行 |
| `event_batches` | 派生器：窗口 |
| `trajectory_events` | 派生器：白名单行 |
| `workspace_states` | `workspace.manifest` |
| `repository_registry` | `repo.identity` |
| `object_registry` | 字节块组装成功 / 缺块终态 |
| `ingest_receipts` | 全部单元（幂等权威） |

范围外表（`adoption_evidence`、`commit_bindings`、`pull_request*`、`attribution_*`、`session_manifests`、`session_rollbacks`、`similarity_calibration`）：v1 不写入，schema 保留。

本版在 teams schema v9 上新增环境复现三列（`sessions.agent_config_sha256`、`workspace_kind`、`agent_config_captured_at`）→ **v10**；沿用扩列纪律：客户端抽取（`agent.env` 载荷 + 信封）、服务端列、服务端校验必须同时改（三仓对齐口径见 `docs/agent-config-sync.md`）。本服务另为 `ingest_receipts.state` / `object_registry.state` 补 CHECK 约束（取值域与模型常量一致）——属服务端安全加固，teams 侧无对应约束，不影响列级一致。

### 4.3 事务与提交顺序

单个单元的单事务内依次：`ingest_receipts(pending)` → 组装校验/写 OBS（OBS 先写，PG 后提交）→ 目标表写入 → `committed`。任一步失败 → 事务回滚 + 收据保留 `pending`，响应不放 `accepted`。

## 5. 与 teams 的同构口径（决策 D1）

**同构（必须一致）**

| 项 | 说明 |
|---|---|
| OBS 键布局与对象种类 | `t/<tenant>/o/...`；快照、事件窗口、IGWS |
| PG 表、列、行键 | schema v9 在范围内表；行键含 `generation` / `sequence` |
| 事件索引白名单字段 | §3.4.3 |
| IGWS 容器字节 | 服务端按 manifest 拼装（同 manifest + 同内容 → 同字节），字节格式与 teams 一致 |
| agent 配置快照 | `agent-config/1.0` 字段集与脱敏白名单与 teams 文档一致（同一单元、同一幂等键语义） |
| `sessions` 锚点推进规则 | `updated_at` 最新，`observed_at` 平手 |

**有意差异（需下游确认）**

| 项 | 本服务 | teams | 影响 |
|---|---|---|---|
| 窗口是否跨代际 | 不跨（切换点前切窗） | 含切换的窗口携带递增后 generation（即窗口内混两个代际） | 下游按行键读取无影响；若按"窗口代际"做统计需改为按行代际 |
| 切窗边界是否依赖轮询 | 不依赖（turn 收尾 + 上限 + 代际边界） | 客户端轮询节奏可能参与 | 本服务可重放得到相同窗口；对同一输入与 teams 的窗口 sha 可能不同 |
| 全量 `events.jsonl` 物化对象 | 可选，不承诺 | 无此对象 | 无 |
| 控制记录 | 无（manifest 端点 + 帧头 `reset`） | 无双工流 | 无 |
| `workspace.manifest` 的 `ignored` 块 | 有（只路径、目录折叠、≤200 条） | 无 | 消费方按名取字段、忽略未知键即可；容器字节因此与 teams 对同一输入不同（决策 D1=A 允许） |

若下游需要"与 teams 对同一输入逐字节相同"，须追加：把切窗规则逐字对齐 teams（含回滚怪癖），并确认 teams 的切窗对输入是确定性的（不依赖轮询时机）。这是开放问题，不是本版的验收项。

## 6. 容量与背压

### 6.1 规模假设

首期容量目标按 **200 并发会话**设计（注册用户 2000）。500 并发是容灾上界，需要先满足 §6.5 的三项前置条件，本版不承诺。

| 项 | 目标 |
|---|---|
| 注册用户 | 2000 |
| 同时活跃上报 | **200 并发会话**（每会话单在途请求） |
| 端到端新鲜度 | ≤ 2 min（p99） |
| 单请求 | ≤ 8 MiB 未压缩（典型 100–500 KB），gzip ≤ 2 MiB |
| 请求速率 | 均值 ≤ 5 req/s，峰值 ≤ 20 req/s（200 会话 × 峰值 10 轮/h ≈ 0.6 轮/s，含重试与补传按 3× 计） |
| 单位处理速率 | 均值 ≈ 6 单位/s，峰值 ≤ 40 单位/s（每轮约 10 个单位） |
| 单节点 | 4 vCPU / 8 GiB，单进程（多副本见 §6.5） |

### 6.2 预算

| 资源 | 预算 | 依据 |
|---|---|---|
| 入站带宽 | 均值 ≤ 5 MB/s，突发 ≤ 10 MB/s | 200 会话 × 1–3 KB/s 有效增量（快照按块）＋ 工作区脏文件的突发上传 |
| OBS 新增 | ≤ 20 GB/天（启用 §6.3 保留后） | 每轮一个快照对象（约 1.5 MiB）＋ 事件窗口 ＋ 工作区容器，约 1.6 万轮/天；未启用保留时按 30–40 GB/天估算 |
| PG 写入 | ≤ 1k 行/s、≤ 300 万行/天 | `ingest_receipts`（约 6 行/s）＋ `trajectory_events`（约 22 行/s 均值，峰值 3×）＋ 窗口/轮次行 |
| 组装内存 | ≤ 512 MiB/租户 | 暂存落盘、内存只留元数据；单请求解压后 ≤ 16 MiB |
| 暂存磁盘 | ≤ 4 GiB/租户，单独挂盘 | 200 会话 × 约 20 MiB 未完成组装（单会话上限 64 MiB） |
| 派生器 CPU | ≤ 2 核 | 40 单位/s × 行解析 ~5 µs/行 |
| 进程 RSS | ≤ 2 GiB | 含 64 个在途请求的帧缓冲 |

**前置条件**（缺一不可，否则 §6.2 不成立）：

1. **快照按块增量**：每轮只传变化的 256 KiB 块。若退化为全量快照，带宽与 OBS 增量会放大 20–100 倍（见设计评审的计算）。
2. **缺块协商**：换机/重装只补缺口，不做全量重放。
3. **保留与分区**：见 §6.3。
4. **工作区按文件增量**：只传变化的文件（未变文件按 sha 去重）；`agent.env` 每会话至少上报一次（同会话多次上报以最新一次成功为准，旧引用按覆写汰换置 `superseded`）。若退化为"每次重传整树"，工作区流量会按脏树大小线性放大。
5. **组装状态在进程重启后仍可恢复**：快照分块内容寻址、由清单重拼；工作区分片在重放（同帧再次到达 → `duplicates` 的重新物化）或 manifest 协商后重新落盘（§3.2-5）。已 `committed` 的收据不得造成"客户端无法重传、服务端又无中间态"的死角。未切窗轨迹行缓冲仍只在进程内存，重启后由客户端 `reset` 重放补齐。
6. **单写者语义**：同一 `session_id` 的写入必须落在同一进程（会话亲和），或由跨进程锁串行化；进程内锁与内存态只在单进程内成立。

### 6.3 保留与分区（要求，不是建议）

- `ingest_receipts`：按月分区，保留 90 天；更早的键降级为按 `(tenant, session_id)` 的摘要表（用于粗粒度去重）。
- `trajectory_events`：按月分区，保留 12 个月，冷数据转 OBS 上的列式快照。
- `event_batches` / `session_turns`：保留 24 个月。
- OBS：快照对象保留 12 个月（`session.json` 的中间版本可降级为只保留每会话最近 N 份 + 被提交引用的版本）；事件窗口对象保留 24 个月。
- 200 并发下 §6.2 的 20 GB/天**只有**在"快照只保留每会话最近 N 份 + 被引用的版本"同时生效时才成立；否则对象总量按轮次线性增长（约 30–40 GB/天）。

### 6.4 背压

三层，从外到内，任何一层都不得让客户端把"未提交"当成"已提交"：

1. **请求闸门**（进程级，默认 64 并发）：在读取请求体之前拒绝，`503` + `Retry-After`（3 s）。用于抵御突发与慢客户端，保护进程内存。
2. **令牌桶**（两级）：单用户 5 req/s（burst 10）＋ 租户总量 50 req/s（burst 100），超限 `429` + `Retry-After`（3 s）。单用户桶防止单个客户端拖垮整租户；租户桶是总闸。
3. **在途批次**（单租户 64）：超限整批 `deferred` + `retry_after_ms`（2 s），无副作用。

- 客户端 WAL 吸收以上三层；超上限按字节丢弃并留证。
- 单租户配额（请求速率、并发组装、未完成组装字节）超限 → 只影响该租户。
- 禁止"先 ACK 后处理"的任何形式异步化：ACK 必须覆盖持久提交。

### 6.5 扩容到 500 并发的前置条件（本版不做）

1. **会话亲和路由或跨进程锁**：会话锁、切窗缓冲、暂存索引都是进程内存态，多副本会破坏"同会话串行"与幂等（同一 `unit_id` 被两个进程处理）。
2. **存储侧**：PG 连接池 ≥ 40；`trajectory_events` / `ingest_receipts` 完成月分区；OBS 生命周期上线。
3. **暂存的压测验证**：分块对象化后快照不再占用暂存盘，剩余增长来自工作区分片；500 并发下需实测"超预算 `deferred` 背压 → 退避重发 → TTL 释放 → 协商补缺"的收敛时间与暂存盘峰值（§3.2-4/5）。

## 7. 验收测试用例

约定：`客户端` 指按 client-design.md 实现的 iCode；服务端用例可用合成帧驱动，不依赖客户端实现。T14 的 IGWS 一致性用签入的黄金语料（输入仓库状态 + 期望容器 sha），不依赖 teams 在线。

### T1 帧与批次
- **给定** 一批 200 帧、含 gzip、含两个坏帧（一个载荷字节被改、一个 `unit_id` 与其 scope 不自洽）；
- **当** 服务端接收；
- **则** 正常帧全部 `accepted`，载荷被改的帧 `rejected(payload_sha_mismatch)`，`unit_id` 不自洽的帧 `rejected(schema_invalid)`，其余不受影响；请求体超限返回 `413` 且不产生任何副作用。

### T2 session.json 逐字节重组
- **给定** 1.5 MiB `session.json`（多轮消息、非 ASCII、嵌套转义），按 256 KiB 分块乱序发送；
- **则** 拼接 sha256 == content_hash；OBS 对象字节与源文件相同；`object_registry.state='verified'`。

### T3 events.jsonl 逐字节重组
- **给定** 含 `trajectory.gap` 行、恰 4096 字节行、非 ASCII 行、两次 `runtime` 启动的会话；
- **则** 拼接结果与本地文件逐字节相同；`gap` 行原样保留；行按 `(generation, sequence)` 唯一。

### T4 快照增量（客户端 + 服务端）
- **给定** 会话已上报一次完整快照，第二轮 `session.json` 只在尾部增长；
- **当** 第二轮上报；
- **则** 客户端只发送变化的块（载荷字节数 < 2 × 256 KiB + 元数据）；服务端 OBS 只新增变化的块与一份新快照对象（未变块内容寻址复用，不重写）；`sessions` 锚点推进。

### T5 manifest 补缺
- **给定** 服务端已有快照与轨迹至 sequence N；客户端游标全丢（换机）；
- **当** 客户端先查 manifest 再上报；
- **则** 客户端只发送服务端缺失的块与 N 之后的行；OBS 对象集合与 L1 行集合不变。

### T6 幂等
- **给定** 同一批帧被发送三次（含并发两次）；
- **则** `ingest_receipts` 每 `unit_id` 恰一行；L1/L2 无重复；第二、三次结果为 `duplicates`。

### T7 ACK-after-commit
- **给定** 在 PG 提交后、响应前注入崩溃；
- **当** 客户端重发同一批；
- **则** 去重后无重复行；最终状态与一次成功等价。反向用例：在 OBS 写入后、PG 提交前崩溃 → 重发后不产生悬挂引用。

### T8 缺块终态、deferred 与自愈
- **给定** `session.meta` 引用的快照缺块；
- **则** 首次到达时 `session.meta` 返回 `deferred`（无副作用，无 `sessions` 行）；超过 TTL 后置 `object_registry(state='missing')` + `sessions.body_state='missing'`，无部分提交；随后补发缺块 → 客户端重发 `session.meta` 被接受，状态推进为 `verified`，`sessions` 行补全。

### T9 乱序与并发会话
- **给定** 同批混入 3 个会话的记录、跨批乱序、`trajectory.lines` 先于其 `snapshot.manifest` 到达；
- **则** 各会话流互不串扰；组装完成后行/对象一致；无跨会话键污染。

### T10 切窗确定性
- **给定** 同一会话的行流以两种不同到达顺序（含重放）上报两次；
- **则** 产出的窗口集合 `(first_seq, last_seq, payload_sha256)` 完全一致，`event_batches` 行一致（重放不产生新窗口）。

### T11 同构索引
- **给定** 含工具调用、子代理、用量、`turn.started/finished` 的会话；
- **则** `trajectory_events` 白名单字段与 teams §5.2 一致（逐项断言）；`session_turns` 按 `(session, generation, turn_number)` 累积；窗口 `completeness` 与 `trajectory.gap` 覆盖一致。

### T12 回滚与代际
- **给定** sequence K 处 `session.rollback` + `branch.superseded`，其后行带新 `branch_id`，`session.json` 重写为截短快照；
- **则** 切换点之后的窗口落在 `generation+1`；**窗口不跨代际**（与 teams 的怪癖不同，§5）；旧代际行与旧快照对象保留；`session_turns` 的重复 `turn_number` 在新代际下不与旧代际冲突；重组 `events.jsonl` 等于本地全文件。

### T13 旧副本 / 分叉 / 多写入者
- **给定** 已归档 `updated_at=T2`（快照 H2）；旧拷贝（`T1<T2`、H1、事件止于 N<M）开始上报；另一写入者在同 `sequence` 写入不同内容；
- **则** `sessions` 锚点不回退；H1 照常落 OBS；关联证据记 `stale_report`；冲突行记 `sequence_conflict`，不覆盖既有行。

### T14 工作区镜像与增量
- **给定** 含 staged/worktree/untracked 三段、二进制文件与被省略条目的仓库，签入的黄金语料；第一轮完整上报后只修改一个文件；
- **当** 第二轮上报；
- **则** 第二轮只上传该文件的内容块（载荷字节数 ≈ 该文件大小 + manifest）；服务端按 manifest 拼装的 IGWS 容器字节与黄金语料逐字节一致、sha 相同；`workspace_states` 各列一致；签名未变化的下一轮**不产生任何单元或对象**；超过限额时 `complete=false, remoteKind=truncated`。

### T15 repository 上报
- **给定** 有 origin、无 origin 单 remote、多 remote 无 origin 三种情形；
- **则** 前两者上报（host/path 明文 + digest），第三者不上报；`repository_registry` 行一致。

### T16 认证与租户隔离
- **给定** 用户 A/B 各自 token；
- **则** 无 token → `401`；A 的数据在 B 的 tenant 下不可见；客户端自报身份字段被忽略；manifest 不泄漏会话存在性。

### T17 未知类型与版本
- **给定** 未知 `kind` 或帧版本 2；
- **则** 原样进隔离对象 + `unsupported` 收据；主流程不受影响。

### T18 客户端体验预算（客户端用例）
- **给定** 服务端注入 10 s 延迟 / TCP 黑洞 / 401；
- **则** 事件循环心跳抖动 < 50 ms；TUI 无冻结；WAL 增长到上限后按字节丢弃并留证；恢复后补传；进程退出 ≤ 5 s。

### T19 启动成本（客户端用例）
- **给定** `report.enabled=false` / `true`；
- **则** false 时不导入上报模块、不起线程、启动增量 ≈ 0；true 时不推迟首帧，且不遍历会话目录。

### T20 容量基线
- **给定** 200 并发客户端 × 30 min（每轮一次快照增量 + 60 s 轨迹批）、4 vCPU / 8 GiB 单节点；
- **则** 入站均值 ≤ 5 MB/s、峰值 ≤ 10 MB/s；p99 端到端 ≤ 2 min；`5xx` 率 < 0.1%；进程 RSS 峰值 ≤ 2 GiB；暂存目录峰值 ≤ 4 GiB/租户；PG 写入 ≤ 1k 行/s 且无长事务；过载时段只出现 `503 overloaded` / `429` / `deferred` 三类响应，且都不产生副作用。

### T21 删除与撤回口径
- **给定** 客户端删除会话；
- **则** 已入队单元照常入库（不撤回）；客户端诊断面可见"已上报但已删除"的计数；服务端审计可按 `user_id` 追查。

### T22 `agent.env`（环境复现）
- **给定** 一份含 profile 正文、技能列表、MCP 服务器、模型参数、hooks 事件集的有效配置（信封带 `workspaceKind` 与 `capturedAt`）；
- **则** `agent.env` 落 OBS，`sessions.agent_config_sha256` 指向它；正文不进 PG；`redacted[]` 列出被剔除的键名；凭据类字段（api_key、headers、命令明文路径）不出现在对象字节里（按字节断言）；同配置重发 → `duplicates`；超大配置按 `schema_invalid` 拒绝而非截断；缺 `sections`/`truncated`/信封字段按 `schema_invalid` 拒绝；同会话换新配置 → 旧对象置 `superseded` 而字节保留。

### T23 任务前快照的可用性
- **给定** 会话第 N-1 轮结束后无外部改动，第 N 轮开始；
- **当** 客户端在 `TurnStarted` 只做签名检查；
- **则** 不产生新的工作区单元，`pre(turn N)` 等于 N-1 轮结束时的快照（可由 `workspace_states` 行时间线验证）；若用户在轮间改了文件 → 产生新快照；若采集期间文件被改动 → 该条目带 `raced=true` 且内容为稳定后的版本（或显式 `omitted(read-error)`）。

### T24 未采集清单与非 git 标记
- **给定** 仓库含 `.gitignore` 忽略目录（如 `node_modules`）与非 git 工作目录；
- **则** `ignored` 只含折叠后的路径且 ≤200 条（单条 ≤512 字符，超出 `truncated=true`）、不含任何文件正文，且随镜像**首采冻结**（重采不改写清单）；非 git 目录不产生工作区单元，且 `sessions.workspace_kind='non_git'`（无仓库为 `no_repo`，由 `agent.env` 信封写入），可被下游过滤。

## 8. 非目标（v1）

- adoption 证据 / commit 观察 / code-change / session-archive / session-rollback 正文 / 本地判据申报 / 阈值标定；
- 归因（attribution）与 MR 拉取；
- 向客户端下发任何策略或指令；
- 运行时环境的采集（环境变量、依赖安装状态、进程环境）；忽略文件的正文；非 git 目录的镜像（只标 `workspace_kind`）。
- 接受率计算本身（服务端只保证 `workspace_states`、`agent.env` 与 `repository` 可用；提交侧事实需另行接入，见客户端设计 §15 开放问题 3）。

## 9. 实现建议

1. 直接复制 teams `src/service/schema.ts` 的 DDL（v9）与 `ingest.ts` 的 `checked*` 纯函数；适配层只做"帧 → 单元对象"翻译。
2. 派生器写成**纯函数**：输入有序行 + 会话状态，输出窗口与索引行，便于用 T10/T11 直接表驱动测试。
3. 组装缓存（chunk、行缓冲）以 `(tenant, session_id)` 分片，内存 + 本地盘两级，全部带 TTL 与字节预算。
4. 所有"不确定"状态一律返回非 2xx 让客户端重发；不做服务端内部重试掩盖。
5. 观测：接收端自身指标（批大小、组装命中率、缺块率、隔离率、派生延迟、进程 RSS、暂存占用、请求闸门与在途批次占用、`429`/`503` 计数）接入自身 OTel；与 teams ingest 的对账报表用签入黄金语料做常驻回归。
