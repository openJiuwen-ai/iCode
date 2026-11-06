# 自评审记录：追上游 v0.34.0（upstream-merge-v0.34.0）

- 责任人：周彬
- 状态：已确认
- 确认日期：2026-11-06
- 确认意见：无特别意见
- 开发 AI：CodeBuddy（Kimi-K3.1-Pro）
- 评审 AI：CodeBuddy/BM:GLM-5.3（文档两轮 + 实现一轮 + 收尾独立评审）

> 记录档：签认一次，后续追加评审记录不触发重签。评审历程与裁决记录详见 verification.md 第六节。

## 一、评审历程（文档两轮 + 实现一轮）

| 轮次 | 发现 | 处置 |
|---|---|---|
| 文档初审（责任人+外部 AI，只读事实核查） | B1（reload 三段式窗口未覆盖）+ B2（sources 收敛性未论证）+ C1（378/379 数字矛盾）+ C2（C007 编号误引） | 全部采纳：B1 融合方案第 4 点扩展（窗口机制+处置口径三案）、B2 核对项①②③、C1/C2 笔误修正；事实核查逐项独立复核 |
| 文档复审 | **通过**，四项 findings 均已妥善解决；B1 裁决采纳建议案（接受+下轮 sync 自愈）；附赠情报：`reload_base` 语义=内存拷贝剔除 disk_ids（核对项②提前定论） | 裁决与情报固化进 design 草案后签认 |
| 实现评审（外部 AI，merge 后） | I1（B1 测试 docstring 口径超前）+ I2（reload_base 无锁读）+ I3（B1 测试空 view 简化）；R2 集合差实测 373 差异文件全落在在册定制点/预期豁免，**无超范围手改** | I1 已消解（评审信息滞后，裁决与签认已完成）；I2/I3 记录留痕不改（论证成立/简化可接受） |
| R2 增补报批 | fork-full 暴露 4 处上游新测试冲突（C136–C139） | 责任人批准；skip reason 统一 C134 同款前缀（组内一致便于 grep） |

## 二、实施期自审发现（自身问题清单）

1. **rollback 测试融合时多加了 C102 marker**：初版在 573 行断言上方新增同文本 marker，
   自查发现 C102 登记计数 1 会漂移——**撤回**（原 HEAD 侧该处本无 marker，保持原状），
   strict 对账 135 零漂移佐证。教训：冲突融合保持两侧 marker 原数量，新增 marker 必须走
   登记预告。
2. **fork-full 后台任务结果获取**：后台执行无主动输出通道，首轮结果需经 pytest
   `lastfailed` 缓存+重跑定位——lastfailed 混入多轮历史（mock_server/test_zz_tmp_fail
   等旧批次噪音），**必须以重跑实测为准**，不可直接采信缓存名单。
3. **traceback 输出通道截断**：管道/长输出下 pytest FAILURES 段偶发不可见——改用
   输出落盘后读文件分析，一次拿全 4 失败完整 traceback，效率最高。
4. **design 初稿数字笔误**（378 vs 379）：干跑实测节的 auto-merge 计数与区间总数未交叉
   验算，评审 C1 纠正。教训：数字声明须与 diff 统计互验。

## 三、遗留风险与 nit

- PR 双 CI 待 push 后跑（aixcoding-tests + 上游 ci.yml 桥接）；
- TUI 冒烟（F6 aixcoding tab / 登录徽标 / live-reload 后 catalog 条目仍在）留人工补验
  （v0.33.0 先例）；
- `test_install_scripts.py` 满载 flaky、`test_gc_freeze_calibration` 偶发 flaky 为在案
  环境噪音（本批两轮 fork-full 均未触发）；
- C136–C138 的 skip 在上游追加新用例时会显式暴露（设计意图），下次升级须复核
  `test_manual_reload.py` / `test_reload_profiles.py` / `test_models_dialog_deletion.py`；
  **两文件合跑存在跨文件状态污染（PR #99 评审候选 1，上游固有：合跑失败、单跑全绿、
  上游树同法复现）——排障按单文件单跑口径，勿按合跑误判回归**；
- I2（reload_base 无锁读）/I3（B1 测试空 view 简化）记录在案不处置（verification 第六节）；
- B1 窗口已裁决接受+自愈；若未来实证频发可凭 design 否决留痕重开裁决。

## 四、结论

文档两轮评审收敛（4 findings 全处置）+ 实现一轮评审（3 findings 处置/留痕）+ R2 增补
批准闭环；5 处 merge 冲突与 4 处全量暴露冲突均按 design/增补报批解决并验证；fork-full
两轮后全绿（tests 34869 + architecture 1247 + custom 513），102 登记行/139 marker 全存活、
C030/C031 淘汰候选经裁决保留。可进入收尾（提交 + PR + 冒烟）。

## 五、2026-11-06 收尾独立评审（CodeBuddy/BM:GLM-5.3，与开发 AI 同工具异模型）

**评审依据**：SELF-REVIEW-HOWTO §二 模板逐项执行，不采信文档自述、关键机检全部独立复跑；
对象 = merge 未提交中间态工作区 + 批次五件文档。

**结论：blocker 0**；R2 比对/冲突解决 5 处/registry 融合五要点/R11/幻觉 API/vacuous test
（B1/B2 改坏实现即红实证）/并发口径/流程完整性全过；strict 102/139、check_docs、
锚定+B1/B2 6 passed、R2/R3、oracle 2705 复跑逐位一致。findings 3 项（nit×1+suggestion×2）处置：

- **F1**（verification 第一节 R3 行 80/76 为 C136–C139 适配前时点，未标注）→ **已修**：
  R2/R3 行更新为适配后复跑实测 83/79/4 并标注时点差值（=3 个适配文件）；
- **F2**（verification 第八节"fork-full 结果待补"与第一节 PASSED 矛盾）→ **已修**：
  删该半句，遗留项仅保留 TUI 冒烟 / PR 双 CI / RUNBOOK 随 PR 提交；
- **F3**（评审 AI 字段缺工具/模型标识）→ **责任人亲自处置**（头部补 CodeBuddy/BM:GLM-5.3），
  本轮记录随本节补齐（含轮次归属）。

评审状态披露（非 finding）：merge 未提交中间态符合 git 逐条授权纪律；收尾 add 清单须
三类齐全（5 已解决冲突 + 7 not staged + 批次文档 untracked），勿漏 not staged 侧。
