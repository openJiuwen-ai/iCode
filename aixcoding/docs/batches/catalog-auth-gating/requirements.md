# 需求：catalog 登录预验证 + mock 凭证校验 + 响应 detail 日志（catalog-auth-gating）

- 责任人：周彬
- 日期：2026-11-04
- 流程：完整管线（责任人 2026-11-03 裁决——需求 1 选客户端预验证方案 B，"如果需要走管线，那就转走管线"）
- 追加：2026-11-04 责任人裁决——本批落实测暴露的三问题（见下节）一并纳入本批次闭环
  （同分支同 PR，不新开管线）；问题 3 的产品侧"明确拒绝即清账+刷新徽标"轻量兜底
  经责任人明确要求实施（非仅留档）。

## 背景（实证）

2026-11-03 21:53 本机实测：TUI（local/mock）启动时，catalog 启动同步使用**本机保存但已失效**的 token 请求模型列表并成功（HTTP 200，1 model applied）；1.5 秒后 auth silent check 才判定该登录失效并清除。

机制归因（0.28 与 fork 逐字相同，非 fork 回归）：

1. catalog 凭证门控 = "本地存在存储 token 即发送"（`_catalog_token()` docstring：
   "a stale token is the server's to reject"），客户端不预验证；
2. mock 的 catalog 路由零凭证校验（`_serve_catalog` 无 token 检查，无 token 也 200）。

## 追加实测发现（2026-11-04，local/mock 长会话联调）

TUI 保持登录约 15 分钟后，轮询出现
`Model catalog sync skipped: no verified login (logged out, rejected, or offline)`，
两级日志无法定位原因、UI 仍显示已登录。逐项归因：

1. **主卷不可归因**：`verify_live_credential()` 把"无凭证/服务端拒绝/离线"压成同一 None
   且自身零日志，WARNING 三合一，开发人员仅凭 `aixcoding.log` 无法定方向；
2. **detail 失败路径空白**：预验证发出的 `user/info` 交互（被 mock 拒绝）在 detail 卷
   零记录，catalog 请求因拦截未发出——一次真实网络交互在两卷均不可见，无法重现问题；
3. **UI 与凭证脱钩**：`silent_login_check` 仅启动时执行，运行期无周期检查；凭证失效后
   徽标长期停留在"已登录"（裁决乙假设的"有界滞后"实际无界）。实测触发源为 mock 的
   grant 600s 过期回收已签发 token——责任人 2026-11-04 确认 PROD 语义为"不主动登出则
   token 长期有效，无服务端主动失效需求"，mock 行为偏离真实后端。

## 需求（责任人 2026-11-03 拍板；4/5 为 2026-11-04 追加）

1. **客户端预验证**：用户未登录（含存储凭证已失效）时，不尝试获取模型列表；
   只有登录态经 `user/info` 验证有效后才获取。启动同步与周期轮询同口径。
2. **服务端拒绝**：mock server 对缺失/无效/失效 token 的 catalog 请求返回 401，
   不返回模型列表（与真实服务端的 authenticated endpoint 语义对齐）。
3. **结果日志**：获取成功与否的记录义务保持；~~进主日志卷~~ 卷归属按方案六——catalog
   成败摘要属技术事件，仅进 detail 卷（2026-11-04 责任人裁决 F3：维持方案六）；
   **响应详细信息**（模型清单等）进 detail 日志卷，与请求详版同通道（ADR-0017）。
4. **日志分层口径**（责任人 2026-11-04 定义；同日实机反馈后定稿为"主卷用户化 + detail 全量"）：
   - **用户可见日志双写** `aixcoding.log` + `aixcoding.detail.log`；**技术事件只写**
     `aixcoding.detail.log`；
   - 由此 `aixcoding.log` + 日志面板 = 用户视角（功能状态与人话提示，不含技术细节）；
     `aixcoding.detail.log` = 开发人员**自包含全量卷**（含全部事件与请求/响应/异常细节，
     单卷即可理解时序、重现问题，无需两卷对照）；
   - 实机依据（2026-11-04 09:08）：启动 rejected 的 5 行主卷记录全量冒泡到面板，前 4 条
     （预验证/同步技术事件）非用户功能信息——本口径即消除该冒泡。
5. **UI 状态一致性 + mock 语义对齐**（2026-11-04）：
   - verify 遇服务端明确拒绝（AuthServerError，区别于离线）：顺带清除凭证并刷新徽标
     （覆盖 PROD 异常情形：后端重置、账号被删等）；离线/无凭证保持零副作用；
   - mock token 寿命对齐 PROD：签发后不随 device code grant 过期而失效
     （grant 过期仅约束登录轮询阶段）；**无失效时间 ≠ 无效凭证放行**——带 auth 形态
     （含组合启动器默认）下缺失/未知/被吊销 token 一律 401，唯一豁免为显式 `--no-auth`
     启动形态（无 auth state 可验，开发者自选的纯 catalog 联调）。
6. **catalog 文案中文化**（2026-11-04 实机反馈，责任人选定范围 1）：`catalog.py` 的日志与
   相关 `ModelCatalogError` 文案整体使用简体中文，避免逐条发现、逐条修改；外层日志与内层异常
   同批处理，必要技术标识及外部原始异常/服务端 message 保留原文，不改变既有行为与卷归属。
7. **登录失败 UI/detail 分离**（2026-11-04 实机反馈，责任人选定范围 1）：获取登录码与授权
   轮询失败时，界面仅显示固定中文行动指引，不向用户拼接 URL、异常类名或服务端技术详情；
   主卷各记录一条不含技术详情的用户版失败事件，detail 卷仅记录异常类型/message 摘要，不记录
   代码异常栈。

## 验收标准

- [ ] 无存储凭证：启动与轮询均不发出 catalog 请求（现状门控，保持）；
- [ ] 存储凭证失效（服务端拒绝）：预验证拦截，不发出 catalog 请求，detail 卷有降级日志，
      旧模型目录保留；失效凭证清理由 verify 的 rejected 分支顺带执行（清账+徽标通知+
      停止轮询，2026-11-04 裁决乙修订版；~~2026-11-03 旧口径"verify-only 不动存储"~~
      仅限离线/无凭证分支）；
- [ ] 网络不通（离线）：预验证不通过，本轮跳过，旧目录与存储凭证均保留
      （"离线不是登出"，与 `check_silent()` 语义一致）；
- [ ] 凭证有效：预验证通过 → 请求 → 发布，detail 卷记录请求/响应/应用摘要（方案六）；
- [ ] `AIXCODING_MODEL_CATALOG_TOKEN` 直配部署形态：无登录 session 可验，
      env token 存在即视为已授权（预验证短路），有效性由服务端判责；
- [ ] mock：无 token / 未知 token → 401；有效 token → 正常 payload；过期 token 长期有效
      （方案五对齐 PROD），显式吊销（`_revoke`）可测 401；
      `--no-auth` 模式豁免校验（纯 catalog 联调不被堵死）；
- [ ] 响应详版（每模型 id/name/model_id/base_url、version、default_profile_id）
      进 `aixcoding.detail.catalog`，不冒泡到主卷/面板/debug.log；
- [ ] 零上游**源文件**改动（全部落在 `src/chrys/service/aixcoding/`、
      `src/chrys/kernel/aixcoding/auth/`、`src/chrys/app/aixcoding/tui/`、`aixcoding/mock/`、
      `aixcoding/tests/`）；上游触碰仅限既有生成物机械刷新，以及方案七在 `pyproject.toml`
      为 `src/chrys/service/aixcoding/**` 增加单条 RUF001 目录级豁免（R2/R13 登记签认）。

### 2026-11-04 追加验收

- [ ] 预验证失败三原因在 **detail 卷**可区分（拒绝含服务端要点；不含 token/userId），
      主卷/面板无预验证与 catalog 技术事件；
- [ ] detail 卷记录预验证 `user/info` 的请求与响应/异常要点，**单卷**即可重现失败路径；
- [ ] 用户可见事件（登录状态变化、登录/登出结果等）双写两卷；主卷仅剩此类事件；
      **含运行期凭证失效**（verify rejected）：主卷一条用户版（A4/A5 同文案，不含服务端
      技术细节）+ detail 技术版；
- [ ] verify 判明确拒绝：凭证被清除、徽标翻转为未登录、面板有人话提示（toast 承担，
      非日志冒泡）；存储清理语义与 `check_silent` 一致；离线保留凭证与徽标（"离线不是登出"）；
- [ ] mock：token 签发后长期有效（grant 过期不回收 token）；`user/info` 与 catalog 401
      判定保持同一真相源；拒绝/过期测试能力以显式手段保留；
- [ ] 实机重放 07:44 场景：mock 长会话联调不再出现 token 到期失效；人为使 token 失效时，
      detail 卷可归因可重现 + 徽标及时翻转 + 面板 toast 提示 + 主卷一条用户版失效记录、
      无技术事件；
- [ ] catalog 34 处日志模板与同文件 `ModelCatalogError` 文案整体中文化；无凭证组合记录、
      成功/失败链路均有中文断言，技术标识与外部原始异常保留，行为及卷归属不变；
- [ ] 获取登录码与授权轮询失败：UI 严格为固定中文指引，无 URL/异常类名/服务端 message；
      主卷恰好一条 `aixcoding.auth` 用户版 ERROR，detail 有异常摘要且无代码异常栈；
      成功与取消流程不变。
