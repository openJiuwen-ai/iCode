# 设计：catalog 登录预验证 + mock 凭证校验 + 响应 detail 日志（catalog-auth-gating）

<!--
签认规则（R10）：责任人本人编辑本头部（状态→已确认、填日期、确认意见可选），AI 不得代填。
签认后修改正文需重新签认。校验：uv run python aixcoding/tools/check_docs.py
-->

- 责任人：周彬
- 状态：已确认
- 确认日期：2026-11-04
- 确认意见：无特别意见

## 范围与前提

- 需求来源：`requirements.md`（责任人 2026-11-03 拍板：客户端预验证 + 服务端拒绝 + 响应详版进 detail；
  2026-11-04 追加：日志分层口径 + UI 状态一致性 + mock 语义对齐，三项同批次闭环）。
- **定制主体**：全部落在 `src/chrys/service/aixcoding/catalog.py`、
  `src/chrys/kernel/aixcoding/auth/session.py`、`src/chrys/app/aixcoding/tui/`（方案四回调注册）、
  `aixcoding/mock/`、`aixcoding/tests/`。`app/tui/app.py` 启动挂点不动。
- **R2/R13 上游触碰与豁免登记**：①`tests/architecture/trajectory_wait_manifest.json`
  为上游**生成物**——本批次新增 await 点触发一致性义务，经既有生成链机械刷新（4 项 reopen，
  实施后评审 F1 已补批）；②方案七在 `pyproject.toml` 的既有 `[tool.ruff.lint.per-file-ignores]`
  增加 `"src/chrys/service/aixcoding/**" = ["RUF001"]`，仅豁免该定制目录中文文案的歧义字符检查，
  保留全角标点，不新增文件级 noqa、不调整其他规则。理由：中文排版与该检查存在结构性冲突；
  就地注释含理由与本 design 批准指向。责任人 2026-11-04 确认采用，现已签认、实施并对账通过。
- 既有事实（实证）：`_catalog_token()` 只读存储不验证（0.28 同构）；`check_silent()` 是现成的
  联网验证入口（`user/info`），失效清除凭证、离线保留凭证、delegated 被拒仅停止 shadowing；
  mock catalog 路由零校验，且与 auth mock 共享 `MockAuthState`（`build_combined_handler_class`）。

## 现状与增量（评审 F1，基线钉死）

**基线 = c0118a07（2026-11-03 旧版实施已提交）**。本文行号除注明外均以该 commit 为准。

- **已落地（非本次增量）**：方案一预验证 + env 短路（`fetch_catalog` 内
  `_resolve_catalog_credential` + `verify_live_credential`）、方案二 mock 401 校验、方案三
  detail 响应详版、trajectory manifest 4 项刷新；
- **本批增量清单**：①verify 日志改造（方案一修订条）；②方案四 rejected 清账+回调；
  ③方案五 mock 过期分支摘除；④测试改写（要点 6/7/8）；⑤**方案六（后追加）**：catalog
  34 处主卷记录全量移 detail、auth B 类 5 条移 detail、A 类 10 条双写（7 条新增 detail 侧）；
  ⑥**方案七**：catalog 日志与相关错误文案中文化；⑦**方案八**：登录失败 UI、主卷用户版与
  detail 技术归因分离；⑧独立评审 N1 离线重复 detail 合并；
- **当前实施分界（2026-11-04 提交前审计，取代历史快照）**：①-⑧均已实施，方案六 B3、
  方案七及方案八均已完成相应自动化与实机验证；无剩余功能增量，不得按旧快照重复实施；
- **待改造的既有代码及其注释/docstring 同步改写清单**：
  1. `session.py:247-266` `verify_live_credential()` docstring——"zero side effects / logs
     nothing" 承诺按裁决乙修订版改写（默认零副作用 + rejected 例外清账/通知 + 自记日志）；
  2. `aixcoding/mock/auth/server.py:251-261` `token_owner()` docstring——"catalog-auth-gating
     (review F2): align with the catalog 401 check — an expired token is refused here too"
     注释随方案五摘除过期判定而改写（F2 裁决 a 被方案五推翻，注释不得留旧口径）。

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
  （`return  # already logged where it was raised`，catalog.py:1029-1030）——因此**预验证失败的
  WARNING 必须在 `fetch_catalog` 内 raise 之前记录**，否则轮询每 15 分钟静默吞错，违反
  §9 降级可查。零新增降级路径，行为与"无凭证"现状同构。
- **失效/离线语义（裁决乙，2026-11-04 修订）**：verify 默认零副作用不变；**例外：服务端
  明确拒绝（AuthServerError）时顺带清除凭证并通知 UI**（责任人 2026-11-04 裁决实施，
  覆盖 PROD 异常情形：后端重置、账号被删等）——清除复用 `check_silent` 同一存储语义，
  通知经 app 层注册回调（方案四）。原"清理归 auth 自身检查链路（有界滞后）"假设被实测
  证伪：`silent_login_check` 仅启动时执行，滞后实际无界。离线 → 返回 None、本轮跳过、
  下轮重试，不动存储（"离线不是登出"）。
- **轮询每轮预验证**：每 15 分钟多一次 `user/info` 往返（内网/mock 可忽略）；
  与登出 `stop_sync_runtime` 既有联动不冲突。
- **成本**：启动同步前增加至多一次 `user/info` 往返，延迟上限 = auth 客户端既有超时；
  失败/超时即降级跳过，不阻断 TUI 启动完成。
- 预验证失败日志（2026-11-04 修订——原"预验证过程由 auth 批次既有日志覆盖"假设被实测
  证伪：verify 为本批次新增方法，既有日志只覆盖 `check_silent` 路径，verify 实际零记录）：
  - **verify 内部按失败原因自记**（方案六后均仅 detail 卷）：服务端拒绝 → DETAIL 记录含
    服务端 message/code 要点（不含 token/userId）；离线 → DETAIL 记录含异常链；请求/响应
    细节与成功路径对等（复用 auth 既有 `_detail_log` 先例）；无凭证走 no-token 早退、
    到不了 verify——其记录由 `fetch_catalog` 既有 no-token 异常消息承担（R5，detail 卷）；
  - catalog 侧 skip 记录（`fetch_catalog` 内 raise 之前）保留为**事件摘要**（detail 卷），
    原因由 auth 侧同卷记录承担——跨层不传原因、verify 返回类型不变（仍 `AccountInfo | None`）。
- **预期日志形态（验收断言依据；方案六后技术事件全部落在 detail 卷）**：
  - rejected/离线：detail 卷——启动 = auth 原因 + catalog skip 摘要 + `sync_catalog_blocking`
    吞错（catalog.py:876-877）**三条**；轮询 = auth 原因 + skip 摘要 **两条**/15min
    （already-logged 契约不变，`_sync_once` 不追加）；
  - 无凭证：detail 卷启动 = 吞错一条；轮询无线程（`test_no_credential_means_no_thread`）；
  - rejected 清账后随 `stop_sync_runtime`（方案四）轮询终止；
  - **主卷**：上述技术事件零记录；启动 rejected 场景主卷仅用户状态记录——verify 的失效
    用户版及后续 `check_silent` 的"未登录"（均双写）。

## 方案二：mock 服务端凭证校验

`_serve_catalog` 前置（`aixcoding/mock/model_catalog/server.py`）：

- `auth_state is None`（`--no-auth`）→ 豁免（纯 catalog 联调模式不被堵死）。
  **豁免边界（2026-11-04 责任人澄清）**：这是显式启动形态（无 auth state 可验），不是
  校验漏洞——**带 auth 形态（含组合启动器默认）下，缺失/未知/被吊销 token 一律 401，
  无任何豁免路径**；token 仅无失效时间（方案五对齐 PROD），不代表无效凭证可放行；
- `token` header 缺失 / 非本进程已签发 → **401** `{"error": ...}`
  （~~已过期~~分支由方案五摘除——2026-11-04 起 token 不随 grant 过期失效）：
  判定与 `user/info` 同一真相源（grant 签发记录），但**响应形态刻意不同**——
  `user/info` 未知 token 为 HTTP 200 + 业务码 400"用户不存在"，catalog 为 HTTP 401
  （走 `raise_for_status` 路径），test-plan 两形态断言不得混用；
- grant 遍历走 `MockAuthState.grants()` 方法的 list 快照（mock auth server.py:290-292），
  **不直接遍历 `_grants`**（其注释 "Unsynchronised on purpose"，dict 变更期遍历会
  RuntimeError）；~~过期判定复用 `time.monotonic() >= grant.expires_at`~~（死文本移除，
  过期分支已由方案五摘除，R4）；
- 有效 → 现状 payload。
- 401 body 定为 `{"error": ...}`（HTTP 层拒绝），与 auth mock 的业务 envelope
  （HTTP 200 + `{"message","code","data"}`）属不同层语义、有意区分；test-plan 锚定该结构，
  避免契约测试两形态混用。
- 产品端连锁：401 走 `HTTPStatusError` 既有路径（detail 卷 WARNING + 保留旧目录，方案六），
  无需产品侧改动。
- mock 契约测试连带：原"无 token 直接 GET"用例改为先走 device flow 取有效 token，
  或按新语义断言 401 二态（缺失/未知）+ `--no-auth` 豁免（失效态经 `_revoke` 显式构造，
  方案五；R1 修订）。

## 方案三：响应详版进 detail 卷

`fetch_catalog` 响应解析成功后，在既有主卷 response 摘要之外补一条（ADR-0017 通道；
方案六后该摘要同移 detail，本方案记录与摘要同卷）：

```
_detail_log.log(_DETAIL_LEVEL,
    "Model catalog response detail: version=%s default_profile_id=%s models=%s",
    version, default_profile_id, 每模型 {id, name, model_id, base_url} 列表)
```

- 与请求详版同 logger（`aixcoding.detail.catalog`），常开、不冒泡、与级别无关；
- 不记原始 payload 全文（体积与字段演进不受控），记结构化清单；
- 模型清单非用户敏感字段，但按"详版归 detail"的统一语义入卷，与 auth 双打一致。

## 方案四：rejected 清账与 UI 通知（2026-11-04 新增；评审 F3/F7/F8/N3 修订）

- **分层约束**：kernel 层不 import app/service 层——`LoginSession` 增加凭证失效通知钩子
  （可选回调 `on_credential_rejected`），app 层注册；verify 仅在 AuthServerError 分支
  清账后触发一次；
- **注册挂点（F7-1）**：`login_actions.silent_login_check()` 内注册（启动必经、已持有
  screen，零上游改动）；回调闭包捕获 screen，经 `screen.app.call_from_thread` 包装——
  **verify 可能在 catalog 轮询线程执行，回调必须线程安全**；`SilentCheckScreen` 协议按需
  补 `app` 成员（协议扩展属定制层）；
- **重注册语义（F7-2）**：每次 `silent_login_check` 执行即覆盖式重注册——`LoginSession`
  为进程级单例而 screen/app 可重建，覆盖赋值保证钩子不持有失效引用；
- **回调异常须吞（F7-3）**：回调整体 try/except + 记日志，不反噬 verify（`_publish` 先例，
  catalog.py:1065-1068）；清账先于通知，回调失败时窗口残留仅在 detail 留痕，不升级；
- **回调内容**：`refresh_login_indicator()` + 人话 toast（"登录已失效，请重新登录"）
  + **`stop_sync_runtime()`**（F3：与 logout 同址同模式——lazy import + stop + notify +
  refresh；实际经 `call_from_thread` 在 **UI 线程**执行（R6）；stop 只 bump generation +
  drop refs、**从不 join 线程**（catalog.py:1154-1168），即使轮询线程内自调亦安全（兜底
  论证）；无凭证终态本应无线程——`test_no_credential_means_no_thread`）；
  不 stop 的替代被否决：清账后轮询走 no-token 早退（raise 前无 WARNING），`_sync_once`
  按 already-logged 契约吞错 → 每 15 分钟静默空转，与本批次立项动机冲突；
- **注册前触发 = 仅清账无通知（N3）**：启动同步（`sync_catalog_blocking`）遇 rejected 时
  钩子尚未注册——语义可接受，启动场景徽标由 `check_silent` 翻转；
- **delegated 形态**：被拒仅停止 shadowing（`check_silent` 同语义），不清父凭证；徽标由
  `refresh_login_indicator` 重读 session 决定，不硬编码翻转为登出；
- **清账代码复用（F8）**：抽私有清账辅助（store 清除 + delegated 停止 shadowing），
  `check_silent` 与 verify 的 AuthServerError 分支共用单一清账点；日志文案按调用路径
  参数化，不复制两段平行逻辑。

## 方案五：mock token 寿命对齐 PROD（2026-11-04 新增）

- 依据：责任人 2026-11-04 确认 PROD"不主动登出则 token 长期有效、无服务端主动失效需求"；
  mock 复刻真实后端原则（DIRECTORY.md §6）——grant 600s 过期回收已签发 token 属过度模拟；
- `token_owner` 摘除 `expires_at` 判定：已签发 token 不再随 grant 过期失效；grant 过期仅
  约束 poll 状态机（device code 登录窗口语义不变）；
- model_catalog 的 401 校验同步摘除过期分支，与 `user/info` 保持同一真相源（缺失/未知
  token → 401 不变）；
- 契约测试的"过期/失效 token → 401"场景**钉死用既有 `MockAuthState._revoke()` 构造**
  （N1：零新增开关/API；mock 契约测试与本包同仓库，调用该私有辅助属包内测试惯例），
  日常联调默认长期有效。
- 留档（第二轮评审 N2）：`poll()` 过期分支的 `_revoke` 会顺带回收已签发 token——正常
  device flow granted 后不再 poll，无影响；测试构造避开"granted 后重放 poll"的 edge。

## 方案六：主卷用户化 + detail 全量（2026-11-04 实机反馈，责任人定稿）

口径（requirements 需求 4）：**用户可见事件双写主卷+detail；技术事件仅 detail**——
主卷/面板 = 用户视角，detail = 开发人员自包含全量卷（单卷可理解时序、重现问题）。

**机制零新增、零上游改动**：detail 子树既有双隔离（`propagate=False` + DETAIL 级低于
DEBUG）保证永不冒泡面板/主卷；面板 whitelist（上游 `_TuiLogHandler`/MODULE_ORDER）不动。

归类总原则：**主卷+面板只保留"登录/登出状态与结果"这类用户功能事件（双写）；模型目录
同步、登录预验证、网络/回调等技术细节一律仅 detail**。全仓主卷 logger 仅 `aixcoding.auth`
与 `aixcoding.catalog` 两处（2026-11-04 全量盘点），清单如下完整覆盖。

**A. 双写（主卷原级别原文案 + detail DETAIL 级）——auth 用户状态事件，共 10 条**：

| # | 记录 | 级别 | 产生点 |
|---|---|---|---|
| A1 | 未登录（首次使用或登录已过期） | INFO | check_silent 无凭证 |
| A2 | 已登录（本机保存的登录） | INFO | check_silent 成功（detail 带 ehr，既有） |
| A3 | 已登录（桌面端登录） | INFO | check_silent delegated 成功（detail 带 ehr，既有） |
| A4 | 本机保存的登录已失效，已自动清除，请重新登录 | ERROR | check_silent store 拒绝 |
| A5 | 桌面端传入的登录已失效，改用本机登录 | ERROR | check_silent delegated 拒绝 |
| A6 | 登录成功 | INFO | complete_login（detail 带 ehr，既有） |
| A7 | 登录失败 | ERROR | complete_login |
| A8 | 取消登录 | INFO | complete_login |
| A9 | 已登出 | INFO | logout |
| A10 | 跳过登出（当前使用桌面端登录，由桌面端管理） | INFO | logout delegated |

A 类 detail 侧口径（评审 F5）：A2/A3/A6 为**既有** ehr 详版双打（保留）；其余 **7 条新增**
（A1/A4/A5/A7/A8/A9/A10）——detail 侧同文案 DETAIL 级；失败/失效类（A4/A5/A7）detail 侧
附异常类名链（`_reason`）以便排障；用户事件不含敏感字段。

**B. 仅 detail——auth 技术记录，共 5 条**：

| # | 记录 | 原级别→新 | 产生点 |
|---|---|---|---|
| B1 | 登录检查未完成（网络不通），登录状态保留 | ERROR→DETAIL | check_silent delegated 离线 |
| B2 | 登录检查未完成（网络不通），登录信息保留 | ERROR→DETAIL | check_silent store 离线 |
| B3 | 登录预验证：服务端拒绝凭证（detail 技术版含服务端 message + 异常链） | 仅 detail 技术版；**主卷补用户版**（实机复审修正） | verify rejected |

> B3 主卷用户版（2026-11-04 实机复审，责任人指出"失效是用户应知事件"）：与 A4/A5 同文案
> 同级别（ERROR）——store 凭证："本机保存的登录已失效，已自动清除，请重新登录"；delegated：
> "桌面端传入的登录已失效，改用本机登录"；不含服务端 message/异常类名（技术细节仅 detail）。
> 由此启动失效（A4）与运行期失效（verify）口径一致；B4 离线不变（离线不是登出，无状态变化）。
| B4 | 登录预验证：网络不通，本轮跳过，凭证保留（含异常链） | WARNING→DETAIL | verify 离线；N1 合并原"离线详情"重复行 |
| B5 | 登录失效通知回调失败 | WARNING→DETAIL | verify 回调异常吞 |

（verify 的"预验证请求/被拒详情/通过"3 条本就在 detail，保持；原"离线详情"与 B4 参数完全相同，
按独立评审 N1 裁决并入 B4 单条，异常类型与 `_reason` 摘要均保留。）

**C. 仅 detail——catalog（`aixcoding.catalog`）全部 34 处主卷记录**
（2026-11-04 `findstr /N "logger\."` 逐行实测，含 exception/防御分支，评审 F2 补齐），按组：

| 组 | 记录 | 原级别 |
|---|---|---|
| C1 传输摘要 | request GET / response HTTP / applied | INFO ×3 |
| C2 预验证与降级 | sync skipped: no verified login / Could not sync ... keeping the previous list | WARNING ×2 |
| C3 请求与解析失败 | HTTP 拒绝 / 无响应 / 非 JSON / 响应不可用 | WARNING ×4 |
| C4 发布与暂存 | publish failed / staging 清理失败 ×2 | WARNING ×3 |
| C5 轮询生命周期 | sync started / stopped / not started（无源/无凭证 ×2）/ resume skipped / generation dropped ×2 | INFO ×7 |
| C6 配置与读取异常 | env retired / sync interval 非法 ×2 / 读取登录 session 失败 ×2（既有 exc_info 保留） | WARNING ×5 |
| C7 解析微调 | id 消歧 / 数值字段丢弃 ×2 / 未知字段丢弃 | DEBUG ×4 |
| C8 防御与异常兜底（F2 补登） | running-loop 调用（:873）/ Unexpected error syncing（:880）/ in poll（:1034）/ applying（:1044）/ registry install failed（:1063）/ sync callback failed（:1068） | WARNING ×1 + EXCEPTION ×5 |

实施后：`aixcoding.log` 与面板仅见 A 类；`aixcoding.detail.log` = A+B+C 全量自包含。
实施口径：catalog.py 内 `logger`（aixcoding.catalog）**全部 34 个调用点**机械迁移到
`_detail_log`，不留主卷调用——防御分支一旦触发也不会破坏"主卷无 aixcoding.catalog"断言。

测试连带（改动面如实登记）：

- `test_session_logging.py` 12 条冻结记录：网络不通 2 条改 detail-only 断言；用户状态记录
  补双写断言；
- `test_catalog_sync.py` 主卷断言反转为 detail 断言（sync/applied/skip/吞错等）；"启动双条/
  轮询单条"形态移 detail 卷断言；
- 面板不冒泡断言：主卷 caplog 无 `aixcoding.catalog` / 技术预验证记录；rejected 的用户版
  失效记录由独立断言覆盖。

## 方案七：catalog 日志与相关错误文案中文化（2026-11-04 实机反馈，责任人选定范围 1）

**范围**：`catalog.py` 既有 34 处 `_detail_log` 记录的消息模板，以及同文件全部
`ModelCatalogError` 文案（获取、解析、校验、发布）。外层同步日志会拼接内层异常，二者必须同批
中文化，避免出现"中文外壳 + 英文原因"。异常文案同时可能显示在 CLI，这一并中文化属于本方案
明确接受的可见变化。同步修正 `session.py::verify_live_credential` docstring 的 B3 旧口径
（独立评审 P2）：主卷仅为不含服务端 message 的用户版，detail 技术版保留 message/异常链；
仅改说明文字，无行为变化。

**口径**：
- 面向中文母语者使用简洁、明确的简体中文；保留 `HTTP`、`GET`、`JSON`、环境变量名、字段名、
  模型/profile ID、provider/api_style 等必要技术标识；
- 外部异常文本与服务端原始 message 不翻译、不改写，仍由 `%s`/异常链原样保留，避免损失归因信息；
- 仅改人类可读文案，不改控制流、日志级别、卷归属、脱敏、清账/通知等任何行为；中文全角标点
  按规范保留，使用范围节登记的 `pyproject.toml` 单条目录级 RUF001 豁免，不新增文件级 noqa；
  除该豁免外零上游文件、零新增依赖。

**测试**：更新 `test_catalog_sync.py` 等既有英文文案断言为中文；重点锚定无凭证时外层
"同步模型目录失败"与内层"没有模型目录凭证"的完整组合记录，以及请求/响应/轮询生命周期、
解析与发布错误路径。实施后全文复核 `catalog.py`，不得残留本方案范围内的英文叙述模板。

## 方案八：登录失败 UI 与 detail 技术归因分离（2026-11-04 实机反馈，责任人选定范围 1）

**现状**：`LoginDialog._run_login` 在获取登录码与授权轮询两个 `AuthError` 分支把 `{exc}`
直接拼进界面；`LoginSession.request_device_code` 仅透传异常，因此服务关闭时既暴露 URL/连接
细节，也无日志可归因。

**设计**：
- 获取登录码失败：界面固定显示"无法获取登录码，请稍后重试。"；主卷补一条 ERROR 用户版
  "登录失败：无法获取登录码，请稍后重试。"；detail 记录"获取登录码失败"及异常摘要；
- 授权轮询失败：界面固定显示"登录未完成，请稍后重试。"；`complete_login` 既有主卷登录失败
  记录改为用户版"登录失败：登录未完成，请稍后重试；env=..."，删除 `_reason` 技术链；detail
  保留"授权轮询失败"与既有原因摘要；
- **主卷路由修正**：主卷 sink 仅挂在 `aixcoding` 根 logger，`chrys.app.aixcoding.tui.login`
  不会写入主卷；获取登录码的主卷记录改用 `aixcoding.auth`，避免 caplog 通过但实机漏记；
- **detail 去堆栈**：两条 UI detail 记录均不传 `exc_info`，仅写异常类型与 message 摘要；
  URL、异常类名和服务端 message 可留 detail，但不得输出 traceback；主卷用户版不含技术详情；
- 在既有 UI/session 异常边界记录，不改 `AuthClient`/`LoginSession` 的异常类型与抛出契约；取消
  登录、成功回调和状态迁移不变。零新增上游文件、零新增依赖、零新增豁免。

**测试**：不可达服务与授权拒绝各走真实 UI 流程；断言界面为纯中文固定文案，主卷恰好一条
`aixcoding.auth` 用户版 ERROR，detail 有对应摘要且 `exc_info is None`。验证顺序按责任人建议：
定向测试 → 实机两卷 → Smart Test 等耗时套件。

## 测试计划要点（test-plan.md 展开）

1. 预验证：有效（mock `verify_live_credential` 返回 AccountInfo）→ 请求发出；失效
   （返回 None）→ 无请求 + WARNING（detail 卷，方案六）+ 旧目录保留；离线（None 且凭证保留）→ 同拦截、
   下轮重试；env token 存在 → `verify_live_credential` 不被调用（短路锚定）；
   **verify-only 零副作用锚定**：真实方法调用后 `stored_token` 原样、delegated 标记不变；
2. 轮询路径：真实 `start_sync_runtime` + 预验证注入，断言每轮都先验证（非只测启动）；
3. mock：401 二态（缺失/未知）+ 有效放行 + `--no-auth` 豁免；失效态经 `_revoke` 显式构造
   （方案五后不再有过期分支，F5）；
4. detail 响应详版：`detail_capture` 断言模型清单字段齐全、不冒泡、主卷无重复详版；
5. 回归：`aixcoding/tests/catalog` 全套全绿（collect 口径 **54 项**，2026-11-04 提交前审计
   `--collect-only -q -n0` 实测，含方案七新增组合中文断言）；预验证默认 mock 为"已验证"，避免大面积改写——
   用 conftest 级 autouse fixture 统一注入，逐用例覆盖语义差异。
   **保底**：至少 1 个非 autouse 端到端用例直测"失效凭证 → 真实 `verify_live_credential`
   → 拦截（且存储未被预验证触碰）"（composition-tests 组合链路为现成挂点），
   防 autouse 全遮导致真实集成零覆盖。
   注（2026-11-04）：保底用例语义随裁决乙修订为"rejected → 拦截 **且存储被清除、回调触发**"，
   零副作用锚定收窄到"离线/无凭证"分支；
6. **verify 日志（方案一修订 + 方案六口径）**：三原因在 **detail 卷**可归因（拒绝含服务端
   要点、离线单条摘要含异常链，N1）+ 请求/响应落卷（detail_capture）；**主卷 caplog 零技术预验证记录**，
   rejected 另断言主卷恰好一条 A4/A5 同文案的用户版失效记录（ERROR，不含服务端 message/
   异常类名）；token/userId 不泄漏断言沿用；
7. **rejected 清账+通知（方案四）**：真实 verify + mock 拒绝 → stored_token 清空、回调触发
   一次、`stop_sync_runtime` 被调用（logout 同构断言）；离线 → 存储与回调均不动；轮询线程
   回调经 `call_from_thread`（线程安全锚定）；回调抛错不反噬 verify（F7-3）；
8. **mock 对齐（方案五）**：grant 过期后已签发 token 仍可用（`user/info` 200、catalog 200）；
   未知/缺失 token 拒绝语义不变。

## 备选方案对比

| 方案 | 说明 | 结论 |
|---|---|---|
| A. 预验证收进 `fetch_catalog` | 三路径共同收敛点，启动/轮询/async 自动统一，零上游改动 | **采纳** |
| B. 启动链先 `check_silent` 再 `sync_catalog_blocking`（app.py 时序调整） | 启动语义直观，但只覆盖启动——轮询仍需另做预验证；且需改上游 app.py、同步阻塞启动链 | 否决（覆盖不全 + 上游改动） |
| C. 不预验证，仅服务端 401 兜底 | 零客户端改动，但不满足需求 1"未登录不尝试获取" | 否决（责任人已裁决预验证） |

## 对上游/既有组件隐式行为的假设

1. **`_sync_once` 的 "already logged" 契约**（catalog.py:1029-1030/1041-1042）：`ModelCatalogError`
   必须在 raise 处已记录——本方案预验证 WARNING 放在 `fetch_catalog` 内 raise 之前（评审 B1
   钉死），该契约不被破坏。
2. **`verify_live_credential()` 副作用契约（裁决乙 2026-11-04 修订版）**：默认只读；唯一
   例外为 AuthServerError 分支清账 + 回调通知（方案四），清账与通知同事务触发——不存在
   "catalog 清了凭证而 UI 不知道"的窗口。`check_silent()` 的失效清凭证、离线保留、
   delegated 被拒仅停止 shadowing 语义不变。
3. **并发安全**：轮询线程的 verify-only 与 TUI 自身 silent check 可能并发——最坏重复一次
   `user/info`（无共享缓存），只读请求天然幂等；启动路径两处各跑一次，成本可接受。
   留档（复审 nit，不修）：token 读取与 verify 之间存在 delegated 状态翻转的理论 TOCTOU
   窗口，最坏一次请求的 token 与被验证者不一致（多一次 401 或一次成功），无正确性影响。
4. **env token 无 session 可验**：`AIXCODING_MODEL_CATALOG_TOKEN` 直配形态不经过登录流，
   预验证短路是保持该部署形态可用的前提。

## 定制点登记预告

方案六及此前改动零上游 marker；方案七的 `pyproject.toml` R2/R13 例外新增 1 个 marker，
按下表点级登记（当前 `--report` 最大 C131，故取 C132）。实施后跑 `--strict` 对账。

| id | 落点 | marker 锚 | 预期计数 | anchor | risk | removal_condition |
|---|---|---|---|---|---|---|
| C132 | pyproject.toml | AIxCoding catalog: full-width Chinese punctuation | 1 | `src/chrys/service/aixcoding/**` 的 RUF001 目录级豁免及批准注释 | 低（单条 lint 配置） | 〔目录退役〕该 service 定制目录退役，或中文文案回到上游 i18n 体系且豁免不再必要 |

## ADR 判定

- [x] 不新增/修订 ADR：预验证复用 auth 既有组件语义；detail 通道消费 ADR-0017 已批准机制
      （与 auth-logging 同构，属消费而非新决策）；mock 为开发资产。
- [x] 不推翻任何已接受 ADR。
- [x] 无第三方依赖新增（R11 不适用）。

## 风险与回滚

- 启动延迟：多一次 `user/info` 往返（上限为 auth 超时），可接受；离线启动只慢不同步失败。
- 轮询请求量翻倍（catalog + user/info），15 分钟周期，忽略。
- ~~`check_silent()` 清除副作用扩展~~ 已由裁决乙消除：预验证 verify-only 零副作用；
  ~~失效清理滞后窗口（到 auth 自身下轮检查）在案~~ 2026-11-04 修订：rejected 清账+通知
  （方案四）后该窗口消除；离线窗口保留为正确语义（"离线不是登出"）。
- 新增（2026-11-04）：verify 清账与 TUI 自身 `check_silent` 可能并发——两者同一存储语义、
  清除幂等，最坏重复一次通知/请求，无正确性影响。
- 回滚 = revert 本批次 PR：预验证拦截、mock 校验、detail 响应记录三处独立可拆；
  mock 校验不影响真实服务端行为。

## 验证口径

- `aixcoding/tests/catalog` + mock 相关测试全绿；ruff check/format；
  `gen_customizations.py --strict`（方案七新增 C132 marker，实施后逐点对账，预期零漂移）；
- Smart Test `--paths` 改动文件；
- 实机重放 21:53 场景：失效凭证启动 → 预验证拦截、无 catalog 请求、**detail 卷**降级记录；
  重新登录 local → 同步成功 + detail 请求/响应双详版；主卷/面板仅用户状态事件（方案六）。
- 实机重放 07:44 场景（2026-11-04）：local/mock 登录后 >15 分钟轮询不再 skip（mock 对齐）；
  人为吊销 token 重放：**detail 卷可归因可重现**（含 `user/info` 记录）+ 徽标翻转 +
  面板 toast 人话提示；主卷一条用户版失效记录（A4/A5 同文案），无技术事件。

## 修订记录

- 2026-11-04：实测三问题纳入本批次（责任人裁决同 PR 闭环）；方案一日志条目与裁决乙修订；
  新增方案四（rejected 清账+UI 通知）、方案五（mock token 寿命对齐 PROD）；测试要点补
  6/7/8。本次正文修订需责任人重新签认（头部签认区由责任人本人更新，AI 未代填）。
- 2026-11-04（独立评审处置，评审基线 c0118a07）：
  - F1（Blocker）：新增"现状与增量"节钉死基线；漂移行号更新（catalog.py:1029-1030/
    :876-877，mock auth server.py:290-292）；docstring/注释同步改写清单入册；
  - F2（Blocker）：requirements 旧验收条同步修订为新裁决口径；
  - F3（Blocker）：清账后改为 `stop_sync_runtime()`（与 logout 对齐），否决"自然 skip"
    （no-token 早退无 WARNING → 轮询静默空转）；stop 不 join 线程、轮询线程自调安全已实证；
  - F4（Blocker）：头部签认区待责任人重新签认（AI 未代填）；
  - F5/F6（Major）：方案二过期分支交叉标注摘除、测试要点 3 改二态；预期日志形态段按
    新设计重写（rejected/离线启动三条、轮询两条）；
  - F7/F8（Major）：方案四补注册挂点（silent_login_check 内）、覆盖式重注册、回调异常吞；
    清账抽私有辅助与 check_silent 共用；
  - N1-N3：测试构造钉死 `_revoke`；collect 口径更新 51 项实测；注册前触发=仅清账无通知；
  - N4（双通知留档）：维持"最坏重复一次通知"在案，不改。
- 2026-11-04（第二轮独立评审处置）：R1 方案二残留"三态"改二态；R2 requirements 旧验收
  第 6 条同型漏修补齐；R3 验收目录清单补 session.py/app 定制层 + 生成物例外注明；
  R4 假设 1 第二处行号（:1041-1042）+ 方案二过期判定死文本移除；R5 方案一"无凭证 →
  detail"死路径修正（no-token 早退承担可归因性）；R6 stop 执行线程表述修正（UI 线程执行，
  自调安全仅兜底论证）。评审另核验：headless/ACP 不起轮询（start_sync_runtime 唯一 wiring
  在 TUI app.py:1482），stop 挂 UI 回调形态覆盖自洽。
- 2026-11-04（实机反馈，责任人定稿方案六）：主卷用户化 + detail 全量——用户可见事件双写、
  技术事件仅 detail；预验证/catalog 技术记录移 detail（启动三条/轮询两条形态随之落 detail）；
  check_silent 网络不通移 detail；auth 用户状态事件双写。依据：09:08 实机 5 行主卷记录冒泡
  面板，前 4 条非用户功能信息。方案一日志段、预期日志形态段、requirements 需求 4 与验收
  同步修订。
- 2026-11-04（第三轮独立评审处置）：F1 要点 6 改 detail 口径；F2 C 组实测 34 处
  （findstr 逐行）+ 补登 C8 防御/异常 6 条 + 全量迁移实施口径；F4 增量节补方案六 +
  进度快照；F5 A 类新增 7 条标注 + detail 侧口径（同文案 DETAIL，失败/失效附 _reason 链）；
  N2 poll 过期 edge 留档；N3 collect 53 项实测回填；N5 login_actions 死 logger 为基线既有，
  不动。**F3 责任人裁决：维持方案六**（catalog 成败摘要仅 detail，requirements 需求 3 已
  修订）；**N4 责任人裁决：随本批次在 DEVELOPMENT.md §9 补跨批次日志分类学规则**（已落）。
- 2026-11-04（第四轮复审处置，方案六口径端到端清扫）：R1 design 验证口径节 ×3 改 detail；
  R2 requirements 验收第 4 条 + 追加验收末项改 detail；R3 test-plan 全量重核（一节加新旧
  口径指引、用例 7/8/9 改 detail、用例 16 按方案五、五节实机重放、六节归因日志改 detail +
  主卷零记录）；R4 方案二"主卷 WARNING"改 detail；R5 要点 1 WARNING 补卷归属；Nit 方案三
  加方案六交叉引用；上轮 N1 一致性锚在 test-plan 补注。**本次正文修订需责任人重新签认**
  （R6 补注；头部签认区由责任人本人更新，AI 未代填）。
- 2026-11-04（第五轮复审处置）：N1 test-plan 回调抛错用例改 detail 口径（B5 最后漏网点）；
  N2 用例 2 行内反转标注 + test-plan 头部版本引用更新。本次正文修订需责任人重新签认。
- 2026-11-04（实机复审 B3 修正，责任人明确反馈）：运行期 verify rejected 除 detail 技术版外，
  主卷补一条 A4/A5 同文案的 ERROR 用户版；不含服务端 message/异常类名。同步修订预期日志形态、
  测试要点、实机验收及 store/delegated 双侧断言。
- 2026-11-04（方案七追加，责任人选定范围 1）：catalog 34 处日志模板与同文件全部
  `ModelCatalogError` 文案整体中文化；技术标识和外部原始异常保留，行为与卷归属不变。
  **本次正文修订需责任人重新签认，签认前不实施代码。**
- 2026-11-04（方案七独立评审处置）：P1 当前实施分界取代 F4 历史快照，方案六已完成、方案七
  为唯一剩余功能增量；P2 verify docstring 的 B3 旧口径纳入方案七同步修正；Nit-1 `grants()`
  改普通方法称呼；Nit-2 由责任人在头部确认意见锚定"对应修订记录最后一条"，AI 未代填。
  评审结论：无 Blocker，可按签认版实施。
- 2026-11-04（方案七中文标点与豁免修订，责任人确认采用）：新增
  `src/chrys/service/aixcoding/**` 的 RUF001 目录级豁免以保留全角标点；登记 pyproject 的
  R2/R13 例外与就地注释义务，不合并既有豁免、不新增文件级 noqa；pyproject 变更触发完整测试。
  **本次正文修订需责任人重新签认，签认前不改 pyproject 或恢复标点。**
- 2026-11-04（C132 登记补正，实施前自查）：R3 要求上游 pyproject 改动携带 AIxCoding 注释，
  故将上一条的“就地注释义务”落为点级登记 C132；仅补足 ADR-0015 登记，不扩大已批准的豁免
  范围。**本登记修订需责任人签认后实施。**
- 2026-11-04（方案八追加，责任人选定范围 1）：获取登录码与授权轮询失败的 UI 不再拼接异常，
  detail 增加事件与异常链；仅触定制 UI 与测试，不改异常契约、取消/成功流程或主卷分类。
  **本次正文修订需责任人签认后实施。**
- 2026-11-04（方案八实机复审，责任人指出主卷缺口）：登录失败属于 A 类用户事件，两条路径主卷
  各补一条用户版 ERROR；complete_login 既有主卷 `_reason` 技术链同步移除，detail 原因链保留。
  **本次正文修订需责任人重新签认。**
- 2026-11-04（方案八二次实机复审）：主卷仍缺失的根因是 UI logger 不在 `aixcoding` 子树，
  改用 `aixcoding.auth`；detail 按责任人要求移除 `exc_info` 堆栈，仅留异常摘要。验证顺序改为
  定向 → 实机 → Smart Test 等耗时套件。**本次正文修订需责任人重新签认。**
- 2026-11-04（独立评审 nit 裁决）：N1 修——verify 离线两条同参数 detail 合并为一条；N2 不修
  （基线顺序、无行为差异、冻结面）；N3 不修（双条 GET 契约保留）。**N1 正文修订需签认后实施。**
- 2026-11-04（提交前文档审计）：catalog 测试计数由方案六时期的 53 项回填为当前实测 54 项，
  并清除验证/自审中的历史“待补”；仅更新实施事实与文档状态，不改变设计口径。**按 R10 需重新签认。**
