# 自评审记录：内网安装器与发布约定迁移（internal-installer）

<!--
签认规则（R10 · 记录档）：责任人本人编辑本头部（状态→已确认、填日期，确认意见可选），AI 不得代填。
签认一次后，后续追加评审记录不触发重签。
双 AI 规则：评审 AI 必须与主力开发 AI 不同（不同工具或不同模型），如实记录标识。
完整操作见 SELF-REVIEW-HOWTO.md。
-->

- 责任人：周彬
- 状态：已确认
- 确认日期：2026-11-06
- 确认意见：无特别意见
- 开发 AI：Kimi-K3.1-Pro（CodeBuddy）
- 评审 AI：GLM-5.3（CodeBuddy，独立评审模式）

> 当前状态：两轮评审均通过，第 2 轮三项 nit 经责任人裁决不修；责任人已于 2026-11-06 签认。
> 五项真实环境验收缺口仍在案，本结论不表示全部端到端验收完成。

## 2026-11-05 第 1 轮：规范联动修订评审

### 范围与结论

- 评审对象仅限 4 个已跟踪文档修改：`aixcoding/README.md`、`aixcoding/docs/INDEX.md`、
  `DEVELOPMENT.md`、`DIRECTORY.md`。
- untracked 的 `release/` 三物料、契约测试、批次文档和 ADR-0020 只做引用完整性交叉核实，
  **未进行代码级评审**。
- 结论：**blocker 0**；R2 零上游、R11 零依赖、INDEX/ADR/release 引用完整、DIRECTORY
  判定树顺序与四处定性同步均通过。
- 治理校验：`check_docs.py` OK（20 ADR / 28 design / 26 self-review）；
  `gen_customizations.py --strict` 98 登记行 / 135 marker、0 warning，与 design §7 基线一致。

### Findings 与责任人处置

| # | 级别 | 位置与发现 | 处置 |
|---|---|---|---|
| 1 | suggestion | `aixcoding/README.md:11-17` 结构图未列 `tools/`；属原版既有缺口，本批插入 `release/` 后差距更显眼 | **不修（责任人 2026-11-05 裁决）**：与本批 release 落点无直接行为关系，按 R5 不扩大规范 diff |
| 2 | nit | ADR-0011:44 保留“根 aixcoding/ 纯开发资产”的历史定性 | **不修（责任人 2026-11-05 裁决）**：历史 ADR 不回改；ADR-0020 已明确新定性并建立修订关系 |

本轮两项均零文件修改，无修复增量需要复审。

## 2026-11-06 第 2 轮：完整工作区独立评审

### 范围与结论

- 评审覆盖全部 PR 候选内容：4 个已跟踪规范文档、release 三物料、契约测试、批次文档、
  ADR-0020 和开发/技术支持手册。
- 结论：**通过，blocker 0 / suggestion 0 / nit 3**。
- R2 零上游、R11 零依赖、R12 采纳/否决项、R13 两处平台 skip 批准、引用实存、静默吞错、
  边界条件、同步进程等待与临时资源清理均通过。
- vacuous test 抽查通过：①破坏自有 `chrys` 判定会使冲突分支用例变红，且外部文件有逐字节
  断言；②交换 `AIXCODING_*`/`ICODE_*` 优先级会使 `new-over-legacy` 请求 404。fake binary
  仅模拟已登记的上游包内安装契约，不是被测逻辑自证。
- 用户 INSTALL、支持手册、publishing 与脚本的改名映射、优先级、SHA、冲突、幂等、rc 清理、
  迁移和回滚口径逐项一致；verification 的五项未实测缺口保持明示。

### Findings 与责任人处置

| # | 级别 | 位置与发现 | 处置 |
|---|---|---|---|
| 1 | nit | INDEX 表头“目录”下含文件条目 | **不修（责任人 2026-11-06 裁决）**：登记是 design 明确要求，表头措辞不影响导航正确性，不扩大规范 diff |
| 2 | nit | sh 的 `${CURL_EXTRA:-}` 无引号展开，极端 glob 值可能展开 | **不修（责任人 2026-11-06 裁决）**：多参数展开是故意行为，与 0.28/上游同构，无实际故障证据 |
| 3 | nit | ps1 非法 channel 由 ValidateSet 报英文参数错误，与 sh 中文 die 不同 | **不修（责任人 2026-11-06 裁决）**：下载前 fail-fast 正确，语言差异属于 D2 允许的不对称 |

三项均零文件修改，无修复增量需要复审。

### 独立复验

- `check_docs.py` OK（20 ADR / 28 design / 27 self-review）。
- `gen_customizations.py --strict`：98 登记行 / 135 marker / 0 warning。
- 定向契约测试：**44 passed / 46 skipped，exit 0**，与 verification.md 一致。

## 结论：通过（责任人已签认 2026-11-06）

两轮评审均已完成并处置全部 findings。本结论覆盖评审对象，不将 Linux/macOS、真实制品、
Windows on Arm、真实外部命令升级及发布回滚的未实测缺口改写为已通过。
