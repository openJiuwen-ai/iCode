# 设计：detail 仅文件日志子树（A′ 非传播子树 + 独立明细卷）

<!--
签认规则（R10）：责任人本人编辑本头部（状态→已确认、填日期，确认意见可选），AI 不得代填。
签认后修改正文需重新签认。校验：uv run python aixcoding/tools/check_docs.py
-->

- 责任人：周彬
- 状态：已确认
- 确认日期：2026-11-02
- 确认意见：无特别意见

## 方案概述

在 diagnostics 既有管道上增设一条**仅文件**通道：预置专用子树 logger `aixcoding.detail`（自定义级别 `DETAIL = 5`，低于 DEBUG=10），`propagate = False`，并为其配备**独立**文件 sink（`_DetailMarker(RotatingFileHandler)`）写 `aixcoding-logs/aixcoding.detail.log`（1MB×3 独立轮转）——detail 记录在子树层即被写文件，随后被 `propagate=False` 阻断，**不冒泡到 root**（F6 面板、上游 `debug.log` 队列 handler 均不接触），并与普通日志**分卷存放**。分卷的收益口径（第二轮评审收敛，2026-11-02）：PII 单独成卷——日常排查主卷不接触敏感内容、敏感历史可单独清理、导出/支持包可显式排除 detail 卷；**这是运维与暴露面隔离，不是访问安全边界**（两卷同目录同权限）。

文件形态沿革：同文件混写（`_Marker` 同一实例双挂，原草案形态）→ 独立卷（第二轮外部评审推荐、责任人采纳，对比见备选表 A″ 行）。隔离机制（propagate=False 截断）在两形态下完全一致，形态差异只在 sink 数量与文件落点。

```
aixcoding.auth 等业务 logger（普通通道，现状不变）
    │ callHandlers 沿树向上
    ▼
logger "aixcoding"（顶级）
    ├─ _Marker 文件 sink ──▶ aixcoding-logs/aixcoding.log（1MB×3）
    └─ propagate = cfg.logging.panel（面板开关，不变）
        ▼ panel=true 时冒泡
root ── _TuiLogHandler（F6） + debug.log 队列 handler

aixcoding.detail.<child>（新：仅文件通道，独立卷）
    │ 预置顺序：先 propagate=False，后 level=DETAIL(5)（防初始化间隙冒泡）
    ├─ _DetailMarker 文件 sink ──▶ aixcoding-logs/aixcoding.detail.log（1MB×3 独立轮转）
    └─ 写文件后即止（不经 "aixcoding"、不经 root）
       → 面板不可见 ✓ 上游 debug.log 不可见 ✓ 主卷不接触 ✓
```

源码改动两处（全在定制层）：①`src/chrys/kernel/aixcoding/diagnostics/logging_support.py`：定义 `DETAIL_LEVEL = 5` + `addLevelName`；`setup_logging` 预置 detail 子树并创建/复用独立 `_DetailMarker` sink（幂等与部分失败处理按风险节六不变量——现有主 sink"找到即 return"逻辑须重写为两 sink 分别 ensure，否则"主成 detail 败"时永远无法补建 detail 卷）——同时修订 docstring 的 Layout 描述（ADR-0017 推翻"handlers attach to the top-level aixcoding logger only"表述）；②`src/chrys/kernel/aixcoding/auth/session.py`：#1/#5/#8 三条含 `ehr` 记录改为双打——面板精简版（原 logger，去 `ehr`）+ detail 全量版（新 `_detail_log = logging.getLogger("aixcoding.detail.auth")`，`log(DETAIL, ...)`）。auth 保持零依赖直写（不 import diagnostics，沿 auth-logging 2026-10-30 修订先例）。

## 埋点改动清单（3 条；消息全文为实施冻结口径）

与 auth-logging 埋点表同编号。其余 9 条（#2/#3/#4/#6/#7/#9/#10/#11/#12）零改动。消息语言、`%s` 参数化、字段白名单口径全部沿既有 design。

| # | 方法 | 面板版（普通通道，级别不变） | 文件版（detail 通道，DETAIL） |
|---|---|---|---|
| 1 | check_silent（delegated 成功） | `已登录（桌面端登录）；env=%s` | `已登录（桌面端登录）；ehr=%s env=%s` |
| 5 | check_silent（stored 成功） | `已登录（本机保存的登录）；env=%s` | `已登录（本机保存的登录）；ehr=%s env=%s` |
| 8 | complete_login（成功） | `登录成功；env=%s` | `登录成功；ehr=%s env=%s` |

说明：此前责任人裁决"ehr 记录不打码"（auth-logging 2026-10-29）——本批次是**细化**而非推翻：文件全量版维持不打码，仅从 UI 呈现面（F6 面板）移除。detail 版不记 `exc`/traceback（沿两条红线）。

## 备选方案对比

分流维度分析（为何只能落在 handler 级别/子树传播上）：上游白名单只认 `record.name.split(".")[0]`，logger 名维度分流需改上游白名单（排除）；`propagate` 是树级开关，无法 record 级分流（排除）。评审输入：外部 AI 两轮（2026-11-02，责任人委托转达）+ 本仓独立核实——第一轮（debug_log.py 两 handler 均未 setLevel、`test_panel_anchor.py` 直调 emit 不过级别检查、`Logger.callHandlers` 才是级别判断位置，三项读码证实）；第二轮（独立卷取舍、双 sink 部分失败六不变量、轮转不保证留存时长、`*.log` glob 不匹配备份、facade 变体，处置均已吸收进本 design 与 ADR-0017）。

| 方案 | 优点 | 缺点 | 升级冲突面（文件数×行数×耦合度，R1） | 结论 |
|---|---|---|---|---|
| **A′. detail 子树 propagate=False + 独立 `_DetailMarker` 卷（`aixcoding.detail.log`）** | 面板/debug.log 双隔离；PII 单独成卷（单独清理/导出排除边界）；**零上游文件、零上游行为假设**；沿用标准 logging；两卷轮转独立互不挤占 | 双打摘要与明细分属两卷，完整排查需按时间戳关联两文件；双 sink 幂等管理（六不变量，见风险节）；修订 diagnostics design 布局表述（ADR-0017）；caplog 策略需改 | 0 上游文件 | **采用**（2026-11-02 责任人拍板 A′ 路线；同日第二轮评审推荐独立卷形态，采纳） |
| A″. detail 子树 propagate=False + `_Marker` 同一实例双挂（同文件混写，原草案形态） | 单文件看全量（时间线集中）；handler 只管一个实例 | 敏感字段与普通记录同卷；明细量大会挤占普通记录留存；清敏感史需删整卷（连带普通记录） | 0 上游文件 | 否（第二轮评审推荐独立卷，责任人采纳文件形态） |
| G. 结构化双打 facade / LoggerAdapter（单一 helper 生成精简+全量两条） | 集中消息构造与字段白名单，减少双打漂移；零上游 | 只约束自愿使用 helper 的调用点，无法拦截既有直写——**非强制安全边界**；当前仅 3 个冻结调用点，抽象收益低 | 0 上游文件 | 否（本批次不做；记档：调用点增多、双打漂移成为实际痛点时重估） |
| A. detail 子树仅靠级别差（propagate 冒泡 + DETAIL<DEBUG） | 不改挂载结构 | **敏感明细会泄入上游 debug.log**（queue/file 两 handler 均未设级别，评审发现、已核实）；依赖上游 `_TuiLogHandler` handler 级别=DEBUG 的行为假设 | 0 上游文件 | 否（评审修正：原推演"天然规避 debug.log"错误） |
| B. 面板桥 handler（自写 handler 持上游单例引用，改写后转发） | 可做中心化脱敏显示 | 依赖上游私有单例内部结构（升级脆弱）；推翻 diagnostics 批次已签认否决；propagate=false 化连改测试基建 | 0 上游文件（私有 API 耦合） | 否；重启触发条件记录在案：出现"禁止调用点自行决定"的信息安全边界需求时按 ADR 判定重估 |
| D. 改上游 `_TuiLogHandler.emit` 支持 record 属性过滤 | 语义直白 | 动上游文件；`record.panel_visible` 隐形寄生协议；emit 是上游面板核心逻辑、冲突面大于本方案 | 1 上游文件 ×2-3 行 | 否 |
| F. 绕过 logger 树直接投递（自构 LogRecord 调 sink） | 零上游零假设 | 绕过标准 logging（异常/stacklevel/延迟格式化自理）；丢 root debug 副本口径；实现土 | 0 上游文件 | 否（A′ 以标准机制达成同等隔离） |
| C. 第二个顶级 logger 名 | — | 需改上游 MODULE_ORDER/白名单 +1 行；Tab 品牌不符 | 1 上游文件 ×1 行 | 否（分析期即排除） |

R1 自查：业务逻辑全部在定制层（diagnostics 预置 + auth 调用点）；上游接触面为零，无接线点可再最小化；detail 子树隔离靠 logging 标准语义（`Logger.callHandlers` 逐层传播 + `propagate` 截断），不依赖任何 chrys 代码。

## 对上游隐式行为的假设

本批次**零新增上游行为假设**（A′ 的 detail 记录不经 root 分发，不依赖 `_TuiLogHandler` 级别设置、不依赖 debug 队列行为）。方案 A 曾依赖"面板 handler 级别=DEBUG"——该假设随 A 一并被否决路线带走。依赖的 Python 标准库契约（非上游）：`Logger.callHandlers` 沿树逐层调用各层 handlers 并受每层 `propagate` 截断；`Handler` 可同时挂在多个 logger 上。

既有假设沿用：diagnostics / auth-logging 批次假设表全部不变（普通通道未触碰）。

## 定制点登记预告

不适用：本批次零上游文件接触（无新增/变更/退役 marker），纯定制目录内改动 + 开发资产（tests/docs）。`gen_customizations.py` 对账预期产物 diff 为空（验证项之一）。

## 风险与最坏情况

- **降级的两种形态**（须区分，第二轮评审补）：①**完全未预置**（`setup_logging` 未跑/失败）——level 沿树回退（`"aixcoding"`=INFO 或 root），DETAIL 记录大概率不产生（fail-safe 静默，与既有"未 setup 无落盘"边界同族）；极端边界：root level=NOTSET 的异常配置下记录可能产生并冒泡（propagate 默认 True）进 debug 队列——真实 app `configure_debug_logging` 设 root=INFO，仅存在于自构异常测试进程，接受；②**已预置 level/propagate 但 detail sink 创建失败**——记录产生后被安全丢弃（propagate=False 不冒泡），stderr note 报告，**不回退到普通通道**——比 ① 更确定的 fail-safe，正是六不变量第 5 条的形态。
- **不串卷断言**：detail 记录只进 detail 卷（在 detail 层写一次即止，不达 `"aixcoding"` 层）；普通记录只进主卷（经 `"aixcoding"` 层写一次，不经过 detail——它是子不是父）。两方向各恰好一次、互不越卷，测试显式断言。**绝不**用两个 handler 实例写同一路径（各自轮转状态/流/备份互相干扰——"同文件"场景唯一安全形态是 A″ 的同一实例双挂，本方案已不涉及）。
- **双 sink 幂等与部分失败（六不变量，第二轮评审固化）**：①每次 `setup_logging` 分别 ensure 主、detail 两个 sink，不因一个已存在跳过另一个（现有主 sink"找到即 return"逻辑必须重写，否则"主成 detail 败"永远无法补建）；②一个创建失败不影响另一个的创建与复用；③缺失的 sink 下次 setup 可重试补建；④两者都存在时不重复挂载；⑤detail sink 失败时保持 `level=DETAIL、propagate=False`，记录丢弃 + stderr note，不回退普通通道；⑥配置 detail 子树时**先设 `propagate=False` 再设 `level=DETAIL`**（防初始化间隙并发日志冒泡）。`_DetailMarker` 非防重挂必需（两 handler 挂不同 logger，查重天然独立），保留作语义与测试辨识度。
- **轮转口径（必须写死）**：沿用 1MB×3；**大小轮转限制的是容量与最大回溯量，不保证 PII 的留存时长**——低频记录即使卷很小也可能长期留在活动文件中。将来若要求"少留敏感历史"：优先减 `backupCount`；若要求"最多保留 N 天"：需专门的到期清理机制，不得靠 `RotatingFileHandler` 宣称达成。
- **敏感卷管控文案（第二轮评审收敛，避免过度承诺）**：清敏感史 = **停止相关进程后**删除 `aixcoding.detail.log` 及其全部轮转备份（Windows 下活动文件被 handler 持有）；独立卷**不构成**访问控制（两卷同目录同权限）；支持包/日志收集要排除敏感卷须显式处理 `aixcoding.detail.log*`——注意 `*.log` 只匹配活动文件、不匹配 `.log.1` 等备份（主卷同现象，非本命名特有）；`aixcoding.detail.log` 文件名本身跨平台无隐患（点分层级合法）。
- **caplog 策略**：propagate=False 后 pytest caplog（挂 root）捕获不到 detail 记录——detail 断言改为直挂测试 handler 到 detail 子树或断言 `_DetailMarker` 输出；普通通道断言策略不变。
- **`addLevelName(5, "DETAIL")` 全局注册**：本仓 `src/` 零 `addLevelName` 调用（已核实）；第三方 TRACE=5 惯例撞名风险低，注册幂等。
- **DETAIL 常量共享位置**（开放问题）：优先 `chrys.kernel.aixcoding` 包根共享（子包→包根 import 待 `test_layering` 实跑验证）；红了则 fallback 各调用文件本地常量（auth-logging 直写先例）；不影响决策本体。
- **回滚**：删除 session.py 3 处 detail 调用 + setup_logging 的 detail 预置段即完全回滚（独立卷文件一并删除）；ehr 双打回滚 = 恢复 3 条消息原文。零残留、零上游修复。

## ADR 判定

按 DEVELOPMENT.md §6 checklist 逐项：

1. 被认真考虑但否决的备选？——**是**（A/B/D/F/C 五案系统对比，且 A′ 推翻 diagnostics 批次 design 的"handler 只挂顶级 logger"既有表述）。
2. 撤销代价高？——中：detail 子树被 auth 等消费后，撤销需改调用点并恢复消息原文；隔离机制本身撤销廉价。
3. 约束未来工作模式？——**是**：确立"敏感/PII 明细走 detail 子树 + 双打"为后续全部埋点（telemetry 等）的既定通道，后来者会问"为什么两条管道"。
4. 依赖上游非公开接口或行为？——**否**（零上游接触、零假设）。

结论：出 ADR——**全局池 `standards/adr/0017-detail仅文件日志子树.md`**（分池依据 §6：决策约束后续全部埋点的敏感字段处理模式并修订跨批次（diagnostics）布局表述，不随本特性消亡；2026-11-02 责任人裁决移入全局池，原草案曾置于本目录 `adr/0001`，已迁移、编号 0001→0017）。

## 验证方式

1. 单测（新增 `aixcoding/tests/diagnostics/test_detail_channel.py`）：经真实 logger 分发——①DETAIL 记录：直挂 detail 子树的测试 handler 收到（name/levelname="DETAIL"/消息全文）、root 上 `_TuiLogHandler` 实例缓冲无、root 模拟 debug 队列 handler 无、主卷 sink 无（**不串卷**）；②普通 INFO 双态（panel true/false）回归；③幂等与部分失败四态（第二轮评审）：主成 detail 败 / 主败 detail 成 / 双成 / 失败后下次 setup 重试补建——六不变量逐条断言；④"不串卷"双向断言；⑤降级两形态（完全未预置→不产生；已预置但 sink 败→丢弃 + stderr，不冒泡不回退）；⑥初始化顺序（propagate 先于 level 设置后，间隙并发记录不冒泡——以属性快照断言顺序效果）；
2. 既有测试回归：`aixcoding/tests/auth/test_session_logging.py` 3 条 ehr 断言迁移（面板版反断言不含 `ehr`、detail 断言含），其余 9 条零改动；`tests/diagnostics` 全量（`aix_logger_state` fixture 扩展保存/恢复 detail 子树状态与两 sink）；
3. `uv run pytest aixcoding/tests -n0` + `uv run pytest tests/architecture -n0` 全绿（auth 直写零依赖不被破坏）；
4. 对账：`uv run python aixcoding/tools/gen_customizations.py` 后产物 diff 为空（连跑两次幂等）；
5. 闭环（人工）：本机 `aixcoding.toml` 现为 `panel=true`——TUI 启动登录后 F6 "aixcoding" tab 见 `登录成功；env=local`（无 ehr），主卷 `aixcoding.log` 同条无 ehr，独立卷 `aixcoding.detail.log` 含 `ehr=...` 且 levelname 为 DETAIL；步骤与输出摘录落 verification.md。
