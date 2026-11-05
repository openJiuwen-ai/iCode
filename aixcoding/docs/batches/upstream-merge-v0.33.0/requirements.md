# 需求：追上游 v0.33.0（upstream-merge-v0.33.0）

> 活文档：随时更新、不签认（git 留痕）；意图确认由 design 签认承载。

## 背景与问题

上游 iCode 已发布 v0.32.3 / v0.33.0（最新 release tag 为 v0.33.0 = 8d5a3208，2026-11-02）。
本 fork 主线 `main_aixcoding_cli` 基线仍为 0.32.2（UPGRADE-RUNBOOK.md 头部「当前基线」节）。
DEVELOPMENT.md §8 升级节奏条款：每次上游发布后评估跟进，长期不升级是需显式决策的风险状态。
区间 `v0.32.2..v0.33.0` 共 47 提交、378 文件、+35641/−1867（干跑实测冲突 6 处，见 design）。

## 目标与非目标

- 目标：`main_aixcoding_cli` 基线追至 **v0.33.0**，全部在册定制点存活且全绿。
- 非目标：
  - 不追镜像头 `origin/main_iCode`（05679bec，领先 v0.33.0 共 24 个未发版提交——留待下次升级）；
  - 不借升级顺手做定制功能开发（含 C4a 运行时品牌等 ROADMAP 阶段三余量批次）；
  - 旧分支 `main_aixcoding_cli_0.28.0_final` 按冻结条款不随升级（runbook §8）。

## 用户场景

1. 内部用户获得上游 0.32.3 / 0.33.0 的修复与新特性（prompt shortcuts、TUI 全彩图像渲染、
   /btw 侧问、/usage 窗口、`{{!command}}` 凭据占位符、Security→Safety 改名等 47 提交内容）。
2. 开发者后续定制开发（ROADMAP 阶段三余量批次）基于新基线，降低三层冲突累积债务。

## 验收标准

- [ ] merge 后 `gen_customizations.py --strict` 0 error / 0 warning，在册定制点无丢失。
- [ ] runbook §4 验证矩阵全绿：fork-full（`verify.py --full`，CodeBuddy 环境配方）、定制四门、
      R2/R3 以新 merge-base（8d5a3208）通过、ty 三平台。
- [ ] UPGRADE-RUNBOOK.md 头部「当前基线」节更新：上游版本 → 0.33.0、升级完成日期
      （CUSTOMIZATIONS.md 人工区已随 customization-registry 批次退役，基线记录现由此节承载）。
- [ ] 行为锚定测试 `aixcoding/tests/diagnostics/test_panel_anchor.py` 绿（anchor 核对机检）。
- [ ] PR 双 CI（aixcoding-tests.yml + 上游 ci.yml 桥接）绿。
- [ ] TUI 冒烟：`uv run aixcoding-cli` 启动正常，F6 可见 aixcoding tab，登录徽标/命令可用。
- [ ] 升级决策清单（冲突点全清单+处理意见、定制化改变清单）已产出，全部决策经责任人裁决并留痕
      （verification.md 追加；淘汰类决策未经裁决不删除任何定制；淘汰检查以
      `gen_customizations.py --report` 全景 95 行登记为输入，逐组记录命中/未命中及依据）。

## 开放问题

（无待决项。）

- （已裁决，2026-11-04）`{{!command}}` × catalog 新信任路径：**选 A**——内网安全受控环境、
  服务端保证永不下发、不考虑被攻破场景；残余风险已知悉接受，B 留作弃用备选
  （见 design「升级引入的新信任路径」小节，评审 2026-11-04 Blocker 1）。
- （已裁决）目标 ref：release tag **v0.33.0 = 8d5a3208**，责任人 2026-11-04 认可——不追
  镜像头、不追上游 main；v0.33.0 对象已在 fork 对象库，无需网络 fetch。
