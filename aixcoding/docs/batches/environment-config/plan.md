# 任务清单：定制环境配置与上游 Settings 解耦（environment-config）

> 活文档（AI 维护，不签认）。续接入口：当前状态、下一步、挂起事项。AI 每次接手先读本文（DEVELOPMENT.md §4 第 1 步）。

## 状态

实现、验证与三轮外部评审完成，待按逐条授权流程提交；未提交、未推送。

## 待办

- [x] 共享 `config/` 子包、唯一缓存快照与环境优先级。
- [x] `startup/` 子包、auth 环境注入与 diagnostics 配置应用。
- [x] auth 统一变量与模型目录 `models/catalog.py` 迁移。
- [x] 退出上游 Settings、刷新 i18n/wait manifest、五个 id 退役。
- [x] 聚焦、上游 Settings、架构、ruff、format、三平台 ty、strict 验证。
- [x] 两个可见 TUI 窗口的登录、模型目录、F4/F6 与文件日志验证完成。
- [x] GLM-5.3 第一轮实现复审及 F-1 至 F-9 处置完成。
- [x] GLM-5.3 第二轮增量复审通过，评审收敛。
- [x] 责任人重签 catalog-sync design。
- [x] 责任人确认 environment-config 的 R2 mo 路径勘误与 §9 支持文档路径。
- [x] 责任人签认 self-review。
- [x] 提交前深度复核：补齐 mock 现行配方、env/TOML 非法值覆盖与三方 drift 断言。
- [x] 补强后聚焦测试、ruff/format、i18n、wait manifest、strict、check_docs 与 diff check。
- [x] GLM-5.3 第三轮增量复审通过；F-R3-1 拼写勘误完成，F-R3-2/F-R3-3 留档。
- [ ] 按逐条授权流程提交 PR。

## 挂起与备忘

- 本机 PATH 的失效 `nvm\\.nodejs` 装入点会触发 WinError 448；过滤后上游失败由 31 降至 5。
- 剩余 5 个上游与 2 个定制失败均为 junction 场景，本批次未改 guard 或 junction 代码；详见 verification。
- 手工 TOML 验证使用的 `temp/environment-config-manual/` 隔离 APPDATA 已清理，不进入提交。
