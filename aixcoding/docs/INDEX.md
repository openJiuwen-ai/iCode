# AIxCoding 定制文档目录导航（INDEX）

> 分工：`ROADMAP.md` 管路线与 DoD（阶段/顺序/进度）；本文只管**目录导航**（目录/类型/一句话简介）。
> 批次验收与签认事实见各目录 `verification.md` 与 `design.md` 头部，本文一概不复制——
> 三列全部为一次性人工定性信息，无状态、无 PR 号、无日期（零漂移维护点，ADR-0016）。
> 登记义务：创建批次目录时同步在本文增加一行（DEVELOPMENT.md §6）。

| 目录 | 类型 | 一句话简介 |
|---|---|---|
| [batches/mock](batches/mock/) | 功能批次 | mock 基建迁入（`aixcoding/mock`：auth/update/model_catalog 三服务 + 启动器） |
| [batches/auth](batches/auth/) | 功能批次 | auth 迁入（`src/chrys/kernel/aixcoding/auth` + LLM token 接线） |
| [batches/auth-logging](batches/auth-logging/) | 功能批次 | auth 登录状态埋点（session 落盘与 token 注入 wiring） |
| [batches/detail-logging](batches/detail-logging/) | 功能批次 | detail 仅文件日志子树（敏感明细不进 F6 面板/上游 debug.log，ADR-0017 A′） |
| [batches/tui](batches/tui/) | 功能批次 | tui 迁入（登录对话框 + 会话徽标 + /login、/logout 命令） |
| [batches/tier-migration](batches/tier-migration/) | 功能批次 | ADR-0011 存量定制包按 tier 回迁 kernel 档 |
| [batches/diagnostics](batches/diagnostics/) | 功能批次 | 本地诊断包（config + logging_support；crash/snapshot/launcher 已退役） |
| [batches/auth-isolation-mock-ports](batches/auth-isolation-mock-ports/) | 功能批次 | auth 测试默认构造隔离 + mock launcher 动态端口（local-verify 前置微改动，豁免管线） |
| [batches/ai-gates](batches/ai-gates/) | 治理批次 | AI 流程闸门 |
| [batches/ci-gate](batches/ci-gate/) | 治理批次 | 定制 CI 质量门（`aixcoding-tests.yml`） |
| [batches/upstream-ci](batches/upstream-ci/) | 治理批次 | 上游 CI 桥接（`aixcoding-upstream-ci.yml`） |
| [batches/customization-registry](batches/customization-registry/) | 治理批次 | 定制点登记机制改造（点级化，ADR-0015） |
| [batches/devtools](batches/devtools/) | 治理批次 | 规范落地四工具（清单生成 / R2 比对 / R3 校验 / import 边界） |
| [batches/exemption-governance](batches/exemption-governance/) | 治理批次 | 豁免治理（R13——豁免须理由与批准） |
| [batches/ruff-test-exemptions](batches/ruff-test-exemptions/) | 治理批次 | `aixcoding/tests` 的 ruff 豁免切换为目录级配置 |
| [batches/local-verify](batches/local-verify/) | 治理批次 | fork 统一本地验收入口 `verify.py`（smart 无条件双跑 + fork-full 语义） |
| [batches/windows-matrix](batches/windows-matrix/) | 治理批次 | 定制门扩 Windows 矩阵（test/govern/gate 三层，DPAPI 平台真实用例实跑） |
| [batches/composition-tests](batches/composition-tests/) | 治理批次 | 服务组合测试（登录→LLM 请求→登出的生产组装链路，凭据生命周期闭环） |
| [batches/docs-reorg](batches/docs-reorg/) | 治理批次 | docs 目录分层组织（`batches/` 布局 + 本索引，ADR-0016） |
| [batches/upstream-merge-v0.32.2](batches/upstream-merge-v0.32.2/) | 升级批次 | 追上游 v0.32.2（基线 0.31.0 → 0.32.2） |
| [batches/launcher-retirement](batches/launcher-retirement/) | 退役批次 | 退役 diagnostics launcher（ADR-0013） |
| [batches/restructure](batches/restructure/) | 改造专项 | 目录重组执行计划——迁入考古参考（管线外） |
| [batches/agents-md-note](batches/agents-md-note/) | 注记 | AGENTS.md 定制规范入口段补登记（定制点 C001，管线外） |
