# 任务清单：追上游 v0.35.1（upstream-merge-v0.35.1）

> 活文档（AI 维护，不签认）。续接入口：当前状态、下一步、挂起事项。AI 每次接手先读本文（DEVELOPMENT.md §4 第 1 步）。

## 状态

已提交待推送（2026-11-09）：merge commit **edec1539** 已落（父 a7efff2e + 7c423b06，
工作树干净）；4 冲突 + 2 处 R2 增补品牌适配（C143/C144）全部完成并验证；
strict 101 行/138 marker/6 退役 0/0；check_docs / R2（82/90 终态）/ R3（82/79/3）/
ruff / format / ty 三平台全绿；i18n 三轮幂等；fork-full 复跑 upstream 35820 passed
（剩余 32 = 31 环境性 WinError 448 + 1 mo 中间态已收敛）；headless 冒烟 --version=0.35.1；
收尾独立评审（CodeBuddy/BM:GLM-5.3）**blocker 0**，nit×5 已处置（F1/F2/F3 修订、F4 留痕、
F5 记录）。
**下一步 = design 措辞修订重签 + self-review 签认 → 补充 add → push（逐条授权）→
责任人建 PR → 双 CI 绿后合并。**

## 待办

- [x] 开工判定呈报（ADR-0012，目标 ref = release tag v0.35.1 获裁，优先级本次为先）
- [x] requirements.md / design.md（干跑 merge-tree 预判 4 冲突；对照前三批次补强 5 处）
- [x] 外部文档评审（CodeBuddy/BM:GLM-5.3，blocker 0；F1–F5 全处置）→ design 签认
- [x] 升级前绿基线（定制 562 passed + 2 guard symlink 环境性在案 + check_docs OK）
- [x] 镜像同步（责任人 GitHub 网页同步 → 本地 main_iCode ff 05679bec→30df994a）
- [x] 建分支 `chore/merge_upstream_v0.35.1`（自 a7efff2e）
- [x] `git merge v0.35.1` → 解冲突 4 处（AGENTS.md 三段保留 / po obsolete 两侧保留 /
      mo 接受删除 / oracle 去计数化接受）+ 豁免表 mo 行移除（_common.py）
- [x] 环境适配：uv 0.12.21→0.13.7 + Python 3.14.10（uv managed）+ uv sync --frozen
      （旧 .venv 改名绕过，本地 exclude 排除）
- [x] i18n 管线三轮 + pot/po hash 幂等 + C028 退役登记迁移（tui design 联动重签）
- [x] R2（base 7c423b06，81 文件全落 approved_pool）+ ruff/format（3046）+ ty 三平台
- [x] fork-full 首轮（upstream 34 failed）→ 归因（31 环境 448 + 1 mo 中间态 + 2 品牌断言）
      → OS 级 448 对照复现坐实 → R2 增补报批（C143/C144，已批准）→ 复跑 35820 passed
- [x] design 增补（R2 清单 / C143/C144 登记预告）→ 重签 → strict 101/138/6 绿
- [x] UPGRADE-RUNBOOK 头部基线更新（0.34.0→0.35.1）+ headless 冒烟（--version=0.35.1）
- [x] verification.md / self-review.md 落盘
- [x] git add + `git rm --cached` mo（逐条授权）→ R3 OK（82/79/3）+ oracle 3 passed + strict 复跑绿
- [x] commit ×2（edec1539 merge + c022feef 评审修订；逐条授权）
- [x] 复查补强：architecture 单跑 1248 passed/3 skipped + uv build/check_wheel 通过
- [ ] push（逐条授权，责任人指示暂缓）
- [ ] PR 双 CI（aixcoding-tests + 上游 ci.yml 桥接）绿后责任人合并
- [x] TUI 冒烟（2026-11-09：人工四项无问题；discard 入口因模型不可用改自动化验证，
      上游 8 个 rollback/discard TUI 测试文件 153 passed）

## 挂起与备忘

- **裁决在案**（verification.md 第六节）：merge 目标 = release tag；C028 退役批准；
  R2 增补 C143/C144 批准；C004/C140 淘汰候选 = **保留**（2026-11-09 裁决：
  startup ≠ per-turn / 扩展接口 ≠ 规则包分发安装通道）。
- **本机环境性在案**：WinError 448 junction 策略族 ×31（OS 级对照复现，2026-11-09 新发，
  同日击中 uv minor 链接；非代码回归，实跑归 PR CI）；guard symlink ×2 升级前既有。
- **下次升级复核点**：C136–C138/C134/C135 skip 文件固定关注（单跑口径）；本批新增
  C143/C144 品牌断言文件（test_extension_tool_hooks.py / test_startup.py）列入固定关注；
  i18n mo/oracle 计数两类历史高频冲突源自本版起消失，后续冲突面收窄。
- **离线打包待办**：`dist\downloads\` PBS 缓存需更新至 20261026（配套 3.14.10），
  下次打包前执行；系统级 Python 3.14.8（C:\Program Files\Python314）可选升 3.14.10
  （uv managed 已满足本仓，非必须）。
- **旧 `.venv_old_pre_0351/` 残留**：safe-delete 回收站删除失败（trash-failed），已入
  `.git/info/exclude` 本地排除（不入 PR）；磁盘清理留责任人手动。
