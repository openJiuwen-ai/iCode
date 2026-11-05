# 任务清单：catalog 只读行为正向测试覆盖盘点与补缺（catalog-readonly-coverage）

> 活文档（AI 维护，不签认）。续接入口：当前状态、下一步、挂起事项。AI 每次接手先读本文（DEVELOPMENT.md §4 第 1 步）。

## 状态

交付阶段——self-review.md 已签认（2026-11-05）；交付前自查完成（CI govern 门禁本地全量
预跑补齐，见 verification 专节）。当前：停等 git add 授权（逐条：add → 自查 → commit →
push → 责任人网页建 PR）。

## 待办

- [x] 只读调查 + 开工判定单呈报（2026-11-05，责任人裁决：路径 A 完整管线，G1+G2[configure]，G3 不补）
- [x] requirements.md / design.md / test-plan.md 草案 + INDEX 登记
- [x] 独立评审修复轮（B1/B2/B3/S1 全处置，见 design 修订记录）
- [x] design.md 复审 + 签认（2026-11-05；Nit 已补明 helper 三元组返回；G2b 未批示纳入=不做）
- [x] git：建分支 `feat/catalog_readonly_coverage`（已授权执行，2026-11-05）
- [x] 实施：test_read_only_gate.py 提取 helper（用例 0）+ G1 四态参数化用例 + G2 用例
- [x] 验证：定向 8 passed → 全定制套件 459 passed/4 skipped → ruff 双检 → gen_customizations --strict 98/135 不变 → check_docs OK
- [x] 变异验证 V1/V2（红面符合预期互不误伤 → 还原复绿 → 零残留 diff，证据入 verification.md）
- [x] 双 AI 自审（评审 AI：Kimi-K3.1-Pro / CodeBuddy，3 轮全通过，S1/N1 已处置）→ self-review.md 草案落盘
- [x] 责任人签认 self-review.md（2026-11-05，确认意见：无特别意见）
- [ ] 交付：PR（逐条授权 add/commit/push；CI 证据只归 PR checks）

## 挂起与备忘

- G2b（picker MANAGE 消费侧接线锚定）：默认不做，design.md 备选留档；如纳入需在 design 签认时批示。
- C134/C135 不属本批次射程；上游若在 `test_models_dialog_deletion.py` 追加用例，函数级 skip 会显式暴露（升级时复核）。
- 本地验证配方：`set "PYTHONPATH=" && set "PYTHONUTF8=1"` + 大写盘符 `D:\`；gen_customizations 基线 98 行/135 marker 不得漂移。
- 分支基点：main_aixcoding_cli = c768e670（PR #91 合并后，用户已 pull）。
