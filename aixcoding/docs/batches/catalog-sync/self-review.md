# 自评审记录：模型目录同步 + 档案集中管理（catalog-sync）

- 责任人：周彬
- 状态：已确认
- 确认日期：2026-10-02
- 确认意见：无特别意见
- 开发 AI：BM:GLM-5.3 / CodeBuddy
- 评审 AI：Kimi-K3.1-Pro / CodeBuddy（独立会话，只读评审）

> 记录档：签认一次，后续追加评审记录不触发重签。

## 2026-11-02 评审结论：通过

- 轮次：3 轮（初评 6 findings 全 confirm → 修复 → 增量复审通过 + 1 nit 处置 → 第三轮
  全量复审 4 findings 全 confirm → 修复，定向复跑 148 passed 基线在案）
- 评审范围：工作区全量变更（25 M 文件 + 3 untracked 目录），对照 design.md R2 表 1-18 号
  与登记预告 C030-C049；十项检查（R2/R1/R11 对名、幻觉 API、vacuous test、并发异步、
  数据边界、静默吞错、过度工程、marker 对账）。
- blocker 处置（初评 4 条，全部已修复并经复审 confirm）：
  - F-1 R2 节标题致 check_r2_diff 解析失效（本批 14 上游文件判 unauthorized）→ 标题改
    标准形态 + 17 号拆 17/18 行；复跑 `check_r2_diff --base HEAD` → OK（approved_pool 41）；
    design 修订记录（八）（九）。教训落盘：gen_customizations 与 check_r2_diff 是两道
    独立门禁，前者过不代表后者过，本仓实施期漏跑后者；
  - F-2 发布目录创建 `OSError` 穿透启动路径（mkdir 在异常转换区外，违背实现自身
    docstring 契约）→ mkdir 入 try + mkdir 故障用例（apply 转换 + blocking 返回 None +
    caplog 留痕）；
  - F-3 外部 payload 类型异常穿透（初评 `int(inf)` OverflowError；复审扩展：dataclass
    构造不做运行时类型校验，非 str id 在 regex match、不可哈希 provider 在 set 判断抛
    TypeError，同样穿透）→ `_positive_int` 捕 OverflowError + 构造与字段校验并入统一
    `(TypeError, ValueError)` 转换；三类 payload 用例（inf 直测 + models 形态端到端 +
    TypeError 断言 ModelCatalogError）；
  - F-4 轮询解析失败无日志（parse_catalog 在 fetch 的日志化 try 之外，`_sync_once`
    "already logged" 注释对该路径不实，周期轮询静默）→ fetch_catalog 内 parse 调用
    warning + re-raise；新用例驱动真实轮询（httpx 假 client + parse 注入 +
    `await wait_until` caplog 断言），非只测启动降级。
- suggestion 处置（初评 2 条，全部已修复）：
  - F-5 阶段一清理测试空断言（故障在首次写入前触发，零 tmp 产生，清理断言 vacuous——
    删实现清理块测试仍绿）→ 重写为首个 profile 真写落盘、第二个抛；复审专项确认
    变异（删清理循环）必红；
  - F-6 tmp 暂存名实为 `<id>.yaml.yaml.tmp`（双后缀，与签认设计 `<id>.yaml.tmp` 不符，
    现有 glob 断言因后缀匹配掩盖）→ 拼接修正 + F-5 用例精确文件名锚定。
- 复审增量 nit 处置（1 条，已修复）：mkdir 用例首次 apply 前未 `_wire_disk`，只读触碰
  真实用户 state 文件 → wire 前移，测试完全隔离；复跑 31 passed。
- 第三轮处置（4 findings 全 confirm，全修复）：
  - F-1 blocker 原生 `items`/裸列表形态 `name=["bad"]` 过校验发布落盘，下次启动
    `load_all()` 的 `name.casefold()` 抛 `AttributeError` 穿透启动链 → parse_catalog
    补 `name`/`model_id` 字符串校验（发布前拦截，五个核心 str 全覆盖）+ 用例；
  - F-2 suggestion NaN interval 热循环 / inf 杀轮询线程 → `math.isfinite` 回退 +
    nan/inf/-inf 用例；
  - F-3 suggestion 退役 env 四名仅测两名、新 TOKEN 优先未测 → 参数化四名全覆盖 +
    `_catalog_token` 新旧并存断言；
  - F-4 nit 文档状态/统计未随 17/18 号与修复轮同步 → verification/design/self-review
    三处刷新（25 files / +238/-84、R1 自查 18 行）。
- 第三轮修复复审（同评审 AI，只复审增量）：**4 项均已修复、变异必红（F-1 两字段）、
  增量零新问题；独立复跑 38 passed / check_r2_diff OK / --strict 45/49/0**——
  四轮评审全收敛，blocker 清零。
- 未修项：无。
- 实施期自纠（开发 AI 修复轮内暴露，如实记录）：`wait_until` async 未 await 假通过
  （coroutine 恒 truthy）、mkdir 用例漏 wire `_catalog_token`——均当场修正，见
  verification.md 修复节。
- 最终验证基线：catalog 套件 38 passed；`check_r2_diff --base HEAD` OK；
  `gen_customizations --strict` 45 rows / 49 markers / 0 warning；ruff/format 全绿；
  ty 三平台零 error；上游全套三分类终甄收敛（6 failed 全部 R2 17/18 号签认跟改）；
  离线构建 85.9MB 冒烟 + 责任人目验三项通过。证据链：verification.md。
