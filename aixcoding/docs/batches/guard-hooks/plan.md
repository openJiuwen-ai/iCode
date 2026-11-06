# 计划：安全护栏（guard hooks）迁入

> 持续维护：任务清单 / 状态 / 续接入口（AI 每次接手先读）。

## 续接入口

- design 状态：**已确认**（2026-11-05，R2 上游改动为 `orchestration/startup.py` 调用点 +
  `pyproject.toml` 最小 lint/type 配置 C141）；
- 实施与第二 AI 自审修复已完成，剩余 PR 组装与 git 写操作逐条授权。

## 任务清单

| # | 任务                                                                                                                                                                                                                                                                                                                                              | 状态 |
|---|-------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|---|
| 0 | 判定呈报（当期基线 vs 旧定制分析）+ 责任人裁决「有改动的迁」                                                                                                                                                                                                                                                                                                               | 完成（2026-11-05） |
| 1 | 批次文档起草（requirements / design / plan / INDEX 登记）                                                                                                                                                                                                                                                                                                 | 完成（2026-11-05） |
| 1.5 | 签认前独立评审（`independent-review.md`，F1–F7）与处置：F1/F2 采纳（R2 补 pyproject 行 C141、selftest 适配写实）、F3/F4/F5 修正、F2 assert 部分经实测否定（unittest 形态不撞守卫）、F6/F7 记录为预存在等价行为                                                                                                                                                                                         | 完成（2026-11-05） |
| 2 | 责任人审阅 requirements + **签认 design**（R2 批准落盘：startup.py C140 + pyproject.toml C141；警告通道按 design 默认＝纯 logger）                                                                                                                                                                                                                                      | 完成（2026-11-05，签认人：李海潮） |
| 3 | test-plan 起草（覆盖矩阵：验收标准 × 用例，7 条全覆盖无空缺）                                                                                                                                                                                                                                                                                                          | 完成（2026-11-05） |
| 4 | 规则包迁移最终形态（F1）：6 个运行时文件 + 包锚点落 `src/chrys/kernel/aixcoding/guard/pack/`；测试/工具/文档按 R4 分流到 `aixcoding/tests/guard/`、`aixcoding/tools/`、本目录 `rule-pack/`；不保留仓库根副本                                                                                                | 完成（2026-11-05） |
| 5 | `guard_install.py` → `src/chrys/kernel/aixcoding/guard/`（kernel 档）：资产路径统一为 `pack/`；flow-style YAML 保留无关 hooks/settings；PEP 758 由 C141 py39 目标保护                                                                                                                                                                                | 完成（2026-11-05） |
| 6 | 接线：`bootstrap_runtime()` 加 C140（lazy import + 全吞错 + logger.warning，+8 行 marker 在位）                                                                                                                                                                                                                                                              | 完成（2026-11-05） |
| 6.5 | **落点最终裁决（F1）**：仓库根 `guard_hooks/` 违反 R4，责任人裁决迁回 tier 内 `pack/`；可分离资产全部按 R4 分流，不保留仓库根副本、不使用 force-include | 完成（2026-11-05） |
| 6.6 | **design 状态已确认**：正文已回写最终落点；R2 为 startup.py + C141 最小 lint/type 配置                                                                                                                                                                                                         | 完成（2026-11-05） |
| 7 | 测试迁移并补齐：`aixcoding/tests/guard/`——`test_guard_install.py`（13 用例，含 flow YAML）、`test_packaging.py`（4 用例）、`test_anchors.py`（5 锚定用例）、`test_guard_core.py`（2 个 junction/symlink 用例）；规则自检 `guard_pack_selftest.py` 70 条（含 T-MCP-01～03 正例 + 必需 ID 集）；部署验证 64 例。24 pytest passed                                                                          | 完成（2026-11-05） |
| 8 | CI 挂接：`aixcoding-tests.yml` 新增 `guard` 三平台 job（安装 → 新模块 selftest → 新路径部署验证）+ gate 聚合 fail-closed                                                                                                                                                                                                                                                       | 完成（2026-11-05） |
| 9 | 验证（见 verification.md 最新节）：ruff/format/ty、pytest、selftest、架构与治理工具                                                                                                                                                                           | 完成（2026-11-05） |
| 9.5 | **R13 最小豁免（F1）**：仅保留 py39 target、RUF001/2/3 per-file ignore、两处 ty import 抑制；T201 通过代码改写消除，无 noqa/force-include/ty override                                                                                                                                                                 | 完成（2026-11-05） |
| 9.6 | 落点修正：部署验证迁 `aixcoding/tests/guard/`、手工安装器迁 `aixcoding/tools/`、文档迁 `rule-pack/`；均符合 R4                                                                                                                                                                                                                  | 完成（2026-11-05） |
| 10 | 双 AI 自审（SELF-REVIEW-HOWTO）：第一轮 F1–F6 已修复，待第二 AI 增量复审 → `self-review.md`                                                                                                                                                                                                                                                                                            | 完成（`self-review.md` 草案，待责任人签认） |
| 11 | PR 组装（§7 交付报告格式）+ 对账迁移（ROADMAP C7 → A 类）                                                                                                                                                                                                                                                                                                        | 待办（需 Owen 授权 git 写操作） |

## 挂起事项

- 无。
