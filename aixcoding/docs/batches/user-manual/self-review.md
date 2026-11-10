# 自评审记录：定制版用户手册入库与安装版捆绑（user-manual）

<!--
签认规则（R10 · 记录档）：责任人本人编辑本头部（状态→已确认、填日期，确认意见可选），AI 不得代填。
签认一次后，后续追加评审记录不触发重签。
双 AI 规则：评审 AI 必须与主力开发 AI 不同（不同工具或不同模型），如实记录标识。
完整操作见 SELF-REVIEW-HOWTO.md。
-->

- 责任人：
- 状态：草案
- 确认日期：
- 确认意见：（可选）
- 开发 AI：CodeBuddy（Claude）
- 评审 AI：待定（须与开发 AI 不同源；候选：另开 IDE 会话的不同模型，或外部 AI 工具）

> 状态说明：本批次为事后补录，双 AI 评审**尚未执行**——按 SELF-REVIEW-HOWTO §一 五步
> 流程走完后由开发 AI 回填下方评审结论节，再交责任人签认。评审时请评审 AI 重点覆盖
> 「评审提示」节列出的本批次特有风险点。

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

## <日期待填> 评审结论：待评审

- 轮次：待执行（每轮 = 评审 + 修复 + 复审）
- blocker 处置：待回填（逐条：finding → 已修复，验证命令与结果，引用 verification.md 对应节）
- 未修项（次要问题，责任人拍板不修）：
  - 待回填（位置 | 说明 | 不修理由）
