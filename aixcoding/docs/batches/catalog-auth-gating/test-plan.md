# 测试计划：catalog-auth-gating

依据：已签认 `design.md`（2026-11-03，含裁决乙 verify-only）。
基数：`aixcoding/tests/catalog` 44 项 / 39 函数（collect 口径）；`aixcoding/tests/mock/test_model_catalog_server.py` 21 项（默认无 auth，401 校验豁免、不受影响）。

## 一、auth：`verify_live_credential()`（新文件 `aixcoding/tests/auth/test_verify_live_credential.py`）

真实 `LoginSession`（MemoryBackend + 临时 config_dir），`fetch_user_info` 以假 client 注入：

| # | 用例 | 注入 | 断言 |
|---|---|---|---|
| 1 | 有效存储凭证 | fetch_user_info 返回 AccountInfo | 返回该 account；`stored_token` 原样；store 未 clear |
| 2 | 服务端拒绝（失效） | fetch_user_info 抛 AuthServerError | 返回 None；**store 未 clear**（与 check_silent 的清理分野，零副作用锚定） |
| 3 | 网络失败（离线） | fetch_user_info 抛 AuthError（非 Server） | 返回 None；store 未动 |
| 4 | 无 live 凭证 | 空 store | 返回 None；fetch_user_info 未被调用 |
| 5 | delegated 优先 | delegated token + store 另有 token | 验证用的是 delegated token（与 stored_token 同一来源）；不置 `_delegated_rejected` |
| 6 | 本地 TTL 过期 | credential.is_expired | 返回 None；fetch_user_info 未被调用（stored_token 已拒） |

## 二、catalog：预验证（`test_catalog_sync.py` 新增 + conftest autouse）

conftest autouse：patch `chrys.kernel.aixcoding.auth.get_login_session` 为假 session
（`stored_token="t"`、`verify_live_credential` async 返回 `AccountInfo()`），并 delenv
`AIXCODING_MODEL_CATALOG_TOKEN`——现有 44 项零改写；下列用例自行覆盖 patch。

| # | 用例 | 注入 | 断言 |
|---|---|---|---|
| 7 | 有效→请求发出 | 默认 autouse + fake httpx 200 | fetch 成功；verify 被调 1 次；主卷请求/响应摘要 |
| 8 | 失效→启动拦截 | verify 返回 None | 无 HTTP 请求；`fetch_catalog` 内 WARNING（raise 前）+ `ModelCatalogError`；`sync_catalog_blocking` 吞错追加第二条 WARNING（**启动双条**）；旧目录保留 |
| 9 | 失效→轮询单条 | 真实 `start_sync_runtime` + verify None | 驱动一轮真实轮询：无 HTTP 请求；主卷仅 **1 条**预验证 WARNING（already-logged 契约，轮询不追加）；线程存活下轮重试 |
| 10 | env token 短路 | setenv `AIXCODING_MODEL_CATALOG_TOKEN` | `verify_live_credential` 不被调用；请求直接发出 |
| 11 | token 来源单点解析 | patch `_resolve_catalog_credential` | fetch 内 token 与来源同源（无第二处环境读取）；`_catalog_token()` 薄包装行为不变 |
| 12 | 保底端到端（非 autouse 遮蔽） | 真实 session + 失效凭证 + 假 fetch_user_info（AuthServerError） | 真实 `verify_live_credential` → 拦截、无 HTTP 请求、**store 未被预验证触碰** |

## 三、catalog：响应详版 detail

| # | 用例 | 断言 |
|---|---|---|
| 13 | `test_response_detail_logged`（fake httpx 200 双模型） | `detail_capture` 含响应详版：version、default_profile_id、每模型 id/name/model_id/base_url；logger 名 `aixcoding.detail.catalog`、DETAIL 级；不冒泡 root；主卷无该详版 |

## 四、mock：401 校验（`test_model_catalog_server.py` 新增）

auth 模式启动（`MockAuthState` 共享），device flow 取有效 token：

| # | 用例 | 断言 |
|---|---|---|
| 14 | 无 token header | 401 + `{"error": ...}` 形态 |
| 15 | 未知 token | 401 |
| 16 | 过期 token | grant.expires_at 置过去 → 401 |
| 17 | 有效 token | 200 + 现状 payload（models 形态不变） |
| 18 | `--no-auth`（默认 start） | 无 token 也 200（豁免，现有 21 项即回归证据） |

## 五、验证口径

- `aixcoding/tests/catalog`、`aixcoding/tests/auth`、`aixcoding/tests/mock` 全绿；
- ruff check/format 改动文件；`gen_customizations.py --strict` 零漂移；
- Smart Test `--paths` 全部改动文件；
- 实机重放 21:53 场景：失效凭证启动 → 预验证拦截无请求 + 主卷降级日志；
  重新登录 local → 同步成功 + 主卷摘要 + detail 请求/响应双详版；
  mock 无 token 直 GET → 401。
