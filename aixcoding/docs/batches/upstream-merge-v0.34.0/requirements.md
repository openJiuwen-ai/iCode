# 需求：追上游 v0.34.0（upstream-merge-v0.34.0）

> 活文档：随时更新、不签认（git 留痕）；意图确认由 design 签认承载。

## 背景与问题

上游 iCode 已发布 v0.34.0（release tag v0.34.0 = 05679bec，2026-11-05 前后）。
本 fork 主线 `main_aixcoding_cli` 基线为 0.33.0（UPGRADE-RUNBOOK.md 头部「当前基线」节，
2026-11-04 完成追平）。DEVELOPMENT.md §8 升级节奏条款：每次上游发布后评估跟进，
长期不升级是需显式决策的风险状态。
区间 `v0.33.0..v0.34.0` 共 24 提交、384 文件、+20993/−5745（干跑实测冲突 5 处，见 design）。

## 目标与非目标

- 目标：`main_aixcoding_cli` 基线追至 **v0.34.0**，全部在册定制点存活且全绿。
- 非目标：
  - 不借升级顺手做定制功能开发（ROADMAP 阶段三余量批次照常独立推进）；
  - 旧分支 `main_aixcoding_cli_0.28.0_final` 按冻结条款不随升级（runbook §8）。

## 上游区间重点变更（v0.33.0..v0.34.0）

1. **Breaking**：`feat(config)!: stop loading .env files at startup`（dfe8d446）——
   移除 `python-dotenv` 依赖；已查证 `aixcoding/` 定制目录零依赖 dotenv（2026-11-06）。
2. **TUI live-reload 系列**（e9e996be/21dee050/b5594e65/265b3a83 + d307557d ACP 侧）：
   settings/memory/skills/agents/models/MCP/hooks/主题/快捷键热重载——引入
   registry `revision`/`reload_base`/`install_reload` 协议（与我方 catalog-sync 的
   C030/C031 锁+快照机制同文件同域，本批次最大语义融合点）。
3. Python extensions 可为 agents 添加工具（3998c2b9）。
4. shell 工具参数命名 `workdir` / `max_output_tokens`（9e5269cd）。
5. 性能：provider/MCP SDK 移出事件循环导入（36fb0c53）；Anthropic client 不再导入
   OpenAI SDK（3b3e9bcc）。
6. TUI：重试倒计时+Retry Now（daedec95）、输入区引用标签（ca3f1040）、Vim Visual
   选择修复（f31316e5）、日志查看器 Home 钳制测试（7f8d290a）。
7. CI TUI 分片重平衡（a3cad3d9）、GC calibration gate 计入 /btw 面板（1bb73840）。
8. 版本 0.33.0→0.34.0；`pypdf` 6.21.2→6.21.4；`.python-version`、fetch_rg 脚本未动。

## 用户场景

1. 内部用户获得上游 0.34.0 的修复与新特性（live-reload、扩展工具、性能优化、
   .env 停载等 24 提交内容）。
2. 开发者后续定制开发（ROADMAP 阶段三余量批次）基于新基线，降低三层冲突累积债务。

## 验收标准

- [ ] merge 后 `gen_customizations.py --strict` 0 error / 0 warning，在册定制点无丢失。
- [ ] runbook §4 验证矩阵全绿：fork-full（`verify.py --full`，CodeBuddy 环境配方）、定制四门、
      R2/R3 以新 merge-base（05679bec）通过、ty 三平台。
- [ ] UPGRADE-RUNBOOK.md 头部「当前基线」节更新：上游版本 → 0.34.0、升级完成日期。
- [ ] 行为锚定测试 `aixcoding/tests/diagnostics/test_panel_anchor.py` 绿（anchor 核对机检）。
- [ ] PR 双 CI（aixcoding-tests.yml + 上游 ci.yml 桥接）绿。
- [ ] TUI 冒烟：`uv run aixcoding-cli` 启动正常，F6 可见 aixcoding tab，登录徽标/命令可用。
- [ ] 升级决策清单（冲突点全清单+处理意见、定制化改变清单）已产出，全部决策经责任人裁决并留痕
      （verification.md 追加；淘汰类决策未经裁决不删除任何定制；淘汰检查以
      `gen_customizations.py --report` 全景登记为输入，逐组记录命中/未命中及依据；
      **重点：C030/C031 的〔上游等价〕条件——上游 v0.34.0 reload/revision 机制是否构成
      "跨线程安全且整替语义官方通道"，须出对比分析提请裁决**）。

## 开放问题

- 目标 ref（design 备选 A）：release tag v0.34.0 = 05679bec——本次 tag、镜像头、
  release commit 三者同一对象；本地无 v0.34.0 tag ref（https 网络未取到），
  拟以裸 hash merge（v0.33.0 批次同法先例）。待责任人签认 design 时一并确认。
