# 测试方案：定制环境配置与上游 Settings 解耦（environment-config）

> 活文档：随时更新、不签认；其质量由双 AI 自审（SELF-REVIEW-HOWTO）与行为锚定测试本身保障。

前置状态：ADR-0022、design 与 self-review 已签认；R2 第 4 项编译 mo 实际路径及 §9 支持手册路径
勘误已由责任人确认；catalog-sync 登记迁移已同步重签。

## 测试范围与策略

- 层级：共享配置、启动组合、auth、模型目录与 diagnostics 以**单元/组件测试**为主；上游 Settings、
  process projection、settings UI 与 i18n 以**行为锚定和生成物门禁**覆盖；真实 mock 服务只用于手工验证。
- 自动化测试不访问真实 dev/prod，不启动真实网络监听。旧环境变量场景仅验证无读取、无检测、无警告，
  不通过旧变量触发任何网络请求。
- 主要落点：
  - 新增 `aixcoding/tests/config/test_config.py`，承接共享 TOML、环境优先级、缓存和容错测试；
  - 改写 `aixcoding/tests/diagnostics/test_config.py` 的文件读取用例并迁入上述 config 套件；
  - 扩展 `aixcoding/tests/diagnostics/test_entry_wiring.py`、`test_logging_support.py`；
  - 扩展 `aixcoding/tests/auth/test_auth_environments.py`；
  - 扩展 `aixcoding/tests/catalog/test_catalog_sync.py`，删除退役 WARNING 旧用例；
  - 跟改 `tests/foundation/i18n/_catalog_oracle_ids.py`、`test_catalog_oracle.py` 及 Smart Test 选中的
    上游 Settings 相关测试。
- 隔离纪律：相关测试分别清理 `load_config.cache_clear()`、auth 注入值和默认登录会话；catalog 既有
  单例/代次隔离保持不变。不把进程级清理扩大为全定制套件 autouse fixture。
- 公共入口锚定：消费方均从 `chrys.service.aixcoding.config` 与 `chrys.app.aixcoding.startup` 导入；
  配置测试验证重导出函数的 `cache_clear()`，启动测试继续 patch `startup._TASKS`。
- 行为锚定纪律：真实调用 `resolve_endpoints()`、`get_login_session()`、`ensure_file_sink()`、上游 Settings
  与 i18n oracle；网络、平台目录、时钟和故障点才可 monkeypatch，禁 mock 被测锚点本身。

## 覆盖矩阵

| 验收标准（requirements.md） | 测试入口（文件::用例） | 类型 |
|---|---|---|
| 1. 新环境变量选择 local，缺省 prod | `test_auth_environments.py::test_environment_variable_selects_tier`、`test_default_environment_is_prod`；`test_catalog_sync.py::test_the_tier_is_auths_tier` | 单元 |
| 2. TOML 环境注入 auth/模型目录，env 优先 | `test_config.py::test_environment_precedence_env_toml_prod`；`test_entry_wiring.py::test_environment_injection_precedes_default_session_construction` | 单元 + 接线 |
| 3. 非法值 WARNING；旧变量完全无处理 | `test_config.py::test_invalid_environment_warns_and_falls_back`、`test_old_environment_names_are_ignored`；auth/catalog 旧变量参数化用例 | 单元 |
| 4. TOML 模型目录 base 替代旧键，完整 URL 最高优先 | `test_catalog_sync.py::test_full_url_env_has_highest_priority`、`test_toml_base_url_appends_catalog_path` | 单元 |
| 5. 旧 Settings 键不再生效，无定制检测 | `test_config.py::test_settings_yaml_is_not_read`；`test_catalog_sync.py::test_old_settings_base_url_is_ignored`；上游 Settings 回归 | 单元 + 行为锚定 |
| 6. 退役变量机制全部移除 | `test_catalog_sync.py::test_retired_environment_names_have_no_effect_or_warning`；删除 `test_retired_env_names_warn_and_do_not_apply` 与新旧双读用例 | 单元 |
| 7. logging 行为保持，坏配置 fail-safe | `test_config.py` 缺文件/坏 TOML/坏类型/坏 level 名用例；`test_logging_support.py` 既有 level/panel、主卷/detail 卷、幂等与容错用例 | 单元 + 组件 |
| 8. 启动注入早于默认 LoginSession，覆盖 main 全入口 | `test_entry_wiring.py::test_environment_injection_precedes_default_session_construction`、`test_main_calls_aixcoding_startup`、`test_startup_task_order_and_single_snapshot` | 接线 + 行为锚定 |
| 9. tier 依赖方向合法，无新增豁免 | `tests/architecture` 全量；`test_wait_manifest_matches_source`；`test_entry_wiring.py::test_startup_placement_and_task_registration` | 架构 |
| 10. 五个 id 退役，C004/C034 保留，strict 全绿 | `gen_customizations.py --strict` 最终对账；`test_main_calls_aixcoding_startup` 与模型目录 TUI 接线锚 | 门禁 + 接线 |
| 11. 定制/上游/Smart Test/i18n/文档通过，mock 手工正常 | 本文最终门禁与手工验证步骤 | 回归 + 手工 |

## 用例清单

### 1. 共享配置子包（`aixcoding/tests/config/test_config.py`）

| 用例 | 构造 | 期待 |
|---|---|---|
| `test_defaults_when_file_missing` | 空 `tmp_path` | environment=prod，diagnostics/model_catalog 默认值，无 WARNING |
| `test_parses_complete_toml` | 写入 environment、logging、model_catalog 三节 | 全字段正确解析，公开类型可由包入口导入 |
| `test_environment_precedence_env_toml_prod` | 分别构造仅缺省、仅 TOML、env+TOML | prod → TOML → env 的优先顺序正确，值经 strip/lower |
| `test_invalid_environment_warns_and_falls_back` | env/TOML 分别写空值、非法值 | 回退 prod，stderr 含 `aixcoding.config` WARNING；非法 env 不再回落 TOML |
| `test_invalid_toml_preserves_environment_variable` | 坏 TOML + 合法 env | logging/model_catalog 默认，env 仍生效，不抛异常 |
| `test_bad_level_name_falls_back` | `[logging] level = "NOISY"` | level 静默回退 `INFO`，不阻断启动 |
| `test_bad_types_fall_back_safely` | logging/model_catalog/environment 使用错误类型 | 对应字段回退并提示，CLI 组合不接收异常 |
| `test_cache_returns_one_snapshot` | 首次读取后改文件，再以相同参数调用 | 返回同一快照；`cache_clear()` 后才读取新文件 |
| `test_public_reexport_and_cache_clear` | 从包入口导入并清缓存 | 与 loader 中函数为同一对象，无第二缓存 |
| `test_settings_yaml_is_not_read` | 同目录写含旧键的 `settings.yaml` | 旧值不进入共享配置 |
| `test_old_environment_names_are_ignored` | 设置全部旧环境变量，不给新配置 | 结果仍为 prod，无读取结果、检测或 WARNING |

### 2. 启动组合与公开入口（`aixcoding/tests/diagnostics/test_entry_wiring.py`）

| 用例 | 构造 | 期待 |
|---|---|---|
| `test_startup_task_order_and_single_snapshot` | 记录两个任务及配置读取调用 | 环境注入先于日志装配；同一缓存快照，文件只解析一次 |
| `test_environment_injection_precedes_default_session_construction` | 清默认 session，记录环境注入和 `LoginSession()` 构造顺序 | 注入先发生；既有 main 接线锚证明其位于 TUI/headless/ACP 分发前 |
| `test_startup_contains_task_failures` | 保留既有 ok/boom/after 任务 | boom 不阻断 after，stderr 降级提示保持 |
| `test_startup_idempotent_when_tasks_are` | 继续 patch `startup._TASKS` | 子包化不破坏既有 patch 方式和幂等语义 |
| `test_startup_placement_and_task_registration` | 读取包入口 `__file__` 与 `_TASKS` | 定位 `startup/__init__.py`，注册环境与日志两个任务，tui 同级包不变 |
| `test_main_calls_aixcoding_startup` | 保留真实 `app.main()` 锚定 | import/调用不变，C004 仅路径注释适配 |

### 3. auth 环境（`aixcoding/tests/auth/test_auth_environments.py`）

- 新变量 `AIXCODING_ENVIRONMENT` 三档选择、大小写/空白归一、非法值 prod 回退；旧
  `AIXCODING_AUTH_ENVIRONMENT` 与 `CHRYS_AUTH_ENVIRONMENT` 不生效。
- `configure_environment()` 注入值在未显式传 `environ` 时生效；显式映射绕过注入，测试间可复位。
- `resolve_endpoints()` 在 `environ=None` 时保留注入值，不得先把 None 归一为 `os.environ` 后绕过注入。
- local/dev/prod 端点表零漂移；`AIXCODING_AUTH_SERVER_URL` 仅替换 origin、保留路径的既有参数化用例全绿。

### 4. 模型目录（`aixcoding/tests/catalog/test_catalog_sync.py`）

- import 与 monkeypatch 全部改到 `chrys.service.aixcoding.models.catalog`；新模块路径由真实消费方接线测试覆盖。
- `catalog_base_url()` 优先级：完整 `AIXCODING_MODEL_CATALOG_URL` > `[model_catalog].base_url` > tier base；
  三者均断言固定 `CATALOG_PATH` 只拼接一次。
- `catalog_tier()` 与 auth 使用同一共享 environment；`_CATALOG_BASES`、合法环境值和 `auth.Environment`
  键集合 drift test 全绿。
- 旧 `model.catalog.base_url` 与 `CHRYS_MODEL_CATALOG_BASE_URL` 不生效；旧 `CHRYS_MODEL_CATALOG_*`、
  `CHRYS_REMOTE_MODEL_CONFIG_URL` 不产生任何定制 WARNING。
- 显式坏 base 的抓取/解析失败沿用现有失败路径：不静默改连 prod、不发布新目录、旧目录保留。
- token、轮询周期、登录联动、目录发布、registry 快照和只读 UI 既有测试全绿。

### 5. diagnostics 与上游删除面

- `ensure_file_sink(config, config_dir=None)` 接收已解析配置；level/panel、主卷/detail 卷、F6 propagate、
  sink 幂等和失败 stderr 行为由既有套件继续锚定。
- kernel diagnostics 不再读取 TOML；service config 构造既有 `DiagnosticsConfig`/`LoggingConfig`，无反向依赖。
- 上游 Settings 字段/spec、process projection、UI 隐藏键删除后，相关上游测试按新契约跟改；旧 YAML 键只服从
  上游既有 unknown-key 通用行为，不新增定制检测。
- i18n extract/update/compile/check 后，pot/po/mo 与 oracle id、计数一致；C040 只读文案保留。

## 异常与降级路径

- 文件缺失不告警；坏 TOML、坏类型、非法 environment 不阻断启动，非法 environment 必须可观测。
- 启动任务继续独立 containment：环境注入失败不阻断日志任务，日志任务失败不阻断 CLI。
- 显式模型目录 base 不可用时不回退 prod，不覆盖既有可用目录。
- 缓存、auth 注入、默认 session 和 catalog runtime 在每用例后复原，避免顺序依赖和跨目录污染。
- 登记工具仅识别无编号 `## 定制点登记预告`。本批次按 F-A 裁决对齐节标题，并与代码 marker、
  catalog-sync 现役行删除同一变更集完成；中间态可因退役与现役并存而 strict 红，终态必须显式达到
  retired=5、0 error / 0 warning，不以此前 0 retired 的假绿代替最终证据。

## 手工验证步骤（结果记入 verification.md）

均由 AI 启动独立可见 cmd 窗口，工作目录使用大写盘符
`D:\Tech\Github\huawei\iCode\fork\iCode-fork_2`；先停止旧 mock 进程并确认 7777 已释放。

1. **环境变量路径**：启动 mock：
   `Start-Process cmd -ArgumentList '/k','uv run python -m aixcoding.mock' -WorkingDirectory 'D:\Tech\Github\huawei\iCode\fork\iCode-fork_2'`；
   再启动 TUI：
   `Start-Process cmd -ArgumentList '/k','set "AIXCODING_ENVIRONMENT=local" && uv run aixcoding-cli' -WorkingDirectory 'D:\Tech\Github\huawei\iCode\fork\iCode-fork_2'`。
2. 预期：7777/7778 端口可连接；登录徽标与 `/login`、`/logout` 正常；登录成功、模型目录同步成功；
   F4 保持禁用；F6 与文件日志行为不回归。
3. **仅 TOML 路径**：在 `~/.chrys/aixcoding.toml` 设置顶层 `environment="local"`，不设置新环境变量，
   用 `set "AIXCODING_ENVIRONMENT=" && uv run aixcoding-cli` 启动独立 TUI，重复登录和模型目录同步验证。
4. **缺省与非法值**：无配置自动化测试已覆盖 prod 默认；非法值 WARNING、旧变量零处理仅做无网络单测，
   不在手工验证中连接真实 prod。

## 最终门禁

1. `uv run python aixcoding/tools/verify.py --smart --paths <本批次全部改动文件>`：完整定制套件 + 上游 Smart Test。
2. `uv run python -m tests.support.trajectory_wait_inventory` 机械刷新 manifest，再运行
   `uv run pytest tests/architecture -n0`：模块路径一致，无向上 tier 依赖、无 kernel 跨子包 import、无新增豁免。
3. 按 AGENTS 运行 ruff、ruff format、三平台 ty 与 `uv run python scripts/i18n.py check`。
4. `uv run python aixcoding/tools/check_docs.py` 与最终的
   `uv run python aixcoding/tools/gen_customizations.py --strict`：C032/C033/C039/C042/C043 退役，
   **retired=5**，C004 保留，0 error / 0 warning。
5. `git diff --check`；逐文件核对 R2 表、C004 路径注释、文档联动和 untracked 新文件均进入同一变更集。
