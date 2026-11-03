# 设计：catalog 登录预验证 + mock 凭证校验 + 响应 detail 日志（catalog-auth-gating）

<!--
签认规则（R10）：责任人本人编辑本头部（状态→已确认、填日期、确认意见可选），AI 不得代填。
签认后修改正文需重新签认。校验：uv run python aixcoding/tools/check_docs.py
-->

- 责任人：周彬
- 状态：已确认
- 确认日期：2026-11-03
- 确认意见：无特别意见

## 范围与前提

- 需求来源：`requirements.md`（责任人 2026-11-03 拍板：客户端预验证 + 服务端拒绝 + 响应详版进 detail）。
- **零上游文件改动**：全部落在 `src/chrys/service/aixcoding/catalog.py`、`aixcoding/mock/`、
  `aixcoding/tests/`。`app/tui/app.py` 启动挂点不动。
- **R2 补充登记（实施后评审 F1，责任人补批）**：`tests/architecture/trajectory_wait_manifest.json`
  为上游**生成物**——本批次新增 await 点触发 `test_wait_manifest_matches_source` 一致性义务，
  经 `uv run python -m tests.support.trajectory_wait_inventory` 机械刷新（4 项 reopen），
  非手工改动、无行为语义（catalog-sync 批次同例）。除此生成物刷新外零上游文件改动。
- 既有事实（实证）：`_catalog_token()` 只读存储不验证（0.28 同构）；`check_silent()` 是现成的
  联网验证入口（`user/info`），失效清除凭证、离线保留凭证、delegated 被拒仅停止 shadowing；
  mock catalog 路由零校验，且与 auth mock 共享 `MockAuthState`（`build_combined_handler_class`）。

## 方案一：客户端预验证（收进 `fetch_catalog`，启动/轮询自动统一）

预验证不新增入口、不改启动链，而是插入 `fetch_catalog` 内部——它是启动同步
（`sync_catalog_blocking`）、周期轮询（`_SyncRuntime`）与 async 路径（`sync_catalog`）的
共同收敛点：

```
fetch_catalog:
  endpoint/token 解析（现状）
  → 预验证（新增，token 来源与判定合并为一处）：
      _catalog_token 解析时带出 token 来源（env / store），预验证复用该来源、不重读环境：
      来源 = env（AIXCODING_MODEL_CATALOG_TOKEN）→ 短路跳过（无 session 可验，服务端判责）
      来源 = store → account = await get_login_session().verify_live_credential()
             account is None → raise ModelCatalogError("未登录或登录未通过验证，跳过本次模型目录同步")
  → 发请求（现状）
```

- **auth 新增公开方法**（`kernel/aixcoding/auth/session.py`，定制文件，零上游）：
  `verify_live_credential() -> AccountInfo | None`——与 `check_silent()` 同走
  `_active_delegated` 优先（保证验证的 token 与实际发送的 token 同一，评审已实证
  session.py:154/:198），只做 `fetch_user_info`，**不触碰 store/delegated 状态**；
  返回 None 表示未验证（无 live 凭证 / 失效 / 离线）。异常归并复用 `check_silent()`
  的 `user/info` 判定与 AuthError 语义，不另造。

- **失败即走既有降级**：启动路径 `sync_catalog_blocking` 对 `ModelCatalogError` 全吞
  （WARNING + 保留旧目录）；轮询 `_sync_once` 对该异常**不记日志**
  （`return  # already logged where it was raised`，catalog.py:996-997）——因此**预验证失败的
  WARNING 必须在 `fetch_catalog` 内 raise 之前记录**，否则轮询每 15 分钟静默吞错，违反
  §9 降级可查。零新增降级路径，行为与"无凭证"现状同构。
- **失效/离线语义（裁决乙：verify-only 职责分离）**：失效 → 预验证拦截，**凭证清理与 UI
  徽标刷新归 auth 自身检查链路**（有界滞后）；期间 catalog 每轮持续被拦截，LLM 请求继续
  带失效 token 由服务端拒绝（既有行为）。离线 → 返回 None、本轮跳过、下轮重试
  （"离线不是登出"，verify-only 不动存储天然满足）。
- **轮询每轮预验证**：每 15 分钟多一次 `user/info` 往返（内网/mock 可忽略）；
  与登出 `stop_sync_runtime` 既有联动不冲突。
- **成本**：启动同步前增加至多一次 `user/info` 往返，延迟上限 = auth 客户端既有超时；
  失败/超时即降级跳过，不阻断 TUI 启动完成。
- 预验证失败日志：主卷 WARNING，**在 `fetch_catalog` 内 raise 之前记录**（不含 token/userId）；
  预验证过程本身的 auth 状态记录（`aixcoding.auth` / `aixcoding.detail.auth`）由 auth 批次
  既有日志覆盖，不重复造。
- **预期日志形态（验收断言依据，两路径不对称属既有行为）**：启动路径
  `sync_catalog_blocking` 吞错时追加第二条 WARNING（catalog.py:844）——启动 = 预验证
  WARNING + 吞错 WARNING **双条**；轮询 `_sync_once` 不追加（already logged 契约）——
  轮询 = **单条** WARNING/15min。

## 方案二：mock 服务端凭证校验

`_serve_catalog` 前置（`aixcoding/mock/model_catalog/server.py`）：

- `auth_state is None`（`--no-auth`）→ 豁免（纯 catalog 联调模式不被堵死）；
- `token` header 缺失 / 非本进程已签发 / 已过期 → **401** `{"error": ...}`：
  判定与 `user/info` 同一真相源（grant 签发记录 + 过期判定），但**响应形态刻意不同**——
  `user/info` 未知 token 为 HTTP 200 + 业务码 400"用户不存在"，catalog 为 HTTP 401
  （走 `raise_for_status` 路径），test-plan 两形态断言不得混用；
- grant 遍历走 `MockAuthState.grants` property 的 list 快照（mock auth server.py:282-284），
  **不直接遍历 `_grants`**（其注释 "Unsynchronised on purpose"，dict 变更期遍历会
  RuntimeError）；过期判定复用 `time.monotonic() >= grant.expires_at`（:264 先例）；
- 有效 → 现状 payload。
- 401 body 定为 `{"error": ...}`（HTTP 层拒绝），与 auth mock 的业务 envelope
  （HTTP 200 + `{"message","code","data"}`）属不同层语义、有意区分；test-plan 锚定该结构，
  避免契约测试两形态混用。
- 产品端连锁：401 走 `HTTPStatusError` 既有路径（主卷 WARNING + 保留旧目录），无需产品侧改动。
- mock 契约测试连带：原"无 token 直接 GET"用例改为先走 device flow 取有效 token，
  或按新语义断言 401 三态（缺失/未知/过期）+ `--no-auth` 豁免。

## 方案三：响应详版进 detail 卷

`fetch_catalog` 响应解析成功后，在既有主卷 response 摘要之外补一条（ADR-0017 通道）：

```
_detail_log.log(_DETAIL_LEVEL,
    "Model catalog response detail: version=%s default_profile_id=%s models=%s",
    version, default_profile_id, 每模型 {id, name, model_id, base_url} 列表)
```

- 与请求详版同 logger（`aixcoding.detail.catalog`），常开、不冒泡、与级别无关；
- 不记原始 payload 全文（体积与字段演进不受控），记结构化清单；
- 模型清单非用户敏感字段，但按"详版归 detail"的统一语义入卷，与 auth 双打一致。

## 测试计划要点（test-plan.md 展开）

1. 预验证：有效（mock `verify_live_credential` 返回 AccountInfo）→ 请求发出；失效
   （返回 None）→ 无请求 + WARNING + 旧目录保留；离线（None 且凭证保留）→ 同拦截、
   下轮重试；env token 存在 → `verify_live_credential` 不被调用（短路锚定）；
   **verify-only 零副作用锚定**：真实方法调用后 `stored_token` 原样、delegated 标记不变；
2. 轮询路径：真实 `start_sync_runtime` + 预验证注入，断言每轮都先验证（非只测启动）；
3. mock：401 三态 + 有效放行 + `--no-auth` 豁免；
4. detail 响应详版：`detail_capture` 断言模型清单字段齐全、不冒泡、主卷无重复详版；
5. 回归：`aixcoding/tests/catalog` 全套全绿（collect 口径 **44 项 / 39 个 test 函数**，
   2026-11-03 `--collect-only` 实测）；预验证默认 mock 为"已验证"，避免大面积改写——
   用 conftest 级 autouse fixture 统一注入，逐用例覆盖语义差异。
   **保底**：至少 1 个非 autouse 端到端用例直测"失效凭证 → 真实 `verify_live_credential`
   → 拦截（且存储未被预验证触碰）"（composition-tests 组合链路为现成挂点），
   防 autouse 全遮导致真实集成零覆盖。

## 备选方案对比

| 方案 | 说明 | 结论 |
|---|---|---|
| A. 预验证收进 `fetch_catalog` | 三路径共同收敛点，启动/轮询/async 自动统一，零上游改动 | **采纳** |
| B. 启动链先 `check_silent` 再 `sync_catalog_blocking`（app.py 时序调整） | 启动语义直观，但只覆盖启动——轮询仍需另做预验证；且需改上游 app.py、同步阻塞启动链 | 否决（覆盖不全 + 上游改动） |
| C. 不预验证，仅服务端 401 兜底 | 零客户端改动，但不满足需求 1"未登录不尝试获取" | 否决（责任人已裁决预验证） |

## 对上游/既有组件隐式行为的假设

1. **`_sync_once` 的 "already logged" 契约**（catalog.py:996-997/1008-1009）：`ModelCatalogError`
   必须在 raise 处已记录——本方案预验证 WARNING 放在 `fetch_catalog` 内 raise 之前（评审 B1
   钉死），该契约不被破坏。
2. **`verify_live_credential()` 零副作用契约**（裁决乙）：只 `fetch_user_info`、不动
   store/delegated——凭证清理与 UI 徽标刷新只发生在 auth 自身检查链路
   （`check_silent()` 的失效清凭证、离线保留、delegated 被拒仅停止 shadowing 语义不变），
   不存在"catalog 清了凭证而 UI 不知道"的窗口。
3. **并发安全**：轮询线程的 verify-only 与 TUI 自身 silent check 可能并发——最坏重复一次
   `user/info`（无共享缓存），只读请求天然幂等；启动路径两处各跑一次，成本可接受。
   留档（复审 nit，不修）：token 读取与 verify 之间存在 delegated 状态翻转的理论 TOCTOU
   窗口，最坏一次请求的 token 与被验证者不一致（多一次 401 或一次成功），无正确性影响。
4. **env token 无 session 可验**：`AIXCODING_MODEL_CATALOG_TOKEN` 直配形态不经过登录流，
   预验证短路是保持该部署形态可用的前提。

## 定制点登记预告

零上游文件改动、零 marker 区触碰——点级登记（gen_customizations）**不适用**：登记义务针对
上游文件内的定制点，本批次全部改动位于定制目录（`src/chrys/service/aixcoding/`、
`aixcoding/mock/`、`aixcoding/tests/`）。实施后照常跑 `--strict` 确认零漂移。

## ADR 判定

- [x] 不新增/修订 ADR：预验证复用 auth 既有组件语义；detail 通道消费 ADR-0017 已批准机制
      （与 auth-logging 同构，属消费而非新决策）；mock 为开发资产。
- [x] 不推翻任何已接受 ADR。
- [x] 无第三方依赖新增（R11 不适用）。

## 风险与回滚

- 启动延迟：多一次 `user/info` 往返（上限为 auth 超时），可接受；离线启动只慢不同步失败。
- 轮询请求量翻倍（catalog + user/info），15 分钟周期，忽略。
- ~~`check_silent()` 清除副作用扩展~~ 已由裁决乙消除：预验证 verify-only 零副作用；
  失效清理滞后窗口（到 auth 自身下轮检查）在案，期间 LLM 请求靠服务端拒绝，属既有行为。
- 回滚 = revert 本批次 PR：预验证拦截、mock 校验、detail 响应记录三处独立可拆；
  mock 校验不影响真实服务端行为。

## 验证口径

- `aixcoding/tests/catalog` + mock 相关测试全绿；ruff check/format；
  `gen_customizations.py --strict`（本批次无 marker 区改动，预期零漂移）；
- Smart Test `--paths` 改动文件；
- 实机重放 21:53 场景：失效凭证启动 → 预验证拦截、无 catalog 请求、主卷降级日志；
  重新登录 local → 同步成功 + 主卷摘要 + detail 请求/响应双详版。
