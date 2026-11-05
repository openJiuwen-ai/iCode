# 验证：日志治理①——现存埋点路由修复

> 活文档（R7：验证即证据——以下命令均实际执行，结果为真实输出摘要；未运行的验证不描述为通过）。

## 实施摘要

五个产品文件 + 两个测试文件，全部位于定制层（R2 清单为空）：

- A1 `src/chrys/kernel/aixcoding/auth/__init__.py`：`_detail_log`（`aixcoding.detail.auth`）+ 本地 `_DETAIL_LEVEL`；消息「读取登录凭证失败，token 请求头未附加。」+ `exc_info`；文件级 `# ruff: noqa: RUF001`（R13 已批，批准指向本批 design）
- A2 `src/chrys/kernel/aixcoding/auth/crypto.py`：三打点——factory 异常 detail（中文+技术参数）/ 往返校验不一致 detail（新增覆盖 M1 缺口）/ `return MemoryBackend()` 前公共 WARNING「系统凭据库不可用…」+ 同文案 detail 双写；文件级 noqa；noqa 头
- A3 `src/chrys/app/aixcoding/tui/login.py`：删 `logger`（`__name__`）定义；回调失败改 `_detail_log`「登录成功回调失败，继续关闭登录对话框。」+ `exc_info`
- C `src/chrys/app/aixcoding/tui/login_actions.py`：删未使用的 `import logging` 与 `logger` 定义（-2 行）
- D `src/chrys/service/aixcoding/catalog.py`：新增 `_log`（`aixcoding.catalog`）；`_warn_retired_env` 公共 WARNING + detail 双打；:72 定调注释修订（补公共例外，含 marker 文本变更）
- 新增 `aixcoding/tests/auth/test_routing_fixes.py`（7 用例：A1×2、A2×4、A3×1，哨兵断言并入 A2 用例 3）；修订 `aixcoding/tests/catalog/test_catalog_sync.py`（D 断言反转 + 公共卷哨兵）

## 机器验证（四连，2026-11-05，本机 Windows / Python 3.14.8）

```
uv run pytest aixcoding/tests -n0 -q
  → 466 passed, 4 skipped in 53.11s

uv run pytest tests/architecture -n0 -q
  → 1248 passed, 3 skipped in 140.40s

uv run python aixcoding/tools/gen_customizations.py --strict
  → check OK — 98 registration row(s) / 135 marker(s) across 10 design(s), 0 retired id(s), 0 warning(s)
  （:72 marker 文本修订后对账幂等，无登记行变更需求）

uv run pytest aixcoding/tests/auth/test_routing_fixes.py aixcoding/tests/catalog/test_catalog_sync.py -n0 -q
  → 58 passed in 1.58s
```

ruff 门禁（改动文件全集）：

```
uv run ruff check <五产品文件>                       → All checks passed!
uv run ruff format --check <五产品文件>              → 4 formatted / 1 reformatted（crypto.py 已格式化后复检通过）
uv run ruff check/format --check <两测试文件>        → All checks passed / already formatted
```

### verify.py --smart 的拆分说明（责任人裁决：选项 A，2026-11-05）

统一入口 `verify.py --smart` 的组成为"上游 Smart Test 选片 + 无条件完整定制套件"。本批改动全在定制层子树（`*/aixcoding/`），上游测试不 import 这些模块，选片增量预期近空；其"无条件定制套件"部分已由上方第一条（`aixcoding/tests` 全量 466 passed）等效覆盖。责任人裁决不重跑统一入口，以本节拆分验证作为验证证据。

## 实施期注记

1. **caplog 与 detail 记录**：本仓 pytest 环境下 caplog 能收到 `aixcoding.detail.*` 记录（propagate=False 未被 caplog 机制尊重）——`test_session_logging.py` 既有先例的应对是按 `r.name` 过滤（`auth_records` helper），本批公共通道断言全部沿用该形态；新测试首版曾用 `caplog.text == ""` 全文断言，2 用例失败后修正为先例形态。
2. **A3 测试形态**：`LoginDialog._run_login` 以未绑定方法直调 + `SimpleNamespace` 假宿主（session/callback/dismiss 均 fake，`asyncio.run` 驱动）——避免构造 Textual app 上下文；测试断言覆盖"回调抛异常后对话框仍 dismiss"（`dismissed == [account]`）与 detail 记录内容/exc_info。
3. **B023**：spies fixture 首版在循环内定义 Handler 类闭包引用循环变量，ruff B023 红——改为类外定义 + `__init__(buf)` 构造期绑定。
4. **login.py 实施小误**：A3 删 logger 行时曾引入一行临时占位注释，随后同批修正为彻底删除（工作区净终态见 diff）。
5. **ruff format**：crypto.py 两处 `_detail_log.log(...)` 长行由 format 收窄折行，语义零变化。

## 人工目验（进行中，2026-11-05）

环境：`python -m aixcoding.mock`（:7777 目录+登录 / :7778 更新）+ `AIXCODING_AUTH_ENVIRONMENT=local` 下 `uv run aixcoding-cli`，`aixcoding.toml [logging] panel=true`。

- **场景 1 已验**（责任人 TUI 目验"符合预期"）：F6 面板 aixcoding tab 出现停用变量 WARNING（仅一条）。
- **AI 代查 debug.log**（`%APPDATA%\chrys\logs\debug.log`，场景 1 运行时段恰好 3 条 aixcoding 记录，逐条符合）：
  1. `16:55:10 aixcoding.catalog WARNING 环境变量 CHRYS_MODEL_CATALOG_URL 已停用…`（D 项公共 WARNING 冒泡）；
  2. `16:55:10 aixcoding.auth INFO 未登录…；env=local`（主通道，无 ehr）；
  3. `16:56:26 aixcoding.auth INFO 登录成功；env=local`（**无 ehr**——与卷内 10-29~10-31 历史残留「登录成功；ehr=1234567」形成对照，后者为 auth-logging 单通道时期、detail-logging 批次 2026-11-02 改造前写入）；
  - 该时段零 `ehr=` 泄入、零 detail 内容（模型目录请求/读取登录凭证失败/加密后端）、零 A3 英文（`Login success callback failed`）、零旧埋点英文（`No login session`/`crypto backend`）。
- **场景 2 已验**（责任人面板目验"没问题" + AI 代查三卷）：净启动零 WARNING（无停用警告、无 crypto 降级——DPAPI 探测成功静默）；主卷恰 3 条 INFO（17:03:33 已登录静默恢复 / 17:04:10 已登出 / 17:04:15 登录成功，责任人在窗口完成登出→重登闭环），全无 ehr；catalog 技术记录不进面板/主卷。
- **场景 3 已验**（AI 代查 detail 卷）：全量自包含——登录预验证请求/通过（含 ehr）、模型目录请求双版（path-only 版 + `userId=1234567` 全量版）、响应详情、同步生命周期（登出停止→登录再启动联动）、状态转换全量版（`ehr=1234567`）。
- **debug.log 场景 2 时段**：恰 3 条，与主卷同文案冒泡（panel=true 现状行为），零 WARNING、零 ehr。

## 评审修复增量（2026-11-05，双 AI 自审闭环）

评审 AI（CodeBuddy / Kimi-K3.1-Pro）1 blocker + 3 suggestion，全部修复（明细见 self-review.md）：

- blocker：`subtree_spies` 补 `setLevel(DETAIL_LEVEL)`（保存/恢复）——修复"旧 logger 零分发"断言对 DEBUG 回退空过的缺陷（vacuous test）；
- S2：健康候选用例补 `A2_WARNING not in detail_texts`（两卷零 WARNING 完整落地）；
- S3：D 用例补 detail 记录提取断言（name/DETAIL 级别/与公共 WARNING 同文案，钉死双写不漂移）；
- S4：overview.md 状态同步为实施后实态。

修复后回归：目标测试 58 passed（2.41s）、定制套件全量 466 passed, 4 skipped（52.81s）、ruff 全过（test_catalog_sync.py 一处 format 折行）。

## PR #95 评审处置增量（2026-11-05）

**首轮 peer review（head 06b95a54）**：10 候选、无阻塞级工程缺陷，处置——C1（A1 无频控，扩范围获批：进程级 once 守卫，每异常类型一条带栈、同类型后续静默）；C2（DEVELOPMENT.md §9 补"用户可操作的配置错误属用户功能事件"例外）；C3/C4/C5（plan/test-plan/requirements 收尾同步）；C7（A2 双写补 levelno 断言钉 crypto 常量副本）；C8（匿名用例改按名过滤）；C6（PR 描述数字，责任人网页修正）；C9/C10（知悉留档）。

**增量评审（频控守卫落地后）**：1 Major（验收勾选引用旧增量数字/verification 未更新/plan 待办漂移）+ 1 Minor（用户场景"每次失败"与频控口径矛盾；注释绝对化表述）——均已处置：本节补齐当前工作树全量证据，requirements 场景与勾选说明更新，守卫注释改为取舍表述，self-review.md 补记，plan 待办同步。

**C1 增量后全量验证（当前工作树）**：

```
uv run pytest aixcoding/tests/auth/test_routing_fixes.py aixcoding/tests/catalog/test_catalog_sync.py -n0 -q
  → 59 passed in 1.66s（路由 8 用例含新增频控用例 + catalog 51）
uv run pytest aixcoding/tests -n0 -q
  → 467 passed, 4 skipped in 53.75s（基线 466 + C1 用例 1）
uv run pytest tests/architecture -n0 -q
  → 1248 passed, 3 skipped in 146.32s
uv run ruff check aixcoding/ src/chrys/kernel/aixcoding/auth/__init__.py
  → All checks passed（含 RUF003 乘号修正）
uv run ruff format --check aixcoding/ src/chrys/kernel/aixcoding/auth/__init__.py
  → 256 files already formatted
uv run python aixcoding/tools/gen_customizations.py --strict
  → check OK — 98 registration row(s) / 135 marker(s) / 0 warning(s)
```

（评审 AI 独立复核：目标 59 passed、ruff/format 通过、对账 0/0；未复跑全量套件，本节上述全量数字为 AI 本机实跑。）
