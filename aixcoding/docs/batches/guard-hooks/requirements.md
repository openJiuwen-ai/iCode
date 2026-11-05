# 需求：安全护栏（guard hooks）迁入

> 活文档：随时更新、不签认（git 留痕）；意图确认由 design 签认承载。

## 背景与问题

安全护栏是 0.28.0 final（`main_aixcoding_cli_0.28.0_final`，内网现网服役分支）的既有功能：仓库根
`guard_hooks/` 规则包（`user_prompt_submit` 提示词层 IN-01~IN-09 + `before_tool_call` 工具层 T-*，
命中即打断）+ `src/chrys/app/guard_install.py` 启动自动安装（fail-open）。新分支
（`main_aixcoding_cli`，现上游 0.33.0 基线）不含任何护栏资产。

批次启动呈报（2026-11-05）已做「当期基线上游现状 vs 旧定制」分析并获责任人裁决：
判定＝**有改动的迁**——规则包与安装算法照迁，挂接形态（接线点 / tier 放置 / project-level
hooks 并存策略 / 文档品牌词）按 0.33.0 基线机制重审（见 design.md）。

## 目标与非目标

- 目标：
  - 护栏拦截行为与 0.28.0 final **等价**迁入新分支（规则内容零增删改，`rules.json` 原样平移）；
  - 启动自动安装（幂等、原子、fail-open），已装环境升级包后规则自动刷新；
  - 规则包随 wheel / 离线包分发，内网用户升级后无需手工操作；
  - CI 三平台护栏自检（安装 → 规则自检 → 部署验证）。
- 非目标：
  - 规则内容演进（词典增删、规则调优）——走 `rules.json` 运维通道，不在本批次；
  - 流式输出层（STREAM_CHUNK）拦截——旧需求即明确不实现（hook 子进程 stdin 为 DEVNULL）；
  - 护栏管理界面（TUI `/hooks` 屏等）；
  - agent_studio 桌面端联调事项——随其自身工作项处理。

## 用户场景

1. 内网用户拿到新版离线包安装后，首次启动任一会话入口（TUI / run / acp），护栏自动装入
   `config_dir/hooks/`，此后拦截即时生效；
2. 管理员修改部署机上的 `rules.json`（词典/规则），下次 hook 触发即生效，无需改代码重发版；
3. 已有其他 hook（遥测、审计）的机器升级后，`hooks.yaml` 中无关条目原样保留，护栏两条目按
   id 原位替换。

## 验收标准

- [ ] 全新环境启动任一会话入口后，`config_dir/hooks/guard/`（payload + rules.json + 启动器）
      与 `hooks.yaml` 中 `security-prompt-guard` / `security-tool-guard` 两条目就位，`hook validate` PASS；
- [ ] 已有 `hooks.yaml` 的环境安装后：无关 hook 与无关 settings 保留，两条目按 id 替换；
      损坏配置被备份（`.pre-guard`）后重置；
- [ ] `aixcoding/tests/guard/guard_pack_selftest.py` 全绿（每条规则一个正例 + 日常开发负例，
      含 T-MCP-01～03 与必需规则 ID 集）；
- [ ] 部署验证脚本（对已装规则包跑 MANUAL_TESTS 全量用例）三平台全绿；
- [ ] wheel / 离线包内含规则包全部运行时资产（打包冒烟；无 force-include、无仓库根副本）；
- [ ] `gen_customizations.py --strict` 对账全绿（C136/C137 已登记）；
- [ ] ruff / ruff format / ty 全绿（仅 design「R13 新增豁免清单」列出的最小豁免）。

## 开放问题

- ~~`install` / `uninstall` / `serve` 入口不触发护栏安装的取舍~~ 已收敛（design 备选 D1-A，
  bootstrap 豁免清单行为，等价旧版可用性）；
- ~~`_install_merge.py` 保留或退役~~ 已收敛（design 备选 D4-A，保留照迁）；
- ~~部署验证脚本环境变量前缀~~ 已收敛（`AIXCODING_GUARD_HOOKS_DIR`，ADR-0009）；
- ~~警告通道~~ 已确认：纯 logger，状态核查走 `hook list` / `hook validate`。
