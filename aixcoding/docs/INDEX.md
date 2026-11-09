# AIxCoding 定制文档目录导航（INDEX）

> 分工：`ROADMAP.md` 管路线与 DoD（阶段/顺序/进度）；本文只管**目录导航**（目录/类型/一句话简介）。
> 批次验收与签认事实见各目录 `verification.md` 与 `design.md` 头部，本文一概不复制——
> 三列全部为一次性人工定性信息，无状态、无 PR 号、无日期（零漂移维护点，ADR-0016）。
> 登记义务：创建批次目录时同步在本文增加一行（DEVELOPMENT.md §6）。

| 目录 | 类型 | 一句话简介 |
|---|---|---|
| [install-uninstall-support.md](install-uninstall-support.md) | 支持手册 | 开发/技术支持用内网安装与卸载详解（协议、版本、全安装方式、清理与排障） |
| [batches/mock](batches/mock/) | 功能批次 | mock 基建迁入（`aixcoding/mock`：auth/update/model_catalog 三服务 + 启动器） |
| [batches/auth](batches/auth/) | 功能批次 | auth 迁入（`src/chrys/kernel/aixcoding/auth` + LLM token 接线） |
| [batches/auth-logging](batches/auth-logging/) | 功能批次 | auth 登录状态埋点（session 落盘与 token 注入 wiring） |
| [batches/detail-logging](batches/detail-logging/) | 功能批次 | detail 仅文件日志子树（敏感明细不进 F6 面板/上游 debug.log，ADR-0017 A′） |
| [batches/tui](batches/tui/) | 功能批次 | tui 迁入（登录对话框 + 会话徽标 + /login、/logout 命令） |
| [batches/tier-migration](batches/tier-migration/) | 功能批次 | ADR-0011 存量定制包按 tier 回迁 kernel 档 |
| [batches/diagnostics](batches/diagnostics/) | 功能批次 | 本地诊断包（config + logging_support；crash/snapshot/launcher 已退役） |
| [batches/auth-isolation-mock-ports](batches/auth-isolation-mock-ports/) | 功能批次 | auth 测试默认构造隔离 + mock launcher 动态端口（local-verify 前置微改动，豁免管线） |
| [batches/catalog-sync](batches/catalog-sync/) | 功能批次 | 模型目录同步 + 档案集中管理（服务端目录整替、15min 轮询、登录联动、模型配置只读） |
| [batches/catalog-auth-gating](batches/catalog-auth-gating/) | 功能批次 | 模型目录登录预验证（未登录/失效不请求）+ mock 凭证 401 校验 + 响应详版 detail 日志 |
| [batches/branding-and-command](batches/branding-and-command/) | 功能批次 | 功能品牌与命令切换 C4（AIxCoding 运行时品牌 + aixcoding-cli 全渠道入口/制品 + 安装迁移与卸载；纯文档后置） |
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
| [batches/catalog-readonly-coverage](batches/catalog-readonly-coverage/) | 治理批次 | 模型目录只读行为正向测试覆盖盘点与补缺（覆盖矩阵落盘 + busy 恒只读 + 指示器 configure 入口锚定） |
| [batches/upstream-merge-v0.32.2](batches/upstream-merge-v0.32.2/) | 升级批次 | 追上游 v0.32.2（基线 0.31.0 → 0.32.2） |
| [batches/launcher-retirement](batches/launcher-retirement/) | 退役批次 | 退役 diagnostics launcher（ADR-0013） |
| [batches/restructure](batches/restructure/) | 改造专项 | 目录重组执行计划——迁入考古参考（管线外） |
| [batches/agents-md-note](batches/agents-md-note/) | 注记 | AGENTS.md 定制规范入口段补登记（定制点 C001，管线外） |
| [batches/upstream-merge-v0.33.0](batches/upstream-merge-v0.33.0/) | 升级批次 | 追上游 v0.33.0（基线 0.32.2 → 0.33.0，目标 ref 8d5a3208 已裁决） |
| [batches/logging-routing-fixes](batches/logging-routing-fixes/) | 治理批次 | 日志治理①——现存埋点路由修复（子树外 logger 归位、死代码清理、crypto 降级与停用 env 警告补公共通道） |
| [batches/internal-installer](batches/internal-installer/) | 功能批次 | 内网安装器与发布约定迁移（`aixcoding/release/` 分发物料：镜像安装器双脚本 + INSTALL.md + 发布约定） |
| [batches/guard-hooks](batches/guard-hooks/) | 功能批次 | 安全护栏迁入（提示词层 + 工具层阻塞拦截规则包 + 启动自装，`src/chrys/kernel/aixcoding/guard`） |
| [batches/guard-switches](batches/guard-switches/) | 功能批次 | 护栏开关（总开关 / 单规则 / 写入范围，`guardctl.py` + 免 uv 入口 `guardctl.cmd` / `guardctl.sh`） |
| [batches/environment-config](batches/environment-config/) | 功能批次 | 定制环境配置与上游 Settings 解耦（AIXCODING_ENVIRONMENT + aixcoding.toml；service 共享快照 + app 注入；零兼容；ADR-0022） |
| [batches/upstream-merge-v0.34.0](batches/upstream-merge-v0.34.0/) | 升级批次 | 追上游 v0.34.0（基线 0.33.0 → 0.34.0） |
| [batches/upstream-merge-v0.35.1](batches/upstream-merge-v0.35.1/) | 升级批次 | 追上游 v0.35.1（基线 0.34.0 → 0.35.1，目标 ref = release tag 已裁决） |
