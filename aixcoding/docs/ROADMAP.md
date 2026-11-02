# AIxCoding 定制化路线图（ROADMAP）

> 定位：三阶段路线的唯一权威描述——阶段边界、各阶段完成标准（DoD）、双轨期管理。
> 文档分工：`standards/DEVELOPMENT.md` 管"怎么做"（规范）；本文管"按什么顺序做"（路线）；
> `batches/restructure/plan.md` 是阶段三的迁入考古参考（旧分支资产映射与验收骨架）。
> 制定：2026-10-25。

## 1. 背景与动机

第一轮定制化（至旧分支 `main_aixcoding_cli_0.28.0_final`，上游 0.28.0 基线）在无规范状态下进行，
团队各成员做法不一致，追版本维护成本高企。2026-10 重建路线：基于上游最新版本拉新分支
`main_aixcoding_cli`（初始 0.31.0 基线；经 `upstream-merge-v0.32.2` 批次现 0.32.2），**不带任何
旧功能代码**，仅带入规范体系，按"规范 → 基础工程 → 功能迁入"三阶段重建定制层。

核心约束（与 DEVELOPMENT.md 一致）：对上游源码改动最少最集中，以降低追版本维护成本。

## 2. 三阶段路线

**"基础工程"的两类范畴**（一词两义，先约定；思考框架：**新建基础工程与旧功能迁移分离讨论**，只基于上游 + 规范的基线做选型）：

- **开发过程类**——服务于开发活动：规范落地校验（四工具）、定制测试 CI 门禁、上游升级 runbook。→ 阶段二主体；
- **产品运行时类**——服务于运行中的产品。其中**本地诊断包**（定制日志落盘、崩溃现场、环境快照；
  设计基线见 `batches/diagnostics/design.md`）为上游空白且零/极小上游改动，**划入阶段二**；遥测上报为旧分支
  既有功能，随阶段三迁入；企业后端对接等按真实痛点排阶段三之后——**是位置安排，不是从清单里删除**。

### 阶段一：定制规范 —— 已就位（2026-10-25）

- 产出：`standards/`（R1–R11 + 8 篇 ADR + 文档模板 + 评审 HOWTO）、`tools/check_docs.py`、`batches/restructure/plan.md`。
- DoD：规范族签认齐全、check_docs.py 通过。

### 阶段二：基础工程 —— **已完成（2026-10-25，DoD 达成）**

**选型论证——为什么不重复建设通用基础工程**：上游已具备 CI（三平台分片）、Smart Test、
测试体系、uv 依赖锁定、ruff + ty、i18n 工具链、构建与 `check_wheel.py` 打包校验、离线打包、
OTel 遥测框架（opt-in）与 trajectory 会话回放——这些定制化直接继承，不重复建设。
定制化要补的是两类：上游空白且 fork 急需的本地诊断能力；上游不会有、只有 fork 才需要的规范落地校验。

内容清单（2026-10-25 与负责人确认，**不扩大**）——完成状态：

1. ✅ **本地诊断包**（`batches/diagnostics/`，design 签认+R2 扩表重签 2026-10-25）：五模块（config /
   logging_support / crash / snapshot / launcher）+ screen.py MODULE_ORDER 一行 +
   test_layering.py DAG 登记（tier=1 + 边豁免含 stale 检测）；29 定制测试含行为锚定；
   清单 C002–C005；试运行期遗留人工项（F6 实测、launcher 冒烟）见 batches/diagnostics/verification；
2. ✅ **规范落地四工具**（`batches/devtools/`）：清单生成（含考古模式，报告
   `batches/devtools/archaeology-report-0.28.0.md` 即 §4 盘点输入）/ R2 比对 / R3 校验 / import 边界；
   双 AI 评审两轮收敛；
3. ✅ **定制 CI 门禁** `aixcoding-tests.yml`（ADR-0004 四步 + 四工具挂入；base=merge-base
   origin/main_iCode；多次运行全绿；实施副产修复 git core.quotepath 中文路径 bug）；
4. ✅ **上游升级 runbook**：`standards/UPGRADE-RUNBOOK.md`（§8 操作展开：镜像纪律、冲突高发区
   对照、工具命令序列、已知坑速查、双轨期注意）；
5. ✅ **版本与发布策略**（`versioning.md`，2026-10-25 定稿：上游三段 + fork 第四段、
   基线升级重置第四位、分发名保持上游、CHANGELOG 约定）。

- DoD 核验（2026-10-25）：四工具与诊断包挂入 CI 跑绿（AIxCoding Tests 多次 success，含
  诊断包 29 用例与 R2/R3 分叉点门禁）✓；runbook 成文 ✓；诊断包 R2 获负责人签认
  （含实施中扩表重签）✓；版本策略定稿不阻塞 ✓。**阶段二 DoD 全项满足。**
- 附带产出：AGENTS.md 既有入口段追溯补登记（`agents-md-note/`，清单 C001）；R3 规范
  YAML 措辞修正；CUSTOMIZATIONS.md 机制全链路五次验证（签认→标记→清单自动收敛）。
- 克制原则执行情况：清单外未建设（遥测后端对接、mock 体系等留阶段三按痛点驱动）。

### 阶段三：旧功能迁入 —— 当前阶段（阶段二 DoD 达成 2026-10-25 起算）

- 输入：§4 盘点清单（工具①落地的首次实战即产出考古扫描）+ `batches/restructure/plan.md` 考古映射
  （其 §3 顺序 / §4 验收骨架仍有效）。
- 原则：
  - 逐特性迁入，每个特性独立走 design + R2 审批 + 验收，迁入是规范的第一次大规模实战；
  - 上游文件定位以 `AIxCoding` 标记在旧分支考古、在新基线重新比对——**不得照抄旧行号**
    （上游 0.28→0.31 已漂移，plan.md 钉死的行号全部失效）；
  - 旧实现与新规范冲突时改实现不改规范；确需改规范走 ADR 推翻。
- 顺序（2026-10-25 负责人确认，调整自 plan.md §3 依赖顺序）：mock 基建 → auth → tui →
  其余按盘点判定 → **telemetry 殿后**。调整原因：telemetry 迁入路线未定——旧直改源码方案
  侵入面大（11+ 上游文件），"迁入后再撤销"的摘除成本为主要顾虑——**撤销 2026-10-08 定稿的
  直改源码方案作为阶段三迁入路径**，降格为双分支候选路线的服役分支实现；其数据口径规格
  不变，仍为各候选路线的对齐目标。候选路线
  （双分支（主干+直改服役分支）/ 最小侵入重实现（上游挂接点或运行时包装+契约测试）/
  session 最小版先行）由两个调研定夺（上游 0.31.0 挂接点调查、session 文件字段覆盖度
  对照）。调研触发点（2026-10-25 评审补写实）：**阶段三部分验收达成，或首次出现追上游
  版本出服需求**（两者先到为准）即启动两个调研；telemetry design 启动以调研结论为前置
  输入。
- DoD（2026-10-25 顺序调整后拆两档）：
  - **部分验收**（非 telemetry 定制迁入完成）：定制点登记对账全绿（`gen_customizations.py
    --strict`，customization-registry 批次起口径，不含
    telemetry 定制点）；定制测试全绿；上游架构测试绿；离线打包冒烟通过。
  - **完整验收**（部分验收 + telemetry）：telemetry 迁入落地，且数据管道切换验收（新旧
    对拍：同一批真实会话下新旧实现上报口径一致比对）通过。
  - 旧分支归档切换点挂完整验收（见 §3）。

## 3. 双轨期管理

- 旧分支 `main_aixcoding_cli_0.28.0_final` 服役至阶段三完成。
- 冻结点（**已确认**，负责人 2026-10-25）：新分支进入阶段三后，旧分支只修阻塞级 bug、不加功能。
  - 生效条件：阶段二 DoD 满足之日自动生效，无需另行宣布；生效前旧分支开发不受限；
  - **冻结已生效（2026-10-25，阶段二 DoD 满足日，见 §2 完成核验）**；
  - "是否阻塞级"由负责人裁决；
  - telemetry 例外（2026-10-25 补）：telemetry 数据管道切换验收前，旧分支 telemetry 相关
    缺陷影响现网数据上报的，按阻塞级 bug 通道修复（属缺陷修复，不违反功能冻结）；
  - 冻结期间新功能需求一律引导至新分支，从源头防止两条分支继续分叉。
- 切换点：阶段三**完整验收**满足之日（DoD 两档定义见 §2），旧分支转归档。telemetry 殿后
  期间：非 telemetry 定制迁完即达部分验收，可先行开展除 telemetry 外的验收核验；旧分支
  最终归档仍以 telemetry 数据管道切换验收为前提——现网数据上报在其完成前持续依赖旧分支
  （0.28.0 基线），若此期间需追上游版本出服，telemetry 路线决策需提前触发。

## 4. 旧功能盘点清单（阶段三考古输入；2026-11-02 填充）

考古来源：旧分支 `main_aixcoding_cli_0.28.0_final`。2026-10-25 预扫描资产分布：

- 产品代码 `src/chrys/aixcoding/`：根模块 6 py（config / context / git_info / http / update_detection / __init__）+ `telemetry/` 9 py；
- 待迁产品代码：`aixcoding/auth/`（9 py）、`aixcoding/tui/`（含 login.tcss）；
- 文档：活文档 `aixcoding/docs/telemetry/`（9 md）；历史文档 `aixcoding/doc/`（9 md，逐份去向见 plan.md R1）；
- 测试：`aixcoding/tests/`（5 测试 + conftest；预扫描原文"6 测试"为文件计数口径，按考古清单
  修正）、`tests/app/aixcoding/`（11 文件）；
- mock：`aixcoding/telemetry-mock/`、`mock_server/` 下三服务（aixcoding_auth / aixcoding_update / chrys_model_catalog）；
- `src/` 树 `AIxCoding` 标记命中 **39 个文件**——多于既有规范文档记载（`skills/adapter.py`×19、
  `agents/schema.py`×6、`catalog.py`×6、`screens/main/screen.py`×8 等为清单外发现），
  其中含品牌字符串等非注释命中，由工具①甄别为归属标记后方可计入清单。

> **判定口径（2026-11-02 盘点定稿）**
>
> - 三档定义：**照迁**＝逻辑主体原样平移（允许 import/env/接线点机械适配）；**有改动的迁**＝
>   核心逻辑平移但挂接形态/并存策略需 design 重审；**重新实现**＝旧实现不合现行规范或上游已有
>   同目的机制，按新范式重写（旧代码仅作行为参考）。三档均为**迁入方式**；不迁的处置结果
>   （废弃/待裁决/暂缓）是另一分类轴，非第四档。
> - **本表判定均为盘点时点（2026-11-02，基线上游 0.32.2）的建议，不构成实施授权**：各特性
>   批次启动时必须基于当期基线重新做"上游现状 vs 旧定制"分析并呈报建议判定，**由责任人裁决**
>   后方可实施（ADR-0012；上游/旧实现随时间漂移，本表结论会过时）。
> - 盘点方法：`batches/devtools/archaeology-report-0.28.0.md`（57 资产文件 + 24 注释标记）↔
>   新分支现存 22 处 `AIxCoding` 标记求差集；C1–C5 另做上游 0.32.2 逐项对比（见各行依据），
>   C6/C7 仅登记待分析（本轮未覆盖）。**差集法仅为初筛且标记扫描存在盲区**：908 非注释命中
>   并非纯品牌文案（skills 自动加载即命中之一）；安全护栏则**无任何标记命中**、由评审从功能面
>   额外发现（2026-11-02，F-01）——完备性须以逐项甄别辅以功能面核查，本表覆盖不承诺穷尽。

### A. 已迁入（对账记录，无需再裁决）

| 特性 | 旧落点（0.28.0） | 判定 | 依据 / 备注 |
|---|---|---|---|
| mock 三服务 | `mock_server/`（auth / update / model_catalog） | 已完成 | mock 批次（M1-M3，2026-10-26）；telemetry-mock 除外（见 B） |
| auth 登录 + LLM token + ACP 接线 | `aixcoding/auth/` 9 py + `app.py:960` 等挂接点 | 已完成 | auth + tier-migration + auth-logging 批次；env 已更名 `AIXCODING_AUTH_*` |
| tui 登录 UI（/login、徽章、静默校验） | `aixcoding/tui/` + screen / commands / app_header 挂接点 | 已完成 | tui 批次；`login_actions.py` 另埋 catalog 批次占位符×2（见 C2） |
| diagnostics | （阶段二新建，非迁入项） | 已完成 | diagnostics + launcher-retirement（ADR-0013） |

### B. 殿后已定（引用 §2 阶段三既有决策，不重新裁决）

| 特性 | 旧落点（0.28.0） | 判定 | 依据 / 备注 |
|---|---|---|---|
| telemetry 数据上报 | `telemetry/` 9 py + 根模块 http / context / git_info + 17 处产品源码挂接点 + 1 处测试标记 + 活文档 9 md | 殿后（路线待双调研） | §2 阶段三；调研触发点（部分验收达成）已满足 |
| telemetry-mock | `aixcoding/telemetry-mock/` 4 文件 | 随 telemetry | plan.md M4 + 顺序调整注 |
| telemetry 测试 | `aixcoding/tests/` 5 份 | 随 telemetry | plan.md T1 注 |

### C. 待迁功能（判定为建议；批次启动时须重新分析、由人决策）

| 特性 | 旧落点（0.28.0） | 建议判定 | 依据 / 备注（2026-11-02 对上游 0.32.2 分析） |
|---|---|---|---|
| 版本更新检测 | `update_detection.py`（内部 `/api/v2/releases/check` 契约）+ screen.py / app.py 挂接点（header 徽章） | **有改动的迁** | 上游 0.32.2 已有同目的的并行机制 `app/update_check.py`（PyPI/GitHub 公网源 + 每日缓存 + 聊天区通知 + settings 开关），内网静默失败；update mock（M2）模拟内部契约已就位。design 须定：① UI 形态（徽章 vs 通知）与上游 update_check 的并存/禁用策略；② **四段版本号 wire contract 为前置阻塞项**——versioning.md 已定稿 fork 第四段，而旧 `_SEMVER_RE` 与上游 `parse_version()` 均只接受三段，四段版本会在请求前静默跳过（update mock 同为三段校验） |
| 模型目录同步 + 档案集中管理 | `catalog.py`（旧分支新增文件，847 行）+ `registry.py`（RLock 定制）+ `config_actions.py` 只读屏 + 测试×3 | **有改动的迁** | 上游无同名文件（零文件冲突）；旧实现已合新范式（settings 声明引用、tier 对齐 auth + drift test、15min 轮询、只读保护）。**但存在真实 API/并发漂移**（外部评审 F-03 + 复审 R-01）：① 旧同步语义依赖 registry `replace_profiles()`（清空重建，服务端删除可生效），新基线仅有 `install_profiles()`（逐项 register 不删除）——同步重建语义须 design 重审；② 旧 registry 以 `RLock` 保护后台同步线程与 UI 并发访问，新基线各方法无锁——线程安全策略须 design 重审；另 `ProcessSettings` 的 `model_catalog_base_url` 投影新基线无对应；接线面除 settings.py 声明与 `app/aixcoding/startup.py`（`login_actions.py` 占位符已预留）外，还含 F4 编辑禁用、picker "Manage models…" 禁用、登录/登出启停 |
| 平台连接配置 | `config.py`（独立 `aixcoding.yaml` + `AIXCODING_EXTENSION_*`，telemetry 与更新检测共用） | **重新实现（拆解随批次）** | 独立 yaml 双轨制与现行配置范式不同构（现行范式：auth `environments.py` 模式 / settings 声明），不整体照迁——端点配置随消费批次（update / telemetry）按现行范式实现。**逐字段归属须 design 定、不得静默丢失**（外部评审 F-06）：`token`/`userId`（或已由 auth session 覆盖）、`toolParamMode`（工具参数上报 full/whitelist）、`telemetryEnabled`（reporter 装配开关）、`profile`/`customServerUrl` 优先级语义 |
| 品牌定制 | `branding.py` 2 常量（`APP_DISPLAY_NAME` / `APP_COMMAND`）+ docs / README / i18n 文案 | **有改动的迁（时机待产品决策）** | 机制上游原生，常量改动 2 行；文案部分注意口径（外部评审 F-07）：908 为全仓非注释命中（含 skills/护栏等功能内容），**非纯品牌文案数**，改名工作量须先甄别。三个处置面分层：① 显示品牌与文案（`APP_DISPLAY_NAME`）；② 用户命令 `APP_COMMAND`——只改帮助文案与安装别名，**不自动变更 wheel 命令入口**（`[project.scripts]` 独立定义，需另行报批）；③ 分发名保持上游（versioning.md） |
| 内网安装器 | `scripts/install.sh`（433 行，内网制品镜像 + channels + offline 包体系）+ 同族 `install.ps1` / `release/INSTALL.md` / channel 发布约定 | **待裁决（需求侧确认）** | 与上游 install.sh 为平行替代品（无演进关系）。**"废弃"证据不足**（外部评审 F-04）：旧脚本已兼容 `icode-*` 包名（final 后修复）与 PyApp 制品（旧分支已在用）；版本正则接受三段+后缀（覆盖四段）；`release/INSTALL.md` 将内网脚本列为推荐安装渠道（prod/test/Windows）。仅在产品确认取消内网分发体系后方可废弃；若保留，按现制品形态有改动的迁 |
| skills 共享目录自动加载 | `service/skills/adapter.py` 定制（`~/.aixcoding/skills` + 工作区 `.aixcoding/skills` 自动加载、优先级、`auto_load_aixcoding_*` 开关）+ `agents/schema.py` + 测试 + 中英文文档 | **待分析（本轮未覆盖）** | 外部评审 F-01 发现（属上游 skills 机制的定制扩展，非注释命中之一，证明 908 非纯品牌文案）；新分支 `src/` 产品源码树无 `auto_load_aixcoding`（已核验，复审 R-04 收窄范围）。上游对比与判定随批次启动补做 |
| 安全护栏（guard hooks） | `app/cli/app.py` 启动自动安装（`guard_install`，fail-open）+ 规则包 + wheel 打包 + 测试 + CI + MANUAL_TESTS | **待分析（本轮未覆盖）** | 外部评审 F-01 发现（属上游 hooks 机制的定制扩展）；**无任何标记命中**（复审 R-02：标记扫描盲区的实例，由功能面核查发现）；新分支 `src/` 产品源码树无 `guard_install`（已核验）。与 D 类历史文档第 9 份（安全护栏 MANUAL_TESTS）同族关联；上游对比与判定随批次启动补做 |
| **用户指南文档定制化（docs/ 双语树）** | `docs/{en,zh-Hans}/` 整树定制（0.28 final 实测）：新增 start/login、start/what-is-aixcoding、faq、daily-use/build-info、running/aixcoding-serve 等定制指南；删除 external-acp-agents / extensions/shortcuts / reference/opentelemetry / configuration/chat-options / running/terminal-status；`icode-run` / `icode-serve` / `icode-acp` 重命名 `aixcoding-*`；各指南内容定制（如 configuration/models.md 集中管理版："不支持创建模型配置，仅支持切换；模型由管理员分配"） | **待批次启动（后续文档批次；语义参考 0.28 final，按 0.32.2 新结构重做）** | 0.32.2 上游文档结构大幅扩充（如 models.md 由 <1KB 扩为 16-17KB 完整指南），旧定制不可平移；与上游 0.28→0.32 文档自身演进混杂（0.28 final vs 上游现状 diff：75 文件 +2361/-6020，含演进噪音），批次启动时逐份甄别。**文档类定制零 marker 命中**（fork `docs/` 现状零 AIxCoding 标记，属标记扫描盲区、由功能面核查发现——同 F-01 性质）。**文档类待办统一并入本项**；首个并入：catalog-sync 批次 F-6（责任人裁决 2026-11-02）——models.md 双语改写不随 catalog-sync 批次，随本工作项处理 |

### D. 跟随处理

| 项 | 旧落点 | 处理 | 依据 / 备注 |
|---|---|---|---|
| pyproject 全角标点豁免 | `pyproject.toml:201`（RUF001/2/3） | 随批次按需（文件头 `noqa`） | 新分支既有做法即文件头 noqa；不独立迁 pyproject 定制 |
| 历史文档 9 份 | `aixcoding/doc/`（登录系列×5、发布指南、LLM 对接、项目索引、安全护栏 MANUAL_TESTS） | 责任人逐份定去向 | plan.md R1 已载"禁止删除"且枚举 8 份——**第 9 份（安全护栏 MANUAL_TESTS.md）超出 R1 枚举范围**（外部评审 F-08），去向单独定，或随 C7 安全护栏批次一并处理 |
| 杂项资产 4 项（对账闭合） | `aixcoding/__init__.py`、`src/chrys/aixcoding/__init__.py`、`aixcoding/tests/conftest.py`、`aixcoding/README.md` | 随主体处置 | 外部评审 F-02 对账补漏：两个 `__init__.py` 分别随 R2′（开发资产锚点）与 P1（telemetry 根模块）处置；conftest 随 T1 测试迁移；`README.md` 两边均无明确去向，**待责任人定** |

## 5. 变更记录

- 2026-10-25：制定；三阶段顺序经负责人与 AI 评审确认。
- 2026-10-25（二）：阶段二内容重构——确立"新建基础工程与旧功能迁移分离"框架；本地诊断包
  划入阶段二（设计基线 `batches/diagnostics/design.md`）；CI 门禁、升级 runbook 入清单；盘点清单
  剥离回阶段三语境。
- 2026-10-25（三）：**阶段二完成**——五项清单全部落地（四工具 / 诊断包 / CI 门禁 /
  升级 runbook `standards/UPGRADE-RUNBOOK.md` / 版本策略）；DoD 全项核验通过；
  依 §3 条款旧分支冻结自动生效；阶段三启动（迁入顺序待按 §4 盘点表逐项规划）。
- 2026-10-25（四）：**阶段三顺序确认**——telemetry 殿后（mock 基建 → auth → tui → 其余
  按盘点 → telemetry）：撤销 2026-10-08 定稿的直改源码方案作为阶段三迁入路径（降格为
  双分支候选路线的服役分支实现，数据口径规格不变），迁入-撤销成本为主要顾虑，迁入路线
  待两个调研定夺（见 §2 阶段三）；
  DoD 拆两档（部分/完整验收），旧分支归档挂完整验收并补 telemetry 现网缺陷冻结例外（见 §3）。
- 2026-10-30：**diagnostics launcher 退役**（ADR-0013，批次 `launcher-retirement/`）——crash /
  snapshot / launcher 三模块删除、`test_layering.py` 豁免机制回滚，阶段二诊断包由五模块收敛为
  config + logging_support 两模块；上游测试文件足迹归零。
  **补做义务（登记勿遗忘，责任人 2026-10-30 要求明确记录）**：
  ① ~~**gen_customizations 登记源单一化**~~ **已销项（2026-10-31，customization-registry 批次）**：
  登记点级化 + 唯一现行登记行（每定制点唯一 design 承载，tier-migration/ai-gates 双源清空迁移）+
  `CUSTOMIZATIONS.md` 产物退役改对账命令（ADR-0015）；
  ② crash 捕获 / 环境快照能力已按 ADR-0013 退役（价值取舍：足迹清零优先），将来需要时按 ADR-0013
  记录的**内联接线替代路径**实现或从 git 历史复原。
- 2026-11-02：**§4 盘点表填充**——考古报告（57 资产 + 24 注释标记）对照新分支现存 22 处标记求差集；
  C1–C5 另做上游 0.32.2 逐项对比分析。**外部 AI 评审（10 findings）+ 第二轮复审（4 findings）
  全 confirm 后修订落盘**（初版判定经评审修正）：版本更新检测＝有改动的迁（上游有同目的并行
  机制；补四段版本 wire contract 前置，F-05）、模型目录同步＝**有改动的迁**（初版"照迁"被
  F-03 推翻：registry `replace_profiles`→`install_profiles` API 漂移影响同步重建语义；复审
  R-01 再补 `RLock` 并发保护漂移，线程安全须 design 重审）、平台连接配置＝重新实现拆解随批次
  （F-06 补逐字段归属：token/userId/toolParamMode/telemetryEnabled/profile）、品牌＝有改动的迁
  时机待产品决策（F-07 修正 908 口径与 APP_COMMAND 三层处置面）、内网安装器＝**待裁决需求侧
  确认**（初版"废弃"被 F-04 推翻：旧脚本已兼容 icode-* 与 PyApp、INSTALL.md 列推荐渠道）、
  **新增 skills 自动加载与安全护栏两项＝待分析**（F-01 blocker：差集初筛遗漏；复审 R-02 澄清
  安全护栏无任何标记命中，属标记扫描盲区、由功能面核查发现；R-04 收窄检索范围表述为新分支
  `src/` 零命中）；另 F-02 补杂项资产 4 项对账闭合与差集法初筛声明（复审 R-03 收窄总述为
  C1–C5 逐项对比）、F-08 第 9 份文档超出 R1 范围、F-09 处置轴澄清、F-10 三处计数/基线口径
  修正。复审确认 F-01~F-10 处置忠实性（9 项通过 + F-01 部分通过即本轮 R 项）。
  **判定均为建议性质，批次启动时须重新分析、由责任人裁决**（判定口径注见 §4 表前引注）。
- 2026-11-02（二）：**§4 C 类新增"用户指南文档定制化"工作项**——catalog-sync 批次
  第三轮评审 F-6 触发（models.md 用户指南与已定稿 UI 形态失真、0.28 final 曾有集中
  管理定制版——考古盲区实例），顺带考古确认 0.28 final `docs/` 双语树为整树定制
  （新增/删除/重命名/内容定制，75 文件差异含上游演进噪音）；责任人指示文档类待办
  统一并入本工作项（首个并入：models.md 双语改写，catalog-sync F-6 裁决留后续
  文档批次）。
