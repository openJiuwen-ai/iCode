# 测试方案：模型目录同步 + 档案集中管理（catalog-sync）

> 活文档：随时更新、不签认；其质量由双 AI 自审（SELF-REVIEW-HOWTO）与行为锚定测试本身保障。

## 测试范围与策略

- 层级：**单元/组件测试**（同步链全程 stub：monkeypatch `catalog_url` / `sync_catalog_blocking` /
  凭据门，测时钟、代次防护、两阶段发布事务、env 解析、协议路由）+ **UI 门测试**（真实 Textual
  组件、轻量 harness）。M3 mock 目录服务只进手工冒烟（§手工验证），单测层零 mock 服务依赖。
- 落点：新增 `aixcoding/tests/catalog/`（`conftest.py` + `test_catalog_sync.py` +
  `test_read_only_gate.py`）；上游 `tests/app/tui/screens/test_model_picker.py` 跟改（R2 表
  14 号，4 处——三分类口径：红因均精确对应已签认行为差异）。
- conftest（aixcoding/tests/catalog/）：
  - `wait_until` 本地复制（tui conftest 同款，ADR-0001 失败域隔离；**不含** theme
    fixture——本目录唯一 TUI 组件测试在 read_only_gate，走真实 `ModelConfigScreen` 构造）；
  - **单例隔离 autouse fixture**：每用例结束时 `stop_sync_runtime()` + 重置模块级单例
    状态（handle/generation/回调引用）——轮询运行时是进程级单例，跨用例泄漏会污染同文件
    及后续文件的全部时钟类用例，是本套件最大基建风险。
- 行为锚定纪律：上游符号真 import（`ModelProfileRegistry` / `ModelConfigScreen` /
  `OptionList.OptionSelected`），禁 mock 被测物本身；monkeypatch 只打网络/时钟边界。

## 覆盖矩阵

| 验收标准（requirements.md） | 测试入口（文件::用例） | 类型 |
|---|---|---|
| 启动同步整替（目录整替 + state 写入 + registry = catalog） | test_catalog_sync.py::test_sync_chain_applies_catalog_and_installs_snapshot | 组件（stub fetch + 真实 apply） |
| 删除语义（install_snapshot 快照安装） | test_catalog_sync.py::test_install_snapshot_forgets_deleted_profiles | 单元 |
| 发布原子性（故障注入 5 情形 + 启动全吞错） | test_catalog_sync.py::test_publish_fault_injection_*（5 用例）+ test_startup_degrades_on_any_failure | 单元（真实磁盘 tmp_path） |
| 无源/无凭据/同步失败三情形不阻启动 | test_catalog_sync.py::test_without_a_source…/ test_no_credential…/ test_startup_degrades…（平移 + 新增） | 单元 |
| 登录即时同步 + 登出停止（占位符实调） | test_catalog_sync.py::test_login_actions_wire_sync_calls（login_actions 真调断言）+ 代次用例 | 单元 + 接线 |
| 登出后 in-flight 不发布（代次防护） | test_catalog_sync.py::test_stale_generation_result_is_dropped | 单元 |
| UI 集中管理三面（read_only 恒真 + 文案 + F4 + picker） | test_read_only_gate.py（3 用例）+ test_model_picker.py 跟改（R2 14 号） | UI 门 + 跟改 |
| tier 对齐 drift test | test_catalog_sync.py::test_the_tier_is_auths_tier（平移改写） | drift |
| 废弃 env 名检测 | test_catalog_sync.py::test_retired_env_names_warn_and_do_not_apply / test_new_env_names_take_priority | 单元 |
| 套件全绿 + 上游不回归 + `--strict` | 回归命令（§手工验证） | 回归 + 检查 |
| 离线打包冒烟（DoD 项） | 手工验证步骤 4 | 手工 |

## 用例清单

### test_catalog_sync.py（平移 10 + 新增 10）

**平移改写映射**（旧 0.28 用例 → 新单例 API；行为语义逐一保留）：

| 旧用例（203 行版） | 改写点 |
|---|---|
| test_syncs_on_the_clock_and_stops_when_signalled | `start_periodic_sync(interval, on_applied)` → `start_sync_runtime(registry, refresh_ui)`（interval 走 env）；`stop.set()` → `stop_sync_runtime()` |
| test_no_credential_means_no_thread | 同名平移（无凭据不启线程、零 fetch） |
| test_immediate_syncs_before_the_first_sleep | immediate 语义 → 登录触发路径（`start_sync_runtime` 幂等：已运行时等价 `sync_now()`） |
| test_sync_now_pulls_one_catalog_ahead_of_the_clock | `stop.sync_now()` → 模块级 `sync_now()`；断言语义不变（一次请求一次同步） |
| test_the_interval_is_the_environments_or_the_default | env 锚改 `AIXCODING_MODEL_CATALOG_SYNC_INTERVAL`；坏值（"soon"/""/"0"/"-1"）回退 15min 不变 |
| test_the_loop_polls_at_the_environments_interval | 同上 env 改名（override 到达运行中 loop） |
| test_without_a_source_there_is_nothing_to_poll | 同名平移（无源不启线程，目录归用户） |
| test_a_failing_callback_does_not_kill_the_loop | 回调（`on_applied`/refresh_ui）抛异常不杀 loop |
| test_the_tier_is_auths_tier | import 改 `chrys.kernel.aixcoding.auth`；`ENVIRONMENT_ENV` 断言改 `AIXCODING_AUTH_ENVIRONMENT`；tier 基址表/PROD 回退断言不变 |
| test_replace_profiles_forgets_deleted_profiles | **改写为快照安装语义**：registry 预载 a/b → `install_snapshot([a])` → `list_ids()==["a"]`；补断言**不重扫磁盘**（快照语义 vs 旧 `replace_profiles` 锁内重扫——对比六 R-a′ 行为锚定） |

**新增用例**：

| # | 用例 | 构造 | 期待 |
|---|---|---|---|
| 1 | test_sync_chain_applies_catalog_and_installs_snapshot | tmp_path 目录 + stub `catalog_url`/`_catalog_token` + monkeypatch `fetch_catalog` 返回两 profile 的 `ModelCatalog`；真调 `sync_catalog_blocking` | 目录含两 `<id>.yaml` + `.catalog-state.json`；registry `install_snapshot` 后 list_ids 一致；无 tmp 残留 |
| 2 | test_publish_fault_injection_stage_one_write_fails | 预置旧目录（a.yaml）；monkeypatch `_write_profile` 第 1 个 profile 写 tmp 时抛 `OSError` | 旧 a.yaml 完好；无 `.yaml.tmp` 残留（阶段一清理）；返回失败 + 日志留痕；registry 未安装 |
| 3 | test_publish_fault_injection_replace_fails | 预置 a.yaml；monkeypatch `os.replace` 对第 1 个抛 `OSError`（第 2 个正常） | 失败转换不穿透（`ModelCatalogError` 语义返回）；无文件丢失（旧文件在或已被替换）；下轮重试收敛（再跑一次成功） |
| 4 | test_publish_fault_injection_state_write_fails | monkeypatch `write_catalog_state` 抛 `OSError` | 目录 yaml 已新、state 未写（server-owned 标记未变）；返回失败 + 日志；下轮重写 state |
| 5 | test_publish_fault_injection_stale_delete_fails | 预置多余旧文件 stale.yaml（不在新集合）；monkeypatch 删除路径抛 `OSError` | 新集合文件已就位；stale.yaml 残留（窗口声明行为）；下轮幂等清理（再跑一次消失） |
| 6 | test_publish_fault_injection_fetch_and_parse_fail | monkeypatch `fetch_catalog` 分别抛 `httpx` 错误 / `parse_catalog` 抛 `ValueError` | 旧目录原样；返回失败 + 日志；不安装 |
| 7 | test_startup_degrades_on_any_failure | 上述各失败形态下调用启动路径入口（B2 挂点同款：lazy + 全吞错） | 无异常穿透；返回失败/None；旧目录可用（验收："启动不受影响"） |
| 8 | test_retired_env_names_warn_and_do_not_apply | 逐一 setenv 四个退役名（`CHRYS_MODEL_CATALOG_URL`/`_TOKEN`/`_SYNC_INTERVAL`/`CHRYS_REMOTE_MODEL_CONFIG_URL`，各自单独存在） | 各自不生效（URL 回退 tier 基址、TOKEN 回退登录 token、INTERVAL 回退 15min）；caplog 断言 `logger.warning` 只含变量名、不含值 |
| 9 | test_new_env_names_take_priority | 新旧名并存 | 新名生效（URL/TOKEN/INTERVAL 三变量） |
| 10 | test_stale_generation_result_is_dropped | `start_sync_runtime` 后 monkeypatch `sync_catalog_blocking` 返回慢结果（事件控制）；执行中调 `stop_sync_runtime()`（代次递增） | 该轮结果不 `on_applied`、不安装（代次校验丢弃）；新 `start_sync_runtime` 后新代次正常发布 |
| 11 | test_tmp_files_are_not_loaded_as_profiles | 目录放 `a.yaml` + `b.yaml.tmp` + `.catalog-state.json` | `load_profiles_from_dir` 只加载 a（假设 2 的 glob 语义测试锚定——比"实施时人工复核"更强的防线） |
| 12 | test_login_actions_wire_sync_calls | 平移 tui 批次 wiring 形态（`inspect.getsource` + MemoryBackend）：登录成功路径断言实调 catalog 单例即时同步；登出路径断言 `stop_sync_runtime` 实调 | 占位符已替换为实调（D3 兑现的接线锚定） |

### test_read_only_gate.py（平移 1 + 新增 2）

| # | 用例 | 构造 | 期待 |
|---|---|---|---|
| 1 | test_open_model_config_screen_is_always_read_only | 平移旧 `test_config_actions.py:516`（`RuntimeConfigController._open_model_config` + `_View` stub + 真实 `ModelConfigScreen`） | `screen._read_only is True`（idle 态亦然——假设 1 锚定） |
| 2 | test_read_only_notice_uses_central_management_wording | 构造 `ModelConfigScreen`（read_only 态），取 `_READ_ONLY_NOTICE` 渲染 | 文案为集中管理措辞，**反断言**不含 "Agent is running"（R2 表 10 号锚定；po msgstr 联动由 i18n 门禁覆盖） |
| 3 | test_f4_binding_absent_from_main_screen | import `MainScreen.BINDINGS` 静态检查 | 无 `key=="f4"` / action `"models_config"` 的 binding（R2 表 6 号锚定；`check_action` 不受影响已核验） |

### test_model_picker.py 跟改（R2 表 14 号，4 处）

| 处 | 改写 | marker |
|---|---|---|
| ① | manage 正断言 → `pytest.raises(OptionDoesNotExist)` 反断言 | C044 |
| ② | 计数 4/5 → 3/3 + 用例改名（lists_profiles_without_rules_or_manage_action） | （随①） |
| ③ | manage 参数化行**不删**，改合成驱动：构造未注册 manage `Option` + `post_message(OptionList.OptionSelected(option_list, option, index))`，保留 `ModelPickerAction.MANAGE` 路由断言 | C045 |
| ④ | 中文用例删 manage 断言段、保留 `模型` 标题断言 + 改名 | （随①） |

## 故障注入实施细节（design 对比八收敛项，随用例 2-7 定稿）

- **tmp 命名**：`<id>.yaml.tmp`（同卷 replace 前提；loader 不扫描由用例 11 锚定，失效则
  暂存目录外置——design 假设 2 回退路径）；
- **孤儿 tmp 清理**：`apply_catalog` 开始时清理目录内全部 `*.yaml.tmp`（上轮进程中断
  残留，幂等）；阶段一失败即清理本轮已写 tmp；
- **失败恢复边界**：每轮同步全量重写（暂存全集 → replace 全集 → 删差集 → 写 state），
  中断后下一轮完整收敛，无增量/断点续传；
- **权限位**：tmp 写入时随行（replace 保留），不额外 chmod 补写。

## 异常与降级路径

- 全链异常统一转换（评审一-1 修复锚定）：抓取（httpx/ValueError）与发布（OSError 全族）
  → `ModelCatalogError` 语义失败返回 + 日志；用例 6/7 断言无未转换异常穿透启动路径；
- 回调异常隔离：代次防护下 refresh_ui/on_applied 异常不杀 loop（平移用例 8）；
- 退出竞态边界（Q3 定稿口径）：不测"绝对无回调"（不可测），测代次丢弃（用例 10）；
  Textual shutdown 窗口行为属 design 对比三留档，不入测试射程；
- 覆盖缺口声明：RLock 线程安全为结构属性（短临界区零 IO），不做并发注入测试——由
  `install_snapshot` 快照语义用例 + 源码评审覆盖。

## 手工验证步骤（结果记入 verification.md）

1. 大写盘符进入仓库根：`set "PYTHONPATH=" && set "PYTHONUTF8=1" && uv run pytest aixcoding/tests -n0` → 全绿（含 catalog 新套件 + 既有 tui/auth/diagnostics 回归）；
2. 上游套件：`set "PYTHONPATH=" && set "PYTHONUTF8=1" && uv run pytest tests -n 8 -m "not integration and not gc_calibration" --deselect tests/app/test_install_scripts.py` → 全绿（三分类口径：仅 R2 14 号跟改的 4 处行为差异允许变化）；
3. `uv run python aixcoding/tools/gen_customizations.py --strict` → 0 error / 0 warning（C030-C045 对账）；
4. 离线打包冒烟（DoD 项）：`powershell -ExecutionPolicy Bypass -File scripts\build_lf.ps1 -Offline`（三下载物缓存就位）→ 产物 `icode.exe` 启动 TUI → mock 登录（M1）→ 观察启动同步（M3 目录 7777）+ picker 仅 profile 列表 + `/models` 打开恒只读屏（集中管理文案）+ F4 不在 footer；
5. i18n 门禁：`uv run python scripts/i18n.py check`（新 key 四处同步 + read_only_notice 翻译一致性；若上游存在断言翻译内容的测试，一并跑绿——design 风险节在案项）；
6. ruff / format / ty 三平台 `src/chrys` 常规门禁（跟改文件入 `--paths` 一起过 Smart Test：产品文件 + 测试文件双传）。
