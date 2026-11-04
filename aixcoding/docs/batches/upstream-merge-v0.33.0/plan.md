# 任务清单：追上游 v0.33.0（upstream-merge-v0.33.0）

> 活文档（AI 维护，不签认）。续接入口：当前状态、下一步、挂起事项。AI 每次接手先读本文（DEVELOPMENT.md §4 第 1 步）。
> 2026-11-04 补记：本件于收尾阶段补全（流程缺件，责任人指出后补齐），按实际执行回写。

## 状态

交付阶段（2026-11-04）：merge commit `33b55cf9` 完成，6 处冲突全解；merge 后全量暴露的 2 处
新测试冲突已按 R2 增补适配（C133/C134）；fork-full 复跑全绿；design 增补已重签；六件文档齐。
下一步 = 收尾提交 → push → PR。

## 待办

- [x] 开工判定呈报（ADR-0012，完整管线批次；目标 ref=release tag 8d5a3208 裁决）
- [x] requirements.md / design.md（三轮外部评审收敛后签认）
- [x] 升级前绿基线（aixcoding/tests 454 passed + check_docs + --report 95 行全景快照）
- [x] 建分支 `chore/merge_upstream_v0.33.0`（自 2bde5abe；批次文档暂留工作树）
- [x] `git merge 8d5a3208` → 解冲突 6 处（po 过时区融合 / config_actions 保留 read_only=True /
  process_settings 两字段并存 / mo 重编 / test_hooks Safety+品牌融合 / oracle 2628）→ merge commit `33b55cf9`
- [x] i18n 管线（extract/update/compile/check）+ oracle 断言等价物全等（2628）+ 二次幂等
- [x] merge 后初检 73 passed（test_panel_anchor / test_hooks / man 融合 / test_model_config）
- [x] R2/R3 新 merge-base（8d5a3208）+ import 边界 + 定制重跑 454 + ty 三平台 + ruff/format
- [x] fork-full 首轮（5 failed）→ 归因实验（worktree 纯上游/变量隔离对照）→ R2 增补报批
  （C133 keyless 品牌断言 / C134 models_dialog_deletion 函数级 skip）→ 复跑全绿
  （custom 454 + tests 34446 + architecture 1247，PASSED exit 0）
- [x] design 增补三节（R2 清单 / C133-C134 登记 / 决策交付物）→ 重签 → --strict 97/134 绿
- [x] test-plan.md / verification.md / self-review.md / plan.md（流程缺件补齐）
- [x] UPGRADE-RUNBOOK 头部基线更新（0.32.2→0.33.0）+ headless 冒烟（--version=0.33.0）
- [ ] **收尾提交**：add（2 测试适配 + INDEX + RUNBOOK + 批次目录；pot 经 diff 实证为
  autocrlf 假脏无内容差异，排除）→ status 检查 → commit（五段式 message 预审）——逐条授权停等
- [ ] **push** `chore/merge_upstream_v0.33.0` → 责任人网页建 PR（目标 main_aixcoding_cli）
- [ ] PR 双 CI（aixcoding-tests + 上游 ci.yml 桥接）绿后责任人合并
- [x] mock（:7777/:7778）+ TUI 人工联调：按 test-plan 手工步骤及联调清单检查，责任人 2026-11-04 确认无问题

## 挂起与备忘

- **裁决在案**（verification.md 第六节）：目标 ref 8d5a3208；Blocker 1 选 A（内网受控+服务端
  承诺不下发+接受残余风险；不出 ADR 沿用 ADR-0018）；R2 两处适配批准；函数级 skip 微调。
- **淘汰检查**：95 行登记逐组 not-hit，零淘汰候选，本批次不删除任何定制。
- **C134 复核义务**：上游若向 `test_models_dialog_deletion.py` 追加用例将显式暴露（函数级
  skip 设计意图），下次升级必须复核该文件。
- **D 待办**：catalog 只读行为正向覆盖盘点 → `fork\temp\aixcoding-todos\2026-11-04-catalog-readonly-test-coverage.md`（独立批次，勿入工程目录）。
- **镜像**：本批次未触碰；`origin/main_iCode` 头（05679bec）领先 v0.33.0 共 24 个未发版提交，
  留下次升级前按 runbook §1.2 统一处理；归因实验 worktree 已清理。
- **环境噪音在案**：`test_install_scripts.py` 满载 flaky（verify.py 不透传 deselect，出现则单独
  重跑）；`test_gc_freeze_calibration` 偶发 flaky；`check_r2_diff` 存量 "同上/文件" warn 为既有
  格式噪音不属本批次。
- **本地配方**：大写盘符 + `set "PYTHONPATH=" && set "PYTHONUTF8=1"`；Python 3.14.8 已就位。
