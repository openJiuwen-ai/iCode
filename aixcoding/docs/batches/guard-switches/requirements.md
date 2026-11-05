# 需求：安全护栏运行期开关（guard switches）

> 活文档，随时更新、不签认（git 留痕）。逐项冲突与取值影响见同目录 `merge-conflicts.md`。

## 背景与问题

`batches/guard-hooks`（已签认、已实施）把 0.28.0 final 的护栏规则包迁到 0.33.2 骨架，
但**只有文件级开关**：`settings.enabled` 与 `settings.write_scope` 都写在 `rules.json` 里，
而 `rules.json` 每次启动由 `bootstrap_runtime()` 重新安装覆盖。结果是：

- 现场遇到误报只能改仓库再发版，改已安装副本下次启动即失效；
- 无法临时关闭整个护栏，也无法只关某一条规则；
- 写入范围只有一个出厂值，现场不可切换。

同事在 0.28 分支上实现了运行期开关（`guardctl.py` + `guard_state.json`），并补充了
Windows 免 uv 入口（`guardctl.cmd`）。本批次把该能力移植到 0.33.2 骨架。

## 目标与非目标

- 目标：
  - 运行期总开关（`on` / `off`）、单条规则开关（含通配符）、写入范围开关
    （`workspace allow` / `restrict`），状态落 `guard_state.json`；
  - 状态文件不在安装 payload 内，升级 / 重装后保留；开关改动不需要重启会话；
  - Windows 无 uv、无 PATH python 的现场也能管理开关（`guardctl.cmd` 借
    `_launcher.ps1` 现找解释器）；
  - **保留** `batches/guard-hooks` 已落地的 10 项加固（严格配置校验、写决策失败
    非零退出、符号链接解析、shell 写目标修正、flow-style 合并、env 前缀等）。
- 非目标：
  - 规则内容演进（词典增删、规则调优）——除下述 C1 一行外不动 `rules.json`；
  - 流式输出层（`STREAM_CHUNK`）拦截——旧需求即不实现；
  - 护栏管理界面（TUI `/hooks` 屏）；
  - 项目级 hooks 安装（仍只装全局）。

## 关键裁决（2026-11-05，责任人）

**C1：出厂写入范围改为不拦截** —— `rules.json` 的
`settings.write_scope.allow_outside_workspace` 由 `false` 改为 `true`。
理由：新版本不需要拦截工作区外写入。该变更**放宽了一条出厂安全口径**，
与 `batches/guard-hooks/requirements.md`「规则内容零增删改」相冲突，
决策与后果记录于本批次 `adr/0001-guard-write-scope-factory-default.md`。

## 用户场景

1. 内网用户被某条规则误报打断，管理员执行 `guardctl.cmd disable T-SHELL-03-*`
   （或 `python <config_dir>/hooks/guard/guardctl.py disable ...`），**无需重启**，
   下一次工具调用即不再拦截；升级新版后开关仍生效。
2. 部署机需要临时停用整个护栏做演示：`guardctl off`，事后 `guardctl on`。
3. 安全收严演练：`guardctl workspace restrict` 后，写到工作区外的路径被
   `T-FS-01` / `T-SHELL-05` 拦下；演练结束 `workspace allow` 回到出厂行为。
4. 装机后想确认护栏状态：`guardctl status` 打印总开关、写入范围、状态文件路径、
   规则条数与开/关数量。

## 验收标准

- [ ] `src/chrys/kernel/aixcoding/guard/pack/` 内新增 `guardctl.py` / `guardctl.cmd`，
      `_launcher.ps1` 支持绝对路径 `GUARD_SCRIPT`，三个文件均随 wheel 分发；
- [ ] 全新环境启动任一会话入口后，`<config_dir>/hooks/guard/` 下 7 个文件就位，
      `guardctl status` / `list` / `show` / `on` / `off` / `disable` / `enable` /
      `workspace allow|restrict` / `reset [--master]` 全部可用；
- [ ] `guard_state.json` 在重装后保留，总开关、单规则开关、写入范围覆盖均生效；
- [ ] 状态文件缺失 → 走出厂默认（总开关开、写入不限制）；损坏 → 回落出厂默认并告警；
      状态值类型非法 → 拒绝该覆盖（不静默反向）；
- [ ] 出厂 `allow_outside_workspace: true`：默认不拦工作区外写入；
      `workspace restrict` 后 `T-FS-01` / `T-SHELL-05` 正常拦截；
- [ ] `aixcoding/tests/guard/guard_pack_selftest.py` 全绿（含移植的 25 条新用例与
      C1 后的 3 个写范围方法前置）；
- [ ] `verify_guard_deployed.py` 61 条全绿（写入范围用例前置限制态状态文件）；
- [ ] 环境变量统一 `AIXCODING_GUARD_RULES` / `_AUDIT` / `_STATE`；
- [ ] CI `guard` job 三平台通过（含 `PYTHONUTF8: "1"`）；
- [ ] ruff / ruff format / ty（三平台）/ `check_docs.py` 全绿；
- [ ] `gen_customizations.py --strict` 全绿（新增 C138）。

## 开放问题

- ~~出厂写入范围~~ 已裁决（C1 = 不拦截，见 ADR-0001）；
- ~~Windows 免 uv 入口是否要带~~ 已裁决（带，`guardctl.cmd`；无 uv 现场是主要使用场景）；
- ~~`_launcher.ps1` 语义变更是否影响既有 hook~~ 已核（hooks.yaml 传相对路径，兼容）；
- ~~同事交付的 `util/verify_guard_deployed.py` 是否采用~~ 已裁决（不采用，以我方
  `aixcoding/tests/guard/verify_guard_deployed.py` 为准）。
