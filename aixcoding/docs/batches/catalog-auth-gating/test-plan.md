# 测试计划：catalog-auth-gating

依据：`design.md` 最新签认版（含方案八及独立评审 N1 收尾）；实施以头部确认意见锚定“修订记录最后一条”的签认为准（评审 Nit-2）。
当前基数：`aixcoding/tests/catalog` **54 项**（2026-11-04 提交前审计 collect 口径）；`aixcoding/tests/mock/test_model_catalog_server.py` 21 项（默认无 auth，401 校验豁免、不受影响）。

## 一、auth：`verify_live_credential()`（新文件 `aixcoding/tests/auth/test_verify_live_credential.py`）

> 2026-11-04 注：本节为 11-03 旧版（裁决乙 verify-only）；verify 语义与日志口径以六节
> 增量为准（拒绝清账+通知、失效用户事件双写、技术记录仅 detail 卷）。

真实 `LoginSession`（MemoryBackend + 临时 config_dir），`fetch_user_info` 以假 client 注入：

| # | 用例 | 注入 | 断言 |
|---|---|---|---|
| 1 | 有效存储凭证 | fetch_user_info 返回 AccountInfo | 返回该 account；`stored_token` 原样；store 未 clear |
| 2 | 服务端拒绝（失效） | fetch_user_info 抛 AuthServerError | 返回 None；~~store 未 clear~~（**六节反转：清账+回调一次**，行内标注防漏看节头注） |
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
| 7 | 有效→请求发出 | 默认 autouse + fake httpx 200 | fetch 成功；verify 被调 1 次；detail 卷请求/响应摘要（方案六） |
| 8 | 失效→启动拦截 | verify 返回 None | 无 HTTP 请求；`fetch_catalog` 内 WARNING（raise 前）+ `ModelCatalogError`；`sync_catalog_blocking` 吞错追加第二条 WARNING（**启动双条**，均 detail 卷，方案六）；旧目录保留；主卷零技术记录 |
| 9 | 失效→轮询单条 | 真实 `start_sync_runtime` + verify None | 驱动一轮真实轮询：无 HTTP 请求；detail 卷仅 **1 条**预验证 WARNING（already-logged 契约，轮询不追加）；主卷零技术记录 |
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
| 16 | 过期 token | ~~grant.expires_at 置过去 → 401~~ 方案五后：grant 过期 token 仍 200；失效态以 `_revoke` 构造（六节） |
| 17 | 有效 token | 200 + 现状 payload（models 形态不变） |
| 18 | `--no-auth`（默认 start） | 无 token 也 200（豁免，现有 21 项即回归证据） |

## 五、验证口径

- `aixcoding/tests/catalog`、`aixcoding/tests/auth`、`aixcoding/tests/mock` 全绿；
- ruff check/format 改动文件；`gen_customizations.py --strict` 零漂移；
- Smart Test `--paths` 全部改动文件；
- 实机重放 21:53 场景：失效凭证启动 → 预验证拦截无请求 + detail 卷降级记录；
  重新登录 local → 同步成功 + detail 请求/响应双详版；主卷/面板仅用户状态事件（方案六）；
  运行期吊销 → 主卷恰好一条用户版失效记录，detail 保留服务端技术详情，主卷无技术事件；
  mock 无 token 直 GET → 401。

## 六、2026-11-04 增量（裁决乙修订版 + 方案四/五，design 二次签认）

### verify（test_verify_live_credential.py 重写，14 用例）

| 用例 | 断言 |
|---|---|
| 有效存储凭证 | store 不动（旧 1 保留）；成功仅 detail（请求 + 通过含 ehr），主卷零记录 |
| 服务端拒绝 | **清账 + 回调一次**（旧 2 反转）；无钩子仅清账不报错（N3）；重复拒绝幂等（第二次短路：1 请求 1 通知） |
| 网络失败 | 凭证保留 + 回调不触发（旧 3 加强）；重复离线仍保留 |
| delegated 拒绝 | 停止 shadowing + 回落 store（旧 5 反转）；store 不动；主卷恰好一条 A5 用户版（ERROR、无服务端 message），detail 保留技术版 |
| 回调抛错 | 吞 + detail 卷"通知回调失败"记录（B5）+ 清账成立（F7-3）；主卷仅用户版失效记录，不含回调技术记录 |
| 归因日志（拒绝） | detail 卷含服务端 message（用户不存在）+ 请求 + 被拒详情；**主卷恰好一条 A4 用户版（ERROR），零技术预验证记录**；token 零泄漏 |
| 归因日志（离线） | detail 卷恰好一条离线摘要，含 AuthNetworkError 与 `_reason`（N1 合并重复行）；**主卷零技术预验证记录，且无用户版失效记录** |
| 无凭证 / 本地 TTL 过期 | 短路无请求（旧 4/6 保留） |

### login_actions 回调（test_catalog_sync.py +2）

- 拒绝回调 = logout 同构：`call_from_thread` hop → `stop_sync_runtime` + toast（登录已失效，
  warning）+ 徽标刷新；
- UI 线程 fallback：`call_from_thread` 抛 RuntimeError → 直接执行（stop→notify→refresh 顺序）。

### catalog 端到端（旧 12 反转）

真实 session + AuthServerError → 拦截无 HTTP 请求 + **store 已清** + stored_token None +
回调一次。

### mock（test_model_catalog_server.py 改写 2）

- grant 过期（expires_in=-1）后已签发 token **两侧仍有效**：user/info 200（ehr=1234567）+
  catalog 200（方案五）；
- 显式 `_revoke` → 两侧拒绝：user/info 业务 400 + catalog 401（替代原过期双拒用例；
  即 user/info 与 catalog 拒绝一致性锚，评审 N1）。

## 七、方案七：catalog 日志与相关错误文案中文化（已签认并实施）

- 将 `test_catalog_sync.py` 及其他定制测试中的 catalog 英文叙述断言同步改为中文；日志级别、
  DETAIL 卷归属、主卷零技术事件等既有断言不变；
- 无凭证启动：detail 的吞错记录同时为中文外层"同步模型目录失败"与中文内层"没有模型目录凭证"，
  环境变量 `AIXCODING_MODEL_CATALOG_TOKEN` 保留；
- 有效链路：请求、响应、响应详版、applied 与轮询 started/stopped 等模板均为中文，`GET`、
  `HTTP`、字段名、模型 ID 和值不变；
- 失败链路：预验证拦截、HTTP/网络/JSON/结构校验/发布/回调异常等既有用例使用中文断言，
  外部异常与服务端 message 仍原样保留；
- 实施后检索 `catalog.py` 的 `_detail_log` 与 `raise ModelCatalogError`，确认范围内无英文叙述
  模板残留；保留中文全角标点，`pyproject.toml` 仅新增 `src/chrys/service/aixcoding/**` 的
  RUF001 目录级豁免，无文件级 noqa；
- 跑定制全套、ruff/format 与文档门禁；因 `pyproject.toml` 变更，上游验证升级为**完整套件**，
  不以 Smart Test 代替；复看实机 detail 日志。

## 八、方案八：登录失败 UI 与 detail 技术归因分离（已签认并实施）

- 获取登录码不可达：真实 `LoginDialog` + 无监听端口；界面严格等于"无法获取登录码，请稍后
  重试。"；主卷恰好一条 `aixcoding.auth` 用户版 ERROR；detail 有异常类型/message 摘要且
  `exc_info is None`，无 traceback；
- 授权轮询拒绝：主卷恰好一条 `aixcoding.auth` 用户版 ERROR 且不含 `_reason`；detail 有
  "授权轮询失败"与原因摘要，无 traceback；
- 成功、取消、凭证存储与回调测试保持原断言；`test_11_login_failed_denied` 主卷去除
  `DevicePollDenied`，detail 摘要断言保留；
- 验证顺序：登录 UI/session 定向 → 关闭 mock 实机复核界面/主卷/detail → 定制全套、Smart Test、
  ruff/format 与文档门禁；耗时套件不先于实机。
