# 测试方案：catalog 只读行为正向测试覆盖盘点与补缺（catalog-readonly-coverage）

> 活文档：随时更新、不签认；其质量由双 AI 自审（SELF-REVIEW-HOWTO）与行为锚定测试本身保障。

## 覆盖矩阵（盘点结论落盘——本批次核心资产，后续批次/升级直接引用）

维度 = "catalog 只读管控语义"的正向行为面。证据列标注运行形态（fork 中持续运行）。

| # | 维度 | 证据（文件::用例 / 位置） | 归属批次 | 本批次动作 |
|---|---|---|---|---|
| 1 | 只读真实渲染：notice 显示+集中管理精确文案、表单 Input/Select/Checkbox 禁用、new/clone/delete/save display=False+disabled、kv 行隐藏、导航区仍可用、cancel 保活、press save 不写盘不 reload | `tests/app/tui/screens/test_model_config.py:716`（C047 文案跟改）、`:764`（空 registry 不 seed） | 上游（fork 运行） | 不补（已有渲染级全覆盖） |
| 2 | controller idle 恒只读（入口恒传 `read_only=True`） | `test_read_only_gate.py::test_open_model_config_screen_is_always_read_only` | catalog-sync | 不动 |
| 3 | **controller busy 态恒只读**（上游 `_config_read_only()` 三信号 `agent_running`/`submit.active`/`execution_busy()` 各单 True + 全 True 四态） | 无（C135 models 参数 skip 后真空；运行中 `/models` 仍可达） | **本批次 G1** | **补** |
| 4 | 指示器 configure 入口 → 恒只读屏（真实路由链） | 无（catalog-sync 假设 11 仅登记为升级核对项） | **本批次 G2** | **补** |
| 5 | picker Manage 条目禁用（不渲染、无 separator 残留、合成事件 MANAGE 路由保留） | `test_model_picker.py`（C044/C045 跟改） | 上游（fork 运行）+ catalog-sync | 不补 |
| 6 | F4 禁用（声明层 BINDINGS / footer 渲染精确集合 / keymap 契约 / provenance 集合） | `test_read_only_gate.py::test_f4_binding_absent_from_main_screen` + `test_chrys_footer.py`（C048）+ `test_bindings.py`（C049） | catalog-sync + 上游（fork 运行） | 不补（三层已齐） |
| 7 | notice 常量措辞（fallback 含 centrally managed、不含 Agent is running） | `test_read_only_gate.py::test_read_only_notice_uses_central_management_wording` | catalog-sync | 不动 |
| 8 | zh-Hans 只读文案 | i18n 门禁（`scripts/i18n.py check` + oracle） | catalog-sync 决策在案 | 不补 |
| 9 | registry 整替（install_snapshot 语义、同步链、故障注入、代次） | `test_catalog_sync.py` 全套（40+ 用例） | catalog-sync/catalog-auth-gating | 不补 |
| 10 | registry 整替 × 只读入口端到端组合 | 无（会话语义由 catalog-sync 假设 7 钉死；端到端需真 app+engine） | — | 不补（design 风险节依据） |
| 11 | handler 层 read_only 守卫单测（_on_delete 等） | 无（UI 路径已被维度 1 display=False 挡死，击穿需双变异） | — | 不补（G3，design 风险节依据） |
| 12 | C134 删除链（F4→删→重载→重建） | 上游测试已 C134 skip——我方语义下删除不成立 | upstream-merge-v0.33.0 | 不触碰（另行呈报才可动） |
| 13 | C135 models 参数（send-in-flight 打开只读） | 上游测试已 C135 skip；**维度本体由本批次 G1 等价接管**——C135 的 commands-running 时点落在 `execution_busy()`，G1 四态覆盖 `_config_read_only()` 全部输入面 | upstream-merge-v0.33.0 + 本批次 | G1 补齐 |
| 14 | 运行中指示器点击 | 上游语义 `execution_busy()` 早退静默忽略（`config_actions.py:161-162`） | 上游 | 不补（上游域，非只读维度） |
| 15 | picker MANAGE 消费侧接线（dismiss → open_model_config） | 无（C045 锚定 picker 侧，消费侧空白） | — | 不补（G2b 可选项，design 留档，默认不做） |

## 用例清单（全部落 `aixcoding/tests/catalog/test_read_only_gate.py`，零新文件）

| # | 用例 | 构造 | 期待 | 变异验证 |
|---|---|---|---|---|
| 0 | （重构）提取 `_make_controller(agent_running, execution_busy, submit_active, registry) -> (controller, pushed, captured)` helper | T1 的 `_View/_Run/_State/_Services` stub 原样上移 + `submit.active` 注入点；`run_worker` 改**捕获协程对象**到返回值 `captured`（评审 B1：裸 `send(None)` 推进到 `to_thread` 挂起点后无人唤醒）；T1 改用 helper，断言零变化（多余返回值 `_` 解包） | T1 原断言不变 | —（纯构造重构，T1 原样绿） |
| 1 | `test_open_model_config_screen_is_read_only_while_busy`（参数化 4 case：agent_running-only / execution_busy-only / submit-active-only / all-three） | helper 对应态 + monkeypatch `get_global_default_profile_id`（helper 内含）→ `await controller._open_model_config()` | `pushed[0]` 为 `ModelConfigScreen` 且 `_read_only is True` | V1：`read_only=not self._config_read_only()`（判定取反——idle 仍只读、任一 busy 信号可编辑）→ idle 用例绿 + 4 case 全红；还原后全绿 |
| 2 | `test_model_indicator_configure_entry_opens_read_only`（async） | helper idle 态 → `controller.on_model_tag_clicked("configure")` → `open_model_config()` 真实执行（防重入/registry 检查）并将协程交 `run_worker` 捕获 → **`await captured[0]`**（事件循环内完整驱动 `to_thread` 与推屏，评审 B1 修复） | 推入屏为 `ModelConfigScreen` 且 `_read_only is True` | V2：删 `case "configure"` → 红；还原 → 绿 |

行为锚定纪律：真实调用 `RuntimeConfigController` 公开/私有方法链（不 mock 被测门）；
monkeypatch 仅打 `env_bridge.get_global_default_profile_id` 边界（T1 既有手法）；不 import
`tests/support/`（ADR-0001）；复用目录 conftest（单例重置 + 登录预验证，本套件不触同步链，
autouse 无害）。

## 验证命令（Windows 11 本地配方：`set "PYTHONPATH=" && set "PYTHONUTF8=1"` + 大写盘符）

1. 定向：`uv run pytest aixcoding/tests/catalog/test_read_only_gate.py -n0 -q` → 全绿（既有 3 + 新 2，参数化展开计 8）；
2. 全定制套件：`uv run pytest aixcoding/tests -n0 -q` → 全绿；
3. ruff：`uv run ruff check aixcoding/tests` + `uv run ruff format --check aixcoding/tests`（目录级豁免配置在案）；
4. `uv run python aixcoding/tools/gen_customizations.py --strict` → 98 行 / 135 marker / 0 error / 0 warning（不变）；
5. `uv run python aixcoding/tools/check_docs.py` → 新批次文档结构合法；
6. 变异验证 V1/V2（手工，**临时源码修改按 design"变异验证授权边界"节执行**，逐条记录变异 diff 与红面输出 → 还原 → 复绿 → `git diff --stat src/chrys/app/tui/screens/main/config_actions.py` 零输出，四步证据入 verification）；
7. CI（PR checks）：aixcoding-tests.yml 全量 + Upstream CI（零上游交付改动，预期无影响）。

## 异常与降级路径

- 本批次零交付产品代码，无运行时降级路径；
- 测试对假设 3/4 的依赖失效时（上游重构）表现为显式红，属防线预期行为，跟改一行级；
- 变异验证中若 V1 不能使 4 case 全红（且 idle 用例保持绿）或 V2 不能使 G2 红 → 测试设计
  缺陷，回炉 test-plan 重审断言强度，不带病交付；
- 变异操作若发现需超出授权清单的源码改动（行号漂移等），停下重新呈报。
