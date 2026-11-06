# 任务清单：追上游 v0.34.0（upstream-merge-v0.34.0）

> 活文档（AI 维护，不签认）。续接入口：当前状态、下一步、挂起事项。AI 每次接手先读本文（DEVELOPMENT.md §4 第 1 步）。

## 状态

本地收尾（2026-11-06）：merge 已解冲突（未提交中间态，符合逐条授权纪律）；
5 冲突 + 4 处 R2 增补适配（C136–C139）全部完成并验证；fork-full 复跑全绿
（tests 34869 + architecture 1247 + custom 513，PASSED exit 0）；strict 102/139；
design 已重签；收尾独立评审（GLM-5.3）blocker 0、F1–F3 已处置；plan.md 已补齐；
headless 冒烟 `--version` = 0.34.0。
**下一步 = git add → commit → push（逐条授权）→ 责任人建 PR → 双 CI 绿后合并。**

## 待办

- [x] 开工判定呈报（ADR-0012，选项 A 完整 runbook 批次获裁）
- [x] requirements.md / design.md（干跑 merge-tree 预判 5 冲突；评审 B1/B2/C1/C2 处置后签认）
- [x] 升级前绿基线（定制 511 passed + check_docs）
- [x] 建分支 `chore/merge_upstream_v0.34.0`（自 28b7d1c7）
- [x] `git merge 05679bec` → 解冲突 5 处（screen.py import 融合 / mo 重编 / **registry 融合**
      （锁域扩展+双整替同锁）/ oracle 2705 / C102 断言融合）
- [x] i18n 管线（extract/update/compile/check）+ oracle 实测 2705 + 二轮幂等零 diff
- [x] B1/B2 时序断言（窗口回退+下轮 sync 自愈 / 快照后 reload 收敛）+ 写盘目录一致性核对
- [x] R2/R3 新 merge-base（05679bec）+ 定制全套 513 + ruff/format + ty 三平台
- [x] fork-full 首轮（upstream 4 failed）→ 归因（3×F4 互斥 + 1×品牌断言，C133–C135 同型）
      → R2 增补报批（C136–C139，已批准）→ 复跑全绿（34869+1247+513，PASSED exit 0）
- [x] design 增补（R2 清单 / C136–C139 登记预告）→ 重签 → strict 102/139 绿
- [x] verification.md / self-review.md / plan.md（含收尾评审 F1–F3 处置与记录补齐）
- [x] UPGRADE-RUNBOOK 头部基线更新（0.33.0→0.34.0）+ headless 冒烟（--version=0.34.0）
- [ ] git add / commit / push（逐条授权）
- [ ] PR 双 CI（aixcoding-tests + 上游 ci.yml 桥接）绿后责任人合并
- [x] TUI 人工冒烟（mock :7777/:7778 + TUI 双可见窗口；F6/徽标/命令/F4 禁用/reload 收敛，
      责任人 2026-11-06 确认无问题）

## 挂起与备忘

- **裁决在案**（verification.md 第六节）：B1 窗口=接受+下轮 sync 自愈（备与否决留痕）；
  C030/C031 淘汰候选=保留（上游 reload 不满足"跨线程安全"条件，〔上游等价〕不命中）；
  R2 增补 C136–C139 已批准（skip reason 统一 C134 前缀）。
- **C030/C031 保留≠结束**：融合并存已全绿；若未来上游提供跨线程安全整替官方通道，
  按 removal_condition 重评。
- **reload_base 无锁读（I2）/B1 测试空 view（I3）**：记录留痕不处置（verification 第六节）。
- **下次升级复核点**：C136–C138 skip 的上游用例追加会显式暴露（设计意图）；
  `test_manual_reload.py` / `test_reload_profiles.py` / `test_models_dialog_deletion.py`
  为固定关注文件。
