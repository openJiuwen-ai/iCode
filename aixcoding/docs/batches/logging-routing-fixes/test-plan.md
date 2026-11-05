# 测试方案：日志治理①——现存埋点路由修复

> 活文档：随时更新、不签认；其质量由双 AI 自审（SELF-REVIEW-HOWTO）与行为锚定测试本身保障。

## 测试范围与策略

- 层级：**单元测试**（经真实 logger 树分发，禁直调 `handler.emit`）；契约锚定 = 目标卷 logger 名（`record.name` 为 `aixcoding.detail.auth` / `aixcoding.auth` / `aixcoding.catalog`）与级别（detail 记录 levelname 渲染 `DETAIL`）；零上游接触故无上游行为锚定项。
- 落点：新增 `aixcoding/tests/auth/test_routing_fixes.py`（A1/A2/A3）；修订 `aixcoding/tests/catalog/test_catalog_sync.py`（D：:699 断言反转 + 双写断言）；`test_auth_crypto.py` 既有用例**不动**（design 定案：日志面断言由新测试承担）。
- fixture：复用 `test_session_logging.py` 先例——`_aix_logger_isolation` 模式（钉扎 `aixcoding` 子树 handlers/propagate/level，防跨测试顺序污染）+ `detail_capture` 模式（直挂 `aixcoding.detail` 捕获 handler，镜像生产预置 `propagate=False`+`level=DETAIL`）。新文件内按需复制这两个 fixture（同 auth 域，跨文件 import 测试设施需评估，复制为先例形态）。
- caplog 边界：detail 记录 `propagate=False`，caplog（root handler）捕获不到——detail 断言一律走捕获 handler；公共通道断言用 caplog。
- 子树外名字哨兵：A1/A2/A3 各挂间谍 handler 于 `chrys.kernel.aixcoding.auth` / `chrys.app.aixcoding.tui.login`（`getLogger(__name__)` 原名），断言零分发——这是"彻底无声/泄入上游卷"缺陷的直接回归锚。
- crypto 候选注入：monkeypatch `crypto._platform_factories`（既有 `_with_factories` 模式，`test_auth_crypto.py` 先例）。

## 覆盖矩阵

> 实施期注记（2026-11-05，PR #95 review C4 回写）：规划用例名以实施为准——"哨兵"并入 A2 异常耗尽用例（7 用例）；PR #95 评审后新增 C1 频控用例（共 8）。

| 验收标准（requirements.md） | 测试入口（文件::用例） | 类型 |
|---|---|---|
| 子树外 logger 名零分发 | test_routing_fixes.py::A1/A2/A3 各自间谍断言 | 单元 |
| A1 detail 记录（"读取登录凭证失败"口径 + exc_info），主卷/面板无 | test_routing_fixes.py::test_a1_failure_lands_on_detail_only | 单元 |
| A1 持续故障频控（每异常类型一条，PR #95 review C1） | test_routing_fixes.py::test_a1_persistent_failure_logs_once_per_exception_type | 单元 |
| A2-①主卷 WARNING 无技术细节（哨兵，并入异常耗尽用例） | test_routing_fixes.py::test_a2_exploding_exhausts_candidates_full_double_write | 单元 |
| A2-②detail 同文案双写 + levelno 断言（C7） | 同上 | 单元 |
| A2-③异常路径与往返不一致路径独立 detail 断言 | test_a2_exploding_exhausts_candidates_full_double_write / test_a2_garbage_roundtrip_mismatch_is_recorded | 单元 |
| A2-④probe 成功两卷零记录 | test_routing_fixes.py::test_a2_healthy_probe_is_silent | 单元 |
| A2-⑤健康候选 `:255` 提前返回零 WARNING | test_routing_fixes.py::test_a2_healthy_candidate_short_circuits_without_warning | 单元 |
| A3 detail 记录 + exc_info，主卷零 | test_routing_fixes.py::test_a3_callback_failure_lands_on_detail_only | 单元 |
| C 无未使用 import/logger | ruff F401/F841 门禁兜底（回归命令） | 检查 |
| D 公共 WARNING + detail 双写；既有断言反转 | test_catalog_sync.py 修订（:699 反转 + 新增双写断言；:697/:698 不动） | 单元修订 |
| D 其余 catalog 记录仍 detail-only（例外钉死一类） | test_catalog_sync.py 既有 detail-only 用例回归不动 | 回归 |
| verify.py --smart / aixcoding/tests / architecture 全绿 | 回归命令 | 回归 |
| gen_customizations --strict 0/0 | 对账命令（:72 注释修订后） | 检查 |

## 用例清单（test_routing_fixes.py）

| # | 用例 | 构造 | 期待 |
|---|---|---|---|
| 1 | A1 失败进 detail | monkeypatch `get_login_session` 抛 `RuntimeError("boom")`；调 `login_token_header_value()` | detail 捕获：`record.name == "aixcoding.detail.auth"`、消息含「读取登录凭证失败」与「token 请求头未附加」、levelname `DETAIL`、`exc_info` 非空；caplog（主卷代理）零记录；`chrys.kernel.aixcoding.auth` 间谍零分发 |
| 2 | A1 正常未登录零记录 | `get_login_session().stored_token` 返回 `None`（monkeypatch） | 两卷零记录（文案口径的语义锚：None 不进异常分支） |
| 3 | A2 异常耗尽→降级双写 | `_with_factories` 形态注入 `[Exploding]`（单候选） | detail 含「加密后端 … 探测失败」+ 异常文本；公共 WARNING「系统凭据库不可用…重启后需重新登录」；detail 同文案双写；`chrys.kernel.aixcoding.auth` 间谍零 |
| 4 | A2 哨兵 | 同用例 3 | 主卷/公共通道全部记录的 `getMessage()` **不含**后端名（如 `Exploding`）与异常文本 `boom` |
| 5 | A2 往返不一致耗尽 | 注入 `[Garbage]`（protect/unprotect 返回不一致值、不抛异常） | detail 含「往返校验不一致」+ 公共 WARNING + detail 双写（非异常路径独立覆盖，M1 缺口） |
| 6 | A2 健康候选提前返回 | 注入 `[Exploding, MemoryBackend]` | detail 含 Exploding 记录；**两卷零 WARNING**（factory 失败≠降级决策锚定） |
| 7 | A2 probe 成功静默 | 注入 `[MemoryBackend]`（或真 PlaintextBackend 类） | 两卷零记录 |
| 8 | A3 回调失败进 detail | 构造 `_on_login_success` 抛异常，驱动 `_run_login`（复用既有登录测试的 session/dialog 设施或最小化直调路径） | detail 捕获：「登录成功回调失败，继续关闭登录对话框。」+ exc_info；caplog 零；`chrys.app.aixcoding.tui.login` 间谍零 |

用例 8 实施注记：`_run_login` 依赖 Textual 对话框上下文——若直调成本高，最小化方案为提取/直测回调包装段（design 范围内：A3 只改一行调用，测试可退化为对修改后表达式的直接分发断言，实施期定，plan 备忘）。

## test_catalog_sync.py 修订（D）

- `:697`（`retired_name in warnings`）与 `:698`（值不泄漏）**不动**；
- `:699` `assert retired_name not in caplog.text` → 反转：`assert retired_name in caplog.text` 且 `caplog.records` 中存在 `name == "aixcoding.catalog"`、`levelno == WARNING` 的记录（消息为停用警告文案）；
- 新增：detail 捕获同文案双写断言（`_warn_retired_env` 双打的 detail 侧）；
- 既有其余 detail-only 用例零改动（例外范围钉死的回归证明）。

## 异常与降级路径

- A2 三种降级形态（异常/往返不一致/耗尽双写）与两种静默形态（健康候选提前返回/probe 成功）全覆盖——peer review 两轮定案的口径完整锚定；
- A1 exc_info 落 detail（意外异常兜底，catalog 先例口径）；A3 同。

## 验证命令清单

```
uv run python aixcoding/tools/verify.py --smart --paths src/chrys/kernel/aixcoding/auth/__init__.py src/chrys/kernel/aixcoding/auth/crypto.py src/chrys/app/aixcoding/tui/login.py src/chrys/app/aixcoding/tui/login_actions.py src/chrys/service/aixcoding/catalog.py aixcoding/tests/auth/test_routing_fixes.py aixcoding/tests/catalog/test_catalog_sync.py
uv run pytest aixcoding/tests -n0
uv run pytest tests/architecture -n0
uv run python aixcoding/tools/gen_customizations.py --strict
```
