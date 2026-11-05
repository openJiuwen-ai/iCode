# 自评审记录：追上游 v0.33.0（upstream-merge-v0.33.0）

- 责任人：周彬
- 状态：已确认
- 确认日期：2026-11-04
- 确认意见：无特别意见
- 开发 AI：CodeBuddy（Kimi-K3.1-Pro）
- 评审 AI：IDE 内独立会话与外部分析，前三轮文档评审型，收尾独立评审：CodeBuddy（GLM-5.3，与开发 AI 不同源）

> 记录档：签认一次，后续追加评审记录不触发重签。评审历程与裁决记录详见 verification.md 第六节。

## 一、评审历程（三轮外部评审 + 一轮外部讨论）

| 轮次 | 发现 | 处置 |
|---|---|---|
| 初审（独立 AI，只读） | 2 Blocker（`{{!command}}` 信任路径 / 29 点旧口径）+ 2 Major（oracle 计算 / `/man` 漏列）+ 1 Minor（镜像口径） | 全部采纳，关键事实逐项独立复核属实后修正 |
| 复审 | 1 流程 Blocker（ADR 判定未覆盖选 A）+ 1 Major（矩阵漏 C003）+ 2 Minor（引用/路径） | 全部采纳；ADR 责任人终裁不出并补足理由（ADR-0018 口径沿用） |
| 终审 | **通过**，无遗留 | — |
| 实施期外部讨论（models_dialog_deletion 处置） | 同意 skip 方向，微调为**函数级**（失效模式更安全）+ reason 带前缀 + 纯 ASCII + 登记义务 | 全部采纳（C134），D 留独立批次待办 |

## 二、实施期自审发现（自身问题清单）

1. **design 初稿定制点口径过期**：沿用 v0.32.2 批次的 C001–C029，未先跑 `--report` 取实时全景
   （评审 Blocker 2 纠正为 95 行/C132）——教训：升级设计的第一输入应是 `--report` 快照，不是上批文档。
2. **oracle 初算口径错误**：用 POT 原始 msgid 行数（2739，含复数/头部）当 oracle 基数——正确口径是
   读上游 `test_catalog_oracle.py` 的断言值（2623）与我方借壳 key 注释（+5，非 +4）。评审 Major 1 纠正。
3. **design 写了不存在的 `translate` 子命令**：i18n 管线实为 extract/update/compile/check（msgstr
   fallback 由 update 内置）。实施时修正，设计表述以脚本实际子命令为准。
4. **deselect 假设不成立**：`chrys_test.py` 与 `verify.py` 均不透传 pytest 参数，配方中的
   `--deselect tests/app/test_install_scripts.py` 无法使用——改用 `verify.py --full` 标准入口，
   已知 flaky 若出现单独重跑确认（test-plan 已记录）。
5. **merge-tree 干跑的能力边界**：只能预判文本冲突，**上游新增测试与我方定制的语义冲突**
   （keyless 品牌 / F4 禁用）只能由全量验证暴露——本批次 2 处均靠 fork-full 发现，按 R2 增补
   报批处置。后续升级设计应把"上游新增测试 × 在册定制语义"列为全量验证的固定关注点。
6. **工具误报识别**：cmd `findstr` 对 UTF-8 中文文件匹配不可靠（C003 行一度误判缺失），
   以 ripgrep（search_content）交叉确认为准。
7. **流程缺件**：test-plan/verification/self-review 未与 requirements/design 同步创建（责任人指出），
   已补齐——正式管线批次应一次性建齐文档骨架再逐步填充。

## 三、遗留风险与 nit

- ~~fork-full 复跑与 architecture 补跑~~ **已完成全绿**（2026-11-04 复跑：custom 454 + tests 34446 +
  architecture 1247，PASSED exit 0，见 verification.md §一；评审 Major-1 修正：原稿遗留清单与
  验证记录矛盾）；
- PR 双 CI 待 push 后跑（aixcoding-tests + 上游 ci.yml 桥接）；
- TUI 冒烟（F6 aixcoding tab / 登录徽标）留人工补验（v0.32.2 先例）；
- `test_install_scripts.py` 满载 flaky、`test_gc_freeze_calibration` 偶发 flaky 为在案环境噪音；
- `check_r2_diff` 对 branding/tier-migration 的存量 "同上/文件" 行 warn（ignored）为既有格式噪音，
  不属本批次处置范围；
- C134 的 skip 在上游追加新用例时会显式暴露（设计意图），下次升级须复核该文件。

## 四、结论

三轮外部评审收敛 + 实施期自审问题全部处置；6 处 merge 冲突与 2 处全量暴露冲突均按设计/增补
报批解决并验证；95+2 登记行全存活、零淘汰候选。可进入收尾（复跑 + 冒烟 + 提交 + PR）。

## 五、2026-11-04 收尾独立评审（CodeBuddy，GLM-5.3，与开发 AI 不同源）

**评审依据**：SELF-REVIEW-HOWTO §二 模板；对象 = merge commit 33b55cf9 + 工作区适配 + 六件文档。

**结论：blocker 0**；工程本体质量高（R2/R11 零越权、冲突解决与 design 逐点吻合、机检门禁复跑
逐位一致、决策交付物齐备）。findings 3 项处置：

- **Major-1**（self-review 遗留清单与 verification 矛盾：复跑已完成却写未完成）→ **已修**：
  §三改为已完成全绿并引用 verification §一，遗留清单保留 PR 双 CI / F6 人工冒烟（见上文修正）。
- **Minor-1**（评审 AI 型号未注明）→ **已修**：头部补全（前三轮未注明 + 收尾 GLM-5.3）。
- **Nit-1**（plan.md 收尾清单含 pot，疑似行尾假脏）→ **实证闭环**：`git diff --numstat` 与
  `--ignore-cr-at-eol` 对 `locales/chrys.pot` 均零输出（autocrlf stat 假脏，无实际内容差异），
  收尾 add 清单排除 pot。

## 六、2026-11-04 C135 最终实施复审（外部 AI 独立会话，型号未注明）

对象 = `test_prompt_shortcut_send.py` 最终 diff + design R2/C135 登记 + verification/test-plan/plan +
仓库外正向覆盖待办。**结论：Blocker 0、Major 0、Minor 1、Nit 1**；确认 models 参数级 skip 不误跳
agents，marker/R2/C135 路径逐字一致，产品语义互斥成立，Windows 收集与 Unix 运行验证边界及 CI 证据
归属均无违规。findings 处置：

- **Minor M1**（verification 遗留项仍写首次“收尾提交”及旧 add 清单，与 PR 修复迭代不符）→ **已修**：
  改为“C135 修复提交（1 测试适配 + 批次文档 + self-review 追加）”，INDEX/RUNBOOK 不重复列入。
- **Nit N1**（Windows fork-full 数字未标注为 C135 前时点）→ **已采纳**：verification 冲突 #9 复核栏
  补明 win32 模块级 skip 使全量数字不受 C135 影响，无需复跑；不把收集检查当作 Unix 运行验证。
- **流程前置项**（self-review 尚未追加 C135 复审）→ **本节补齐**；按文件头部“后续追加评审记录
  不触发重签”规则处理。

复审引用门禁：R2 80/83、R3 76+4、strict 98/135、ruff/format、2 tests collected、check_docs 全绿；
Unix 实际运行结果仍归 PR checks，不在本文预记结论。
