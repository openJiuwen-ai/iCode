# 任务清单：日志治理①——现存埋点路由修复

> 活文档（AI 维护，不签认）。续接入口：当前状态、下一步、挂起事项。AI 每次接手先读本文（DEVELOPMENT.md §4 第 1 步）。

## 状态

双 AI 自审 findings 全部修复（2026-11-05，评审 AI：CodeBuddy / Kimi-K3.1-Pro，1 blocker + 3 suggestion 全修，回归 58/466 全绿，见 self-review.md）。**下一步：增量复审 → self-review.md 责任人签认 → PR**（git 写操作逐条授权）。

## 待办

- [x] requirements.md（活文档；2026-11-05 两次修订：B 撤销、M1/M2/M5 与复审 Major 验收项同步）
- [x] design.md 起草 + 两轮 peer review 闭环（首轮 3 Major/2 Minor、复审 1 Major，全部处置）+ **签认（2026-11-05）**
- [x] overview.md（体系全景与路由对照辅助说明，2026-11-05 责任人指示落位本目录）
- [x] test-plan.md（覆盖矩阵对照 requirements 九条验收标准）
- [x] plan.md（本文）
- [x] 切特性分支（`fix/logging_routing_fixes`，2026-11-05 授权执行）
- [x] 实施 A1：`auth/__init__.py`（detail 归位 + 中文文案 + exc_info + 文件级 noqa）
- [x] 实施 A2：`crypto.py`（三打点：异常 detail / 往返不一致 detail / 降级公共 WARNING + detail 双写；文件级 noqa）
- [x] 实施 A3：`login.py`（删 `__name__` logger；回调失败改 detail 中文 + exc_info）
- [x] 实施 C：`login_actions.py`（删未使用 import 与 logger，-2 行）
- [x] 实施 D：`catalog.py`（新增 `_log`；`_warn_retired_env` 双打；:72 定调注释修订）
- [x] 测试：新增 `test_routing_fixes.py`（7 用例）；修订 `test_catalog_sync.py`（:699 反转 + 公共卷哨兵）
- [x] 机器验证：四连全绿 + ruff check/format 全过（verify.py --smart 选项 A 拆分覆盖，见 verification.md）
- [x] verification.md（机器验证证据 + 实施期注记五条 + 可选目验项）
- [x] 双 AI 自审评审 + 修复（评审 AI：CodeBuddy / Kimi-K3.1-Pro，2026-11-05；1 blocker + 3 suggestion 全修，回归 58/466 全绿）
- [x] 增量复审（通过，无新增 finding，blocker 清零，2026-11-05）
- [ ] self-review.md 责任人签认
- [ ] PR：commit（message 规范预审）→ push → 责任人网页建 PR（git 写操作逐条授权）

## 挂起与备忘

- **B 项撤销留档**（防重提）：catalog.py:680/681 非重复违规——"full endpoint stays on detail"是 catalog-auth-gating 签认行为（test_catalog_sync.py:973-1020 钉死）；676 注释约束对象是面向用户的错误文案。详见 design"调查结论修正"节。
- 签认时裁决项落实：R13 豁免（design R13 节，签认即批准）——`auth/__init__.py`/`crypto.py` 文件级 noqa，批准指向本批 design；A2 WARNING 文案按 design 草案生效；catalog-sync design 联动注记——签认意见未要求，实施不加，如后续要求再补（备忘在案）。
- D 修订 :72 注释文本 → 提交前 `gen_customizations --strict` 对账义务（ADR-0015）；若对账要求更新登记行属 R10 重签范围，届时呈报。
- A2 告警口径（peer review2 定案）：仅在候选全部耗尽的最终降级点告警；`_platform_factories` 现为每平台单候选；未来多候选时 `:255` 提前返回路径告警缝隙已留档 design 风险节。
- 验证注意（本机配方）：`set "PYTHONUTF8=1"`、清空 `PYTHONPATH`、大写盘符启动；aixcoding/tests 直跑变体 `uv run pytest aixcoding/tests -m "not integration and not gc_calibration"`。
