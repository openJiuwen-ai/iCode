# 自评审记录：catalog-auth-gating

- 责任人：周彬
- 状态：已确认
- 确认日期：2026-11-03
- 确认意见：无特别意见
- 开发 AI：Kimi-K3.1-Pro / CodeBuddy
- 评审 AI：独立第二 AI（两轮 design 评审 + 一轮复审，只读）

> 记录档：design 已经两轮外部 AI 评审 + 一轮复审（2 blocker + 6 设计改进点 + 4 suggestion/nit
> 全闭环，裁决乙 verify-only）；本文为实施后自审，签认以责任人对 design.md 的签认为准。

日期：2026-11-03。按 SELF-REVIEW-HOWTO 口径，对 design（已签认）与 diff 对照自审。

## 一、design 落地对照

- 方案一（预验证收进 `fetch_catalog`）：已落地。store 来源 `await verify_live_credential()`，
  None → WARNING 在 raise 之前（B1/D2 契约）；env 短路复用 `_resolve_catalog_credential()`
  带出的来源（D3，无第二处环境读取）；启动双条/轮询单条日志形态有用例锚定。
- 方案二（mock 401）：已落地。`grants()` 方法快照遍历（D4，非 `_grants` 私有遍历）；
  `time.monotonic()` 过期判定（:264 先例）；401 body `{"error": ...}`（S4/D5 形态区分）；
  `--no-auth` 豁免有显式用例。
- 方案三（响应详版）：已落地。结构化清单（id/name/model_id/base_url + version +
  default_profile_id），不记原始 payload；不冒泡有断言。
- 裁决乙（verify-only）：`verify_live_credential()` 零副作用——auth 单测覆盖"拒绝不清 store /
  离线不动 / 不置 `_delegated_rejected` / delegated 优先与发送同源"；D6 保底用例为真实
  `LoginSession` 端到端（非 autouse 遮蔽）。

## 二、实施期发现与处置（均随发现随修）

1. 双打用例 `detail_capture` 计数 1→2：响应详版引入的预期演进，断言改为按请求详版过滤
   （不再依赖总数），非行为回退。
2. `RunningCatalogMock.server` → 实为 `_server` 私有字段：2 个 mock 用例首跑 AttributeError，
   已修正（幻觉 API 自查：以类定义为准）。
3. ruff PLW0108（lambda 可内联为类）+ 文件末尾换行：已修。
4. `test_wait_manifest_matches_source` 红：源码改动未刷生成物——`trajectory_wait_inventory`
   刷新（4 项 reopen）后 architecture 1238 全绿。教训同 catalog-sync：生成物随同改动刷新。

## 三、test-plan 覆盖核对

18 编号用例全部实现（auth 6 + 幂等参数化 = 8 项；catalog 7；mock 5，合计 20 项新测试），
回归 44 项零改写（conftest autouse 默认已验证），三套 259 项全绿。

## 四、遗留与限制（在案，非本批次范围）

- 失效清理滞后窗口（裁决乙固有）：catalog 持续拦截至 auth 自身下轮检查清理，LLM 请求
  期间靠服务端拒绝——design 风险节在案。
- httpx 第三方 INFO 日志仍含完整 URL（userId 入 debug.log）：责任人 2026-11-03 明示"暂时不用管"。
- mock `_issue_token` 测试捷径使用私有 `_grants`/`_authorize`：限测试内，产品代码走 `grants()` 快照。
- 本机 `aixcoding.toml panel=true`：主卷（非 detail）catalog 记录仍会冒泡到 debug.log/F6——
  全局开关语义，非缺陷。
- TOCTOU nit（复审留档不修）：token 读取与 verify 间 delegated 翻转窗口，最坏一次请求
  token 与被验证者不一致，无正确性影响。

## 五、实施后评审处置（GLM-5.3，2026-11-03）

四项 findings 全闭环：F1（R2 流程）design 补登记生成物刷新例外、责任人重签；
F2（裁决 a）`token_owner` 过期判定 + 过期双拒一致性用例；F3 conftest 补 `stored_user_id`；
F4 前序改动归属备注。实施后增量验证：三套 260 项全绿 + ruff 通过。

## 六、结论

design 三方案 + 两轮评审 12 项 findings + 裁决乙全部落地；测试证据（259 + 10814 + 1238）
与实机重放（401×2、未登录拦截、登录后双详版）齐备。可呈报责任人验收并走外部评审/提交流程。
