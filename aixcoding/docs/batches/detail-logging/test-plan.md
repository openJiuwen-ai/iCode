# 测试方案：detail 仅文件日志子树（独立明细卷）

> 活文档：随时更新、不签认；其质量由双 AI 自审（SELF-REVIEW-HOWTO）与行为锚定测试本身保障。

## 测试范围与策略

- 层级：**单元测试**（经真实 logger 树分发，禁直调 `handler.emit`——第二轮评审确认 emit 不经 `callHandlers` 级别检查，直调验证不了分流）；零上游接触故无上游行为锚定项；契约锚定 = detail 命名空间与 `propagate` 语义（`record.name == "aixcoding.detail.auth"`、levelname 渲染 `DETAIL`）。
- 落点：新增 `aixcoding/tests/diagnostics/test_detail_channel.py`；改造 `aixcoding/tests/auth/test_session_logging.py`（#1/#5/#8 三条 ehr 断言迁移）；扩展 `aixcoding/tests/diagnostics/conftest.py` 的 `aix_logger_state` fixture（保存/恢复 `aixcoding` 与 `aixcoding.detail` 两 logger 的 level/propagate/handlers，防跨测试顺序污染）。
- caplog 边界：caplog 的 handler 挂 root，detail 记录 `propagate=False` **捕获不到**——detail 断言两种方式：①`setup_logging(config_dir=tmp_path)` 真建独立卷后读文件内容（端到端）；②测试内临时 handler 直挂 `aixcoding.detail` 子树（捕获形态断言）。普通通道断言策略不变（caplog 照用）。
- root 间谍 handler：测试内给 root 挂记录型 handler（level 不设），模拟"上游 debug 队列 handler 未设级别"的实测环境——detail 记录不得出现在其中（隔离的核心断言）；同时挂真实 `_TuiLogHandler` 实例（import 上游符号，真调不 mock——锚定其缓冲行为）。
- 复用：`test_session_logging.py` 既有 `MockServer`/`make_session` 设施不动；部分失败用 monkeypatch `_DetailMarker` 构造抛 `OSError` 注入。

## 覆盖矩阵

| 验收标准（requirements.md） | 测试入口（文件::用例） | 类型 |
|---|---|---|
| DETAIL 记录只进独立卷，不进面板/debug/root handler/主卷 | test_detail_channel.py::test_detail_record_isolated（真实分发 + root 间谍 + `_TuiLogHandler` 实例 + 读两卷文件断言） | 单元（含端到端文件断言） |
| 普通 INFO 回归（panel 双态） | test_detail_channel.py::test_normal_info_panel_true / test_normal_info_panel_false（主卷落盘 + 面板缓冲有/无） | 单元 |
| ehr 分离：#1/#5/#8 面板与主卷无 ehr、独立卷有 | test_session_logging.py 改造（面板版 caplog 断言 + 反断言 ehr 值；detail 版读独立卷断言含 ehr） | 单元 |
| 其余 9 条埋点零改动 | test_session_logging.py 既有用例不动（回归即证） | 回归 |
| 双 sink 六不变量 + 部分失败四态 | test_detail_channel.py::test_setup_idempotent（双成/不重复挂载/属性稳定）、test_partial_failure_main_ok_detail_fails、test_partial_failure_main_fails_detail_ok、test_partial_failure_retry（重试补建） | 单元 |
| pytest aixcoding/tests 全绿 | 全目录回归命令 | 回归 |
| tests/architecture 全绿（auth 直写零依赖不被破坏） | architecture 回归命令 | 回归 |
| gen_customizations 幂等（零上游 marker） | 对账命令连跑两次 | 检查 |

## 用例清单（test_detail_channel.py）

| # | 用例 | 构造 | 期待 |
|---|---|---|---|
| 1 | detail 记录隔离 | fixture 预置（setup_logging, tmp_path）+ root 挂间谍 handler 与真实 `_TuiLogHandler`；`getLogger("aixcoding.detail.auth").log(DETAIL, "detail line")` | 独立卷含 `DETAIL aixcoding.detail.auth: detail line`；主卷无此行；间谍 handler 无；`_TuiLogHandler` 快照 total=0 |
| 2 | levelname 渲染 | 同上分发 | 独立卷行含字面 `DETAIL`（addLevelName 生效） |
| 3 | 不串卷（双向） | 分发一条 INFO（`aixcoding.auth`）+ 一条 DETAIL | INFO 只进主卷、DETAIL 只进独立卷 |
| 4 | 普通 INFO 双态回归 | panel=True / panel=False 各分发一条 INFO | panel=True：主卷+`_TuiLogHandler` 缓冲有；panel=False：主卷有、面板无（root 间谍照收——普通通道既有行为） |
| 5 | 幂等 | 两次 `setup_logging` | 两 sink 各恰好一个实例（`_Marker`/`_DetailMarker` 计数）、两卷路径不变、属性稳定（`propagate=False`、`level=DETAIL`） |
| 6 | 部分失败：主成 detail 败 | monkeypatch detail sink 构造抛 OSError | 主卷正常可用；detail 子树仍 `propagate=False`+`level=DETAIL`；分发 DETAIL 后间谍无（丢弃不冒泡）；stderr note 可查（capsys） |
| 7 | 部分失败：主败 detail 成 | monkeypatch 主 sink 构造抛 OSError（对称） | 独立卷正常可用；主通道照旧降级（stderr note）；互不牵连 |
| 8 | 重试补建 | 先 monkeypatch detail 败 → 恢复 → 再次 `setup_logging` | detail sink 补建成功、后续 DETAIL 落卷 |
| 9 | 完全未预置降级 | 不跑 setup（fixture 隔离后子树纯净）；直接 `log(DETAIL, ...)` | root 间谍 handler 无记录（level 门槛沿树抑制，fail-safe 静默）；不抛异常 |
| 10 | DETAIL 常量幂等注册 | 两次 setup 后 `logging.getLevelName(5)` | `"DETAIL"` |

## ehr 断言迁移（test_session_logging.py，#1/#5/#8）

- 面板版：既有 caplog 用例改造——消息全文改为去 ehr 版本（`已登录（桌面端登录）；env=local` 等），并加**反断言**：构造可识别 ehr 值（如 `ehr=9990001`），断言其不出现在任何 `aixcoding.auth` 记录的 `getMessage()`；
- detail 版：测试内预置 detail 子树（tmp_path sink 或直挂捕获 handler），断言对应记录含 `ehr=9990001` 且 `record.name == "aixcoding.detail.auth"`、levelname 为 `DETAIL`；
- 其余 9 条用例与全部公共反断言（token 红线等）零改动。

## 异常与降级路径

- 部分失败四态（六不变量 ①②③⑤）：用例 6/7/8——单 sink 失败不牵连另一个、缺失可补建、失败态保持隔离丢弃（不回退普通通道、不冒泡）；
- 完全未预置（六不变量外的既有边界）：用例 9——level 门槛抑制，静默无输出；
- 初始化顺序（不变量 ⑥ 先 propagate 后 level）：以用例 5 的属性终态断言承载（`propagate is False` 且 `level == DETAIL`）；顺序本身无法在单线程测试中观测初始化间隙，依赖源码评审覆盖——test-plan 如实声明此为覆盖缺口及理由；
- handler 故障不扰主流程：标准库契约（`Handler.handleError` → stderr），不为标准库行为写注入测试（auth-logging 先例：测标准库≠测我方代码）。

## 手工验证步骤（结果记入 verification.md）

1. 大写盘符进入仓库根后：`set "PYTHONPATH=" && set "PYTHONUTF8=1" && uv run pytest aixcoding/tests -n0` → 全绿（含既有 diagnostics/auth 全量回归）；
2. `set "PYTHONPATH=" && set "PYTHONUTF8=1" && uv run pytest tests/architecture -n0` → 全绿；
3. `uv run python aixcoding/tools/gen_customizations.py` 连跑两次，`git diff aixcoding/docs/CUSTOMIZATIONS.md` → 空；
4. 本机 `%APPDATA%\chrys\aixcoding.toml` 已是 `panel=true`：`uv run icode` 启动 TUI 登录 → F6 "aixcoding" tab 见 `登录成功；env=local`（无 ehr）→ 查主卷 `aixcoding-logs/aixcoding.log` 同条无 ehr、独立卷 `aixcoding-logs/aixcoding.detail.log` 含 `ehr=...` 且 levelname 为 `DETAIL`（UTF-8 中文正常）。
