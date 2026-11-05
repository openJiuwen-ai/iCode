# 自评审记录：安全护栏（guard hooks）迁入

<!--
签认规则（R10 · 记录档）：责任人本人编辑本头部（状态→已确认、填日期，确认意见可选），AI 不得代填。
签认一次后，后续追加评审记录不触发重签。
双 AI 规则：评审 AI 必须与主力开发 AI 不同（不同工具或不同模型），如实记录标识。
完整操作见 SELF-REVIEW-HOWTO.md。
-->

- 责任人：李海潮
- 状态：已确认
- 确认日期：2026-11-05 18:05:17
- 确认意见：无意见
- 开发 AI：主力开发会话（CodeBuddy）
- 评审 AI：Kimi（CodeBuddy 会话，独立第二 AI）

> 记录档：签认一次，后续追加评审记录不触发重签。

## 2026-11-05 评审结论：通过（待责任人签认）

- 轮次：2 轮（第 1 轮全量评审 + 修复；第 2 轮增量复审 + 全量护栏验证）
- 第 1 轮 findings：7 blocker、6 次要项。

- blocker 处置：
  - F1（规则包落点违反 R4）→ 已修复：迁入
    `src/chrys/kernel/aixcoding/guard/pack/`；测试/工具/文档按 R4 分流；撤销 force-include，
    仓库根不保留副本。验证：`test_packaging.py`、wheel 构建与隔离安装冒烟通过。
  - F2（R2 清单待确认）→ 已处置：design 正文回写最终落点，状态按责任人裁决保持已确认；
    plan 同步。AI 未代填签认字段。
  - F3（合法 flow-style YAML 丢配置）→ 已修复：hooks/settings 均保留无关项；新增
    `test_flow_style_hooks_keep_unrelated_entries` 与 `test_flow_style_settings_keep_unrelated_keys`。
  - F4（MCP 规则无正例）→ 已修复：新增 T-MCP-01/02/03 正例与必需规则 ID 集断言；
    selftest 由 63 条增至 70 条。
  - F5（block 决策写失败仍退出 0）→ 已修复：写入失败返回非零；新增子进程故障用例。
  - F6（非法规则结构静默放行）→ 已修复：加载时严格校验规则、检测器、disposition 和
    settings 枚举，非法配置抛 `GuardError`；新增结构错误与枚举错误用例。
  - F7（T-SHELL-05 写入范围静态解析边界）→ 已修复：拆分紧贴参数的重定向；`zip` 检查
    归档目标；`Copy-Item`/`Move-Item` 检查目的地参数；范围判定解析 native
    symlink/junction。selftest 增加对应正例，`test_guard_core.py` 以真实 junction/symlink
    覆盖 Shell 与文件系统双入口。

- 未修项（次要问题，责任人拍板保留）：
  - S1 | `aixcoding/tools/guard_install_merge.py` 半安装修复仅按 prompt ID 判断 | 手工
    应急通道，正常启动自装器覆盖；本批不改。
  - S2 | 部署验证直调 Python，不经过真实 PowerShell 启动器/HookRunner | 启动器与上游
    runner 契约由锚定测试覆盖；真实端到端归 CI/真机手工。
  - S4 | path/extension 检测的部分旧匹配行为偏宽 | 属旧规则逻辑，保持旧行为。
  - S5 | 敏感命中证据完整写入本地 audit；`audit: false` 旧行为未生效 | 旧设计有意留痕，
    后续安全批次评估。

- 增量复审证据：
  - `aixcoding/tests/guard`：24 passed（含 2 个真实 junction/symlink 写入范围用例）
  - `guard_pack_selftest.py`：70 tests OK（新增重定向/zip/PowerShell 目标命令组）
  - 部署验证：64 条，失败 0
  - F7 定向验证：`echo x>../tight.txt`、`zip ../outside.zip src`、
    `Copy-Item a.txt ../outside.txt`、workspace junction 均命中 T-SHELL-05/T-FS-01
  - ruff / format：全仓通过；ty：linux/darwin/win32 通过
  - 架构守卫：270 passed；定制全套：483 passed / 4 skipped
  - `uv build` 成功；wheel 内 6 个运行时资产齐全，隔离安装 `WHEEL_GUARD_SMOKE_OK`
  - 本地全量上游套件未取得通过结论：Windows `LongPathsEnabled=0` 与默认 GBK 导致环境性
    失败，已记录于 `verification.md`；不把该结果冒充为本批通过证据。

- 未验证事项与已知风险：
  - 三平台 CI guard job 尚未在远端执行；PR 后以 checks 为准。
  - 全量本地套件的长路径/编码失败需在启用 Windows 长路径且 UTF-8 的环境或 CI 复验。
