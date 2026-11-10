# AIxCoding 定制化路线图（ROADMAP）

> 定位：三阶段路线的唯一权威描述——阶段边界、各阶段完成标准（DoD）、双轨期管理。
> 文档分工：`standards/DEVELOPMENT.md` 管"怎么做"（规范）；本文管"按什么顺序做"（路线）；
> `batches/restructure/plan.md` 是阶段三的迁入考古参考（旧分支资产映射与验收骨架）。
> 制定：2026-10-25。

## 1. 背景与动机

第一轮定制化（至旧分支 `main_aixcoding_cli_0.28.0_final`，上游 0.28.0 基线）在无规范状态下进行，
团队各成员做法不一致，追版本维护成本高企。2026-10 重建路线：基于上游最新版本拉新分支
`main_aixcoding_cli`（初始 0.31.0 基线；经 `upstream-merge-v0.32.2`、`upstream-merge-v0.33.0`、
`upstream-merge-v0.34.0`、`upstream-merge-v0.35.1` 批次现 0.35.1），**不带任何
旧功能代码**，仅带入规范体系，按"规范 → 基础工程 → 功能迁入"三阶段重建定制层。

核心约束（与 DEVELOPMENT.md 一致）：对上游源码改动最少最集中，以降低追版本维护成本。

## 2. 三阶段路线

**"基础工程"的两类范畴**（一词两义，先约定；思考框架：**新建基础工程与旧功能迁移分离讨论**，只基于上游 + 规范的基线做选型）：

- **开发过程类**——服务于开发活动：规范落地校验（四工具）、定制测试 CI 门禁、上游升级 runbook。→ 阶段二主体；
- **产品运行时类**——服务于运行中的产品。其中**本地诊断包**（定制日志落盘、崩溃现场、环境快照；
  设计基线见 `batches/diagnostics/design.md`）为上游空白且零/极小上游改动，**划入阶段二**；遥测上报为旧分支
  既有功能，随阶段三迁入；企业后端对接等按真实痛点排阶段三之后——**是位置安排，不是从清单里删除**。

### 阶段一：定制规范 —— 已就位（2026-10-25）

- 产出：`standards/`（R 族规则 + ADR 族 + 文档模板 + 评审 HOWTO）、`tools/check_docs.py`、`batches/restructure/plan.md`。
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
5. ✅ **版本与发布策略**（`versioning.md`，2026-10-25 初定四段方案；2026-11-09 暂改为
   保持上游三段，四段/系列方案待最终裁决、暂不出 ADR；分发名保持上游、CHANGELOG 约定不变）。

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
  版本出服需求**（两者先到为准）即启动两个调研；**2026-11-05 责任人裁决：调研暂不启动，
  待基于 0.33.0 的定制化版本相对稳定后再开始（启动时点以本裁决为准）**；telemetry design
  启动以调研结论为前置输入。
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
>   新分支现存 22 处 `AIxCoding` 标记求差集；版本更新检测、模型目录同步、平台连接配置、
>   品牌与命令切换、内网安装器与分发身份五组另做上游 0.32.2 逐项对比（见各行依据）；
>   skills 自动加载与安全护栏当时仅登记待分析（本轮未覆盖）。**差集法仅为初筛且标记扫描存在盲区**：908 非注释命中
>   并非纯品牌文案（skills 自动加载即命中之一）；安全护栏则**无任何标记命中**、由评审从功能面
>   额外发现（2026-11-02，F-01）——完备性须以逐项甄别辅以功能面核查，本表覆盖不承诺穷尽。
>
> **分区字母口径（2026-11-07 重排起生效）**：A 已迁入 / B 待迁功能 / C 殿后已定 /
> D 已裁决维持现状 / E 跟随处理 / F 将来重构候选。§5 早于本次重排的历史记录及已签认
> 批次文档中的旧字母（B=殿后、C=待迁、D=跟随、E=重构）保留历史原义，不回改。

### A. 已迁入（对账记录，无需再裁决）

| 特性 | 旧落点（0.28.0） | 判定 | 依据 / 备注 |
|---|---|---|---|
| mock 三服务 | `mock_server/`（auth / update / model_catalog） | 已完成 | mock 批次（M1-M3，2026-10-26）；telemetry-mock 除外（见 C） |
| auth 登录 + LLM token + ACP 接线 | `aixcoding/auth/` 9 py + `app.py:960` 等挂接点 | 已完成 | auth + tier-migration + auth-logging 批次；env 已更名 `AIXCODING_AUTH_*` |
| tui 登录 UI（/login、徽章、静默校验） | `aixcoding/tui/` + screen / commands / app_header 挂接点 | 已完成 | tui 批次；`login_actions.py` 另埋 `catalog-sync` 批次占位符×2（见 A 类"模型目录同步 + 档案集中管理"行） |
| diagnostics | （阶段二新建，非迁入项） | 已完成 | diagnostics + launcher-retirement（ADR-0013） |
| 模型目录同步 + 档案集中管理 | `catalog.py`（旧分支新增文件，847 行）+ `registry.py`（RLock 定制）+ `config_actions.py` 只读屏 + 测试×3 | 已完成 | catalog-sync 批次（PR #85，2026-11-03 合并：790b562e 主体 + 2164186e 双评审处置 + a4ea553a CI 修复）；服务端目录整替（两阶段发布）、15min 轮询、恒只读、登录联动启停；盘点 F-03/R-01 两处 API/并发漂移（`replace_profiles()` 同步语义 / `RLock` 线程安全）经 design 重审落地；ADR-0018（内网部署明文 HTTP）；批次文档 `batches/catalog-sync/`。后续增强：catalog-auth-gating（PR #89，2026-11-04 合并：登录失效清理/轮询启停 UI 隔离/中文 detail，C132）与 catalog-readonly-coverage（PR #92，2026-11-05 合并：busy 四态恒只读与指示器 configure 入口正向覆盖，C134/C135） |
| 功能品牌与命令切换 | `branding.py` 2 常量 + 运行时散点 + 用户命令与离线制品 / 安装别名 + i18n 文案 | 已完成 | branding-and-command 批次（PR #87，2026-11-03 合并：f9d62145 单提交，merge eb2e7a80，PR 腿双 CI 绿）；82 定制点 C050-C131 + kernel 档 `command_migration.py` 单源归属判断与安全清理；GLM 三轮评审收敛 + PR 评审两候选记档（批次 verification）；ADR-0019；批次文档 `batches/branding-and-command/`。边界：`iCode-TUI` 分发名/包身份归 D 类"分发身份 `iCode-TUI`"（ADR-0021），纯文档品牌归"用户指南文档定制化"工作项 |
| 内网安装器与发布约定 | `scripts/install.sh`（433 行，内网制品镜像 + channels + offline 包体系 + `--from` 离线安装）+ `install.ps1`（51 行，仅在线单平台，无 `--from`/SHA-256/`icode-*` 回退——两平台功能不对称）+ `release/INSTALL.md` + channel 发布约定 | 已完成 | internal-installer 批次（PR #97，2026-11-06 合并：4c85018c 主体 + a85816fa 评审修复 C1-C4，merge 28b7d1c7）：并存新增 `aixcoding/release/`（install-internal-mirror.{sh,ps1} 双脚本 + INSTALL.md 一般用户版 + 支持手册 `docs/install-uninstall-support.md` + publishing.md 发布约定），上游 `scripts/install.*` 与其测试零触碰；统一镜像 IP + `AIXCODING_*`/`ICODE_*` 双读逃生门（显式参数 > 新 > 旧 > 默认）；旧 `create_launch_command` 与 `aixcoding` 旧命令不引入（0.33/品牌与命令切换契约适配）；ps1/sh 维持不对称（D2）；ADR-0020；契约测试 44+2（Linux POSIX 用例由 PR CI ubuntu 实跑关闭，macOS 缺口维持）；**真实制品端到端验收缺口 5 项在案**（批次 verification「尚未执行的验收与覆盖缺口」，不得称全部验收完成） |
| 安全护栏（guard hooks） | `app/cli/app.py` 启动自动安装（`guard_install`，fail-open）+ 规则包 + wheel 打包 + 测试 + CI + MANUAL_TESTS；旧分支盘点后演进（2026-11-04 PR #90：hooks.yaml 超时×4 + guard_install settings 键同步 +126 行） | 已完成 | guard-hooks 批次（PR #98，2026-11-06 合并，merge c238ea05，分支 lhc/安全护栏_0.33.2）：规则包落 tier 内 `src/chrys/kernel/aixcoding/guard/pack/`（随包、无 force-include）+ 接线 `startup.py` bootstrap_runtime（C140，覆盖全部会话入口）+ pyproject lint profile（C141）；F7 写入范围增强（紧贴重定向/zip/Copy-Item·Move-Item/junction 指外拦截，2026-11-05 责任人裁决）；运维手册重建为批次 `rule-pack/` 四文档（MANUAL_TESTS 演进至 106 条）。姊妹批次 guard-switches（运行期开关：guardctl/guardctl.cmd + guard_state.json 现场状态 + `AIXCODING_GUARD_*` 环境变量，同事 v2 交付逐块合成、含 bool("false") 静默反向修正，ADR 0001 出厂 `allow_outside_workspace=true`，C142）。合入后修复：POSIX Windows 风格 cwd 锚定（b7de350b）、bootstrap 自动装护栏污染 pytest 沙箱（6e2dd9c4）；R13 目录级豁免（4ef0cdb7）；selftest 100 条 + 部署验证 64 例 0 失败 + 三平台 CI 绿 |
| 平台连接配置 | `config.py`（独立 `aixcoding.yaml` + `AIXCODING_EXTENSION_*`，telemetry 与更新检测共用） | 已完成 | environment-config 批次（PR #103，2026-11-09 合并：f4dbf024 主体 + a4e8a468 PR peer review C1 补测，merge 30c94550）：部署环境统一 `AIXCODING_ENVIRONMENT`（local/dev/prod），共享配置落 `~/.chrys/aixcoding.toml`（顶层 environment + `[model_catalog].base_url`），模型目录私有键退出上游 Settings；service 共享快照 + app 启动注入以遵守 kernel 跨子包约束。零向后兼容：旧变量不读取/不检测/不告警，模型目录既有退役 WARNING 一并删除。未来 update/telemetry 仅固定 `[update]`/`[telemetry]` 契约，不写占位实现；token/userId 由 auth session 提供。ADR-0022；批次文档 `batches/environment-config/` |

### B. 待迁功能（判定为建议；批次启动时须重新分析、由人决策）

| 特性 | 旧落点（0.28.0） | 建议判定 | 依据 / 备注（2026-11-02 对上游 0.32.2 分析） |
|---|---|---|---|
| 版本更新检测 | `update_detection.py`（内部 `/api/v2/releases/check` 契约）+ screen.py / app.py 挂接点（header 徽章） | **有改动的迁** | 上游 0.32.2 已有同目的的并行机制 `app/update_check.py`（PyPI/GitHub 公网源 + 每日缓存 + 聊天区通知 + settings 开关），内网静默失败；update mock（M2）模拟内部契约已就位。design 须定：① UI 形态（徽章 vs 通知）与上游 update_check 的并存/禁用策略；② **四段版本号 wire contract**（2026-11-09 起版本号暂保持三段、此项暂不构成阻塞；未来启用四段/系列时仍为前置项）——旧 `_SEMVER_RE` 与上游 `parse_version()` 均只接受三段，四段版本会在请求前静默跳过（update mock 同为三段校验）；2026-11-05 复核：0.33.0 下 update_check.py 零改动（含 parse_version 三段解析）；2026-11-06 复核：0.34.0 下该文件仍零改动，本行上游对比结论无漂移；**范围边界（D 类分发身份/ADR-0021）**：版本更新检测批次只做更新链内部化，卸载提示与 `install` 误用提示中的 `iCode-TUI` 是真实包管理器命令，不随该批次做品牌性替换 |
| skills 共享目录自动加载 | `service/skills/adapter.py` 定制（`~/.aixcoding/skills` + 工作区 `.aixcoding/skills` 自动加载、优先级、`auto_load_aixcoding_*` 开关）+ `agents/schema.py` + 测试 + 中英文文档 | **待分析（本轮未覆盖）** | 外部评审 F-01 发现（属上游 skills 机制的定制扩展，非注释命中之一，证明 908 非纯品牌文案）；新分支 `src/` 产品源码树无 `auto_load_aixcoding`（已核验，复审 R-04 收窄范围）。上游对比与判定随批次启动补做 |
| **用户指南文档定制化（docs/ 双语树）** | `docs/{en,zh-Hans}/` 整树定制（0.28 final 实测）：新增 start/login、start/what-is-aixcoding、faq、daily-use/build-info、running/aixcoding-serve 等定制指南；删除 external-acp-agents / extensions/shortcuts / reference/opentelemetry / configuration/chat-options / running/terminal-status；`icode-run` / `icode-serve` / `icode-acp` 重命名 `aixcoding-*`；各指南内容定制（如 configuration/models.md 集中管理版："不支持创建模型配置，仅支持切换；模型由管理员分配"） | **待批次启动（后续文档批次；语义参考 0.28 final，按 0.32.2 新结构重做）** | 0.32.2 上游文档结构大幅扩充（如 models.md 由 <1KB 扩为 16-17KB 完整指南），旧定制不可平移；与上游 0.28→0.32 文档自身演进混杂（0.28 final vs 上游现状 diff：75 文件 +2361/-6020，含演进噪音），批次启动时逐份甄别。**文档类定制零 marker 命中**（fork `docs/` 现状零 AIxCoding 标记，属标记扫描盲区、由功能面核查发现——同 F-01 性质）。**文档类待办统一并入本项**；首个并入：catalog-sync 批次 F-6（责任人裁决 2026-11-02）——models.md 双语改写不随 catalog-sync 批次，随本工作项处理。**第二个并入：品牌与命令文档切换（2026-11-03 重裁，不再设独立子批次）**——功能品牌与命令由品牌与命令切换批次闭环，本独立文档项仅处理 docs/README/AGENTS 完整文案等纯说明材料（该批次已修复其中立即失效的最小开发命令）；除品牌词外还须同步 `aixcoding-cli` 命令示例、D 类已裁决的分发身份、离线包名和相关文件名。待目标上游版本及其他功能改动确定后集中实施，不再设独立品牌文档期限；双语同批成对改、半自动+不可替换清单先行，与结构定制（新增/删除/重命名/内容改写）合并规划，勿另起并行批次 |

### C. 殿后已定（引用 §2 阶段三既有决策，不重新裁决）

| 特性 | 旧落点（0.28.0） | 判定 | 依据 / 备注 |
|---|---|---|---|
| telemetry 数据上报 | `telemetry/` 9 py + 根模块 http / context / git_info + 17 处产品源码挂接点 + 1 处测试标记 + 活文档 9 md | 殿后（路线待双调研） | §2 阶段三；调研启动时点（2026-11-05 责任人裁决）：**暂不启动，待基于 0.33.0 的定制化版本相对稳定后再开始**（原句"触发点（部分验收达成）已满足"与 DoD 字面不符而订正，见 §5 2026-11-05（三））；channelName 口径交叉：旧分支 PR #59（2026-10-26）已统一 CLI/TUI/Web channelName 为 `icode`（telemetry reporters + ACP 文档双语），新分支品牌为 `aixcoding-cli`（品牌与命令切换批次）——迁入/数据口径对齐时须裁决该字段口径 |
| telemetry-mock | `aixcoding/telemetry-mock/` 4 文件 | 随 telemetry | plan.md M4 + 顺序调整注 |
| telemetry 测试 | `aixcoding/tests/` 5 份 | 随 telemetry | plan.md T1 注 |

### D. 已裁决维持现状（不迁；重开须新 ADR）

> 定位：登记经责任人签认 ADR 裁决"不迁移/保持上游现状"的事项——与 B 类"待迁功能"分轴。
> 此类事项不再是待迁清单；重开仅按对应 ADR 的触发条件执行，且须以新 ADR 取代原决策，
> 不因批次启动自动重审。

| 事项 | 处置对象 | 裁决 | 依据 / 重开条件 |
|---|---|---|---|
| 分发身份 `iCode-TUI` | **无 0.28 定制落点**——0.28 包名 = `chrys`（与上游 v0.28.0 原版逐字一致、从未品牌化）；`iCode-TUI` 系上游 943142e4（2026-10-08，v0.29.1 首含）引入、新基线 0.31.0+ 继承的现状（2026-11-05 考古勘误） | **已裁决：沿用上游（ADR-0021，2026-11-07 签认）** | ①可见性核查（2026-11-05）：内网离线包用户正常交互路径不暴露包名（OFFLINE 卸载提示返回 None；若启用四段则四段版本会被三段 parse 拦下不展示更新通知——OFFLINE 更新检查仍外呼 GitHub，内部化归 B 类"版本更新检测"）；仅 pip/uv/pipx 场景可见：Python 运行时 3 处（update_check.py:77-81 升级命令 / install_flavor.py:90-94 卸载提示 / installer.py:585 误用提示）+ 安装脚本 ≥1 处（install.sh:159），均为真实可执行命令、禁品牌性字符串替换；`pyapp/icode-tui/` 仅 pip/uv flavor 场景存在，非离线用户可见。②裁决时点 `versioning.md` §2 曾同向预定"分发名/wheel 名保持上游、四段版本即身份标识"（该论据随 2026-11-09 版本策略临时调整暂不成立，待最终裁决后同步）；ADR-0021 已签认正式确认（2026-11-07），不沿用则须以新 ADR 取代并同步修订该文档。③沿用上游的前提与重开条件（ADR-0021）：内网仅受控 PyApp 离线分发（内网安装体系已于 internal-installer 批次迁移落地，见 A 类）；启用内部 pip/uv、对外发布、品牌合规覆盖分发层或出现实际同名跨源误升级风险时重开裁决，且先定义内部索引、公共源隔离与防同名跨源误升级（上游三段版本在比较上胜过内部四段）。④若更名（须新 ADR 取代 ADR-0021）：处理 wheel 前缀、uv/pipx 新旧包迁移、更新检查、PyApp/build/CD 引用，保留内网镜像、channels 与发布策略 |

### E. 跟随处理

| 项 | 旧落点 | 处理 | 依据 / 备注 |
|---|---|---|---|
| pyproject 全角标点豁免 | `pyproject.toml:201`（RUF001/2/3） | 随批次按需 | 不独立迁 pyproject 定制；新分支现做法为 pyproject 目录级 `per-file-ignores`（PR #71 ruff-test-exemptions 起，此前曾用文件头 `noqa`） |
| 历史文档 9 份 | `aixcoding/doc/`（登录系列×5、发布指南、LLM 对接、项目索引、安全护栏 MANUAL_TESTS） | 责任人逐份定去向 | plan.md R1 已载"禁止删除"且枚举 8 份——**第 9 份（安全护栏 MANUAL_TESTS.md）超出 R1 枚举范围**（外部评审 F-08），去向单独定，或随安全护栏批次一并处理（2026-11-06 注：guard 批次已重建运维手册为批次 `rule-pack/` 四文档、MANUAL_TESTS 演进至 106 条，旧原件去向裁决仍待责任人） |
| 杂项资产 4 项（对账闭合） | `aixcoding/__init__.py`、`src/chrys/aixcoding/__init__.py`、`aixcoding/tests/conftest.py`、`aixcoding/README.md` | 随主体处置 | 外部评审 F-02 对账补漏：两个 `__init__.py` 分别随 R2′（开发资产锚点）与 P1（telemetry 根模块）处置；conftest 随 T1 测试迁移；`README.md` 两边均无明确去向，**待责任人定** |

### F. 将来重构候选（暂不实施；触发再评估）

> 定位：登记经责任人裁决"暂不做、将来再判断"的重构项——与 B 类"待迁功能"分轴（F 类无旧分支
> 对应资产，是对现状架构的重构评估）。触发条件满足时先做触发核实与必要性分析呈报，由责任人
> 裁决后再立项（ADR-0012；触发条件命中不自动开工）。

| 重构项 | 触发再评估条件（先到为准） | 2026-11-03 评估结论（暂不实施的依据） |
|---|---|---|
| 定制层公共基础层/服务层提取（tier 地址注册表、共享常量、网络访问封装、通用工具；形态参考 aixcoding-continue `core/aixcoding/{types,network,utils}` 公共层） | ①定制功能增多后出现 **≥2 个真实跨特性共享消费方**（lazy import 接口复用无法覆盖的共享形态）；②**第 3 个 tier 地址消费方**出现（最可能为 telemetry 或版本更新检测批次）。届时随消费批次 design 一并裁决模块归属与 tier 放置（ADR-0011 适配），或按需独立批次走 §6 管线（涉 ADR-0011 补充则加 ADR） | 新分支 4 特性（auth / diagnostics / catalog / tui）实测无提取对象：地址仅 2 定义点且已有 drift test 防漂移（`aixcoding/tests/catalog/test_catalog_sync.py`）；网络访问 3 调用点语义刻意不同（auth 请求-响应 + `trust_env=False` vs catalog 轮询拉取，错误类型为特性域各归各）；常量零跨特性重复；唯一真实共享点（当前用户身份）已用接口复用解决（catalog `_catalog_user_id()` lazy import auth `stored_user_id`，旧分支 telemetry `current_user_id()` 同模式）。考古证据：旧分支 `src/chrys/aixcoding/` 根模块 5 py 实证为 telemetry 支撑模块长在根上（http / context / git_info 消费方均仅 telemetry 家族），属伪公共层——tier 放置（ADR-0011）为纠偏而非丢失；对比侧 aixcoding-continue 公共层系多 feature 共享密度长出（5 URL × 3 profile × 多 feature），且集中止步于 core 层（vscode / IntelliJ 扩展各持独立地址表、无跨层一致性测试）——"集中"为层内相对集中，边界由跨语言/跨进程结构决定。2026-11-09 environment-config 批次仅承担共享配置与环境解析子集（service config + app 注入），按触发条件①路径随消费批次裁决实施（ADR-0022 已确认）；地址注册表、网络封装、通用工具等其余子项仍按本行触发条件独立评估，不整项销项 |
| 安全护栏规则包外置分发（`rules.json` 等规则/数据文件与二进制产物解耦——现随 wheel/PyApp 打包于 `src/chrys/kernel/aixcoding/guard/pack/`，系快速实现的临时措施，责任人 2026-11-07 认定；重构方向：规则文件作为独立配置/数据文件分发，规则更新不依赖二进制重建发版） | ①出现不随版本发布的规则更新需求（如安全事件需即时下发新规则）；②规则维护方与二进制发布方职责分离需求明确（运维/安全侧直接维护规则文件）；③分发体系（ADR-0020 内网镜像/安装器）出现被复用的独立数据文件下发通道 | 2026-11-07 登记：现状可用——运行时读部署副本 + `AIXCODING_GUARD_RULES` env 覆盖逃生门在位；规则演进走仓库源文件随版本发布（批次 `rule-pack/SWITCHES.md` 已载双通道口径）。立项须裁决的开放问题：外置分发机理（挂 ADR-0020 体系或独立机制）、外置规则文件缺失/损坏时的 fail-closed 语义、`guard_state.json` 开关状态与外置规则的归属关系、规则 schema 与 `_guard_core.py` 引擎版本配对兼容 |

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
- 2026-11-03：**C4 品牌定制拆分定稿**——责任人逐点裁决 D1-D7（拆分方案讨论稿经外部
  AI 评审 8 findings + 逐条只读核实后收敛）：C4a 运行时品牌（常量 2 + 插值散点 6 文件 +
  YAML 直改+marker 2 文件 + serve banner 旧分支纯 ASCII 先例 + locales 2 条，零 docs/
  触碰）先行、待 ADR-0012 开工判定；C4b 文档品牌并入"用户指南文档定制化"（第二个并入
  项，限期 2026-11-05 暂定）；install 脚本品牌/安装别名归 C5、pyproject `[project.urls]`
  随 C1、NOTICE 实测零品牌命中。决策记录（聊天侧材料）：
  `fork/temp/2026-11-02-c4-branding-split-decision.md`；细节以批次 design.md 签认版为准。
- 2026-11-03（二）：**登记将来重构项"定制层公共基础层/服务层提取"（§4 E 类，决策：暂不实施）**
  ——跨工程实践对比触发（aixcoding-continue 公共层 `core/aixcoding/{types,network,utils}` vs
  fork 按 tier 分散的定制代码；端点地址散落为切入子问题）。三方实证核查（新分支 4 特性现状 /
  旧分支根模块考古 / 对比侧公共层形态）结论均支持暂不实施（证据与触发条件详见 §4 E 类）：
  当前无真实提取对象，跨特性共享需求已由接口复用（lazy import）覆盖；旧分支"根模块公共层"
  实证为 telemetry 支撑模块（伪公共层），tier 放置为纠偏而非丢失。责任人裁决（2026-11-03）：
  **暂时不做重构，登记将来重构项**——将来定制功能增多、触发条件满足时再评估是否重构；
  届时先核实触发条件与必要性呈报，由责任人裁决后再立项。
- 2026-11-03（三）：**catalog-sync 对账迁移**——PR #85 合并进 `main_aixcoding_cli`
  （2026-11-03：790b562e 主体 + 2164186e 双评审处置 + a4ea553a CI 修复），"模型目录同步 +
  档案集中管理"自 §4 C 类迁入 A 类"已迁入"（对账记录：漂移处置与接线细节见批次文档
  `batches/catalog-sync/`）。INDEX.md 为零状态导航（ADR-0016）、DIRECTORY.md 已含
  catalog-sync 批次注记，均无过时表述，无需联动更新。
- 2026-11-03（四）：**C4 品牌批次重构**——代码盘点证实 `APP_COMMAND` 同时驱动 CLI
  文案、离线 alias 与卸载发现，原"文案先行、入口/安装别名归 C5"的窗口方案放弃。现行
  只保留 C4（目录 `branding-and-command`），运行时品牌、wheel 单命令 `aixcoding-cli`、
  PyApp/CD 制品、install 脚本、旧 alias 安全迁移与卸载同批闭环；C5 负责 `iCode-TUI`
  分发身份裁决/迁移和内网安装体系。ADR-0019 草案原地重写。不再设 C4a/C4b；纯
  docs/README/AGENTS 归独立用户指南项，待目标上游版本和功能改动确定后集中实施。
- 2026-11-03（五）：**C4 实施完成、三轮评审收敛**——`branding-and-command` 全链落地
  （82 定制点 C050-C131 + 迁移/卸载矩阵 + 定制测试）；GLM 三轮评审收敛（F-1/F-2/F-3 修复、
  F-4 裁决留痕、F-5 拍板不修）；全量 33262+1237 零失败、strict 94/131、check_r2_diff/
  check_r3_markers/import 边界全绿；design D 节 R2 表路径补全待重签、self-review 待签认；
  tagged CD/三平台/POSIX/Linux oracle 归 CI。INDEX.md 为零状态导航（ADR-0016），无需联动。
- 2026-11-03（六）：**branding-and-command 对账迁移**——PR #87 合并进 `main_aixcoding_cli`
  （f9d62145 单提交，merge eb2e7a80，PR 腿 Tests + Upstream CI 双绿），"功能品牌与命令
  切换"自 §4 C 类迁入 A 类"已迁入"；PR #87 评审两候选（C1 外部 icode 进程名误伤面 /
  C2 verification 数字口径）经裁决记档于批次 verification.md，不单独提交。INDEX.md
  零状态导航无需联动。
- 2026-11-09：**版本策略临时调整**——版本号暂保持上游三段、与基线一致（四段/系列方案为
  候选、未最终裁决，暂不出 ADR）。同步修订 `versioning.md`、`standards/UPGRADE-RUNBOOK.md`
  §6 第 4 步、本文 §2 第 5 项、§4 B 类"版本更新检测"的四段 wire contract 表述与 D 类
  "分发身份"行的引用时态；ADR-0021（"四段版本号即身份标识"论据）暂不修订、待版本号方案
  最终裁决后同步。
- 2026-11-05：**D 类"pyproject 全角标点豁免"备注订正**——做法口径由"文件头 noqa"更新为
  现状"pyproject 目录级 per-file-ignores"（PR #71 ruff-test-exemptions 起，此前曾用文件头
  noqa）；处理列"随批次按需"核心结论不变。
- 2026-11-05（二）：**§4 旧分支增量对账 + 基线订正**——考古基准 8476b962（PR #83，2026-11-02）后
  旧分支仅增 PR #90（2026-11-04，guard hooks 超时调整 + guard_install settings 键同步），C7 旧落点
  增补、判定不变；§1 基线 0.32.2→0.33.0（upstream-merge-v0.33.0，PR #91）；C1 复核 0.33.0 下
  update_check 零改动无漂移；B 类 telemetry 增 channelName 口径交叉备注（旧=icode vs 新品牌
  aixcoding-cli）；核实 catalog-sync 已完整覆盖旧分支 PR #72/#83 的模型管理入口隐藏（删除+marker
  形态），A 类行无需修订。
- 2026-11-05（三）：**全文系统核对订正（第二件事）**——①B 类 telemetry"调研触发点（部分
  验收达成）已满足"与 DoD 字面不符（"非 telemetry 定制迁入完成"限定未满足；技术核验四项
  口径则已达成、两次追上游是否属出服需求待认定）——按责任人裁决订正为：**调研暂不启动，
  待基于 0.33.0 的定制化版本相对稳定后再开始**（§2 触发点规则句同步加注）；②阶段一产出
  "R1–R11 + 8 篇 ADR"去具体数字（R 族/ADR 族经批次持续扩充，改提 R 族规则 + ADR 族，避免
  数字快照过时）；③核对确认无问题项：全文引用路径有效（docs-reorg 后已同步）、
  windows-matrix / composition-tests / local-verify / ai-gates 批次为工程/流程类不进 §4、
  §2 迁入顺序与 §3 双轨期表述与实际一致。
- 2026-11-05（四）：**C5 行考古勘误 + 拆行**——包名时间线修正：0.28 定制版包名 = `chrys`
  （与上游 v0.28.0 原版逐字一致，无 `DISTRIBUTION_NAME` 概念），`iCode-TUI` 系上游 943142e4
  （2026-10-08，v0.29.1 首含）引入、新基线 0.31.0+ 继承的**现状**，非 0.28 旧定制遗产——原
  158 行"旧落点（0.28.0）"列口径有误。混合行按处置轴拆为"内网安装器与发布约定"（旧定制
  迁移，体系保留前提已定，按 0.33/C4 契约适配）与"分发身份 `iCode-TUI`"（新基线现状处置，
  待需求侧裁决）两行；他处 C5 引用语义不变（合称两行）。外部 AI 评审收敛（聊天侧，未落盘）：
  离线"零可见"限定为包名不暴露（OFFLINE 更新检查外呼 GitHub 归 C1）；可见点修正为 Python
  运行时 3 处 + 安装脚本 ≥1 处（pip/uv 场景）；C1 只承担更新链内部化、不做包名替换；A1 迁移
  防原样平移（旧 `aixcoding` 命令不再引入、ps1/sh parity 单独裁决、`ICODE_*` 双读兼容）。
- 2026-11-06：**追上游 v0.34.0**——upstream-merge-v0.34.0 批次（PR #99 合并：502cb8de 主体 +
  553eceb0 评审留痕，merge 8f853df6），基线 0.33.0→0.34.0（§1 已更新）：冲突 5 处全解
  （screen.py TYPE_CHECKING 融合 / chrys.mo 重生成 / registry.py C030/C031 融合入锁 /
  catalog oracle 2628→2705 / rollback C102 融合）+ merge 后 4 处新测试适配 C136-C139
  （3 函数级 skip + 1 品牌断言）；C030/C031 淘汰候选经对比分析由责任人裁决保留（上游
  reload 协议无跨线程安全保证，不构成等价命中）；fork-full 34869+1247 全绿、TUI 冒烟
  通过（责任人人工验证）、macOS CI flake re-run 后双绿；strict 98/135→102/139；
  C1 复核 0.34.0 下 update_check.py 仍零改动（本日 git log 核实）。
- 2026-11-06（二）：**internal-installer 对账迁移**——PR #97 合并（merge 28b7d1c7），
  "内网安装器与发布约定"自 §4 C 类迁入 A 类"已迁入"；真实制品端到端验收缺口 5 项
  留痕批次 verification（不得称全部验收完成）；"分发身份 `iCode-TUI`"行加注内网安装
  体系已落地。顺带补记 catalog 域后续增强批次于 A 类 catalog 行（catalog-auth-gating
  PR #89 / catalog-readonly-coverage PR #92）。
- 2026-11-06（三）：**guard 对账迁移**——PR #98 合并（merge c238ea05，分支
  lhc/安全护栏_0.33.2，含 guard-switches 姊妹批次同事交付合成），"安全护栏（guard
  hooks）"自 §4 C 类迁入 A 类"已迁入"；D 类第 9 份文档行加注手册重建事实（rule-pack/
  四文档、MANUAL_TESTS 106 条，旧原件去向仍待裁决）；登记终态 105 行/142 marker/
  13 designs（guard 贡献 C140/C141/C142，含 0.34.0 撞号重编）。
- 2026-11-07：**C5 分发身份决策（ADR-0021）**——按轻量纯文档路径落盘：B1 沿用
  上游 `iCode-TUI`，接受 pip/uv/pipx 场景真实包名可见性，禁止品牌性替换命令；C1 仅
  承担更新链内部化；重开条件=内部 pip/uv、对外发布、品牌合规覆盖分发层或实际同名
  跨源误升级风险。ADR-0019 已签认，依其头部规则不改正文，由 ADR-0021 回指闭合其预留
  子决策；`versioning.md` §2 补决策出处。ADR-0021 已于 2026-11-07 责任人签认，本表
  C5 行已改"已裁决"。
- 2026-11-07（二）：**§4 分区重排（责任人裁决）**——新增"已裁决维持现状"节并调整
  顺序为 A 已迁入 / B 待迁功能 / C 殿后已定 / D 已裁决维持现状 / E 跟随处理 /
  F 将来重构候选；分发身份 `iCode-TUI` 行自待迁节移入 D 类。早于本次重排的 §5
  记录与已签认批次文档保留旧字母与旧规划编号历史原义；当前 ROADMAP/versioning 的
  live 引用改为具体事项名或新分区（A 类 mock 行"见 C"、F 类与 B 类分轴表述、
  `versioning.md` 分发身份出处）。纯结构重排，各条目决策内容不变。
- 2026-11-07（三）：**§4 F 类新增"安全护栏规则包外置分发"重构候选**——责任人指示登记：guard 批次（PR #98）将规则包随 wheel/PyApp 打包系快速实现的临时措施，`rules.json` 作为配置/数据文件应独立分发（规则更新不依赖二进制重建发版）；现状逃生门在位（部署副本 + `AIXCODING_GUARD_RULES` env 覆盖），规则演进暂走源文件随版本发布（`rule-pack/SWITCHES.md` 已载双通道口径）；触发再评估条件与立项须裁决的开放问题（分发挂靠 ADR-0020 或独立机制、外置文件缺失/损坏的 fail-closed 语义、`guard_state.json` 归属、规则 schema 与引擎版本配对）见 F 类行。
- 2026-11-09（二）：**environment-config 对账迁移**——PR #103 合并进 `main_aixcoding_cli`（f4dbf024 主体 + a4e8a468 PR peer review C1 补测，merge 30c94550），"平台连接配置"自 §4 B 类迁入 A 类"已迁入"；对账更新由合并后的独立纯文档 PR 落盘。批次本体、三轮 GLM 复审与 PR peer review 记录见 `batches/environment-config/`；INDEX.md 零状态导航（ADR-0016）无需联动。
- 2026-11-10：**追上游 v0.35.1**——upstream-merge-v0.35.1 批次（PR #105 合并：edec1539 主体 +
  c022feef 收尾评审 + e06701ff 补验闭环，merge b2899590），基线 0.34.0→0.35.1（§1 已更新）：
  冲突 4 处全解（AGENTS.md 定制段双块保留 / chrys.po obsolete 区两侧双留 / chrys.mo 接受删除
  改构建时编译 / catalog oracle 去计数化 C028 退役）+ merge 后 2 处新测试品牌断言适配
  C143/C144；C004/C140 淘汰候选经对比分析由责任人裁决保留（上游扩展机制无进程启动时机
  承载、tool hooks 为扩展编程接口非规则包分发通道）；i18n 构建机制适配（mo 由 build hook
  生成）；fork-full 复跑真实语义冲突清零（32 环境性 WinError 448 失败在案，OS 级对照坐实）+
  architecture 1248 全绿、TUI 冒烟通过（--version=0.35.1）、双 CI 全绿（aixcoding-tests +
  上游 ci.yml 17 job）；strict 100/137→101/138、6 退役。
