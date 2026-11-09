# 自评审记录：定制环境配置与上游 Settings 解耦（environment-config）

<!--
签认规则（R10 · 记录档）：责任人本人编辑本头部（状态→已确认、填日期，确认意见可选），AI 不得代填。
签认一次后，后续追加评审记录不触发重签。双 AI 评审须由不同工具或模型完成。
-->

- 责任人：周彬
- 状态：已确认
- 确认日期：2026-11-09
- 确认意见：无特别意见
- 开发 AI：Kimi-K3.1-Pro / CodeBuddy
- 评审 AI：GLM-5.3 / CodeBuddy（与开发 AI 不同模型）

## 开发方自审范围

- R2 八项上游改动：Settings 字段/spec、process projection、settings UI、pot/po/mo、i18n oracle、C004 路径注释、
  C034 import、wait manifest。编译 mo 实际路径已在第 4 项勘误，待责任人确认。
- 新增 `service/aixcoding/config/`、`service/aixcoding/models/`、`app/aixcoding/startup/` 与
  `aixcoding/tests/config/`；旧 `catalog.py`、`startup.py`、diagnostics 文件读取测试已迁移或删除。
- 零第三方依赖，零 git 写操作，零新增上游 marker；最终 strict retired=5。

## 开发方自审结论（不代替外部复审）

- 配置边界：唯一文件读取在 service loader；kernel diagnostics 只保留数据类型与日志目录解析，未出现向上 import。
- 启动边界：公开 import 与 `startup._TASKS` patch 兼容；环境注入先于默认 `LoginSession` 构造，单快照解析一次。
- auth 边界：跨层仅传字符串；显式 `environ` 继续绕过注入；`resolve_endpoints(None)` 不再绕过启动注入。
- 模型目录边界：完整 URL > TOML base > tier base；token/userId、同步、发布、轮询和只读 UI 主体未改。
- 零兼容：旧变量不读取、不检测、不告警；退役 WARNING 函数、状态表、logger 与旧测试已删除。
- 安全：不持久化 token/userId，不记录配置值；显式坏 base 不回退 prod；未放宽任何审批或工具权限。
- 生成物：i18n 与 wait manifest 均由项目命令刷新；manifest 仅模块路径差异。
- 验证：聚焦 127 passed；上游 Settings 2774 passed/12 skipped；架构 1248 passed/3 skipped；静态门禁通过。
  环境变量与仅 TOML 两条手工路径均通过，模型目录 HTTP 200 且档案落盘。完整 verify 的 7 个 junction
  环境失败已如实记录，未宣称全绿。

## 待外部复审重点

1. `load_config()` 的环境优先级、坏类型与缓存隔离是否存在遗漏。
2. `configure_environment()` 的生产注入与显式 `environ` 测试隔离是否形成其他隐式旁路。
3. startup 子包的任务表持有方式是否完整保持现有 monkeypatch 与幂等契约。
4. Settings/i18n/manifest 删除面是否还有未被检索发现的旧键或旧模块引用。
5. 7 个 junction 失败的环境性判断是否有任何证据能与本批次改动关联。

## 2026-11-09 GLM-5.3 外部实现复审：第一轮处置

- F-1（blocker）：mock 源码、三个 README、支持手册与 DIRECTORY §9 已补齐；mock 树旧变量和旧
  Settings 键检索为 0。受影响测试 215 passed / 2 skipped。
- F-2：design §9 支持手册路径已改为实际 `aixcoding/docs/install-uninstall-support.md`。
- F-3：TOML base 有无尾斜杠已参数化锚定，目录路径均只拼接一次。
- F-4：ROADMAP 两处 ADR-0022 状态已同步为已确认。
- F-5：覆盖矩阵已改用真实测试用例名。
- F-6：不改。登记工具要求无编号 `## 定制点登记预告`，保留该工具契约，编号由 §5 到 §7 不影响内容。
- F-7：恢复 `test_main_calls_aixcoding_startup` 的 in-function import 测试接缝说明。
- F-8：非法 environment WARNING 只保留来源和 prod 回退，不再输出配置值。
- F-9：空配置与正常配置统一经 `_load_diagnostics()` 构造，删除重复专用分支。
- 第一轮处置后进入第二轮增量复审。

## 2026-11-09 GLM-5.3 外部实现复审：第二轮收敛结论

- 第一轮 blocker（F-1）已修复且复审通过；其余 findings 均已有效处置，F-6 不修理由成立。
- 增量改动未引入新问题；本轮评审收敛。
- 后续仅剩责任人确认 environment-config design 的 R2 mo 路径与 §9 支持文档路径两项勘误，并签认本记录。
- 本记录不代替责任人签认。

## 2026-11-09 提交前复核与补强（第二轮收敛后）

- 深度复核发现第一轮 F-1 的 mock 清理不完整：README、launcher、update mock 与模型目录注释仍有失效旧变量配方；
  已全部改用现行配置或明确 update 尚无产品消费方，当前 mock 树旧变量与旧 Settings 键检索为 0。
- 补齐 env/TOML 两种来源各自空值和非法值参数化覆盖；drift 断言现同时约束 `_CATALOG_BASES`、loader
  `VALID_ENVIRONMENTS` 与 `auth.Environment` 三方一致。
- 清理现行测试模块的退役 WARNING 描述，同步 plan、test-plan 与 ROADMAP 的签认/复核状态。
- 验证：聚焦 217 passed / 2 skipped；ruff 与 format 通过；i18n check、wait manifest（0 reopened）、strict、
  check_docs、git diff --check 均通过。
- 上述改动发生在 GLM 第二轮收敛之后，须再取得 GLM-5.3 增量复审通过；复审前不得视为提交就绪。

## 2026-11-09 GLM-5.3 外部实现复审：第三轮收敛结论

- blocker 无；mock 清理、两项测试补强、verification 纠正记录、活文档同步与范围控制均复审通过。
- F-R3-1（应修）：开发 AI 标识拼写已由 `Ki-i-K3.1-Pro` 勘误为 `Kimi-K3.1-Pro`；未改签认三要素。
- F-R3-2（可留档）：mock README telemetry 行的 0.28 `config.py` 描述非现行操作指引，留待 telemetry 批次处理。
- F-R3-3（观察）：verification 前节原始结论与后续纠正节并存符合不删改历史纪律，无需改动。
- 第三轮增量补强评审收敛，可进入责任人逐条授权的 add/commit/push 与 PR 流程。

## 2026-11-09 PR #103 peer review：C1 处置

- peer review 确认 R2/R11 清单内、零幻觉 API、CI 26 checks 全绿；唯一缺陷候选 C1 为旧
  `test_bad_level_name_falls_back` 迁移遗漏，非法 level 名回退失去测试锚定。
- 责任人裁决本批补测；已在 `aixcoding/tests/config/test_config.py` 补回该用例，行为本体未改。
- 补测后聚焦 218 passed / 2 skipped；ruff、format、check_docs、strict（100 行 / 137 marker /
  retired=5 / 0 warning）、git diff --check 通过。
