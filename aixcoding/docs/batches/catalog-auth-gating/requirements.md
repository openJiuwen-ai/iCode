# 需求：catalog 登录预验证 + mock 凭证校验 + 响应 detail 日志（catalog-auth-gating）

- 责任人：周彬
- 日期：2026-11-03
- 流程：完整管线（责任人 2026-11-03 裁决——需求 1 选客户端预验证方案 B，"如果需要走管线，那就转走管线"）

## 背景（实证）

2026-11-03 21:53 本机实测：TUI（local/mock）启动时，catalog 启动同步使用**本机保存但已失效**的 token 请求模型列表并成功（HTTP 200，1 model applied）；1.5 秒后 auth silent check 才判定该登录失效并清除。

机制归因（0.28 与 fork 逐字相同，非 fork 回归）：

1. catalog 凭证门控 = "本地存在存储 token 即发送"（`_catalog_token()` docstring：
   "a stale token is the server's to reject"），客户端不预验证；
2. mock 的 catalog 路由零凭证校验（`_serve_catalog` 无 token 检查，无 token 也 200）。

## 需求（责任人 2026-11-03 拍板）

1. **客户端预验证**：用户未登录（含存储凭证已失效）时，不尝试获取模型列表；
   只有登录态经 `user/info` 验证有效后才获取。启动同步与周期轮询同口径。
2. **服务端拒绝**：mock server 对缺失/无效/失效 token 的 catalog 请求返回 401，
   不返回模型列表（与真实服务端的 authenticated endpoint 语义对齐）。
3. **结果日志**：获取成功与否进主日志卷（现状已满足，保持）；**响应详细信息**
   （模型清单等）进 detail 日志卷，与请求详版同通道（ADR-0017）。

## 验收标准

- [ ] 无存储凭证：启动与轮询均不发出 catalog 请求（现状门控，保持）；
- [ ] 存储凭证失效（服务端拒绝）：预验证拦截，不发出 catalog 请求，主卷有降级日志，
      旧模型目录保留；失效凭证由 auth 自身检查链路清理（预验证 verify-only 不动存储，
      2026-11-03 裁决乙）；
- [ ] 网络不通（离线）：预验证不通过，本轮跳过，旧目录与存储凭证均保留
      （"离线不是登出"，与 `check_silent()` 语义一致）；
- [ ] 凭证有效：预验证通过 → 请求 → 发布，主卷记录请求/响应/应用摘要；
- [ ] `AIXCODING_MODEL_CATALOG_TOKEN` 直配部署形态：无登录 session 可验，
      env token 存在即视为已授权（预验证短路），有效性由服务端判责；
- [ ] mock：无 token / 未知 token / 过期 token → 401；有效 token → 正常 payload；
      `--no-auth` 模式豁免校验（纯 catalog 联调不被堵死）；
- [ ] 响应详版（每模型 id/name/model_id/base_url、version、default_profile_id）
      进 `aixcoding.detail.catalog`，不冒泡到主卷/面板/debug.log；
- [ ] 零上游文件改动（全部落在 `src/chrys/service/aixcoding/`、`aixcoding/mock/`、
      `aixcoding/tests/`）。
