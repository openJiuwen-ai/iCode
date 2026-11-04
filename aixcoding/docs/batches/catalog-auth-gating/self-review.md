# 自评审记录：catalog-auth-gating

- 责任人：周彬
- 状态：已确认
- 确认日期：2026-11-04
- 确认意见：无特别意见
- 开发 AI：Kimi-K3.1-Pro / CodeBuddy
- 评审 AI：GLM-5.3（CodeBuddy，独立评审模式，两轮 design 评审 + 一轮复审，只读）

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

## 七、2026-11-04 增量自审（裁决乙修订版 + 方案四/五）

### design 落地对照

- verify 归因日志：三路径与 design 一致——rejected 主卷 WARNING 含服务端 message（detail 含
  请求 + 被拒详情）、离线 WARNING 含异常链、成功仅 detail；无凭证走 no-token 早退不到 verify
  （R5 口径）。**注意**：主卷 WARNING 携带服务端 message 是对 auth-logging"never message"
  约定的局部例外（仅限 verify 的 rejected 分支；message 为后端 envelope 文案，不含 token），
  依据 = 2026-11-04 design 签认的"含服务端要点"要求，token 双卷零泄漏有用例锚定。
- 方案四：共享清账点 `_clear_rejected_credential()`（check_silent 两分支迁入，F8）；
  回调 = stop + toast + 徽标（logout 同构，F3）；`call_from_thread` + RuntimeError fallback；
  覆盖式注册于 `silent_login_check`（F7-1/2）；回调异常吞（F7-3）有用例。
- 方案五：mock 双侧过期判定摘除，docstring/注释旧口径（F2 裁决 a）已同步改写（F1 清单）；
  吊销拒绝能力以 `_revoke` 用例保留（N1）。

### 实施期发现与处置

- mock verify 单元测试（`_wire_unverified` 假 session）不打 auth 侧 WARNING——启动双条/轮询
  单条的 catalog 侧用例**无需改写**；真实路径形态（启动三条/轮询两条）由真实 session 端到端
  用例与 verify 单测承担，F6 预期形态与测试分工自洽。
- 无新增 await 点 → trajectory manifest 未再触发刷新（Smart Test architecture 323 全绿实证）。

### 遗留与限制更新

- 原"失效清理滞后窗口"（四节）已由方案四消除（rejected 即清账 + 通知 + 止轮询）；离线窗口
  保留为正确语义。
- 双通知（verify 回调与并发 check_silent）最坏重复一次 toast（N4 留档）。
- 实机重放已完成并补录 verification 七节；运行期失效主卷用户版的追加复验亦已闭环。

## 八、方案六自审（2026-11-04，主卷用户化 + detail 全量）

- 归类清单（A 双写 10 / B auth 5 / C catalog 34）全部落地；catalog 主卷 logger 定义已删，
  主卷 catalog 零记录有测试锚（request-redact 用例主卷空断言 + unknown-fields 主卷空断言）；
- 主卷 WARNING 携带服务端 message 的例外（七节）随 B3 迁移已消除——技术记录现仅 detail；
  实机复审后 rejected 补一条 A4/A5 同文案的 ERROR 用户版，不含服务端 message/异常类名，
  auth-logging"never message"约定在主卷保持无例外；
- 测试反转 14 用例（verify 3 + session_logging 2 + catalog 9）全绿；rejected 与 delegated
  拒绝另覆盖主卷用户版恰好一条 + detail 技术版；轮询断言改消息过滤的原因已在 verification
  七节留档；
- 跨批次口径已固化 DEVELOPMENT.md §9（N4 裁决）；
- 实机重放已确认未登录不空转、登录后 detail 全链路、吊销后的 detail 归因/徽标/toast/止轮询；
  运行期失效缺少主卷用户版的 B3 缺口，经代码、自动化断言及 10 秒轮询实机复验闭环：
  主卷恰好一条用户版，detail 保留服务端 message/异常链，主卷无技术记录；
- 五轮文档评审（F1-F8/R1-R6/三轮复审）全部闭环，修订记录逐轮留痕。

## 九、方案七自审（2026-11-04，catalog 中文化）

- 34 处日志与全部 `ModelCatalogError` 文案已中文化；仅改人类可读文本，控制流、级别、卷归属、
  脱敏与清账/通知不变；P2 verify docstring 已同步 B3 实态；
- 初始 ASCII 标点是避免未批准豁免的保守实现；责任人明确选择保留全角标点后，按 R2/R13
  最小新增 service 目录级豁免并登记 C132，无文件级豁免、无既有豁免合并清理；
- 新增无凭证双层组合中文断言，既有英文断言全量同步；完整上游 33262、定制 453、
  architecture 1237 全绿，英文叙述模板残留检索为零；
- 实机无凭证启动已看到外层与内层完整中文文案，主卷无该技术记录；方案七自动化与实机闭环。

## 十、方案八自审（2026-11-04，登录失败 UI/日志分离）

- 两条失败路径均做到固定中文 UI + 主卷用户版 ERROR + detail 异常摘要；取消、成功与异常抛出
  契约不变，complete_login 主卷不再携带 `_reason`；
- 初版 `exc_info=True` 不符合责任人对 detail 的简洁性要求，已移除并以 `exc_info is None`
  双路径锚定；
- 初版主卷使用 UI 模块 logger，caplog 可见但实机主卷漏记；根因是 sink 的 `aixcoding` 子树边界，
  已改用 `aixcoding.auth` 并由实机确认。教训：logger 路由不能仅靠 caplog 验证；
- 按责任人建议将顺序调整为定向 → 实机 → 耗时套件，避免实机问题在长时间回归后才暴露；
- 最终定向 16、定制 454、Smart Test 4284、architecture 324 全绿；实机主卷/detail/UI 三者分离。

## 十一、独立评审裁决（2026-11-04，GLM-5.3 / CodeBuddy）

- 评审结论：通过，blocker 清零；R2/R11、幻觉 API、静默吞错、真实行为断言、数据边界与并发检查
  全部通过，核心定向独立复跑 64 passed；
- N1 修（责任人裁决并签认）：verify 离线 B4 与"离线详情"已合并为一条 detail 摘要；
  定向 13、定制 454 全绿，单条与归因断言均通过；本次 Smart Test 按责任人明确指示跳过并留档；
- N2 不修：delegated 拒绝分支记录/清账顺序为基线既有，无 await 间隙与行为差异，避免触碰
  auth-logging 冻结面；
- N3 不修：detail 请求脱敏版 + 完整版为既有双条测试契约，属机械迁移的自然冗余，不作非必要改动。

## 十二、提交前审计（2026-11-04）

- 发现并清理文档中的历史待补、方案七待实施快照、旧计数/版本与 C132 旧验证口径；
  当前方案六/七/八及 N1 均已有实施和验证记录，无剩余功能或文档事实修正事项；
- 忽略 Smart Test 的提交前复验：定制 **454 passed / 4 skipped**，catalog collect **54 项**；
  ruff/format、IDE 诊断、C132 strict、文档门禁及 diff 检查全部通过；
- 设计正文发生了非语义的事实回填，按 R10 仍需责任人重新签认；本节追加后 self-review
  亦需责任人最终确认。
