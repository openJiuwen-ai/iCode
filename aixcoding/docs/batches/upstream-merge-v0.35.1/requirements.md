# 需求：追上游 v0.35.1（upstream-merge-v0.35.1）

> 活文档：随时更新、不签认（git 留痕）；意图确认由 design 签认承载。

## 背景与问题

上游 iCode 已发布 v0.35.1（release tag v0.35.1 = 7c423b06，2026-11-09 发布；
上游无 v0.35.0 tag，本区间一并覆盖 0.35.0 内容）。本 fork 主线 `main_aixcoding_cli`
基线为 0.34.0（UPGRADE-RUNBOOK.md 头部「当前基线」节，2026-11-06 完成追平）。
DEVELOPMENT.md §8 升级节奏条款：每次上游发布后评估跟进，长期不升级是需显式决策的风险状态。
区间 `v0.34.0..v0.35.1` 共 56 提交（32 非合并）、409 文件、+26260/−4480
（merge-tree 干跑实测冲突 4 处，见 design）。

责任人已裁决（2026-11-09 开工判定）：merge 目标 ref = **release tag v0.35.1**；
本批次优先级高于 ROADMAP 其他候选批次（版本更新检测 / skills 自动加载等）。

## 目标与非目标

- 目标：`main_aixcoding_cli` 基线追至 **v0.35.1**，全部在册定制点存活且全绿。
- 非目标：
  - 不借升级顺手做定制功能开发（ROADMAP 阶段三余量批次照常独立推进）；
  - 旧分支 `main_aixcoding_cli_0.28.0_final` 按冻结条款不随升级（runbook §8）。

## 上游区间重点变更（v0.34.0..v0.35.1）

1. **i18n 构建机制重构（b787c974，本批次最大机制变化）**：zh-Hans MO 改为构建时编译、
   不再 git 跟踪（新增 `hatch_build.py` hook + `scripts/i18n_mo.py` 编译器 +
   `[tool.uv].cache-keys`；build-system 增 `babel==2.18.0`）；oracle **去计数化**
   （删 `len(EXPECTED_MESSAGE_IDS) == N` 计数行，id 集合保留并拆分区域文件）。
   直接影响：mo modify/delete 冲突、test_catalog_oracle.py 计数行冲突（C028 载体消失）、
   豁免表 mo 项失效。
2. **AGENTS.md 大改**（3e4fb7d6 trim 30% + b787c974，区间 112+/125−）——C001/C130/C131
   三段 marker 保留、正文合入，已知冲突高发区。
3. **TUI 交互大改**：运行中 turn 转向（50cefb97）、discard turns（4991086c/0fc00e62）、
   会话变更预览与拒绝编辑追踪修复（fa47a363）、thinking 行 token 进度（e6c5c708）、
   @ 文件索引常驻+并行扫描（cc32e5a9）、refused drafts 重提交（935c2938）。
   diff/screen.py 区间 244 行变化（C006 暴露面）、main/screen.py +101 行（C013–C017）。
4. **Python 扩展能力扩展（ff22b004）**：per-turn context + tool hooks（检查/拒绝工具调用、
   改参、变换结果），主 agent/子 agent/workflow 全支持——淘汰检查重点：
   C004（〔消费者退役〕判据含"上游官方插件/hook 机制"）、C140（〔上游机制替代〕）。
5. **ACP 错误分类**（8a6dd71e）：余额不足/认证失败等具体原因 + 重试指引，子 agent 失败
   不再悬挂。
6. **Buddy 模型配置 breaking**：`CHRYS_PET_MODEL` 停读，改用 `CHRYS_MODEL_PROFILE_BUDDY`
   （旧配置自动迁移）；实际改动在 `src/chrys/foundation/config/settings.py`
   （10+/10−：`buddy_model`→`buddy_model_profile`、env 改名、`migrate_buddy_model_profile`
   迁移函数，2026-11-09 评审复核），与 environment-config 批次模型目录定制
   （service/aixcoding/models/catalog）及 C034 落点 app/tui/app.py 无交叠，列核对项。
7. **环境**：`.python-version` 3.14.8→**3.14.10**（7aa63a37）；uv 0.13.7；
   `scripts/build.ps1` 2 行变化（离线打包缓存版本需核对）；fetch_rg 脚本零改动。
8. 其他：doc_converter 删除线/多行单元格修复、`/usage` 按 model profile 分组、
   通用 TUI 本地化修复（interrupt banner/错误卡片等；`logs/screen.py` 区间零改动，
   C007 低风险）。

## 用户场景

1. 内部用户获得上游 0.35.1 的修复与新特性（turn 转向/discard、会话变更预览、
   扩展 tool hooks、ACP 错误分类、文档转换修复等 56 提交内容）。
2. 开发者后续定制开发基于新基线，降低三层冲突累积债务；i18n 新机制落地后
   mo/oracle 计数这两类历史高频冲突源消失，后续升级冲突面收窄。

## 验收标准

- [ ] merge 后 `gen_customizations.py --strict` 0 error / 0 warning，在册定制点无丢失
      （C028 等载体消失点的处置以责任人裁决为准，登记行同步）。
- [ ] runbook §4 验证矩阵全绿：fork-full（`verify.py --full`，CodeBuddy 环境配方）、定制四门、
      R2/R3 以新 merge-base 通过、ty 三平台。
- [ ] i18n 新管线全通：extract → update → compile → check；oracle id 集合含我方 4 借壳 key；
      mo 由 build hook 生成且 git 状态干净（不再跟踪）。
- [ ] UPGRADE-RUNBOOK.md 头部「当前基线」节更新：上游版本 → 0.35.1、升级完成日期。
- [ ] 行为锚定测试 `aixcoding/tests/diagnostics/test_panel_anchor.py` 绿（anchor 核对机检）。
- [ ] PR 双 CI（aixcoding-tests.yml + 上游 ci.yml 桥接）绿。
- [ ] TUI 冒烟：`uv run aixcoding-cli` 启动正常，F6 可见 aixcoding tab，登录徽标/命令可用。
- [ ] 升级决策清单（冲突点全清单+处理意见、定制化改变清单）已产出，全部决策经责任人裁决
      并留痕（verification.md 追加；淘汰类决策未经裁决不删除任何定制；淘汰检查以
      `gen_customizations.py --report` 全景登记为输入，逐组记录命中/未命中及依据；
      **重点：C004/C140 与上游 v0.35.1 扩展 tool hooks 机制的对比分析，须提请裁决**）。

## 开放问题

- 无（merge 目标 ref 已经责任人裁决 = release tag v0.35.1 = 7c423b06，本地 tag 已取到）。

## 升级前基线状态（2026-11-09 实测）

- 工作树干净，`main_aixcoding_cli` 与 origin 同步（a7efff2e）。
- 定制测试：562 passed / 51 skipped / **2 failed**——
  `aixcoding/tests/guard/test_guard_core.py::test_workspace_symlink_cannot_escape_{shell,filesystem}_write_scope`
  （`assert "T-SHELL-05" in []`，本地 Windows symlink 环境性失败，guard 批次 PR #98 双 CI 绿在案；
  与本次 merge 无关，merge 后同口径复跑对比，不引入回归即达标）。
- `check_docs.py` OK；定制点全景 100 登记行 / 142 编号（含退役 5），借壳 key 现存 4 个。
