# 自评审记录：定制版用户手册入库与安装版捆绑（user-manual）

<!--
签认规则（R10 · 记录档）：责任人本人编辑本头部（状态→已确认、填日期，确认意见可选），AI 不得代填。
签认一次后，后续追加评审记录不触发重签。
双 AI 规则：评审 AI 必须与主力开发 AI 不同（不同工具或不同模型），如实记录标识。
完整操作见 SELF-REVIEW-HOWTO.md。
-->

- 责任人：赵阔
- 状态：已确认
- 确认日期：2026-11-10
- 确认意见：同意第一轮评审拍板结论（1A + 2 必做 + 3 方案一 + 4/5/6 做）；剩余收尾项按 plan.md 跟踪，增量复审记录后续追加。
- 开发 AI：CodeBuddy + D4 PRO
- 评审 AI：Claude Code + GLM-5.3

> 状态说明：本批次为事后补录；双 AI 评审已完成（第一轮 + 增量复审，记录见下），
> 评审时按「评审提示」节聚焦本批次特有风险点。

## 评审提示（开发 AI 预置——供评审 AI 聚焦，非评审结论）

1. R2 比对：diff 中上游文件应**仅有** `pyproject.toml`（force-include 节）、
   `scripts/check_wheel.py`（check_archive 比对源 2 行）与
   `tests/foundation/test_documentation.py`（契约测试断言键跟改，C147——评审 F1 增补）——
   出现任何其他上游文件改动即 blocker；
2. R3 现状：两处上游改动**尚未**带正式 `AIxCoding` 单行标记（收尾待办，plan.md）——
   评审时按"已登记待补录"口径检查 plan/design 表述一致性，标记缺失本身不判 blocker；
3. 幻觉 API：设计/文档中引用的上游符号（`resolve_docs_root` / `BUNDLED_DOCS_PATH` /
   `BUNDLED_DOCS_PREFIX` / force-include 键名）逐一对照 `src/chrys/foundation/documentation.py`
   与 `scripts/check_wheel.py` 实际定义；
4. 假设钉死：design"对上游隐式行为的假设"4 条是否与上游代码现状逐字相符（含行号漂移）；
5. 覆盖矩阵：requirements 验收 8 条逐条在 test-plan 有行、在 verification 有证据或标注待补；
6. 数据边界：`aixcoding/manual/` 内容是否残留第三方外链 / 敏感信息（用 check_links.py 复扫）；
7. vacuous test：本批次无新增测试——检查"构建比对"是否真的会在捆绑缺失时红
   （故意删一个 manual 文件重建，check_wheel 应报 missing）。

## 2026-11-10 评审结论：第一轮已收敛（增量复审记录后续追加）

- 轮次：第 1 轮（评审 AI 只读核查：git diff 逐文件比对、上游符号实证、边界扫描、关键测试复现、
  F8 解析独立复现；未修改仓库文件）。报告：桌面 `user-manual批次独立评审报告-20261110.md`
  （3 blocker / 2 suggestion / 1 nit）。
- blocker 处置（全部修复，证据见 verification.md「独立评审第一轮处置」节）：
  - **F1** 契约测试连带跟改漏报（`test_wheel_bundles_docs_where_the_resolver_looks` 断言
    `force_include["docs"]` 必红）→ 已跟改断言键并补 R2 表第 3 行 + C147 标记；
    单验 1 passed。
  - **F2** "92 passed" 证据对应 pyproject 切换前树况（R7 失真）→ 已在提交态重跑，92 passed
    （含契约测试）；旧记录加注树况差异。
  - **F3** manual 树 100 处围栏外第三方外链（验收勾选失真）→ 已去链 98 处（保留产品仓库
    发布页 2 处），复扫 manual 树剩 2、wheel 内 0；requirements 验收第 2 项证据已更新。
- suggestion / nit 处置：F4 批次文档与 INDEX 已成笔入库、plan 措辞修正；F5 等价校验脚本
  曾落盘 `verify_wheel_bundle.py`（重跑 67=67 哈希零差异），责任人 2026-11-10 裁决删除、
  输出证据留 verification；F6 随 F3 联动更新。
- 开发 AI 复检追加发现（评审未覆盖）：`ruff format --check aixcoding/`（CI govern 门）会重排
  manual 树 md 围栏内有意对齐的注释 → 已加 `[tool.ruff] extend-exclude = ["aixcoding/manual"]`
  （C148），ruff 双门本地全绿。
- 未修项（次要问题，责任人拍板不修）：无（suggestion/nit 全部处置）。

## 2026-11-10 增量复审（第一轮修复后）：已收敛

- 轮次：第 2 轮（同一评审 AI 复审增量，只读 + 关键主张独立复现；对象 `4239ee95`，
  连 `2faa4c71` 一并核验）。
- 原 6 项处置核验：F1-F6 **6/6 修复**，逐项独立复现通过（92 passed 复跑一致、manual 树
  复扫 2 处保留项、脚本实在、证据对口）。
- 新增 findings 处置：
  - **N1（suggestion，C148 "必红"证据口径）→ 已修**：复审实证 `ruff format --check
    --config "extend-exclude=[]" aixcoding/` 为 316 files already formatted（md 未被目录
    遍历覆盖），"CI 必红"不成立。开发 AI 复现确认：**显式路径** `aixcoding/manual` 下
    6 个 md 确报 unformatted（机制前提为真）。处置：design R2 行与 verification 节改为
    防御性策略声明（防未来行为变化/显式路径调用），保留可复现命令与输出，原表述按
    "更正不删旧记录"原则加注。
  - **N2（nit，C145-C148 口径三处不一致）→ 已修**：requirements 验收第 8 条、
    verification 待补项、plan 待办统一为 C145-C148。
  - **N3（nit，双轨同源缺实证）→ 已登记**：plan.md「挂起与备忘」新增下批次待办
    （从 manual 树重出桌面版并做字符级对账），本批次不追溯补齐。
- 结论：blocker 清零，suggestion/nit 全部处置或登记，收敛。
