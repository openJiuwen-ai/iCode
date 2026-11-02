# ADR-0017：detail 仅文件日志子树（A′ 非传播变体）

<!--
签认规则（R10）：责任人本人编辑本头部（状态→已确认、填日期，确认意见可选），AI 不得代填。
签认后修改正文需重新签认；被取代时由新 ADR 回指，本文状态行改为：被取代（被 ADR-00NN 取代）。
校验：uv run python aixcoding/tools/check_docs.py

命名与编号：NNNN-标题.md，取目录现有最大编号+1；编号被引用后不改号；
并行撞号罕见且必然被 git 同名冲突暴露，暴露早期重新编号即可。

ADR 判定 checklist（出 ADR 的门槛，任一"是"；粒度是决策不是功能）：
① 有被认真考虑但否决的备选方案
② 撤销代价高（依赖方向/目录落点/数据格式/对外契约）
③ 约束未来工作模式，后来者会问"为什么"
④ 依赖上游非公开接口或行为（另登记定制清单 anchor）
反面清单（不出）：模块内实现选择、唯一合理路径、纯文档修正。

自包含快照原则：决策节完整记录当时定的机制内容，写定不改；规范演进不回改本文——
重大规则变更出新 ADR，本文标"被取代"。指向规范的引用仅作导航注记
（"本文为决策时点快照；现行以 <规范条款> 为准"），不作为决策内容的一部分。

决策更新处理：决策实质变更 → 新建 ADR 取代（背景须引用旧 ADR 并说明"什么变了"），
旧 ADR 仅改头部状态、正文不动、不触发重签；原地修改仅限错别字/链接/背景补充等
非决策性修正。

分池依据（DEVELOPMENT.md §6）：全局/架构级决策入本目录（standards/adr/，不随特性消亡）；
本决策确立后续全部埋点的敏感字段处理模式并修订 diagnostics 批次布局表述，属全局级
（2026-11-02 责任人裁决移入本目录；原草案曾置于 batches/detail-logging/adr/）。
-->

- 责任人：周彬
- 状态：已确认
- 确认日期：2026-11-02
- 确认意见：无特别意见

> 本文为决策时点快照；现行规则以 DEVELOPMENT.md R1/R2 与 detail-logging 批次 design.md 为准。

## 背景

diagnostics 批次（2026-10-25 签认）确立日志管道基线：文件 sink（`_Marker`）**只挂顶级 `aixcoding` logger**、面板开关 = 顶级 logger 的 `propagate`，其 design 方案对比表中"转发 handler 调上游单例"路线被否决。新需求出现：部分信息（`ehr` 等 PII）不宜在 F6 面板呈现，但文件排查需要全量——需要 record 级分流，这在既有"propagate 树级开关"架构内无解。

外部评审（2026-11-02，责任人委托）关键发现并经本仓独立核实：上游 `debug.log` 的 queue/file 两级 handler 均未 `setLevel`（默认 NOTSET=0），任何冒泡到 root 的记录都会写入——"低于 DEBUG 的自定义级别"只能挡住 F6 面板（其 handler 设了 DEBUG），挡不住 `debug.log`。

## 决策

建立 `aixcoding.detail` **仅文件子树**：`level = DETAIL(5)`（低于 DEBUG=10 的自定义级别，`addLevelName` 注册）、`propagate = False`，并配备**独立**文件 sink（`_DetailMarker(RotatingFileHandler)`，1MB×3 独立轮转）写 `aixcoding-logs/aixcoding.detail.log`；detail 记录在子树层写文件后即止，不经 root 分发，**与普通日志分卷存放**（PII 单独成卷——单独清理/导出排除边界；属运维与暴露面隔离，非访问安全边界，两卷同目录同权限）。同时推翻 diagnostics design "handlers attach to the top-level `aixcoding` logger only" 的布局表述（该批次无 ADR，表述修订以本 ADR 为出处，旧文档不回改）。敏感/PII 明细的埋点规范自此为：调用方**双打**——普通通道精简版（面板+主卷）+ detail 通道全量版（仅独立卷）。

文件形态沿革：初版草案为"同一 `_Marker` 实例双挂"（同文件混写）；第二轮外部评审（2026-11-02，责任人委托）推荐独立卷（PII 管控收益 > 双 sink 管理成本），责任人采纳。

## 理由

被否决备选的死因（R8 记录义务）：

- **A（级别差冒泡，detail 仅设低级别不设 propagate=False）**：敏感明细泄入上游 `debug.log`（两级 handler 未设级别，已核实 `debug_log.py:207-225`）——评审发现，推翻本仓原推演"天然规避 debug.log"（错误）；
- **B（面板桥 handler 持上游 `install_log_handler()` 单例引用转发）**：依赖上游私有单例内部结构，升级重构脆弱；且等价路线已被 diagnostics 批次签认否决，重启需新前提；
- **D（改上游 `_TuiLogHandler.emit` 加 record 属性过滤）**：触碰上游文件 + 隐形寄生协议（上游不认识该属性）+ emit 为上游面板核心逻辑、升级冲突面更大；
- **F（绕过 logger 树自构 LogRecord 直接投递 sink）**：绕开标准 logging 设施（异常/stacklevel/延迟格式化自理），无增益；
- **C（第二个顶级 logger 名）**：需改上游 `MODULE_ORDER` 白名单。

A′ 是唯一同时满足"面板不可见 + debug.log 不进 + 零上游文件 + 零上游行为假设"的方案：隔离靠 logging 标准语义（`propagate` 截断），不依赖任何 chrys 上游代码。代价为 diagnostics design 一处布局表述修订与本 ADR。

文件形态的取舍（第二轮评审，2026-11-02）：独立卷（采用）vs 同文件双挂（A″，初稿形态）——独立卷的实质收益是 PII 运维边界（单独清理/导出排除、两卷轮转互不挤占、日常排查不接触敏感内容），代价是双打摘要与明细分属两卷需按时间戳关联、双 sink 幂等管理引入部分失败态（六不变量约束，见后果）；同文件混写更简单但敏感史与普通记录同卷。"同文件但独立轮转"（两个 handler 写同一路径）因轮转状态互相干扰直接排除——同文件唯一安全形态是同一实例双挂。

## 后果

- `setup_logging`（`logging_support.py`）新增 detail 子树预置义务与**双 sink 部分失败六不变量**（第二轮评审固化）：两 sink 分别 ensure 不互相跳过、单失败不牵连、缺失可重试补建、共存不重复挂载、detail sink 失败时保持隔离丢弃记录（stderr note，不回退普通通道）、`propagate=False` 先于 `level` 设置；
- 后续埋点（telemetry、model catalog 等）遇到敏感/PII 字段必须走 detail 子树双打，不允许"只打一条全量"或把敏感字段留在普通通道；
- **轮转口径**：大小轮转只限制容量与回溯量，不保证 PII 留存时长；"少留敏感史"优先减 `backupCount`，"最多保留 N 天"需专门清理机制；
- detail 通道的测试策略与 caplog 不同（propagate=False，root 挂的捕获器拿不到）——detail-logging 批次 test-plan 固化写法；部分失败四态（主成 detail 败/主败 detail 成/双成/重试补建）为必测项；
- B 路线的重启触发条件记录在案：出现"中心化脱敏、禁止调用点自行决定"的信息安全边界需求时，按 DEVELOPMENT.md §6 ADR 判定重估（需求前提变化构成重新评估的正当理由，非承认本决策错误）。轻量"结构化双打 facade"变体（单一 helper 生成两条记录，只约束自愿调用点、非强制边界）记档于 detail-logging design 备选表 G 行：调用点增多、双打漂移成实际痛点时重估。
